from pathlib import Path

import pytest

import app.summary_rules as summary_rules
from app.summary_rules import RuleLoadError, load_markdown_rules


RULE_BODY = """# 基金简称

## 核对对象
核对产品资料概要中的基金简称。

## 查找内容
在目标文件和参考文件的产品概况中查找。

## 来源与适用范围
以同一基金及适用份额为范围。

## 比较方法
比较双方披露的简称含义。

## 特殊披露
保留原文中的简称及适用对象。

## 需要复核的情况
身份或证据无法确认时待复核。
"""


def _write_rule(path: Path, body: str = RULE_BODY):
    path.write_text(body, encoding="utf-8")


def test_markdown_rules_load_without_technical_metadata(tmp_path: Path):
    _write_rule(tmp_path / "fund_short_name.md")

    config, rules = load_markdown_rules(tmp_path)

    assert config["version"].startswith("product-summary-prose-v2:")
    assert rules[0]["item_id"] == "fund_short_name"
    assert rules[0]["label"] == "基金简称"
    assert rules[0]["sections"]["比较方法"] == "比较双方披露的简称含义。"
    assert rules[0]["execution_plan"] is None
    assert "excel_row" not in rules[0]
    assert "comparison_policy" not in rules[0]


def test_markdown_rule_loader_rejects_stable_identity_collisions(tmp_path: Path):
    _write_rule(tmp_path / "fund_short_name.md")
    _write_rule(tmp_path / "fund_short_name_基金简称.md")

    with pytest.raises(RuleLoadError, match="重复的稳定身份"):
        load_markdown_rules(tmp_path)


def test_markdown_rule_loader_requires_business_sections(tmp_path: Path):
    _write_rule(tmp_path / "fund_short_name.md", "# 基金简称\n\n## 核对对象\n核对简称\n")

    with pytest.raises(RuleLoadError, match="缺少业务说明分区"):
        load_markdown_rules(tmp_path)


def test_summary_rule_can_declare_multiple_composite_text_facts(tmp_path: Path, monkeypatch):
    _write_rule(tmp_path / "fund_short_name.md")
    plan = {
        "rule_identity": "fund_short_name",
        "display_title": "简称组合",
        "business_group": "产品概况",
        "source_selection": {"primary_role": "periodic_report", "fallbacks": []},
        "search_intent": {"preferred_sections": [], "preferred_sections_by_role": {}},
        "comparison": {"fields": [
            {"field": "exchange_name", "label": "场内简称", "operation": "text", "required": True},
            {"field": "extended_name", "label": "扩位证券简称", "operation": "text", "required": True},
        ]},
    }
    monkeypatch.setattr(summary_rules, "_read_plan", lambda path, source: plan)

    _, rules = load_markdown_rules(tmp_path, require_execution_plans=True)

    assert [field["label"] for field in rules[0]["comparison_fields"]] == ["场内简称", "扩位证券简称"]


def test_multi_field_summary_comparison_uses_model_only_for_text_semantics():
    from app.summary_check import _compare_by_execution_plan

    rule = {"execution_plan": {"comparison": {"fields": [
        {"field": "exchange_name", "operation": "text"},
        {"field": "extended_name", "operation": "text"},
    ]}}}
    assert _compare_by_execution_plan(rule, "", "", "MATCH") == "MATCH"
    assert _compare_by_execution_plan(rule, "", "", "UNRESOLVED") == "UNRESOLVED"
    rule["execution_plan"]["comparison"]["fields"][0]["operation"] = "decimal"
    assert _compare_by_execution_plan(rule, "", "", "MATCH") == "UNRESOLVED"
