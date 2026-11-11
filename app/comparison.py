from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app.domain import normalize_identifier, normalize_text
from app.rule_compiler import load_compiled_execution_plan


_UNIT_ALIASES = {
    "人民币元": "元",
    "人民币 元": "元",
    "rmb元": "元",
    "cny元": "元",
    "人民币百分比": "%",
    "百分比": "%",
    "％": "%",
}


def _canonical_unit(unit: Any) -> str:
    value = normalize_text(str(unit or "")).lower()
    return _UNIT_ALIASES.get(value, value)

def _disclosure_key(disclosure: dict[str, Any]) -> tuple[str, str] | None:
    period = "".join(ch for ch in str(disclosure.get("period_end") or "") if ch.isdigit())
    scope = disclosure.get("scope")
    if not period or not isinstance(scope, dict) or not scope:
        return None
    normalized = tuple(sorted((normalize_text(key), normalize_text(value)) for key, value in scope.items()))
    if any(not key or not value or value.casefold() in {"unknown", "未知", "不确定"} for key, value in normalized):
        return None
    return period, repr(normalized)


def _compare_empty_disclosures(
    left_module: dict[str, Any], right_module: dict[str, Any],
) -> str:
    """Compare verified chapter-level disclosures when neither side has table rows."""
    left = left_module.get("disclosures") or []
    right = right_module.get("disclosures") or []
    if not left or not right:
        return "UNRESOLVED_STRUCTURE"
    if any(module.get("status") != "EXTRACTED" for module in (left_module, right_module)):
        return "UNRESOLVED_STRUCTURE"
    left_keys = [_disclosure_key(item) for item in left]
    right_keys = [_disclosure_key(item) for item in right]
    if any(key is None for key in (*left_keys, *right_keys)):
        return "UNRESOLVED_STRUCTURE"
    if any(item.get("status") != "NO_DATA" for item in (*left, *right)):
        return "UNRESOLVED_STRUCTURE"
    if sorted(left_keys) == sorted(right_keys):
        return "MATCH"
    return "UNRESOLVED_STRUCTURE"


def _no_data_conflicts_with_rows(
    empty_module: dict[str, Any], empty_doc: dict[str, Any],
    populated_module: dict[str, Any], populated_doc: dict[str, Any],
    matching: dict[str, Any],
) -> bool | None:
    """Check whether a verified no-data statement contradicts rows in the same scope."""
    claims = [claim for claim in empty_module.get("disclosures", [])
              if claim.get("status") == "NO_DATA"]
    if empty_module.get("status") != "EXTRACTED" or not claims:
        return None
    populated_period = str(populated_doc.get("document_info", {}).get("period_end") or "")
    if not populated_period or not populated_module.get("rows"):
        return None
    scope_fields = matching.get("scope_fields", [])
    for claim in claims:
        claim_period = str(claim.get("period_end") or empty_doc.get("document_info", {}).get("period_end") or "")
        if not claim_period or _generic_date(claim_period) != _generic_date(populated_period):
            continue
        claim_scope = claim.get("scope")
        if not isinstance(claim_scope, dict) or not claim_scope:
            continue
        applicable_fields = [field for field in scope_fields if field in claim_scope]
        if not applicable_fields:
            continue
        if all(
            all(_generic_normalize_identity(row.get(field)) == _generic_normalize_identity(claim_scope[field])
                for row in populated_module["rows"])
            for field in applicable_fields
        ):
            return True
    return None


def _numeric_equal(left: dict[str, Any], right: dict[str, Any]) -> bool | None:
    left_kind, right_kind = left.get("value_kind"), right.get("value_kind")
    if left_kind == "DASH" or right_kind == "DASH":
        return left_kind == right_kind
    left_value = left.get("normalized_value")
    right_value = right.get("normalized_value")
    left_unit = _canonical_unit(left.get("unit"))
    right_unit = _canonical_unit(right.get("unit"))
    if left_value is None:
        left_value, inferred_unit = _decimal_value(left.get("raw_text"))
        left_unit = left_unit or inferred_unit
    if right_value is None:
        right_value, inferred_unit = _decimal_value(right.get("raw_text"))
        right_unit = right_unit or inferred_unit
    if left_value == "DASH" or right_value == "DASH":
        return left_value == right_value
    if left_value is None or right_value is None:
        return None
    if bool(left_unit) != bool(right_unit) or (left_unit and right_unit and left_unit != right_unit):
        return None
    try:
        return Decimal(str(left_value)) == Decimal(str(right_value))
    except (InvalidOperation, TypeError):
        return None


