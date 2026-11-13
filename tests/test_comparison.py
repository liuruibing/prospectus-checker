from app.comparison import _generic_compare_module, compare_declared_field, compare_documents, evaluate_declared_context
from app.domain import normalize_numeric
from app.rule_compiler import load_investment_rule_definitions

_, _, MODULE_RULES = load_investment_rule_definitions()
_ASSET_RULE = next(rule for rule in MODULE_RULES if rule["rule_id"] == "investment.asset_allocation")
_ASSET_RATIO_FIELD = next(field for field, label in _ASSET_RULE["fields"].items() if "比例" in label)


def value(raw: str, unit: str = "元") -> dict:
    normalized = normalize_numeric(raw)
    return {
        "raw_text": raw,
        "normalized_value": normalized["value"],
        "value_kind": normalized["kind"],
        "unit": unit,
        "evidence": {"status": "LOCATED"},
    }


def test_declared_comparison_operations_are_shared_and_rule_data_driven():
    assert compare_declared_field(
        {"operation": "decimal"}, value("1,000.00"), value("1000"),
    ) == "MATCH"
    assert compare_declared_field(
        {"operation": "identifier"}, {"raw_text": "A-001"}, {"raw_text": "A001"},
    ) == "MATCH"
    assert compare_declared_field(
        {"operation": "identifier"}, {"raw_text": "基金代码：510300"}, {"raw_text": "基金主代码 510300"},
    ) == "MATCH"
    assert compare_declared_field(
        {"operation": "identifier"}, {"raw_text": "代码 510300 / 510500"}, {"raw_text": "510300"},
    ) == "UNRESOLVED"
    assert compare_declared_field(
        {"operation": "identifier"},
        {"raw_text": "基金代码：000001；交易代码：000001（前端）、000002（后端）；下属基金代码：000001"},
        {"raw_text": "基金主代码：000001；交易代码：000001（前端）、000002（后端）"},
        semantic_status="MATCH",
    ) == "MATCH"
    assert compare_declared_field(
        {"operation": "identifier"},
        {"raw_text": "基金代码：000001；交易代码：000002（后端）"},
        {"raw_text": "基金主代码：000001；交易代码：000003（后端）"},
        semantic_status="MATCH",
    ) == "UNRESOLVED"
    assert compare_declared_field(
        {"operation": "semantic"}, {"raw_text": "文本甲"}, {"raw_text": "文本乙"},
        semantic_status="MATCH",
    ) == "MATCH"
    assert compare_declared_field(
        {"operation": "semantic"}, {"raw_text": "文本甲"}, {"raw_text": "文本乙"},
    ) == "UNRESOLVED"


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
        if rule["rule_id"] == "investment.stock_holdings":
            rows = []
            for original in stock_rows or []:
                row = {**original, "fields": dict(original.get("fields") or {})}
                if not row.get("security_code"):
                    row["security_code"] = row.get("stock_code")
                if not row.get("security_name"):
                    row["security_name"] = row.get("stock_name") or row.get("label")
                row["fields"].setdefault("security_name", value(str(row.get("security_name") or ""), ""))
                if "holding_quantity" not in row["fields"] and "quantity" in row["fields"]:
                    row["fields"]["holding_quantity"] = row["fields"]["quantity"]
                if "nav_ratio" not in row["fields"] and "ratio_of_net_assets" in row["fields"]:
                    row["fields"]["nav_ratio"] = row["fields"]["ratio_of_net_assets"]
                row.setdefault("investment_scope", "all")
                rows.append(row)
        elif rule["rule_id"] == "investment.industry_stock_portfolio":
            rows = []
            for original in industry_rows or []:
                row = {**original, "fields": dict(original.get("fields") or {})}
                if "nav_ratio" not in row["fields"] and "ratio_of_net_assets" in row["fields"]:
                    row["fields"]["nav_ratio"] = row["fields"]["ratio_of_net_assets"]
                rows.append(row)
        supplied = {"investment.asset_allocation": asset_rows,
                    "investment.industry_stock_portfolio": industry_rows,
                    "investment.stock_holdings": stock_rows}[rule['rule_id']]
        modules.append({"rule_id": rule["rule_id"], "status": "EXTRACTED", "rows": rows,
                        "scope": {"investment_scope": "all", "disclosure_scope": "all",
                                  "fund_identity": "示例基金"},
                        "disclosures": [{"status": "NO_DATA", "period_end": period,
                                         "scope": {"market_scope": "all"}, "page": 1, "quote": "已核验的测试披露"}]
                        if supplied is None else []})
    return {
        "filename": "sample.pdf",
        "page_count": 10,
        "document_info": {
            "fund_name": "示例基金",
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
            # Field roles are supplied by the active Markdown rule plan.
            _ASSET_RATIO_FIELD: value(ratio, "%"),
        },
    }


