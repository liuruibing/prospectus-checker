from app.historical_performance import compare_historical_documents, performance_modules_for_report
from app import runtime
from app.extraction import capture_investment_rule_snapshot


def _evidence(value):
    return {"raw_text": value, "value_kind": "NUMBER", "normalized_value": value.rstrip("%"),
            "unit": "%", "evidence": {"status": "LOCATED", "page": 2, "quote": value}}


def _row(year, share, nav, benchmark):
    return {
        "row_key": f"{year}-{share}", "period_year": str(year),
        "period_start": f"{year}-01-01", "period_end": f"{year}-12-31",
        "share_class": share,
        "row_evidence": {"status": "LOCATED", "page": 1, "quote": str(year)},
        "fields": {"nav_growth_rate": _evidence(nav), "benchmark_return_rate": _evidence(benchmark)},
    }


def _document(filename, disclosure_date, rows):
    period_ends = [row.get("period_end") for row in rows if row.get("period_end")]
    return {
        "filename": filename, "page_count": 4, "page_dimensions": [],
        "document_info": {"fund_code": "123456", "disclosure_date": disclosure_date},
        "metadata": {"period_end": max(period_ends) if period_ends else None},
        "modules": [{"rule_id": "performance.annual_performance", "status": "EXTRACTED", "rows": rows}],
    }


def _plan():
    return {
        "rule_identity": "performance.annual_performance", "display_title": "年度业绩",
        "source_selection": {"primary_role": "periodic_report"},
        "row_matching": {
            "identity_fields": ["year", "share_class", "period_start", "period_end"],
            "required_scope_fields": ["share_class", "period_start", "period_end"],
        },
        "comparison": {
            "context_checks": [{"label": "参考报告期末不晚于业绩截止日", "target_path": "document.metadata.period_end",
                                "reference_path": "document.metadata.period_end",
                                "operation": "reference_before_or_equal", "required": True}],
            "context_check_groups": [],
            "fields": [
                {"field": "nav_growth_rate", "label": "净值增长率", "operation": "decimal"},
                {"field": "benchmark_return_rate", "label": "基准收益率", "operation": "decimal"},
            ],
        },
    }


def test_multi_report_comparison_keeps_year_share_and_source_identity():
    target = _document("prospectus.pdf", "2026-03-01", [
        _row(2024, "A", "10.00%", "8.00%"), _row(2025, "A", "12.00%", "9.00%"),
    ])
    result = compare_historical_documents(target, [
        {"role": "periodic_report_001", "document": _document("q4-2024.pdf", "2025-01-20", [_row(2024, "A", "10.00%", "8.00%")])},
        {"role": "periodic_report_002", "document": _document("q4-2025.pdf", "2026-01-20", [_row(2025, "A", "11.00%", "9.00%")])},
    ], {"performance.annual_performance": _plan()})

    module = result["modules"][0]
    assert module["status"] == "MISMATCH"
    assert result["documents"]["periodic_report_001"]["filename"] == "q4-2024.pdf"
    assert result["documents"]["periodic_report_002"]["filename"] == "q4-2025.pdf"
    rows = {item["identity"][0]: item for item in module["rows"]}
    assert rows["2024"]["status"] == "MATCH"
    assert rows["2024"]["sources"][0]["role"] == "periodic_report_001"
    assert rows["2025"]["fields"]["nav_growth_rate"]["status"] == "MISMATCH"


def test_identity_fields_and_required_scope_fields_are_not_duplicated():
    target = _document("prospectus.pdf", "2026-03-01", [_row(2025, "A", "12.00%", "9.00%")])
    plan = _plan()
    plan["row_matching"] = {
        "identity_fields": ["period_year", "share_class", "period_start", "period_end"],
        "required_scope_fields": ["share_class", "period_start", "period_end"],
    }
    result = compare_historical_documents(
        target, [{"role": "periodic_report_001", "document": _document(
            "annual.pdf", "2026-01-20", [_row(2025, "A", "12.00%", "9.00%")]
        )}], {"performance.annual_performance": plan}
    )
    identity = result["modules"][0]["rows"][0]["identity"]
    assert identity == ["2025", "a", "2025-01-01", "2025-12-31"]


