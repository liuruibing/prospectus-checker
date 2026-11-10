from __future__ import annotations

import json
import os
import shutil
import tempfile
import asyncio
import time
from pathlib import Path
from typing import Any

from langchain_openai import ChatOpenAI


class ModelText(str):
    metrics: dict[str, Any]

    def __new__(cls, text: str, metrics: dict[str, Any]):
        value = super().__new__(cls, text)
        value.metrics = metrics
        return value


class LangChainProvider:
    """OpenAI-compatible model adapter using the AI-base LangChain client."""

    def __init__(self) -> None:
        self.base_url = os.getenv("LLM_BASE_URL", "").strip().rstrip("/")
        self.api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY", "")
        self.model_name = os.getenv("LLM_MODEL", "").strip()
        self.reasoning_effort = os.getenv("LLM_REASONING_EFFORT", "").strip() or None
        self.timeout = float(os.getenv("PROSPECTUS_LLM_TIMEOUT_SECONDS", "360"))
        self._model = None
        self._slots = asyncio.Semaphore(max(1, int(os.getenv("PROSPECTUS_MODEL_CONCURRENCY", "3"))))
        self._start_lock = asyncio.Lock()
        self._last_start = 0.0
        self.request_interval = max(0.0, float(os.getenv("LLM_REQUEST_INTERVAL_SECONDS", "0")))

    @property
    def cache_namespace(self) -> dict[str, Any]:
        return {"provider": type(self).__name__, "url": self.base_url, "model": self.model_name,
                "temperature": 0, "reasoning_effort": self.reasoning_effort}

    def require_configured(self) -> None:
        missing = [
            name for name, value in
            (("LLM_BASE_URL", self.base_url), ("LLM_MODEL", self.model_name))
            if not value
        ]
        if missing:
            raise RuntimeError("AI_NOT_CONFIGURED: set " + ", ".join(missing) + " in .env")

    def build_model(self) -> ChatOpenAI:
        self.require_configured()
        if self._model is not None:
            return self._model
        self._model = ChatOpenAI(
            model=self.model_name,
            base_url=self.base_url,
            api_key=self.api_key or "not-required",
            temperature=0,
            timeout=self.timeout,
            max_retries=int(os.getenv("LLM_NETWORK_RETRIES", "1")),
            **({"reasoning_effort": self.reasoning_effort} if self.reasoning_effort else {}),
        )
        return self._model

    async def generate_text(self, system_prompt: str, user_prompt: str) -> str:
        queued = time.monotonic()
        async with self._slots:
            async with self._start_lock:
                delay = self.request_interval - (time.monotonic() - self._last_start)
                if delay > 0:
                    await asyncio.sleep(delay)
                self._last_start = time.monotonic()
            started = time.monotonic()
            response = await asyncio.wait_for(self.build_model().ainvoke([
                ("system", system_prompt),
                ("user", user_prompt),
            ]), timeout=self.timeout)
        content: Any = getattr(response, "content", "")
        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        if not text.strip():
            raise RuntimeError("AI_INVALID_RESPONSE: empty message content")
        metadata = getattr(response, "response_metadata", {}) or {}
        return ModelText(text, {
            "queue_ms": round((started - queued) * 1000),
            "request_ms": round((time.monotonic() - started) * 1000),
            "usage": getattr(response, "usage_metadata", None),
            "finish_reason": metadata.get("finish_reason"),
        })


class OMPProvider:
    """Run a one-shot OMP request without tools, extensions, or saved sessions."""

    def __init__(self) -> None:
        self.command = os.getenv("PROSPECTUS_OMP_COMMAND", "omp").strip() or "omp"
        self.model_name = os.getenv("PROSPECTUS_OMP_MODEL", "openai-codex/gpt-6-luna").strip()
        self.timeout = float(os.getenv("PROSPECTUS_LLM_TIMEOUT_SECONDS", "360"))

    def require_configured(self) -> None:
        if not shutil.which(self.command):
            raise RuntimeError(f"AI_NOT_CONFIGURED: OMP command not found: {self.command}")
        if not self.model_name:
            raise RuntimeError("AI_NOT_CONFIGURED: set PROSPECTUS_OMP_MODEL")

    async def generate_text(self, system_prompt: str, user_prompt: str) -> str:
        self.require_configured()
        prompt_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", prefix="prospectus-omp-", suffix=".txt", delete=False,
            ) as prompt_file:
                prompt_file.write(user_prompt)
                prompt_path = Path(prompt_file.name)

            command = [
                self.command,
                "--print",
                "--no-session",
                "--no-tools",
                "--no-extensions",
                "--no-context-files",
                "--mode", "json",
                "--model", self.model_name,
                "--system-prompt", system_prompt,
                f"@{prompt_path}",
            ]
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self.timeout)
            except asyncio.TimeoutError as exc:
                process.kill()
                await process.communicate()
                raise RuntimeError(f"AI_TIMEOUT: OMP exceeded {self.timeout:g} seconds") from exc
            if process.returncode:
                detail = stderr.decode("utf-8", errors="replace").strip()[-1000:]
                raise RuntimeError(f"OMP_REQUEST_FAILED: {detail or process.returncode}")
            return _omp_response_text(stdout.decode("utf-8", errors="replace"))
        finally:
            if prompt_path is not None:
                prompt_path.unlink(missing_ok=True)


def _omp_response_text(output: str) -> str:
    assistant_text: list[str] = []
    final_text = ""
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = event.get("message", {})
        if event.get("type") != "message_end" or message.get("role") != "assistant":
            continue
        texts = [
            part["text"] for part in message.get("content", [])
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ]
        if not texts:
            continue
        text = "".join(texts).strip()
        assistant_text.append(text)
        signature = message.get("textSignature")
        try:
            is_final = json.loads(signature).get("phase") == "final_answer"
        except (TypeError, json.JSONDecodeError):
            is_final = False
        if is_final:
            final_text = text
    result = final_text or (assistant_text[-1] if assistant_text else "")
    if not result:
        raise RuntimeError("AI_INVALID_RESPONSE: OMP returned no assistant text")
    return result


def make_provider() -> LangChainProvider | OMPProvider:
    provider_name = os.getenv("PROSPECTUS_LLM_PROVIDER", "langchain").strip().casefold()
    if provider_name == "omp":
        return OMPProvider()
    if provider_name in {"langchain", "openai-compatible"}:
        return LangChainProvider()
    raise ValueError(f"AI_PROVIDER_INVALID: unsupported provider {provider_name!r}")
