import asyncio
import copy
import json
from pathlib import Path

import pytest

import app.summary_check as summary
from app.summary_rules import RuleLoadError, load_markdown_rules


def _catalogue(role, sections):
    sources = []
    chapters = []
    for index, (title, lines) in enumerate(sections):
        section_id = f"{role}-section-{index}"
        section_sources = []
        for line in [title, *lines]:
            source_id = f"{role}-{len(sources)}"
            source = {"source_id": source_id, "document_role": role, "source_kind": "text_line",
                      "page": index + 1, "bbox": [0, len(sources), 100, len(sources) + 1], "raw_text": line,
                      "section_id": section_id, "section_path": [title], "row_source_ids": [source_id]}
            sources.append(source)
            section_sources.append(source)
        chapters.append({"section_id": section_id, "title": title, "path": title, "pages": [index + 1],
                         "source_ids": [source["source_id"] for source in section_sources]})
    return {"role": role, "filename": f"{role}.pdf", "sha256": role, "page_count": len(sections),
            "page_dimensions": [], "source_count": len(sources), "chapters": chapters,
            "pages": [{"page": index + 1, "sources": [source for source in sources if source["page"] == index + 1]}
                      for index in range(len(sections))]}


def _manager():
    return copy.deepcopy(next(rule for rule in summary.RULES if rule["item_id"] == "manager"))


def _install_rules(monkeypatch, rules):
    """Inject a deterministic snapshot through the same seam used at runtime."""
    config = {**summary.RULE_CONFIG, "items": rules,
              "modules": sorted({rule["module_id"] for rule in rules})}
    monkeypatch.setattr(summary, "RULES", rules)
    monkeypatch.setattr(summary, "_load_rule_snapshot", lambda: (config, rules))


def _run(provider, target, report):
    documents = {role: {"filename": cat["filename"], "metadata": {"report_subtype": "quarterly"}}
                 for role, cat in (("product_summary", target), ("periodic_report", report))}
    return asyncio.run(summary.compare_product_summary(
        documents=documents, provider=provider,
        source_catalogues={"product_summary": target, "periodic_report": report}, historical=False,
    ))


def test_prescribed_chapter_beats_keyword_mentions_elsewhere():
    catalogue = _catalogue("periodic_report", [
        ("一、基金基本情况", ["基金管理人：甲公司", "本表后续数据"]),
        ("二、风险提示", ["基金管理人对风险作如下说明", "与身份无关的风险正文" * 100]),
    ])
    result = summary._source_candidates(catalogue, _manager())
    assert {source["page"] for source in result} == {1}
    assert "本表后续数据" in {source["raw_text"] for source in result}


def test_specific_fee_chapter_precedes_parent_and_other_fee_sections():
    catalogue = _catalogue("periodic_report", [
        ("6.4.9.2 关联方报酬", ["报酬总览"]),
        ("6.4.9.2.1 基金管理费", ["管理费按0.70%年费率计提"]),
        ("6.4.9.2.2 基金托管费", ["托管费按0.20%年费率计提"]),
        ("6.4.9.2.3 销售服务费", ["销售服务费按0.35%计提"]),
    ])
    rule = next(rule for rule in summary.RULES if rule["item_id"] == "management_fee")
    result = summary._source_candidates(catalogue, rule)
    assert {source["page"] for source in result} == {2}


@pytest.mark.parametrize("marker", ["（一）", "(一)", "一、"])
def test_supplement_identity_uses_overview_not_all_mentions(marker):
    catalogue = _catalogue("prospectus", [
        (marker + "基金管理人概况", ["名称：甲公司", "注册地址：乙地"]),
        ("二、基金管理人的职责", ["基金管理人依法履行各项职责"]),
        ("19、基金管理人：指甲公司", ["释义内容"]),
    ])
    candidates = summary._source_candidates(catalogue, _manager())
    assert {source["page"] for source in candidates} == {1}
    assert {source["raw_text"] for source in candidates} >= {"名称：甲公司", "注册地址：乙地"}


