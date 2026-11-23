import json

import pymupdf as fitz

from app.summary_check import (
    RULES,
    _compare_by_execution_plan,
    _reason_for_final_status,
    _available_metadata_reference_ids,
    _metadata_facts_for_roles,
    _filter_unavailable_metadata_refs,
    _review_conditions_are_clear,
    _primary_lookup_reference,
    _shared_context,
    _context_plan,
    _heading_info,
    build_document_source_catalogue,
    build_source_catalogue,
)
from app.summary_rules import load_markdown_rules


def _pdf_bytes(*pages: str) -> bytes:
    doc = fitz.open()
    for text in pages:
        page = doc.new_page(width=600, height=900)
        for index, line in enumerate(text.splitlines()):
            page.insert_text((40, 50 + index * 20), line, fontname="china-s", fontsize=11)
    result = doc.tobytes()
    doc.close()
    return result


def test_active_summary_rules_are_chinese_markdown_with_compiled_generic_plans():
    config, rules = load_markdown_rules(require_execution_plans=True)
    assert config["version"]
    assert len(rules) == 19
    assert "regional_allocation_chart" in {rule["item_id"] for rule in rules}
    assert len({rule["item_id"] for rule in rules}) == len(rules)
    assert all(rule["execution_plan"]["rule_scope"] == "product_summary" for rule in rules)
    assert all(rule["comparison_fields"] for rule in rules)
    assert all("excel_row" not in rule and "excel_source_cell" not in rule for rule in rules)
    assert {rule["item_id"] for rule in RULES} == {rule["item_id"] for rule in rules}


def test_pdf_catalogue_preserves_addressable_sources_and_cross_page_sections():
    catalogue = build_source_catalogue(
        "sample.pdf",
        _pdf_bytes("一、产品概况", "基金管理人：甲公司"),
        "product_summary",
    )
    assert catalogue["page_count"] == 2
    assert len(catalogue["pages"]) == 2
    assert all(source["source_id"] and source["bbox"] for page in catalogue["pages"] for source in page["sources"])
    assert catalogue["chapters"]
    assert catalogue["chapters"][0]["pages"] == [1, 2]


def test_document_catalogue_keeps_verified_markdown_conversion_provenance():
    catalogue = build_document_source_catalogue(
        "sample.pdf", _pdf_bytes("5.1 基金基本情况", "基金管理人：甲公司"), "periodic_report",
    )
    assert catalogue["page_count"] == 2
    assert catalogue["chapters"]
    sources = [source for page in catalogue["pages"] for source in page["sources"]]
    assert sources
    assert all(source.get("document_role") == "periodic_report" for source in sources)


def test_context_planning_respects_budget_and_reports_full_coverage():
    sources = [{"source_id": f"s{index}", "raw_text": f"第{index}条原文", "page": 1,
                "bbox": [0, index, 100, index + 1], "section_path": ["基金基本情况"]}
               for index in range(120)]
    plan = _context_plan({"pages": [{"sources": sources}]}, sources)
    sent = [source for batch in plan["batches"] for source in batch["candidates"]]
    assert plan["coverage_complete"]
    assert plan["source_count"] == len(sources)
    assert {source["source_id"] for source in sent} == {source["source_id"] for source in sources}
    assert all(len(json.dumps(batch, ensure_ascii=False)) <= 16_000 for batch in plan["batches"])


def test_fallback_context_includes_verified_target_evidence_as_addressable_sources():
    target = {
        "source_id": "target-rate",
        "document_role": "product_summary",
        "raw_text": "管理费 0.15%",
        "page": 3,
    }
    fallback = {
        "source_id": "reference-rate",
        "document_role": "prospectus",
        "raw_text": "本基金管理费年费率为0.15%",
        "page": 80,
    }

    context = _shared_context([{
        "item_id": "test-rule",
        "verified_target": {"value": "管理费0.15%", "source_ids": ["target-rate"]},
        "verified_target_candidates": [target],
        "fallback_candidates": [fallback],
    }])

    item = context["items"][0]
    assert item["verified_target"]["source_ids"] == ["target-rate"]
    if "context_pool" in context:
        assert item["allowed_verified_target_source_ids"] == ["target-rate"]
        sources = context["context_pool"]["sources"]
        assert sources["target-rate"]["raw_text"] == "管理费 0.15%"
        assert sources["reference-rate"]["raw_text"] == "本基金管理费年费率为0.15%"
    else:
        assert item["verified_target_candidates"] == [target]
        assert item["fallback_candidates"] == [fallback]


def test_generic_heading_parser_rejects_bare_codes_and_dates():
    assert _heading_info("5.1 基金基本情况")
    assert _heading_info("510100") is None
    assert _heading_info("2019-09-06") is None


def test_declared_multi_field_text_comparison_uses_model_verdict_and_numeric_fails_closed():
    text_rule = {"execution_plan": {"comparison": {"fields": [
        {"field": "trading_name", "operation": "text"},
        {"field": "extended_name", "operation": "semantic"},
    ]}}}
    numeric_rule = {"execution_plan": {"comparison": {"fields": [
        {"field": "amount", "operation": "decimal"},
        {"field": "ratio", "operation": "decimal"},
    ]}}}
    assert _compare_by_execution_plan(text_rule, "简称甲；全称甲", "简称甲；全称甲", "MATCH") == "MATCH"
    assert _compare_by_execution_plan(text_rule, "简称甲；全称甲", "简称乙；全称乙", "UNRESOLVED") == "UNRESOLVED"
    assert _compare_by_execution_plan(
        text_rule, "值甲", "值乙", "MISMATCH", applicability_status="UNRESOLVED",
    ) == "UNRESOLVED"
    assert _compare_by_execution_plan(
        text_rule, "值甲", "值乙", "MISMATCH", applicability_status="CONFIRMED",
    ) == "MISMATCH"
    assert _compare_by_execution_plan(
        text_rule, "值甲", "值乙", "MISMATCH", applicability_status="CONFIRMED",
        review_conditions_clear=False,
    ) == "UNRESOLVED"
    assert _compare_by_execution_plan(
        text_rule, "值甲", "值乙", "MISMATCH", applicability_status="CONFIRMED",
        declared_context_clear=False,
    ) == "UNRESOLVED"
    assert _compare_by_execution_plan(numeric_rule, "1; 2", "1; 2", "MATCH") == "UNRESOLVED"


def test_final_comparison_reason_matches_deterministic_status():
    assert "判定一致" in _reason_for_final_status("MATCH", "模型仍无法确认")
    assert "存在差异" in _reason_for_final_status("MISMATCH", "模型仍无法确认")
    assert _reason_for_final_status("UNRESOLVED", "缺少适用日期") == "缺少适用日期"
    assert "未全部核验通过" in _reason_for_final_status(
        "UNRESOLVED", "模型认为双方一致", model_status="MATCH", review_conditions_clear=False,
    )
    assert "上下文条件未全部通过" in _reason_for_final_status(
        "UNRESOLVED", "模型认为双方一致", model_status="MATCH", declared_context_clear=False,
    )
    sanitized = _reason_for_final_status(
        "UNRESOLVED",
        "定期报告候选及原文均未提供；报告coverage为batch_count 0（periodic_report.sent_date）。",
    )
    assert "coverage" not in sanitized
    assert "batch_count" not in sanitized
    assert "periodic_report.sent_date" not in sanitized
    assert "定期报告送出日" in sanitized


