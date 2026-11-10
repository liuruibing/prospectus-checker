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
        supplied = {"investment.asset_allocation": asset_rows,
                    "investment.industry_stock_portfolio": industry_rows,
                    "investment.stock_holdings": stock_rows}[rule['rule_id']]
        modules.append({"rule_id": rule["rule_id"], "status": "EXTRACTED", "rows": rows,
                        "scope": {"investment_scope": "all", "disclosure_scope": "all"},
                        "disclosures": [{"status": "NO_DATA", "period_end": period,
                                         "scope": {"market_scope": "all"}, "page": 1, "quote": "已核验的测试披露"}]
                        if supplied is None else []})
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


def test_split_decimal_digits_are_normalized_as_the_same_number():
    assert normalize_numeric("6,316,294,115.6\n0") == normalize_numeric("6,316,294,115.60")


def test_rmb_yuan_unit_aliases_compare_equal_but_different_scales_do_not():
    left_row = asset_row("4,722.00", "100.00")
    right_row = asset_row("4722", "100.00")
    right_row["fields"]["amount"]["unit"] = "人民币元"
    result = compare_documents(document(asset_rows=[left_row]), document(asset_rows=[right_row]))
    assert result["status"] == "MATCH"
    assert result["difference_count"] == 0

    right_row["fields"]["amount"]["unit"] = "万元"
    result = compare_documents(document(asset_rows=[left_row]), document(asset_rows=[right_row]))
    assert result["status"] == "UNRESOLVED"
    assert result["modules"][0]["differences"][0]["status"] == "UNRESOLVED"


def test_unique_source_backed_unmapped_asset_rows_match_by_label():
    def unmapped_asset_row(amount: str):
        row = asset_row(amount, "-")
        row.update(row_key="", parent_key=None, label="基金投资")
        return row

    result = compare_documents(
        document(asset_rows=[unmapped_asset_row("-")]),
        document(asset_rows=[unmapped_asset_row("-")]),
    )

    assert result["status"] == "MATCH"
    assert result["difference_count"] == 0
    assert result["modules"][0]["status"] == "MATCH"


def test_model_semantic_asset_key_aligns_equivalent_source_labels():
    left_row = asset_row("405,540,438.99", "0.11")
    left_row.update(row_key="other_assets", label="其他资产")
    right_row = asset_row("405,540,438.99", "0.11")
    right_row.update(row_key="other_assets", label="其他各项资产")

    result = compare_documents(
        document(asset_rows=[left_row]),
        document(asset_rows=[right_row]),
    )

    assert result["status"] == "MATCH"
    assert result["difference_count"] == 0


def test_asset_rows_with_different_model_keys_align_by_unique_source_label_and_parent():
    parent_left = asset_row("100", "100")
    parent_left.update(row_key="top_level_left", parent_key=None, label="权益投资")
    child_left = asset_row("100", "100")
    child_left.update(row_key="equity_detail_left", parent_key="top_level_left", label="其中：股票")
    parent_right = asset_row("100", "100")
    parent_right.update(row_key="top_level_right", parent_key=None, label="权益投资")
    child_right = asset_row("100", "100")
    child_right.update(row_key="equity_detail_right", parent_key="top_level_right", label="其中：股票")

    result = compare_documents(
        document(asset_rows=[parent_left, child_left]),
        document(asset_rows=[parent_right, child_right]),
    )

    assert result["status"] == "MATCH"
    assert result["difference_count"] == 0


def test_ambiguous_asset_source_labels_do_not_get_fallback_matched():
    def duplicate(key: str):
        row = asset_row("10", "1")
        row.update(row_key=key, parent_key=None, label="补充项目")
        return row

    result = compare_documents(
        document(asset_rows=[duplicate("left_a"), duplicate("left_b")]),
        document(asset_rows=[duplicate("right_a"), duplicate("right_b")]),
    )

    assert result["status"] == "UNRESOLVED"
    assert result["modules"][0]["status"] == "UNRESOLVED_STRUCTURE"


def test_duplicate_model_semantic_asset_keys_remain_unresolved():
    def duplicate(label: str):
        row = asset_row("10", "1")
        row.update(row_key="other_assets", label=label)
        return row

    result = compare_documents(
        document(asset_rows=[duplicate("其他资产"), duplicate("其他项目")]),
        document(asset_rows=[duplicate("其他资产"), duplicate("其他项目")]),
    )

    assert result["status"] == "UNRESOLVED"
    assert result["modules"][0]["status"] == "UNRESOLVED_STRUCTURE"


def test_duplicate_unmapped_asset_labels_remain_unresolved():
    def unmapped_asset_row():
        row = asset_row("10", "1")
        row.update(row_key="", parent_key=None, label="补充项目")
        return row

    result = compare_documents(
        document(asset_rows=[unmapped_asset_row(), unmapped_asset_row()]),
        document(asset_rows=[unmapped_asset_row(), unmapped_asset_row()]),
    )

    assert result["status"] == "UNRESOLVED"
    assert result["modules"][0]["status"] == "UNRESOLVED_STRUCTURE"