def test_numeric_formatting_differences_compare_equal():
    result = compare_documents(
        document(asset_rows=[asset_row("4,722.00", "100.0")]),
        document(asset_rows=[asset_row("4722", "100.00")]),
    )

    assert result["status"] == "MATCH"
    assert result["difference_count"] == 0


def test_unresolved_extraction_is_not_reported_as_a_missing_module_difference():
    plan = {
        "rule_identity": "investment.synthetic",
        "display_title": "示例章节",
        "comparison": {},
    }
    result = _generic_compare_module(
        plan,
        {"modules": [{"rule_id": "investment.synthetic", "status": "UNRESOLVED_STRUCTURE", "rows": []}]},
        {"modules": [{"rule_id": "investment.synthetic", "status": "MISSING_MODULE", "rows": []}]},
        None,
    )
    assert result["status"] == "UNRESOLVED_STRUCTURE"
    assert result["differences"] == []

def _synthetic_rule_plan():
    return {
        "protocol_version": 2,
        "rule_identity": "synthetic_distribution_metric",
        "rule_scope": "investment",
        "display_title": "分红信息",
        "target_role": "prospectus",
        "reference_roles": ["periodic_report"],
        "search_intent": {
            "target_description": "核对基金分红信息",
            "preferred_sections": ["收益分配"],
            "include_continuations": True,
        },
        "row_matching": {
            "identity_description": "按基金和份额类别匹配",
            "identity_fields": ["instrument_id"],
            "scope_fields": ["share_class"],
            "hierarchy_fields": [],
            "fallback_identity_fields": [],
            "target_population": "target_document",
            "reference_superset_allowed": True,
            "require_complete_source_for_missing": True,
        },
        "comparison": {
            "identity_description": "比较目标文档列出的分红金额",
            "scope_conditions": ["基金身份一致"],
            "context_checks": [],
            "fields": [{"field": "distribution_amount", "label": "分红金额", "operation": "decimal", "required": True}],
        },
        "evidence_requirements": ["原文引用和定位"],
        "review_conditions": ["范围或证据不完整时待复核"],
    }


def test_new_rule_uses_same_generic_comparator_and_ignores_reference_extras():
    left, right = document(), document()
    target_row = {
        "instrument_id": "fund-x",
        "share_class": "A",
        "label": "A 类分红",
        "row_evidence": {"status": "LOCATED"},
        "fields": {"distribution_amount": value("0.25")},
    }
    reference_row = {
        **target_row,
        "fields": {"distribution_amount": value("0.2500")},
    }
    reference_extra = {
        "instrument_id": "fund-y",
        "share_class": "A",
        "label": "其他基金",
        "fields": {"distribution_amount": value("9.00")},
    }
    left["modules"].append({"rule_id": "synthetic_distribution_metric", "status": "EXTRACTED", "rows": [target_row], "scope": {}})
    right["modules"].append({"rule_id": "synthetic_distribution_metric", "status": "EXTRACTED", "rows": [reference_row, reference_extra], "scope": {"disclosure_scope": "all"}})

    result = compare_documents(left, right, {"synthetic_distribution_metric": _synthetic_rule_plan()})

    module = next(item for item in result["modules"] if item["rule_id"] == "synthetic_distribution_metric")
    assert module["status"] == "MATCH"
    assert module["differences"] == []


def test_rule_declared_row_name_equivalence_matches_different_local_keys():
    left, right = document(), document()
    left_row = {
        "row_key": "local-key-a", "label": "canonical label",
        "fields": {"distribution_amount": value("100")},
    }
    right_row = {
        "row_key": "local-key-b", "label": "explicit alternate label",
        "fields": {"distribution_amount": value("100.00")},
    }
    left["modules"].append({"rule_id": "synthetic_distribution_metric", "status": "EXTRACTED",
                            "rows": [left_row], "scope": {"disclosure_scope": "all"}})
    right["modules"].append({"rule_id": "synthetic_distribution_metric", "status": "EXTRACTED",
                             "rows": [right_row], "scope": {"disclosure_scope": "all"}})
    plan = _synthetic_rule_plan()
    plan["row_matching"]["identity_fields"] = ["row_key"]
    plan["row_matching"]["identity_equivalence_groups"] = [{
        "canonical_identity": "canonical label",
        "equivalent_labels": ["explicit alternate label"],
    }]

    result = compare_documents(left, right, {"synthetic_distribution_metric": plan})

    module = next(item for item in result["modules"] if item["rule_id"] == "synthetic_distribution_metric")
    assert module["status"] == "MATCH"
    assert module["differences"] == []


