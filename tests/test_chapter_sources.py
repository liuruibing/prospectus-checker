import hashlib
import json
import asyncio
from pathlib import Path

from app import extraction
from app.chapter_sources import ChapterCache, bind_sources, merge_source_batch_modules, resolve_data_columns, source_catalogue, source_catalogue_batches, source_gaps
from app.rule_compiler import load_investment_rule_definitions, read_rule_source

_, EXECUTION_PLANS, MODULE_RULES = load_investment_rule_definitions()


def test_cross_page_chapters_cover_all_real_source_rows():
    rule_texts = {rule["rule_id"]: read_rule_source(rule["file"]) for rule in MODULE_RULES}
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
                tables, {rule["rule_id"]: extraction._header_aliases(EXECUTION_PLANS[rule["rule_id"]]) for rule in MODULE_RULES},
                required_fields_by_rule={rule["rule_id"]: set(rule["fields"]) for rule in MODULE_RULES},
                column_tolerance=extraction._cross_page_column_tolerance(extraction.COMMON_RULE_FILE.read_text()),
            )
            assert [page["physical_page"] for page in contexts[MODULE_RULES[1]["rule_id"]]["physical_pages"]] == expected_pages
            for rule, expected in zip(MODULE_RULES, [12, 20, 10]):
                context = contexts[rule["rule_id"]]
                active_rule = extraction._rule_for_execution_plan(rule, EXECUTION_PLANS[rule["rule_id"]])
                sources = source_catalogue(context, active_rule, tables, maps, hashlib.sha256(content).hexdigest())
                assert len(sources) == expected
                assert source_gaps(context, sources, active_rule) == []
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
    base_rule = MODULE_RULES[1]
    rule = extraction._rule_for_execution_plan(base_rule, EXECUTION_PLANS[base_rule["rule_id"]])
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


def test_source_binding_uses_rule_declared_identity_fields_without_domain_field_list():
    rule = {
        "rule_id": "synthetic.contracts",
        "fields": {"notional": "名义金额"},
        "row_matching": {
            "identity_fields": ["row_key"],
            "fallback_identity_fields": ["contract_code"],
            "scope_fields": ["share_class"],
            "hierarchy_fields": [],
            "identity_headers": {"contract_code": ["合约代码"], "share_class": ["份额类别"]},
        },
    }
    records = {
        "source-1": {
            "page": 7,
            "cells": ["IF2609", "A类", "1,250.00"],
            "mapping": {"contract_code": 0, "share_class": 1},
            "fields": {"notional": {"raw_text": "1,250.00"}},
        },
    }

    bound = bind_sources({"rows": [{"source_id": "source-1", "row_label": "IF2609"}]}, records, rule)

    assert bound["rows"][0]["contract_code"] == "IF2609"
    assert bound["rows"][0]["share_class"] == "A类"
    assert bound["rows"][0]["fields"]["notional"]["raw_text"] == "1,250.00"


def test_industry_table_without_optional_code_column_still_builds_numeric_sources():
    rule = MODULE_RULES[1]
    rule_text = read_rule_source(rule["file"])
    matrix = [
        ["行业类别", "公允价值（人民币元）", "占基金资产净值比例（%）"],
        ["采矿业", "590,532,437.70", "12.41"],
        ["制造业", "1,669,817,146.80", "35.08"],
    ]
    def make_table(rows):
        cell_rows = []
        for row_index in range(len(rows)):
            y0 = row_index * 20
            cell_rows.append([[index * 100, y0, (index + 1) * 100, y0 + 20] for index in range(3)])
        return {"matrix": rows, "columns": 3, "cells_by_row": cell_rows, "page_width": 300}

    adjacent_market_table = make_table([
        ["国家（地区）", "公允价值（人民币元）", "占基金资产净值比例（%）"],
        ["美国", "17,686,928,778.66", "90.85"],
        ["合计", "17,686,928,778.66", "90.85"],
    ])
    industry_table = make_table(matrix)
    tables = {1: [adjacent_market_table, industry_table]}
    maps = extraction._header_column_maps(
        tables,
        {rule["rule_id"]: extraction._header_aliases(EXECUTION_PLANS[rule["rule_id"]])},
        required_fields_by_rule={rule["rule_id"]: set(rule["fields"])},
        column_tolerance=0.015,
    )

    fair_value_field = next(field for field, label in rule["fields"].items() if "公允价值" in label)
    ratio_field = next(field for field, label in rule["fields"].items() if "比例" in label)
    numeric_columns = {fair_value_field: 1, ratio_field: 2}
    assert maps[rule["rule_id"]][1][0] == numeric_columns
    assert maps[rule["rule_id"]][1][1] == {
        "industry_name": 0,
        **numeric_columns,
    }
    context = {
        "physical_pages": [{"physical_page": 1}],
        "section_markdown": "|行业类别|公允价值（人民币元）|占基金资产净值比例（%）|\n"
                            "|采矿业|590,532,437.70|12.41|\n"
                            "|制造业|1,669,817,146.80|35.08|",
    }
    sources = source_catalogue(context, rule, tables, maps, "doc-hash")

    assert len(sources) == 2
    assert {source["cells"][0] for source in sources.values()} == {"采矿业", "制造业"}
    assert all(source["source_ref"]["table"] == 1 for source in sources.values())
    assert all(set(rule["fields"]).issubset(source["fields"]) for source in sources.values())


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


