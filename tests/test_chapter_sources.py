import hashlib
import json
import asyncio
from pathlib import Path

from app import extraction
from app.chapter_sources import ChapterCache, bind_sources, merge_source_batch_modules, resolve_data_columns, source_catalogue, source_catalogue_batches, source_gaps
from app.domain import MODULE_RULES


def test_cross_page_chapters_cover_all_real_source_rows():
    rule_texts = {rule["rule_id"]: extraction.effective_rule_text(rule["file"]) for rule in MODULE_RULES}
    from app.pdf_document import page_tables, verified_conversion

    for filename, expected_pages in [
        ("易方达上证50交易型开放式指数证券投资基金更新的招募说明书.pdf", [55, 56]),
        ("易方达上证50交易型开放式指数证券投资基金2025年第4季度报告.pdf", [9, 10]),
        ("易方达上证50交易型开放式指数证券投资基金2026年第2季度报告.pdf", [9, 10]),
    ]:
        path = Path("assets") / filename
        content = path.read_bytes()
        document, pages = extraction._read_pdf(content, filename)
        try:
            conversion = verified_conversion(content)
            markdown = conversion["reader_markdown"]
            contexts = extraction._chapter_contexts(extraction._anydoc_sections(markdown, rule_texts), pages, rule_texts)
            selected = {page["physical_page"] for context in contexts.values() for page in context["physical_pages"]}
            native = page_tables(conversion)
            tables = {page: native.get(page, []) for page in selected}
            maps = extraction._header_column_maps(
                tables, {rule["rule_id"]: extraction._header_aliases(rule_texts[rule["rule_id"]]) for rule in MODULE_RULES},
                column_tolerance=extraction._cross_page_column_tolerance(extraction.COMMON_RULE_FILE.read_text()),
            )
            assert [page["physical_page"] for page in contexts[MODULE_RULES[1]["rule_id"]]["physical_pages"]] == expected_pages
            for rule, expected in zip(MODULE_RULES, [12, 20, 10]):
                context = contexts[rule["rule_id"]]
                sources = source_catalogue(context, rule, tables, maps, hashlib.sha256(content).hexdigest())
                assert len(sources) == expected
                assert source_gaps(context, sources, rule) == []
                assert all(source["source_kind"] == "table_row" for source in sources.values())
                assert all(source["document_role"] == "periodic_report" for source in sources.values())
                assert all(len(source["source_id"]) == 24 for source in sources.values())
                assert all(source["bbox"] and len(source["bbox"]) == 4 for source in sources.values())
                assert all(source["raw_text"] and source["page"] > 0 for source in sources.values())
                money_field = "amount" if rule is MODULE_RULES[0] else "fair_value"
                assert all(source["fields"][money_field]["unit"] == "元" for source in sources.values())
                if rule is MODULE_RULES[1]:
                    assert any("合计" in source["cells"] for source in sources.values())
        finally:
            document.close()


def test_source_binding_rejects_missing_duplicate_and_foreign_rows_and_preserves_zero():
    rule = MODULE_RULES[1]
    records = {
        "p2t0r1": {"page": 2, "cells": ["B", "采矿业", "0", "-"],
                    "mapping": {"industry_code": 0, "industry_name": 1},
                    "fields": {"fair_value": {"raw_text": "0"}, "ratio_of_net_assets": {"raw_text": "-"}}},
        "p3t0r1": {"page": 3, "cells": ["", "合计", "0", "-"], "mapping": {}, "fields": {}},
    }
    module = {"found": True, "rows": [
        {"source_id": "p2t0r1", "row_label": "采矿业", "industry_code": "WRONG", "fields": {"fair_value": "999"}},
        {"source_id": "p2t0r1", "row_label": "重复"},
        {"source_id": "p99t0r1", "row_label": "其他章节"},
    ]}
    bound = bind_sources(module, records, rule)
    assert bound["rows"][0]["fields"]["fair_value"]["raw_text"] == "0"
    assert bound["rows"][0]["industry_code"] == "B"
    assert len(bound["rows"]) == 1
    assert bound["source_coverage"]["missing"] == ["p3t0r1"]
    assert bound["source_warnings"]


