from app.comparison import compare_documents
from app.domain import MODULE_RULES, normalize_numeric


def value(raw: str, unit: str = "元") -> dict:
    normalized = normalize_numeric(raw)
    return {
        "raw_text": raw,
        "normalized_value": normalized["value"],
        "value_kind": normalized["kind"],
        "unit": unit,
        "evidence": {"status": "LOCATED"},
    }


def document(*, period: str = "2025-12-31", disclosure: str = "2026-01-30", asset_rows=None, industry_rows=None, stock_rows=None) -> dict:
    modules = []
    for rule in MODULE_RULES:
        rows = []
        if rule["rule_id"] == "investment.asset_allocation":
            rows = asset_rows or []
        elif rule["rule_id"] == "investment.stock_holdings":
            rows = stock_rows or []
        elif rule["rule_id"] == "investment.industry_stock_portfolio":
            rows = industry_rows or []
        modules.append({"rule_id": rule["rule_id"], "status": "EXTRACTED", "rows": rows})
    return {
        "filename": "sample.pdf",
        "page_count": 10,
        "document_info": {
            "fund_code": "050001",
            "period_end": period,
            "disclosure_date": disclosure,
        },
        "modules": modules,
    }


def asset_row(amount: str = "4,722.00", ratio: str = "100.00") -> dict:
    return {
        "row_key": "equity_investment",
        "label": "权益投资",
        "fields": {
            "amount": value(amount),
            "ratio_of_total_assets": value(ratio, "%"),
        },
    }


def test_numeric_formatting_differences_compare_equal():
    result = compare_documents(
        document(asset_rows=[asset_row("4,722.00", "100.0")]),
        document(asset_rows=[asset_row("4722", "100.00")]),
    )

    assert result["status"] == "MATCH"
    assert result["difference_count"] == 0


def test_dash_and_zero_are_not_treated_as_equal():
    result = compare_documents(
        document(asset_rows=[asset_row("-", "100.00")]),
        document(asset_rows=[asset_row("0", "100.00")]),
    )

    assert result["status"] == "MISMATCH"
    difference = result["modules"][0]["differences"][0]
    assert difference["field_key"] == "amount"
    assert difference["status"] == "MISMATCH"


def test_different_report_periods_still_compare_disclosed_fields():
    result = compare_documents(
        document(period="2025-12-31", asset_rows=[asset_row("1")]),
        document(period="2026-09-30", asset_rows=[asset_row("2")]),
    )

    assert result["status"] == "MISMATCH"
    assert result["difference_count"] == 1
    assert result["modules"][0]["status"] == "MISMATCH"
    assert "仍按规则逐项比较字段" in result["warnings"][0]


def test_report_disclosed_after_prospectus_is_not_used():
    result = compare_documents(
        document(disclosure="2026-01-30", asset_rows=[asset_row("1")]),
        document(disclosure="2026-02-01", asset_rows=[asset_row("2")]),
    )

    assert result["status"] == "UNRESOLVED"
    assert all(module["status"] == "UNRESOLVED" for module in result["modules"])
    assert all(not module["differences"] for module in result["modules"])
    assert any("晚于招募说明书披露日期" in warning for warning in result["warnings"])

def test_missing_row_shows_the_side_where_it_was_found():
    result = compare_documents(
        document(asset_rows=[asset_row()]),
        document(asset_rows=[]),
    )

    difference = result["modules"][0]["differences"][0]
    assert difference["status"] == "MISSING_ROW"
    assert difference["prospectus"]["raw_text"] == "权益投资"
    assert difference["periodic_report"] is None


def test_unique_stock_matches_when_only_one_market_is_unknown():
    row = {"label": "示例股票", "stock_code": "600001", "stock_name": "示例股票", "market_scope": "domestic",
           "fields": {"fair_value": value("10"), "quantity": value("2", "股"), "ratio_of_net_assets": value("1", "%")}}
    result = compare_documents(document(stock_rows=[row]), document(stock_rows=[{**row, "market_scope": "unknown"}]))
    assert result["status"] == "MATCH"
    assert result["difference_count"] == 0
    conflict = compare_documents(document(stock_rows=[row]), document(stock_rows=[{**row, "market_scope": "hk_connect"}]))
    assert conflict["status"] == "UNRESOLVED"
    assert conflict["difference_count"] == 0