def _decimal_value(raw: Any) -> tuple[str | None, str]:
    """Parse one unambiguous numeric fact and its explicit unit generically."""
    text = str(raw or "").strip()
    if not text:
        return None, ""
    if text in {"-", "—", "–"}:
        return "DASH", ""
    import re
    matches = re.findall(r"(?<![A-Za-z0-9_.])[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?(?![A-Za-z0-9_.])", text)
    if len(matches) != 1:
        return None, ""
    token = matches[0].replace(",", "")
    if token.endswith("%"):
        token = token[:-1]
        inferred_unit = "%"
    else:
        unit_match = re.search(r"(?:人民币|港币|美元)?\s*(亿元|万元|元|股|份|人|天|年|月|日|倍)", text)
        inferred_unit = _canonical_unit(unit_match.group(1)) if unit_match else ""
    try:
        return format(Decimal(token).normalize(), "f"), inferred_unit
    except (InvalidOperation, ValueError):
        return None, ""


def _generic_date(value: Any) -> str:
    import re
    from datetime import date
    text = str(value or "").strip()
    match = re.search(r"(?<!\d)(\d{4})\s*(?:年|[-/.])\s*(\d{1,2})\s*(?:月|[-/.])\s*(\d{1,2})\s*日?(?!\d)", text)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
        except ValueError:
            pass
    return normalize_text(text)


def _field_difference(
    row_id: str, row_label: str, key: str, left: dict[str, Any] | None,
    right: dict[str, Any] | None, status: str, message: str, field_label: str | None = None,
) -> dict[str, Any]:
    return {
        "row_id": row_id,
        "row_label": row_label,
        "field_key": key,
        "field_label": field_label or key,
        "status": status,
        "message": message,
        "prospectus": left,
        "periodic_report": right,
    }


def _field_value(row: dict[str, Any], field: str) -> dict[str, Any] | None:
    value = row.get("fields", {}).get(field)
    if isinstance(value, dict):
        return value
    raw = row.get(field)
    if raw is None:
        return None
    evidence = row.get(f"{field}_evidence") or row.get("row_evidence")
    return {"raw_text": str(raw), "evidence": evidence if isinstance(evidence, dict) else {"status": "UNVERIFIED"}}


def _generic_normalize_identity(value: Any, operation: str = "text") -> str:
    if operation == "identifier":
        return normalize_identifier(value)
    return normalize_text(value)


def _identity_equivalence_map(matching: dict[str, Any]) -> dict[str, str]:
    """Build the rule-declared, normalized name equivalences for row identity."""
    result: dict[str, str] = {}
    for group in matching.get("identity_equivalence_groups", []):
        canonical = _generic_normalize_identity(group.get("canonical_identity"))
        if not canonical:
            continue
        for label in [group.get("canonical_identity"), *group.get("equivalent_labels", [])]:
            normalized = _generic_normalize_identity(label)
            if normalized:
                result[normalized] = canonical
    return result


def _canonical_row_name(value: Any, matching: dict[str, Any]) -> str:
    """Apply only rule-declared label aliases for identity matching."""
    text = unicodedata.normalize("NFKC", str(value or ""))
    for prefix in sorted(matching.get("identity_prefixes_to_ignore", []), key=len, reverse=True):
        marker = unicodedata.normalize("NFKC", str(prefix or "")).strip().rstrip(":：")
        if marker:
            text = re.sub(r"^\s*" + re.escape(marker) + r"\s*[:：]?\s*", "", text, count=1, flags=re.IGNORECASE)
    normalized = _generic_normalize_identity(text)
    return _identity_equivalence_map(matching).get(normalized, normalized)


def _row_identity_value(row: dict[str, Any], field: str, matching: dict[str, Any]) -> str:
    value = row.get(field)
    if value is None:
        value = (row.get("fields") or {}).get(field)
    if isinstance(value, dict):
        value = value.get("raw_text")
    if field == "label":
        return _canonical_row_name(value, matching)
    operation = "identifier" if field.endswith(("_code", "_id")) else "text"
    return _generic_normalize_identity(value, operation)


def _generic_identifier_value(value: Any) -> str | None:
    """Compare the identifier token in a cited value, not its varying label."""
    text = unicodedata.normalize("NFKC", str(value or "")).upper()
    cjk_positions = [index for index, character in enumerate(text)
                     if "\u3400" <= character <= "\u9fff"]
    if cjk_positions:
        text = text[cjk_positions[-1] + 1:]
    elif ":" in text or "：" in text:
        text = re.split(r"[:：]", text)[-1]
    tokens = re.findall(r"[A-Z0-9]+", text)
    candidates = [token for token in tokens if any(character.isdigit() for character in token)]
    distinct = set(candidates)
    if len(distinct) == 1:
        value_token = distinct.pop()
        preceding = tokens[:tokens.index(value_token)]
        if len(preceding) == 1 and preceding[0].isalpha():
            return preceding[0] + value_token
        if preceding and not cjk_positions:
            return None
        return value_token
    if distinct:
        return None
    if cjk_positions:
        # A compound Chinese description can contain several labeled IDs.
        # If no unique identifier token remains after its label context, do
        # not compare the whole prose string as though it were an ID.
        return None
    normalized = normalize_identifier(text)
    return normalized or None


