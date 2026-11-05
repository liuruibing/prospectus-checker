from __future__ import annotations

import json
import os
import shutil
import tempfile
import asyncio
from pathlib import Path
from typing import Any

from langchain_openai import ChatOpenAI


class LangChainProvider:
    """OpenAI-compatible model adapter using the AI-base LangChain client."""

    def __init__(self) -> None:
        self.base_url = os.getenv("LLM_BASE_URL", "").strip().rstrip("/")
        self.api_key = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY", "")
        self.model_name = os.getenv("LLM_MODEL", "").strip()
        self.timeout = float(os.getenv("PROSPECTUS_LLM_TIMEOUT_SECONDS", "180"))

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
        return ChatOpenAI(
            model=self.model_name,
            base_url=self.base_url,
            api_key=self.api_key or "not-required",
            temperature=0,
            timeout=self.timeout,
            max_retries=int(os.getenv("LLM_NETWORK_RETRIES", "3")),
        )

    async def generate_text(self, system_prompt: str, user_prompt: str) -> str:
        response = await self.build_model().ainvoke([
            ("system", system_prompt),
            ("user", user_prompt),
        ])
        content: Any = getattr(response, "content", "")
        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        if not text.strip():
            raise RuntimeError("AI_INVALID_RESPONSE: empty message content")
        return text


class OMPProvider:
    """Run a one-shot OMP request without tools, extensions, or saved sessions."""

    def __init__(self) -> None:
        self.command = os.getenv("PROSPECTUS_OMP_COMMAND", "omp").strip() or "omp"
        self.model_name = os.getenv("PROSPECTUS_OMP_MODEL", "openai-codex/gpt-6-luna").strip()
        self.timeout = float(os.getenv("PROSPECTUS_LLM_TIMEOUT_SECONDS", "180"))

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

            process = await asyncio.create_subprocess_exec(
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
