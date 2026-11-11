import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pymupdf as fitz
import pytest

from app import extraction


def test_cross_page_column_tolerance_is_independent_of_business_rule_text():
    assert extraction._cross_page_column_tolerance("没有技术参数的中文公共规则") == 0.015
    assert extraction._cross_page_column_tolerance("normalized_column_boundary_tolerance = 0.9") == 0.015
from app.rule_compiler import load_investment_rule_definitions

_, _EXECUTION_PLANS, MODULE_RULES = load_investment_rule_definitions()


def _routing_plans():
    aliases = {
        "investment.asset_allocation": ["报告期末基金资产组合情况", "期末基金资产组合情况"],
        "investment.industry_stock_portfolio": ["报告期末按行业分类的股票投资组合", "按行业分类", "境内股票投资组合", "港股通投资股票投资组合"],
        "investment.stock_holdings": [
            "期末按公允价值占基金资产净值比例排序的股票投资明细",
            "按公允价值占基金资产净值比例大小排序", "前十名股票投资明细", "前五名股票投资明细",
            "所有股票投资明细", "股票及存托凭证明细", "存托凭证投资明细",
        ],
    }
    return {
        rule["rule_id"]: {
            "rule_identity": rule["rule_id"],
            "display_title": rule["title"],
            "search_intent": {"target_description": rule["title"], "preferred_sections": aliases[rule["rule_id"]]},
            "row_matching": {"identity_headers": {}},
            "comparison": {"fields": []},
        }
        for rule in MODULE_RULES
    }


def test_numbered_child_heading_without_space_has_its_full_depth():
    assert extraction._heading_depth('12.3.4章节内容') == 3
    assert extraction._heading_depth('## 12.3 章节内容') == 2


def test_heading_alias_selects_complete_title_and_quote_ignores_source_comments():
    rules = {rule['rule_id']: Path(rule['file']).read_text(encoding='utf-8') for rule in MODULE_RULES}
    title = '5.3 报告期末按公允价值占基金资产净值比例大小排序的前十名股票投资明细'
    sentence = '该范围本期无持仓。'
    md = f'## {title}\n<!-- source-pages: 2 -->\n![原图：物理页 2](assets/page2.png)\n{sentence}\n## 5.4 债券投资\n其他内容'
    section = extraction._anydoc_sections(md, rules, _routing_plans())['investment.stock_holdings']
    assert section.startswith('## '+title)
    assert '其他内容' not in section
    claims = [{'status': 'NO_DATA', 'page': 2, 'quote': title+'\n'+sentence}]
    verified = extraction._verify_disclosures(claims, section, [{'page': 2, 'text': title+'\n'+sentence}], {2})
    assert len(verified) == 1
    warnings = []
    extraction._verify_disclosures([dict(claims[0], quote='不在原文的引句')], section,
                                  [{'page': 2, 'text': sentence}], {2}, diagnostics=warnings)
    assert '引文未在归集章节中找到' in warnings[0]


def test_metadata_dates_accept_only_contextual_omitted_year_in_valid_range():
    assert extraction._metadata_value_matches('period_end', '2024-12-31', '本报告期自2024年10月1日起至12月31日止')
    assert not extraction._metadata_value_matches('period_end', '2024-12-31', '2024年成立，12月31日送出')
    assert not extraction._metadata_value_matches('period_end', '2024-02-31', '2024年2月31日')
    assert not extraction._metadata_value_matches('period_end', '2024-01-31', '2024年12月1日至1月31日')


def test_metadata_date_evidence_accepts_chinese_numerals():
    assert extraction._metadata_value_matches(
        'disclosure_date', '2025-08-29', '报告送出日期：二〇二五年八月二十九日',
    )
    assert extraction._metadata_value_matches(
        'period_end', '2025-06-30', '本报告期自二〇二五年一月一日起至六月三十日止',
    )


def test_header_mapping_uses_a_unique_cross_field_column_assignment():
    tables = {
        1: [{
            'matrix': [
                ['序号', '项目', '金额（元）', '占基金总资产的比例（%）'],
                ['1', '权益投资', '100.00', '2.50'],
            ],
            'columns': 4,
            'page_width': 600,
            'cells_by_row': [],
        }],
    }
    aliases = {
        'investment.asset_allocation': {
            'amount': ['金额', '资产金额', '占基金总资产的比例'],
            'total_asset_ratio': ['占基金总资产的比例', '占总资产比例', '比例'],
        },
    }
    maps = extraction._header_column_maps(
        tables, aliases,
        required_fields_by_rule={'investment.asset_allocation': {'amount', 'total_asset_ratio'}},
        column_tolerance=0.015,
    )
    assert maps['investment.asset_allocation'][1][0] == {
        'amount': 2,
        'total_asset_ratio': 3,
    }