def test_review_conditions_must_be_explicitly_resolved_with_rule_scoped_evidence():
    rule = {"execution_plan": {"review_conditions": ["报告期间无法对应时待复核", "适用时点不明时待复核"]}}
    allowed = {"summary-date", "reference-period"}
    assert not _review_conditions_are_clear(rule, {}, allowed, allowed)
    assert _review_conditions_are_clear(rule, {
        "review_condition_assessments": [
            {"condition_index": 0, "status": "CLEAR", "source_ids": ["summary-date"], "reason": "概要日期原文"},
            {"condition_index": 1, "status": "CLEAR", "source_ids": ["reference-period"], "reason": "报告期间原文"},
        ],
    }, allowed, allowed)
    assert not _review_conditions_are_clear(rule, {
        "review_condition_assessments": [
            {"condition_index": 0, "status": "CLEAR", "source_ids": ["summary-date"], "reason": "概要日期原文"},
            {"condition_index": 1, "status": "UNRESOLVED", "source_ids": [], "reason": "缺报告期间"},
        ],
    }, allowed, allowed)
    assert not _review_conditions_are_clear(rule, {
        "review_condition_assessments": [
            {"condition_index": 0, "status": "CLEAR", "source_ids": ["summary-date"], "reason": "概要日期原文"},
            {"condition_index": 0, "status": "CLEAR", "source_ids": ["reference-period"], "reason": "报告期间原文"},
        ],
    }, allowed, allowed)


def test_review_condition_can_cite_trusted_document_metadata_without_pdf_source_id():
    rule = {"execution_plan": {"review_conditions": ["报告送出日期不晚于概要编制日期"]}}
    assert _review_conditions_are_clear(rule, {
        "review_condition_assessments": [
            {
                "condition_index": 0,
                "status": "CLEAR",
                "source_ids": [],
                "metadata_refs": ["periodic_report.sent_date", "product_summary.compiled_date"],
                "reason": "报告在概要编制前已送出",
            },
        ],
    }, set(), set(), available_metadata_refs={
        "periodic_report.sent_date", "product_summary.compiled_date",
    })


def test_review_condition_rejects_unknown_metadata_reference():
    rule = {"execution_plan": {"review_conditions": ["报告日期有效"]}}
    assert not _review_conditions_are_clear(rule, {
        "review_condition_assessments": [
            {
                "condition_index": 0,
                "status": "CLEAR",
                "source_ids": [],
                "metadata_refs": ["periodic_report.made_up_date"],
                "reason": "日期可用",
            },
        ],
    }, set(), set(), available_metadata_refs={"periodic_report.sent_date"})


def test_metadata_reference_allowlist_omits_missing_or_empty_facts():
    assert _available_metadata_reference_ids({
        "product_summary": {"metadata": {"fund_code": "050011", "fund_name": None}},
        "periodic_report": {"metadata": {"sent_date": "2026-08-31", "period_end": ""}},
    }) == {"product_summary.fund_code", "periodic_report.sent_date"}


def test_primary_metadata_context_is_invariant_to_an_unselected_fallback_document():
    documents = {
        "product_summary": {"metadata": {"fund_code": "050011", "compiled_date": "2026-09-18"}},
        "periodic_report": {"metadata": {"fund_code": "050011", "sent_date": "2026-08-31"}},
        "prospectus": {"metadata": {
            "fund_name": "博时信用债券投资基金", "fund_code": None,
            "filename_date_interval": {"start": "2026-09-21", "end": "2026-09-21"},
            "filename_date_quote": "文件名日期前缀：2026-09-21",
            "filename_date_precision": "day",
        }},
    }
    primary_roles = {"product_summary", "periodic_report"}
    assert _available_metadata_reference_ids(documents, primary_roles) == {
        "product_summary.fund_code", "product_summary.compiled_date",
        "periodic_report.fund_code", "periodic_report.sent_date",
    }
    assert _metadata_facts_for_roles(documents, primary_roles) == {
        "product_summary": {"fund_code": "050011", "compiled_date": "2026-09-18"},
        "periodic_report": {"fund_code": "050011", "sent_date": "2026-08-31"},
    }
    assert _metadata_facts_for_roles(documents, primary_roles | {"prospectus"})["prospectus"]["filename_date_interval"] == {
        "start": "2026-09-21", "end": "2026-09-21",
    }
    assert "prospectus.filename_date_quote" in _available_metadata_reference_ids(
        documents, primary_roles | {"prospectus"},
    )
    assert "prospectus.fund_name" in _available_metadata_reference_ids(documents, primary_roles | {"prospectus"})


def test_unavailable_metadata_refs_are_removed_but_retained_for_audit_when_pdf_citation_exists():
    assessment = {
        "review_condition_assessments": [{
            "condition_index": 0, "status": "CLEAR", "source_ids": ["manager-date"],
            "metadata_refs": ["product_summary.fund_name"], "reason": "PDF日期证据支持任职日期",
        }],
    }
    normalized = _filter_unavailable_metadata_refs(assessment, {"product_summary.compiled_date"})
    condition = normalized["review_condition_assessments"][0]
    assert condition["metadata_refs"] == []
    assert condition["rejected_metadata_refs"] == ["product_summary.fund_name"]
    rule = {"execution_plan": {"review_conditions": ["任职日期已核实"]}}
    assert _review_conditions_are_clear(rule, normalized, {"manager-date"}, {"manager-date"},
                                        available_metadata_refs={"product_summary.compiled_date"})


def test_unavailable_metadata_refs_alone_do_not_satisfy_review_condition():
    assessment = _filter_unavailable_metadata_refs({
        "review_condition_assessments": [{
            "condition_index": 0, "status": "CLEAR", "source_ids": [],
            "metadata_refs": ["product_summary.fund_name"], "reason": "未知字段",
        }],
    }, set())
    rule = {"execution_plan": {"review_conditions": ["产品身份已确认"]}}
    assert not _review_conditions_are_clear(rule, assessment, set(), set(), available_metadata_refs=set())


def test_unavailable_metadata_refs_do_not_make_not_applicable_assessment_valid():
    assessment = _filter_unavailable_metadata_refs({
        "review_condition_assessments": [{
            "condition_index": 0, "status": "NOT_APPLICABLE", "source_ids": [],
            "metadata_refs": ["product_summary.fund_name"], "reason": "未使用此分支",
        }],
    }, set())
    rule = {"execution_plan": {"review_conditions": ["仅在使用补充来源时适用"]}}
    assert not _review_conditions_are_clear(rule, assessment, set(), set(), available_metadata_refs=set())


def test_malformed_metadata_reference_is_rejected_without_crashing():
    normalized = _filter_unavailable_metadata_refs({
        "review_condition_assessments": [{
            "condition_index": 0, "status": "CLEAR", "source_ids": [],
            "metadata_refs": [{"role": "product_summary", "field": "fund_name"}],
            "reason": "元数据引用格式无效",
        }],
    }, {"product_summary.fund_code"})
    condition = normalized["review_condition_assessments"][0]
    assert condition["metadata_refs"] == []
    assert condition["rejected_metadata_refs"] == [{"role": "product_summary", "field": "fund_name"}]


def test_primary_section_lookup_preserves_applicability_and_rule_assessments():
    rule = {"item_id": "management_fee", "primary_role": "periodic_report",
            "execution_plan": {"review_conditions": ["费率适用时点不清时待复核"]}}
    found = {
        "comparison_status": "MATCH",
        "applicability_status": "CONFIRMED",
        "coverage_status": "COMPLETE",
        "review_condition_assessments": [
            {"condition_index": 0, "status": "CLEAR", "source_ids": ["report-rate"], "reason": "同一费率原文已提供"},
        ],
        "reason": "费率相同",
    }
    primary_fact = {"value": "0.70%", "source_ids": ["report-rate"]}
    reference = _primary_lookup_reference(rule, found, primary_fact, coverage_complete=True)
    assert reference["status"] == "MATCH"
    assert reference["applicability_status"] == "CONFIRMED"
    assert reference["coverage_status"] == "COMPLETE"
    assert _review_conditions_are_clear(rule, reference, {"report-rate"}, {"report-rate"})


def test_review_condition_can_be_explicitly_not_applicable_without_document_citation():
    rule = {"execution_plan": {"review_conditions": ["仅在使用补充来源时适用"]}}
    assert _review_conditions_are_clear(rule, {
        "review_condition_assessments": [
            {"condition_index": 0, "status": "NOT_APPLICABLE", "source_ids": [], "reason": "本次没有使用补充来源"},
        ],
    }, set(), set())


