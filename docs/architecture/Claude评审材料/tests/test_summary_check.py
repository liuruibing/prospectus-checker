import asyncio
import json
from pathlib import Path

import pymupdf as fitz

from app.summary_check import (
    RULES, RULE_VERSION, _fallback_source_date_status, _fallback_trigger_reason,
    _field_evidence_is_substantive, _markdown_table_context,
    _numbers_are_grounded, _can_compare_numeric_deterministically, _compare_code_sets, _normalize_model_item_id, _source_candidates,
    build_document_source_catalogue, build_source_catalogue, compare_product_summary,
)
from app.summary_rules import module_groups


def _pdf_bytes(text: str) -> bytes:
    document = fitz.open()
    page = document.new_page(width=600, height=1000)
    for index, line in enumerate(text.splitlines()):
        page.insert_text((40, 40 + index * 14), line, fontname="china-s", fontsize=10)
    data = document.tobytes()
    document.close()
    return data


def _pdf_bytes_by_pages(*pages: str) -> bytes:
    document = fitz.open()
    for page_text in pages:
        page = document.new_page(width=600, height=1000)
        for index, line in enumerate(page_text.splitlines()):
            page.insert_text((40, 40 + index * 14), line, fontname="china-s", fontsize=10)
    data = document.tobytes()
    document.close()
    return data


def _docs(summary: str, report: str, prospectus: str | None = None):
    docs = {
        "product_summary": {"filename": "概要.pdf", "content": _pdf_bytes(summary), "metadata": {"compiled_date": "2026-01-26"}},
        "periodic_report": {"filename": "定期报告.pdf", "content": _pdf_bytes(report), "metadata": {"sent_date": "2026-01-22", "report_subtype": "quarterly"}},
    }
    if prospectus is not None:
        docs["prospectus"] = {"filename": "招募说明书.pdf", "content": _pdf_bytes(prospectus)}
    return docs


class MatchProvider:
    async def generate_text(self, system, prompt):
        pack = json.loads(prompt.split("字段与证据包：\n", 1)[1])
        assert all("item_id" in item and "excel_row" not in item for item in pack)
        result = []
        for item in pack:
            target = next(source for source in item["summary_candidates"] if item["label"] in source["raw_text"])
            reference = next(source for source in item["primary_candidates"] if item["label"] in source["raw_text"])
            result.append({
                "item_id": item["item_id"],
                "summary_value": target["raw_text"], "summary_source_ids": [target["source_id"]],
                "references": [{"role": item["primary_role"], "status": "MATCH" if target["raw_text"] == reference["raw_text"] else "MISMATCH", "value": reference["raw_text"], "source_ids": [reference["source_id"]]}],
                "reason": "测试证据一致",
            })
        return json.dumps({"items": result}, ensure_ascii=False)


def test_markdown_rules_define_periodic_report_as_primary_and_scoped_prospectus_fallback():
    assert RULE_VERSION == "product-summary-md-v1"
    assert {rule["excel_row"] for rule in RULES} == set(range(1, 17))
    assert len({rule["item_id"] for rule in RULES}) == 16
    assert all(rule["primary_source"]["role"] == "periodic_report" for rule in RULES)
    assert next(rule for rule in RULES if rule["key"] == "manager")["fallback_source"]["role"] == "prospectus"
    assert next(rule for rule in RULES if rule["key"] == "fund_code").get("fallback_source") is None


def test_rule_declared_same_contract_annual_rate_fallback_requires_no_verified_primary_conclusion():
    rule = next(rule for rule in RULES if rule["key"] == "management_fee")
    conditions = rule["fallback_when"]
    assert _fallback_trigger_reason(rule, conditions, lookup_override="UNRESOLVED",
                                    primary_not_applicable=False, primary_conclusion_verified=False) == "PRIMARY_ANNUAL_RATE_NOT_VERIFIED"
    assert _fallback_trigger_reason(rule, conditions, lookup_override="UNRESOLVED",
                                    primary_not_applicable=False, primary_conclusion_verified=True) is None
    assert _fallback_trigger_reason(rule, conditions, lookup_override="CONFIRMED_ABSENT",
                                    primary_not_applicable=False, primary_conclusion_verified=True) == "PRIMARY_CONFIRMED_ABSENT"


def test_fallback_rate_source_date_must_be_applicable_to_summary_compilation():
    rule = next(rule for rule in RULES if rule["key"] == "management_fee")
    documents = {
        "product_summary": {"metadata": {"compiled_date": "2026-07-24"}},
        "prospectus": {"metadata": {"date_interval": {"start": "2026-07-27", "end": "2026-07-27"}}},
    }
    assert _fallback_source_date_status(rule, documents, "prospectus", historical=True) == (
        "AFTER_TARGET_DATE", "补充文件版本晚于概要编制日期，不能直接作为当时适用依据。"
    )
    documents["prospectus"]["metadata"]["date_interval"] = {"start": "2026-01-01", "end": "2026-01-31"}
    assert _fallback_source_date_status(rule, documents, "prospectus", historical=True)[0] == "ELIGIBLE"


def test_item_id_is_primary_join_key_and_excel_row_is_read_only_compatibility():
    rows = {2: "fund_code"}
    assert _normalize_model_item_id({"item_id": "fund_code", "excel_row": 99}, rows)["item_id"] == "fund_code"
    assert _normalize_model_item_id({"excel_row": 2}, rows)["item_id"] == "fund_code"
    assert _normalize_model_item_id({"excel_row": 99}, rows) is None