def test_unidentified_industry_rows_cannot_silently_pass_or_become_single_sided():
    row = {"label": "采矿业", "industry_code": "B", "market_scope": "unknown",
           "fields": {"fair_value": value("10"), "ratio_of_net_assets": value("1", "%")}}
    result = compare_documents(document(industry_rows=[row]), document(industry_rows=[row]))
    assert result["status"] == "UNRESOLVED"
    assert result["difference_count"] == 0
    complete = document(asset_rows=[asset_row()])
    incomplete = document()
    incomplete["modules"][0]["status"] = "UNRESOLVED_STRUCTURE"
    result = compare_documents(complete, incomplete)
    assert result["status"] == "UNRESOLVED"
    assert not result["modules"][0]["differences"]


def test_changed_stock_rank_is_reported_as_a_module_difference():
    prospectus_stock = {
        "stock_code": "600519",
        "stock_name": "贵州茅台",
        "market_scope": "domestic",
        "rank": 1,
        "rank_evidence": {"status": "LOCATED"},
        "fields": {"fair_value": value("10"), "ratio_of_net_assets": value("1", "%"), "quantity": value("2", "股")},
    }
    report_stock = {**prospectus_stock, "rank": 2}
    result = compare_documents(
        document(stock_rows=[prospectus_stock]),
        document(stock_rows=[report_stock]),
    )

    stock_module = result["modules"][2]
    assert result["status"] == "MISMATCH"
    assert stock_module["status"] == "MISMATCH"
    assert any(item["status"] == "RANK_ORDER_CHANGED" for item in stock_module["differences"])



def test_ordinal_fields_not_in_metric_rules_are_not_compared():
    prospectus_row = asset_row()
    prospectus_row["fields"]["display_order"] = value("1", "序号")
    result = compare_documents(
        document(asset_rows=[prospectus_row]),
        document(asset_rows=[asset_row()]),
    )

    assert result["status"] == "MATCH"
    assert result["difference_count"] == 0



def test_optional_stock_rank_field_is_not_required_metric_data():
    stock = {
        "stock_code": "600519",
        "stock_name": "贵州茅台",
        "market_scope": "domestic",
        "fields": {
            "quantity": value("2", "股"),
            "fair_value": value("10"),
            "ratio_of_net_assets": value("1", "%"),
        },
    }
    prospectus_stock = {**stock, "fields": {**stock["fields"], "rank": value("1", "序号")}}
    result = compare_documents(
        document(stock_rows=[prospectus_stock]),
        document(stock_rows=[stock]),
    )

    assert result["status"] == "MATCH"
    assert result["difference_count"] == 0

def test_industry_total_rows_without_code_are_matched_as_summary_rows():
    total_row = {
        "label": "合计",
        "industry_name": "合计",
        "market_scope": "domestic",
        "fields": {
            "fair_value": value("4,722,395,708.64"),
            "ratio_of_net_assets": value("99.21", "%"),
        },
    }
    result = compare_documents(
        document(industry_rows=[total_row]),
        document(industry_rows=[total_row]),
    )

    industry_module = result["modules"][1]
    assert result["status"] == "MATCH"
    assert industry_module["status"] == "MATCH"
    assert industry_module["differences"] == []


def test_one_sided_placeholder_row_does_not_count_as_difference():
    placeholder = {
        "label": "农、林、牧、渔业",
        "industry_code": "A",
        "industry_name": "农、林、牧、渔业",
        "market_scope": "domestic",
        "fields": {
            "fair_value": value("-"),
            "ratio_of_net_assets": value("-", "%"),
        },
    }
    result = compare_documents(
        document(industry_rows=[placeholder]),
        document(industry_rows=[]),
    )

    assert result["status"] == "MATCH"
    assert result["difference_count"] == 0
    assert result["modules"][1]["differences"] == []
