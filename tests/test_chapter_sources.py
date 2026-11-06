import json
import asyncio
from pathlib import Path

from app import extraction
from app.chapter_sources import ChapterCache, bind_sources, source_catalogue, source_gaps
from app.domain import MODULE_RULES


def test_cross_page_chapters_cover_all_real_source_rows():
    rule_texts = {rule["rule_id"]: rule["file"].read_text(encoding="utf-8") for rule in MODULE_RULES}
    import anydoc

    for filename, expected_pages in [
        ("易方达上证50交易型开放式指数证券投资基金更新的招募说明书.pdf", [55, 56]),
        ("易方达上证50交易型开放式指数证券投资基金2025年第4季度报告.pdf", [9, 10]),
        ("易方达上证50交易型开放式指数证券投资基金2026年第2季度报告.pdf", [9, 10]),
    ]:
        path = Path("assets") / filename
        content = path.read_bytes()
        document, pages = extraction._read_pdf(content, filename)
        try:
            markdown = anydoc.to_markdown_bytes(content, "pdf")
            contexts = extraction._chapter_contexts(extraction._anydoc_sections(markdown, rule_texts), pages, rule_texts)
            selected = {page["physical_page"] for context in contexts.values() for page in context["physical_pages"]}
            tables = extraction._collect_tables(document, selected)
            maps = extraction._header_column_maps(
                tables, {rule["rule_id"]: extraction._header_aliases(rule_texts[rule["rule_id"]]) for rule in MODULE_RULES},
                column_tolerance=extraction._cross_page_column_tolerance(extraction.COMMON_RULE_FILE.read_text()),
            )
            assert [page["physical_page"] for page in contexts[MODULE_RULES[1]["rule_id"]]["physical_pages"]] == expected_pages
            for rule, expected in zip(MODULE_RULES, [12, 20, 10]):
                context = contexts[rule["rule_id"]]
                sources = source_catalogue(context, rule, tables, maps)
                assert len(sources) == expected
                assert source_gaps(context, sources, rule) == []
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
        {rule["rule_id"]: rule["file"].read_text() for rule in MODULE_RULES},
        {}, {}, set(), None, False, "sample", None,
    ))
    assert [provider.calls[rule["rule_id"]] for rule in MODULE_RULES] == [1, 2, 1]
    assert len(result["modules"]) == 3