def compare_declared_field(
    field_spec: dict[str, Any], left: dict[str, Any] | None, right: dict[str, Any] | None,
    *, semantic_status: str | None = None,
) -> str:
    """Apply one plan-declared comparison operation to two verified fact values.

    Business meaning is carried by the plan. This helper only dispatches among
    shared operations and returns a neutral comparison state.
    """
    if not isinstance(left, dict) or not isinstance(right, dict):
        return "UNRESOLVED"
    operation = field_spec.get("operation")
    if operation == "decimal":
        equal = _numeric_equal(left, right)
    elif operation == "semantic":
        if semantic_status in {"MATCH", "MISMATCH"}:
            return semantic_status
        equal = None
    elif operation == "date":
        left_date, right_date = _generic_date(left.get("raw_text")), _generic_date(right.get("raw_text"))
        equal = left_date == right_date if left_date and right_date else None
    elif operation == "identifier":
        left_identifier = _generic_identifier_value(left.get("raw_text"))
        right_identifier = _generic_identifier_value(right.get("raw_text"))
        equal = (left_identifier == right_identifier
                 if left_identifier is not None and right_identifier is not None else None)
    else:
        equal = _generic_normalize_identity(left.get("raw_text"), operation) == _generic_normalize_identity(
            right.get("raw_text"), operation,
        )
    if equal is None:
        return "UNRESOLVED"
    return "MATCH" if equal else "MISMATCH"


def _plan_row_candidates(
    row: dict[str, Any], matching: dict[str, Any], parent_labels: dict[str, str] | None = None,
) -> list[str]:
    scopes = []
    for field in matching.get("scope_fields", []):
        value = row.get(field)
        normalized = normalize_text(value)
        if normalized and normalized not in {"unknown", "未知", "不确定"}:
            scopes.append(f"{field}={normalized}")
    hierarchy = []
    for field in matching.get("hierarchy_fields", []):
        value = row.get(field)
        normalized = normalize_text(value)
        # Parent keys are document-local pointers. Compare their resolved label,
        # when available, so two documents can match the same hierarchy even
        # when each extractor assigned a different internal row key.
        if field == "parent_key" and normalized and parent_labels:
            normalized = parent_labels.get(normalized, normalized)
        if normalized and normalized not in {"unknown", "未知", "不确定"}:
            hierarchy.append(f"{field}={normalized}")
    prefix = "|".join((*scopes, *hierarchy))
    candidates: list[str] = []
    for kind, fields in (("primary", matching.get("identity_fields", [])),
                         ("fallback", matching.get("fallback_identity_fields", []))):
        for field in fields:
            normalized = _row_identity_value(row, field, matching)
            if normalized:
                candidates.append(f"{prefix}|{kind}:{field}={normalized}")
    label = row.get("label")
    canonical_label = _canonical_row_name(label, matching)
    if label and canonical_label and _generic_normalize_identity(label) in _identity_equivalence_map(matching):
        candidates.append(f"{prefix}|declared_equivalence={canonical_label}")
    return candidates


def _plan_identity_candidates(row: dict[str, Any], matching: dict[str, Any]) -> set[str]:
    fields = matching.get("identity_fields", [])
    values = []
    for field in fields:
        normalized = _row_identity_value(row, field, matching)
        if normalized:
            values.append(f"{field}={normalized}")
    # A stable key is preferred, but it is often document-local (or generated
    # differently by two extractors). Keep declared fallback identities as
    # additional constrained candidates so the caller can use them only when
    # the primary identity does not line up and the row is unique.
    for field in matching.get("fallback_identity_fields", []):
        normalized = _row_identity_value(row, field, matching)
        if normalized:
            values.append(f"{field}={normalized}")
    label = row.get("label")
    canonical_label = _canonical_row_name(label, matching)
    if label and canonical_label and _generic_normalize_identity(label) in _identity_equivalence_map(matching):
        values.append(f"declared_equivalence={canonical_label}")
    return set(values)