def test_large_source_catalogue_batches_preserve_headers_headings_and_exact_coverage():
    records = {
        f"id-{index}": {"source_id": f"id-{index}", "page": 4,
                        "source_ref": {"table": 0, "row": index + 1},
                        "cells": [str(index), f"证券{index}"]}
        for index in range(7)
    }
    context = {"section_markdown": "## 投资组合\n### 指数投资\n<table></table>"}
    tables = {4: [{"matrix": [["序号", "名称"], *[[str(i), f"证券{i}"] for i in range(7)]]}]}

    batches = source_catalogue_batches(context, records, tables, max_chars=180)

    assert len(batches) > 1
    assert [row["source_id"] for batch in batches for row in batch["rows"]] == list(records)
    assert all(batch["section_headings"] == ["## 投资组合", "### 指数投资"] for batch in batches)
    assert all(batch["table"]["headers"] == [["序号", "名称"], ["0", "证券0"], ["1", "证券1"]]
               for batch in batches)


def test_batch_merge_marks_missing_source_rows_and_merges_scope_conservatively():
    records = {"a": {}, "b": {}, "c": {}}
    merged = merge_source_batch_modules([
        {"found": True, "scope": {"market_scope": "domestic", "investment_scope": "index"},
         "rows": [{"source_id": "a"}, {"source_id": "b"}]},
        {"found": True, "scope": {"market_scope": "hk_connect", "investment_scope": "active"},
         "rows": [{"source_id": "b"}]},
    ], records)

    assert merged["scope"] == {"market_scope": "all", "investment_scope": "all"}
    assert merged["source_coverage"] == {"expected": 3, "covered": 2, "missing": ["c"]}
    assert len(merged["source_warnings"]) == 2


def test_source_gaps_detects_rule_named_plain_text_continuation_row():
    rule = MODULE_RULES[0]
    context = {"section_markdown": "|8|其他各项资产|10|1|\n第 44 页 共 65 页\n合计 110 100 注：表格口径说明。"}
    records = {"row1": {"cells": ["8", "其他各项资产", "10", "1"]}}

    gaps = source_gaps(context, records, rule)

    assert gaps == ["合计 110 100 注：表格口径说明。"]


def test_merged_header_columns_bind_to_body_cells_by_horizontal_geometry():
    rule = MODULE_RULES[1]
    table = {
        "matrix": [
            ["", "代码", "", "", "行业类别", "公允价值（元）", "占基金资产净值比例(%)", ""],
            ["A", None, None, "农、林、牧、渔业", None, "10.00", "0.10", None],
        ],
        "columns": 8,
        "cells_by_row": [
            [[76, 0, 82, 10], [82, 0, 111, 10], [111, 0, 117, 10], [117, 0, 122, 10],
             [122, 0, 208, 10], [208, 0, 369, 10], [369, 0, 513, 10], [513, 0, 519, 10]],
            [[76, 10, 117, 30], None, None, [117, 10, 208, 30], None,
             [208, 10, 369, 30], [369, 10, 519, 30], None],
        ],
        "page_width": 595,
    }
    mapping = {"industry_code": 1, "industry_name": 4, "fair_value": 5, "ratio_of_net_assets": 6}
    resolved = resolve_data_columns(table, mapping, 1)
    assert resolved == {"industry_code": 0, "industry_name": 3, "fair_value": 5, "ratio_of_net_assets": 6}

    context = {"section_markdown": "|A|农、林、牧、渔业|10.00|0.10|", "physical_pages": [{"physical_page": 10}]}
    records = source_catalogue(context, rule, {10: [table]}, {rule["rule_id"]: {10: {0: mapping}}})
    record = next(iter(records.values()))
    assert record["mapping"] == resolved
    assert record["fields"]["fair_value"]["raw_text"] == "10.00"
    assert record["fields"]["ratio_of_net_assets"]["raw_text"] == "0.10"