def test_period_end_fallback_requires_an_explicit_label_and_unambiguous_value():
    pages = [
        {"page": 1, "text": "本基金成立于2019年9月6日。"},
        {"page": 59, "text": "投资组合报告截止日为2025 年06 月30 日，本报告财务资料未经审计。"},
    ]
    candidate = extraction._unique_labeled_period_end(pages)
    assert candidate == {
        "page": 59,
        "quote": "投资组合报告截止日为2025 年06 月30 日",
        "value": "2025-06-30",
    }

    conflicting = pages + [{"page": 60, "text": "报告截止日：2024年12月31日。"}]
    assert extraction._unique_labeled_period_end(conflicting) is None
    assert extraction._unique_labeled_period_end([pages[0]]) is None


def test_repeated_quote_is_located_only_when_chapter_heading_disambiguates_it():
    pdf = fitz.open()
    p = pdf.new_page()
    for y, text in ((40, '1 Earlier holdings'), (60, 'No positions.'),
                    (100, '2 Later holdings'), (120, 'No positions.')):
        p.insert_text((40, y), text)
    pages = [{'page': 1, 'text': p.get_text()}]
    claim = [{'status': 'NO_DATA', 'page': 1, 'quote': 'No positions.'}]
    try:
        located = extraction._verify_disclosures(claim, '## 2 Later holdings\nNo positions.', pages, {1}, document=pdf)
        assert located[0]['evidence']['status'] == 'LOCATED'
        assert 80 < located[0]['evidence']['bbox'][1] < 100
        assert located[0]['evidence']['bbox'][3] > 120
        ambiguous = extraction._verify_disclosures(claim, 'No positions.', pages, {1}, document=pdf)
        assert ambiguous[0]['evidence']['status'] == 'QUOTE_ONLY'
    finally:
        pdf.close()


def test_repeated_disclosure_uses_child_heading_and_stops_at_next_section():
    with fitz.open() as pdf:
        page = pdf.new_page()
        for y, text in ((40, '5.2 Industry holdings'), (70, '5.2.1 Domestic holdings'),
                        (95, 'No positions.'), (125, '5.2.2 Overseas holdings'),
                        (150, 'No overseas positions.'), (180, '5.3 Stock holdings'),
                        (205, 'No positions.')):
            page.insert_text((40, y), text)
        pages = [{'page': 1, 'text': page.get_text()}]
        chapter = ('## 5.2 Industry holdings\n### 5.2.1 Domestic holdings\n'
                   '<!-- source-pages: 1 -->\nNo positions.\n'
                   '### 5.2.2 Overseas holdings\nNo overseas positions.')
        claims = [{'status': 'NO_DATA', 'page': 1, 'quote': 'No positions.',
                   'scope': {'market_scope': 'domestic'}}]
        result = extraction._verify_disclosures(claims, chapter, pages, {1}, document=pdf)
        evidence = result[0]['evidence']
        assert result[0]['scope']['market_scope'] == 'domestic'
        assert evidence['status'] == 'LOCATED'
        assert 50 < evidence['bbox'][1] < 70
        assert 95 < evidence['bbox'][3] < 125


def test_repeated_disclosure_with_two_occurrences_in_same_section_stays_ambiguous():
    with fitz.open() as pdf:
        page = pdf.new_page()
        for y, text in ((40, '5.2 Holdings'), (70, 'No positions.'), (100, 'No positions.')):
            page.insert_text((40, y), text)
        claims = [{'status': 'NO_DATA', 'page': 1, 'quote': 'No positions.'}]
        result = extraction._verify_disclosures(
            claims, '## 5.2 Holdings\nNo positions.\nNo positions.',
            [{'page': 1, 'text': page.get_text()}], {1}, document=pdf,
        )
        assert result[0]['evidence']['status'] == 'QUOTE_ONLY'
        assert result[0]['evidence']['bbox'] is None


