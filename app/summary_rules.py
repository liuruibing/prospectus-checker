"""Load product-summary business rules from Markdown documents.

The TOML front matter carries only fields needed by the runtime contract. The
Markdown body remains the editable business explanation and is passed through
to extraction/comparison prompts. ``item_id`` is the stable execution identity;
Excel coordinates are retained only as provenance.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any


RULE_DIR = Path(__file__).resolve().parents[1] / "rules" / "product_summary"
REQUIRED_METADATA = {
    "item_id", "module_id", "label", "value_type", "primary_source",
    "period_policy", "unit_policy", "comparison_policy",
}


class RuleLoadError(ValueError):
    """Raised when a rule cannot be safely used by the workflow."""


def _parse_document(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "+++":
        raise RuleLoadError(f"{path.name}: expected TOML front matter beginning with +++")
    try:
        end = next(index for index in range(1, len(lines)) if lines[index].strip() == "+++")
    except StopIteration as exc:
        raise RuleLoadError(f"{path.name}: TOML front matter is not closed") from exc
    try:
        metadata = tomllib.loads("\n".join(lines[1:end]))
    except tomllib.TOMLDecodeError as exc:
        raise RuleLoadError(f"{path.name}: invalid TOML metadata: {exc}") from exc
    body = "\n".join(lines[end + 1 :]).strip()
    missing = REQUIRED_METADATA - metadata.keys()
    if missing:
        raise RuleLoadError(f"{path.name}: missing metadata: {', '.join(sorted(missing))}")
    if not body:
        raise RuleLoadError(f"{path.name}: missing Markdown business rule body")
    item_id = metadata["item_id"]
    if not isinstance(item_id, str) or not item_id.strip():
        raise RuleLoadError(f"{path.name}: item_id must be a non-empty stable string")
    module_id = metadata["module_id"]
    if not isinstance(module_id, str) or not module_id.strip():
        raise RuleLoadError(f"{path.name}: module_id must be a non-empty stable string")
    primary_source = metadata["primary_source"]
    if not isinstance(primary_source, dict) or not isinstance(primary_source.get("role"), str):
        raise RuleLoadError(f"{path.name}: primary_source.role is required")
    fallback = metadata.get("fallback_source")
    if fallback is not None and (not isinstance(fallback, dict) or not isinstance(fallback.get("role"), str)):
        raise RuleLoadError(f"{path.name}: fallback_source.role must be a string")
    for list_key in ("search_terms", "section_hints", "target_section_path", "fallback_when"):
        values = metadata.get(list_key, [])
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise RuleLoadError(f"{path.name}: {list_key} must be an array of strings")
    role_hints = metadata.get("section_hints_by_role", {})
    if not isinstance(role_hints, dict) or any(
        not isinstance(role, str) or not isinstance(hints, list)
        or any(not isinstance(hint, str) or not hint.strip() for hint in hints)
        for role, hints in role_hints.items()
    ):
        raise RuleLoadError(f"{path.name}: section_hints_by_role must map roles to non-empty strings")
    rule = {**metadata, "meaning": body, "rule_path": path.name}
    return rule


def load_markdown_rules(rule_dir: Path = RULE_DIR) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return versioned rule config and items keyed by stable item_id."""
    paths = sorted(rule_dir.glob("*.md"))
    if not paths:
        raise RuleLoadError(f"No Markdown rules found in {rule_dir}")
    rules = [_parse_document(path) for path in paths]
    seen: set[str] = set()
    for rule in rules:
        item_id = rule["item_id"]
        if item_id in seen:
            raise RuleLoadError(f"duplicate item_id: {item_id}")
        seen.add(item_id)
    if any("display_order" in rule for rule in rules):
        if any(not isinstance(rule.get("display_order"), int) for rule in rules):
            raise RuleLoadError("display_order must be an integer when configured")
        rules.sort(key=lambda rule: (rule["display_order"], rule["item_id"]))
    config = {
        "version": "product-summary-md-v1",
        "items": rules,
        "modules": sorted({rule["module_id"] for rule in rules}),
    }
    return config, rules


def module_groups(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group eligible items by their rule-declared module, never spreadsheet row."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        module_id = item.get("module_id")
        if not isinstance(module_id, str) or not module_id:
            raise RuleLoadError(f"{item.get('item_id', '<unknown>')}: module_id is required")
        groups.setdefault(module_id, []).append(item)
    return groups