def test_verified_primary_fact_with_uncertain_comparison_stops_lookup(monkeypatch):
    _install_rules(monkeypatch, [_manager()])
    target = _catalogue("product_summary", [("一、产品概况", ["基金管理人：甲公司"])])
    report = _catalogue("periodic_report", [("一、基金基本情况", ["基金管理人：甲公司"])])

    class Provider:
        calls = 0

        async def generate_text(self, system, prompt):
            self.calls += 1
            assert "字段与证据包：" in prompt
            return json.dumps({"items": [{"item_id": "manager", "summary_value": "甲公司",
                "summary_source_ids": [target["pages"][0]["sources"][1]["source_id"]],
                "references": [{"role": "periodic_report", "status": "UNRESOLVED", "value": "甲公司",
                    "source_ids": [report["pages"][0]["sources"][1]["source_id"]], "reason": "适用时间需确认"}]}]})

    provider = Provider()
    result = _run(provider, target, report)["items"][0]
    assert provider.calls == 1
    assert result["primary_lookup"]["status"] == "FOUND"
    assert result["reference"]["raw_text"] == "甲公司"
    assert result["status"] == "UNRESOLVED"


@pytest.mark.parametrize("basis, model_status, expected", [
    (None, "MISMATCH", "UNRESOLVED"),
    ({"same_period": False, "same_nature": True, "same_unit": True}, "MISMATCH", "UNRESOLVED"),
    ({"same_period": True, "same_nature": False, "same_unit": True}, "MATCH", "UNRESOLVED"),
    ({"same_period": True, "same_nature": True, "same_unit": None}, "MISMATCH", "UNRESOLVED"),
    ({"same_period": "true", "same_nature": True, "same_unit": True}, "MISMATCH", "UNRESOLVED"),
    ({"same_period": True, "same_nature": True, "same_unit": True}, "MISMATCH", "MISMATCH"),
    ({"same_period": True, "same_nature": True, "same_unit": True}, "MATCH", "MATCH"),
])
def test_amount_verdict_requires_confirmed_period_nature_and_unit(monkeypatch, basis, model_status, expected):
    rule = copy.deepcopy(next(rule for rule in summary.RULES if rule["item_id"] == "audit_fee"))
    rule["primary_source"]["subtypes"] = ["quarterly"]
    _install_rules(monkeypatch, [rule])
    target = _catalogue("product_summary", [("（二）基金运作相关费用", ["审计费用：55,000.00元"])])
    report_value = "55,000.00" if model_status == "MATCH" else "27,500.00"
    report = _catalogue("periodic_report", [("6.4.7.18 其他费用", [f"审计费用：{report_value}元"])])

    class Provider:
        async def generate_text(self, system, prompt):
            return json.dumps({"items": [{"item_id": "audit_fee", "summary_value": "55,000.00元",
                "summary_source_ids": [target["pages"][0]["sources"][1]["source_id"]],
                "references": [{"role": "periodic_report", "status": model_status, "value": report_value+"元",
                    "source_ids": [report["pages"][0]["sources"][1]["source_id"]], "comparison_basis": basis}]}]})

    result = _run(Provider(), target, report)["items"][0]
    assert result["status"] == expected
    assert result["evidence_status"] == "VERIFIED"
    assert result["primary_lookup"]["status"] == "FOUND"
    assert result["comparison_basis"] == basis
    if expected == "UNRESOLVED":
        assert result["reason_code"] == "COMPARISON_BASIS_UNCONFIRMED"


def test_matching_digits_cannot_promote_unresolved_code_roles(monkeypatch):
    rule = copy.deepcopy(next(rule for rule in summary.RULES if rule["item_id"] == "fund_code"))
    _install_rules(monkeypatch, [rule])
    target = _catalogue("product_summary", [("一、产品概况", ["基金代码050011；下属基金代码050011"])])
    report = _catalogue("periodic_report", [("一、基金基本情况", ["基金主代码050011"])])

    class Provider:
        async def generate_text(self, system, prompt):
            assert "字段与证据包：" in prompt
            return json.dumps({"items": [{"item_id": "fund_code", "summary_value": "基金代码050011；下属基金代码050011",
                "summary_source_ids": [target["pages"][0]["sources"][1]["source_id"]],
                "references": [{"role": "periodic_report", "status": "UNRESOLVED", "value": "基金主代码050011",
                    "source_ids": [report["pages"][0]["sources"][1]["source_id"]], "reason": "下属基金代码角色没有披露"}]}]})

    result = _run(Provider(), target, report)["items"][0]
    assert result["status"] == "UNRESOLVED"
    assert result["primary_lookup"]["status"] == "FOUND"