def test_complete_sections_accept_spacing_and_title_wording_variants():
    rules = {rule['rule_id']: Path(rule['file']).read_text(encoding='utf-8') for rule in MODULE_RULES}
    markdown = '''## 12.1 期末基金资产组合情况
|项目|金额|占基金总资产的比例|
|现金|100|1.00|
## 12.2 报告期末按行业分类的股票投资组合
12.2.1报告期末按行业分类的境内股票投资组合
<!-- source-pages: 7,8 -->
<table><tr><td>代码</td><td>行业类别</td><td>公允价值</td><td>占基金资产净值比例</td></tr>
<tr><td>A</td><td>行业甲</td><td>100</td><td>1.00</td></tr></table>
## 12.3 期末按公允价值占基金资产净值比例大小排序的所有股票投资明细
<table><tr><td>股票代码</td><td>股票名称</td><td>数量</td><td>公允价值</td><td>占基金资产净值比例</td></tr>
<tr><td>123456</td><td>样本甲</td><td>100</td><td>200</td><td>1.00</td></tr></table>
## 12.4 报告期内股票投资组合的重大变动
后续章节不应混入
'''
    sections = extraction._anydoc_sections(markdown, rules, _routing_plans())
    assert '行业甲' in sections['investment.industry_stock_portfolio']
    assert '123456' in sections['investment.stock_holdings']
    assert '所有股票投资明细' in sections['investment.stock_holdings']
    assert '后续章节不应混入' not in sections['investment.stock_holdings']


def test_detached_anydoc_heading_numbers_end_the_previous_module_section():
    rules = {rule['rule_id']: Path(rule['file']).read_text(encoding='utf-8') for rule in MODULE_RULES}
    markdown = '''3
<!-- source-pages: 10 -->
报告期末按公允价值占基金资产净值比例大小排序的前十名股票投资明细
<table><tr><td>股票代码</td><td>股票名称</td><td>数量</td><td>公允价值</td><td>占基金资产净值比例</td></tr>
<tr><td>600000</td><td>浦发银行</td><td>10</td><td>100</td><td>1.00</td></tr></table>
4
<!-- source-pages: 11 -->
报告期末按债券品种分类的债券投资组合
<table><tr><td>债券品种</td><td>公允价值</td><td>比例</td></tr>
<tr><td>国债</td><td>200</td><td>2.00</td></tr></table>
'''
    section = extraction._anydoc_sections(markdown, rules, _routing_plans())['investment.stock_holdings']
    assert '600000' in section
    assert '前十名股票投资明细' in section
    assert '债券品种分类' not in section
    assert '国债' not in section


def test_inserted_document_scope_and_wrapped_heading_still_route_complete_modules():
    rules = {rule['rule_id']: Path(rule['file']).read_text(encoding='utf-8') for rule in MODULE_RULES}
    markdown = '''## 5.3 报告期末按行业分类的股票及存托凭证投资组合
### 5.3.1报告期末指数投资按行业分类的股票及存托凭证投资组合
<table><tr><td>行业类别</td><td>公允价值</td><td>比例</td></tr>
<tr><td>信息技术</td><td>100</td><td>1.00</td></tr></table>
## 5.4 报告期末按公允价值占基金资产净值比例大小排序的股票及存托凭证投资
明细
### 5.4.1期末指数投资按公允价值占基金资产净值比例大小排序的前十名股票及
存托凭证投资明细
<table><tr><td>证券代码</td><td>名称</td><td>数量</td><td>公允价值</td><td>比例</td></tr>
<tr><td>NVDA US</td><td>英伟达</td><td>10</td><td>200</td><td>2.00</td></tr></table>
## 5.5 报告期末按债券信用等级分类的债券投资组合
债券内容不得归入股票模块。
'''
    sections = extraction._anydoc_sections(markdown, rules, _routing_plans())
    industry = sections['investment.industry_stock_portfolio']
    stocks = sections['investment.stock_holdings']
    assert '存托凭证投资组合' in industry
    assert '信息技术' in industry
    assert '债券信用等级' not in industry
    assert 'NVDA US' in stocks
    assert '存托凭证投资明细' in stocks
    assert '债券信用等级' not in stocks