def test_rule_modules_drive_grouping_without_excel_row_slices():
    groups = module_groups([
        {"item_id": "new_profile_field", "module_id": "fund_profile", "excel_row": 500},
        {"item_id": "new_fee_field", "module_id": "fees", "excel_row": 1},
    ])
    assert {module: [item["item_id"] for item in items] for module, items in groups.items()} == {
        "fund_profile": ["new_profile_field"], "fees": ["new_fee_field"]
    }


def test_source_catalogue_and_chapter_candidates_are_addressable():
    content = _pdf_bytes("一、产品概况\n基金管理人：易方达基金管理有限公司\n管理费率：每年0.15%")
    catalogue = build_source_catalogue("概要.pdf", content, "product_summary")
    rule = next(rule for rule in RULES if rule["key"] == "management_fee")
    candidates = _source_candidates(catalogue, rule)
    assert catalogue["source_count"] >= 2
    assert all(source["bbox"][0] < source["bbox"][2] for source in candidates)
    assert any("管理费率" in source["raw_text"] for source in candidates)
    assert all(source["section_path"] == ["一、产品概况"] for source in candidates)
    chapter = next(chapter for chapter in catalogue["chapters"] if chapter["path"] == "一、产品概况")
    assert chapter["source_ids"]
    assert chapter["heading_source_ids"]
    assert chapter["pages"] == [1]
    assert all(source["section_id"] == chapter["section_id"] for source in candidates)


def test_candidate_search_uses_reconstructed_row_text_without_indicator_special_cases():
    catalogue = {"pages": [{"page": 1, "sources": [
        {"source_id": "label", "page": 1, "bbox": [1, 1, 30, 10], "section_path": [],
         "raw_text": "基金管理", "row_text": "基金管理 | 人 | 甲基金管理公司"},
        {"source_id": "value", "page": 1, "bbox": [31, 1, 60, 10], "section_path": [],
         "raw_text": "甲基金管理公司", "row_text": "基金管理 | 人 | 甲基金管理公司"},
    ]}]}
    rule = next(rule for rule in RULES if rule["item_id"] == "manager")
    assert {source["source_id"] for source in _source_candidates(catalogue, rule)} == {"label", "value"}


def test_anydoc_table_structure_is_available_as_context_but_keeps_pdf_source_ids():
    path = "assets/易方达上证50交易型开放式指数证券投资基金基金产品资料概要更新.pdf"
    catalogue = build_document_source_catalogue(path.rsplit("/", 1)[-1], open(path, "rb").read(), "product_summary")
    rule = next(rule for rule in RULES if rule["key"] == "fund_code")
    table_contexts = _markdown_table_context(catalogue, rule)
    assert catalogue["reader_context"]["provider"] == "AnyDoc"
    assert catalogue["reader_context"]["status"] == "READY"
    assert any("510100" in context for context in table_contexts)
    assert all("source_id" in source for page in catalogue["pages"] for source in page["sources"])
    source = next(source for page in catalogue["pages"] for source in page["sources"]
                  if source["raw_text"].strip() == "510100")
    assert source["source_kind"] == "text_line"
    assert source["document_role"] == "product_summary"
    assert len(source["source_id"]) == 24
    assert source["bbox"] and len(source["bbox"]) == 4
    assert source["layout_context"]
    assert all("source_id" in line for line in source["layout_context"])


def test_same_table_layout_context_links_split_manager_date_label_and_name():
    path = "assets/华夏成长证券投资基金/2026-07-27_产品资料概要更新_A份额.pdf"
    catalogue = build_source_catalogue(Path(path).name, Path(path).read_bytes(), "product_summary")
    start_date = next(source for source in catalogue["pages"][0]["sources"]
                      if source["raw_text"] == "2024-12-26")
    nearby_text = " ".join(line["raw_text"] for line in start_date["layout_context"])
    assert "开始担任本基金" in nearby_text
    assert "刘睿聪" in nearby_text


def test_numeric_codes_and_dates_do_not_pollute_section_hierarchy():
    catalogue = build_source_catalogue(
        "概要.pdf", _pdf_bytes("一、产品概况\n510100\n2019-09-06\n2025 年报\n1. 投资范围"), "product_summary"
    )
    source_paths = {
        source["raw_text"]: source["section_path"]
        for page in catalogue["pages"] for source in page["sources"]
    }
    assert source_paths["510100"] == ["一、产品概况"]
    assert source_paths["2019-09-06"] == ["一、产品概况"]
    assert source_paths["2025 年报"] == ["一、产品概况"]
    assert source_paths["1. 投资范围"] == ["一、产品概况", "1. 投资范围"]


def test_body_paragraph_starting_with_quarter_number_is_not_a_heading():
    catalogue = build_source_catalogue(
        "季度报告.pdf", _pdf_bytes("2季度，全球经济在动荡中分化加剧。\n§1 重要提示"), "periodic_report"
    )
    source = next(source for source in catalogue["pages"][0]["sources"]
                  if source["raw_text"].startswith("2季度"))
    assert source["section_path"] == []


