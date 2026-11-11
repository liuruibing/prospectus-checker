import copy

from app.summary_check import RULES, _context_plan, _merge_fallback_contexts, _prescribed_sections, _source_candidates
from app.summary_rules import load_markdown_rules


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
            "page_dimensions": [], "source_count": len(sources), "sources": sources, "chapters": chapters,
            "pages": [{"page": index + 1, "sources": [source for source in sources if source["page"] == index + 1]}
                      for index in range(len(sections))]}


def _rule_with_sections(role, preferred):
    rule = copy.deepcopy(RULES[0])
    plan = rule["execution_plan"]
    plan["search_intent"]["preferred_sections_by_role"][role] = preferred
    rule["preferred_sections_by_role"] = plan["search_intent"]["preferred_sections_by_role"]
    return rule


def test_source_routing_uses_compiled_rule_sections_and_does_not_search_unrelated_mentions():
    catalogue = _catalogue("periodic_report", [
        ("基金运作相关费用", ["审计费用：55,000元", "补充说明"]),
        ("风险说明", ["审计费用可能由管理人承担", "无关正文"]),
    ])
    rule = _rule_with_sections("periodic_report", ["基金运作相关费用"])
    candidates = _source_candidates(catalogue, rule)
    assert {source["page"] for source in candidates} == {1}
    assert "补充说明" in {source["raw_text"] for source in candidates}


def test_source_discovery_matches_short_table_labels_inside_long_rule_descriptions():
    catalogue = _catalogue("product_summary", [
        ("三、投资本基金涉及的费用", [
            "管理费",
            "年费率 0.15%",
            "基金管理人和销售机构",
            "托管费",
            "年费率 0.05%",
            "基金托管人",
        ]),
    ])
    rule = next(copy.deepcopy(item) for item in RULES if item["item_id"] == "management_fee")
    plan = rule["execution_plan"]
    plan["search_intent"]["preferred_sections_by_role"]["product_summary"] = ["不存在的精确章节"]
    plan["search_intent"]["preferred_sections"] = []

    candidates = _source_candidates(catalogue, rule)

    assert {"管理费", "年费率 0.15%", "基金管理人和销售机构"} <= {
        source["raw_text"] for source in candidates
    }
    assert {"托管费", "年费率 0.05%", "基金托管人"} <= {
        source["raw_text"] for source in candidates
    }


def test_prescribed_sections_are_taken_from_the_active_execution_plan():
    catalogue = _catalogue("prospectus", [
        ("费用与税收", ["管理费按年费率计提"]),
        ("基金管理人职责", ["管理人依法履行职责"]),
    ])
    rule = _rule_with_sections("prospectus", ["费用与税收"])
    sections = _prescribed_sections(catalogue, rule)
    assert [section["title"] for section in sections] == ["费用与税收"]


def test_prose_section_hint_routes_to_similarly_worded_document_heading():
    catalogue = _catalogue("prospectus", [
        ("四、与基金财产管理、运用有关费用的提取、支付方式与比例", ["费用条款"]),
        ("（二）基金费用计提方法、计提标准和支付方式", ["基金管理人的管理费按年费率计提"]),
        ("二十、基金的风险揭示", ["基金管理费可能影响投资回报"]),
    ])
    rule = _rule_with_sections("prospectus", [
        "基金费用计提方法、计提标准和支付方式，或基金财产管理相关费用提取与支付比例的章节",
    ])

    sections = _prescribed_sections(catalogue, rule)

    titles = {section["title"] for section in sections}
    assert "（二）基金费用计提方法、计提标准和支付方式" in titles
    assert "二十、基金的风险揭示" not in titles


def test_long_prose_hint_routes_when_it_contains_the_substantive_heading():
    catalogue = _catalogue("prospectus", [
        ("二十五、其他应披露事项", ["基金合同修订及费率调整公告"]),
        ("管理人职责", ["管理人依法履行职责"]),
    ])
    rule = _rule_with_sections("prospectus", [
        "其他应披露事项或含义相近的公告目录、事项清单中涉及费率调整或基金合同修订的条目",
    ])

    sections = _prescribed_sections(catalogue, rule)

    assert [section["title"] for section in sections] == ["二十五、其他应披露事项"]


def test_all_applicable_rule_sections_are_routed_by_default():
    catalogue = _catalogue("prospectus", [
        ("基金费用计提方法与标准", ["管理费按年费率计提"]),
        ("其他应披露事项", ["基金费率调整公告"]),
        ("风险揭示", ["市场风险说明"]),
    ])
    rule = _rule_with_sections("prospectus", ["基金费用计提方法与标准", "其他应披露事项"])

    sections = _prescribed_sections(catalogue, rule)

    assert [section["title"] for section in sections] == ["基金费用计提方法与标准", "其他应披露事项"]


def test_fallback_contexts_merge_related_chapters_for_each_item():
    parts = [
        {"item_id": "rule-a", "fallback_candidates": [{"source_id": "fee"}],
         "logical_units": [{"unit_id": "fee-unit"}], "coverage": {"batch_count": 2}},
        {"item_id": "rule-a", "fallback_candidates": [{"source_id": "notice"}],
         "logical_units": [{"unit_id": "notice-unit"}], "coverage": {"batch_count": 2}},
        {"item_id": "rule-b", "fallback_candidates": [{"source_id": "other"}],
         "logical_units": [{"unit_id": "other-unit"}], "coverage": {"batch_count": 1}},
    ]

    merged, oversized = _merge_fallback_contexts(parts)

    assert not oversized
    assert [item["item_id"] for item in merged] == ["rule-a", "rule-b"]
    assert {source["source_id"] for source in merged[0]["fallback_candidates"]} == {"fee", "notice"}
    assert {unit["unit_id"] for unit in merged[0]["logical_units"]} == {"fee-unit", "notice-unit"}


def test_context_plan_carries_entire_selected_section_without_silent_truncation():
    catalogue = _catalogue("periodic_report", [("投资说明", [f"内容第{i}行" for i in range(200)])])
    rule = _rule_with_sections("periodic_report", ["投资说明"])
    candidates = _source_candidates(catalogue, rule)
    plan = _context_plan(catalogue, candidates, item=rule)
    sent_ids = {source["source_id"] for batch in plan["batches"] for source in batch["candidates"]}
    assert plan["coverage_complete"]
    assert sent_ids == {source["source_id"] for source in candidates}


def test_compiled_plan_is_required_for_new_summary_tasks():
    _, rules = load_markdown_rules(require_execution_plans=True)
    assert len(rules) == 16
    assert all(rule["execution_plan"]["search_intent"]["include_continuations"] for rule in rules)
    assert all(rule["preferred_sections_by_role"] for rule in rules)