def test_indicator_missing_from_target_scope_is_not_sent_to_model(monkeypatch):
    _install_rules(monkeypatch, [_manager()])
    target = _catalogue("product_summary", [("一、产品概况", ["基金代码050011"])])
    report = _catalogue("periodic_report", [("一、基金基本情况", ["基金代码050011"])])

    class Provider:
        async def generate_text(self, system, prompt):
            raise AssertionError("No target indicator exists in the prescribed chapter")

    result = _run(Provider(), target, report)["items"][0]
    assert result["status"] == "NOT_CHECKED"
    assert result["reason_code"] == "TARGET_FIELD_NOT_FOUND_IN_PRESET_SCOPE"


def test_rate_value_can_bind_its_own_unique_section_heading():
    catalogue = _catalogue("periodic_report", [("6.4.9.2.1 基金管理费", ["支付管理人报酬按0.70%的年费率计提"])])
    rule = next(rule for rule in summary.RULES if rule["item_id"] == "management_fee")
    sources = catalogue["pages"][0]["sources"]
    fact = summary._verified_fact("0.70%年费率", [sources[1]["source_id"]], sources, rule)
    assert fact is not None
    assert set(fact["source_ids"]) == {source["source_id"] for source in sources}
    duplicate = {**sources[0], "source_id": "duplicate-heading"}
    assert summary._verified_fact("0.70%年费率", [sources[1]["source_id"]], sources+[duplicate], rule) is None
    other = _catalogue("periodic_report", [("6.4.9.2.2 基金托管费", ["按0.70%的年费率计提"])])["pages"][0]["sources"]
    assert summary._verified_fact("0.70%年费率", [other[1]["source_id"]], other, rule) is None


def test_same_chapter_indicators_stay_together_when_forming_model_batches():
    def item(key, chapter):
        return {"item_id": key, "policy": "x"*4000,
                "primary_candidates": [{"source_id": chapter, "raw_text": chapter*(1000 if chapter == "base" else 1500)}]}
    items = [item("basic_a", "base"), item("manager_a", "manager"), item("basic_b", "base"), item("manager_b", "manager")]
    batches = summary._payload_batches(items, budget=26000)
    assert any({value["item_id"] for value in batch} == {"manager_a", "manager_b"} for batch in batches)
    assert all(summary._json_size(summary._shared_context(batch)) <= 26000 for batch in batches)


def test_operations_fee_subchapter_excludes_sales_charges():
    assert summary._heading_info("（二）基金运作相关费用")[1] == 2
    path = Path("assets/博时信用债券投资基金/2026-09-21_产品资料概要更新_AB份额.pdf")
    catalogue = summary.build_source_catalogue(path.name, path.read_bytes(), "product_summary")
    rule = next(rule for rule in summary.RULES if rule["item_id"] == "management_fee")
    sources = summary._source_candidates(catalogue, rule)
    assert any("0.70%" in source["raw_text"] for source in sources)
    assert not any("赎回费持有期" in source["raw_text"] for source in sources)