def test_numbered_table_row_sharing_a_baseline_is_not_a_section_heading():
    document = fitz.open()
    page = document.new_page(width=600, height=1000)
    page.insert_text((40, 40), "一、投资组合", fontname="china-s", fontsize=12)
    page.insert_text((40, 100), "1. 贵州茅台", fontname="china-s", fontsize=10)
    page.insert_text((360, 100), "500.00", fontname="china-s", fontsize=10)
    data = document.tobytes()
    document.close()
    catalogue = build_source_catalogue("报告.pdf", data, "periodic_report")
    row = next(source for source in catalogue["pages"][0]["sources"] if source["raw_text"] == "1. 贵州茅台")
    assert len(row["row_source_ids"]) == 2
    assert row["section_path"] == ["一、投资组合"]


def test_section_hint_recovers_manager_table_when_header_cells_are_fragmented():
    content = _pdf_bytes("4.1 基金经理(或基金经理小组)简介\n1. 基金经理明细\n姓名\n任职日期\n余海燕\n2019-09-06")
    catalogue = build_source_catalogue("定期报告.pdf", content, "periodic_report")
    rule = next(rule for rule in RULES if rule["key"] == "manager_start_dates")
    candidates = _source_candidates(catalogue, rule)
    assert {source["raw_text"] for source in candidates} >= {"1. 基金经理明细", "任职日期", "余海燕", "2019-09-06"}


def test_section_candidates_span_more_than_one_following_page_without_fixed_clipping():
    content = _pdf_bytes_by_pages(
        "4.1 基金经理(或基金经理小组)简介\n经理信息", "基金经理任职经历", "余海燕\n任职日期：2024-01-01", "5. 费用信息\n管理费率：0.15%"
    )
    catalogue = build_source_catalogue("定期报告.pdf", content, "periodic_report")
    rule = next(rule for rule in RULES if rule["key"] == "fund_managers")
    candidates = _source_candidates(catalogue, rule)
    assert {source["page"] for source in candidates} >= {1, 2, 3}
    assert not any(source["page"] == 4 for source in candidates)
    assert any(source["raw_text"] == "余海燕" for source in candidates)


def test_vertical_short_chinese_fragments_keep_original_ids_and_expose_cell_text():
    catalogue = build_source_catalogue("概要.pdf", _pdf_bytes("余\n海\n燕"), "product_summary")
    fragments = [source for source in catalogue["pages"][0]["sources"] if source["raw_text"] in {"余", "海", "燕"}]
    assert len(fragments) == 3
    assert {source["cell_text"] for source in fragments} == {"余海燕"}
    assert len({source["source_id"] for source in fragments}) == 3


def test_short_fund_code_and_manager_name_are_valid_field_evidence():
    code_rule = next(rule for rule in RULES if rule["key"] == "fund_code")
    manager_rule = next(rule for rule in RULES if rule["key"] == "fund_managers")
    code_evidence = [{"raw_text": "基金代码"}, {"raw_text": "510100"}]
    manager_evidence = [{"raw_text": "基金经理"}, {"raw_text": "余海燕"}]
    assert _field_evidence_is_substantive(code_rule, code_evidence)
    assert _field_evidence_is_substantive(manager_rule, manager_evidence)


def test_numeric_grounding_requires_a_complete_token_not_a_substring():
    assert _numbers_are_grounded("10", [{"raw_text": "基金代码 510100"}]) is False
    assert _numbers_are_grounded("0.15%", [{"raw_text": "基金管理费年费率0.15%"}]) is True
    assert _numbers_are_grounded("510100", [{"raw_text": "基金代码 510100"}]) is True
    assert _numbers_are_grounded("4,722,395,708.64", [{"raw_text": "合计 4,722,395,708.64 元"}]) is True


def test_code_comparison_does_not_treat_shared_fund_code_as_share_class_match():
    assert _compare_code_sets("基金代码：000001（前端）", "基金主代码：000001；交易代码：000001（前端）、000002（后端）") is None
    assert _compare_code_sets("基金代码：000001", "基金主代码：000001") == "MATCH"
    assert _compare_code_sets("基金代码：000001", "基金主代码：000002") == "MISMATCH"
    assert _compare_code_sets("基金代码未识别", "基金主代码：000002") is None


def test_deterministic_numeric_comparison_is_limited_to_evidenced_annual_rates():
    management_fee = next(rule for rule in RULES if rule["key"] == "management_fee")
    audit_fee = next(rule for rule in RULES if rule["key"] == "audit_fee")
    assert _can_compare_numeric_deterministically(management_fee, "MISMATCH")
    assert not _can_compare_numeric_deterministically(management_fee, "UNRESOLVED")
    assert not _can_compare_numeric_deterministically(audit_fee, "MISMATCH")


def test_comparison_selects_periodic_report_even_when_prospectus_has_matching_value():
    provider = MatchProvider()
    summary = "一、产品概况\n基金管理人：甲基金管理公司"
    report = "一、基金概况\n基金管理人：乙基金管理公司"
    prospectus = "一、基金概况\n基金管理人：甲基金管理公司"
    result = asyncio.run(compare_product_summary(documents=_docs(summary, report, prospectus), provider=provider))
    manager = next(item for item in result["items"] if item["key"] == "manager")
    assert manager["status"] == "MISMATCH"
    assert manager["comparison_status"] == "MISMATCH"
    assert manager["extraction_status"] == {"target": "FOUND", "primary_reference": "FOUND", "selected_reference": "FOUND"}
    assert manager["applicability_status"] == "ELIGIBLE"
    assert manager["evidence_status"] == "VERIFIED"
    assert manager["selected_reference"]["role"] == "periodic_report"
    assert manager["primary_lookup"]["status"] == "FOUND"
    assert manager["fallback_lookup"]["status"] == "NOT_TRIGGERED"