def test_large_source_section_is_batched_before_global_analysis_limit(monkeypatch):
    target_rule = MODULE_RULES[-1]
    monkeypatch.setattr(extraction, 'MAX_ANALYSIS_CHARS', 10000)
    monkeypatch.setattr(extraction, 'SOURCE_BATCH_CHARS', 100)
    monkeypatch.setattr(extraction, 'source_catalogue', lambda context, rule, *args: {'source': {}} if rule['rule_id'] == target_rule['rule_id'] else {})
    monkeypatch.setattr(extraction, 'source_gaps', lambda *args: [])
    monkeypatch.setattr(extraction, 'render_catalogue', lambda records: '[]')
    batched = []
    def make_batches(context, records, tables, max_chars):
        batched.append(True)
        return [{'section_headings': [], 'table': {}, 'rows': []}]
    monkeypatch.setattr(extraction, 'source_catalogue_batches', make_batches)

    class InvalidProvider:
        async def generate_text(self, *args):
            return 'not json'

    async def run():
        result = await extraction._extract_chapters(
            filename='sample.pdf', role='prospectus', provider=InvalidProvider(),
            contexts={target_rule['rule_id']: {'section_markdown': 'x' * 20000, 'physical_pages': []}},
            pages=[], rule_texts={rule['rule_id']: '' for rule in MODULE_RULES},
            tables={}, header_maps={}, candidate_numbers=set(), trace_callback=None,
            force_refresh=True, document_hash='sample', document=None,
        )
        assert batched
        assert len(result['modules']) == len(MODULE_RULES)

    asyncio.run(run())


def test_similar_heading_cannot_replace_the_asset_type_in_a_rule_title():
    alias = '期末按公允价值占基金资产净值比例大小排序的股票投资明细'
    assert not extraction._alias_spans('8.5 '+alias.replace('股票','债券'),[alias])
    assert extraction._alias_spans('8.3 '+alias.replace('股票','所有股票'),[alias])


def test_empty_stock_sections_keep_parent_and_stop_at_next_spaced_heading():
    rules = {rule['rule_id']: Path(rule['file']).read_text(encoding='utf-8') for rule in MODULE_RULES}
    markdown = '''1 报告期末基金资产组合情况
|项目|金额|占基金总资产的比例|
|现金|100|100.00|
2 报告期末按行业分类的股票投资组合
## 2.1 报告期末按行业分类的境内股票投资组合
本基金本报告期末未持有股票。
## 2.2 报告期末按行业分类的港股通投资股票投资组合
本基金本报告期末未持有港股通投资股票。
3 报告期末按公允价值占基金资产净值比例大小排序的前十名股票投资明细
<!-- source-pages: 8 -->
本基金本报告期末未持有股票。
4 报告期末按债券品种分类的债券投资组合
后续债券表格及其余文件内容
'''
    sections = extraction._anydoc_sections(markdown, rules, _routing_plans())
    industry = sections['investment.industry_stock_portfolio']
    stocks = sections['investment.stock_holdings']
    assert '境内股票投资组合' in industry
    assert '港股通投资股票投资组合' in industry
    assert '未持有股票' in stocks
    assert '债券' not in stocks
    assert '其余文件内容' not in stocks
    assert '行业分类' not in sections['investment.asset_allocation']
    assert sections['investment.asset_allocation'].count('|现金|100|100.00|') == 1


def test_empty_chapter_selection_does_not_borrow_next_chapters_table_score():
    rules = {rule['rule_id']: Path(rule['file']).read_text(encoding='utf-8') for rule in MODULE_RULES}
    markdown = '''2 报告期末按行业分类的股票投资组合
2.1 报告期末按行业分类的境内股票投资组合
本基金本报告期末未持有股票。
2.2 报告期末按行业分类的港股通投资股票投资组合
本基金本报告期末未持有港股通投资股票。
3 报告期末按公允价值占基金资产净值比例大小排序的前十名股票投资明细
本基金本报告期末未持有股票。
4 报告期末按债券品种分类的债券投资组合
'''
    markdown += '\n'.join('|国债|100|1.00|' for _ in range(25))
    section = extraction._anydoc_sections(markdown, rules, _routing_plans())['investment.industry_stock_portfolio']
    assert '境内股票投资组合' in section
    assert '港股通投资股票投资组合' in section
    assert '国债' not in section


def test_chapter_failure_cancels_pending_requests_before_returning(monkeypatch):
    state = {'cancelled': 0, 'pending': 0}
    failed_rule = MODULE_RULES[-1]['rule_id']
    monkeypatch.setattr(extraction, 'MAX_ANALYSIS_CHARS', 10000)
    monkeypatch.setattr(extraction, 'source_catalogue', lambda *args: [])
    monkeypatch.setattr(extraction, 'source_gaps', lambda *args: [])

    class WaitingProvider:
        async def generate_text(self, *args):
            state['pending'] += 1
            try:
                await asyncio.Future()
            finally:
                state['pending'] -= 1
                state['cancelled'] += 1

    async def run():
        import pytest
        with pytest.raises(ValueError, match='PDF_ANALYSIS_LIMIT_EXCEEDED'):
            await extraction._extract_chapters(
                filename='sample.pdf', role='prospectus', provider=WaitingProvider(),
                contexts={failed_rule: {'section_markdown': 'x' * 20000, 'physical_pages': []}},
                pages=[], rule_texts={r['rule_id']: '' for r in MODULE_RULES},
                tables={}, header_maps={}, candidate_numbers=set(), trace_callback=None,
                force_refresh=True, document_hash='sample', document=None,
            )
        assert state['pending'] == 0
        assert state['cancelled'] == len(MODULE_RULES) - 1

    asyncio.run(run())