def test_review_condition_rejects_not_applicable_with_unrelated_source_ids():
    rule = {"execution_plan": {"review_conditions": ["仅在使用补充来源时适用"]}}
    assert not _review_conditions_are_clear(rule, {
        "review_condition_assessments": [
            {"condition_index": 0, "status": "NOT_APPLICABLE", "source_ids": ["unrelated"], "reason": "本次没有使用补充来源"},
        ],
    }, {"unrelated"}, {"unrelated"})


import asyncio as _asyncio
from app.summary_check import (
    _compare_by_execution_plan,
    _numbers_are_grounded,
    _verified_fact,
    capture_product_summary_rule_snapshot,
    compare_product_summary,
)


def test_numbers_are_grounded_supports_table_percent_header_chinese_percent_and_metadata():
    table_source = [{
        "source_id": "t1",
        "raw_text": "权益投资 99.13",
        "row_text": "权益投资 | 99.13",
        "cell_text": "99.13",
        "section_path": ["5.1 2025年年末资产组合情况"],
        "layout_context": [{"raw_text": "占基金总资产的比例（%）"}],
    }]
    assert _numbers_are_grounded("2025年权益投资：99.13%", table_source)
    assert not _numbers_are_grounded("权益投资：99.13%，合计100.00%", table_source)

    chinese_fee_source = [{"source_id": "f1", "raw_text": "本基金的管理费年费率为百分之零点一五。"}]
    assert _numbers_are_grounded("0.15%", chinese_fee_source)

    metadata_source = [{"source_id": "m1", "raw_text": "产品资料概要更新编制日期为披露日期"}]
    assert _numbers_are_grounded("2026-01-26", metadata_source, extra_texts=["2026-01-26"])

    cjk_noise_source = [{"source_id": "n1", "raw_text": "上证50指数成分比例(%)；第5条费率比例(%)"}]
    assert not _numbers_are_grounded("50%", cjk_noise_source)
    assert not _numbers_are_grounded("5%", cjk_noise_source)


def test_verified_fact_rescues_derived_total_using_structured_field_values():
    rule = {"item_id": "asset_allocation_chart"}
    candidates = [{"source_id": "s1", "raw_text": "权益投资 99.13%，银行存款 0.87%"}]
    rescued = _verified_fact(
        "权益投资99.13%，银行存款0.87%，合计100.00%",
        ["s1"],
        candidates,
        rule,
        field_values={"percentage": {"权益投资": "99.13%", "银行存款": "0.87%"}},
    )
    assert rescued is not None
    assert "100.00%" not in rescued["value"]
    assert "99.13%" in rescued["value"] and "0.87%" in rescued["value"]
    assert rescued["field_values"] == {"percentage": {"权益投资": "99.13%", "银行存款": "0.87%"}}

    ungrounded = _verified_fact(
        "合计100.00%",
        ["s1"],
        candidates,
        rule,
        field_values={"percentage": {"合计": "100.00%"}},
    )
    assert ungrounded is None


def test_compare_by_execution_plan_handles_structured_multi_fields_and_dash_rows():
    date_decimal_rule = {
        "execution_plan": {
            "comparison": {
                "fields": [
                    {"field": "data_cutoff_date", "label": "数据截止日", "operation": "date", "required": True},
                    {"field": "percentage", "label": "占比明细", "operation": "decimal", "required": True},
                ],
            },
        },
    }
    target_fields = {"data_cutoff_date": "2025-12-31", "percentage": {"权益投资": "99.13%", "其他资产": "0.87%"}}
    ref_match_fields = {"data_cutoff_date": "2025年12月31日", "percentage": {"权益投资": "99.13%", "固定收益投资": "-", "其他资产": "0.87%"}}
    assert _compare_by_execution_plan(
        date_decimal_rule, "ignored", "ignored", "MATCH",
        target_fields=target_fields, reference_fields=ref_match_fields,
    ) == "MATCH"

    ref_mismatch_fields = {"data_cutoff_date": "2025-12-31", "percentage": {"权益投资": "98.00%", "其他资产": "0.87%"}}
    assert _compare_by_execution_plan(
        date_decimal_rule, "ignored", "ignored", "MATCH",
        target_fields=target_fields, reference_fields=ref_mismatch_fields,
    ) == "MISMATCH"

    ref_extra_nonzero = {"data_cutoff_date": "2025-12-31", "percentage": {"权益投资": "99.13%", "固定收益投资": "1.00%", "其他资产": "0.87%"}}
    assert _compare_by_execution_plan(
        date_decimal_rule, "ignored", "ignored", "UNRESOLVED",
        target_fields=target_fields, reference_fields=ref_extra_nonzero,
    ) == "UNRESOLVED"

    # Without structured fields, multi-field numeric/date rules remain fail-closed.
    assert _compare_by_execution_plan(date_decimal_rule, "2025-12-31; 99.13%", "2025-12-31; 99.13%", "MATCH") == "UNRESOLVED"

    equals_rule = {
        "execution_plan": {
            "comparison": {
                "fields": [
                    {"field": "nav_growth_rate", "label": "基金净值增长率", "operation": "equals", "required": True},
                    {"field": "benchmark_return_rate", "label": "业绩比较基准收益率", "operation": "equals", "required": True},
                ],
            },
        },
    }
    assert _compare_by_execution_plan(
        equals_rule, "ignored", "ignored", "MATCH",
        target_fields={"nav_growth_rate": {"2024": "10.50%"}, "benchmark_return_rate": {"2024": "8.20%"}},
        reference_fields={"nav_growth_rate": {"2024": "10.50%"}, "benchmark_return_rate": {"2024": "8.20%"}},
    ) == "MATCH"
    assert _compare_by_execution_plan(
        equals_rule, "ignored", "ignored", "MATCH",
        target_fields={"nav_growth_rate": {"2024": "10.5%"}, "benchmark_return_rate": {"2024": "8.20%"}},
        reference_fields={"nav_growth_rate": {"2024": "10.50%"}, "benchmark_return_rate": {"2024": "8.20%"}},
    ) == "MISMATCH"