def test_unconfirmed_primary_candidate_miss_does_not_trigger_prospectus_fallback():
    class NoCallProvider:
        calls = 0
        async def generate_text(self, system, prompt):
            self.calls += 1
            return json.dumps({"items": []})

    provider = NoCallProvider()
    result = asyncio.run(compare_product_summary(documents=_docs(
        "产品资料概要\n基金管理人：甲基金管理公司", "定期报告正文无对应内容", "基金管理人：甲基金管理公司"
    ), provider=provider))
    manager = next(item for item in result["items"] if item["key"] == "manager")
    assert manager["status"] == "UNRESOLVED"
    assert manager["comparison_status"] == "UNRESOLVED"
    assert manager["evidence_status"] == "MISSING"
    assert manager["fallback_lookup"]["status"] == "NOT_TRIGGERED"
    assert provider.calls == 2  # 初次目标事实提取 + 定期报告完整章节路由


def test_late_or_undated_primary_report_is_never_reintroduced_by_full_report_lookup():
    class NoCallProvider:
        calls = 0
        async def generate_text(self, system, prompt):
            self.calls += 1
            raise AssertionError("日期无效的报告不应进入模型或全文补查")

    for metadata, expected_code in (
        ({"compiled_date": "2026-01-26", "sent_date": "2026-01-30", "report_subtype": "quarterly"},
         "PRIMARY_SOURCE_AFTER_TARGET_DATE"),
        ({"compiled_date": "2026-01-26", "report_subtype": "quarterly"},
         "PRIMARY_SOURCE_DATE_UNCONFIRMED"),
        ({"compiled_date_interval": {"start": "2026-01-01", "end": "2026-01-31"},
          "sent_date_interval": {"start": "2026-01-10", "end": "2026-01-20"}, "report_subtype": "quarterly"},
         "PRIMARY_SOURCE_DATE_UNCONFIRMED"),
    ):
        provider = NoCallProvider()
        documents = _docs("一、产品概况\n基金管理人：甲基金管理公司", "一、基金概况\n基金管理人：甲基金管理公司",
                          "一、基金概况\n基金管理人：甲基金管理公司")
        documents["periodic_report"]["metadata"] = metadata
        if metadata.get("sent_date_interval"):
            documents["product_summary"]["metadata"] = {
                "compiled_date_interval": {"start": "2026-01-01", "end": "2026-01-31"}
            }
        result = asyncio.run(compare_product_summary(documents=documents, provider=provider))
        manager = next(item for item in result["items"] if item["key"] == "manager")
        assert manager["status"] == "NOT_CHECKED", metadata
        assert manager["reason_code"] == expected_code
        assert manager["primary_lookup"]["status"] == ("AFTER_TARGET_DATE" if "AFTER_TARGET_DATE" in expected_code else "DATE_UNCONFIRMED")
        assert manager["fallback_lookup"]["status"] == "NOT_TRIGGERED"
        assert provider.calls == 0


def test_unresolved_candidate_requires_full_report_confirmation_before_fallback():
    class CandidateMissingProvider:
        calls = []
        async def generate_text(self, system, prompt):
            self.calls.append(prompt)
            if "字段与证据包：\n" in prompt:
                pack = json.loads(prompt.split("字段与证据包：\n", 1)[1])
                manager = next(item for item in pack if item["key"] == "manager")
                target = next(source for source in manager["summary_candidates"] if "基金管理人" in source["raw_text"])
                return json.dumps({"items": [{"item_id": manager["item_id"], "summary_value": target["raw_text"],
                    "summary_source_ids": [target["source_id"]], "references": [{"role": "periodic_report", "status": "UNRESOLVED",
                    "value": "候选出现管理费项目但不清楚是否为年费率", "source_ids": [], "reason": "候选口径不足"}]}]}, ensure_ascii=False)
            if "只恢复failed_sides列出的失败侧字段证据" in prompt:
                return json.dumps({"items": []})
            if "选择定期报告中的完整章节" in prompt:
                payload = json.loads(prompt.split("\n", 1)[1])
                manager = next(item for item in payload["items"] if item["item_id"] == "manager")
                section = next(section for section in payload["sections"] if "基金概况" in section["path"])
                return json.dumps({"items": [{"item_id": manager["item_id"], "section_ids": [section["section_id"]]}]}, ensure_ascii=False)
            if "仅检查每项列出的完整章节原文" in prompt:
                payload = json.loads(prompt.split("\n", 1)[1])
                manager = next(item for item in payload["items"] if item["item_id"] == "manager")
                target = next(source for source in manager["summary_candidates"] if "基金管理人" in source["raw_text"])
                return json.dumps({"items": [{"item_id": manager["item_id"], "summary_value": target["raw_text"],
                    "summary_source_ids": [target["source_id"]], "primary_status": "UNRESOLVED",
                    "primary_value": "", "primary_source_ids": [], "comparison_status": "UNRESOLVED",
                    "reason": "全文检查后仍无法确认"}]}, ensure_ascii=False)
            raise AssertionError("不应触发补充来源检索")

    provider = CandidateMissingProvider()
    result = asyncio.run(compare_product_summary(documents=_docs(
        "一、产品概况\n基金管理人：甲基金管理公司",
        "一、基金概况\n基金管理人信息待补充",
        "一、基金概况\n基金管理人：甲基金管理公司",
    ), provider=provider))
    manager = next(item for item in result["items"] if item["key"] == "manager")
    assert manager["primary_lookup"]["status"] == "UNRESOLVED"
    assert manager["fallback_lookup"]["status"] == "NOT_TRIGGERED"
    assert not any("主要定期报告已明确确认" in prompt for prompt in provider.calls)


