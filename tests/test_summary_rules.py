from pathlib import Path

import pytest

from app.summary_rules import RuleLoadError, load_markdown_rules, module_groups


def _write_rule(path: Path, item_id: str, module_id: str = "profile", body: str = "核对业务含义。"):
    path.write_text(
        "+++\n"
        f'item_id = "{item_id}"\n'
        f'module_id = "{module_id}"\n'
        'label = "基金简称"\n'
        'value_type = "text"\n'
        'excel_source_cell = "Sheet1!A2:B2"\n'
        'primary_source = { role = "periodic_report", subtypes = ["quarterly"] }\n'
        'period_policy = "current_identity"\n'
        'unit_policy = "preserve_source_unit"\n'
        'comparison_policy = "semantic_with_identity_and_scope"\n'
        'search_terms = ["基金简称"]\n'
        "+++\n\n"
        f"{body}\n",
        encoding="utf-8",
    )


def test_markdown_rules_load_stable_ids_and_group_by_module(tmp_path: Path):
    _write_rule(tmp_path / "a.md", "fund_short_name", "identity")
    _write_rule(tmp_path / "b.md", "manager", "people")
    config, rules = load_markdown_rules(tmp_path)

    assert config["version"] == "product-summary-md-v1"
    assert {rule["item_id"] for rule in rules} == {"fund_short_name", "manager"}
    assert set(module_groups(rules)) == {"identity", "people"}
    assert rules[0]["meaning"] == "核对业务含义。"
    assert rules[0]["excel_source_cell"] == "Sheet1!A2:B2"


def test_markdown_rule_loader_rejects_duplicate_ids(tmp_path: Path):
    _write_rule(tmp_path / "a.md", "manager")
    _write_rule(tmp_path / "b.md", "manager", "fees")
    with pytest.raises(RuleLoadError, match="duplicate item_id"):
        load_markdown_rules(tmp_path)


def test_markdown_rule_loader_requires_business_rule_body(tmp_path: Path):
    _write_rule(tmp_path / "a.md", "manager", body=" ")
    with pytest.raises(RuleLoadError, match="missing Markdown business rule body"):
        load_markdown_rules(tmp_path)