def test_multi_report_comparison_does_not_choose_between_duplicate_sources():
    target = _document("prospectus.pdf", "2026-03-01", [_row(2025, "A", "12.00%", "9.00%")])
    references = [
        {"role": f"periodic_report_{index:03}", "document": _document(f"q4-{index}.pdf", "2026-01-20", [_row(2025, "A", "12.00%", "9.00%")])}
        for index in (1, 2)
    ]
    result = compare_historical_documents(target, references, {"performance.annual_performance": _plan()})
    assert result["status"] == "UNRESOLVED"
    assert result["modules"][0]["rows"][0]["status"] == "UNRESOLVED"
    assert "多份参考证据" in result["modules"][0]["rows"][0]["review_message"]


def test_missing_year_stays_visible_as_unresolved_coverage():
    target = _document("prospectus.pdf", "2026-03-01", [
        _row(2024, "A", "10.00%", "8.00%"), _row(2025, "A", "12.00%", "9.00%"),
    ])
    result = compare_historical_documents(target, [
        {"role": "periodic_report_001", "document": _document("q4-2024.pdf", "2025-01-20", [_row(2024, "A", "10.00%", "8.00%")])},
    ], {"performance.annual_performance": _plan()})

    module = result["modules"][0]
    rows = {item["identity"][0]: item for item in module["rows"]}
    assert module["status"] == "UNRESOLVED"
    assert rows["2024"]["status"] == "MATCH"
    assert rows["2025"]["status"] == "UNRESOLVED"
    assert result["coverage"] == {
        "target_rows": 2, "matched_rows": 1, "mismatched_rows": 0, "unresolved_rows": 1,
    }


def test_reference_level_error_does_not_inflate_unresolved_target_row_count():
    target = _document("prospectus.pdf", "2026-03-01", [_row(2025, "A", "12.00%", "9.00%")])
    report = _document("annual.pdf", "2025-03-28", [])
    report["modules"] = []
    result = compare_historical_documents(
        target, [{"role": "periodic_report_001", "document": report}],
        {"performance.annual_performance": _plan()},
    )
    assert result["coverage"] == {
        "target_rows": 1, "matched_rows": 0, "mismatched_rows": 0, "unresolved_rows": 1,
    }
    assert result["unresolved_count"] == 2


def test_past_year_without_proven_full_year_interval_cannot_match():
    target = _document("prospectus.pdf", "2026-03-01", [_row(2025, "A", "12.00%", "9.00%")])
    report_row = _row(2025, "A", "12.00%", "9.00%")
    report_row["period_start"] = None
    report_row["period_end"] = None
    report_row["row_evidence"]["quote"] = "过去一年 12.00% 9.00%"
    result = compare_historical_documents(target, [
        {"role": "periodic_report_001", "document": _document("q4-2025.pdf", "2026-01-20", [report_row])},
    ], {"performance.annual_performance": _plan()})

    assert result["status"] == "UNRESOLVED"
    assert result["modules"][0]["rows"][0]["status"] == "UNRESOLVED"
    assert result["modules"][0]["rows"][0]["sources"] == []