def test_unlocated_row_is_not_accepted_by_a_module_specific_exception(monkeypatch):
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
        rule_text=Path(rule["file"]).read_text(encoding="utf-8"),
        source_pages=set(),
        section_markdown="",
        table_cache={},
        header_maps={},
    )

    assert result["status"] == "UNRESOLVED_STRUCTURE"
    assert result["rows"][0]["row_evidence"]["status"] == "QUOTE_ONLY"


def test_row_location_can_be_recovered_from_verified_cells_on_the_same_source_row():
    pages = [{"width": 600, "height": 800}]
    table_cache = {1: [{
        "matrix": [["合计", "100", "2.0"]],
        "cells_by_row": [[[10, 20, 100, 40], [100, 20, 300, 40], [300, 20, 400, 40]]],
    }]}
    fields = {
        "amount": {"evidence": {"status": "LOCATED", "page": 1,
                                  "source_ref": {"table": 0, "row": 0, "mapping": {"amount": 1}}}},
        "ratio": {"evidence": {"status": "LOCATED", "page": 1,
                                 "source_ref": {"table": 0, "row": 0, "mapping": {"ratio": 2}}}},
    }

    evidence = extraction._row_evidence_from_field_cells(fields, "合计", pages, table_cache)

    assert evidence is not None
    assert evidence["status"] == "LOCATED"
    assert evidence["bbox"] == [10.0, 20.0, 400.0, 40.0]
    assert evidence["source_ref_recovered"] == "shared_verified_field_row"
    assert extraction._row_evidence_from_field_cells(fields, "其他项目", pages, table_cache) is None

    fields["ratio"]["evidence"]["source_ref"] = {"table": 0, "row": 1, "mapping": {"ratio": 2}}
    assert extraction._row_evidence_from_field_cells(fields, "合计", pages, table_cache) is None


def test_amount_unit_in_nearby_table_context_is_inherited_from_the_mapped_header(monkeypatch):
    table = {
        "matrix": [
            ["序号", "项目", "金额", "占基金总资产的比例（%）"],
            ["9", "合计", "100.00", "100.00"],
        ],
        "columns": 4,
        "cells_by_row": [[None] * 4, [None] * 4],
        "page_width": 600,
        "unit_context": "金额单位：人民币元",
    }
    maps = extraction._header_column_maps(
        {1: [table]},
        {"investment.sample": {
            "row_key": ["项目"], "amount": ["金额"], "total_assets_ratio": ["比例"],
        }},
        required_fields_by_rule={"investment.sample": {"amount", "total_assets_ratio"}},
        column_tolerance=0.015,
    )

    assert maps["investment.sample"][1][0] == {"row_key": 1, "amount": 2, "total_assets_ratio": 3}
    assert table["column_units"]["investment.sample"] == {
        "row_key": "", "amount": "元", "total_assets_ratio": "%",
    }
    monkeypatch.setattr(extraction, "_evidence", lambda *args, **kwargs: {"status": "LOCATED"})

    result = extraction._verify_field(
        document=None, pages=[], value={
            "raw_text": "100.00", "page": 1, "quote": "9 合计 100.00 100.00",
            "source_ref": {"table": 0, "row": 1},
        }, field_name="amount", rule_id="investment.sample", row_label="合计",
        table_cache={1: [table]}, header_maps=maps,
    )

    assert result["unit"] == "元"
    assert result["unit_source"] == "mapped_table_header"