def test_matching_verified_empty_disclosures_compare_as_match():
    left, right = document(), document()
    disclosure = {
        "status": "NO_DATA", "period_end": "2025年12月31日",
        "scope": {"market": "domestic", "investment": "equity"},
        "page": 48, "quote": "本基金本报告期末未持有股票。",
    }
    for item in (left, right):
        module = next(m for m in item["modules"] if m["rule_id"] == "investment.stock_holdings")
        module["disclosures"] = [disclosure]
    result = compare_documents(left, right)
    module = next(m for m in result["modules"] if m["rule_id"] == "investment.stock_holdings")
    assert module["status"] == "MATCH"


def test_empty_disclosures_with_different_scopes_do_not_match():
    left, right = document(), document()
    for item, market in ((left, "domestic"), (right, "hk_connect")):
        module = next(m for m in item["modules"] if m["rule_id"] == "investment.stock_holdings")
        module["disclosures"] = [{
            "status": "NO_DATA", "period_end": "2025-12-31",
            "scope": {"market": market}, "page": 1, "quote": "原文依据",
        }]
    result = compare_documents(left, right)
    module = next(m for m in result["modules"] if m["rule_id"] == "investment.stock_holdings")
    assert module["status"] == "UNRESOLVED_STRUCTURE"


def test_empty_disclosures_missing_period_or_scope_never_fall_through_to_match():
    for claim in ({'status': 'NO_DATA', 'scope': {'market': 'unknown'}},
                  {'status': 'NO_DATA', 'period_end': '2025-12-31', 'scope': {}}, {}):
        left, right = document(), document()
        for doc in (left, right):
            doc['document_info']['period_end'] = None
            m = next(m for m in doc['modules'] if m['rule_id'] == 'investment.stock_holdings')
            m['disclosures'] = [claim] if claim else []
        m = compare_documents(left, right)['modules'][2]
        assert m['status'] == 'UNRESOLVED_STRUCTURE'
        assert m['review_message']


def test_explicit_no_data_disclosure_conflicts_with_rows_on_other_side():
    left, right = document(), document(stock_rows=[{
        "stock_code": "600519", "stock_name": "贵州茅台", "rank": 1,
        "market_scope": "domestic", "investment_scope": "active", "label": "贵州茅台",
        "fields": {"quantity": value("100", "股"), "fair_value": value("10000"),
                   "ratio_of_net_assets": value("1", "%")},
    }])
    module = next(m for m in left["modules"] if m["rule_id"] == "investment.stock_holdings")
    module["disclosures"] = [{"status": "NO_DATA", "period_end": "2025-12-31",
                              "scope": {"portfolio": "equity holdings"}, "page": 1, "quote": "无持仓"}]
    result = compare_documents(left, right)
    module = next(m for m in result["modules"] if m["rule_id"] == "investment.stock_holdings")
    assert module["status"] == "MISMATCH"


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
    common = {**asset_row(), "row_key": "cash", "label": "现金"}
    result = compare_documents(
        document(asset_rows=[asset_row(), common]),
        document(asset_rows=[common]),
    )

    difference = result["modules"][0]["differences"][0]
    assert difference["status"] == "MISSING_ROW"
    assert difference["prospectus"]["raw_text"] == "权益投资"
    assert difference["periodic_report"] is None


def test_prospectus_top_n_is_compared_against_superset_report_without_report_only_rows():
    row = {'label': '样本', 'stock_name': '样本', 'stock_code': '600001',
           'market_scope': 'domestic', 'investment_scope': 'all',
           'fields': {'fair_value': value('10'), 'quantity': value('2', '股'), 'ratio_of_net_assets': value('1', '%')}}
    left, right = document(stock_rows=[row]), document(stock_rows=[row, {**row, 'stock_code': '600002'}])
    left['modules'][2]['scope'].update(disclosure_scope='top_n', limit=5)
    compared = compare_documents(left, right)['modules'][2]
    assert compared['status'] == 'MATCH'
    assert compared['differences'] == []
    assert compared['disclosure_ranges'] == {
        'prospectus': ('top_n', 5), 'periodic_report': ('all', None),
    }


def test_unknown_aggregate_scope_still_compares_target_rows_and_ignores_report_extras():
    def stock(code: str, *, investment_scope: str, amount: str = '10'):
        return {
            'label': f'证券{code}', 'stock_name': f'证券{code}', 'stock_code': code,
            'market_scope': 'domestic', 'investment_scope': investment_scope,
            'fields': {
                'fair_value': value(amount), 'quantity': value('2', '股'),
                'ratio_of_net_assets': value('1', '%'),
            },
        }

    target_rows = [stock('600001', investment_scope='index'), stock('600002', investment_scope='active')]
    left, right = document(stock_rows=target_rows), document(stock_rows=[
        *target_rows, stock('600003', investment_scope='index'),
    ])
    left['modules'][2]['scope'].update(disclosure_scope='unknown', limit=None)
    right['modules'][2]['scope'].update(disclosure_scope='all', limit=None)

    compared = compare_documents(left, right)['modules'][2]

    assert compared['status'] == 'MATCH'
    assert compared['differences'] == []
    assert {row['stock_code'] for row in compared['periodic_rows']} == {'600001', '600002'}