def test_year_end_lookback_matches_verified_full_year_from_annual_report():
    plan = _plan()
    prior = _row(2024, "C", "3.64%", "2.75%")
    prior["label"] = "2024.01.01-2024.12.31"
    target = _document("prospectus.pdf", "2026-03-01", [prior, _row(2025, "C", "0.75%", "2.75%")])
    report_row = _row(2025, "招商产业债券C", "0.75%", "2.75%")
    report_row.update(label="过去一年", period_year=None, period_start=None, period_end=None)
    report_row["row_evidence"]["quote"] = "过去一年 0.75% 2.75%"
    report = _document("annual-2025.pdf", "2026-01-20", [report_row])
    report["document_info"].update(report_subtype="annual", period_end="2025-12-31")
    report["metadata"]["period_end"] = "2025-12-31"
    report["metadata_evidence"] = {"period_end": {"quote": "2025 年12 月31 日", "status": "QUOTE_ONLY"}}
    refs = [{"role": "historical_report_001", "document": report}]
    plans = {"performance.annual_performance": plan}

    result = compare_historical_documents(target, refs, plans)
    assert [row["status"] for row in result["modules"][0]["rows"]] == ["UNRESOLVED", "MATCH"]

    report["metadata_evidence"].clear()
    result = compare_historical_documents(target, refs, plans)
    assert result["coverage"]["matched_rows"] == 0

    report["metadata_evidence"] = {"period_end": {"quote": "2025 年12 月31 日", "status": "QUOTE_ONLY"}}
    target["modules"][0]["rows"].remove(prior)
    result = compare_historical_documents(target, refs, plans)
    assert result["coverage"]["matched_rows"] == 0


def test_annual_report_sent_after_prospectus_can_verify_earlier_full_year():
    target_row = _row(2025, "A", "1.26%", "2.75%")
    target_row["label"] = "2025.01.01-2025.12.31"
    target = _document("prospectus.pdf", "2026-03-11", [target_row])
    target["metadata"] = {"period_end": "2025-12-31"}
    report_row = _row(2025, "招商产业债券A", "1.26%", "2.75%")
    report_row.update(label="过去一年", period_year=None, period_start=None, period_end=None)
    report_row["row_evidence"]["quote"] = "过去一年 1.26% 2.75%"
    report = _document("annual-2025.pdf", "2026-03-31", [report_row])
    report["document_info"]["period_end"] = "2025-12-31"
    report["metadata"] = {"period_end": "2025-12-31", "sent_date": "2026-03-31"}
    report["metadata_evidence"] = {
        "period_end": {"quote": "2025 年12 月31 日", "status": "QUOTE_ONLY"},
    }

    result = compare_historical_documents(
        target, [{"role": "periodic_report", "document": report}],
        {"performance.annual_performance": _plan()},
    )

    matched = result["modules"][0]["rows"][0]
    assert matched["identity"] == ["2025", "a", "2025-01-01", "2025-12-31"]
    assert matched["status"] == "MATCH"
    assert matched["sources"][0]["filename"] == "annual-2025.pdf"

def test_annual_window_uses_verified_target_cutoff():
    plan = _plan()
    plan["comparison"]["calendar_year_window"] = 2
    rows = [_row(year, "A", "12.00%", "9.00%") for year in (2023, 2024, 2025, 2026)]
    for row in rows:
        row["label"] = f"{row['period_year']}.01.01-{row['period_year']}.12.31"
    target = _document("prospectus.pdf", "2026-03-01", rows)
    target["document_info"]["period_end"] = "2026-02-28"
    target["metadata"]["period_end"] = "2026-02-28"
    target["metadata_evidence"] = {
        "period_end": {"quote": "业绩截止日为2026年2月28日", "status": "QUOTE_ONLY"},
    }
    reference = _document("q4-2025.pdf", "2026-01-20", [rows[2]])
    references = [{"role": "historical_report_001", "document": reference}]
    plans = {"performance.annual_performance": plan}

    result = compare_historical_documents(target, references, plans)
    assert [row["identity"][0] for row in result["modules"][0]["rows"]] == ["2024", "2025"]
    assert result["coverage"]["matched_rows"] == 1

    target["metadata_evidence"].clear()
    result = compare_historical_documents(target, references, plans)
    assert result["status"] == "UNRESOLVED"
    assert result["modules"][0]["rows"] == []