def test_rule_declared_row_name_equivalence_does_not_bridge_conflicting_scope():
    left, right = document(), document()
    left_row = {"row_key": "left", "label": "canonical label", "market_scope": "domestic",
                "fields": {"distribution_amount": value("100")}}
    right_row = {"row_key": "right", "label": "explicit alternate label", "market_scope": "overseas",
                 "fields": {"distribution_amount": value("100")}}
    left["modules"].append({"rule_id": "synthetic_distribution_metric", "status": "EXTRACTED",
                            "rows": [left_row], "scope": {"disclosure_scope": "all"}})
    right["modules"].append({"rule_id": "synthetic_distribution_metric", "status": "EXTRACTED",
                             "rows": [right_row], "scope": {"disclosure_scope": "all"}})
    plan = _synthetic_rule_plan()
    plan["row_matching"].update(identity_fields=["row_key"], scope_fields=["market_scope"])
    plan["row_matching"]["identity_equivalence_groups"] = [{
        "canonical_identity": "canonical label",
        "equivalent_labels": ["explicit alternate label"],
    }]

    result = compare_documents(left, right, {"synthetic_distribution_metric": plan})

    module = next(item for item in result["modules"] if item["rule_id"] == "synthetic_distribution_metric")
    assert module["status"] == "UNRESOLVED_STRUCTURE"


def test_generic_plan_detects_field_mismatch_without_chapter_specific_code():
    left, right = document(), document()
    left_row = {
        "instrument_id": "fund-x", "share_class": "A", "label": "A 类分红",
        "fields": {"distribution_amount": value("0.25")},
    }
    right_row = {
        **left_row, "fields": {"distribution_amount": value("0.30")},
    }
    left["modules"].append({"rule_id": "synthetic_distribution_metric", "status": "EXTRACTED", "rows": [left_row], "scope": {}})
    right["modules"].append({"rule_id": "synthetic_distribution_metric", "status": "EXTRACTED", "rows": [right_row], "scope": {"disclosure_scope": "all"}})

    result = compare_documents(left, right, {"synthetic_distribution_metric": _synthetic_rule_plan()})

    module = next(item for item in result["modules"] if item["rule_id"] == "synthetic_distribution_metric")
    assert module["status"] == "MISMATCH"
    assert module["differences"][0]["field_key"] == "distribution_amount"


def test_generic_context_checks_use_rule_declared_paths_without_business_field_aliases():
    left, right = document(), document()
    left["document_info"]["product_identity"] = "fund-x"
    right["document_info"]["product_identity"] = "fund-x"
    left_module = {"rule_id": "synthetic_distribution_metric", "status": "EXTRACTED", "rows": [{
        "instrument_id": "fund-x", "label": "A 类分红",
        "fields": {"distribution_amount": value("0.25")},
    }], "scope": {"disclosure_scope": "all"}}
    right_module = {**left_module, "rows": [{**left_module["rows"][0], "fields": {
        "distribution_amount": value("0.25")}}]}
    left["modules"].append(left_module)
    right["modules"].append(right_module)
    plan = _synthetic_rule_plan()
    plan["comparison"]["context_checks"] = [{
        "label": "产品身份",
        "target_path": "document.document_info.product_identity",
        "reference_path": "module.scope.product_identity",
        "operation": "identifier",
        "required": True,
    }]
    right_module["scope"]["product_identity"] = "different-fund"

    result = compare_documents(left, right, {"synthetic_distribution_metric": plan})

    module = next(item for item in result["modules"] if item["rule_id"] == "synthetic_distribution_metric")
    assert module["status"] == "UNRESOLVED_STRUCTURE"
    assert "已停止逐行差异判定" in module["review_message"]
    assert module["differences"] == []


def test_context_checks_can_be_declared_for_a_specific_reference_role():
    plan = _synthetic_rule_plan()
    plan["reference_roles"] = ["periodic_report", "prospectus"]
    plan["comparison"]["context_checks_by_reference_role"] = {
        "prospectus": [{
            "label": "版本日期不晚于概要编制日",
            "target_path": "document.metadata.compiled_date",
            "reference_path": "document.metadata.filename_date_quote",
            "operation": "reference_before_or_equal",
            "required": True,
        }]
    }
    target = {"metadata": {"compiled_date": "2025-11-24"}}
    reference = {"metadata": {"filename_date_quote": "文件名日期前缀：2025-11-24"}}

    matched, assessments = evaluate_declared_context(
        plan, target, reference, reference_role="prospectus",
    )
    assert matched is True
    assert assessments == [{"label": "版本日期不晚于概要编制日", "status": "MATCH"}]

    later = {"metadata": {"filename_date_quote": "文件名日期前缀：2025-11-25"}}
    rejected, assessments = evaluate_declared_context(
        plan, target, later, reference_role="prospectus",
    )
    assert rejected is False
    assert assessments[0]["status"] == "UNRESOLVED"