def test_headerless_continuation_inherits_column_centers_across_different_grid_counts():
    rule = MODULE_RULES[1]
    previous = {
        "matrix": [["", "代码", "", "", "行业类别", "公允价值（元）", "占基金资产净值比例(%)", ""],
                   ["A", None, None, "农业", None, "10.00", "0.10", ""]],
        "columns": 8,
        "cells_by_row": [
            [[76, 0, 82, 10], [82, 0, 111, 10], [111, 0, 117, 10], [117, 0, 122, 10],
             [122, 0, 208, 10], [208, 0, 369, 10], [369, 0, 513, 10], [513, 0, 519, 10]],
            [[76, 10, 117, 30], None, None, [117, 10, 208, 30], None,
             [208, 10, 369, 30], [369, 10, 519, 30], None],
        ], "page_width": 595,
    }
    continued = {
        "matrix": [["B", "采矿业", "20.00", "0.20"]],
        "columns": 4,
        "cells_by_row": [[[76, 0, 117, 20], [117, 0, 208, 20], [208, 0, 369, 20], [369, 0, 519, 20]]],
        "page_width": 595,
    }
    rules = {rule["rule_id"]: extraction._header_aliases(extraction.effective_rule_text(rule["file"]))}
    maps = extraction._header_column_maps({1: [previous], 2: [continued]}, rules, column_tolerance=0.02)

    inherited = maps[rule["rule_id"]][2][0]
    assert extraction._tables_have_compatible_columns(previous, continued, 0.02) is False
    assert resolve_data_columns(continued, inherited, 0, rule["rule_id"]) == {
        "industry_code": 0, "industry_name": 1, "fair_value": 2, "ratio_of_net_assets": 3,
    }


def test_cached_chapter_is_invalidated_by_rules_document_and_model_and_corruption(tmp_path, monkeypatch):
    monkeypatch.setenv("PROSPECTUS_CACHE_DIR", str(tmp_path))
    cache = ChapterCache()
    namespace = {"model": "model-a", "document_hash": "doc-a"}
    key = cache.key(namespace, "system", "rules-a")
    cache.save(key, '{"found":true,"rows":[]}')
    assert cache.load(key)["response"] == '{"found":true,"rows":[]}'
    assert cache.load(cache.key(namespace, "system", "rules-b")) is None
    assert cache.load(cache.key({**namespace, "model": "model-b"}, "system", "rules-a")) is None
    assert cache.load(cache.key({**namespace, "document_hash": "doc-b"}, "system", "rules-a")) is None
    (tmp_path / f"{key}.json").write_text(json.dumps({"key": "wrong", "response": ""}))
    assert cache.load(key) is None


def test_source_address_resolves_duplicate_labels_but_rejects_wrong_values():
    tables = {1: [{"matrix": [["重复项目", "1"], ["重复项目", "2"]],
                   "cells_by_row": [[[0, 0, 10, 10], [10, 0, 20, 10]], [[0, 10, 10, 20], [10, 10, 20, 20]]]}]}
    maps = {"module": {1: {0: {"amount": 1}}}}
    assert extraction._referenced_bbox(tables, maps, 1, "module", "重复项目", "2", "amount", {"table": 0, "row": 1}) == [10.0, 10.0, 20.0, 20.0]
    assert extraction._referenced_bbox(tables, maps, 1, "module", "重复项目", "1", "amount", {"table": 0, "row": 1}) is None
    assert extraction._referenced_bbox(tables, maps, 1, "module", "其他行", "2", "amount", {"table": 0, "row": 1}) is None


def test_invalid_json_retries_only_the_failed_chapter():
    class Provider:
        def __init__(self):
            self.calls = {}

        async def generate_text(self, system, prompt):
            rule_id = next(rule["rule_id"] for rule in MODULE_RULES if f"模块：{rule['rule_id']}。" in prompt)
            self.calls[rule_id] = self.calls.get(rule_id, 0) + 1
            if rule_id == MODULE_RULES[1]["rule_id"] and self.calls[rule_id] == 1:
                return '{"found":'
            return '{"found":true,"rows":[]}'

    provider = Provider()
    result = asyncio.run(extraction._extract_chapters(
        "sample.pdf", "periodic_report", provider, {}, [],
        {rule["rule_id"]: extraction.effective_rule_text(rule["file"]) for rule in MODULE_RULES},
        {}, {}, set(), None, False, "sample", None,
    ))
    assert [provider.calls[rule["rule_id"]] for rule in MODULE_RULES] == [1, 2, 1]
    assert len(result["modules"]) == 3
