import json
import asyncio
from pathlib import Path

import pytest

import app.rule_compiler as compiler


def _prose() -> str:
    return """# 管理费率

## 核对对象
核对产品资料概要披露的管理费年费率。

## 查找内容
优先查适用定期报告的费用章节。

## 来源与适用范围
定期报告优先，招募说明书只在定期报告确认未披露时补充。

## 比较方法
比较年费率，不比较实际发生金额。
"""


class FakeProvider:
    model_name = "test-luna"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def generate_text(self, system_prompt, user_prompt):
        self.calls += 1
        response = self.responses.pop(0)
        return response


def _valid_legacy_rule() -> str:
    return '''+++
item_id = "management_fee"
module_id = "fees"
label = "管理费率"
value_type = "annual_rate"
display_order = 13
primary_source = { role = "periodic_report", subtypes = ["quarterly", "interim", "annual"] }
period_policy = "applicable_at_summary_compilation"
unit_policy = "annual_rate"
comparison_policy = "normalized_numeric"
fallback_source = { role = "prospectus" }
fallback_when = ["primary_confirmed_absent"]
+++

核对管理费年费率。'''


def test_compiler_validates_and_caches_generated_plan(tmp_path, monkeypatch):
    monkeypatch.setattr(compiler, "COMPILED_ROOT", tmp_path / "compiled")
    legacy = _valid_legacy_rule()
    response = json.dumps({"execution_markdown": legacy}, ensure_ascii=False)
    provider = FakeProvider([response, response])
    source = _prose()

    plan = asyncio.run(compiler.compile_rule(
        scope="product_summary", filename="management_fee.md", source_content=source,
        existing_execution=legacy, provider=provider, validate_summary_set=False,
    ))
    compiler.persist_compiled_rule("product_summary", "management_fee.md", plan)
    active_path = Path("rules/product_summary/management_fee.md")
    assert compiler.load_compiled_rule(active_path, source) == legacy

    cached = asyncio.run(compiler.compile_rule(
        scope="product_summary", filename="management_fee.md", source_content=source,
        existing_execution="different current wrapper", provider=provider, validate_summary_set=False,
    ))
    assert cached["execution_markdown"] == legacy
    assert provider.calls == 1


def test_compiler_rejects_unloadable_plan_without_activation(tmp_path, monkeypatch):
    monkeypatch.setattr(compiler, "COMPILED_ROOT", tmp_path / "compiled")
    provider = FakeProvider([
        json.dumps({"execution_markdown": "---\ninvalid: plan\n---\n"}, ensure_ascii=False),
        json.dumps({"execution_markdown": "still invalid"}, ensure_ascii=False),
    ])
    with pytest.raises(compiler.RuleCompilationError):
        asyncio.run(compiler.compile_rule(
            scope="product_summary", filename="management_fee.md", source_content=_prose(),
            existing_execution=_valid_legacy_rule(), provider=provider,
        ))

    assert provider.calls == 2
    target = compiler._compiled_path("product_summary", "management_fee.md", compiler._sha(_prose()))
    assert not target.exists()


def test_compiled_plan_is_bound_to_source_and_public_rule_versions(tmp_path, monkeypatch):
    monkeypatch.setattr(compiler, "COMPILED_ROOT", tmp_path / "compiled")
    path = Path("rules/product_summary/management_fee.md")
    source = _prose()
    with pytest.raises(compiler.RuleCompilationError, match="尚未生成"):
        compiler.load_compiled_rule(path, source)

    stale = {
        "protocol_version": compiler.PROTOCOL_VERSION,
        "source_sha256": compiler._sha(source),
        "public_rules_sha256": "0" * 64,
        "execution_markdown": _valid_legacy_rule(),
    }
    compiler.persist_compiled_rule("product_summary", "management_fee.md", stale)
    with pytest.raises(compiler.RuleCompilationError, match="公共规则已变更"):
        compiler.load_compiled_rule(path, source)


def test_manager_authored_rule_is_detected_by_chinese_sections():
    assert compiler._is_plain_chinese_rule(_prose())
    assert not compiler._is_plain_chinese_rule(_valid_legacy_rule())


def test_compiler_normalizes_json_escaped_markdown():
    assert compiler._normalize_model_markdown("+++\\nitem_id = \\\"x\\\"\\n+++") == (
        '+++\nitem_id = "x"\n+++'
    )


def test_rule_set_version_changes_when_source_prose_changes(tmp_path, monkeypatch):
    rules = tmp_path / "rules" / "investment"
    rules.mkdir(parents=True)
    (rules / "common_公共规则.md").write_text("公共约定 A", encoding="utf-8")
    module = rules / "module.md"
    module.write_text("内部解析结构", encoding="utf-8")
    monkeypatch.setattr(compiler, "ROOT", tmp_path)
    monkeypatch.setattr(compiler, "COMPILED_ROOT", tmp_path / "compiled")

    before = compiler.capture_rule_set("investment")
    module.write_text("内部解析结构与业务规则变更", encoding="utf-8")
    after = compiler.capture_rule_set("investment")

    assert before["rules_version"] != after["rules_version"]
    assert before["files"]["module.md"]["source_sha256"] != after["files"]["module.md"]["source_sha256"]