def test_table_unit_caption_is_found_when_pymupdf_merges_it_into_table_text_block():
    table = SimpleNamespace(
        bbox=(50, 150, 500, 300), col_count=3,
        rows=[SimpleNamespace(cells=[None, None, None])],
        extract=lambda: [["项目", "金额", "比例"]],
    )

    class FakePage:
        rect = SimpleNamespace(width=600, height=800)

        def find_tables(self):
            return SimpleNamespace(tables=[table])

        def get_text(self, kind):
            if kind == "dict":
                # Mimic PyMuPDF's merged block: the caption line is above the
                # table, while the same block's combined bbox extends below it.
                return {"blocks": [{"bbox": (80, 135, 520, 330), "lines": [
                    {"bbox": (300, 132, 480, 145), "spans": [
                        {"text": "金额单位：人民币元"},
                    ]},
                ]}]}
            if kind == "blocks":
                return [(80, 135, 520, 330, "金额单位：人民币元 表格内容")]
            raise AssertionError(kind)

    cache = extraction._collect_tables([FakePage()], {1})

    assert cache[1][0]["unit_context"] == "金额单位：人民币元"


def test_source_backed_asset_row_keeps_generated_semantic_key_without_static_registry(monkeypatch):
    rule = next(item for item in MODULE_RULES if item["rule_id"] == "investment.asset_allocation")
    monkeypatch.setattr(extraction, "_verify_field", lambda *args, **kwargs: {
        "raw_text": "-", "normalized_value": None, "value_kind": "DASH", "unit": "元",
        "evidence": {"status": "LOCATED"},
    })
    monkeypatch.setattr(extraction, "_evidence", lambda *args, **kwargs: {"status": "LOCATED"})

    result = extraction._verify_module(
        document=None, pages=[],
        raw_module={"found": True, "scope": {}, "rows": [{
            "row_key": "unmapped_row", "row_label": "基金投资", "row_page": 1,
            "row_quote": "基金投资 - -", "fields": {field: "-" for field in rule["fields"]},
        }]},
        rule=rule, rule_text=Path(rule["file"]).read_text(encoding="utf-8"), source_pages=set(),
        section_markdown="", table_cache={}, header_maps={},
    )

    assert result["status"] == "EXTRACTED"
    assert result["rows"][0]["label"] == "基金投资"
    assert result["rows"][0]["row_key"] == "unmapped_row"


def test_semantic_scope_and_hierarchy_values_are_not_required_to_appear_in_source_row_text(monkeypatch):
    base_rule = next(item for item in MODULE_RULES if item["rule_id"] == "investment.stock_holdings")
    rule = extraction._rule_for_execution_plan(base_rule, _EXECUTION_PLANS[base_rule["rule_id"]])
    rule["row_matching"] = {**rule["row_matching"], "hierarchy_fields": [
        *rule["row_matching"].get("hierarchy_fields", []), "investment_subtable",
    ]}
    monkeypatch.setattr(extraction, "_verify_field", lambda *args, **kwargs: {
        "raw_text": "1", "normalized_value": "1", "value_kind": "NUMBER", "unit": "",
        "evidence": {"status": "LOCATED"},
    })
    monkeypatch.setattr(extraction, "_evidence", lambda *args, **kwargs: {"status": "LOCATED"})
    raw_row = {
        "row_key": "stock_600519", "row_label": "贵州茅台", "row_page": 1,
        "row_quote": "600519 贵州茅台 100 200 0.1",
        "security_code": "600519", "security_name": "贵州茅台",
        "market_scope": "all", "investment_scope": "index", "investment_subtable": "index",
        "fields": {field: {"raw_text": "1", "page": 1, "quote": "贵州茅台 1"} for field in rule["fields"]},
    }

    result = extraction._verify_module(
        document=None, pages=[{"page": 1, "text": raw_row["row_quote"]}],
        raw_module={"found": True, "scope": {}, "rows": [raw_row]},
        rule=rule, rule_text=Path(rule["file"]).read_text(encoding="utf-8"), source_pages={1},
        section_markdown=raw_row["row_quote"], table_cache={}, header_maps={},
    )

    assert result["status"] == "EXTRACTED"
    for scope_field in rule["row_matching"]["scope_fields"]:
        assert result["rows"][0][scope_field] == raw_row[scope_field]
    assert result["rows"][0]["investment_subtable"] == "index"


def test_verified_no_data_disclosure_is_valid_without_table_rows():
    rule = next(item for item in MODULE_RULES if item["rule_id"] == "investment.stock_holdings")
    quote = "本基金本报告期末未持有股票。"
    result = extraction._verify_module(
        document=None, pages=[{"page": 8, "text": quote}],
        raw_module={"found": True, "rows": [], "scope": {}, "disclosures": [{
            "status": "NO_DATA", "period_end": "2025-12-31",
            "scope": {"portfolio": "equity holdings"}, "page": 8, "quote": quote,
        }]},
        rule=rule, rule_text=Path(rule["file"]).read_text(encoding="utf-8"),
        source_pages={8}, section_markdown=quote, table_cache={}, header_maps={},
    )
    assert result["status"] == "EXTRACTED"
    assert result["rows"] == []
    assert result["disclosures"][0]["quote"] == quote