def test_prospectus_performance_indicator_reports_uncovered_years():
    plan = _plan()
    earlier = _row(2024, "A", "10.00%", "8.00%")
    current = _row(2025, "A", "12.00%", "9.00%")
    target = _document("prospectus.pdf", "2026-03-01", [earlier, current])
    report = _document("q4.pdf", "2026-01-20", [current])
    modules = performance_modules_for_report(target, report, {"performance.annual_performance": plan})

    module = modules[0]
    assert module["status"] == "UNRESOLVED"
    assert "2024 年 A 类" in module["review_message"]
    assert module["differences"] == []
    assert len(module["checks"]) == 2
    assert all(check["row_label"] == "2025 年 A 类" for check in module["checks"])
    assert all(check["prospectus"]["evidence"]["status"] == "LOCATED" for check in module["checks"])
    assert module["periodic_rows"] == [current]


def test_prospectus_performance_indicator_compares_multiple_years_disclosed_in_one_report():
    earlier = _row(2024, "A", "10.00%", "8.00%")
    current = _row(2025, "A", "12.00%", "9.00%")
    target = _document("prospectus.pdf", "2026-03-01", [earlier, current])
    report = _document("annual.pdf", "2026-01-20", [earlier, current])
    modules = performance_modules_for_report(target, report, {"performance.annual_performance": _plan()})

    module = modules[0]
    assert module["status"] == "MATCH"
    assert len(module["checks"]) == 4
    assert {check["row_label"] for check in module["checks"]} == {"2024 年 A 类", "2025 年 A 类"}
    assert all(check["status"] == "MATCH" for check in module["checks"])


def test_performance_document_exposes_rule_addressable_metadata_from_preflight():
    prospectus = runtime._performance_document(
        {"document_info": {"fund_name": "样本基金", "fund_code": None, "period_end": "2025-12-31"}},
        {"fund_name": "样本基金", "version_date_interval": {"start": "2026-03-01", "end": "2026-03-01"}},
        "prospectus",
    )
    report = runtime._performance_document(
        {"document_info": {"fund_name": "样本基金", "fund_code": None}},
        {"fund_name": "样本基金", "fund_code": "123456",
         "report_send_date_interval": {"start": "2025-03-28", "end": "2025-03-28"},
         "date_interval": {"start": "2025-12-31", "end": "2025-12-31"}},
        "periodic_report",
    )
    assert prospectus["metadata"]["version_date"] == "2026-03-01"
    assert prospectus["metadata"]["compiled_date"] == "2026-03-01"
    assert prospectus["metadata"]["fund_name"] == "样本基金"
    assert prospectus["metadata"]["period_end"] == "2025-12-31"
    assert report["metadata"]["sent_date"] == "2025-03-28"
    assert report["metadata"]["fund_code"] == "123456"
    assert report["metadata"]["period_end"] == "2025-12-31"