def test_generic_period_context_mismatch_stops_row_difference_judgment():
    left, right = document(), document()
    left["document_info"]["period_end"] = "2025-06-30"
    right["document_info"]["period_end"] = "2025-12-31"
    left_row = {
        "instrument_id": "fund-x", "label": "持仓金额",
        "row_evidence": {"status": "LOCATED"},
        "fields": {"distribution_amount": value("100.00")},
    }
    right_row = {
        **left_row, "fields": {"distribution_amount": value("200.00")},
    }
    left["modules"].append({"rule_id": "synthetic_distribution_metric", "status": "EXTRACTED",
                            "rows": [left_row], "scope": {"disclosure_scope": "all"}})
    right["modules"].append({"rule_id": "synthetic_distribution_metric", "status": "EXTRACTED",
                             "rows": [right_row], "scope": {"disclosure_scope": "all"}})
    plan = _synthetic_rule_plan()
    plan["comparison"]["context_checks"] = [{
        "label": "报告期末",
        "target_path": "document.document_info.period_end",
        "reference_path": "document.document_info.period_end",
        "operation": "date",
        "required": True,
    }]

    result = compare_documents(left, right, {"synthetic_distribution_metric": plan})

    module = next(item for item in result["modules"] if item["rule_id"] == "synthetic_distribution_metric")
    assert module["status"] == "UNRESOLVED_STRUCTURE"
    assert module["differences"] == []
    assert result["warnings"] == []


def test_target_date_order_check_is_generic_and_fail_closed():
    plan = {"comparison": {"context_checks": [{
        "label": "目标日期不晚于参考日期",
        "target_path": "document.metadata.compiled_date",
        "reference_path": "document.metadata.sent_date",
        "operation": "before_or_equal",
        "required": True,
    }]}}
    target = {"metadata": {"compiled_date": "2026-01-20"}}
    reference = {"metadata": {"sent_date": "2026-01-21"}}

    assert evaluate_declared_context(plan, target, reference)[0] is True
    reference["metadata"]["sent_date"] = "2026-01-19"
    assert evaluate_declared_context(plan, target, reference)[0] is False
    reference["metadata"]["sent_date"] = None
    assert evaluate_declared_context(plan, target, reference)[0] is False


def test_reference_date_must_not_be_later_than_target_date():
    plan = {"comparison": {"context_checks": [{
        "label": "来源在目标时点已可用",
        "target_path": "document.metadata.compiled_date",
        "reference_path": "document.metadata.sent_date",
        "operation": "reference_before_or_equal",
        "required": True,
    }]}}
    target = {"metadata": {"compiled_date": "2026-01-26"}}
    reference = {"metadata": {"sent_date": "2026-01-21"}}

    assert evaluate_declared_context(plan, target, reference)[0] is True
    reference["metadata"]["sent_date"] = "2026-01-27"
    assert evaluate_declared_context(plan, target, reference)[0] is False


def test_comparison_uses_markdown_plan_date_order_instead_of_global_date_gate():
    left, right = document(), document()
    left["document_info"]["disclosure_date"] = "2026-01-26"
    right["document_info"]["disclosure_date"] = "2026-01-27"
    row = {"instrument_id": "fund-x", "label": "A 类分红",
           "fields": {"distribution_amount": value("0.25")}}
    for doc in (left, right):
        doc["modules"].append({"rule_id": "synthetic_distribution_metric", "status": "EXTRACTED",
                               "rows": [row], "scope": {"disclosure_scope": "all"}})
    plan = _synthetic_rule_plan()
    plan["comparison"]["context_checks"] = [{
        "label": "参考披露时点",
        "target_path": "document.document_info.disclosure_date",
        "reference_path": "document.document_info.disclosure_date",
        "operation": "reference_before_or_equal",
        "required": True,
    }]

    result = compare_documents(left, right, {"synthetic_distribution_metric": plan})

    module = result["modules"][0]
    assert module["status"] == "UNRESOLVED_STRUCTURE"
    assert module["differences"] == []
    assert result["status"] == "UNRESOLVED"


def test_generic_context_check_supports_normalized_exact_equality():
    left, right = document(), document()
    for doc in (left, right):
        doc["modules"].append({"rule_id": "synthetic_distribution_metric", "status": "EXTRACTED", "rows": [{
            "instrument_id": "fund-x", "label": "A 类分红",
            "fields": {"distribution_amount": value("0.25")},
        }], "scope": {"product_identity": "Fund X"}})
    plan = _synthetic_rule_plan()
    plan["comparison"]["context_checks"] = [{
        "label": "产品身份",
        "target_path": "module.scope.product_identity",
        "reference_path": "module.scope.product_identity",
        "operation": "equals",
        "required": True,
    }]

    result = compare_documents(left, right, {"synthetic_distribution_metric": plan})

    module = next(item for item in result["modules"] if item["rule_id"] == "synthetic_distribution_metric")
    assert module["status"] == "MATCH"


