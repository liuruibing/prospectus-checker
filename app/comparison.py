from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from app.domain import MODULE_RULES, normalize_identifier, normalize_stock_code, normalize_text


MODULE_TITLES = {
    "investment.asset_allocation": "报告期末基金资产组合情况",
    "investment.industry_stock_portfolio": "报告期末按行业分类的股票投资组合",
    "investment.stock_holdings": "期末按公允价值占基金资产净值比例排序的股票投资明细",
}
MODULE_FIELDS = {rule["rule_id"]: set(rule["fields"]) for rule in MODULE_RULES}
FIELD_LABELS = {
    "amount": "金额", "ratio_of_total_assets": "占基金总资产比例",
    "fair_value": "公允价值", "ratio_of_net_assets": "占基金资产净值比例",
    "quantity": "持有数量", "rank": "披露序号", "stock_name": "股票名称",
    "row": "项目", "module": "模块", "row_identity": "行标识",
}

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

def _row_has_only_empty_metrics(module_id: str, row: dict[str, Any]) -> bool:
    fields = row.get("fields", {})
    return all(
        isinstance(fields.get(key), dict)
        and fields[key].get("value_kind") in {"DASH", "EMPTY"}
        and fields[key].get("evidence", {}).get("status") == "LOCATED"
        for key in MODULE_FIELDS[module_id]
    )


def _investment_scope_key(value: Any) -> str:
    investment = normalize_text(value or "unknown")
    return {"指数投资": "index", "指数": "index", "index": "index",
            "积极投资": "active", "积极": "active", "active": "active"}.get(investment, investment)


def _industry_identity_scope(row: dict[str, Any]) -> str:
    market = normalize_text(row.get("market_scope") or "unknown")
    investment = _investment_scope_key(row.get("investment_scope"))
    return f"{market}:{investment}"


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


def _holdings_coverage(module: dict[str, Any]) -> tuple[str, int | None] | None:
    scope = module.get("scope") or {}
    if scope.get("disclosure_scope") == "all":
        return "all", None
    if scope.get("disclosure_scope") == "top_n":
        try:
            limit = int(scope.get("limit"))
        except (TypeError, ValueError):
            return None
        if limit > 0:
            return "top_n", limit
    return None


def _reference_covers_target_coverage(
    target: tuple[str, int | None] | None,
    reference: tuple[str, int | None] | None,
) -> bool:
    """The periodic report may contain a superset of the prospectus target rows."""
    if target is None or reference is None:
        return False
    target_kind, target_limit = target
    reference_kind, reference_limit = reference
    if target_kind == "all":
        return reference_kind == "all"
    if target_kind != "top_n" or target_limit is None:
        return False
    return reference_kind == "all" or (
        reference_kind == "top_n" and reference_limit is not None and reference_limit >= target_limit
    )



def _numeric_equal(left: dict[str, Any], right: dict[str, Any]) -> bool | None:
    left_kind, right_kind = left.get("value_kind"), right.get("value_kind")
    if left_kind == "DASH" or right_kind == "DASH":
        return left_kind == right_kind
    if left_kind != "NUMBER" or right_kind != "NUMBER":
        return None
    left_unit, right_unit = _canonical_unit(left.get("unit")), _canonical_unit(right.get("unit"))
    if left_unit and right_unit and left_unit != right_unit:
        return None
    try:
        return Decimal(str(left["normalized_value"])) == Decimal(str(right["normalized_value"]))
    except (InvalidOperation, TypeError, KeyError):
        return None


