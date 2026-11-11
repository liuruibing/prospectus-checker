import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException

import app.main as main
import app.rule_compiler as rule_compiler
import app.rule_store as rule_store


def _setup_public_rule_update(monkeypatch, tmp_path, *, fail_compile=False):
    events = []
    current = {"content": "# 当前公共规则\n", "sha256": "a" * 64}
    scope_dir = tmp_path / "rules" / "investment"
    scope_dir.mkdir(parents=True)
    (scope_dir / "common_公共规则.md").write_text("# 当前公共规则\n", encoding="utf-8")
    for index in range(3):
        path = scope_dir / f"module_{index}.md"
        path.write_text(f"# 模块 {index}\n", encoding="utf-8")

    monkeypatch.setattr(main, "ROOT", tmp_path)
    monkeypatch.setattr(main, "get_rule", lambda scope, filename: current)

    async def compile_one(**kwargs):
        filename = kwargs["filename"]
        events.append(("compile", filename))
        if fail_compile and filename == "module_1.md":
            raise main.RuleCompilationError("模拟单模块编译失败")
        return {
            "execution_plan": {"rule_identity": filename},
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


def test_module_rule_update_compiles_prose_before_activating_matching_source(monkeypatch, tmp_path):
    events = []
    scope_dir = tmp_path / "rules" / "product_summary"
    scope_dir.mkdir(parents=True)
    (scope_dir / "custom_metric.md").write_text("# 旧规则\n", encoding="utf-8")
    current = {"content": "# 旧规则\n", "sha256": "a" * 64}
    updated = """# 自定义指标

## 核对对象
核对该指标的披露内容。

## 查找内容
在相关章节和完整表格中查找。

## 来源与适用范围
按规则描述的来源和适用范围核实。

## 比较方法
按同一对象和口径比较。

## 需要复核的情况
证据或适用范围无法确认时待复核。
"""
    monkeypatch.setattr(main, "ROOT", tmp_path)
    monkeypatch.setattr(main, "get_rule", lambda scope, filename: current)

    async def compile_one(**kwargs):
        events.append(("compile", kwargs["source_content"]))
        return {
            "execution_plan": {"rule_identity": "custom_metric"},
            "protocol_version": "test",
            "source_sha256": "b" * 64,
            "model": "test-luna",
            "compiled_at": "test-time",
        }

    def persist(scope, filename, plan):
        events.append(("stage", filename, plan["source_sha256"]))

    def activate(scope, filename, content, expected_sha256):
        events.append(("activate", filename, content, expected_sha256))
        return {"scope": scope, "filename": filename, "content": content, "sha256": "b" * 64}

    monkeypatch.setattr(main, "compile_rule", compile_one)
    monkeypatch.setattr(main, "persist_compiled_rule", persist)
    monkeypatch.setattr(main, "save_rule", activate)

    saved = asyncio.run(main.update_rule_file(
        "product_summary", "custom_metric.md",
        main.RuleUpdatePayload(content=updated, expected_sha256="a" * 64),
    ))

    assert [event[0] for event in events] == ["compile", "stage", "activate"]
    assert events[0][1] == updated
    assert events[2][2:] == (updated, "a" * 64)
    assert saved["sha256"] == "b" * 64
    assert saved["execution_version"]["source_sha256"] == "b" * 64


def test_saved_rule_refresh_keeps_old_execution_version_for_existing_task(monkeypatch, tmp_path):
    rules_root = tmp_path / "rules"
    summary_dir = rules_root / "product_summary"
    summary_dir.mkdir(parents=True)
    compiled_root = tmp_path / "var" / "compiled_rules"
    monkeypatch.setattr(main, "ROOT", tmp_path)
    monkeypatch.setattr(rule_store, "RULE_ROOT", rules_root)
    monkeypatch.setattr(rule_compiler, "ROOT", tmp_path)
    monkeypatch.setattr(rule_compiler, "COMPILED_ROOT", compiled_root)

    filename = "management_fee.md"
    source_v1 = """# 管理费率

## 核对对象
核对产品资料概要披露的管理费年费率。

## 查找内容
查找双方适用的费用章节。

## 来源与适用范围
定期报告优先，招募说明书仅在规则允许时补充。

## 比较方法
在同一基金及适用条件下比较年费率。

## 特殊披露
区分年费率与实际发生金额。

## 需要复核的情况
来源或适用时间无法确认时待复核。
"""
    source_v2 = source_v1.replace(
        "查找双方适用的费用章节。",
        "优先查找定期报告的管理费及管理人报酬章节。",
    )
    rule_path = summary_dir / filename
    rule_path.write_text(source_v1, encoding="utf-8")

    def execution_plan(section_hint):
        return {
            "protocol_version": 2,
            "rule_identity": "management_fee",
            "rule_scope": "product_summary",
            "display_title": "管理费率",
            "business_group": "费用",
            "target_role": "product_summary",
            "reference_roles": ["periodic_report", "prospectus"],
            "search_intent": {
                "target_description": "基金管理费年费率",
                "preferred_sections": [section_hint],
                "preferred_sections_by_role": {},
                "include_continuations": True,
            },
            "source_selection": {
                "primary_role": "periodic_report",
                "fallbacks": [{"role": "prospectus", "when": [{
                    "fact": "primary.status", "operator": "equals", "value": "CONFIRMED_ABSENT",
                }]}],
            },
            "row_matching": {
                "identity_description": "同一管理费率事实",
                "identity_fields": ["fund_id", "fee_role"],
                "scope_fields": [],
                "hierarchy_fields": [],
                "fallback_identity_fields": [],
                "target_population": "both_documents",
                "reference_superset_allowed": False,
                "require_complete_source_for_missing": True,
            },
            "comparison": {
                "identity_description": "按规则核对管理费年费率",
                "scope_conditions": [],
                "context_checks": [],
                "fields": [{"field": "annual_rate", "label": "年费率", "operation": "decimal", "required": True}],
            },
            "evidence_requirements": ["保留双方原文和 PDF 定位"],
            "review_conditions": ["适用条件无法确认时待复核"],
        }

    initial_plan = rule_compiler.validate_execution_plan(
        execution_plan("基金费用"), expected_identity="management_fee",
    )

    def artifact(source, plan):
        return {
            "execution_plan": plan,
            "source_sha256": rule_compiler._sha(source),
            "public_rules_sha256": rule_compiler._public_rule_hash("product_summary"),
            "protocol_version": rule_compiler.PROTOCOL_VERSION,
            "prompt_version": rule_compiler.PROMPT_VERSION,
            "model": "test-fixture",
            "compiled_at": "test-time",
        }

    rule_compiler.persist_compiled_rule(
        "product_summary", filename, artifact(source_v1, initial_plan),
    )
    old_task_rule_set = rule_compiler.capture_rule_set("product_summary")

    async def compile_one(**kwargs):
        plan = rule_compiler.validate_execution_plan(
            execution_plan("基金管理费及管理人报酬"), expected_identity="management_fee",
        )
        return artifact(kwargs["source_content"], plan)

    monkeypatch.setattr(main, "compile_rule", compile_one)
    saved = asyncio.run(main.update_rule_file(
        "product_summary", filename,
        main.RuleUpdatePayload(
            content=source_v2,
            expected_sha256=rule_store._file_hash(source_v1),
        ),
    ))

    refreshed_rule = rule_store.get_rule("product_summary", filename)
    current_plan = rule_compiler.load_compiled_execution_plan(rule_path)
    prior_plan = rule_compiler.load_compiled_execution_plan(rule_path, source_v1)
    new_task_rule_set = rule_compiler.capture_rule_set("product_summary")

    assert saved["content"] == source_v2
    assert refreshed_rule["sha256"] == rule_store._file_hash(source_v2)
    assert current_plan["search_intent"]["preferred_sections"] == ["基金管理费及管理人报酬"]
    assert prior_plan["search_intent"]["preferred_sections"] == ["基金费用"]
    assert old_task_rule_set["files"][filename]["source_sha256"] == rule_store._file_hash(source_v1)
    assert new_task_rule_set["files"][filename]["source_sha256"] == rule_store._file_hash(source_v2)
    assert old_task_rule_set["rules_version"] != new_task_rule_set["rules_version"]
