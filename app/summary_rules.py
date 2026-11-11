"""Read Chinese Markdown rules and their system-generated generic plans."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any


RULE_DIR = Path(__file__).resolve().parents[1] / "rules" / "product_summary"
REQUIRED_SECTIONS = (
    "核对对象", "查找内容", "来源与适用范围", "比较方法", "特殊披露", "需要复核的情况",
)


class RuleLoadError(ValueError):
    """Raised when manager-authored rule prose is malformed or unusable."""


def parse_rule_markdown(text: str, *, filename: str = "规则.md") -> dict[str, Any]:
    """Parse user-facing prose without interpreting business semantics."""
    lines = text.splitlines()
    title_match = next((re.match(r"^#\s+(.+?)\s*#*\s*$", line) for line in lines
                        if re.match(r"^#\s+\S", line)), None)
    if title_match is None:
        raise RuleLoadError(f"{filename}: 缺少中文一级标题")
    title = title_match.group(1).strip()
    sections: dict[str, list[str]] = {}
    active: str | None = None
    for line in lines:
        if line.startswith("## "):
            active = line[3:].strip()
            if not active or active in sections:
                raise RuleLoadError(f"{filename}: 规则分区标题不能为空或重复")
            sections[active] = []
        elif active is not None:
            sections[active].append(line)
    if not sections:
        raise RuleLoadError(f"{filename}: 至少需要一个中文二级分区")
    empty = [name for name, content in sections.items() if not "\n".join(content).strip()]
    if empty:
        raise RuleLoadError(f"{filename}: 以下分区尚未填写：" + "、".join(empty))
    missing = [name for name in REQUIRED_SECTIONS if name not in sections]
    if missing:
        raise RuleLoadError(f"{filename}: 缺少业务说明分区：" + "、".join(missing))
    return {
        "title": title,
        "sections": {name: "\n".join(content).strip() for name, content in sections.items()},
        "content": text,
    }


def _read_plan(path: Path, source: str) -> dict[str, Any] | None:
    from app.rule_compiler import load_compiled_execution_plan

    return load_compiled_execution_plan(path, source)


def _rule_from_source(path: Path, *, require_execution_plan: bool) -> dict[str, Any]:
    from app.rule_compiler import _stable_rule_identity

    source = path.read_text(encoding="utf-8")
    parsed = parse_rule_markdown(source, filename=path.name)
    if path.name.startswith("common_"):
        raise RuleLoadError(f"{path.name}: 公共规则不应作为单项核对指标加载")
    try:
        plan = _read_plan(path, source)
    except (OSError, ValueError):
        if require_execution_plan:
            raise
        plan = None
    if require_execution_plan and plan is None:
        raise RuleLoadError(f"规则“{path.name}”没有当前版本的通用执行计划，不能用于新任务")

    identity = _stable_rule_identity("product_summary", path.name)
    if plan is not None and plan.get("rule_identity") != identity:
        raise RuleLoadError(f"{path.name}: 执行计划身份与规则登记身份不一致")
    plan = plan or {}
    source_selection = plan.get("source_selection", {})
    search_intent = plan.get("search_intent", {})
    role_hints = search_intent.get("preferred_sections_by_role", {})
    primary_role = source_selection.get("primary_role")
    fallbacks = source_selection.get("fallbacks", [])
    fields = plan.get("comparison", {}).get("fields", [])
    module_id = plan.get("business_group")
    if require_execution_plan:
        if not module_id:
            raise RuleLoadError(f"{path.name}: 执行计划未说明业务分类")
        if not primary_role:
            raise RuleLoadError(f"{path.name}: 执行计划未说明主参考来源")
        if not fields:
            raise RuleLoadError(f"{path.name}: 执行计划未声明比较事实")

    return {
        "item_id": identity,
        "key": identity,
        "label": plan.get("display_title", parsed["title"]),
        "module_id": module_id or "未分类",
        "rule_path": path.name,
        "meaning": source,
        "business_rule_markdown": source,
        "sections": parsed["sections"],
        "execution_plan": plan or None,
        "primary_role": primary_role,
        "fallbacks": fallbacks,
        "preferred_sections": search_intent.get("preferred_sections", []),
        "preferred_sections_by_role": role_hints,
        "comparison_fields": fields,
        "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
    }


def load_markdown_rules(
    rule_dir: Path = RULE_DIR, *, require_execution_plans: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Load editable prose; optionally require current generated plans for execution."""
    paths = sorted(path for path in rule_dir.glob("*.md") if not path.name.startswith("common_"))
    if not paths:
        raise RuleLoadError(f"没有找到产品资料概要规则：{rule_dir}")
    rules = [_rule_from_source(path, require_execution_plan=require_execution_plans) for path in paths]
    identities = [rule["item_id"] for rule in rules]
    if len(identities) != len(set(identities)):
        raise RuleLoadError("规则文件生成了重复的稳定身份")
    common_files = sorted(rule_dir.glob("common_*.md"))
    common_hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in common_files if path.is_file() and not path.is_symlink()
    }
    version_input = "\n".join([
        *(f"{rule['rule_path']}:{rule['source_sha256']}" for rule in rules),
        *(f"{name}:{digest}" for name, digest in sorted(common_hashes.items())),
    ])
    config = {
        "version": "product-summary-prose-v2:" + hashlib.sha256(version_input.encode()).hexdigest()[:20],
        "items": rules,
        "groups": sorted({rule["module_id"] for rule in rules}),
        "common_rule_hashes": common_hashes,
    }
    return config, rules


def module_groups(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Batch rules by the business category generated from their Chinese prose."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        group = item.get("module_id")
        if not isinstance(group, str) or not group.strip():
            raise RuleLoadError(f"{item.get('item_id', '<unknown>')}: 缺少规则业务分类")
        groups.setdefault(group, []).append(item)
    return groups
