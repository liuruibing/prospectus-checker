import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException

import app.main as main


def _setup_public_rule_update(monkeypatch, tmp_path, *, fail_compile=False):
    events = []
    current = {"content": "# 当前公共规则\n", "sha256": "a" * 64}
    modules = []
    for index in range(3):
        path = tmp_path / f"module_{index}.md"
        path.write_text(f"# 模块 {index}\n", encoding="utf-8")
        modules.append({"file": path})

    monkeypatch.setattr(main, "MODULE_RULES", modules)
    monkeypatch.setattr(main, "get_rule", lambda scope, filename: current)
    monkeypatch.setattr(main, "load_compiled_rule", lambda path, source: f"旧执行规则：{path.name}")
    monkeypatch.setattr(main, "_validate_execution_markdown", lambda *args, **kwargs: None)

    async def compile_one(**kwargs):
        filename = kwargs["filename"]
        events.append(("compile", filename))
        if fail_compile and filename == "module_1.md":
            raise main.RuleCompilationError("模拟单模块编译失败")
        return {
            "execution_markdown": "内部规则",
            "public_rules_sha256": "b" * 64,
            "protocol_version": "test",
            "compiled_at": f"time-{filename}",
        }

    monkeypatch.setattr(main, "compile_rule", compile_one)
    monkeypatch.setattr(main, "persist_compiled_rule", lambda scope, filename, plan: events.append(("stage", filename)))
    monkeypatch.setattr(main, "save_rule", lambda scope, filename, content, expected: events.append(("activate", filename)) or {
        "scope": scope, "filename": filename, "content": content, "sha256": "c" * 64,
    })
    return events


def test_public_rule_update_stages_all_dependent_plans_before_activation(monkeypatch, tmp_path):
    events = _setup_public_rule_update(monkeypatch, tmp_path)
    payload = main.RuleUpdatePayload(content="# 新公共规则\n", expected_sha256="a" * 64)

    saved = asyncio.run(main.update_rule_file("investment", "common_公共规则.md", payload))

    assert [event[0] for event in events] == ["compile"] * 3 + ["stage"] * 3 + ["activate"]
    assert saved["execution_version"]["compiled_rules"] == 3


def test_public_rule_compile_failure_does_not_stage_or_activate_any_plan(monkeypatch, tmp_path):
    events = _setup_public_rule_update(monkeypatch, tmp_path, fail_compile=True)
    payload = main.RuleUpdatePayload(content="# 新公共规则\n", expected_sha256="a" * 64)

    with pytest.raises(HTTPException) as raised:
        asyncio.run(main.update_rule_file("investment", "common_公共规则.md", payload))

    assert raised.value.status_code == 422
    assert [event[0] for event in events] == ["compile", "compile"]