def test_confirmed_primary_missing_uses_allowed_prospectus_and_preserves_its_result():
    class FallbackProvider:
        async def generate_text(self, system, prompt):
            if "选择定期报告中的完整章节" in prompt:
                payload = json.loads(prompt.split("\n", 1)[1])
                item = next(value for value in payload["items"] if value["item_id"] == "manager")
                section = next(section for section in payload["sections"] if "定期报告" in section["path"])
                return json.dumps({"items": [{"item_id": item["item_id"], "section_ids": [section["section_id"]]}]}, ensure_ascii=False)
            if "仅检查每项列出的完整章节原文" in prompt:
                payload = json.loads(prompt.split("\n", 1)[1])
                item = next(value for value in payload["items"] if value["item_id"] == "manager")
                target_label = next(source for source in item["summary_candidates"] if source["raw_text"] == "基金管理人")
                target_value = next(source for source in item["summary_candidates"] if source["raw_text"] == "甲基金管理公司")
                return json.dumps({"items": [{"item_id": item["item_id"], "summary_value": "甲基金管理公司",
                    "summary_source_ids": [target_label["source_id"], target_value["source_id"]], "primary_status": "CONFIRMED_ABSENT",
                    "primary_value": "", "primary_source_ids": [], "comparison_status": "UNRESOLVED",
                    "reason": "已检查定期报告全文，未披露该管理人字段"}]}, ensure_ascii=False)
            if "按每项已验证目标事实及其Markdown规则" in prompt:
                payload = json.loads(prompt.split("\n", 1)[1])
                item = next(value for value in payload["items"] if value["item_id"] == "manager")
                candidate = next(source for source in item["fallback_candidates"] if "基金管理人：" in source["raw_text"])
                return json.dumps({"items": [{"item_id": "manager", "status": "FOUND", "comparison_status": "MATCH",
                    "value": candidate["raw_text"], "source_ids": [candidate["source_id"]],
                    "reason": "招募说明书补充依据"}]}, ensure_ascii=False)
            return json.dumps({"items": []})

    documents = _docs(
        "一、产品概况\n基金管理人\n甲基金管理公司", "一、定期报告\n其他内容：本期报告已披露。", "一、基金管理人\n基金管理人：甲基金管理公司"
    )
    documents["product_summary"]["metadata"] = {"compiled_date": "2026-01-26"}
    documents["prospectus"]["metadata"] = {"date_interval": {"start": "2025-12-01", "end": "2025-12-31"}}
    result = asyncio.run(compare_product_summary(documents=documents, provider=FallbackProvider()))
    manager = next(item for item in result["items"] if item["key"] == "manager")
    assert manager["status"] == "MATCH"
    assert manager["summary"]["raw_text"] == "甲基金管理公司"
    assert len(manager["summary"]["evidence"]) == 2
    assert manager["selected_reference"]["role"] == "prospectus"
    assert manager["fallback_lookup"]["status"] == "FOUND"
    assert manager["references"]["periodic_report"]["status"] == "CONFIRMED_ABSENT"