def test_horizontal_calendar_year_table_provides_field_level_nav_growth_without_benchmark():
    target_2023 = _row(2023, "A", "5.00%", "2.75%")
    target_2023["label"] = "2023.01.01-2023.12.31"
    target_2024 = _row(2024, "A", "4.15%", "2.75%")
    target_2024["label"] = "2024.01.01-2024.12.31"
    target_2025 = _row(2025, "A", "1.26%", "2.75%")
    target_2025["label"] = "2025.01.01-2025.12.31"
    target = _document("prospectus.pdf", "2026-03-20", [target_2023, target_2024, target_2025])
    target["metadata"] = {"period_end": "2025-12-31"}

    def horizontal_year_row(year: str, nav: str) -> dict:
        row = _row(int(year), "招商产业债券A", nav, "0.00%")
        row.update(label=f"{year} 年", period_year=year, period_start=None, period_end=None)
        row["fields"].pop("benchmark_return_rate", None)
        row["row_evidence"]["quote"] = f"本期基金份额净值增长率 {nav}"
        return row

    past_one_year = _row(2025, "招商产业债券A", "1.26%", "2.75%")
    past_one_year.update(label="过去一年", period_year=None, period_start=None, period_end=None)
    past_one_year["row_evidence"]["quote"] = "过去一年 1.26% 2.75%"

    report = _document(
        "annual-2025.pdf", "2026-03-31",
        [past_one_year, horizontal_year_row("2025", "1.26%"), horizontal_year_row("2024", "4.15%"), horizontal_year_row("2023", "5.00%")],
    )
    report["document_info"]["period_end"] = "2025-12-31"
    report["metadata"] = {"period_end": "2025-12-31", "sent_date": "2026-03-31"}
    report["metadata_evidence"] = {"period_end": {"quote": "2025 年12 月31 日", "status": "QUOTE_ONLY"}}

    modules = performance_modules_for_report(target, report, {"performance.annual_performance": _plan()})
    module = modules[0]
    assert module["status"] == "UNRESOLVED"
    by_label = {(c["row_label"], c["field_key"]): c["status"] for c in module["checks"]}
    assert by_label[("2025 年 A 类", "nav_growth_rate")] == "MATCH"
    assert by_label[("2025 年 A 类", "benchmark_return_rate")] == "MATCH"
    assert by_label[("2024 年 A 类", "nav_growth_rate")] == "MATCH"
    assert by_label[("2024 年 A 类", "benchmark_return_rate")] == "UNRESOLVED"
    assert by_label[("2023 年 A 类", "nav_growth_rate")] == "MATCH"
    assert by_label[("2023 年 A 类", "benchmark_return_rate")] == "UNRESOLVED"


def test_same_report_conflict_between_tables_is_flagged_unresolved():
    target_2025 = _row(2025, "A", "1.26%", "2.75%")
    target_2025["label"] = "2025.01.01-2025.12.31"
    target = _document("prospectus.pdf", "2026-03-20", [target_2025])
    target["metadata"] = {"period_end": "2025-12-31"}

    row_full = _row(2025, "招商产业债券A", "1.26%", "2.75%")
    row_full.update(label="过去一年", period_year=None, period_start=None, period_end=None)
    row_full["row_evidence"]["quote"] = "过去一年 1.26% 2.75%"

    row_conflict = _row(2025, "招商产业债券A", "9.99%", "0.00%")
    row_conflict.update(label="2025 年", period_year="2025", period_start=None, period_end=None)
    row_conflict["fields"].pop("benchmark_return_rate", None)
    row_conflict["row_evidence"]["quote"] = "本期基金份额净值增长率 9.99%"

    report = _document("annual-2025.pdf", "2026-03-31", [row_full, row_conflict])
    report["document_info"]["period_end"] = "2025-12-31"
    report["metadata"] = {"period_end": "2025-12-31", "sent_date": "2026-03-31"}
    report["metadata_evidence"] = {"period_end": {"quote": "2025 年12 月31 日", "status": "QUOTE_ONLY"}}

    result = compare_historical_documents(
        target, [{"role": "periodic_report", "document": report}],
        {"performance.annual_performance": _plan()},
    )
    row_res = result["modules"][0]["rows"][0]
    assert row_res["status"] == "UNRESOLVED"
    assert "冲突" in row_res["review_message"]