def test_document_dates_are_passed_to_comparison_model(monkeypatch):
    _install_rules(monkeypatch, [_manager()])
    target = _catalogue("product_summary", [("一、产品概况", ["基金管理人：甲公司"])])
    report = _catalogue("periodic_report", [("一、基金基本情况", ["基金管理人：甲公司"])])
    documents = {"product_summary": {"filename": "概要.pdf", "metadata": {"compiled_date": "2026-01-26"}},
                 "periodic_report": {"filename": "报告.pdf", "metadata": {"sent_date": "2026-01-22", "period_end": "2025-12-31", "report_subtype": "quarterly"}}}

    class Provider:
        async def generate_text(self, system, prompt):
            assert all(date in system for date in ["2026-01-26", "2026-01-22", "2025-12-31"])
            return json.dumps({"items": [{"item_id": "manager", "summary_value": "甲公司",
                "summary_source_ids": [target["pages"][0]["sources"][1]["source_id"]],
                "references": [{"role": "periodic_report", "status": "MATCH", "value": "甲公司",
                    "source_ids": [report["pages"][0]["sources"][1]["source_id"]]}]}]})

    result = asyncio.run(summary.compare_product_summary(documents=documents, provider=Provider(),
        source_catalogues={"product_summary": target, "periodic_report": report}))
    assert result["items"][0]["status"] == "MATCH"


def test_total_model_failure_stops_before_recovery_instead_of_completing(monkeypatch):
    _install_rules(monkeypatch, [_manager()])
    target = _catalogue("product_summary", [("一、产品概况", ["基金管理人：甲公司"])])
    report = _catalogue("periodic_report", [("一、基金基本情况", ["基金管理人：甲公司"])])

    class UnavailableProvider:
        calls = 0

        async def generate_text(self, system, prompt):
            self.calls += 1
            raise RuntimeError("503 - No compatible account is available for this model")

    provider = UnavailableProvider()
    with pytest.raises(RuntimeError, match="SUMMARY_MODEL_FAILED.*503"):
        _run(provider, target, report)
    assert provider.calls == 2


def test_chapter_directory_retains_ids_without_body_preview():
    catalogue = _catalogue("periodic_report", [("基金基本情况", ["正文" * 1000])])
    directory = summary._chapter_directory(catalogue)
    assert directory[0]["source_ids"] == [source["source_id"] for source in catalogue["pages"][0]["sources"]]
    assert "preview" not in directory[0]


def test_arabic_subheading_does_not_end_chinese_parent_chapter():
    assert summary._heading_info("二、基金投资与净值表现")[1] == 1
    assert summary._heading_info("1、决策依据 2、投资管理体制")[1] > 1


def test_real_summary_continued_table_fields_remain_in_prescribed_chapter():
    path = Path("assets/华泰柏瑞沪深300交易型开放式指数证券投资基金/2025-11-24_产品资料概要更新.pdf")
    catalogue = summary.build_document_source_catalogue(path.name, path.read_bytes(), "product_summary")
    for item_id, label in (("benchmark", "业绩比较基准"), ("risk_return", "风险收益特征")):
        rule = next(rule for rule in summary.RULES if rule["item_id"] == item_id)
        candidates = summary._source_candidates(catalogue, rule)
        assert any(label in source["raw_text"] for source in candidates)
        assert any("基金投资与净值表现" in " / ".join(source["section_path"]) for source in candidates)


def test_real_interim_fee_chapters_exclude_other_fee_types_and_keep_rate():
    path = Path("assets/博时信用债券投资基金/2026-08-31_2026年中期报告.pdf")
    catalogue = summary.build_source_catalogue(path.name, path.read_bytes(), "periodic_report")
    for item_id, label, rate in (("management_fee", "基金管理费", "0.70%"), ("custody_fee", "基金托管费", "0.20%")):
        rule = next(rule for rule in summary.RULES if rule["item_id"] == item_id)
        sources = summary._source_candidates(catalogue, rule)
        assert any(label in source["raw_text"] for source in sources)
        assert any(rate in source["raw_text"] for source in sources)
        assert not any("销售服务费" in source["raw_text"] for source in sources)


def test_real_large_manager_table_stays_whole_in_a_separate_budgeted_packet():
    path = Path("assets/纳斯达克100交易型开放式指数证券投资基金/2026-08-31_2026年中期报告.pdf")
    catalogue = summary.build_document_source_catalogue(path.name, path.read_bytes(), "periodic_report")
    rule = next(rule for rule in summary.RULES if rule["item_id"] == "fund_managers")
    plan = summary._context_plan(catalogue, summary._source_candidates(catalogue, rule), item=rule)
    assert not plan["skipped_units"]
    large = [batch for batch in plan["batches"] if summary._json_size(batch) > summary.CONTEXT_BATCH_CHARACTERS]
    assert len(large) == 1 and len(large[0]["logical_units"]) == 1
    assert summary._json_size(large[0]) <= summary.LOGICAL_TABLE_CHARACTERS
    assert len(large[0]["candidates"]) == 180