def test_unverified_annual_rate_uses_rule_allowed_prospectus_but_keeps_primary_unresolved():
    class AnnualRateFallbackProvider:
        async def generate_text(self, system, prompt):
            if "字段与证据包：\n" in prompt:
                items = json.loads(prompt.split("字段与证据包：\n", 1)[1])
                target = next(item for item in items if item["item_id"] == "management_fee")
                summary_source = next(source for source in target["summary_candidates"] if "0.15%" in source["raw_text"])
                return json.dumps({"items": [{"item_id": "management_fee", "summary_value": "0.15%",
                    "summary_source_ids": [summary_source["source_id"]],
                    "references": [{"role": "periodic_report", "status": "UNRESOLVED", "value": "", "source_ids": []}],
                    "reason": "报告只有本期发生额，未确认年费率"}]}, ensure_ascii=False)
            if "只恢复failed_sides列出的失败侧字段证据" in prompt:
                return json.dumps({"items": []})
            if "选择定期报告中的完整章节" in prompt:
                payload = json.loads(prompt.split("\n", 1)[1])
                routes = []
                for item in payload["items"]:
                    sections = [section["section_id"] for section in payload["sections"]
                                if "费用" in section["path"] or "管理费" in section["path"]]
                    routes.append({"item_id": item["item_id"], "section_ids": sections[:1] if item["item_id"] == "management_fee" else []})
                return json.dumps({"items": routes}, ensure_ascii=False)
            if "仅检查每项列出的完整章节原文" in prompt:
                payload = json.loads(prompt.split("\n", 1)[1])
                target = next(item for item in payload["items"] if item["item_id"] == "management_fee")
                summary_source = next(source for source in target["summary_candidates"] if "0.15%" in source["raw_text"])
                return json.dumps({"items": [{"item_id": "management_fee", "summary_value": "0.15%",
                    "summary_source_ids": [summary_source["source_id"]], "primary_status": "UNRESOLVED",
                    "primary_value": "", "primary_source_ids": [], "comparison_status": "UNRESOLVED",
                    "reason": "报告未提供可验证的年费率"}]}, ensure_ascii=False)
            if "按每项已验证目标事实及其Markdown规则" in prompt:
                payload = json.loads(prompt.split("\n", 1)[1])
                request = next(item for item in payload["items"] if item["item_id"] == "management_fee")
                source = next(source for source in request["fallback_candidates"] if "0.15%" in source["raw_text"])
                return json.dumps({"items": [{"item_id": "management_fee", "status": "FOUND",
                    "comparison_status": "MISMATCH", "value": "0.15%", "source_ids": [source["source_id"]],
                    "reason": "同一基金合同年费率"}]}, ensure_ascii=False)
            raise AssertionError(f"unexpected model call: {prompt[:80]}")

    documents = _docs(
        "一、基金产品概况\n基金管理费率：0.15%",
        "5. 费用信息\n本期发生基金管理费100元",
        "八、基金的费用与税收\n基金管理费年费率0.15%",
    )
    documents["product_summary"]["metadata"] = {"compiled_date": "2026-07-24"}
    documents["prospectus"]["metadata"] = {"date_interval": {"start": "2026-01-01", "end": "2026-01-31"}}
    result = asyncio.run(compare_product_summary(documents=documents, provider=AnnualRateFallbackProvider()))
    fee = next(item for item in result["items"] if item["item_id"] == "management_fee")
    assert fee["primary_lookup"]["status"] == "UNRESOLVED"
    assert fee["fallback_lookup"]["status"] == "FOUND"
    assert fee["selected_reference"]["role"] == "prospectus"
    assert fee["status"] == "MATCH"
    assert fee["references"]["periodic_report"]["status"] == "UNRESOLVED"


def test_target_fact_is_extracted_even_when_primary_keyword_candidates_are_empty():
    class TargetFirstFallbackProvider:
        async def generate_text(self, system, prompt):
            if "字段与证据包：\n" in prompt:
                items = json.loads(prompt.split("字段与证据包：\n", 1)[1])
                item = next(item for item in items if item["item_id"] == "custody_fee")
                assert not item["primary_candidates"]
                target = next(source for source in item["summary_candidates"] if "0.20%" in source["raw_text"])
                return json.dumps({"items": [{"item_id": "custody_fee", "summary_value": "0.20%",
                    "summary_source_ids": [target["source_id"]], "references": [{"role": "periodic_report",
                    "status": "UNRESOLVED", "value": "", "source_ids": []}]}]}, ensure_ascii=False)
            if "只恢复failed_sides列出的失败侧字段证据" in prompt:
                return json.dumps({"items": []})
            if "选择定期报告中的完整章节" in prompt:
                payload = json.loads(prompt.split("\n", 1)[1])
                return json.dumps({"items": [{"item_id": item["item_id"], "section_ids": []}
                                             for item in payload["items"]]}, ensure_ascii=False)
            if "按每项已验证目标事实及其Markdown规则" in prompt:
                payload = json.loads(prompt.split("\n", 1)[1])
                item = next(item for item in payload["items"] if item["item_id"] == "custody_fee")
                source = next(source for source in item["fallback_candidates"] if "0.20%" in source["raw_text"])
                return json.dumps({"items": [{"item_id": "custody_fee", "status": "FOUND",
                    "comparison_status": "MATCH", "value": "0.20%", "source_ids": [source["source_id"]]}]}, ensure_ascii=False)
            raise AssertionError(f"unexpected model call: {prompt[:80]}")

    documents = _docs(
        "三、投资本基金涉及的费用\n托管费率：固定费率0.20%",
        "一、定期报告\n本期披露事项",
        "八、基金的费用与税收\n基金托管费年费率0.20%",
    )
    documents["product_summary"]["metadata"] = {"compiled_date": "2026-06-30"}
    documents["prospectus"]["metadata"] = {"date_interval": {"start": "2026-01-01", "end": "2026-01-31"}}
    result = asyncio.run(compare_product_summary(documents=documents, provider=TargetFirstFallbackProvider()))
    fee = next(item for item in result["items"] if item["item_id"] == "custody_fee")
    assert fee["summary"]["raw_text"] == "0.20%"
    assert fee["selected_reference"]["role"] == "prospectus"
    assert fee["status"] == "MATCH"