def test_compare_product_summary_runs_concurrent_lookup_and_fallback_with_mock_provider():
    snapshot = capture_product_summary_rule_snapshot()
    selected_ids = ("custodian", "audit_fee", "annual_nav_performance_chart")
    sub_rules = [rule for rule in snapshot["rules"] if rule["item_id"] in selected_ids]
    mini_snapshot = {**snapshot, "rules": sub_rules}

    def make_source(role: str, sid: str, sec_id: str, sec_title: str, page: int, text: str) -> dict:
        return {
            "source_id": sid,
            "document_role": role,
            "page": page,
            "section_id": sec_id,
            "section_path": [sec_title],
            "raw_text": text,
            "row_text": text,
            "cell_text": text,
        }

    sum_sources = [
        make_source("product_summary", "s_cust", "sum_1", "产品概况", 1, "基金托管人：中国建设银行股份有限公司"),
        make_source("product_summary", "s_audit", "sum_2", "投资本基金涉及的费用", 1, "审计费用：120000元"),
        {**make_source("product_summary", "s_nav", "sum_3", "(三) 自基金合同生效以来基金每年的净值增长率及与同期业绩比较基准的比较", 1, "2024年基金净值增长率10.50%，业绩比较基准收益率8.20%"), "cell_text": "基金净值增长率"},
    ]
    report_sources = [
        make_source("periodic_report", "r_noise", "sec_noise", "§1 重要提示", 1, "托管及净值与审计费用提示"),
        make_source("periodic_report", "r_sec0", "sec_0", "§2 运作提示", 2, "基金运作说明"),
        make_source("periodic_report", "r_sec1", "sec_1", "§3 相关服务机构", 3, "基金托管人：中国建设银行股份有限公司"),
        make_source("periodic_report", "r_sec2", "sec_2", "§4 主要财务指标", 4, "期末资产净值情况"),
        make_source("periodic_report", "r_sec3", "sec_3", "§5 基金份额变动", 5, "份额申购赎回情况"),
        make_source("periodic_report", "r_sec4", "sec_4", "§6 备查文件目录", 6, "备查文件清单"),
        make_source("periodic_report", "r_audit", "sec_fee", "§7 基金费用披露", 7, "本期审计费用与基金费用说明"),
        make_source("periodic_report", "r_nav", "sec_nav", "§3.2 基金净值表现", 8, "基金净值增长率与业绩比较基准：过去三个月净值增长率3.10%，不含自然年度数据"),
    ]
    pros_sources = [
        make_source("prospectus", "p_audit", "p_fee", "基金的费用与税收", 1, "审计费用：120000元（年预估费用）"),
        make_source("prospectus", "p_nav", "p_perf", "基金的业绩", 2, "2024年基金净值增长率10.50%，同期业绩比较基准收益率8.20%"),
    ]

    catalogues = {
        "product_summary": {
            "filename": "summary.pdf", "role": "product_summary", "page_count": 1, "page_dimensions": {},
            "source_count": len(sum_sources), "logical_units": [],
            "chapters": [{"section_id": s["section_id"], "title": s["section_path"][0], "path": " / ".join(s["section_path"]), "pages": [1], "source_ids": [s["source_id"]]} for s in sum_sources],
            "pages": [{"page": 1, "sources": sum_sources}],
        },
        "periodic_report": {
            "filename": "report.pdf", "role": "periodic_report", "page_count": 8, "page_dimensions": {},
            "source_count": len(report_sources), "logical_units": [],
            "chapters": [{"section_id": s["section_id"], "title": s["section_path"][0], "path": " / ".join(s["section_path"]), "pages": [s["page"]], "source_ids": [s["source_id"]]} for s in report_sources],
            "pages": [{"page": s["page"], "sources": [s]} for s in report_sources],
        },
        "prospectus": {
            "filename": "prospectus.pdf", "role": "prospectus", "page_count": 2, "page_dimensions": {},
            "source_count": len(pros_sources), "logical_units": [],
            "chapters": [{"section_id": s["section_id"], "title": s["section_path"][0], "path": " / ".join(s["section_path"]), "pages": [s["page"]], "source_ids": [s["source_id"]]} for s in pros_sources],
            "pages": [{"page": s["page"], "sources": [s]} for s in pros_sources],
        },
    }
    documents = {
        "product_summary": {"filename": "summary.pdf", "content": b"", "metadata": {"compiled_date": "2026-02-10", "compiled_date_quote": "2026年2月10日"}},
        "periodic_report": {"filename": "report.pdf", "content": b"", "metadata": {"sent_date": "2026-01-20", "sent_date_quote": "2026年1月20日", "report_subtype": "quarterly_report"}},
        "prospectus": {"filename": "prospectus.pdf", "content": b"", "metadata": {"version_date": "2026-01-15", "version_date_quote": "2026年1月15日"}},
    }

    state = {"lookup_active": 0, "lookup_max": 0, "lookup_seen_sections": [], "fallback_active": 0, "fallback_max": 0}

    class ConcurrentMockProvider:
        model_name = "mock-concurrent"

        async def generate_text(self, system_prompt: str, user_prompt: str) -> str:
            payload = json.loads(user_prompt.rsplit("\n", 1)[-1])
            req_items = payload["items"] if isinstance(payload, dict) else payload
            pool = payload.get("context_pool", {}).get("sources", {}) if isinstance(payload, dict) else {}

            def candidate_sources(req: dict, key: str) -> list[dict]:
                ids = req.get(f"allowed_{key}_source_ids")
                if isinstance(ids, list):
                    return [pool[sid] for sid in ids if sid in pool]
                return list(req.get(f"{key}_candidates") or [])

            def find_alias_by_text(req: dict, key: str, substr: str) -> str | None:
                for rec in candidate_sources(req, key):
                    if substr in str(rec.get("raw_text", "")):
                        return rec["source_id"]
                for alias, rec in pool.items():
                    if substr in str(rec.get("raw_text", "")):
                        return alias
                return None

            if "只做路由" in user_prompt:
                return json.dumps({
                    "items": [{"item_id": "custodian", "section_ids": ["sec_0", "sec_1", "sec_2", "sec_3", "sec_4"], "reason": "可能包含托管人名称"}]
                }, ensure_ascii=False)

            if "NOT_FOUND_IN_SCOPE" in user_prompt:
                state["lookup_active"] += 1
                state["lookup_max"] = max(state["lookup_max"], state["lookup_active"])
                await _asyncio.sleep(0.05)
                state["lookup_active"] -= 1
                out = []
                for req in req_items:
                    sec = req["selected_sections"][0]
                    state["lookup_seen_sections"].append(sec)
                    r_id = find_alias_by_text(req, "primary", "中国建设银行")
                    if r_id:
                        rc_count = len(req["execution_plan"]["review_conditions"])
                        out.append({
                            "item_id": "custodian",
                            "summary_value": "中国建设银行股份有限公司",
                            "summary_source_ids": req.get("verified_target", {}).get("source_ids", []),
                            "primary_status": "FOUND",
                            "primary_value": "中国建设银行股份有限公司",
                            "primary_source_ids": [r_id],
                            "applicability_status": "CONFIRMED",
                            "coverage_status": "COMPLETE",
                            "uncovered_aspects": [],
                            "review_condition_assessments": [
                                {"condition_index": i, "status": "CLEAR", "source_ids": [r_id], "metadata_refs": [], "reason": "原文明确载明托管人名称。"}
                                for i in range(rc_count)
                            ],
                            "comparison_status": "MATCH",
                            "reason": "托管人名称一致。",
                        })
                    else:
                        out.append({
                            "item_id": "custodian",
                            "primary_status": "NOT_FOUND_IN_SCOPE",
                            "primary_value": "",
                            "primary_source_ids": [],
                            "applicability_status": "UNRESOLVED",
                            "coverage_status": "COMPLETE",
                            "uncovered_aspects": [],
                            "review_condition_assessments": [],
                            "comparison_status": "UNRESOLVED",
                            "reason": "本分包未提及托管人名称。",
                        })
                return json.dumps({"items": out}, ensure_ascii=False)

            if "在规则声明的补充来源中查找目标事实" in user_prompt:
                state["fallback_active"] += 1
                state["fallback_max"] = max(state["fallback_max"], state["fallback_active"])
                await _asyncio.sleep(0.05)
                state["fallback_active"] -= 1
                req = req_items[0]
                iid = req["item_id"]
                p_alias = candidate_sources(req, "fallback")[0]["source_id"]
                rc_count = len(req["execution_plan"]["review_conditions"])
                if iid == "audit_fee":
                    audit_field = req["execution_plan"]["comparison"]["fields"][0]["field"]
                    return json.dumps({"items": [{
                        "item_id": iid,
                        "status": "FOUND",
                        "applicability_status": "CONFIRMED",
                        "comparison_status": "MATCH",
                        "review_condition_assessments": [
                            {"condition_index": i, "status": "CLEAR", "source_ids": [p_alias], "metadata_refs": ["prospectus.version_date"], "reason": "招募说明书已披露审计费用及适用日期。"}
                            for i in range(rc_count)
                        ],
                        "value": "120000元",
                        "fields": {audit_field: "120000元"},
                        "source_ids": [p_alias],
                        "reason": "双方审计费用金额一致。",
                    }]}, ensure_ascii=False)
                nav_fields = [f["field"] for f in req["execution_plan"]["comparison"]["fields"]]
                return json.dumps({"items": [{
                    "item_id": iid,
                    "status": "FOUND",
                    "applicability_status": "CONFIRMED",
                    "comparison_status": "MATCH",
                    "review_condition_assessments": [
                        {"condition_index": i, "status": "CLEAR", "source_ids": [p_alias], "metadata_refs": ["prospectus.version_date"], "reason": "招募说明书完整披露自然年度收益。"}
                        for i in range(rc_count)
                    ],
                    "value": "2024年基金净值增长率10.50%，业绩比较基准收益率8.20%",
                    "fields": {nav_fields[0]: {"2024": "10.50%"}, nav_fields[1]: {"2024": "8.20%"}},
                    "source_ids": [p_alias],
                    "reason": "2024年双方自然年度收益率与基准收益率一致。",
                }]}, ensure_ascii=False)

            # analyze_group / initial extraction
            out = []
            for req in req_items:
                iid = req["item_id"]
                s_alias = candidate_sources(req, "summary")[0]["source_id"]
                if iid == "custodian":
                    out.append({
                        "item_id": iid,
                        "summary_value": "中国建设银行股份有限公司",
                        "summary_source_ids": [s_alias],
                        "references": [{"role": "periodic_report", "status": "SOURCE_MISSING", "applicability_status": "UNRESOLVED", "coverage_status": "UNKNOWN", "uncovered_aspects": [], "review_condition_assessments": [], "value": "", "source_ids": [], "reason": "初步未定位托管人。"}],
                        "reason": "概要已提取托管人，报告待路由章节。",
                    })
                elif iid == "audit_fee":
                    r_alias = find_alias_by_text(req, "primary", "审计费用")
                    audit_field = req["execution_plan"]["comparison"]["fields"][0]["field"]
                    out.append({
                        "item_id": iid,
                        "summary_value": "120000元",
                        "summary_fields": {audit_field: "120000元"},
                        "summary_source_ids": [s_alias],
                        "references": [{"role": "periodic_report", "status": "UNRESOLVED", "applicability_status": "UNRESOLVED", "coverage_status": "COMPLETE", "uncovered_aspects": [], "review_condition_assessments": [], "value": "本期审计费用与基金费用说明", "source_ids": [r_alias], "reason": "季报未披露年度审计费用明细。"}],
                        "reason": "季报仅列本期费用提示，触发补充来源审核。",
                    })
                elif iid == "annual_nav_performance_chart":
                    r_alias = find_alias_by_text(req, "primary", "过去三个月")
                    nav_fields = [f["field"] for f in req["execution_plan"]["comparison"]["fields"]]
                    out.append({
                        "item_id": iid,
                        "summary_value": "2024年基金净值增长率10.50%，业绩比较基准收益率8.20%",
                        "summary_fields": {nav_fields[0]: {"2024": "10.50%"}, nav_fields[1]: {"2024": "8.20%"}},
                        "summary_source_ids": [s_alias],
                        "references": [{"role": "periodic_report", "status": "UNRESOLVED", "applicability_status": "UNRESOLVED", "coverage_status": "NOT_FOUND", "uncovered_aspects": [], "review_condition_assessments": [], "value": "3.10%", "source_ids": [r_alias], "reason": "季报仅披露滚动区间收益率，缺少自然年度数据。"}],
                        "reason": "季报不含自然年度数据，切换招募说明书补充核对。",
                    })
            return json.dumps({"items": out}, ensure_ascii=False)

    result = _asyncio.run(compare_product_summary(
        documents=documents,
        provider=ConcurrentMockProvider(),
        source_catalogues=catalogues,
        rule_snapshot=mini_snapshot,
    ))
    by_id = {item["item_id"]: item for item in result["items"]}
    assert by_id["custodian"]["status"] == "MATCH"
    assert by_id["audit_fee"]["status"] == "MATCH"
    assert by_id["annual_nav_performance_chart"]["status"] == "MATCH"
    assert result["status"] == "MATCH"
    # Concurrent wave lookup: wave 1 dispatched sec_0..sec_3 in parallel, early-stopped before sec_4, and excluded sec_noise.
    assert state["lookup_max"] >= 2
    assert state["fallback_max"] >= 2
    assert all("重要提示" not in str(sec) for sec in state["lookup_seen_sections"])
    assert all("备查文件" not in str(sec) for sec in state["lookup_seen_sections"])