def _row_key(module_id: str, row: dict[str, Any], counts: dict[str, int]) -> tuple[str | None, bool]:
    if module_id == "investment.asset_allocation":
        key = str(row.get("row_key") or "").strip()
        if key:
            identity = f"{row.get('parent_key') or ''}/{key}"
            return identity, counts.get(f"semantic:{identity}", 0) == 1
        label = normalize_text(row.get("label"))
        fallback = f"{row.get('parent_key') or ''}/{label}"
        return (f"unmapped:{fallback}", counts.get(f"unmapped:{fallback}", 0) == 1) if label else (None, False)
    if module_id == "investment.industry_stock_portfolio":
        scope = _industry_identity_scope(row)
        if scope.split(":", 1)[0] not in {"domestic", "hk_connect", "other"} or scope.endswith(":unknown"):
            return None, False
        name = normalize_text(row.get("industry_name") or row.get("label"))
        if name == "合计":
            return f"{scope}:total", True
        code = normalize_identifier(row.get("industry_code"))
        if code:
            key = f"{scope}:code:{code}"
            return key, counts.get(f"{scope}:{code}", 0) == 1
        if name and counts.get(f"{scope}:{name}", 0) == 1:
            return f"{scope}:name:{name}", False
        return None, False
    scope = f"{normalize_text(row.get('market_scope') or 'unknown')}:{_investment_scope_key(row.get('investment_scope'))}"
    code = normalize_stock_code(row.get("stock_code"))
    if not code or counts.get(f"{scope}:{code}", 0) > 1 or (scope.startswith("unknown:") and counts.get(f"all:{code}", 0) > 1):
        return None, False
    return f"{scope}:{code}", True


def _row_index(module_id: str, rows: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], set[str]]:
    counts: dict[str, int] = {}
    for row in rows:
        if module_id == "investment.asset_allocation":
            row_key = str(row.get("row_key") or "").strip()
            if row_key:
                identity = f"{row.get('parent_key') or ''}/{row_key}"
                counts[f"semantic:{identity}"] = counts.get(f"semantic:{identity}", 0) + 1
            label = normalize_text(row.get("label"))
            if label:
                identity = f"{row.get('parent_key') or ''}/{label}"
                counts[f"unmapped:{identity}"] = counts.get(f"unmapped:{identity}", 0) + 1
            continue
        elif module_id == "investment.industry_stock_portfolio":
            scope = _industry_identity_scope(row)
            identity = normalize_identifier(row.get("industry_code")) or normalize_text(row.get("industry_name") or row.get("label"))
        elif module_id == "investment.stock_holdings":
            scope = f"{normalize_text(row.get('market_scope') or 'unknown')}:{_investment_scope_key(row.get('investment_scope'))}"
            identity = normalize_stock_code(row.get("stock_code"))
            if identity:
                counts[f"all:{identity}"] = counts.get(f"all:{identity}", 0) + 1
        else:
            continue
        if identity:
            key = f"{scope}:{identity}"
            counts[key] = counts.get(key, 0) + 1
    result: dict[str, dict[str, Any]] = {}
    unresolved: set[str] = set()
    for index, row in enumerate(rows):
        key, confident = _row_key(module_id, row, counts)
        if not key or key in result or key in unresolved or not confident:
            result.pop(key, None)
            unresolved.add(key or f"unmapped:{index}")
            continue
        result[key] = row
    return result, unresolved