def test_future_prospectus_evidence_is_retained_but_not_treated_as_applicable():
    class FutureVersionFallbackProvider:
        async def generate_text(self, system, prompt):
            if "选择定期报告中的完整章节" in prompt:
                payload = json.loads(prompt.split("\n", 1)[1])
                item = next(value for value in payload["items"] if value["item_id"] == "manager")
                section = next(section for section in payload["sections"] if "基金概况" in section["path"])
                return json.dumps({"items": [{"item_id": item["item_id"], "section_ids": [section["section_id"]]}]})
            if "仅检查每项列出的完整章节原文" in prompt:
                payload = json.loads(prompt.split("\n", 1)[1])
                item = next(value for value in payload["items"] if value["item_id"] == "manager")
                target = next(source for source in item["summary_candidates"] if "基金管理人" in source["raw_text"])
                return json.dumps({"items": [{"item_id": item["item_id"], "summary_value": "甲基金管理公司",
                    "summary_source_ids": [target["source_id"]], "primary_status": "CONFIRMED_ABSENT",
                    "primary_value": "", "primary_source_ids": [], "comparison_status": "UNRESOLVED",
                    "reason": "报告完整章节未披露该字段"}]}, ensure_ascii=False)
            if "按每项已验证目标事实及其Markdown规则" in prompt:
                payload = json.loads(prompt.split("\n", 1)[1])
                item = next(value for value in payload["items"] if value["item_id"] == "manager")
                candidate = next(source for source in item["fallback_candidates"] if "基金管理人：" in source["raw_text"])
                return json.dumps({"items": [{"item_id": "manager", "status": "FOUND", "comparison_status": "MATCH",
                    "value": candidate["raw_text"], "source_ids": [candidate["source_id"]]}]}, ensure_ascii=False)
            return json.dumps({"items": []})

    documents = _docs(
        "一、产品概况\n基金管理人：甲基金管理公司",
        "一、基金概况\n其他内容：本期报告已披露。",
        "一、基金管理人\n基金管理人：甲基金管理公司",
    )
    documents["product_summary"]["metadata"] = {"compiled_date": "2026-07-24"}
    documents["prospectus"]["metadata"] = {"date_interval": {"start": "2026-07-27", "end": "2026-07-27"}}
    result = asyncio.run(compare_product_summary(documents=documents, provider=FutureVersionFallbackProvider()))
    manager = next(item for item in result["items"] if item["key"] == "manager")
    assert manager["fallback_lookup"]["status"] == "FOUND"
    assert manager["selected_reference"]["role"] == "prospectus"
    assert manager["selected_reference"]["evidence"]
    assert manager["selected_source_date_status"] == "AFTER_TARGET_DATE"
    assert manager["status"] == "UNRESOLVED"
    assert manager["reason_code"] == "FALLBACK_SOURCE_DATE_UNCONFIRMED"


def test_split_table_label_and_value_citations_get_one_bounded_recovery():
    class CitationRecoveryProvider:
        async def generate_text(self, system, prompt):
            if "只恢复failed_sides列出的失败侧字段证据" in prompt:
                payload = json.loads(prompt.split("\n", 1)[1])
                item = next(value for value in payload["items"] if value["key"] == "manager")
                def source(lines, label):
                    return next(value for value in lines if value["raw_text"] == label)
                summary_label = source(item["summary_candidates"], "基金管理人")
                summary_value = source(item["summary_candidates"], "甲基金管理公司")
                report_label = source(item["primary_candidates"], "基金管理人")
                report_value = source(item["primary_candidates"], "甲基金管理公司")
                return json.dumps({"items": [{"item_id": "manager", "summary_value": "甲基金管理公司",
                    "summary_source_ids": [summary_label["source_id"], summary_value["source_id"]],
                    "primary_value": "甲基金管理公司", "primary_source_ids": [report_label["source_id"], report_value["source_id"]],
                    "comparison_status": "MATCH", "reason": "标签和值的引用均已恢复"}]}, ensure_ascii=False)
            payload = json.loads(prompt.split("字段与证据包：\n", 1)[1])
            result = []
            for item in payload:
                summary_value = next(source for source in item["summary_candidates"] if source["raw_text"] == "甲基金管理公司")
                report_value = next(source for source in item["primary_candidates"] if source["raw_text"] == "甲基金管理公司")
                result.append({"item_id": item["item_id"], "summary_value": "甲基金管理公司",
                    "summary_source_ids": [summary_value["source_id"]],
                    "references": [{"role": item["primary_role"], "status": "MATCH", "value": "甲基金管理公司", "source_ids": [report_value["source_id"]]}]})
            return json.dumps({"items": result}, ensure_ascii=False)

    result = asyncio.run(compare_product_summary(documents=_docs(
        "一、产品概况\n基金管理人\n甲基金管理公司",
        "一、基金概况\n基金管理人\n甲基金管理公司",
    ), provider=CitationRecoveryProvider()))
    manager = next(item for item in result["items"] if item["key"] == "manager")
    assert manager["status"] == "MATCH"
    assert len(manager["summary"]["evidence"]) == 2
    assert len(manager["reference"]["evidence"]) == 2