def test_review_conditions_tolerate_benign_trailing_clear_and_pickup_section_id_ocr():
    from app.summary_check import _source_candidates

    rule = {"execution_plan": {"review_conditions": ["条件0", "条件1"]}}
    allowed = {"s1", "s2"}
    assert _review_conditions_are_clear(rule, {
        "review_condition_assessments": [
            {"condition_index": 0, "status": "CLEAR", "source_ids": ["s1"], "reason": "条件0满足"},
            {"condition_index": 1, "status": "NOT_APPLICABLE", "source_ids": [], "reason": "条件1不适用"},
            {"condition_index": 2, "status": "CLEAR", "source_ids": ["s2"], "reason": "额外冗余条件也通过"},
        ],
    }, allowed, allowed)
    assert not _review_conditions_are_clear(rule, {
        "review_condition_assessments": [
            {"condition_index": 0, "status": "CLEAR", "source_ids": ["s1"], "reason": "条件0满足"},
            {"condition_index": 1, "status": "CLEAR", "source_ids": ["s2"], "reason": "条件1满足"},
            {"condition_index": 2, "status": "REVIEW_REQUIRED", "source_ids": ["s2"], "reason": "发现异常风险"},
        ],
    }, allowed, allowed)

    # Cached catalogues that missed an image_ocr source_id inside chapters[*].source_ids still include it via section_id.
    catalogue = {
        "role": "product_summary",
        "chapters": [{
            "section_id": "sec_alloc",
            "title": "(二) 投资组合资产配置图表/区域配置图表",
            "path": "二、基金投资与净值表现 / (二) 投资组合资产配置图表/区域配置图表",
            "pages": [2],
            "source_ids": ["txt_title"],
        }],
        "pages": [{
            "page": 2,
            "sources": [
                {"source_id": "txt_title", "section_id": "sec_alloc", "raw_text": "投资组合资产配置图表"},
                {"source_id": "img_ocr", "section_id": "sec_alloc", "source_kind": "image_ocr", "raw_text": "| 权益投资 | 99.13% |"},
            ],
        }],
    }
    alloc_item = {
        "label": "投资组合资产配置图表",
        "execution_plan": {
            "target_role": "product_summary",
            "search_intent": {
                "target_description": "资产配置比例",
                "preferred_sections": ["(二) 投资组合资产配置图表/区域配置图表"],
                "preferred_sections_by_role": {"product_summary": ["(二) 投资组合资产配置图表/区域配置图表"]},
            },
        },
    }
    picked_ids = [source["source_id"] for source in _source_candidates(catalogue, alloc_item)]
    assert picked_ids == ["txt_title", "img_ocr"]


def test_compare_by_execution_plan_normalizes_declared_percent_decimals():
    pct_rule = {
        "execution_plan": {
            "comparison": {
                "fields": [
                    {"field": "data_cutoff_date", "label": "数据截止日期", "operation": "date", "required": True},
                    {
                        "field": "percentage",
                        "label": "占基金总资产的比例（%）",
                        "operation": "decimal",
                        "required": True,
                        "source_headers": ["占基金总资产的比例（%）"],
                    },
                ]
            }
        }
    }
    assert (
        _compare_by_execution_plan(
            pct_rule,
            "2025-12-31；权益投资 99.13%",
            "2025年12月31日；权益投资 99.13",
            "MATCH",
            target_fields={"data_cutoff_date": "2025-12-31", "percentage": {"权益投资": "99.13%"}},
            reference_fields={"data_cutoff_date": "2025-12-31", "percentage": {"权益投资": "99.13"}},
        )
        == "MATCH"
    )
    assert (
        _compare_by_execution_plan(
            pct_rule,
            "2025-12-31；权益投资 99.13%",
            "2025年12月31日；权益投资 99.14",
            "MISMATCH",
            target_fields={"data_cutoff_date": "2025-12-31", "percentage": {"权益投资": "99.13%"}},
            reference_fields={"data_cutoff_date": "2025-12-31", "percentage": {"权益投资": "99.14"}},
        )
        == "MISMATCH"
    )
    non_pct_rule = {
        "execution_plan": {
            "comparison": {
                "fields": [
                    {"field": "fee_amount", "label": "金额", "operation": "decimal", "required": True},
                ]
            }
        }
    }
    assert (
        _compare_by_execution_plan(
            non_pct_rule,
            "99.13%",
            "99.13",
            "MATCH",
            target_fields={"fee_amount": "99.13%"},
            reference_fields={"fee_amount": "99.13"},
        )
        == "UNRESOLVED"
    )


