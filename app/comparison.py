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

def _row_has_only_empty_metrics(module_id: str, row: dict[str, Any]) -> bool:
    fields = row.get("fields", {})
    return all(
        isinstance(fields.get(key), dict)
        and fields[key].get("value_kind") in {"DASH", "EMPTY"}
        and fields[key].get("evidence", {}).get("status") == "LOCATED"
        for key in MODULE_FIELDS[module_id]
    )



def _numeric_equal(left: dict[str, Any], right: dict[str, Any]) -> bool | None:
    left_kind, right_kind = left.get("value_kind"), right.get("value_kind")
    if left_kind == "DASH" or right_kind == "DASH":
        return left_kind == right_kind
    if left_kind != "NUMBER" or right_kind != "NUMBER":
        return None
    if left.get("unit") and right.get("unit") and left["unit"] != right["unit"]:
        return None
    try:
        return Decimal(str(left["normalized_value"])) == Decimal(str(right["normalized_value"]))
    except (InvalidOperation, TypeError, KeyError):
        return None


def _row_key(module_id: str, row: dict[str, Any], counts: dict[str, int]) -> tuple[str | None, bool]:
    if module_id == "investment.asset_allocation":
        key = str(row.get("row_key") or "").strip()
        if key:
            return f"{row.get('parent_key') or ''}/{key}", True
        label = normalize_text(row.get("label"))
        return (f"unmapped:{row.get('parent_key') or ''}/{label}", False) if label else (None, False)
    if module_id == "investment.industry_stock_portfolio":
        scope = str(row.get("market_scope") or "unknown")
        if scope not in {"domestic", "hk_connect", "other"}:
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
    scope = str(row.get("market_scope") or "unknown")
    code = normalize_stock_code(row.get("stock_code"))
    if not code or counts.get(f"{scope}:{code}", 0) > 1 or (scope == "unknown" and counts.get(f"all:{code}", 0) > 1):
        return None, False
    return f"{scope}:{code}", True


def _row_index(module_id: str, rows: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], set[str]]:
    counts: dict[str, int] = {}
    for row in rows:
        if module_id == "investment.industry_stock_portfolio":
            scope = str(row.get("market_scope") or "unknown")
            identity = normalize_identifier(row.get("industry_code")) or normalize_text(row.get("industry_name") or row.get("label"))
        elif module_id == "investment.stock_holdings":
            scope = str(row.get("market_scope") or "unknown")
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
    for row in rows:
        key, confident = _row_key(module_id, row, counts)
        if not key or key in result or not confident:
            if key:
                unresolved.add(key)
            continue
        result[key] = row
    return result, unresolved


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
    left_index, left_unresolved = _row_index(module_id, left_rows)
    right_index, right_unresolved = _row_index(module_id, right_rows)
    unresolved_rows = left_unresolved | right_unresolved
    differences: list[dict[str, Any]] = []
    unresolved = left_module.get("status") != "EXTRACTED" or right_module.get("status") != "EXTRACTED"
    for row_key in sorted(unresolved_rows):
        unresolved = True
        differences.append(_field_difference(row_key, row_key, "row_identity", None, None, "UNRESOLVED", "行身份重复或不足以唯一匹配，需要人工复核"))
    all_keys = sorted(set(left_index) | set(right_index))
    for row_key in all_keys:
        left_row, right_row = left_index.get(row_key), right_index.get(row_key)
        label = str((left_row or right_row or {}).get("label") or row_key)
        if left_row is None or right_row is None:
            present_row = left_row or right_row
            if _row_has_only_empty_metrics(module_id, present_row):
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
    return {"rule_id": module_id, "title": title, "status": status, "differences": differences,
            "prospectus_rows": left_rows, "periodic_rows": right_rows}


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