def test_reference_failure_keeps_independently_verified_target_fact():
    class TargetOnlyProvider:
        async def generate_text(self, system, prompt):
            if "只恢复failed_sides列出的失败侧字段证据" in prompt or "字段与证据包：" not in prompt:
                return json.dumps({"items": []})
            pack = json.loads(prompt.split("字段与证据包：\n", 1)[1])
            items = []
            for item in pack:
                if item["item_id"] != "manager":
                    continue
                label = next(source for source in item["summary_candidates"] if source["raw_text"] == "基金管理人")
                value = next(source for source in item["summary_candidates"] if source["raw_text"] == "甲基金管理公司")
                items.append({"item_id": "manager", "summary_value": "甲基金管理公司",
                    "summary_source_ids": [label["source_id"], value["source_id"]],
                    "references": [{"role": "periodic_report", "status": "UNRESOLVED", "value": "",
                                    "source_ids": ["unknown-source"]}]})
            return json.dumps({"items": items}, ensure_ascii=False)

    result = asyncio.run(compare_product_summary(documents=_docs(
        "一、产品概况\n基金管理人\n甲基金管理公司",
        "一、基金概况\n基金管理人\n乙基金管理公司",
    ), provider=TargetOnlyProvider()))
    manager = next(item for item in result["items"] if item["item_id"] == "manager")
    assert manager["status"] == "UNRESOLVED"
    assert manager["facts"]["target"]["status"] == "VERIFIED"
    assert manager["facts"]["target"]["value"] == "甲基金管理公司"
    assert manager["facts"]["selected_reference"]["status"] == "CANDIDATE_UNVERIFIED"


def test_recovery_cannot_rewrite_a_verified_target_fact():
    class RecoveryAttemptsToRewriteTarget:
        async def generate_text(self, system, prompt):
            if "只恢复failed_sides列出的失败侧字段证据" in prompt:
                payload = json.loads(prompt.split("\n", 1)[1])
                item = next(value for value in payload["items"] if value["item_id"] == "manager")
                assert item["verified_target"]["value"] == "甲基金管理公司"
                report_label = next(source for source in item["primary_candidates"] if source["raw_text"] == "基金管理人")
                report_value = next(source for source in item["primary_candidates"] if source["raw_text"] == "甲基金管理公司")
                return json.dumps({"items": [{
                    "item_id": "manager", "summary_value": "乙基金管理公司", "summary_source_ids": [],
                    "primary_value": "甲基金管理公司",
                    "primary_source_ids": [report_label["source_id"], report_value["source_id"]],
                    "comparison_status": "MISMATCH", "reason": "模型尝试改写目标事实",
                }]}, ensure_ascii=False)
            pack = json.loads(prompt.split("字段与证据包：\n", 1)[1])
            items = []
            for item in pack:
                if item["item_id"] != "manager":
                    continue
                label = next(source for source in item["summary_candidates"] if source["raw_text"] == "基金管理人")
                value = next(source for source in item["summary_candidates"] if source["raw_text"] == "甲基金管理公司")
                items.append({"item_id": "manager", "summary_value": "甲基金管理公司",
                    "summary_source_ids": [label["source_id"], value["source_id"]],
                    "references": [{"role": "periodic_report", "status": "UNRESOLVED", "value": "",
                                    "source_ids": ["unknown-source"]}]})
            return json.dumps({"items": items}, ensure_ascii=False)

    result = asyncio.run(compare_product_summary(documents=_docs(
        "一、产品概况\n基金管理人\n甲基金管理公司",
        "一、基金概况\n基金管理人\n甲基金管理公司",
    ), provider=RecoveryAttemptsToRewriteTarget()))
    manager = next(item for item in result["items"] if item["item_id"] == "manager")
    assert manager["summary"]["raw_text"] == "甲基金管理公司"
    assert manager["facts"]["target"]["value"] == "甲基金管理公司"
    assert manager["status"] == "MATCH"


def test_not_applicable_primary_source_stays_not_checked_without_becoming_unresolved():
    class NotApplicableProvider:
        async def generate_text(self, system, prompt):
            if "字段与证据包：\n" not in prompt:
                return json.dumps({"items": []})
            groups = json.loads(prompt.split("字段与证据包：\n", 1)[1])
            results = []
            for item in groups:
                if item["item_id"] != "audit_fee":
                    continue
                target = next(source for source in item["summary_candidates"] if "55,000.00" in source["raw_text"])
                label = next(source for source in item["summary_candidates"] if source["raw_text"] == "审计费用")
                results.append({"item_id": "audit_fee", "summary_value": "审计费用：年费用金额（元）55,000.00",
                    "summary_source_ids": [label["source_id"], target["source_id"]],
                    "references": [{"role": "periodic_report", "status": "SOURCE_MISSING",
                                    "value": "该报告类型不适用", "source_ids": []}]})
            return json.dumps({"items": results}, ensure_ascii=False)

    result = asyncio.run(compare_product_summary(documents=_docs(
        "一、投资本基金涉及的费用\n审计费用\n年费用金额（元）55,000.00",
        "一、季度报告\n报告期末基金份额净值",
    ), provider=NotApplicableProvider()))
    audit_fee = next(item for item in result["items"] if item["item_id"] == "audit_fee")
    assert audit_fee["applicability_status"] == "NOT_APPLICABLE"
    assert audit_fee["primary_lookup"]["status"] == "NOT_APPLICABLE"
    assert audit_fee["status"] == "NOT_CHECKED"
    assert audit_fee["facts"]["target"]["status"] == "VERIFIED"


def test_empty_keyword_recall_is_not_reported_as_confirmed_absence_or_match():
    class NoCallProvider:
        calls = 0
        async def generate_text(self, system, prompt):
            self.calls += 1
            return json.dumps({"items": []})
    provider = NoCallProvider()
    result = asyncio.run(compare_product_summary(documents=_docs("没有列出核对项", "没有相关字段"), provider=provider))
    assert result["status"] == "NOT_CHECKED"
    assert result["compared_count"] == 0
    assert result["not_checked_count"] == len(RULES)
    assert provider.calls == 0