def test_unknown_target_scope_does_not_call_unmatched_target_security_missing():
    target = {
        'label': '目标证券', 'stock_name': '目标证券', 'stock_code': '600001',
        'market_scope': 'domestic', 'investment_scope': 'index',
        'fields': {
            'fair_value': value('10'), 'quantity': value('2', '股'),
            'ratio_of_net_assets': value('1', '%'),
        },
    }
    left, right = document(stock_rows=[target]), document(stock_rows=[])
    left['modules'][2]['scope'].update(disclosure_scope='unknown', limit=None)
    right['modules'][2]['scope'].update(disclosure_scope='all', limit=None)

    compared = compare_documents(left, right)['modules'][2]

    assert compared['status'] == 'UNRESOLVED_STRUCTURE'
    assert compared['differences'] == []
    assert '范围或对应证券证据不足' in compared['review_message']


def test_prospectus_target_rows_are_compared_even_when_a_report_has_extra_rows():
    target = {'label': '目标证券', 'stock_name': '目标证券', 'stock_code': '600001',
              'market_scope': 'domestic', 'investment_scope': 'all',
              'fields': {'fair_value': value('10'), 'quantity': value('2', '股'), 'ratio_of_net_assets': value('1', '%')}}
    extra = {'label': '报告额外证券', 'stock_name': '报告额外证券', 'stock_code': '600002',
             'market_scope': 'domestic', 'investment_scope': 'all',
             'fields': {'fair_value': value('20'), 'quantity': value('3', '股'), 'ratio_of_net_assets': value('2', '%')}}
    left, right = document(stock_rows=[target]), document(stock_rows=[{**target, 'fields': {
        'fair_value': value('11'), 'quantity': value('2', '股'), 'ratio_of_net_assets': value('1', '%')}} , extra])
    left['modules'][2]['scope'].update(disclosure_scope='top_n', limit=1)
    compared = compare_documents(left, right)['modules'][2]
    assert compared['status'] == 'MISMATCH'
    assert {item['field_key'] for item in compared['differences']} == {'fair_value'}
    assert all(item['row_label'] != '报告额外证券' for item in compared['differences'])


def test_report_top_n_smaller_than_prospectus_target_remains_unresolved():
    rows = [
        {'label': f'证券{i}', 'stock_name': f'证券{i}', 'stock_code': f'60000{i}',
         'market_scope': 'domestic', 'investment_scope': 'all',
         'fields': {'fair_value': value('10'), 'quantity': value('2', '股'), 'ratio_of_net_assets': value('1', '%')}}
        for i in range(1, 6)
    ]
    left, right = document(stock_rows=rows), document(stock_rows=rows[:3])
    left['modules'][2]['scope'].update(disclosure_scope='top_n', limit=5)
    right['modules'][2]['scope'].update(disclosure_scope='top_n', limit=3)
    compared = compare_documents(left, right)['modules'][2]
    assert compared['status'] == 'UNRESOLVED_STRUCTURE'
    assert compared['differences'] == []
    assert '披露范围' in compared['review_message']


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
        "investment_scope": "index",
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


def test_industry_identity_includes_investment_scope_and_normalizes_scope_aliases():
    def industry(scope, amount):
        return {
            "label": "制造业", "industry_code": "C", "industry_name": "制造业",
            "market_scope": "domestic", "investment_scope": scope,
            "fields": {"fair_value": value(amount), "ratio_of_net_assets": value("1", "%")},
        }

    result = compare_documents(
        document(industry_rows=[industry("index", "100"), industry("active", "20")]),
        document(industry_rows=[industry("指数投资", "100"), industry("积极投资", "20")]),
    )
    assert result["status"] == "MATCH"
    assert result["difference_count"] == 0
    assert result["modules"][1]["status"] == "MATCH"


def test_stock_identity_keeps_index_and_active_scope_separate():
    def stock(scope, amount):
        return {
            "label": "样例公司", "stock_code": "600001", "stock_name": "样例公司",
            "market_scope": "domestic", "investment_scope": scope,
            "fields": {"quantity": value("10", "股"), "fair_value": value(amount),
                       "ratio_of_net_assets": value("1", "%")},
        }

    result = compare_documents(
        document(stock_rows=[stock("index", "100"), stock("active", "20")]),
        document(stock_rows=[stock("指数投资", "100"), stock("积极投资", "20")]),
    )
    assert result["status"] == "MATCH"
    assert result["difference_count"] == 0


def test_one_sided_placeholder_row_does_not_count_as_difference():
    placeholder = {
        "label": "农、林、牧、渔业",
        "industry_code": "A",
        "industry_name": "农、林、牧、渔业",
        "market_scope": "domestic",
        "investment_scope": "index",
        "fields": {
            "fair_value": value("-"),
            "ratio_of_net_assets": value("-", "%"),
        },
    }
    result = compare_documents(
        document(industry_rows=[placeholder]),
        document(industry_rows=[]),
    )

    assert result["status"] == "UNRESOLVED"
    assert result["difference_count"] == 0
    assert result["modules"][1]["differences"] == []