def _row_dimension_values(
    row: dict[str, Any], fields: list[str], parent_labels: dict[str, str] | None = None,
    matching: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Return known declared dimensions; blank/unknown values are unspecified."""
    unknown = {"unknown", "未知", "不确定", "未识别", "不详"}
    result: dict[str, str] = {}
    for field in fields:
        value = row.get(field)
        if value is None:
            value = row.get("fields", {}).get(field)
        if isinstance(value, dict):
            value = value.get("raw_text")
        normalized = normalize_text(value)
        if not normalized or normalized in unknown:
            continue
        if field == "parent_key":
            # A parent key is a document-local pointer, not a business label.
            # Compare it only when it resolves to a row in this document; an
            # unknown pointer is missing hierarchy evidence, not a conflict.
            if not parent_labels or normalized not in parent_labels:
                continue
            normalized = parent_labels[normalized]
            normalized = _canonical_row_name(normalized, matching or {})
        result[field] = normalized
    return result


def _row_dimensions_compatible(
    left: dict[str, Any], right: dict[str, Any], matching: dict[str, Any],
    left_parent_labels: dict[str, str], right_parent_labels: dict[str, str],
) -> bool:
    """Allow an unspecified dimension to pair only under a shared row identity.

    Known conflicting scope or hierarchy values remain distinct. The caller
    requires a shared declared identity and a unique compatible candidate.
    """
    fields = list(dict.fromkeys([
        *matching.get("scope_fields", []), *matching.get("hierarchy_fields", []),
    ]))
    left_values = _row_dimension_values(left, fields, left_parent_labels, matching)
    right_values = _row_dimension_values(right, fields, right_parent_labels, matching)
    return all(
        left_values[field] == right_values[field]
        for field in left_values.keys() & right_values.keys()
    )


def _required_scope_is_present(row: dict[str, Any], matching: dict[str, Any], parent_labels: dict[str, str]) -> bool:
    required = matching.get("required_scope_fields", [])
    if not required:
        return True
    values = _row_dimension_values(row, required, parent_labels, matching)
    return all(field in values for field in required)


def _generic_align_rows(
    left_rows: list[dict[str, Any]], right_rows: list[dict[str, Any]], matching: dict[str, Any],
    target_population: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], set[str]]:
    def parent_labels(rows: list[dict[str, Any]]) -> dict[str, str]:
        aliases: dict[str, str] = {}
        identity_fields = [*matching.get("identity_fields", []), *matching.get("fallback_identity_fields", [])]
        for row in rows:
            label = normalize_text(row.get("label"))
            if not label:
                continue
            canonical_label = _canonical_row_name(label, matching)
            for field in identity_fields:
                value = row.get(field)
                if value is None:
                    value = row.get("fields", {}).get(field)
                if isinstance(value, dict):
                    value = value.get("raw_text")
                alias = _generic_normalize_identity(
                    value, "identifier" if field.endswith(("_code", "_id")) else "text",
                )
                if alias:
                    aliases[alias] = canonical_label
        return aliases

    left_parent_labels, right_parent_labels = parent_labels(left_rows), parent_labels(right_rows)
    left_candidates = [_plan_row_candidates(row, matching, left_parent_labels) for row in left_rows]
    right_candidates = [_plan_row_candidates(row, matching, right_parent_labels) for row in right_rows]
    left_identity = [_plan_identity_candidates(row, matching) for row in left_rows]
    right_identity = [_plan_identity_candidates(row, matching) for row in right_rows]
    left_counts: dict[str, int] = {}
    right_counts: dict[str, int] = {}
    for candidate in (value for row in left_candidates for value in row):
        left_counts[candidate] = left_counts.get(candidate, 0) + 1
    for candidate in (value for row in right_candidates for value in row):
        right_counts[candidate] = right_counts.get(candidate, 0) + 1

    left_index: dict[str, dict[str, Any]] = {}
    right_index: dict[str, dict[str, Any]] = {}
    unresolved: set[str] = set()
    claimed_right: set[int] = set()
    matched_left: set[int] = set()
    for left_idx, (row, candidates) in enumerate(zip(left_rows, left_candidates)):
        if not _required_scope_is_present(row, matching, left_parent_labels):
            unresolved.add(f"left:{left_idx}")
            continue
        shared = [candidate for candidate in candidates
                  if left_counts.get(candidate) == 1 and right_counts.get(candidate) == 1]
        right_matches = {
            right_idx for right_idx, other_candidates in enumerate(right_candidates)
            if any(candidate in other_candidates for candidate in shared)
        }
        # Structured extraction can leave a declared scope/hierarchy unknown
        # on one side even when the row's strong identity is unique. Use the
        # identity as a constrained fallback: a known conflict is never
        # bridged, and ambiguity remains unresolved.
        if not right_matches and left_identity[left_idx]:
            possible = {
                right_idx for right_idx, other_identity in enumerate(right_identity)
                if left_identity[left_idx] & other_identity
                and _row_dimensions_compatible(
                    row, right_rows[right_idx], matching,
                    left_parent_labels, right_parent_labels,
                )
            }
            if len(possible) == 1:
                right_matches = possible
        if len(right_matches) == 0:
            if left_identity[left_idx] and any(left_identity[left_idx] & identity for identity in right_identity):
                unresolved.add(f"left:{left_idx}")
                continue
            unique_left_candidates = [candidate for candidate in candidates if left_counts.get(candidate) == 1]
            collides_in_reference = any(right_counts.get(candidate, 0) > 1 for candidate in candidates)
            if unique_left_candidates and not collides_in_reference:
                left_index[unique_left_candidates[0]] = row
            else:
                unresolved.add(f"left:{left_idx}")
            continue
        if len(right_matches) > 1:
            unresolved.add(f"left:{left_idx}")
            continue
        right_idx = next(iter(right_matches))
        if not _required_scope_is_present(right_rows[right_idx], matching, right_parent_labels):
            unresolved.add(f"left:{left_idx}")
            continue
        if right_idx in claimed_right:
            unresolved.add(f"left:{left_idx}")
            unresolved.add(f"right:{right_idx}")
            continue
        shared_exact = [candidate for candidate in candidates if candidate in right_candidates[right_idx]]
        if shared_exact:
            canonical = shared_exact[0]
        else:
            identity = sorted(left_identity[left_idx] & right_identity[right_idx])[0]
            dimensions = _row_dimension_values(row, [
                *matching.get("scope_fields", []), *matching.get("hierarchy_fields", []),
            ], left_parent_labels, matching)
            dimensions.update(_row_dimension_values(right_rows[right_idx], [
                *matching.get("scope_fields", []), *matching.get("hierarchy_fields", []),
            ], right_parent_labels, matching))
            suffix = "|".join(f"{field}={dimensions[field]}" for field in sorted(dimensions))
            canonical = f"{identity}|{suffix}" if suffix else identity
        left_index[canonical] = row
        right_index[canonical] = right_rows[right_idx]
        matched_left.add(left_idx)
        claimed_right.add(right_idx)

    if target_population == "both_documents":
        for left_idx, row in enumerate(left_rows):
            if left_idx not in matched_left:
                candidate = next(iter(left_candidates[left_idx]), f"unmapped:left:{left_idx}")
                if candidate not in left_index:
                    left_index[candidate] = row
                else:
                    unresolved.add(f"left:{left_idx}")
        for right_idx, row in enumerate(right_rows):
            if right_idx not in claimed_right:
                candidate = next(iter(right_candidates[right_idx]), f"unmapped:right:{right_idx}")
                if candidate not in right_index:
                    right_index[candidate] = row
                else:
                    unresolved.add(f"right:{right_idx}")
    return left_index, right_index, unresolved


def _generic_coverage_complete(module: dict[str, Any]) -> bool:
    if module.get("status") != "EXTRACTED" or module.get("completeness_warnings"):
        return False
    scope = module.get("scope") or {}
    disclosure_scope = scope.get("disclosure_scope")
    return disclosure_scope in {"all", "complete", "all_rows"} or module.get("coverage_complete") is True


def _generic_path_value(doc: dict[str, Any], module: dict[str, Any], path: str) -> Any:
    """Read a rule-declared data path without interpreting its business name."""
    roots = {"document": doc, "module": module}
    parts = path.split(".")
    value: Any = roots.get(parts[0])
    for part in parts[1:]:
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    if isinstance(value, dict):
        if "raw_text" in value:
            return value.get("raw_text")
        if "value" in value:
            return value.get("value")
    return value


def _context_values_match(left_value: Any, right_value: Any, operation: str) -> bool | None:
    if left_value in (None, "") or right_value in (None, ""):
        return None
    if operation in {"before_or_equal", "reference_before_or_equal"}:
        left_date, right_date = _generic_date(left_value), _generic_date(right_value)
        if not left_date or not right_date:
            return None
        if operation == "reference_before_or_equal":
            return right_date <= left_date
        return left_date <= right_date
    if operation == "decimal":
        return _numeric_equal({"raw_text": str(left_value)}, {"raw_text": str(right_value)})
    return _generic_normalize_identity(left_value, operation) == _generic_normalize_identity(right_value, operation)


def evaluate_declared_context(
    plan: dict[str, Any], target_doc: dict[str, Any], reference_doc: dict[str, Any],
    *, reference_role: str | None = None,
) -> tuple[bool, list[dict[str, Any]]]:
    """Evaluate common and reference-role-specific context declared by a rule plan."""
    comparison = plan.get("comparison", {})
    assessments: list[dict[str, Any]] = []
    unresolved = False

    def assess(check: dict[str, Any], *, required: bool = True) -> bool | None:
        left = _generic_path_value(target_doc, {}, check["target_path"])
        right = _generic_path_value(reference_doc, {}, check["reference_path"])
        matches = _context_values_match(left, right, check["operation"])
        if matches is None and not required:
            return None
        assessments.append({"label": check["label"], "status": "MATCH" if matches else "UNRESOLVED"})
        return matches

    checks = [*comparison.get("context_checks", [])]
    if reference_role:
        checks.extend(comparison.get("context_checks_by_reference_role", {}).get(reference_role, []))
    for check in checks:
        matches = assess(check, required=check.get("required", True))
        if matches is False or matches is None and check.get("required", True):
            unresolved = True
    for group in comparison.get("context_check_groups", []):
        matched, conflict = 0, False
        for check in group["checks"]:
            result = assess(check, required=False)
            if result is True:
                matched += 1
            elif result is False:
                conflict = True
        group_ok = not conflict and matched >= group["minimum_matches"]
        assessments.append({"label": group["label"], "status": "MATCH" if group_ok else "UNRESOLVED"})
        unresolved = unresolved or not group_ok
    return not unresolved, assessments


def _generic_compare_module(
    plan: dict[str, Any], left_doc: dict[str, Any], right_doc: dict[str, Any], blocked_status: str | None,
) -> dict[str, Any]:
    rule_id = plan["rule_identity"]
    title = plan["display_title"]
    left_modules = {module.get("rule_id"): module for module in left_doc.get("modules", [])}
    right_modules = {module.get("rule_id"): module for module in right_doc.get("modules", [])}
    left_module, right_module = left_modules.get(rule_id), right_modules.get(rule_id)
    base = {"rule_id": rule_id, "title": title,
            "prospectus_rows": (left_module or {}).get("rows", []),
            "periodic_rows": (right_module or {}).get("rows", [])}
    if blocked_status:
        return {**base, "status": blocked_status, "differences": []}
    if not left_module or not right_module or any(
        module.get("status") == "UNRESOLVED_STRUCTURE" for module in (left_module, right_module) if module
    ):
        return {
            **base,
            "status": "UNRESOLVED_STRUCTURE",
            "differences": [],
            "review_message": "至少一侧章节结构尚未确认，不能据此判断模块缺失。",
        }
    missing_sides = [module for module in (left_module, right_module)
                     if module.get("status") == "MISSING_MODULE"]
    if missing_sides and len(missing_sides) == 2:
        return {
            **base,
            "status": "UNRESOLVED_STRUCTURE",
            "differences": [],
            "review_message": "两侧均未定位到该模块，尚不能确认其是否不适用。",
        }
    if missing_sides:
        diff = _field_difference(rule_id, title, "module", None, None, "MISSING_MODULE", "一侧未识别到对应内容")
        return {**base, "status": "MISSING_MODULE", "differences": [diff]}

    scope_unresolved = False
    for check in plan["comparison"].get("context_checks", []):
        left_value = _generic_path_value(left_doc, left_module, check["target_path"])
        right_value = _generic_path_value(right_doc, right_module, check["reference_path"])
        equal = _context_values_match(left_value, right_value, check["operation"])
        if equal is None:
            scope_unresolved = scope_unresolved or check["required"]
            continue
        scope_unresolved = scope_unresolved or equal is not True
    for group in plan["comparison"].get("context_check_groups", []):
        matched = 0
        conflict = False
        for check in group["checks"]:
            left_value = _generic_path_value(left_doc, left_module, check["target_path"])
            right_value = _generic_path_value(right_doc, right_module, check["reference_path"])
            equal = _context_values_match(left_value, right_value, check["operation"])
            if equal is None:
                continue
            if equal:
                matched += 1
            else:
                conflict = True
        if conflict or matched < group["minimum_matches"]:
            scope_unresolved = True
    if scope_unresolved:
        return {
            **base,
            "status": "UNRESOLVED_STRUCTURE",
            "differences": [],
            "review_message": "规则声明的基金身份、期间或适用范围未能确认一致，已停止逐行差异判定。",
        }

    left_rows = left_module.get("rows", [])
    right_rows = right_module.get("rows", [])
    matching = plan["row_matching"]
    if not left_rows and right_rows:
        conflict = _no_data_conflicts_with_rows(left_module, left_doc, right_module, right_doc, matching)
        if conflict:
            difference = _field_difference(
                rule_id, title, "disclosure_status", None, None, "MISMATCH",
                "同一报告期和适用范围内，一侧明确披露无数据，另一侧存在数据行。",
            )
            return {**base, "status": "MISMATCH", "differences": [difference],
                    "review_message": "明确无数据披露与同范围数据行相互矛盾。",
                    "prospectus_disclosures": left_module.get("disclosures", []),
                    "periodic_disclosures": right_module.get("disclosures", [])}
    elif left_rows and not right_rows:
        conflict = _no_data_conflicts_with_rows(right_module, right_doc, left_module, left_doc, matching)
        if conflict:
            difference = _field_difference(
                rule_id, title, "disclosure_status", None, None, "MISMATCH",
                "同一报告期和适用范围内，一侧明确披露无数据，另一侧存在数据行。",
            )
            return {**base, "status": "MISMATCH", "differences": [difference],
                    "review_message": "明确无数据披露与同范围数据行相互矛盾。",
                    "prospectus_disclosures": left_module.get("disclosures", []),
                    "periodic_disclosures": right_module.get("disclosures", [])}
    if not left_rows and not right_rows:
        def with_period(module: dict[str, Any], doc: dict[str, Any]) -> dict[str, Any]:
            period = doc.get("document_info", {}).get("period_end")
            return {**module, "disclosures": [
                {**claim, "period_end": claim.get("period_end") or period}
                for claim in module.get("disclosures", [])
            ]}
        disclosure_status = _compare_empty_disclosures(
            with_period(left_module, left_doc), with_period(right_module, right_doc),
        )
        if disclosure_status:
            differences = [] if disclosure_status == "MATCH" else [
                _field_difference(rule_id, title, "disclosure_status", None, None, "UNRESOLVED",
                                  "披露状态、期间或范围证据不足")
            ]
            return {**base, "status": disclosure_status, "differences": differences,
                    "review_message": None if disclosure_status == "MATCH" else "双方明确未持有披露的期间或适用范围证据不足。",
                    "prospectus_disclosures": left_module.get("disclosures", []),
                    "periodic_disclosures": right_module.get("disclosures", [])}

    target_population = matching["target_population"]
    target_only = target_population == "target_document" and matching["reference_superset_allowed"]
    left_index, right_index, unresolved_rows = _generic_align_rows(
        left_rows, right_rows, matching, "target_document" if target_only else "both_documents",
    )
    unresolved = bool(unresolved_rows or scope_unresolved)
    keys = set(left_index) | set(right_index)

    differences: list[dict[str, Any]] = []
    for key in sorted(keys):
        left_row, right_row = left_index.get(key), right_index.get(key)
        label = str((left_row or right_row or {}).get("label") or key)
        if left_row is None or right_row is None:
            if target_only and left_row is None:
                continue
            if unresolved_rows:
                # An unmatched row may be an identity/scope collision that the
                # shared matcher could not disambiguate. Preserve it for review
                # instead of turning a possible correspondence into a confirmed
                # one-sided disclosure.
                unresolved = True
                continue
            missing_side_module = left_module if left_row is None else right_module
            if (matching["require_complete_source_for_missing"]
                    and not _generic_coverage_complete(missing_side_module)):
                unresolved = True
                continue
            present = left_row or right_row or {}
            diff = _field_difference(key, label, "row",
                                     {"raw_text": present.get("label"), "evidence": present.get("row_evidence")} if left_row else None,
                                     {"raw_text": present.get("label"), "evidence": present.get("row_evidence")} if right_row else None,
                                     "MISSING_ROW", "仅在一侧文档中出现")
            differences.append(diff)
            continue
        for field_spec in plan["comparison"]["fields"]:
            field = field_spec["field"]
            left_value, right_value = _field_value(left_row, field), _field_value(right_row, field)
            if left_value is None or right_value is None:
                if field_spec["required"]:
                    unresolved = True
                    differences.append(_field_difference(key, label, field, left_value, right_value,
                                                         "UNRESOLVED", "比较所需字段未能完整识别"))
                continue
            if any((value.get("evidence") or {}).get("status") != "LOCATED" for value in (left_value, right_value)):
                unresolved = True
                differences.append(_field_difference(key, label, field, left_value, right_value,
                                                     "UNRESOLVED", "字段原文证据未能定位"))
                continue
            field_status = compare_declared_field(field_spec, left_value, right_value)
            if field_status == "UNRESOLVED":
                unresolved = True
                differences.append(_field_difference(key, label, field, left_value, right_value,
                                                     "UNRESOLVED", "字段值或比较口径无法确认"))
            elif field_status == "MISMATCH":
                differences.append(_field_difference(key, label, field, left_value, right_value,
                                                     "MISMATCH", f"{field_spec['label']}披露不一致",
                                                     field_label=field_spec["label"]))

    if any(item["status"] in {"MISMATCH", "MISSING_ROW", "MISSING_FIELD", "MISSING_MODULE"} for item in differences):
        status = "MISMATCH"
    elif unresolved or any(item["status"] == "UNRESOLVED" for item in differences):
        status = "UNRESOLVED_STRUCTURE"
    else:
        status = "MATCH"
    messages = []
    if unresolved_rows:
        messages.append("部分行身份或适用范围不能唯一匹配，已保留待复核状态。")
    if scope_unresolved:
        messages.append("基金身份、期间或规则声明的适用范围未能确认一致。")
    if not _generic_coverage_complete(right_module) and matching["require_complete_source_for_missing"]:
        messages.append("参考文档的披露范围或结构完整性尚未确认。")
    disclosure_ranges = None
    if any((module.get("scope") or {}).get("disclosure_scope") is not None
           for module in (left_module, right_module)):
        disclosure_ranges = {
            role: ((module.get("scope") or {}).get("disclosure_scope"),
                   (module.get("scope") or {}).get("limit"))
            for role, module in (("prospectus", left_module), ("periodic_report", right_module))
        }
    display_right_rows = right_rows
    if target_only:
        selected_reference_ids = {id(row) for row in right_index.values()}
        display_right_rows = [row for row in right_rows if id(row) in selected_reference_ids]
    return {**base, "status": status, "differences": differences,
            "periodic_rows": display_right_rows,
            "review_message": " ".join(messages) or None,
            **({"disclosure_ranges": disclosure_ranges} if disclosure_ranges is not None else {})}


def _compare_module_without_plan(
    module_id: str, left_doc: dict[str, Any], right_doc: dict[str, Any], blocked_status: str | None,
) -> dict[str, Any]:
    """Fail closed when no validated plan exists; never borrow chapter semantics."""
    left_modules = {module.get("rule_id"): module for module in left_doc.get("modules", [])}
    right_modules = {module.get("rule_id"): module for module in right_doc.get("modules", [])}
    left_module, right_module = left_modules.get(module_id), right_modules.get(module_id)
    title = (left_module or right_module or {}).get("title") or module_id
    rows = {
        "prospectus_rows": (left_module or {}).get("rows", []),
        "periodic_rows": (right_module or {}).get("rows", []),
    }
    if blocked_status:
        return {"rule_id": module_id, "title": title, "status": blocked_status, "differences": [], **rows}
    return {
        "rule_id": module_id,
        "title": title,
        "status": "UNRESOLVED_STRUCTURE",
        "differences": [],
        "review_message": "该规则没有可用的已验证执行计划，未套用其他规则的比较逻辑。",
        **rows,
    }


def _load_active_investment_plans() -> dict[str, dict[str, Any]]:
    """Discover plans from the rule directory; rule identities select data, not algorithms."""
    directory = Path(__file__).resolve().parents[1] / "rules" / "investment"
    plans: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("*.md")):
        if path.name.startswith("common_"):
            continue
        plan = load_compiled_execution_plan(path)
        if plan is not None:
            plans[plan["rule_identity"]] = plan
    return plans

def compare_documents(
    prospectus: dict[str, Any], periodic_report: dict[str, Any],
    execution_plans: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    left_info = prospectus.get("document_info", {})
    right_info = periodic_report.get("document_info", {})
    # Identity, report period and applicable disclosure time are evaluated by
    # each activated Markdown execution plan. The document-level comparator
    # only aggregates those declarations; it must not impose a second policy.
    blocked_status = None
    if execution_plans is None:
        execution_plans = _load_active_investment_plans()
    discovered_ids = [
        module.get("rule_id")
        for document in (prospectus, periodic_report)
        for module in document.get("modules", [])
        if isinstance(module.get("rule_id"), str) and module.get("rule_id")
    ]
    module_ids = list(dict.fromkeys([*discovered_ids, *(execution_plans or {}).keys()]))
    modules = []
    for module_id in module_ids:
        plan = execution_plans.get(module_id)
        if plan is not None:
            modules.append(_generic_compare_module(plan, prospectus, periodic_report, blocked_status))
        else:
            modules.append(_compare_module_without_plan(module_id, prospectus, periodic_report, blocked_status))
    differences = [item for module in modules for item in module["differences"]]
    real_differences = sum(item["status"] in {"MISMATCH", "MISSING_ROW", "MISSING_FIELD", "MISSING_MODULE", "RANK_ORDER_CHANGED"} for item in differences)
    if real_differences:
        status = "MISMATCH"
    elif any(module["status"] != "MATCH" for module in modules):
        status = "UNRESOLVED"
    else:
        status = "MATCH"
    return {
        "status": status,
        "documents": {
            "prospectus": {"filename": prospectus.get("filename"), "page_count": prospectus.get("page_count"), "page_dimensions": prospectus.get("page_dimensions", []), "document_info": left_info, "metadata_evidence": prospectus.get("metadata_evidence", {})},
            "periodic_report": {"filename": periodic_report.get("filename"), "page_count": periodic_report.get("page_count"), "page_dimensions": periodic_report.get("page_dimensions", []), "document_info": right_info, "metadata_evidence": periodic_report.get("metadata_evidence", {})},
        },
        "difference_count": real_differences,
        "warnings": [],
        "modules": modules,
    }