def test_single_share_etf_multi_report_backfill_and_document_role_tagging():
    target_rows = [
        {**_row(2025, None, "18.50%", "15.20%"), "label": "2025.01.01-2025.12.31", "share_class": None},
        {**_row(2024, None, "6.10%", "5.40%"), "label": "2024.01.01-2024.12.31", "share_class": None},
        {**_row(2023, None, "-2.30%", "-3.10%"), "label": "2023.01.01-2023.12.31", "share_class": None},
    ]
    target = _document("prospectus.pdf", "2026-03-31", target_rows)

    def horizontal_row(year, nav):
        base = {
            **_row(year, None, nav, "0.00%"),
            "label": f"{year}年",
            "share_class": None,
            "period_year": None,
            "period_start": f"{year}-01-01",
            "period_end": f"{year}-12-31",
        }
        base["fields"].pop("benchmark_return_rate", None)
        return base

    annual_2025_rows = [
        {**_row(2025, None, "18.50%", "15.20%"), "share_class": None, "label": "过去一年", "period_year": None, "period_start": None, "period_end": None},
        horizontal_row(2024, "6.10%"),
        horizontal_row(2023, "-2.30%"),
    ]
    annual_2025_rows[0]["row_evidence"]["quote"] = "过去一年 18.50% 15.20%"
    doc_2025 = _document("annual-2025.pdf", "2026-03-31", annual_2025_rows)
    doc_2025["document_info"].update(report_subtype="annual", period_end="2025-12-31")
    doc_2025["metadata"]["period_end"] = "2025-12-31"
    doc_2025["metadata_evidence"] = {"period_end": {"quote": "2025 年12 月31 日", "status": "QUOTE_ONLY"}}

    annual_2024_rows = [
        {**_row(2024, None, "6.10%", "5.40%"), "share_class": None, "label": "过去一年", "period_year": None, "period_start": None, "period_end": None},
        horizontal_row(2023, "-2.30%"),
    ]
    annual_2024_rows[0]["row_evidence"]["quote"] = "过去一年 6.10% 5.40%"
    doc_2024 = _document("annual-2024.pdf", "2025-03-31", annual_2024_rows)
    doc_2024["document_info"].update(report_subtype="annual", period_end="2024-12-31")
    doc_2024["metadata"]["period_end"] = "2024-12-31"
    doc_2024["metadata_evidence"] = {"period_end": {"quote": "2024 年12 月31 日", "status": "QUOTE_ONLY"}}

    modules = performance_modules_for_report(
        target,
        [
            {"role": "periodic_report", "document": doc_2025},
            {"role": "historical_report_001", "document": doc_2024},
        ],
        {"performance.annual_performance": _plan()},
    )
    assert len(modules) == 1
    checks = modules[0]["checks"]
    by_key = {(c["row_label"], c["field_key"]): c for c in checks}
    assert by_key[("2025 年", "nav_growth_rate")]["status"] == "MATCH"
    assert by_key[("2025 年", "nav_growth_rate")]["document_role"] == "periodic_report"
    assert by_key[("2024 年", "nav_growth_rate")]["status"] == "MATCH"
    assert by_key[("2024 年", "benchmark_return_rate")]["status"] == "MATCH"
    assert by_key[("2024 年", "benchmark_return_rate")]["document_role"] == "historical_report_001"
    assert by_key[("2024 年", "benchmark_return_rate")]["document_filename"] == "annual-2024.pdf"
    assert by_key[("2023 年", "nav_growth_rate")]["status"] == "MATCH"
    assert by_key[("2023 年", "nav_growth_rate")]["document_role"] == "periodic_report"
    assert by_key[("2023 年", "benchmark_return_rate")]["status"] == "UNRESOLVED"


