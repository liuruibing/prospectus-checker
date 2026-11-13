"""Validated, chapter-agnostic execution plans produced from Chinese rules.

This module defines capabilities the shared runtime can execute. It must not
contain business behavior selected by a rule id, filename, or chapter title.
"""

from __future__ import annotations

import re
from typing import Any


PLAN_PROTOCOL_VERSION = 2
_FIELD = re.compile(r"^[a-z][a-z0-9_]*$")
_OPERATIONS = {"decimal", "identifier", "text", "date", "semantic", "equals"}
_CONTEXT_OPERATIONS = _OPERATIONS | {"matches_target", "before_or_equal", "reference_before_or_equal", "within_days"}
_MISMATCH_ACTIONS = {"block", "warn"}
_POPULATIONS = {"target_document", "both_documents"}
_ROLES = {"prospectus", "periodic_report", "product_summary"}
_SOURCE_FACTS = {
    "primary.status", "primary.coverage", "primary.applicability",
    "scope.fund_identity", "scope.period", "scope.fact_nature",
    "scope.unit", "scope.effective_time",
}
_TARGET_FACTS = {fact.replace("scope.", "target.scope.") for fact in _SOURCE_FACTS if fact.startswith("scope.")}
_SOURCE_OPERATORS = {"equals", "matches_target"}
_TARGET_FACT_BY_SOURCE = {
    "scope.fund_identity": "target.scope.fund_identity",
    "scope.period": "target.scope.period",
    "scope.fact_nature": "target.scope.fact_nature",
    "scope.unit": "target.scope.unit",
    "scope.effective_time": "target.scope.effective_time",
}


class ExecutionPlanError(ValueError):
    """Raised when a generated execution plan uses unsupported capabilities."""


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExecutionPlanError(f"{label} 必须是非空文字")
    return value.strip()


def _only_keys(value: dict[str, Any], allowed: set[str], label: str) -> None:
    unexpected = set(value) - allowed
    if unexpected:
        raise ExecutionPlanError(f"{label} 包含未支持的参数：" + ", ".join(sorted(unexpected)))


