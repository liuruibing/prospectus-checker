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
    source = Path("rules/product_summary/management_fee.md").read_text(encoding="utf-8")
    path = summary_dir / "management_fee.md"
    path.write_text(source, encoding="utf-8")
    monkeypatch.setattr(rule_store, "RULE_ROOT", root)

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
    current = "# 当前规则\n"
    path.write_text(current, encoding="utf-8")
    monkeypatch.setattr(rule_store, "RULE_ROOT", root)

    with pytest.raises(rule_store.RuleConflictError):
        rule_store.save_rule("investment", path.name, "# 旧页面的修改\n", "0" * 64)

    assert path.read_text(encoding="utf-8") == current


def test_rule_store_rejects_path_traversal(tmp_path, monkeypatch):
    root = tmp_path / "rules"
    (root / "investment").mkdir(parents=True)
    monkeypatch.setattr(rule_store, "RULE_ROOT", root)

    with pytest.raises(FileNotFoundError):
        rule_store.get_rule("investment", "../outside.md")