def test_unverifiable_no_data_disclosure_does_not_clear_review_state():
    rule = next(item for item in MODULE_RULES if item["rule_id"] == "investment.stock_holdings")
    result = extraction._verify_module(
        document=None, pages=[{"page": 8, "text": "这是有数据的章节。"}],
        raw_module={"found": True, "rows": [], "scope": {}, "disclosures": [{
            "status": "NO_DATA", "period_end": "2025-12-31",
            "scope": {"portfolio": "equity holdings"}, "page": 8, "quote": "没有持仓",
        }]},
        rule=rule, rule_text=Path(rule["file"]).read_text(encoding="utf-8"),
        source_pages={8}, section_markdown="这是有数据的章节。", table_cache={}, header_maps={},
    )
    assert result["status"] == "UNRESOLVED_STRUCTURE"
    assert result["disclosures"] == []


def test_invalid_table_row_reference_recovers_only_unique_mapped_cell():
    document = fitz.open()
    page = document.new_page()
    page.insert_text((40, 40), "基金投资 -")
    pages = [{"text": "基金投资 -", "width": float(page.rect.width), "height": float(page.rect.height)}]
    tables = {1: [{
        "matrix": [["项目", "金额（元）"], ["基金投资", "-"]],
        "cells_by_row": [[None, None], [None, [100.0, 100.0, 180.0, 120.0]]],
    }]}
    maps = {"investment.asset_allocation": {1: {0: {"amount": 1}}}}

    evidence = extraction._evidence(
        document, pages,
        {"page": 1, "quote": "基金投资 -", "raw_text": "-", "source_ref": {"table": 0, "row": 99}},
        table_cache=tables, header_maps=maps, rule_id="investment.asset_allocation",
        row_label="基金投资", field_key="amount",
    )

    assert evidence["status"] == "LOCATED"
    assert evidence["bbox"] == [100.0, 100.0, 180.0, 120.0]
    assert evidence["source_ref_recovered"] == "unique_row_column_value"
    document.close()


def test_cross_page_section_keeps_subheadings_until_next_peer_module():
    rules = {rule["rule_id"]: Path(rule["file"]).read_text(encoding="utf-8") for rule in MODULE_RULES}
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

    sections = extraction._anydoc_sections(markdown, rules, _routing_plans())

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
    rule_texts = {rule["rule_id"]: Path(rule["file"]).read_text(encoding="utf-8")}
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
    rules = {rule["rule_id"]: Path(rule["file"]).read_text(encoding="utf-8") for rule in MODULE_RULES}
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

    sections = extraction._anydoc_sections(markdown, rules, _routing_plans())
    section = sections["investment.stock_holdings"]

    assert "600519" in section
    assert "5.3.1" in section
    assert "目录" not in section
    assert "后续章节内容" not in section


def test_body_mention_of_section_alias_does_not_absorb_later_chapters():
    rules = {rule["rule_id"]: Path(rule["file"]).read_text(encoding="utf-8") for rule in MODULE_RULES}
    markdown = """本基金投资组合报告等内容详见下文，数据截至本报告期末。
<!-- source-pages: 50 -->
# 1 报告期末基金资产组合情况
|序号|项目|金额|比例|
|1|现金|100|1.00|
# 2 报告期债券回购融资情况
此处是另一个完整章节。
"""

    section = extraction._anydoc_sections(markdown, rules, _routing_plans())["investment.asset_allocation"]

    assert "# 1 报告期末基金资产组合情况" in section
    assert "|1|现金|100|1.00|" in section
    assert "本基金投资组合报告等内容" not in section
    assert "# 2 报告期债券回购融资情况" not in section
    assert "另一个完整章节" not in section


def test_specific_module_heading_wins_over_generic_parent_report_heading():
    rules = {rule["rule_id"]: Path(rule["file"]).read_text(encoding="utf-8") for rule in MODULE_RULES}
    markdown = """# §7 投资组合报告
<!-- source-pages: 20 -->
## 7.1 期末基金资产组合情况
|序号|项目|金额|比例|
|1|固定收益投资|100|100.00|
## 7.2 债券品种投资组合
|债券品种|金额|
|国债|99|
"""
    section = extraction._anydoc_sections(markdown, rules, _EXECUTION_PLANS)["investment.asset_allocation"]
    assert "固定收益投资" in section
    assert "债券品种投资组合" not in section
    assert "国债" not in section