def test_runtime_check_performance_merges_supplementary_historical_reports(monkeypatch):
    import asyncio
    from app import runtime

    target = _document("prospectus.pdf", "2026-03-31", [
        {**_row(2025, "A", "10.00%", "8.00%"), "label": "2025.01.01-2025.12.31", "start_date": "2025-01-01", "end_date": "2025-12-31"},
        {**_row(2024, "A", "5.00%", "4.00%"), "label": "2024.01.01-2024.12.31", "start_date": "2024-01-01", "end_date": "2024-12-31"},
    ])
    target["document_info"]["period_end"] = "2025-12-31"
    target["metadata_evidence"] = {"period_end": {"status": "QUOTE_ONLY", "quote": "2025-12-31"}}
    doc_2025 = _document("2025年报.pdf", "2026-03-31", [
        {**_row(2025, "A", "10.00%", "8.00%"), "label": "过去一年", "period_year": None, "period_start": None, "period_end": None},
    ])
    doc_2025["modules"][0]["rows"][0]["row_evidence"]["quote"] = "过去一年 10.00% 8.00%"
    doc_2025["document_info"].update(report_subtype="annual", period_end="2025-12-31")
    doc_2025["metadata_evidence"] = {"period_end": {"status": "QUOTE_ONLY", "quote": "2025-12-31"}}
    doc_2024 = _document("2024年报.pdf", "2025-03-31", [
        {**_row(2024, "A", "5.00%", "4.00%"), "label": "过去一年", "period_year": None, "period_start": None, "period_end": None},
    ])
    doc_2024["modules"][0]["rows"][0]["row_evidence"]["quote"] = "过去一年 5.00% 4.00%"
    doc_2024["document_info"].update(report_subtype="annual", period_end="2024-12-31")
    doc_2024["metadata_evidence"] = {"period_end": {"status": "QUOTE_ONLY", "quote": "2024-12-31"}}
    by_name = {
        "prospectus.pdf": {**target, "page_count": 10, "page_dimensions": [{"width": 595, "height": 842}]},
        "2025年报.pdf": {**doc_2025, "page_count": 20, "page_dimensions": [{"width": 595, "height": 842}]},
        "2024年报.pdf": {**doc_2024, "page_count": 18, "page_dimensions": [{"width": 595, "height": 842}]},
    }

    async def fake_analyze_document(*, filename, content, role, provider, force_refresh=False, trace_callback=None, rule_snapshot=None):
        return dict(by_name[filename])

    monkeypatch.setattr(runtime, "analyze_document", fake_analyze_document)
    preflight = {
        "prospectus": {
            "fund_name": "测试基金", "fund_code": "123456",
            "date_interval": {"start": "2026-03-31", "end": "2026-03-31"},
            "portfolio_period_end_interval": {"start": "2025-12-31", "end": "2025-12-31"},
        },
        "periodic_report": {
            "fund_name": "测试基金", "fund_code": "123456", "report_subtype": "annual",
            "date_interval": {"start": "2025-12-31", "end": "2025-12-31"},
            "report_send_date_interval": {"start": "2026-03-31", "end": "2026-03-31"},
        },
        "historical_reports": {
            "historical_report_001": {
                "fund_name": "测试基金", "fund_code": "123456", "report_subtype": "annual",
                "date_interval": {"start": "2024-12-31", "end": "2024-12-31"},
                "report_send_date_interval": {"start": "2025-03-31", "end": "2025-03-31"},
            },
        },
    }
    payload = {
        "prospectus_filename": "prospectus.pdf", "prospectus_content": b"%PDF",
        "periodic_filename": "2025年报.pdf", "periodic_content": b"%PDF", "periodic_preview_id": "prev_main",
        "historical_reports": [{"role": "historical_report_001", "filename": "2024年报.pdf", "content": b"%PDF", "preview_id": "prev_2024"}],
    }
    modules, extra_docs = asyncio.run(runtime._check_performance(runtime.WorkflowContext(input_val=payload, provider=None), payload, preflight))
    assert len(modules) == 1
    assert modules[0]["status"] == "MATCH"
    assert "historical_report_001" in extra_docs
    assert extra_docs["historical_report_001"]["preview_id"] == "prev_2024"
    roles_seen = {check["document_role"] for check in modules[0]["checks"]}
    assert roles_seen == {"periodic_report", "historical_report_001"}