def _align_asset_rows(left_rows: list[dict], right_rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Use unique source labels as a conservative fallback for model key drift."""
    left, right = ([dict(row) for row in rows] for rows in (left_rows, right_rows))
    sides = (left, right)
    parent_maps = []
    semantic_parent_keys = []
    for rows in sides:
        keyed: dict[str, list[dict]] = {}
        labels: dict[str, list[dict]] = {}
        for row in rows:
            key = str(row.get("row_key") or "").strip()
            label = normalize_text(row.get("label"))
            if key:
                keyed.setdefault(key, []).append(row)
            if label:
                labels.setdefault(label, []).append(row)
        parent_maps.append((keyed, labels))
        semantic_parent_keys.append(set(keyed))
    shared_keys = semantic_parent_keys[0] & semantic_parent_keys[1]
    shared_labels = set(parent_maps[0][1]) & set(parent_maps[1][1])
    parent_tokens = []
    for side_index, rows in enumerate(sides):
        keyed, labels = parent_maps[side_index]
        tokens = {}
        for key, parents in keyed.items():
            if len(parents) == 1:
                label = normalize_text(parents[0].get("label"))
                if key in shared_keys:
                    tokens[key] = f"semantic:{key}"
                elif label in shared_labels and len(parent_maps[1 - side_index][1].get(label, [])) == 1:
                    tokens[key] = f"label:{label}"
        parent_tokens.append(tokens)
    for side_index, rows in enumerate(sides):
        tokens = parent_tokens[side_index]
        for row in rows:
            parent_key = str(row.get("parent_key") or "").strip()
            if parent_key in tokens:
                row["parent_key"] = tokens[parent_key]

    indexes = []
    for rows in sides:
        index: dict[tuple[str, str], list[dict]] = {}
        for row in rows:
            label = normalize_text(row.get("label"))
            parent = str(row.get("parent_key") or "")
            if label:
                index.setdefault((parent, label), []).append(row)
        indexes.append(index)
    for identity in set(indexes[0]) & set(indexes[1]):
        first, second = indexes[0][identity], indexes[1][identity]
        if len(first) != 1 or len(second) != 1:
            for row in (*first, *second):
                row["schema_warnings"] = [*row.get("schema_warnings", []), "ambiguous_source_label"]
            continue
        row_a, row_b = first[0], second[0]
        key_a = str(row_a.get("row_key") or "").strip()
        key_b = str(row_b.get("row_key") or "").strip()
        if key_a != key_b:
            shared = f"source-label:{identity[0]}/{identity[1]}"
            row_a["row_key"] = shared
            row_b["row_key"] = shared
    return left, right


def _align_stock_markets(left_rows: list[dict], right_rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Use a unique security code when a market is absent, never across conflicting scopes."""
    left = [dict(row) for row in left_rows]
    right = [dict(row) for row in right_rows]
    indexes = []
    for rows in (left, right):
        index: dict[str, list[dict]] = {}
        for row in rows:
            code = normalize_stock_code(row.get("stock_code"))
            if code:
                index.setdefault(code, []).append(row)
        indexes.append(index)
    for code in set(indexes[0]) & set(indexes[1]):
        a, b = indexes[0][code], indexes[1][code]
        if len(a) != 1 or len(b) != 1:
            continue
        first, second = a[0], b[0]
        market_a, market_b = first.get("market_scope") or "unknown", second.get("market_scope") or "unknown"
        scope_a = _investment_scope_key(first.get("investment_scope"))
        scope_b = _investment_scope_key(second.get("investment_scope"))
        if (market_a != "unknown" and market_b != "unknown" and market_a != market_b) or (
            scope_a != "unknown" and scope_b != "unknown" and scope_a != scope_b
        ):
            first["schema_warnings"] = [*first.get("schema_warnings", []), "scope_conflict"]
            second["schema_warnings"] = [*second.get("schema_warnings", []), "scope_conflict"]
            continue
        market = market_a if market_a != "unknown" else market_b
        first["market_scope"] = second["market_scope"] = market
    return left, right


def _field_difference(row_id: str, row_label: str, key: str, left: dict[str, Any] | None, right: dict[str, Any] | None, status: str, message: str) -> dict[str, Any]:
    return {
        "row_id": row_id,
        "row_label": row_label,
        "field_key": key,
        "field_label": FIELD_LABELS.get(key, key),
        "status": status,
        "message": message,
        "prospectus": left,
        "periodic_report": right,
    }


def _compare_module(module_id: str, left_doc: dict[str, Any], right_doc: dict[str, Any], blocked_status: str | None) -> dict[str, Any]:
    left_modules = {module.get("rule_id"): module for module in left_doc.get("modules", [])}
    right_modules = {module.get("rule_id"): module for module in right_doc.get("modules", [])}
    left_module, right_module = left_modules.get(module_id), right_modules.get(module_id)
    title = MODULE_TITLES[module_id]
    if blocked_status:
        return {"rule_id": module_id, "title": title, "status": blocked_status, "differences": [],
                "prospectus_rows": (left_module or {}).get("rows", []), "periodic_rows": (right_module or {}).get("rows", [])}
    if not left_module or not right_module or left_module.get("status") == "MISSING_MODULE" or right_module.get("status") == "MISSING_MODULE":
        difference = _field_difference(module_id, title, "module", None, None, "MISSING_MODULE", "一侧未识别到可核对的对应模块")
        return {"rule_id": module_id, "title": title, "status": "MISSING_MODULE", "differences": [difference],
                "prospectus_rows": (left_module or {}).get("rows", []), "periodic_rows": (right_module or {}).get("rows", [])}
    left_rows = left_module.get("rows", [])
    right_rows = right_module.get("rows", [])
    if bool(left_rows) != bool(right_rows):
        empty_module = right_module if left_rows else left_module
        empty_claims = empty_module.get("disclosures") or []
        if (empty_claims and all(item.get("status") == "NO_DATA" and _disclosure_key(item) is not None for item in empty_claims)
                and all(module.get("status") == "EXTRACTED" for module in (left_module, right_module))):
            difference = _field_difference(
                module_id, title, "disclosure_status", None, None, "MISMATCH",
                "一份文档明确披露无数据，另一份文档披露了数据行",
            )
            return {
                "rule_id": module_id, "title": title, "status": "MISMATCH",
                "differences": [difference],
                "review_message": "两份文档的披露状态不同。",
                "prospectus_rows": left_rows, "periodic_rows": right_rows,
            }
    if not left_rows and not right_rows:
        def with_document_period(module, doc):
            return {**module, "disclosures": [
                {**claim, "period_end": claim.get("period_end") or doc.get("document_info", {}).get("period_end")}
                for claim in module.get("disclosures", [])
            ]}
        disclosure_status = _compare_empty_disclosures(
            with_document_period(left_module, left_doc), with_document_period(right_module, right_doc),
        )
        if disclosure_status:
            return {
                "rule_id": module_id,
                "title": title,
                "status": disclosure_status,
                "differences": [] if disclosure_status == "MATCH" else [
                    _field_difference(module_id, title, "disclosure_status", None, None,
                                      "MISMATCH" if disclosure_status == "MISMATCH" else "UNRESOLVED",
                                      "两份文档的披露状态或适用范围不同" if disclosure_status == "MISMATCH"
                                      else "披露状态或适用范围证据不足，需复核")
                ],
                "review_message": "两侧原文均明确披露无数据，报告期及适用范围一致。" if disclosure_status == "MATCH" else "两份文档的披露原文、报告期或适用范围未能确认完整对应。" + " ".join(
                    message for module in (left_module, right_module) for message in module.get("completeness_warnings", [])
                ),
                "prospectus_disclosures": left_module.get("disclosures", []),
                "periodic_disclosures": right_module.get("disclosures", []),
                "prospectus_rows": [],
                "periodic_rows": [],
            }
    disclosure_ranges = None
    stock_target_coverage_confirmed = True
    if module_id == "investment.stock_holdings":
        left_coverage, right_coverage = _holdings_coverage(left_module), _holdings_coverage(right_module)
        stock_target_coverage_confirmed = _reference_covers_target_coverage(left_coverage, right_coverage)
        disclosure_ranges = {"prospectus": left_coverage, "periodic_report": right_coverage}
        left_rows, right_rows = _align_stock_markets(left_rows, right_rows)
        # The prospectus defines the requested comparison set. Compare every
        # target security that can be identified in the report, even when the
        # chapter contains multiple sub-tables and its aggregate scope is unknown.
        # Report-only securities are outside the requested comparison set.
        target_codes = {
            normalize_stock_code(row.get("stock_code"))
            for row in left_rows
            if normalize_stock_code(row.get("stock_code"))
        }
        if target_codes:
            right_rows = [
                row for row in right_rows
                if normalize_stock_code(row.get("stock_code")) in target_codes
            ]
    if module_id == "investment.asset_allocation":
        left_rows, right_rows = _align_asset_rows(left_rows, right_rows)
    schema_issue = any(row.get("schema_warnings") for row in (*left_rows, *right_rows))
    if schema_issue:
        return {
            "rule_id": module_id,
            "title": title,
            "status": "UNRESOLVED_STRUCTURE",
            "differences": [],
            "review_message": "部分行身份或引用无法确认，暂不将其列为指标差异。",
            "prospectus_rows": left_rows,
            "periodic_rows": right_rows,
        }
    left_index, left_unresolved = _row_index(module_id, left_rows)
    right_index, right_unresolved = _row_index(module_id, right_rows)
    unresolved_rows = left_unresolved | right_unresolved
    differences: list[dict[str, Any]] = []
    identity_unresolved = bool(unresolved_rows)
    unresolved = (
        left_module.get("status") != "EXTRACTED"
        or right_module.get("status") != "EXTRACTED"
        or identity_unresolved
        or not left_rows or not right_rows
    )
    all_keys = sorted(set(left_index) | set(right_index))
    for row_key in all_keys:
        left_row, right_row = left_index.get(row_key), right_index.get(row_key)
        label = str((left_row or right_row or {}).get("label") or row_key)
        if left_row is None or right_row is None:
            present_row = left_row or right_row
            if _row_has_only_empty_metrics(module_id, present_row):
                continue
            if module_id == "investment.stock_holdings" and left_row is not None and not stock_target_coverage_confirmed:
                unresolved = True
                continue
            if unresolved:
                continue
            left_value = {"raw_text": left_row.get("label") or row_key, "evidence": left_row.get("row_evidence")} if left_row else None
            right_value = {"raw_text": right_row.get("label") or row_key, "evidence": right_row.get("row_evidence")} if right_row else None
            differences.append(_field_difference(row_key, label, "row", left_value, right_value, "MISSING_ROW", "仅在一份文档中出现"))
            continue
        field_keys = MODULE_FIELDS[module_id]
        for field_key in sorted(field_keys):
            left_field = left_row.get("fields", {}).get(field_key)
            right_field = right_row.get("fields", {}).get(field_key)
            if left_field is None and right_field is None:
                unresolved = True
                differences.append(_field_difference(row_key, label, field_key, None, None, "UNRESOLVED", "两份文档均未识别到该指标"))
                continue
            if left_field is None or right_field is None:
                differences.append(_field_difference(row_key, label, field_key, left_field, right_field, "MISSING_FIELD", "一侧未识别到此字段"))
                continue
            if any(item.get("evidence", {}).get("status") != "LOCATED" for item in (left_field, right_field)):
                unresolved = True
                differences.append(_field_difference(row_key, label, field_key, left_field, right_field, "UNRESOLVED", "原文证据未能唯一定位到 PDF 单元格"))
                continue
            equal = _numeric_equal(left_field, right_field)
            if equal is None:
                unresolved = True
                differences.append(_field_difference(row_key, label, field_key, left_field, right_field, "UNRESOLVED", "数值或单位口径无法安全比较"))
            elif not equal:
                differences.append(_field_difference(row_key, label, field_key, left_field, right_field, "MISMATCH", "招募说明书与定期报告披露值不同"))
        if module_id == "investment.industry_stock_portfolio" and left_row.get("industry_code") and right_row.get("industry_code"):
            if normalize_text(left_row.get("industry_name")) != normalize_text(right_row.get("industry_name")):
                unresolved = True
                differences.append(_field_difference(
                    row_key, label, "industry_name",
                    {"raw_text": left_row.get("industry_name"), "evidence": left_row.get("row_evidence")},
                    {"raw_text": right_row.get("industry_name"), "evidence": right_row.get("row_evidence")},
                    "UNRESOLVED", "行业代码相同但行业名称冲突，需人工复核",
                ))
        if module_id == "investment.stock_holdings" and normalize_text(left_row.get("stock_name")) != normalize_text(right_row.get("stock_name")):
            differences.append(_field_difference(
                row_key, label, "stock_name",
                {"raw_text": left_row.get("stock_name"), "evidence": left_row.get("row_evidence")},
                {"raw_text": right_row.get("stock_name"), "evidence": right_row.get("row_evidence")},
                "MISMATCH", "证券代码相同但股票名称披露不同",
            ))
        if module_id == "investment.stock_holdings" and left_row.get("rank") is not None and right_row.get("rank") is not None and str(left_row["rank"]) != str(right_row["rank"]):
            differences.append(_field_difference(row_key, label, "rank", {"raw_text": str(left_row["rank"]), "evidence": left_row.get("rank_evidence")}, {"raw_text": str(right_row["rank"]), "evidence": right_row.get("rank_evidence")}, "RANK_ORDER_CHANGED", "持仓披露顺序变化；不作为证券身份或持仓数值错误"))
    if any(item["status"] in {"MISMATCH", "MISSING_ROW", "MISSING_FIELD", "MISSING_MODULE", "RANK_ORDER_CHANGED"} for item in differences):
        status = "MISMATCH"
    elif unresolved or any(item["status"] == "UNRESOLVED" for item in differences):
        status = "UNRESOLVED_STRUCTURE"
    else:
        status = "MATCH"
    review_message = None
    completeness_messages = []
    for document_label, module in (("招募说明书", left_module), ("定期报告", right_module)):
        if not isinstance(module, dict):
            continue
        completeness_messages.extend(
            f"{document_label}：{message}"
            for message in module.get("completeness_warnings", [])
        )
    if identity_unresolved:
        completeness_messages.append("部分行身份不足以唯一匹配，系统未将其判为单侧出现。")
    if module_id == "investment.stock_holdings" and not stock_target_coverage_confirmed:
        target_codes = {
            normalize_stock_code(row.get("stock_code"))
            for row in left_rows
            if normalize_stock_code(row.get("stock_code"))
        }
        report_codes = {
            normalize_stock_code(row.get("stock_code"))
            for row in right_rows
            if normalize_stock_code(row.get("stock_code"))
        }
        if target_codes - report_codes:
            completeness_messages.append("定期报告披露范围或对应证券证据不足，无法确认招募说明书中未匹配证券是否确实缺失。")
    if completeness_messages:
        review_message = " ".join(completeness_messages)
    return {
        "rule_id": module_id,
        "title": title,
        "status": status,
        "differences": differences,
        "review_message": review_message,
        "prospectus_rows": left_rows,
        "periodic_rows": right_rows,
        **({"disclosure_ranges": disclosure_ranges} if disclosure_ranges is not None else {}),
    }


def compare_documents(prospectus: dict[str, Any], periodic_report: dict[str, Any]) -> dict[str, Any]:
    left_info = prospectus.get("document_info", {})
    right_info = periodic_report.get("document_info", {})
    warnings: list[str] = []
    left_code, right_code = str(left_info.get("fund_code") or "").strip(), str(right_info.get("fund_code") or "").strip()
    fund_mismatch = bool(left_code and right_code and normalize_identifier(left_code) != normalize_identifier(right_code))
    left_period, right_period = left_info.get("period_end"), right_info.get("period_end")
    if left_period and right_period and str(left_period) != str(right_period):
        warnings.append(f"报告期末不同（招募说明书：{left_period}；定期报告：{right_period}），仍按规则逐项比较字段，期间口径请人工确认。")
    elif not left_period or not right_period:
        warnings.append("至少一份文档的报告期末未能从原文证实；仍按规则比较已定位字段，期间信息需人工复核。")
    left_disclosure, right_disclosure = left_info.get("disclosure_date"), right_info.get("disclosure_date")
    report_after = bool(left_disclosure and right_disclosure and str(right_disclosure) > str(left_disclosure))
    if report_after:
        warnings.append("所选定期报告披露日期晚于招募说明书披露日期；不能作为该招书披露时点的校验报告。")
    blocked_status = "FUND_MISMATCH" if fund_mismatch else "UNRESOLVED" if report_after else None
    modules = [_compare_module(module_id, prospectus, periodic_report, blocked_status) for module_id in MODULE_TITLES]
    differences = [item for module in modules for item in module["differences"]]
    real_differences = sum(item["status"] in {"MISMATCH", "MISSING_ROW", "MISSING_FIELD", "MISSING_MODULE", "RANK_ORDER_CHANGED"} for item in differences)
    if fund_mismatch:
        status = "FUND_MISMATCH"
        warnings.append(f"基金代码不一致（招募说明书：{left_code}；定期报告：{right_code}）。")
    elif report_after:
        status = "UNRESOLVED"
    elif real_differences:
        status = "MISMATCH"
    elif not left_period or not right_period:
        status = "UNRESOLVED"
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
        "warnings": warnings,
        "modules": modules,
    }