def _string_list(value: Any, label: str, *, fields: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise ExecutionPlanError(f"{label} 必须是列表")
    result: list[str] = []
    for item in value:
        item = _string(item, label)
        if fields and not _FIELD.fullmatch(item):
            raise ExecutionPlanError(f"{label} 包含不支持的字段名：{item}")
        result.append(item)
    if len(result) != len(set(result)):
        raise ExecutionPlanError(f"{label} 不能包含重复项")
    return result


def _dedupe_conditions(conditions: list[dict[str, str]]) -> list[dict[str, str]]:
    """Remove repeated predicates without changing their boolean meaning."""
    unique: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for condition in conditions:
        key = (condition["fact"], condition["operator"], condition["value"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(condition)
    return unique


def validate_execution_plan(plan: Any, *, expected_identity: str | None = None) -> dict[str, Any]:
    """Validate and normalize a generic declarative rule execution plan.

    The plan describes *data* consumed by shared operations. It cannot name a
    Python callable, expression, import, or per-rule algorithm.
    """
    if not isinstance(plan, dict):
        raise ExecutionPlanError("execution_plan 必须是 JSON 对象")
    required = {
        "protocol_version", "rule_identity", "rule_scope", "display_title", "business_group",
        "target_role", "reference_roles", "search_intent", "source_selection",
        "row_matching", "comparison", "evidence_requirements", "review_conditions",
    }
    missing = required - plan.keys()
    if missing:
        raise ExecutionPlanError("执行计划缺少字段：" + ", ".join(sorted(missing)))
    _only_keys(plan, required, "execution_plan")
    if plan["protocol_version"] != PLAN_PROTOCOL_VERSION:
        raise ExecutionPlanError("执行计划协议版本不受支持")

    identity = _string(plan["rule_identity"], "rule_identity")
    if expected_identity is not None and identity != expected_identity:
        raise ExecutionPlanError("执行计划不能改变规则稳定身份")
    scope = _string(plan["rule_scope"], "rule_scope")
    if scope not in {"investment", "product_summary"}:
        raise ExecutionPlanError(f"不支持的规则工作流：{scope}")
    target_role = _string(plan["target_role"], "target_role")
    if target_role not in _ROLES:
        raise ExecutionPlanError(f"不支持的目标文档角色：{target_role}")
    reference_roles = _string_list(plan["reference_roles"], "reference_roles")
    if not reference_roles or any(role not in _ROLES for role in reference_roles):
        raise ExecutionPlanError("reference_roles 必须包含受支持的文档角色")
    business_group = _string(plan["business_group"], "business_group")

    source_selection = plan["source_selection"]
    if not isinstance(source_selection, dict):
        raise ExecutionPlanError("source_selection 必须是对象")
    _only_keys(source_selection, {"primary_role", "fallbacks"}, "source_selection")
    primary_role = _string(source_selection.get("primary_role"), "source_selection.primary_role")
    if primary_role not in reference_roles:
        raise ExecutionPlanError("source_selection.primary_role 必须属于 reference_roles")
    fallbacks = source_selection.get("fallbacks")
    if not isinstance(fallbacks, list):
        raise ExecutionPlanError("source_selection.fallbacks 必须是列表")
    normalized_fallbacks: list[dict[str, Any]] = []
    for index, fallback in enumerate(fallbacks):
        label = f"source_selection.fallbacks[{index}]"
        if not isinstance(fallback, dict):
            raise ExecutionPlanError(f"{label} 必须是对象")
        _only_keys(fallback, {"role", "when", "when_any", "candidate_review_when_any"}, label)
        role = _string(fallback.get("role"), f"{label}.role")
        if role not in reference_roles or role == primary_role:
            raise ExecutionPlanError(f"{label}.role 必须是非首选的已声明参考来源")
        conditions = fallback.get("when", [])
        any_groups = fallback.get("when_any", [])
        candidate_review_groups = fallback.get("candidate_review_when_any", [])
        if (not isinstance(conditions, list) or not isinstance(any_groups, list)
                or not isinstance(candidate_review_groups, list)):
            raise ExecutionPlanError(f"{label}.when、when_any 与 candidate_review_when_any 必须是列表")
        normalized_conditions: list[dict[str, str]] = []

        def normalize_condition(condition: Any, condition_label: str) -> dict[str, str]:
            if not isinstance(condition, dict):
                raise ExecutionPlanError(f"{condition_label} 必须是对象")
            _only_keys(condition, {"fact", "path", "operator", "value"}, condition_label)
            if ("fact" in condition) == ("path" in condition):
                raise ExecutionPlanError(f"{condition_label} 必须且只能提供 fact 或 path")
            fact = _string(condition.get("fact", condition.get("path")), f"{condition_label}.fact")
            operator = _string(condition.get("operator"), f"{condition_label}.operator")
            value = _string(condition.get("value"), f"{condition_label}.value")
            if fact not in _SOURCE_FACTS:
                raise ExecutionPlanError(f"{condition_label}.fact 不属于通用来源事实")
            if operator not in _SOURCE_OPERATORS:
                raise ExecutionPlanError(f"{condition_label}.operator 不受支持")
            if operator == "matches_target" and value not in _TARGET_FACTS:
                raise ExecutionPlanError(
                    f"{condition_label}.value={value!r} 必须是目标侧通用事实之一：{', '.join(sorted(_TARGET_FACTS))}"
                )
            if operator == "matches_target" and _TARGET_FACT_BY_SOURCE.get(fact) != value:
                raise ExecutionPlanError(
                    f"{condition_label} 的来源事实与目标事实维度不对应，不能把不同业务维度互相匹配"
                )
            if operator == "equals" and value in _TARGET_FACTS:
                raise ExecutionPlanError(f"{condition_label} 比较目标侧事实时必须使用 matches_target")
            return {"fact": fact, "operator": operator, "value": value}

        for condition_index, condition in enumerate(conditions):
            normalized_conditions.append(normalize_condition(condition, f"{label}.when[{condition_index}]"))
        normalized_conditions = _dedupe_conditions(normalized_conditions)
        normalized_any_groups: list[list[dict[str, str]]] = []
        for group_index, group in enumerate(any_groups):
            if not isinstance(group, list) or not group:
                raise ExecutionPlanError(f"{label}.when_any[{group_index}] 必须是非空条件列表")
            normalized_group = [
                normalize_condition(condition, f"{label}.when_any[{group_index}][{condition_index}]")
                for condition_index, condition in enumerate(group)
            ]
            normalized_any_groups.append(_dedupe_conditions(normalized_group))
        normalized_candidate_review_groups: list[list[dict[str, str]]] = []
        for group_index, group in enumerate(candidate_review_groups):
            if not isinstance(group, list) or not group:
                raise ExecutionPlanError(f"{label}.candidate_review_when_any[{group_index}] 必须是非空条件列表")
            normalized_group = [
                normalize_condition(condition,
                                    f"{label}.candidate_review_when_any[{group_index}][{condition_index}]")
                for condition_index, condition in enumerate(group)
            ]
            normalized_candidate_review_groups.append(_dedupe_conditions(normalized_group))
        if not normalized_conditions and not normalized_any_groups and not normalized_candidate_review_groups:
            raise ExecutionPlanError(f"{label} 必须至少声明一个来源切换或候选适用性审核条件")
        direct_groups = normalized_any_groups or ([normalized_conditions] if normalized_conditions else [])
        direct_signatures = {
            frozenset((condition["fact"], condition["operator"], condition["value"]) for condition in group)
            for group in direct_groups
        }
        if any(
            frozenset((condition["fact"], condition["operator"], condition["value"]) for condition in group)
            in direct_signatures
            for group in normalized_candidate_review_groups
        ):
            raise ExecutionPlanError(f"{label} 的候选审核条件不能同时作为自动来源切换条件")
        normalized_fallbacks.append({
            "role": role,
            "when": normalized_conditions,
            "when_any": normalized_any_groups,
            "candidate_review_when_any": normalized_candidate_review_groups,
        })
    # Some model outputs split alternatives into repeated entries for the
    # same role. Preserve their generic meaning (entries are alternatives,
    # each entry's conditions are conjunctive) by compiling them into OR groups.
    fallback_groups: dict[str, list[dict[str, Any]]] = {}
    for fallback in normalized_fallbacks:
        fallback_groups.setdefault(fallback["role"], []).append(fallback)
    merged_fallbacks: list[dict[str, Any]] = []
    for role, entries in fallback_groups.items():
        if len(entries) == 1:
            merged_fallbacks.append(entries[0])
            continue
        alternatives = []
        for entry in entries:
            groups = entry["when_any"] or [entry["when"]]
            alternatives.extend(entry["when"] + group for group in groups if entry["when"] or group)
        candidate_review_alternatives = [
            group for entry in entries for group in entry["candidate_review_when_any"]
        ]
        merged_fallbacks.append({"role": role, "when": [], "when_any": alternatives,
                                 "candidate_review_when_any": candidate_review_alternatives})

    search = plan["search_intent"]
    if not isinstance(search, dict):
        raise ExecutionPlanError("search_intent 必须是对象")
    _only_keys(search, {
        "target_description", "preferred_sections", "preferred_sections_by_role", "include_continuations",
    }, "search_intent")
    _string(search.get("target_description"), "search_intent.target_description")
    _string_list(search.get("preferred_sections"), "search_intent.preferred_sections")
    role_sections = search.get("preferred_sections_by_role", {})
    if not isinstance(role_sections, dict):
        raise ExecutionPlanError("search_intent.preferred_sections_by_role 必须是角色到章节列表的对象")
    normalized_role_sections = {}
    for role, sections in role_sections.items():
        if role not in _ROLES or (role not in reference_roles and role != target_role):
            raise ExecutionPlanError(f"search_intent.preferred_sections_by_role 包含未声明角色：{role}")
        normalized_role_sections[role] = _string_list(
            sections, f"search_intent.preferred_sections_by_role.{role}",
        )
    if not isinstance(search.get("include_continuations"), bool):
        raise ExecutionPlanError("search_intent.include_continuations 必须是布尔值")

    matching = plan["row_matching"]
    if not isinstance(matching, dict):
        raise ExecutionPlanError("row_matching 必须是对象")
    _only_keys(matching, {
        "identity_description", "identity_fields", "identity_prefixes_to_ignore", "scope_fields", "required_scope_fields", "hierarchy_fields", "fallback_identity_fields",
        "identity_headers", "identity_equivalence_groups", "target_population", "reference_superset_allowed",
        "require_complete_source_for_missing",
    }, "row_matching")
    _string(matching.get("identity_description"), "row_matching.identity_description")
    identity_fields = _string_list(matching.get("identity_fields"), "row_matching.identity_fields", fields=True)
    identity_prefixes_to_ignore = _string_list(
        matching.get("identity_prefixes_to_ignore", []),
        "row_matching.identity_prefixes_to_ignore",
    )
    if any(not prefix.strip() for prefix in identity_prefixes_to_ignore):
        raise ExecutionPlanError("row_matching.identity_prefixes_to_ignore 不能包含空前缀")
    scope_fields = _string_list(matching.get("scope_fields"), "row_matching.scope_fields", fields=True)
    required_scope_fields = _string_list(
        matching.get("required_scope_fields", []), "row_matching.required_scope_fields", fields=True,
    )
    if not set(required_scope_fields).issubset(scope_fields):
        raise ExecutionPlanError("row_matching.required_scope_fields 必须属于 scope_fields")
    hierarchy_fields = _string_list(matching.get("hierarchy_fields", []), "row_matching.hierarchy_fields", fields=True)
    fallback_fields = _string_list(
        matching.get("fallback_identity_fields", []),
        "row_matching.fallback_identity_fields", fields=True,
    )
    # Fact-oriented rules can compare one declared fact without table row keys.
    identity_headers = matching.get("identity_headers", {})
    if not isinstance(identity_headers, dict):
        raise ExecutionPlanError("row_matching.identity_headers 必须是字段到表头语义列表的对象")
    identity_header_map: dict[str, list[str]] = {}
    for field, headers in identity_headers.items():
        if not isinstance(field, str) or not _FIELD.fullmatch(field):
            raise ExecutionPlanError(f"row_matching.identity_headers 包含无效身份字段名：{field}")
        identity_header_map[field] = _string_list(headers, f"row_matching.identity_headers.{field}")
    # A header mapping itself declares an identity role to extract. Fold it
    # into the primary identity list so extraction and matching share one
    # generic schema without requiring the model to duplicate that declaration.
    identity_fields = list(dict.fromkeys([*identity_fields, *identity_header_map.keys()]))
    raw_equivalence_groups = matching.get("identity_equivalence_groups", [])
    if not isinstance(raw_equivalence_groups, list):
        raise ExecutionPlanError("row_matching.identity_equivalence_groups 必须是列表")
    equivalence_groups: list[dict[str, Any]] = []
    seen_equivalence_labels: set[str] = set()
    for index, group in enumerate(raw_equivalence_groups):
        label = f"row_matching.identity_equivalence_groups[{index}]"
        if not isinstance(group, dict):
            raise ExecutionPlanError(f"{label} 必须是对象")
        _only_keys(group, {"canonical_identity", "equivalent_labels"}, label)
        canonical = _string(group.get("canonical_identity"), f"{label}.canonical_identity")
        aliases = _string_list(group.get("equivalent_labels"), f"{label}.equivalent_labels")
        if not aliases:
            raise ExecutionPlanError(f"{label}.equivalent_labels 必须至少包含一个等价名称")
        normalized_labels = [re.sub(r"\s+", "", alias).casefold() for alias in [canonical, *aliases]]
        if any(not value for value in normalized_labels) or len(normalized_labels) != len(set(normalized_labels)):
            raise ExecutionPlanError(f"{label} 的规范身份及等价名称不能重复或为空")
        overlap = seen_equivalence_labels.intersection(normalized_labels)
        if overlap:
            raise ExecutionPlanError(f"{label} 与前面的等价组重复：" + ", ".join(sorted(overlap)))
        seen_equivalence_labels.update(normalized_labels)
        equivalence_groups.append({"canonical_identity": canonical, "equivalent_labels": aliases})
    population = _string(matching.get("target_population"), "row_matching.target_population")
    if population not in _POPULATIONS:
        raise ExecutionPlanError(f"不支持的目标范围：{population}")
    if not isinstance(matching.get("reference_superset_allowed"), bool):
        raise ExecutionPlanError("reference_superset_allowed 必须是布尔值")
    if not isinstance(matching.get("require_complete_source_for_missing"), bool):
        raise ExecutionPlanError("require_complete_source_for_missing 必须是布尔值")

    comparison = plan["comparison"]
    if not isinstance(comparison, dict):
        raise ExecutionPlanError("comparison 必须是对象")
    _only_keys(comparison, {
        "identity_description", "scope_conditions", "context_checks", "context_checks_by_reference_role",
        "context_check_groups", "fields",
    }, "comparison")
    _string(comparison.get("identity_description"), "comparison.identity_description")
    fields = comparison.get("fields")
    if not isinstance(fields, list) or not fields:
        raise ExecutionPlanError("comparison.fields 必须至少声明一个比较字段")
    normalized_fields: list[dict[str, Any]] = []
    seen_fields: set[str] = set()
    for item in fields:
        if not isinstance(item, dict):
            raise ExecutionPlanError("comparison.fields 中的字段定义必须是对象")
        _only_keys(item, {"field", "label", "operation", "required", "source_headers"}, "comparison.fields")
        field = _string(item.get("field"), "comparison.fields.field")
        label = _string(item.get("label"), f"comparison.fields.{field}.label")
        operation = _string(item.get("operation"), f"comparison.fields.{field}.operation")
        if not _FIELD.fullmatch(field):
            raise ExecutionPlanError(f"比较字段名无效：{field}")
        if operation not in _OPERATIONS:
            raise ExecutionPlanError(f"不支持的通用比较能力：{operation}")
        if field in seen_fields:
            raise ExecutionPlanError(f"比较字段重复：{field}")
        if not isinstance(item.get("required"), bool):
            raise ExecutionPlanError(f"comparison.fields.{field}.required 必须是布尔值")
        source_headers = _string_list(
            item.get("source_headers", []), f"comparison.fields.{field}.source_headers",
        )
        seen_fields.add(field)
        normalized_fields.append({
            "field": field,
            "label": label,
            "operation": operation,
            "required": item["required"],
            "source_headers": source_headers,
        })
    def normalize_context_checks(context_checks: Any, prefix: str) -> list[dict[str, Any]]:
        if not isinstance(context_checks, list):
            raise ExecutionPlanError(f"{prefix} 必须是列表")
        normalized: list[dict[str, Any]] = []
        for index, check in enumerate(context_checks):
            label = f"{prefix}[{index}]"
            if not isinstance(check, dict):
                raise ExecutionPlanError(f"{label} 必须是对象")
            _only_keys(
                check,
                {"label", "target_path", "reference_path", "operation", "required", "max_days", "on_mismatch"},
                label,
            )
            check_label = _string(check.get("label"), f"{label}.label")
            paths = {}
            for path_key in ("target_path", "reference_path"):
                path_value = _string(check.get(path_key), f"{label}.{path_key}")
                parts = path_value.split(".")
                if parts[0] not in {"document", "module"} or any(not _FIELD.fullmatch(part) for part in parts):
                    raise ExecutionPlanError(f"{label}.{path_key}={path_value!r} 只能使用由点分隔的 document/module 数据字段路径（字段名用 snake_case）")
                if target_role == "product_summary" and parts[:3] == ["document", "document_info", "period_end"]:
                    raise ExecutionPlanError(
                        f"{label}.{path_key} 不能把产品资料概要与参考文件的文档报告期末当作同一指标期间；"
                        "如规则要求核对某项事实的期间，应从该项事实或模块范围中提取期间"
                    )
                paths[path_key] = path_value
            operation = _string(check.get("operation"), f"{label}.operation")
            if operation not in _CONTEXT_OPERATIONS:
                raise ExecutionPlanError(f"{label}.operation 不支持：{operation}")
            if not isinstance(check.get("required"), bool):
                raise ExecutionPlanError(f"{label}.required 必须是布尔值")
            entry: dict[str, Any] = {
                "label": check_label,
                **paths,
                "operation": operation,
                "required": check["required"],
            }
            max_days = check.get("max_days")
            if operation == "within_days":
                if not isinstance(max_days, int) or isinstance(max_days, bool) or max_days <= 0:
                    raise ExecutionPlanError(f"{label}.max_days 必须是正整数")
                entry["max_days"] = max_days
            elif max_days is not None:
                raise ExecutionPlanError(f"{label}.max_days 只能用于 within_days 运算")
            on_mismatch = check.get("on_mismatch")
            if on_mismatch is not None:
                on_mismatch_value = _string(on_mismatch, f"{label}.on_mismatch")
                if on_mismatch_value not in _MISMATCH_ACTIONS:
                    raise ExecutionPlanError(f"{label}.on_mismatch 不支持：{on_mismatch_value}")
                if on_mismatch_value != "block":
                    entry["on_mismatch"] = on_mismatch_value
            normalized.append(entry)
        return normalized

    normalized_context_checks = normalize_context_checks(
        comparison.get("context_checks", []), "comparison.context_checks",
    )
    role_context_checks = comparison.get("context_checks_by_reference_role", {})
    if not isinstance(role_context_checks, dict):
        raise ExecutionPlanError("comparison.context_checks_by_reference_role 必须是角色到检查列表的对象")
    normalized_role_context_checks = {}
    for role, checks in role_context_checks.items():
        if role not in reference_roles:
            raise ExecutionPlanError(f"comparison.context_checks_by_reference_role 包含未声明的参考角色：{role}")
        normalized_role_context_checks[role] = normalize_context_checks(
            checks, f"comparison.context_checks_by_reference_role.{role}",
        )

    def context_check_key(check: dict[str, Any]) -> tuple[Any, ...]:
        return (
            check["target_path"], check["reference_path"], check["operation"], check["required"],
            check.get("max_days"), check.get("on_mismatch", "block"),
        )

    # A condition explicitly scoped to a source role must not also run as a
    # global condition. Model-produced plans can contain both copies; keeping
    # the global copy would incorrectly require metadata from a different,
    # unselected source. Normalize this generic plan-shape error without
    # assigning business meaning to any rule, field, or chapter.
    role_scoped_keys = {
        context_check_key(check)
        for checks in normalized_role_context_checks.values()
        for check in checks
    }
    remaining_context_checks: list[dict[str, Any]] = []
    role_contract_paths = {
        "document.metadata.sent_date": "periodic_report",
        "document.metadata.version_date": "prospectus",
    }
    for check in normalized_context_checks:
        key = context_check_key(check)
        if key in role_scoped_keys:
            continue
        inferred_role = role_contract_paths.get(check["reference_path"])
        if normalized_role_context_checks and inferred_role in reference_roles and inferred_role not in normalized_role_context_checks:
            normalized_role_context_checks.setdefault(inferred_role, []).append(check)
            role_scoped_keys.add(key)
            continue
        remaining_context_checks.append(check)
    normalized_context_checks = remaining_context_checks
    for role, checks in normalized_role_context_checks.items():
        unique_checks: list[dict[str, Any]] = []
        seen_checks: set[tuple[Any, ...]] = set()
        for check in checks:
            key = context_check_key(check)
            if key in seen_checks:
                continue
            seen_checks.add(key)
            unique_checks.append(check)
        normalized_role_context_checks[role] = unique_checks
    context_check_groups = comparison.get("context_check_groups", [])
    if not isinstance(context_check_groups, list):
        raise ExecutionPlanError("comparison.context_check_groups 必须是列表")
    normalized_context_check_groups: list[dict[str, Any]] = []
    for group_index, group in enumerate(context_check_groups):
        label = f"comparison.context_check_groups[{group_index}]"
        if not isinstance(group, dict):
            raise ExecutionPlanError(f"{label} 必须是对象")
        _only_keys(group, {"label", "mode", "minimum_matches", "checks"}, label)
        group_label = _string(group.get("label"), f"{label}.label")
        mode = _string(group.get("mode"), f"{label}.mode")
        if mode != "all_available_match":
            raise ExecutionPlanError(f"{label}.mode 目前只支持 all_available_match")
        checks = group.get("checks")
        if not isinstance(checks, list) or not checks:
            raise ExecutionPlanError(f"{label}.checks 必须是非空列表")
        minimum_matches = group.get("minimum_matches")
        if not isinstance(minimum_matches, int) or isinstance(minimum_matches, bool) or not 1 <= minimum_matches <= len(checks):
            raise ExecutionPlanError(f"{label}.minimum_matches 必须在 1 与 checks 数量之间")
        normalized_group_checks: list[dict[str, str]] = []
        for check_index, check in enumerate(checks):
            check_label = f"{label}.checks[{check_index}]"
            if not isinstance(check, dict):
                raise ExecutionPlanError(f"{check_label} 必须是对象")
            _only_keys(check, {"label", "target_path", "reference_path", "operation"}, check_label)
            normalized_paths = {}
            for path_key in ("target_path", "reference_path"):
                path_value = _string(check.get(path_key), f"{check_label}.{path_key}")
                parts = path_value.split(".")
                if parts[0] not in {"document", "module"} or any(not _FIELD.fullmatch(part) for part in parts):
                    raise ExecutionPlanError(f"{check_label}.{path_key} 只能使用 document/module 数据字段路径")
                if target_role == "product_summary" and parts[:3] == ["document", "document_info", "period_end"]:
                    raise ExecutionPlanError(
                        f"{check_label}.{path_key} 不能把产品资料概要与参考文件的文档报告期末当作同一指标期间"
                    )
                normalized_paths[path_key] = path_value
            check_operation = _string(check.get("operation"), f"{check_label}.operation")
            if check_operation not in _CONTEXT_OPERATIONS:
                raise ExecutionPlanError(f"{check_label}.operation 不支持：{check_operation}")
            normalized_group_checks.append({
                "label": _string(check.get("label"), f"{check_label}.label"),
                **normalized_paths,
                "operation": check_operation,
            })
        normalized_context_check_groups.append({
            "label": group_label,
            "mode": mode,
            "minimum_matches": minimum_matches,
            "checks": normalized_group_checks,
        })
    _string_list(comparison.get("scope_conditions"), "comparison.scope_conditions")
    _string_list(plan["evidence_requirements"], "evidence_requirements")
    _string_list(plan["review_conditions"], "review_conditions")

    # Reject executable or free-expression escape hatches. Unknown business
    # prose can live in descriptions, but runtime instructions are schema-bound.
    forbidden_keys = {"python", "code", "expression", "callable", "function", "script"}

    def inspect(value: Any, path: str = "execution_plan") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).casefold() in forbidden_keys:
                    raise ExecutionPlanError(f"{path} 不允许可执行字段：{key}")
                inspect(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                inspect(child, f"{path}[{index}]")

    inspect(plan)
    return {
        **plan,
        "rule_identity": identity,
        "rule_scope": scope,
        "display_title": _string(plan["display_title"], "display_title"),
        "business_group": business_group,
        "target_role": target_role,
        "reference_roles": reference_roles,
        "search_intent": {
            **search,
            "preferred_sections": _string_list(search.get("preferred_sections"), "search_intent.preferred_sections"),
            "preferred_sections_by_role": normalized_role_sections,
        },
        "source_selection": {"primary_role": primary_role, "fallbacks": merged_fallbacks},
        "row_matching": {
            **matching,
            "identity_fields": identity_fields,
            "identity_prefixes_to_ignore": identity_prefixes_to_ignore,
            "scope_fields": scope_fields,
            "required_scope_fields": required_scope_fields,
            "hierarchy_fields": hierarchy_fields,
            "fallback_identity_fields": fallback_fields,
            "identity_headers": identity_header_map,
            "identity_equivalence_groups": equivalence_groups,
        },
        "comparison": {
            **comparison,
            "context_checks": normalized_context_checks,
            "context_checks_by_reference_role": normalized_role_context_checks,
            "context_check_groups": normalized_context_check_groups,
            "fields": normalized_fields,
        },
    }


def eligible_fallback_roles(plan: dict[str, Any], facts: dict[str, Any]) -> list[str]:
    """Return fallback roles whose rule-declared generic evidence conditions hold."""
    return [item["role"] for item in eligible_fallbacks(plan, facts)]


def eligible_fallbacks(plan: dict[str, Any], facts: dict[str, Any]) -> list[dict[str, str]]:
    """Return sources eligible for selection or candidate applicability review."""
    source_selection = plan.get("source_selection")
    if not isinstance(source_selection, dict):
        return []

    def matches(condition: dict[str, str]) -> bool:
        actual = facts.get(condition["fact"])
        operator, expected = condition["operator"], condition["value"]
        if operator == "equals":
            return actual is not None and str(actual).strip().casefold() == expected.strip().casefold()
        if operator == "matches_target":
            target = facts.get(expected)
            return actual is not None and target is not None and str(actual).strip() == str(target).strip()
        return False

    result: list[dict[str, str]] = []
    for item in source_selection.get("fallbacks", []):
        direct = (
            bool(item.get("when") or item.get("when_any"))
            and all(matches(condition) for condition in item.get("when", []))
            and (not item.get("when_any") or any(
                all(matches(condition) for condition in group)
                for group in item["when_any"]
            ))
        )
        primary_confirmed = (
            str(facts.get("primary.status") or "").strip().casefold() == "found"
            and str(facts.get("primary.applicability") or "").strip().casefold() in {"confirmed", "not_required"}
            and str(facts.get("primary.coverage") or "").strip().casefold() != "partial"
        )
        candidate_review = (
            not primary_confirmed
            and any(
                all(matches(condition) for condition in group)
                for group in item.get("candidate_review_when_any", [])
            )
        )
        if direct:
            result.append({"role": item["role"], "mode": "select"})
        elif candidate_review:
            result.append({"role": item["role"], "mode": "candidate_review"})
    return result