def test_same_period_annual_preferred_over_q4_when_overlapping_sources_agree():
    target = _document("prospectus.pdf", "2026-03-31", [
        {**_row(2024, "A", "5.00%", "4.00%"), "label": "2024.01.01-2024.12.31"},
    ])
    row_2024 = {**_row(2024, "A", "5.00%", "4.00%"), "label": "过去一年", "period_year": None, "period_start": None, "period_end": None}
    row_2024["row_evidence"]["quote"] = "过去一年 5.00% 4.00%"

    doc_annual = _document("2024年报.pdf", "2025-03-31", [dict(row_2024)])
    doc_annual["document_info"].update(report_subtype="annual", period_end="2024-12-31")
    doc_annual["metadata"]["period_end"] = "2024-12-31"
    doc_annual["metadata_evidence"] = {"period_end": {"status": "QUOTE_ONLY", "quote": "2024-12-31"}}

    doc_q4 = _document("2024Q4.pdf", "2025-01-22", [dict(row_2024)])
    doc_q4["document_info"].update(report_subtype="quarterly", period_end="2024-12-31")
    doc_q4["metadata"]["period_end"] = "2024-12-31"
    doc_q4["metadata_evidence"] = {"period_end": {"status": "QUOTE_ONLY", "quote": "2024-12-31"}}

    modules = performance_modules_for_report(
        target,
        [
            {"role": "historical_report_001", "document": doc_q4},
            {"role": "historical_report_002", "document": doc_annual},
        ],
        {"performance.annual_performance": _plan()},
    )
    assert modules[0]["status"] == "MATCH"
    assert {c["document_role"] for c in modules[0]["checks"]} == {"historical_report_002"}


def test_q4_same_year_end_range_and_unknown_start_date_anchor_past_one_year():
    from app.domain import quoted_dates

    assert quoted_dates("本报告期自2025 年10 月1 日起至12 月31 日止。") == ("2025-10-01", "2025-12-31")
    target = _document("prospectus.pdf", "2026-03-20", [
        {**_row(2025, "A", "1.26%", "2.75%"), "label": "2025.01.01-2025.12.31", "start_date": "2025-01-01", "end_date": "2025-12-31"},
    ])
    target["document_info"].update(fund_name="招商产业债券", period_end="2025-12-31")
    target["metadata"].update(fund_name="招商产业债券", period_end="2025-12-31")
    target["metadata_evidence"] = {"period_end": {"status": "QUOTE_ONLY", "quote": "2025-12-31"}}

    q4_row = {
        **_row(2025, "招商产业债券A", "1.26%", "2.75%"),
        "label": "过去一年",
        "period_year": None,
        "period_start": None,
        "period_end": None,
        "start_date": "unknown",
        "end_date": "2025-12-31",
    }
    q4_row["row_evidence"]["quote"] = "过去一年 1.26% 2.75%"
    # Simulate LLM wrongly emitting 2025-01-01..2025-12-31 on sibling relative lookback rows in Q4.
    q4_noise_3m = {
        **_row(2025, "招商产业债券A", "0.41%", "0.69%"),
        "label": "过去三个月", "start_date": "2025-01-01", "end_date": "2025-12-31",
    }
    q4_noise_3y = {
        **_row(2025, "招商产业债券A", "10.74%", "8.25%"),
        "label": "过去三年", "start_date": "2025-01-01", "end_date": "2025-12-31",
    }
    q4_noise_inception = {
        **_row(2025, "招商产业债券A", "131.06%", "43.91%"),
        "label": "自基金合同生效起至今", "start_date": "2025-01-01", "end_date": "2025-12-31",
    }
    q4_doc = _document("q4-2025.pdf", "2026-01-21", [q4_noise_3m, q4_row, q4_noise_3y, q4_noise_inception])
    q4_doc["document_info"].update(fund_name="招商产业债券", report_subtype="quarterly", period_end="2025-12-31")
    q4_doc["metadata"].update(fund_name="招商产业债券", period_end="2025-12-31")
    q4_doc["metadata_evidence"] = {"period_end": {"status": "QUOTE_ONLY", "quote": "本报告期自2025 年10 月1 日起至12 月31 日止。"}}

    snap = capture_investment_rule_snapshot(scope="historical_performance")
    modules = performance_modules_for_report(
        target,
        [{"role": "periodic_report", "document": q4_doc}],
        snap["execution_plans"],
    )
    assert modules[0]["status"] == "MATCH"
    assert [c["status"] for c in modules[0]["checks"]] == ["MATCH", "MATCH"]
