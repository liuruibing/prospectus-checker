import asyncio
import json
from pathlib import Path

from app import extraction
from app.domain import MODULE_RULES


def test_industry_total_accepts_located_metrics_without_row_bbox(monkeypatch):
    rule = next(item for item in MODULE_RULES if item["rule_id"] == "investment.industry_stock_portfolio")
    located_value = {
        "raw_text": "100",
        "normalized_value": "100",
        "value_kind": "NUMBER",
        "unit": "元",
        "evidence": {"status": "LOCATED"},
    }
    monkeypatch.setattr(extraction, "_verify_field", lambda *args, **kwargs: dict(located_value))
    monkeypatch.setattr(extraction, "_evidence", lambda *args, **kwargs: {"status": "QUOTE_ONLY"})

    result = extraction._verify_module(
        document=None,
        pages=[],
        raw_module={
            "found": True,
            "scope": {"market_scope": "domestic"},
            "rows": [{
                "row_label": "合计",
                "row_quote": "合计",
                "row_page": 1,
                "market_scope": "domestic",
                "fields": {"fair_value": "100", "ratio_of_net_assets": "2.0"},
            }],
        },
        rule=rule,
        table_cache={},
        header_maps={},
    )

    assert result["status"] == "EXTRACTED"
    assert result["rows"][0]["row_evidence"]["status"] == "QUOTE_ONLY"


def test_cross_page_section_keeps_subheadings_until_next_peer_module():
    rules = {rule["rule_id"]: rule["file"].read_text(encoding="utf-8") for rule in MODULE_RULES}
    markdown = """5.1 报告期末基金资产组合情况
|权益投资|100|50|
5.2 报告期末按行业分类的股票投资组合
5.2.1 报告期末按行业分类的境内股票投资组合
|制造业|80|40|
5.3 期末按公允价值占基金资产净值比例大小排序的股票投资明细
5.3.1 报告期末前十名股票投资明细
|600519|贵州茅台|10|50|25|
5.4 其他报告章节
"""

    sections = extraction._anydoc_sections(markdown, rules)

    assert "|制造业|80|40|" in sections["investment.industry_stock_portfolio"]
    assert "5.2.1" in sections["investment.industry_stock_portfolio"]
    assert "5.3.1" in sections["investment.stock_holdings"]
    assert "|600519|贵州茅台|10|50|25|" in sections["investment.stock_holdings"]
    assert "5.4" not in sections["investment.stock_holdings"]


def test_anydoc_page_footer_includes_following_physical_continuation_page():
    pages = [
        {"page": 1, "text": "表格上半页\n第 8 页共 14 页"},
        {"page": 2, "text": "表格续页"},
        {"page": 3, "text": "其他页\n第 10 页共 14 页"},
    ]

    assert extraction._physical_pages_in_markdown(
        "表格行\n第 8 页共 14 页\n续表行", pages
    ) == {1, 2}


def test_chapter_context_aggregates_multi_page_content_and_keeps_page_sources():
    rule = next(item for item in MODULE_RULES if item["rule_id"] == "investment.asset_allocation")
    rule_texts = {rule["rule_id"]: rule["file"].read_text(encoding="utf-8")}
    pages = [
        {
            "page": 1,
            "text": "报告期末基金资产组合情况\n第 8 页共 14 页",
            "blocks": [{"bbox": [10.0, 20.0, 100.0, 40.0], "text": "权益投资 100 50"}],
        },
        {
            "page": 2,
            "text": "买断式回购的买入返售金融资产 - -",
            "blocks": [{"bbox": [10.0, 20.0, 100.0, 60.0], "text": "买断式回购的买入返售金融资产 - -"}],
        },
        {
            "page": 3,
            "text": "目录：报告期末基金资产组合情况 其他章节",
            "blocks": [{"bbox": None, "text": "目录：报告期末基金资产组合情况 其他章节"}],
        },
    ]

    contexts = extraction._chapter_contexts(
        {rule["rule_id"]: "报告期末基金资产组合情况\n权益投资 100 50\n第 8 页共 14 页\n买断式回购的买入返售金融资产 - -"},
        pages,
        rule_texts,
    )
    rendered = extraction._format_chapter_contexts(contexts)

    assert [page["physical_page"] for page in contexts[rule["rule_id"]]["physical_pages"]] == [1, 2]
    assert "买断式回购的买入返售金融资产 - -" in contexts[rule["rule_id"]]["section_markdown"]
    assert rendered.index("[章节归集内容") < rendered.index("[章节对应的 PDF 原文区块")
    assert "[PHYSICAL PAGE 1]" in rendered
    assert "[PHYSICAL PAGE 2]" in rendered
    assert "其他章节" not in rendered


def test_anydoc_section_prefers_body_over_contents_and_stops_at_next_peer_heading():
    rules = {rule["rule_id"]: rule["file"].read_text(encoding="utf-8") for rule in MODULE_RULES}
    markdown = """目录
5.3 期末按公允价值占基金资产净值比例大小排序的股票投资明细 ........ 56
正文
5.3 期末按公允价值占基金资产净值比例大小排序的股票投资明细
**5.3.1** 报告期末按公允价值占基金资产净值比例大小排序的前十名股票投资明细
|序号|股票代码|股票名称|数量|公允价值|比例|
|---|---|---|---|---|---|
|1|600519|贵州茅台|10|100|1.0|
5.4 报告期末按债券品种分类的债券投资组合
后续章节内容
"""

    sections = extraction._anydoc_sections(markdown, rules)
    section = sections["investment.stock_holdings"]

    assert "600519" in section
    assert "5.3.1" in section
    assert "目录" not in section
    assert "后续章节内容" not in section