def test_analyze_document_scopes_requests_and_never_accepts_false_missing_source_rows():
    pdf_path = Path("assets/易方达上证50交易型开放式指数证券投资基金2025年第4季度报告.pdf")

    class RecordingProvider:
        prompts = []

        async def generate_text(self, system_prompt, user_prompt):
            self.prompts.append(user_prompt)
            return json.dumps({"found": False, "rows": []})

    provider = RecordingProvider()
    result = asyncio.run(extraction.analyze_document(
        filename=pdf_path.name, content=pdf_path.read_bytes(), role="periodic_report", provider=provider,
    ))
    assert len(MODULE_RULES) <= len(provider.prompts) <= 2 * len(MODULE_RULES)
    for rule in MODULE_RULES:
        matching = [prompt for prompt in provider.prompts if f"模块：{rule['rule_id']}。" in prompt]
        assert 1 <= len(matching) <= 2
        assert all(Path(rule["file"]).read_text(encoding="utf-8") in prompt for prompt in matching)
        assert all("## selector" not in prompt for prompt in matching)
    assert all(module["status"] == "UNRESOLVED_STRUCTURE" for module in result["modules"])


def test_active_execution_plan_is_the_source_of_table_field_and_header_contract():
    plan = {
        "rule_identity": "synthetic.example",
        "display_title": "示例核对项",
        "row_matching": {"identity_headers": {"asset_type": ["资产类别", "类别"]}},
        "comparison": {"fields": [{
            "field": "adjusted_rate", "label": "适用费率", "source_headers": ["适用费率", "费率"],
        }]},
    }
    rule = {"rule_id": "legacy.id", "title": "旧标题", "fields": {"legacy_amount": "金额"}}

    assert extraction._header_aliases(plan) == {
        "asset_type": ["资产类别", "类别"], "adjusted_rate": ["适用费率", "费率"],
    }
    adapted = extraction._rule_for_execution_plan(rule, plan)
    assert adapted["rule_id"] == "synthetic.example"
    assert adapted["title"] == "示例核对项"
    assert adapted["fields"] == {"adjusted_rate": "适用费率"}
    assert rule["fields"] == {"legacy_amount": "金额"}
    table_maps = extraction._header_column_maps(
        {4: [{"columns": 2, "matrix": [["资产类别", "适用费率"], ["现金", "0.20"]]}]},
        {plan["rule_identity"]: extraction._header_aliases(plan)},
        required_fields_by_rule={plan["rule_identity"]: set(adapted["fields"])},
        column_tolerance=0.015,
    )
    assert table_maps["synthetic.example"][4][0] == {"asset_type": 0, "adjusted_rate": 1}


def test_active_table_semantics_require_a_validated_execution_plan():
    with pytest.raises(ValueError, match="RULE_EXECUTION_PLAN_REQUIRED"):
        extraction._header_aliases(None)
    with pytest.raises(ValueError, match="RULE_EXECUTION_PLAN_REQUIRED"):
        extraction._rule_for_execution_plan({"rule_id": "synthetic.example"}, None)



def test_table_cell_evidence_can_validate_a_quote_that_skips_an_intervening_cell():
    pdf_path = Path("assets/易方达上证50交易型开放式指数证券投资基金更新的招募说明书.pdf")
    document, pages = extraction._read_pdf(pdf_path.read_bytes(), pdf_path.name)
    rule = next(item for item in MODULE_RULES if item["rule_id"] == "investment.industry_stock_portfolio")
    rule_text = Path(rule["file"]).read_text(encoding="utf-8")
    table_cache = extraction._collect_tables(document, {55})
    header_maps = extraction._header_column_maps(
        table_cache,
            {rule["rule_id"]: extraction._header_aliases(_EXECUTION_PLANS[rule["rule_id"]])},
        required_fields_by_rule={rule["rule_id"]: set(rule["fields"])},
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
                    field_key=next(
                        field for field, label in rule["fields"].items() if "比例" in label
                    ),
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
                    rule["rule_id"]: extraction._header_aliases(_EXECUTION_PLANS[rule["rule_id"]])
                for rule in MODULE_RULES
            }
            header_maps = extraction._header_column_maps(
                table_cache,
                aliases,
                required_fields_by_rule={rule["rule_id"]: set(rule["fields"]) for rule in MODULE_RULES},
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