def test_unresolved_fallback_preserves_verified_evidence_and_reason():
    import asyncio as _asyncio
    from app.summary_check import capture_product_summary_rule_snapshot, compare_product_summary

    snapshot = capture_product_summary_rule_snapshot()
    sub_rules = [rule for rule in snapshot["rules"] if rule["item_id"] == "annual_nav_performance_chart"]
    mini_snapshot = {**snapshot, "rules": sub_rules}

    def make_source(role: str, sid: str, sec_id: str, sec_title: str, page: int, text: str) -> dict:
        return {
            "source_id": sid,
            "document_role": role,
            "page": page,
            "section_id": sec_id,
            "section_path": [sec_title],
            "raw_text": text,
            "row_text": text,
            "cell_text": text,
        }

    sum_source = {
        **make_source(
            "product_summary", "s_nav", "sum_nav",
            "(三) 自基金合同生效以来基金每年的净值增长率及与同期业绩比较基准的比较",
            2, "2024年基金净值增长率10.50%，业绩比较基准收益率8.20%",
        ),
        "cell_text": "基金净值增长率",
    }
    report_source = make_source(
        "periodic_report", "r_nav", "sec_nav", "§3.2 基金净值表现", 4,
        "过去三个月净值增长率2.22%，不含自然年度数据",
    )
    pros_source = make_source(
        "prospectus", "p_nav", "p_perf", "十二、基金的业绩", 59,
        "2024年基金净值增长率10.50%，业绩比较基准收益率8.20%",
    )
    catalogues = {
        "product_summary": {
            "filename": "summary.pdf", "role": "product_summary", "page_count": 2, "page_dimensions": {},
            "source_count": 1, "logical_units": [],
            "chapters": [{"section_id": "sum_nav", "title": "(三) 自基金合同生效以来基金每年的净值增长率及与同期业绩比较基准的比较", "path": "基金的业绩", "pages": [2], "source_ids": ["s_nav"]}],
            "pages": [{"page": 2, "sources": [sum_source]}],
        },
        "periodic_report": {
            "filename": "report.pdf", "role": "periodic_report", "page_count": 4, "page_dimensions": {},
            "source_count": 1, "logical_units": [],
            "chapters": [{"section_id": "sec_nav", "title": "§3.2 基金净值表现", "path": "§3.2 基金净值表现", "pages": [4], "source_ids": ["r_nav"]}],
            "pages": [{"page": 4, "sources": [report_source]}],
        },
        "prospectus": {
            "filename": "prospectus.pdf", "role": "prospectus", "page_count": 60, "page_dimensions": {},
            "source_count": 1, "logical_units": [],
            "chapters": [{"section_id": "p_perf", "title": "十二、基金的业绩", "path": "十二、基金的业绩", "pages": [59], "source_ids": ["p_nav"]}],
            "pages": [{"page": 59, "sources": [pros_source]}],
        },
    }
    documents = {
        "product_summary": {"filename": "summary.pdf", "content": b"", "metadata": {"compiled_date": "2026-01-26"}},
        "periodic_report": {"filename": "report.pdf", "content": b"", "metadata": {"sent_date": "2026-01-22", "report_subtype": "quarterly"}},
        "prospectus": {"filename": "prospectus.pdf", "content": b"", "metadata": {"date_interval": {"start": "2026-01-01", "end": "2026-01-31"}, "date_label": "招募说明书封面日期"}},
    }

    class UnresolvedFallbackProvider:
        model_name = "mock-fallback-unresolved"

        async def generate_text(self, system_prompt: str, user_prompt: str) -> str:
            payload = json.loads(user_prompt.rsplit("\n", 1)[-1])
            req_items = payload["items"] if isinstance(payload, dict) else payload
            pool = payload.get("context_pool", {}).get("sources", {}) if isinstance(payload, dict) else {}
            req = req_items[0]

            def candidate_sources(key: str) -> list[dict]:
                ids = req.get(f"allowed_{key}_source_ids")
                if isinstance(ids, list):
                    return [pool[sid] for sid in ids if sid in pool]
                return list(req.get(f"{key}_candidates") or [])

            nav_fields = [f["field"] for f in req["execution_plan"]["comparison"]["fields"]]
            if "在规则声明的补充来源中查找目标事实" in user_prompt:
                p_alias = candidate_sources("fallback")[0]["source_id"]
                return json.dumps({"items": [{
                    "item_id": "annual_nav_performance_chart",
                    "status": "UNRESOLVED",
                    "applicability_status": "UNRESOLVED",
                    "comparison_status": "UNRESOLVED",
                    "review_condition_assessments": [
                        {"condition_index": i, "status": "CLEAR", "source_ids": [p_alias], "reason": "条件满足"}
                        for i in range(len(req["execution_plan"]["review_conditions"]))
                    ],
                    "value": "2024年基金净值增长率10.50%，业绩比较基准收益率8.20%",
                    "fields": {nav_fields[0]: {"2024": "10.50%"}, nav_fields[1]: {"2024": "8.20%"}},
                    "source_ids": [p_alias],
                    "reason": "已在补充招募说明书取得2024年一致数据，但封面日期仅精确到2026年1月，无法确认版本日期不晚于2026年1月26日。",
                }]}, ensure_ascii=False)

            s_alias = candidate_sources("summary")[0]["source_id"]
            r_alias = candidate_sources("primary")[0]["source_id"]
            return json.dumps({"items": [{
                "item_id": "annual_nav_performance_chart",
                "summary_value": "2024年基金净值增长率10.50%，业绩比较基准收益率8.20%",
                "summary_fields": {nav_fields[0]: {"2024": "10.50%"}, nav_fields[1]: {"2024": "8.20%"}},
                "summary_source_ids": [s_alias],
                "references": [{
                    "role": "periodic_report", "status": "UNRESOLVED", "applicability_status": "CONFIRMED",
                    "coverage_status": "NOT_FOUND", "uncovered_aspects": [], "review_condition_assessments": [],
                    "value": "2.22%", "source_ids": [r_alias],
                    "reason": "季度报告不含自然年度拆分数据。本组未提供可核验的补充招募说明书，暂不能判断年度数据是否一致。",
                }],
                "reason": "季度报告不含自然年度拆分数据。",
            }]}, ensure_ascii=False)

    result = _asyncio.run(compare_product_summary(
        documents=documents,
        provider=UnresolvedFallbackProvider(),
        source_catalogues=catalogues,
        rule_snapshot=mini_snapshot,
    ))
    item = result["items"][0]
    assert item["status"] == "UNRESOLVED"
    assert item["selected_reference"]["role"] == "prospectus"
    assert item["reference"]["role"] == "prospectus"
    assert [src["source_id"] for src in item["references"]["prospectus"]["evidence"]] == ["p_nav"]
    assert "10.50%" in (item["references"]["prospectus"]["raw_text"] or "")
    assert [src["source_id"] for src in item["references"]["periodic_report"]["evidence"]] == ["r_nav"]
    assert "补充招募说明书" in item["reason"]
    assert "未提供可核验的补充招募说明书" not in item["reason"]


