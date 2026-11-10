import pymupdf
import pytest
from pathlib import Path

from app.pdf_markdown import (assemble, convert_pdf_bytes, extract_layout,
                              render_markdown, table_heading, verify_markdown)


def sample_pdf(new_heading=False):
    with pymupdf.open() as pdf:
        first = pdf.new_page()
        xs = [50, 200, 350, 500]
        def grid(page, ys, values):
            for y in ys:
                page.draw_line((xs[0], y), (xs[-1], y))
            for x in xs:
                page.draw_line((x, ys[0]), (x, ys[-1]))
            for row, texts in enumerate(values):
                for col, text in enumerate(texts):
                    page.insert_text((xs[col]+5, ys[row]+15), text)
        first.insert_text((50, 50), "7.1 Asset allocation")
        grid(first, [700, 725, 780], [["Item", "Amount", "Ratio"], ["Cash", "100.00", "100.00"]])
        second = pdf.new_page()
        if new_heading:
            second.insert_text((50, 40), "7.2 Another table")
        grid(second, [70, 100], [["Total", "100.00", "100.00"]])
        return pdf.tobytes()


def test_single_row_continuation_is_part_of_one_table_and_values_survive():
    result = convert_pdf_bytes(sample_pdf(), include_assets=False)
    tables = [b for b in result["source_map"]["blocks"] if b["kind"] == "table"]
    assert len(tables) == 1
    assert tables[0]["pages"] == [1, 2]
    assert len(tables[0]["rows"]) == 3
    segments = result["source_map"]["segments"]
    assert ["".join(segments[s]["text"] for s in c["segments"]).strip() for c in tables[0]["rows"][-1]] == ["Total", "100.00", "100.00"]
    assert result["verification"]["ok"]
    assert result["verification"]["source_characters"] == result["verification"]["output_characters"]


def test_new_section_prevents_merging_two_similar_tables():
    result = convert_pdf_bytes(sample_pdf(new_heading=True), include_assets=False)
    assert len([b for b in result["source_map"]["blocks"] if b["kind"] == "table"]) == 2
    assert not any(e.get("merged") for e in result["source_map"]["events"])


def test_missing_duplicate_or_changed_text_fails_conservation():
    result = convert_pdf_bytes(sample_pdf(), include_assets=False)
    doc, md = result["source_map"], result["markdown"]
    assert not verify_markdown(md.replace("Total", "Different"), doc)["ok"]
    assert not verify_markdown(md+md, doc)["ok"]
    assert not verify_markdown(md.replace('data-source="', 'removed="', 1), doc)["ok"]


def test_unrecognized_borderless_continuation_uses_confirmed_columns():
    layout = extract_layout(sample_pdf())
    second = layout["pages"][1]
    table = second["tables"][0]
    # Simulate a parser that returns the continuation cells as independent text blocks.
    second["blocks"] = [{"kind": "text", "page": 2, "pages": [2], "bbox": c["bbox"],
                         "font_size": 11, "segments": c["segments"]} for c in table["rows"][0]]
    second["tables"] = []
    doc = assemble(layout)
    assert doc["events"][0]["recovered_from_text"]
    assert len([b for b in doc["blocks"] if b["kind"] == "table"]) == 1
    assert verify_markdown(render_markdown(doc), doc)["ok"]


def test_no_text_layer_is_not_reported_as_verified_text_conversion():
    with pymupdf.open() as pdf:
        pdf.new_page()
        result = convert_pdf_bytes(pdf.tobytes())
    assert result["status"] == "REQUIRES_REVIEW"
    assert result["warnings"][0]["code"] == "NO_TEXT_LAYER"
    assert result["assets"]


def test_literal_financial_formula_is_not_interpreted_as_markdown_formatting():
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((50, 50), "Index*85%+Bond*15% field_a_b [value]")
        result = convert_pdf_bytes(pdf.tobytes(), include_assets=False)
    assert "&#42;85%" in result["markdown"]
    assert result["verification"]["ok"]


def test_real_futures_header_and_open_top_split_row_match_pdf():
    path = Path("assets/华泰柏瑞沪深300交易型开放式指数证券投资基金/2025-08-30_2025年中期报告.pdf")
    result = convert_pdf_bytes(path.read_bytes(), include_assets=False)
    doc = result["source_map"]
    table = next(b for b in doc["blocks"] if b["kind"] == "table" and b["pages"] == [21, 22])
    rows = [["".join(doc["segments"][s]["text"] for s in c["segments"]).strip() for c in row] for row in table["rows"]]
    assert len(table["xs"])-1 == 5
    assert rows[0] == ["代码", "名称", "持仓量（买/卖）", "合约市值", "公允价值变动"]
    assert rows[-2][0] == "减：可抵销期货暂收款"
    assert rows[-2][-1] == "134,226,540.00"
    assert rows[-1][0] == "净额" and rows[-1][-1] == "0.00"
    assert result["verification"]["ok"]