def test_source_batches_bind_rows_to_their_matching_markdown_subsection():
    markdown = """## 股票投资明细
### 指数投资
<table>
<tr><td>代码</td><td>名称</td></tr>
<tr><td>600001</td><td>指数证券</td></tr>
</table>
### 积极投资
<!-- source-pages: 53,54 -->
<table>
<tr><td>代码</td><td>名称</td></tr>
<tr><td>688001</td><td>积极证券</td></tr>
</table>"""
    records = {
        "active-row": {"source_id": "active-row", "page": 54,
                       "source_ref": {"table": 0, "row": 1},
                       "cells": ["688001", "积极证券"]},
    }
    tables = {54: [{"matrix": [["代码", "名称"], ["688001", "积极证券"]]}]}

    batches = source_catalogue_batches(
        {"section_markdown": markdown}, records, tables, max_chars=1000,
    )

    assert len(batches) == 1
    assert batches[0]["section_headings"] == ["## 股票投资明细", "### 积极投资"]
    assert batches[0]["rows"][0]["source_id"] == "active-row"


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


def test_source_gaps_detects_unmapped_plain_text_continuation_row_without_row_name_rules():
    rule = MODULE_RULES[0]
    context = {"section_markdown": "|8|其他各项资产|10|1|\n第 44 页 共 65 页\n合计 110 100 注：表格口径说明。"}
    field_values = dict(zip(rule["fields"], ["10", "1"]))
    records = {"row1": {"cells": ["8", "其他各项资产", "10", "1"],
                         "fields": {field: {"raw_text": value} for field, value in field_values.items()}}}

    gaps = source_gaps(context, records, rule)

    assert gaps == ["合计 110 100 注：表格口径说明。"]


def test_source_gaps_ignores_page_image_links_in_plain_text_completeness_scan():
    context = {
        "section_markdown": """<table>
<tr><td>行业</td><td>金额</td><td>比例</td></tr>
<tr><td>采矿业</td><td>10.00</td><td>0.10</td></tr>
</table>
<!-- source-pages: 37 -->
![原图：物理页 37](assets/p0037-image1.png)"""
    }
    records = {
        "row-1": {
            "cells": ["采矿业", "10.00", "0.10"],
            "fields": {
                "amount": {"raw_text": "10.00"},
                "ratio": {"raw_text": "0.10"},
            },
        }
    }
    rule = {"fields": ["amount", "ratio"]}

    assert source_gaps(context, records, rule) == []


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
    fair_value_field = next(field for field, label in rule["fields"].items() if "公允价值" in label)
    ratio_field = next(field for field, label in rule["fields"].items() if "比例" in label)
    mapping = {"industry_code": 1, "industry_name": 4, fair_value_field: 5, ratio_field: 6}
    resolved = resolve_data_columns(table, mapping, 1)
    assert resolved == {"industry_code": 0, "industry_name": 3, fair_value_field: 5, ratio_field: 6}

    context = {"section_markdown": "|A|农、林、牧、渔业|10.00|0.10|", "physical_pages": [{"physical_page": 10}]}
    records = source_catalogue(context, rule, {10: [table]}, {rule["rule_id"]: {10: {0: mapping}}})
    record = next(iter(records.values()))
    assert record["mapping"] == resolved
    assert record["fields"][fair_value_field]["raw_text"] == "10.00"
    assert record["fields"][ratio_field]["raw_text"] == "0.10"


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
    rules = {rule["rule_id"]: extraction._header_aliases(EXECUTION_PLANS[rule["rule_id"]])}
    maps = extraction._header_column_maps(
        {1: [previous], 2: [continued]}, rules,
        required_fields_by_rule={rule["rule_id"]: set(rule["fields"])},
        column_tolerance=0.02,
    )

    inherited = maps[rule["rule_id"]][2][0]
    assert extraction._tables_have_compatible_columns(previous, continued, 0.02) is False
    fair_value_field = next(field for field, label in rule["fields"].items() if "公允价值" in label)
    ratio_field = next(field for field, label in rule["fields"].items() if "比例" in label)
    assert resolve_data_columns(continued, inherited, 0, rule["rule_id"]) == {
        "industry_code": 0, "industry_name": 1, fair_value_field: 2, ratio_field: 3,
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
        {rule["rule_id"]: read_rule_source(rule["file"]) for rule in MODULE_RULES},
        {}, {}, set(), None, False, "sample", None,
    ))
    assert [provider.calls[rule["rule_id"]] for rule in MODULE_RULES] == [1, 2, 1]
    assert len(result["modules"]) == 3
