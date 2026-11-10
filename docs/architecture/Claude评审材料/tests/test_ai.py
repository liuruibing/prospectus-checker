import asyncio
import json

from app import ai


def _omp_event(role, text, *, phase=None):
    message = {"role": role, "content": [{"type": "text", "text": text}]}
    if phase:
        message["textSignature"] = json.dumps({"phase": phase})
    return json.dumps({"type": "message_end", "message": message})


def test_omp_response_uses_only_final_assistant_text():
    output = "\n".join([
        _omp_event("custom", "unrelated local context"),
        _omp_event("assistant", "draft"),
        _omp_event("assistant", '{"ok":true}', phase="final_answer"),
    ])

    assert ai._omp_response_text(output) == '{"ok":true}'


def test_omp_provider_uses_isolated_one_shot_cli_and_removes_prompt_file(monkeypatch):
    provider = ai.OMPProvider()
    provider.command = "omp-test"
    monkeypatch.setattr(ai.shutil, "which", lambda command: "/mock/omp" if command == "omp-test" else None)
    seen = {}

    class Process:
        returncode = 0

        async def communicate(self):
            path = seen["prompt_path"]
            seen["prompt"] = path.read_text(encoding="utf-8")
            return _omp_event("assistant", '{"ok":true}', phase="final_answer").encode(), b""

    async def start_process(*args, **kwargs):
        seen["args"] = args
        seen["prompt_path"] = ai.Path(args[-1][1:])
        seen["flags"] = args
        return Process()

    monkeypatch.setattr(ai.asyncio, "create_subprocess_exec", start_process)
    result = asyncio.run(provider.generate_text("system", "large private prompt"))

    assert result == '{"ok":true}'
    assert seen["prompt"] == "large private prompt"
    assert all(flag in seen["flags"] for flag in ("--no-session", "--no-tools", "--no-extensions", "--no-context-files"))
    assert not seen["prompt_path"].exists()


def test_make_provider_selects_omp_from_environment(monkeypatch):
    monkeypatch.setenv("PROSPECTUS_LLM_PROVIDER", "omp")

    assert isinstance(ai.make_provider(), ai.OMPProvider)


def test_langchain_limits_parallel_requests_and_records_per_request_metrics(monkeypatch):
    monkeypatch.setenv("PROSPECTUS_MODEL_CONCURRENCY", "2")
    provider = ai.LangChainProvider()
    active, peak = 0, 0

    class Model:
        async def ainvoke(self, messages):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return type("Reply", (), {"content": messages[-1][1], "usage_metadata": {"output_tokens": 1},
                                      "response_metadata": {"finish_reason": "stop"}})()

    monkeypatch.setattr(provider, "build_model", lambda: Model())

    async def run():
        return await asyncio.gather(*(provider.generate_text("system", str(i)) for i in range(6)))

    result = asyncio.run(run())
    assert peak == 2
    assert list(result) == [str(i) for i in range(6)]
    assert all(value.metrics["usage"]["output_tokens"] == 1 for value in result)
    assert result[-1].metrics["queue_ms"] > 0