def test_html_table_context_keeps_colspan_and_all_continuation_text():
    table = '<table><tr><td colspan="2">基金管理人</td></tr>\n<tr><td>甲公司</td><td>跨页后半段</td></tr></table>'
    catalogue = {"reader_context": {"status": "READY", "markdown": "正文\n" + table + "\n尾注"}}
    assert summary._markdown_table_context(catalogue, _manager()) == [table]


def test_whole_cross_page_table_expands_selected_ids_without_reordering_or_splitting():
    catalogue = _catalogue("periodic_report", [("基金基本情况", ["基金管理人：甲公司"]), ("续表", ["完整后半段"])])
    sources = [source for page in catalogue["pages"] for source in page["sources"]]
    table = "<table><tr><td>基金管理人：甲公司</td></tr><tr><td>完整后半段</td></tr></table>"
    catalogue["reader_context"] = {"logical_units": [
        {"unit_id": "whole-table", "kind": "table", "pages": [1, 2],
         "source_ids": [source["source_id"] for source in reversed(sources)], "markdown": table},
    ]}
    plan = summary._context_plan(catalogue, sources[:1], budget=3000)
    assert len(plan["batches"]) == 1
    assert plan["batches"][0]["logical_units"][0]["markdown"] == table
    assert [source["source_id"] for source in plan["batches"][0]["candidates"]] == [source["source_id"] for source in sources]
    assert plan["coverage_complete"]


def test_oversized_table_never_becomes_a_partial_repair_excerpt():
    catalogue = _catalogue("periodic_report", [("基金基本情况", ["基金管理人：甲公司", "长表内容" * 200])])
    sources = catalogue["pages"][0]["sources"]
    catalogue["reader_context"] = {"logical_units": [{"unit_id": "large-table", "kind": "table", "pages": [1],
        "source_ids": [source["source_id"] for source in sources], "markdown": "<table>" + "表格正文" * 500 + "</table>"}]}
    plan = summary._context_plan(catalogue, sources, budget=500)
    assert not plan["batches"]
    assert plan["skipped_units"][0]["reason"] == "LOGICAL_UNIT_EXCEEDS_BUDGET"
    item = {"primary_candidates": sources, "primary_context_plan": plan}
    assert summary._recovery_candidates(item, "primary") == []
    assert item["primary_excerpt_coverage"]["skipped_unit_count"] == 1


def test_unbound_table_source_blocks_partial_claim_of_complete_coverage():
    catalogue = _catalogue("periodic_report", [("基金基本情况", ["基金管理人：甲公司"])])
    sources = catalogue["pages"][0]["sources"]
    catalogue["logical_units"] = [{"unit_id": "unbound", "kind": "table", "source_ids": [sources[0]["source_id"], "unknown"]}]
    plan = summary._context_plan(catalogue, sources[:1])
    assert not plan["coverage_complete"]
    assert plan["skipped_units"][0]["reason"] == "LOGICAL_UNIT_SOURCE_IDS_UNBOUND"


def test_large_section_is_fully_partitioned_without_truncating_lines():
    catalogue = _catalogue("periodic_report", [("基金基本情况", [f"原文{index}" + "完整段落" * 20 for index in range(40)])])
    sources = catalogue["pages"][0]["sources"]
    plan = summary._context_plan(catalogue, sources, budget=700)
    assert len(plan["batches"]) > 1
    actual = [source for batch in plan["batches"] for source in batch["candidates"]]
    assert [source["raw_text"] for source in actual] == [source["raw_text"] for source in sources]
    assert all(summary._json_size(batch) <= 700 for batch in plan["batches"])
    assert plan["coverage_complete"]