def test_generic_identity_group_allows_a_missing_identifier_when_another_matches():
    left, right = document(), document()
    left["document_info"]["fund_code"] = None
    right["document_info"]["fund_code"] = "050001"
    row = {"instrument_id": "fund-x", "label": "A 类分红",
           "fields": {"distribution_amount": value("0.25")}}
    left["modules"].append({"rule_id": "synthetic_distribution_metric", "status": "EXTRACTED",
                            "rows": [row], "scope": {"disclosure_scope": "all"}})
    right["modules"].append({"rule_id": "synthetic_distribution_metric", "status": "EXTRACTED",
                             "rows": [row.copy()], "scope": {"disclosure_scope": "all"}})
    plan = _synthetic_rule_plan()
    plan["comparison"]["context_check_groups"] = [{
        "label": "基金身份",
        "mode": "all_available_match",
        "minimum_matches": 1,
        "checks": [
            {"label": "基金名称", "target_path": "document.document_info.fund_name",
             "reference_path": "document.document_info.fund_name", "operation": "text"},
            {"label": "基金代码", "target_path": "document.document_info.fund_code",
             "reference_path": "document.document_info.fund_code", "operation": "identifier"},
        ],
    }]

    result = compare_documents(left, right, {"synthetic_distribution_metric": plan})

    module = next(item for item in result["modules"] if item["rule_id"] == "synthetic_distribution_metric")
    assert module["status"] == "MATCH"


def test_generic_identity_group_blocks_conflicting_available_identifiers():
    left, right = document(), document()
    left["document_info"]["fund_code"] = None
    right["document_info"]["fund_code"] = None
    left["document_info"]["alternate_code"] = "050001"
    right["document_info"]["alternate_code"] = "050002"
    row = {"instrument_id": "fund-x", "label": "A 类分红",
           "fields": {"distribution_amount": value("0.25")}}
    left["modules"].append({"rule_id": "synthetic_distribution_metric", "status": "EXTRACTED",
                            "rows": [row], "scope": {"disclosure_scope": "all"}})
    right["modules"].append({"rule_id": "synthetic_distribution_metric", "status": "EXTRACTED",
                             "rows": [row.copy()], "scope": {"disclosure_scope": "all"}})
    plan = _synthetic_rule_plan()
    plan["comparison"]["context_check_groups"] = [{
        "label": "基金身份", "mode": "all_available_match", "minimum_matches": 1,
        "checks": [
            {"label": "基金名称", "target_path": "document.document_info.fund_name",
             "reference_path": "document.document_info.fund_name", "operation": "text"},
            {"label": "补充身份代码", "target_path": "document.document_info.alternate_code",
             "reference_path": "document.document_info.alternate_code", "operation": "identifier"},
        ],
    }]

    result = compare_documents(left, right, {"synthetic_distribution_metric": plan})

    module = next(item for item in result["modules"] if item["rule_id"] == "synthetic_distribution_metric")
    assert module["status"] == "UNRESOLVED_STRUCTURE"
    assert module["differences"] == []


def test_generic_required_scope_field_stays_unresolved_when_both_sides_are_unknown():
    left, right = document(), document()
    row = {"industry_code": "B", "label": "采矿业", "market_scope": "unknown",
           "fields": {"distribution_amount": value("10")}}
    left["modules"].append({"rule_id": "synthetic_distribution_metric", "status": "EXTRACTED",
                            "rows": [row], "scope": {"disclosure_scope": "all"}})
    right["modules"].append({"rule_id": "synthetic_distribution_metric", "status": "EXTRACTED",
                             "rows": [row.copy()], "scope": {"disclosure_scope": "all"}})
    plan = _synthetic_rule_plan()
    plan["row_matching"].update(identity_fields=["industry_code"], scope_fields=["market_scope"],
                                required_scope_fields=["market_scope"])

    result = compare_documents(left, right, {"synthetic_distribution_metric": plan})

    module = next(item for item in result["modules"] if item["rule_id"] == "synthetic_distribution_metric")
    assert module["status"] == "UNRESOLVED_STRUCTURE"
    assert module["differences"] == []


def test_rule_declared_identity_prefix_normalization_preserves_hierarchy_and_original_labels():
    left, right = document(), document()
    parent_left = {"row_key": "target_parent", "label": "权益投资",
                   "fields": {"distribution_amount": value("200")}}
    parent_right = {"row_key": "reference_parent", "label": "权益投资",
                    "fields": {"distribution_amount": value("200.00")}}
    child_left = {"row_key": "target_child", "label": "其中：股票", "parent_key": "target_parent",
                  "fields": {"distribution_amount": value("100")}}
    child_right = {"row_key": "reference_child", "label": "股票", "parent_key": "reference_parent",
                   "fields": {"distribution_amount": value("100.00")}}
    left["modules"].append({"rule_id": "synthetic_distribution_metric", "status": "EXTRACTED",
                            "rows": [parent_left, child_left], "scope": {"disclosure_scope": "all"}})
    right["modules"].append({"rule_id": "synthetic_distribution_metric", "status": "EXTRACTED",
                             "rows": [parent_right, child_right], "scope": {"disclosure_scope": "all"}})
    plan = _synthetic_rule_plan()
    plan["row_matching"].update(
        identity_fields=["row_key"], fallback_identity_fields=["label"], hierarchy_fields=["parent_key"],
        identity_prefixes_to_ignore=["其中"],
    )

    result = compare_documents(left, right, {"synthetic_distribution_metric": plan})

    module = next(item for item in result["modules"] if item["rule_id"] == "synthetic_distribution_metric")
    assert module["status"] == "MATCH"
    assert module["prospectus_rows"][1]["label"] == "其中：股票"
    assert module["periodic_rows"][1]["label"] == "股票"