def test_compiled_exchange_names_plan_accepts_merged_periodic_short_names():
    _, rules = load_markdown_rules(require_execution_plans=True)
    rule = next(item for item in rules if item["item_id"] == "exchange_names")
    fields = rule["execution_plan"]["comparison"]["fields"]
    assert len(fields) == 1
    assert fields[0]["field"] == "exchange_names"
    assert fields[0]["operation"] == "semantic"
    assert fields[0]["required"] is True

    periodic_checks = rule["execution_plan"]["comparison"]["context_checks_by_reference_role"]["periodic_report"]
    assert any(check.get("operation") == "reference_before_or_equal" for check in periodic_checks)

    assert _compare_by_execution_plan(
        rule,
        "场内简称：SZ50ETF；扩位证券简称：上证 50ETF 易方达",
        "场内简称：SZ50ETF、上证 50ETF 易方达",
        "MATCH",
        target_fields={"exchange_names": "场内简称：SZ50ETF；扩位证券简称：上证 50ETF 易方达"},
        reference_fields={"exchange_names": "场内简称：SZ50ETF、上证 50ETF 易方达"},
    ) == "MATCH"
    assert _compare_by_execution_plan(
        rule,
        "场内简称：SZ50ETF；扩位证券简称：上证 50ETF 易方达",
        "场内简称：SZ50ETF",
        "UNRESOLVED",
        target_fields={"exchange_names": "场内简称：SZ50ETF；扩位证券简称：上证 50ETF 易方达"},
        reference_fields={"exchange_names": "场内简称：SZ50ETF"},
    ) == "UNRESOLVED"
    assert _compare_by_execution_plan(
        rule,
        "场内简称：SZ50ETF；扩位证券简称：上证 50ETF 易方达",
        "场内简称：SZ50ETF、沪深 300ETF",
        "MISMATCH",
        target_fields={"exchange_names": "场内简称：SZ50ETF；扩位证券简称：上证 50ETF 易方达"},
        reference_fields={"exchange_names": "场内简称：SZ50ETF、沪深 300ETF"},
    ) == "MISMATCH"


def test_compiled_annual_nav_performance_chart_marks_extra_right_years_unresolved_with_reason():
    from app.summary_check import _structured_extra_reference_keys

    _, rules = load_markdown_rules(require_execution_plans=True)
    rule = next(item for item in rules if item["item_id"] == "annual_nav_performance_chart")
    assert rule["execution_plan"]["row_matching"]["reference_superset_allowed"] is False

    target_fields = {
        "nav_growth_rate": {"2019": "6.23%", "2024": "18.66%"},
        "benchmark_return_rate": {"2019": "3.63%", "2024": "15.42%"},
    }
    reference_fields = {
        "nav_growth_rate": {"2019": "6.23%", "2024": "18.66%", "2025": "16.60%"},
        "benchmark_return_rate": {"2019": "3.63%", "2024": "15.42%", "2025": "12.90%"},
    }
    assert _structured_extra_reference_keys(rule, target_fields, reference_fields) == ["2025"]
    assert _compare_by_execution_plan(
        rule,
        "2019: 6.23%, 3.63%; 2024: 18.66%, 15.42%",
        "2019: 6.23%, 3.63%; 2024: 18.66%, 15.42%; 2025: 16.60%, 12.90%",
        "MATCH",
        target_fields=target_fields,
        reference_fields=reference_fields,
    ) == "UNRESOLVED"