def test_lookup_expands_one_section_at_a_time_and_cannot_rewrite_target(monkeypatch):
    rule = _manager()
    rule["section_hints_by_role"]["periodic_report"].append("其他披露")
    _install_rules(monkeypatch, [rule])
    target = _catalogue("product_summary", [("一、产品概况", ["基金管理人：甲公司"])])
    report = _catalogue("periodic_report", [("一、基金基本情况", ["基金管理人信息待核实"]),
                                            ("二、其他披露", ["基金管理人：甲公司"])])
    class Provider:
        def __init__(self):
            self.lookup_pages = []

        async def generate_text(self, system, prompt):
            assert len(system) + len(prompt) <= summary.MODEL_REQUEST_CHARACTERS
            if "字段与证据包：\n" in prompt:
                payload = json.loads(prompt.split("字段与证据包：\n")[1])[0]
                source = next(source for source in payload["summary_candidates"] if "甲公司" in source["raw_text"])
                return json.dumps({"items": [{"item_id": "manager", "summary_value": "甲公司", "summary_source_ids": [source["source_id"]],
                    "references": [{"role": "periodic_report", "status": "UNRESOLVED", "source_ids": [], "value": ""}]}]})
            data = json.loads(prompt.split("\n", 1)[1])
            if "只恢复failed_sides" in prompt:
                assert data["items"][0]["verified_target"]["value"] == "甲公司"
                assert not data["items"][0]["summary_candidates"]
                return '{"items":[]}'
            if "选择定期报告中的完整章节" in prompt:
                return json.dumps({"items": [{"item_id": "manager", "section_ids": [section["section_id"] for section in data["sections"]]}]})
            request = data["items"][0]
            assert request["verified_target"]["value"] == "甲公司"
            assert not request["summary_candidates"]
            pages = {source["page"] for source in request["primary_candidates"]}
            self.lookup_pages.append(pages)
            source = next((source for source in request["primary_candidates"] if "甲公司" in source["raw_text"]), None)
            return json.dumps({"items": [{"item_id": "manager", "summary_value": "恶意改写", "summary_source_ids": [],
                "primary_status": "FOUND" if source else "NOT_FOUND_IN_SCOPE", "primary_value": "甲公司" if source else "",
                "primary_source_ids": [source["source_id"]] if source else [], "comparison_status": "MATCH"}]})
    provider = Provider()
    result = _run(provider, target, report)["items"][0]
    assert provider.lookup_pages == [{1}, {2}]
    assert result["summary"]["raw_text"] == "甲公司"
    assert result["status"] == "MATCH"


def test_oversized_section_unit_cannot_confirm_absence(monkeypatch):
    _install_rules(monkeypatch, [_manager()])
    target = _catalogue("product_summary", [("一、产品概况", ["基金管理人：甲公司"])])
    report = _catalogue("periodic_report", [("一、基金基本情况", ["基金管理人信息", "表内正文" * 5000])])
    sources = report["pages"][0]["sources"]
    report["reader_context"] = {"logical_units": [{"unit_id": "oversized", "kind": "table", "pages": [1],
        "source_ids": [source["source_id"] for source in sources], "markdown": "完整大表" * 5000}]}
    class Provider:
        async def generate_text(self, system, prompt):
            assert "完整大表" not in prompt
            if "字段与证据包：\n" in prompt:
                source = next(source for source in json.loads(prompt.split("字段与证据包：\n")[1])[0]["summary_candidates"] if "甲公司" in source["raw_text"])
                return json.dumps({"items": [{"item_id": "manager", "summary_value": "甲公司", "summary_source_ids": [source["source_id"]], "references": []}]})
            return '{"items":[]}'
    result = _run(Provider(), target, report)["items"][0]
    assert result["primary_lookup"]["status"] == "UNRESOLVED"
    assert result["primary_lookup"]["reason_code"] == "LOGICAL_UNIT_EXCEEDS_BUDGET"
    assert result["context_coverage"]["primary"]["skipped_units"]
    assert result["facts"]["target"]["status"] == "VERIFIED"
    assert result["fallback_lookup"]["status"] != "FOUND"