def test_numbered_statement_data_row_is_not_a_new_chapter_heading():
    cells = [{"col": 0, "colspan": 1, "segments": ["label"]},
             {"col": 1, "colspan": 1, "segments": ["amount"]}]
    segments = {"label": {"text": "三、利润总额"}, "amount": {"text": "100.00"}}
    assert not table_heading({"xs": [0, 100, 200], "rows": [cells]}, segments)


def test_horizontal_only_summary_tables_keep_spans_and_cross_page_headers():
    content = Path("assets/华夏成长证券投资基金/2026-07-27_产品资料概要更新_A份额.pdf").read_bytes()
    result = convert_pdf_bytes(content, include_assets=False)
    document = result["source_map"]
    tables = [b for b in document["blocks"] if b["kind"] == "table"]
    def rows(table):
        return [["".join(document["segments"][s]["text"] for s in c["segments"]).strip() for c in r] for r in table["rows"]]
    strategy = next(b for b in tables if b["pages"] == [1, 2])
    strategy_row = next(r for r in rows(strategy) if r[0] == "主要投资策略")
    assert "主要投资策略包括" in strategy_row[1] and "从基本面的分析入手挑选成长股" in strategy_row[1]
    fees = next(b for b in tables if b["pages"] == [3, 4])
    assert len(fees["xs"])-1 == 3
    assert rows(fees)[0] == ["费用类别", "收费方式/年费率或金额", "收费方"]
    assert rows(fees)[1] == ["管理费", "固定费率1.20%", "基金管理人、销售机构"]
    profile = next(b for b in tables if b["id"] == "p1-t1")
    manager = next(c for r in profile["rows"] for c in r if "基金经理" == "".join(document["segments"][s]["text"] for s in c["segments"]))
    assert manager["rowspan"] == 4
    assert result["verification"]["ok"]


def test_raster_ruled_summary_restores_cells_and_preserves_real_charts():
    content = Path('assets/易方达上证50交易型开放式指数证券投资基金基金产品资料概要更新.pdf').read_bytes()
    result = convert_pdf_bytes(content, include_assets=False)
    document = result['source_map']
    tables = [b for b in document['blocks'] if b['kind']=='table']
    rows = [[[''.join(document['segments'][s]['text'] for s in c['segments']).strip()
              for c in row] for row in t['rows']] for t in tables]
    assert any(['基金管理人','易方达基金管理有限公司','基金托管人','招商银行股份有限公司'] in r for r in rows)
    assert any(['审计费用','年费用金额61,100.00 元','会计师事务所'] in r for r in rows)
    assert any(['信息披露费','年费用金额120,000.00 元','规定披露报刊'] in r for r in rows)
    assert any(['基金运作综合费率（年化）'] in r and ['0.21%'] in r for r in rows)
    assert len([b for b in document['blocks'] if b['kind']=='image']) == 2
    assert sum(len(p['consumed_image_indices']) for p in document['page_fragments']) == 2169
    assert result['status'] == 'READY' and result['verification']['ok']


@pytest.mark.parametrize('scale', [.8, 1.4])
def test_repeated_raster_rules_restore_table_but_keep_standalone_icon(scale):
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=600*scale,height=500*scale)
        pix = pymupdf.Pixmap(pymupdf.csRGB,pymupdf.IRect(0,0,5,1),False)
        pix.clear_with(0)
        image = pix.tobytes('png')
        page.insert_text((50*scale,55*scale),'Heading',fontsize=11*scale)
        page.insert_image(pymupdf.Rect(30*scale,30*scale,32*scale,32*scale),stream=image)
        for y in [100,160]:
            for a,b in [(50,200),(200,500)]:
                page.draw_rect(pymupdf.Rect(a*scale,y*scale,b*scale,(y+.7)*scale),color=None,fill=(0,0,0))
        for x in range(50,500,4):
            page.insert_image(pymupdf.Rect(x*scale,130*scale,(x+3)*scale,130.7*scale),stream=image)
        for row,values in enumerate([['Name','Value'],['Alpha','123.45']]):
            for x,value in zip([55,205],values):
                page.insert_text((x*scale,(120+30*row)*scale),value,fontsize=11*scale)
        result = convert_pdf_bytes(pdf.tobytes(),include_assets=False)
    assert result['status']=='READY'
    assert len([b for b in result['source_map']['blocks'] if b['kind']=='table'])==1
    assert len([b for b in result['source_map']['blocks'] if b['kind']=='image'])==1


def test_unbound_raster_rules_require_review_instead_of_passing_character_check():
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((50,60),'Body text')
        pix = pymupdf.Pixmap(pymupdf.csRGB,pymupdf.IRect(0,0,5,1),False)
        pix.clear_with(0)
        for x in range(50,250,4):
            page.insert_image(pymupdf.Rect(x,100,x+3,100.7),stream=pix.tobytes('png'))
        result = convert_pdf_bytes(pdf.tobytes(),include_assets=False)
    assert result['verification']['ok']
    assert result['status']=='REQUIRES_REVIEW'
    assert any(w['code']=='UNRESOLVED_RASTER_RULES' for w in result['warnings'])