def test_rule_without_execution_plan_fails_closed_instead_of_using_other_rule_semantics():
    left, right = document(), document()
    custom_module = {"rule_id": "synthetic_new_chapter", "title": "新章节", "status": "EXTRACTED", "rows": []}
    left["modules"].append(custom_module)
    right["modules"].append(custom_module.copy())

    result = compare_documents(left, right)

    module = next(item for item in result["modules"] if item["rule_id"] == "synthetic_new_chapter")
    assert module["status"] == "UNRESOLVED_STRUCTURE"
    assert "未套用其他规则" in module["review_message"]


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


def test_asset_child_parent_label_aligns_when_reference_parent_key_is_missing():
    parent_left = asset_row("100", "100")
    parent_left.update(row_key="equity_investments", parent_key=None, label="权益投资")
    child_left = asset_row("100", "100")
    child_left.update(row_key="equity_investments_stocks", parent_key="equity_investments", label="其中：股票")
    parent_right = asset_row("100", "100")
    parent_right.update(row_key="", parent_key=None, label="权益投资")
    child_right = asset_row("100", "100")
    child_right.update(row_key="", parent_key="权益投资", label="其中：股票")

    result = compare_documents(
        document(asset_rows=[parent_left, child_left]),
        document(asset_rows=[parent_right, child_right]),
    )

    assert result["status"] == "MATCH"
    assert result["difference_count"] == 0


def test_declared_fallback_identity_aligns_document_local_keys_and_partial_parent_refs():
    parent_left = asset_row("100", "100")
    parent_left.update(row_key="equity_investment", parent_key=None, label="权益投资")
    child_left = asset_row("100", "100")
    child_left.update(row_key="stock", parent_key="equity_investment", label="其中：股票")

    parent_right = asset_row("100", "100")
    parent_right.update(row_key="权益投资", parent_key=None, label="权益投资")
    child_right = asset_row("100", "100")
    # The right extractor retained a parent pointer from another identity
    # namespace. Its unique source label is still a valid declared fallback.
    child_right.update(row_key="其中：股票", parent_key="equity_investment", label="其中：股票")

    result = compare_documents(
        document(asset_rows=[parent_left, child_left]),
        document(asset_rows=[parent_right, child_right]),
    )

    assert result["status"] == "MATCH"
    assert result["difference_count"] == 0

    child_right["fields"]["amount"] = value("99")
    mismatch = compare_documents(
        document(asset_rows=[parent_left, child_left]),
        document(asset_rows=[parent_right, child_right]),
    )
    assert mismatch["status"] == "MISMATCH"
    assert any(
        difference.get("row_label") == "其中：股票"
        and difference.get("field_key") == "amount"
        and difference.get("status") == "MISMATCH"
        for module in mismatch["modules"]
        for difference in module.get("differences", [])
    )


def test_unresolved_document_local_parent_references_do_not_block_unique_row_identity():
    left_row = asset_row("250.00", "1.00")
    left_row.update(row_key="same_subrow", parent_key="resale_financial_assets", label="子项目")
    right_row = asset_row("250.00", "1.00")
    right_row.update(row_key="same_subrow", parent_key="resale_agreements", label="子项目")

    result = compare_documents(
        document(asset_rows=[left_row]),
        document(asset_rows=[right_row]),
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


def test_unique_declared_fallback_identity_can_disambiguate_repeated_primary_key():
    def duplicate(label: str):
        row = asset_row("10", "1")
        row.update(row_key="other_assets", label=label)
        return row

    result = compare_documents(
        document(asset_rows=[duplicate("其他资产"), duplicate("其他项目")]),
        document(asset_rows=[duplicate("其他资产"), duplicate("其他项目")]),
    )

    assert result["status"] == "MATCH"
    assert result["modules"][0]["status"] == "MATCH"


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
                              "scope": {"market_scope": "domestic", "investment_scope": "active"},
                              "page": 1, "quote": "无持仓"}]
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