def test_analyze_document_sends_aggregated_chapter_and_page_sources_to_provider():
    pdf_path = Path("assets/易方达上证50交易型开放式指数证券投资基金2025年第4季度报告.pdf")
    raw = {
        "document_info": {},
        "modules": [
            {"rule_id": rule["rule_id"], "found": False, "scope": {}, "rows": []}
            for rule in MODULE_RULES
        ],
    }

    class RecordingProvider:
        user_prompt = ""

        async def generate_text(self, system_prompt, user_prompt):
            self.user_prompt = user_prompt
            return json.dumps(raw, ensure_ascii=False)

    provider = RecordingProvider()
    asyncio.run(extraction.analyze_document(
        filename=pdf_path.name,
        content=pdf_path.read_bytes(),
        role="periodic_report",
        provider=provider,
    ))

    prompt = provider.user_prompt
    assert "【章节：investment.asset_allocation】" in prompt
    assert "[章节归集内容，包含跨页正文、表格和表注" in prompt
    assert "买断式回购的买入返售金融资产" in prompt
    assert "【章节：investment.industry_stock_portfolio】" in prompt
    assert "[PHYSICAL PAGE 8]" in prompt
    assert "[PHYSICAL PAGE 9]" in prompt




def test_table_cell_evidence_can_validate_a_quote_that_skips_an_intervening_cell():
    pdf_path = Path("assets/易方达上证50交易型开放式指数证券投资基金更新的招募说明书.pdf")
    document, pages = extraction._read_pdf(pdf_path.read_bytes(), pdf_path.name)
    rule = next(item for item in MODULE_RULES if item["rule_id"] == "investment.industry_stock_portfolio")
    rule_text = Path(rule["file"]).read_text(encoding="utf-8")
    table_cache = extraction._collect_tables(document, {55})
    header_maps = extraction._header_column_maps(
        table_cache,
        {rule["rule_id"]: extraction._header_aliases(rule_text)},
        column_tolerance=extraction._cross_page_column_tolerance(
            extraction.COMMON_RULE_FILE.read_text(encoding="utf-8")
        ),
    )
    try:
        evidence = extraction._evidence(
            document,
            pages,
            {
                "page": 55,
                "quote": "G 交通运输、仓储和邮政业 \n2.62",
                "raw_text": "2.62",
            },
            table_cache=table_cache,
            header_maps=header_maps,
            rule_id=rule["rule_id"],
            row_label="交通运输、仓储和邮政业",
            field_key="ratio_of_net_assets",
        )
    finally:
        document.close()

    assert evidence["status"] == "LOCATED"
    assert evidence["bbox"] == [444.92, 687.22, 521.85, 708.24]


def test_table_row_evidence_uses_unique_structured_row_when_quote_is_reordered():
    cases = [
        (
            "assets/易方达上证50交易型开放式指数证券投资基金更新的招募说明书.pdf",
            55,
            "investment.asset_allocation",
            "其中：买断式回购的买入返售金融资产",
            "其中：买断式回购的买入返售金 - - 融资产",
            [74.7, 319.41, 521.85, 362.2],
        ),
        (
            "assets/易方达上证50交易型开放式指数证券投资基金2025年第4季度报告.pdf",
            9,
            "investment.asset_allocation",
            "其中：买断式回购的买入返售金融资产",
            "其中：买断式回购的买入返售\n- \n- \n金融资产",
            [75.22, 72.19, 522.12, 120.98],
        ),
        (
            "assets/易方达上证50交易型开放式指数证券投资基金2025年第4季度报告.pdf",
            9,
            "investment.industry_stock_portfolio",
            "电力、热力、燃气及水生产和供应业",
            "电力、热力、燃气及水生产和供应\n213,673,058.20\n4.49 \nD \n业",
            [82.82, 381.05, 522.12, 422.33],
        ),
    ]
    rule_texts = {rule["rule_id"]: Path(rule["file"]).read_text(encoding="utf-8") for rule in MODULE_RULES}

    for pdf, page_number, rule_id, row_label, quote, expected_bbox in cases:
        path = Path(pdf)
        document, pages = extraction._read_pdf(path.read_bytes(), path.name)
        try:
            table_pages = {page_number}
            if page_number > 1:
                table_pages.add(page_number - 1)
            table_cache = extraction._collect_tables(document, table_pages)
            aliases = {
                rule["rule_id"]: extraction._header_aliases(rule_texts[rule["rule_id"]])
                for rule in MODULE_RULES
            }
            header_maps = extraction._header_column_maps(
                table_cache,
                aliases,
                column_tolerance=extraction._cross_page_column_tolerance(
                    extraction.COMMON_RULE_FILE.read_text(encoding="utf-8")
                ),
            )

            evidence = extraction._evidence(
                document,
                pages,
                {"page": page_number, "quote": quote, "raw_text": row_label},
                table_cache=table_cache,
                header_maps=header_maps,
                rule_id=rule_id,
                row_label=row_label,
            )
        finally:
            document.close()

        assert evidence["status"] == "LOCATED"
        assert evidence["bbox"] == expected_bbox
