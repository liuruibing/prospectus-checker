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
    assert len(rules) == 16
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