def test_rule_required_period_mismatch_does_not_become_field_difference():
    result = compare_documents(
        document(period="2025-12-31", asset_rows=[asset_row("1")]),
        document(period="2026-09-30", asset_rows=[asset_row("2")]),
    )

    assert result["status"] == "UNRESOLVED"
    assert result["difference_count"] == 0
    assert result["modules"][0]["status"] == "UNRESOLVED_STRUCTURE"
    assert result["modules"][0]["differences"] == []
    assert result["warnings"] == []


def test_report_disclosed_after_prospectus_is_not_used():
    result = compare_documents(
        document(disclosure="2026-01-30", asset_rows=[asset_row("1")]),
        document(disclosure="2026-02-01", asset_rows=[asset_row("2")]),
    )

    assert result["status"] == "UNRESOLVED"
    assert all(module["status"] == "UNRESOLVED_STRUCTURE" for module in result["modules"])
    assert all(not module["differences"] for module in result["modules"])
    assert all("身份、期间或适用范围" in module["review_message"] for module in result["modules"])

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


def test_complete_reference_can_confirm_a_target_row_missing_even_when_target_scope_is_unknown():
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

    assert compared['status'] == 'MISMATCH'
    assert compared['differences'][0]['status'] == 'MISSING_ROW'


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


def test_unique_stock_with_unknown_market_matches_by_unique_code_without_conflict():
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


def test_changed_rank_is_not_compared_when_the_plan_does_not_declare_it_a_fact():
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
    assert result["status"] == "MATCH"
    assert stock_module["status"] == "MATCH"
    assert stock_module["differences"] == []



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
                "nav_ratio": value("99.21", "%"),
                "nav_percentage": value("99.21", "%"),
                "total_fair_value": value("4,722,395,708.64"),
                "total_nav_ratio": value("99.21", "%"),
                "total_nav_percentage": value("99.21", "%"),
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


def test_industry_identity_keeps_investment_scope_separate():
    def industry(scope, amount):
        return {
            "label": "制造业", "industry_code": "C", "industry_name": "制造业",
            "market_scope": "domestic", "investment_scope": scope,
            "fields": {"fair_value": value(amount), "ratio_of_net_assets": value("1", "%")},
        }

    left, right = document(), document()
    rows = [industry("index", "100"), industry("active", "20")]
    left["modules"].append({"rule_id": "synthetic_scoped_rows", "status": "EXTRACTED", "rows": rows, "scope": {}})
    right["modules"].append({"rule_id": "synthetic_scoped_rows", "status": "EXTRACTED", "rows": rows, "scope": {}})
    plan = _synthetic_rule_plan()
    plan["rule_identity"] = "synthetic_scoped_rows"
    plan["row_matching"].update(identity_fields=["industry_code"], scope_fields=["market_scope", "investment_scope"],
                                 fallback_identity_fields=["industry_name"])
    plan["comparison"]["fields"] = [
        {"field": "fair_value", "label": "公允价值", "operation": "decimal", "required": True},
        {"field": "nav_ratio", "label": "占净值比例", "operation": "decimal", "required": True},
    ]
    for row in rows:
        row["industry_code"] = "C"
        row["fields"]["nav_ratio"] = value("1", "%")
    result = compare_documents(left, right, {"synthetic_scoped_rows": plan})
    module = next(item for item in result["modules"] if item["rule_id"] == "synthetic_scoped_rows")
    assert module["status"] == "MATCH"
    assert module["differences"] == []


def test_stock_identity_keeps_index_and_active_scope_separate():
    def stock(scope, amount):
        return {
            "label": "样例公司", "stock_code": "600001", "stock_name": "样例公司",
            "market_scope": "domestic", "investment_scope": scope, "investment_subtable": scope,
            "fields": {"quantity": value("10", "股"), "fair_value": value(amount),
                       "ratio_of_net_assets": value("1", "%")},
        }

    result = compare_documents(
        document(stock_rows=[stock("index", "100"), stock("active", "20")]),
        document(stock_rows=[stock("index", "100"), stock("active", "20")]),
    )
    assert result["status"] == "MATCH"
    assert result["difference_count"] == 0


def test_generic_row_alignment_can_pair_an_unspecified_scope_by_unique_identity():
    left, right = document(), document()
    target_row = {
        "instrument_id": "600001", "label": "样例公司",
        "fields": {"distribution_amount": value("100")},
    }
    reference_row = {
        **target_row, "market_scope": "domestic",
        "fields": {"distribution_amount": value("100.00")},
    }
    left["modules"].append({"rule_id": "synthetic_distribution_metric", "status": "EXTRACTED",
                            "rows": [target_row], "scope": {"disclosure_scope": "all"}})
    right["modules"].append({"rule_id": "synthetic_distribution_metric", "status": "EXTRACTED",
                             "rows": [reference_row], "scope": {"disclosure_scope": "all"}})
    plan = _synthetic_rule_plan()
    plan["row_matching"].update(identity_fields=["instrument_id"], scope_fields=["market_scope"],
                                target_population="both_documents", reference_superset_allowed=False)

    result = compare_documents(left, right, {"synthetic_distribution_metric": plan})

    module = next(item for item in result["modules"] if item["rule_id"] == "synthetic_distribution_metric")
    assert module["status"] == "MATCH"
    assert module["differences"] == []


