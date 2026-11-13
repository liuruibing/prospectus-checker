import hashlib
from pathlib import Path

import pytest

import app.rule_store as rule_store


def _sha(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def test_summary_rule_save_validates_and_persists_latest_content(tmp_path, monkeypatch):
    root = tmp_path / "rules"
    summary_dir = root / "product_summary"
    summary_dir.mkdir(parents=True)
    source = '''# 管理费率

## 核对对象
核对管理费年费率。

## 查找内容
查找双方适用的费用章节。

## 来源与适用范围
确认同一基金和适用时间。

## 比较方法
比较年费率。

## 特殊披露
保留费率变更信息。

## 需要复核的情况
生效时间不清时待复核。
'''
    path = summary_dir / "management_fee.md"
    path.write_text(source, encoding="utf-8")
    monkeypatch.setattr(rule_store, "RULE_ROOT", root)
    monkeypatch.setattr(rule_store, "load_compiled_execution_plan", lambda *_: {"execution_plan": {}})

    updated = source + "\n维护记录：演示写入校验。\n"
    saved = rule_store.save_rule("product_summary", path.name, updated, _sha(source))

    assert path.read_text(encoding="utf-8") == updated
    assert saved["content"] == updated
    assert saved["sha256"] == _sha(updated)


def test_stale_rule_hash_rejects_save_without_overwriting_current_content(tmp_path, monkeypatch):
    root = tmp_path / "rules"
    investment_dir = root / "investment"
    investment_dir.mkdir(parents=True)
    path = investment_dir / "common_公共规则.md"
    current = "# 当前规则\n\n## 核对对象\n对象\n"
    path.write_text(current, encoding="utf-8")
    monkeypatch.setattr(rule_store, "RULE_ROOT", root)

    with pytest.raises(rule_store.RuleConflictError):
        rule_store.save_rule("investment", path.name, "# 旧页面的修改\n\n## 核对对象\n旧对象\n", "0" * 64)

    assert path.read_text(encoding="utf-8") == current


def test_rule_store_rejects_path_traversal(tmp_path, monkeypatch):
    root = tmp_path / "rules"
    (root / "investment").mkdir(parents=True)
    monkeypatch.setattr(rule_store, "RULE_ROOT", root)

    with pytest.raises(FileNotFoundError):
        rule_store.get_rule("investment", "../outside.md")


def test_list_rules_includes_dynamic_category_metadata(tmp_path, monkeypatch):
    root = tmp_path / "rules"
    (root / "investment").mkdir(parents=True)
    (root / "product_summary").mkdir(parents=True)
    (root / "investment" / "common_公共规则.md").write_text("# 投资公共规则\n\n## 核对对象\n内容\n", encoding="utf-8")
    (root / "investment" / "asset_allocation.md").write_text("# 资产组合\n\n## 核对对象\n内容\n", encoding="utf-8")
    (root / "product_summary" / "common_概要公共.md").write_text("# 概要公共规则\n\n## 核对对象\n内容\n", encoding="utf-8")
    (root / "product_summary" / "management_fee.md").write_text("# 管理费率\n\n## 核对对象\n内容\n", encoding="utf-8")
    monkeypatch.setattr(rule_store, "RULE_ROOT", root)
    monkeypatch.setattr(
        rule_store,
        "load_compiled_execution_plan",
        lambda path, _content=None: {"business_group": "费用信息"} if path.name == "management_fee.md" else None,
    )

    items = {(item["scope"], item["filename"]): item for item in rule_store.list_rules()}
    assert items[("investment", "common_公共规则.md")]["category_label"] == "全局规则"
    assert items[("investment", "asset_allocation.md")]["category_label"] == "核对章节"
    assert items[("product_summary", "common_概要公共.md")]["category_label"] == "全局规则"
    assert items[("product_summary", "management_fee.md")]["category_label"] == "费用信息"
