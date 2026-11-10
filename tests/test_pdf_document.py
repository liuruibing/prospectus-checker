from pathlib import Path

import pymupdf
import pytest

from app.pdf_document import bind_catalogue, page_tables, require_ready, verified_conversion
from app.summary_check import build_source_catalogue


def pdf_content():
    with pymupdf.open() as pdf:
        p = pdf.new_page()
        for x in [50, 180, 300, 420]:
            p.draw_line((x, 60), (x, 110))
        for y in [60, 85, 110]:
            p.draw_line((50, y), (420, y))
        for i, text in enumerate(["Item", "Amount", "Ratio"]):
            p.insert_text((55+i*125, 78), text)
        for i, text in enumerate(["Cash", "100.00", "1.00"]):
            p.insert_text((55+i*125, 103), text)
        return pdf.tobytes()


def test_archive_reuses_verified_bytes_and_repairs_corrupt_cache(tmp_path):
    content = pdf_content()
    first = verified_conversion(content, tmp_path)
    assert not first["cache_hit"]
    second = verified_conversion(content, tmp_path)
    assert second["cache_hit"]
    root = Path(second["archive_path"])
    (root/"reader.md").write_text("damaged")
    repaired = verified_conversion(content, tmp_path)
    assert not repaired["cache_hit"]
    assert repaired["reader_markdown"] == first["reader_markdown"]
    assert list(tmp_path.glob("invalid-*"))


def test_logical_units_bind_existing_evidence_and_keep_table_whole(tmp_path):
    content = pdf_content()
    package = verified_conversion(content, tmp_path)
    catalogue = build_source_catalogue("test.pdf", content, "periodic_report")
    context = bind_catalogue(catalogue, package)
    table = next(u for u in context["logical_units"] if u["kind"] == "table")
    source_ids = {s["source_id"] for p in catalogue["pages"] for s in p["sources"]}
    assert set(table["source_ids"]) == source_ids
    assert "100.00" in table["markdown"]
    assert page_tables(package)[1][0]["columns"] == 3


def test_conversion_with_unresolved_structure_cannot_enter_model_stage():
    with pytest.raises(ValueError, match="PDF_MD_REQUIRES_REVIEW"):
        require_ready({"status": "REQUIRES_REVIEW", "verification": {"ok": True}, "warnings": []})


def test_complete_cross_page_allocation_reaches_existing_extraction_catalogue(tmp_path):
    from app import extraction
    from app.chapter_sources import source_catalogue, source_gaps
    from app.domain import MODULE_RULES
    path = Path("assets/华泰柏瑞沪深300交易型开放式指数证券投资基金/2025-08-30_2025年中期报告.pdf")
    content = path.read_bytes()
    package = verified_conversion(content, tmp_path)
    texts = {r["rule_id"]: r["file"].read_text() for r in MODULE_RULES}
    document, pages = extraction._read_pdf(content, path.name)
    try:
        contexts = extraction._chapter_contexts(extraction._anydoc_sections(package["reader_markdown"], texts), pages, texts)
        tables = page_tables(package)
        maps = extraction._header_column_maps(tables, {r["rule_id"]: extraction._header_aliases(texts[r["rule_id"]]) for r in MODULE_RULES},
            column_tolerance=extraction._cross_page_column_tolerance(extraction.COMMON_RULE_FILE.read_text()))
        rule = MODULE_RULES[0]
        rows = source_catalogue(contexts[rule["rule_id"]], rule, tables, maps)
        assert len(rows) == 13
        assert source_gaps(contexts[rule["rule_id"]], rows, rule) == []
        total = next(r for r in rows.values() if any(c.strip()=="合计" for c in r["cells"]))
        assert total["page"] == 45
        assert total["fields"]["amount"]["raw_text"].strip() == "374,989,491,483.87"
    finally:
        document.close()