def test_generic_row_alignment_does_not_bridge_conflicting_or_ambiguous_scopes():
    left, right = document(), document()
    target_row = {
        "instrument_id": "600001", "market_scope": "domestic", "label": "样例公司",
        "fields": {"distribution_amount": value("100")},
    }
    target_unspecified = {**target_row, "market_scope": None}
    other_scopes = [
        (target_row, [{**target_row, "market_scope": "overseas"}]),
        (target_unspecified, [{**target_row, "market_scope": "domestic"},
                              {**target_row, "market_scope": "overseas"}]),
    ]
    plan = _synthetic_rule_plan()
    plan["row_matching"].update(identity_fields=["instrument_id"], scope_fields=["market_scope"],
                                target_population="both_documents", reference_superset_allowed=False)
    expected_statuses = ["UNRESOLVED_STRUCTURE", "UNRESOLVED_STRUCTURE"]
    for (target, reference_rows), expected_status in zip(other_scopes, expected_statuses):
        left_copy, right_copy = document(), document()
        left_copy["modules"].append({"rule_id": "synthetic_distribution_metric", "status": "EXTRACTED",
                                     "rows": [target],
                                     "scope": {"disclosure_scope": "all"}})
        right_copy["modules"].append({"rule_id": "synthetic_distribution_metric", "status": "EXTRACTED",
                                      "rows": reference_rows,
                                      "scope": {"disclosure_scope": "all"}})

        result = compare_documents(left_copy, right_copy, {"synthetic_distribution_metric": plan})

        module = next(item for item in result["modules"] if item["rule_id"] == "synthetic_distribution_metric")
        assert module["status"] == expected_status


def test_one_sided_placeholder_row_is_not_silently_equated_with_absence():
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

    assert result["status"] == "MISMATCH"
    assert result["difference_count"] == 1
    assert result["modules"][1]["differences"][0]["status"] == "MISSING_ROW"


def test_within_days_and_warn_allow_comparison_within_half_year():
    import copy
    _, plans, _ = load_investment_rule_definitions()
    plan = copy.deepcopy(plans["investment.asset_allocation"])
    plan["comparison"]["context_checks"] = [
        {
            "label": "定期报告披露日期不晚于招募说明书披露日期",
            "target_path": "document.document_info.disclosure_date",
            "reference_path": "document.document_info.disclosure_date",
            "operation": "reference_before_or_equal",
            "required": True,
        },
        {
            "label": "双方报告期末相距不超过183天",
            "target_path": "document.document_info.period_end",
            "reference_path": "document.document_info.period_end",
            "operation": "within_days",
            "max_days": 183,
            "required": True,
        },
        {
            "label": "双方报告期末不一致提示",
            "target_path": "document.document_info.period_end",
            "reference_path": "document.document_info.period_end",
            "operation": "date",
            "required": False,
            "on_mismatch": "warn",
        },
    ]
    left = document(disclosure="2025-11-24", period="2025-06-30", asset_rows=[asset_row("100")])
    right = document(disclosure="2025-10-28", period="2025-09-30", asset_rows=[asset_row("200")])

    result = compare_documents(left, right, {"investment.asset_allocation": plan})

    assert result["status"] == "MISMATCH"
    assert result["difference_count"] == 1
    assert any("2025-06-30" in w and "2025-09-30" in w for w in result["warnings"])

    far_right = document(disclosure="2025-10-28", period="2026-03-31", asset_rows=[asset_row("200")])
    blocked = compare_documents(left, far_right, {"investment.asset_allocation": plan})
    assert blocked["status"] == "UNRESOLVED"
    assert blocked["modules"][0]["status"] == "UNRESOLVED_STRUCTURE"


def test_hydrate_investment_document_dates_fills_prospectus_cover_month_and_version_date():
    from app.runtime import _hydrate_investment_document_dates

    left, right = _hydrate_investment_document_dates(
        {"document_info": {"fund_name": "样本基金", "disclosure_date": None, "period_end": "2025-06-30"}},
        {"document_info": {"fund_name": "样本基金", "disclosure_date": "2025-10-28", "period_end": "2025-09-30"}},
        {
            "prospectus": {
                "version_date_interval": None,
                "date_interval": {"start": "2025-11-01", "end": "2025-11-30"},
                "date_quote": "二〇二五年十一月",
            },
            "periodic_report": {},
        },
    )
    assert left["document_info"]["disclosure_date"] == "2025-11-01"
    assert right["document_info"]["disclosure_date"] == "2025-10-28"