def test_compare_product_summary_flags_extra_years_and_cross_chart_cutoff_year_rules():
    snapshot = capture_product_summary_rule_snapshot()
    sub_rules = [
        rule for rule in snapshot["rules"]
        if rule["item_id"] in {"asset_allocation_chart", "annual_nav_performance_chart"}
    ]
    mini_snapshot = {**snapshot, "rules": sub_rules}
    asset_rule = next(rule for rule in sub_rules if rule["item_id"] == "asset_allocation_chart")
    asset_field_names = [f["field"] for f in asset_rule["execution_plan"]["comparison"]["fields"]]

    def make_source(role: str, sid: str, sec_id: str, sec_title: str, page: int, text: str, kind: str = "text_line") -> dict:
        return {
            "source_id": sid,
            "document_role": role,
            "source_kind": kind,
            "page": page,
            "section_id": sec_id,
            "section_path": [sec_title],
            "raw_text": text,
            "row_text": text,
            "cell_text": text,
        }

    def run_chart_scenario(
        *,
        upper_cutoff: str,
        lower_cutoff: str,
        ref_extra_2025: bool,
    ) -> dict:
        sum_sources = [
            make_source(
                "product_summary",
                "s_alloc",
                "sum_alloc",
                "(二) 投资组合资产配置图表/区域配置图表",
                2,
                f"### 投资组合资产配置图表\n数据截止日期：{upper_cutoff}\n| 资产类别 | 占比 |\n| 权益投资 | 99.13% |",
                kind="image_ocr",
            ),
            {
                **make_source(
                    "product_summary",
                    "s_nav",
                    "sum_nav",
                    "(三) 自基金合同生效以来基金每年的净值增长率及与同期业绩比较基准的比较",
                    2,
                    f"### 自基金合同生效以来基金每年的净值增长率及与同期业绩比较基准的比较图\n数据截止日期：{lower_cutoff}\n| 年份 | 净值增长率 | 业绩比较基准收益率 |\n| 2024 | 18.66% | 15.42% |",
                    kind="image_ocr",
                ),
                "cell_text": "基金净值增长率",
            },
        ]
        report_sources = [
            make_source(
                "periodic_report",
                "r_alloc",
                "rep_alloc",
                "5.1 报告期末基金资产组合情况",
                7,
                f"报告期末（{upper_cutoff}）权益投资占基金总资产比例 99.13%",
            ),
            make_source(
                "periodic_report",
                "r_nav",
                "rep_nav",
                "基金净值表现",
                5,
                "2024年 净值增长率 18.66%，业绩比较基准收益率 15.42%" + ("；2025年 净值增长率 16.60%，业绩比较基准收益率 12.90%" if ref_extra_2025 else ""),
            ),
        ]
        catalogues = {
            "product_summary": {
                "filename": "summary.pdf", "role": "product_summary", "page_count": 2, "page_dimensions": {},
                "source_count": len(sum_sources), "logical_units": [],
                "chapters": [{"section_id": s["section_id"], "title": s["section_path"][0], "path": " / ".join(s["section_path"]), "pages": [2], "source_ids": [s["source_id"]]} for s in sum_sources],
                "pages": [{"page": 2, "sources": sum_sources}],
            },
            "periodic_report": {
                "filename": "report.pdf", "role": "periodic_report", "page_count": 8, "page_dimensions": {},
                "source_count": len(report_sources), "logical_units": [],
                "chapters": [{"section_id": s["section_id"], "title": s["section_path"][0], "path": " / ".join(s["section_path"]), "pages": [s["page"]], "source_ids": [s["source_id"]]} for s in report_sources],
                "pages": [{"page": s["page"], "sources": [s]} for s in report_sources],
            },
        }
        documents = {
            "product_summary": {"filename": "summary.pdf", "content": b"", "metadata": {"compiled_date": "2026-04-20"}},
            "periodic_report": {"filename": "report.pdf", "content": b"", "metadata": {"sent_date": "2026-04-15", "period_end": upper_cutoff}},
        }

        def build_asset_fields(pct: str) -> dict:
            out_fields = {}
            for name in asset_field_names:
                if name in {"data_cutoff_date", "date"}:
                    out_fields[name] = upper_cutoff
                else:
                    out_fields[name] = {"权益投资": pct}
            return out_fields

        class ScenarioProvider:
            async def generate_text(self, system_prompt: str, user_prompt: str) -> str:
                req_payload = json.loads(user_prompt.rsplit("\n", 1)[-1])
                req_items = req_payload.get("items", []) if isinstance(req_payload, dict) else req_payload
                pool = req_payload.get("context_pool", {}).get("sources", {}) if isinstance(req_payload, dict) else {}

                def candidate(req: dict, side: str, marker: str = "") -> str:
                    ids = req.get(f"allowed_{side}_source_ids")
                    recs = [pool[sid] for sid in ids if sid in pool] if isinstance(ids, list) else list(req.get(f"{side}_records") or req.get(f"{side}_candidates") or req.get("sources") or [])
                    if marker:
                        for rec in recs:
                            if marker in str(rec.get("raw_text", "")):
                                return rec["source_id"]
                        for sid, rec in pool.items():
                            if marker in str(rec.get("raw_text", "")):
                                return sid
                    if recs:
                        return recs[0]["source_id"]
                    if pool:
                        return next(iter(pool))
                    return ((req.get("verified_target") or {}).get("source_ids") or [""])[0]

                if "仅在当前批次所属子章节内查找" in system_prompt:
                    out = []
                    for req in req_items:
                        iid = req["item_id"]
                        r_id = candidate(req, "primary", "基金总资产比例" if iid == "asset_allocation_chart" else "2024年")
                        rc_count = len(req["execution_plan"]["review_conditions"])
                        out.append({
                            "item_id": iid,
                            "summary_source_ids": req.get("verified_target", {}).get("source_ids", []),
                            "primary_status": "FOUND",
                            "primary_value": "99.13%" if iid == "asset_allocation_chart" else "2024: 18.66%, 15.42%",
                            "primary_source_ids": [r_id],
                            "applicability_status": "CONFIRMED",
                            "coverage_status": "COMPLETE",
                            "uncovered_aspects": [],
                            "review_condition_assessments": [
                                {"condition_index": i, "status": "CLEAR", "source_ids": [r_id], "metadata_refs": [], "reason": "条件满足。"}
                                for i in range(rc_count)
                            ],
                            "comparison_status": "MATCH",
                            "reason": "一致。",
                        })
                    return json.dumps({"items": out}, ensure_ascii=False)

                out = []
                for req in req_items:
                    iid = req["item_id"]
                    s_id = candidate(req, "summary", "资产配置" if iid == "asset_allocation_chart" else "净值增长率")
                    r_id = candidate(req, "primary", "基金总资产比例" if iid == "asset_allocation_chart" else "2024年")
                    rc_count = len(req["execution_plan"]["review_conditions"])
                    if iid == "asset_allocation_chart":
                        out.append({
                            "item_id": iid,
                            "summary_value": f"数据截止日期：{upper_cutoff}；权益投资99.13%",
                            "summary_fields": build_asset_fields("99.13%"),
                            "summary_source_ids": [s_id],
                            "references": [{
                                "role": "periodic_report",
                                "status": "MATCH",
                                "applicability_status": "CONFIRMED",
                                "coverage_status": "COMPLETE",
                                "uncovered_aspects": [],
                                "review_condition_assessments": [
                                    {"condition_index": i, "status": "CLEAR", "source_ids": [r_id], "metadata_refs": [], "reason": "条件满足。"}
                                    for i in range(rc_count)
                                ],
                                "value": f"数据截止日期：{upper_cutoff}；权益投资99.13%",
                                "fields": build_asset_fields("99.13%"),
                                "source_ids": [r_id],
                                "reason": "资产配置比例一致。",
                            }],
                            "reason": "资产配置一致。",
                        })
                    else:
                        nav_fields = [f["field"] for f in req["execution_plan"]["comparison"]["fields"]]
                        nav_ref = {"2024": "18.66%"}
                        bench_ref = {"2024": "15.42%"}
                        if ref_extra_2025:
                            nav_ref["2025"] = "16.60%"
                            bench_ref["2025"] = "12.90%"
                        out.append({
                            "item_id": iid,
                            "summary_value": "2024: 18.66%, 15.42%",
                            "summary_fields": {nav_fields[0]: {"2024": "18.66%"}, nav_fields[1]: {"2024": "15.42%"}},
                            "summary_source_ids": [s_id],
                            "references": [{
                                "role": "periodic_report",
                                "status": "MATCH",
                                "applicability_status": "CONFIRMED",
                                "coverage_status": "COMPLETE",
                                "uncovered_aspects": [],
                                "review_condition_assessments": [
                                    {"condition_index": i, "status": "CLEAR", "source_ids": [r_id], "metadata_refs": [], "reason": "条件满足。"}
                                    for i in range(rc_count)
                                ],
                                "value": "2024: 18.66%, 15.42%" + ("；2025: 16.60%, 12.90%" if ref_extra_2025 else ""),
                                "fields": {nav_fields[0]: nav_ref, nav_fields[1]: bench_ref},
                                "source_ids": [r_id],
                                "reason": "共同年份一致；参考报告额外披露的2025年不纳入比较。" if ref_extra_2025 else "年度收益一致。",
                            }],
                            "reason": "年度收益已核对。",
                        })
                return json.dumps({"items": out}, ensure_ascii=False)

        return _asyncio.run(compare_product_summary(
            documents=documents,
            provider=ScenarioProvider(),
            source_catalogues=catalogues,
            rule_snapshot=mini_snapshot,
        ))

    # Scenario 1: extra 2025 on reference + 2025-12-31 vs 2024-12-31 cutoff mismatch -> BOTH UNRESOLVED with explanations.
    res_mismatch = run_chart_scenario(upper_cutoff="2025-12-31", lower_cutoff="2024-12-31", ref_extra_2025=True)
    by_id_bad = {item["item_id"]: item for item in res_mismatch["items"]}
    assert by_id_bad["asset_allocation_chart"]["status"] == "UNRESOLVED"
    assert by_id_bad["annual_nav_performance_chart"]["status"] == "UNRESOLVED"
    assert "2025-12-31" in by_id_bad["asset_allocation_chart"]["reason"]
    assert "2024-12-31" in by_id_bad["asset_allocation_chart"]["reason"]
    assert "2025" in by_id_bad["annual_nav_performance_chart"]["reason"]
    assert "不纳入比较" not in by_id_bad["annual_nav_performance_chart"]["reason"]
    assert res_mismatch["status"] == "UNRESOLVED"
    assert res_mismatch["unresolved_count"] == 2
    assert res_mismatch["matched_count"] == 0

    # Scenario 2: upper=2026-03-31 (nearest year-end 2025-12-31) vs lower=2025-12-31 -> allowed, both MATCH!
    res_q1_valid = run_chart_scenario(upper_cutoff="2026-03-31", lower_cutoff="2025-12-31", ref_extra_2025=False)
    assert all(item["status"] == "MATCH" for item in res_q1_valid["items"])
    assert res_q1_valid["status"] == "MATCH"

    # Scenario 3: upper=2025-09-30 (nearest year-end 2024-12-31) vs lower=2024-12-31 -> allowed, both MATCH!
    res_q3_valid = run_chart_scenario(upper_cutoff="2025-09-30", lower_cutoff="2024-12-31", ref_extra_2025=False)
    assert all(item["status"] == "MATCH" for item in res_q3_valid["items"])
    assert res_q3_valid["status"] == "MATCH"


def test_asset_allocation_equivalence_and_subitem_filtering():
    from app.summary_check import _compare_by_execution_plan, _structured_extra_reference_keys

    rule = {
        "item_id": "asset_allocation_chart",
        "execution_plan": {
            "row_matching": {
                "hierarchy_fields": ["parent_key"],
                "identity_prefixes_to_ignore": ["其中："],
                "identity_equivalence_groups": [
                    {"canonical_identity": "其他资产", "equivalent_labels": ["其他资产", "其他各项资产"]},
                ],
                "reference_superset_allowed": False,
            },
            "comparison": {
                "fields": [
                    {"field": "data_cutoff_date", "label": "截止日期", "operation": "date", "required": True},
                    {"field": "total_assets_percentage", "label": "占比", "operation": "decimal", "required": True},
                ],
            },
        },
    }
    target_fields = {
        "data_cutoff_date": "2026-06-30",
        "total_assets_percentage": {"权益投资": "90.75%", "银行存款和结算备付金合计": "8.49%", "其他资产": "0.77%"},
    }
    reference_fields = {
        "data_cutoff_date": "2026-06-30",
        "total_assets_percentage": {
            "权益投资": "90.75",
            "其中：普通股": "89.24",
            "存托凭证": "1.51",
            "银行存款和结算备付金合计": "8.49",
            "其他各项资产": "0.77",
        },
    }
    assert _structured_extra_reference_keys(rule, target_fields, reference_fields) == []
    status = _compare_by_execution_plan(
        rule, "90.75%", "90.75", "MATCH", "CONFIRMED", True, True,
        target_fields=target_fields, reference_fields=reference_fields,
    )
    assert status == "MATCH"