def test_request_budget_is_checked_before_contacting_provider():
    class Provider:
        async def generate_text(self, system, prompt):
            raise AssertionError("must not call model")
    with pytest.raises(ValueError, match="MODEL_CONTEXT_EXCEEDS_BUDGET"):
        asyncio.run(summary._generate_model_text(Provider(), "system", "正文" * summary.MODEL_REQUEST_CHARACTERS, stage="test", trace=None))


def test_group_context_shares_sources_and_tables_but_retains_each_items_scope():
    source = {"source_id": "shared", "raw_text": "完整章节正文" * 500, "page": 1, "bbox": [0, 0, 100, 100]}
    extra = {"source_id": "extra", "raw_text": "仅第二项可引用", "page": 2, "bbox": [0, 0, 100, 100]}
    table = {"unit_id": "table", "kind": "table", "markdown": "<table>" + "完整表格正文" * 500 + "</table>"}
    items = [{"item_id": f"field-{index}", "summary_candidates": [source] + ([extra] if index == 1 else []),
              "summary_logical_units": [table]} for index in range(8)]
    packed = summary._shared_context(items)
    assert len(packed["context_pool"]["sources"]) == 2
    assert len(packed["context_pool"]["logical_units"]) == 1
    assert packed["items"][0]["allowed_summary_source_ids"] == ["shared"]
    assert packed["items"][1]["allowed_summary_source_ids"] == ["shared", "extra"]
    assert "summary_candidates" not in packed["items"][0]
    assert len(summary._payload_batches(items, budget=20_000)) == 1
    assert summary._json_size(packed) < summary._json_size({"items": items}) / 4


def test_rule_loader_rejects_invalid_role_hints(tmp_path):
    text = (Path(summary.__file__).resolve().parents[1] / "rules/product_summary/manager.md").read_text()
    text = text.replace('section_hints_by_role = {', 'section_hints_by_role = "bad"\nignored = {', 1)
    (tmp_path / "rule.md").write_text(text)
    with pytest.raises(RuleLoadError, match="section_hints_by_role"):
        load_markdown_rules(tmp_path)


def test_unverified_conversion_cannot_fall_back_to_raw_text(monkeypatch):
    monkeypatch.setattr(summary, "build_source_catalogue", lambda *args: _catalogue("product_summary", [("产品概况", ["基金管理人：甲公司"])]))
    monkeypatch.setattr(summary, "verified_conversion", lambda content: {"status": "REQUIRES_REVIEW", "verification": {"ok": False}})
    with pytest.raises(ValueError, match="PDF_MD_REQUIRES_REVIEW"):
        summary.build_document_source_catalogue("概要.pdf", b"pdf", "product_summary")


def test_real_corpus_prescribed_scope_covers_gold_pages_and_manager_continuations():
    manifest = json.loads(Path("evaluation/cases/manifest.json").read_text())
    for code in ("000001", "510300"):
        case = next(case for case in manifest["cases"] if case["case_id"] == f"{code}_SUMMARY_PRIMARY")
        path = Path(next(document["path"] for document in case["documents"] if document["role"] == "primary_reference"))
        catalogue = summary.build_source_catalogue(path.name, path.read_bytes(), "periodic_report")
        gold = json.loads(Path(case["expected_path"]).read_text())
        rules = {rule["item_id"]: rule for rule in summary.RULES}
        for item in gold["items"]:
            evidence = item.get("primary_evidence") or {}
            if item.get("status") != "CONFIRMED" or not isinstance(evidence, dict) or not evidence.get("pdf_page"):
                continue
            candidates = summary._source_candidates(catalogue, rules[item.get("item_id", item["key"])])
            assert evidence["pdf_page"] in {source["page"] for source in candidates}
        rule = rules["manager_start_dates"]
        candidates = summary._source_candidates(catalogue, rule)
        text = summary._norm("".join(source["raw_text"] for source in candidates))
        if code == "000001":
            assert all(value in text for value in ("郑晓辉", "刘睿聪", "2024-12-26"))
            assert {5, 6}.issubset({source["page"] for source in candidates})
        else:
            assert "柳军" in text and "2012年5月4日" in text
            assert {4, 5, 6}.issubset({source["page"] for source in candidates})
