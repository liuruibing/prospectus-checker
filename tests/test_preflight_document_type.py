import pymupdf as fitz

from app.preflight import _document_metadata, _extract_product_name, preflight_documents, preflight_summary_documents
from app.runtime import _summary_document_metadata


def _pdf_bytes(text: str) -> bytes:
    document = fitz.open()
    page = document.new_page(width=600, height=800)
    page.insert_text((40, 60), text, fontname="china-s", fontsize=12)
    content = document.tobytes()
    document.close()
    return content


def test_prospectus_product_name_returns_none_when_cover_has_no_fund_name():
    assert _extract_product_name("招募说明书", "prospectus") is None


def test_investment_flow_rejects_product_summary_uploaded_as_periodic_report():
    result = preflight_documents({
        "prospectus_filename": "招募说明书.pdf",
        "prospectus_content": _pdf_bytes("易方达上证50交易型开放式指数证券投资基金更新的招募说明书"),
        "periodic_filename": "基金产品资料概要更新.pdf",
        "periodic_content": _pdf_bytes("易方达上证50交易型开放式指数证券投资基金 产品资料概要更新 编制日期：2026年1月26日"),
    })

    assert result["status"] == "WRONG_DOCUMENT_TYPE"
    assert result["blocked"] is True
    assert "产品资料概要核对" in result["message"]


def test_investment_preflight_does_not_reject_by_arbitrary_cover_to_period_gap():
    result = preflight_documents({
        "prospectus_filename": "招募说明书.pdf",
        "prospectus_content": _pdf_bytes(
            "样本债券型证券投资基金更新的招募说明书\n2025年11月\n基金代码：000001"
        ),
        "periodic_filename": "定期报告.pdf",
        "periodic_content": _pdf_bytes(
            "样本债券型证券投资基金2024年年度报告\n报告期末：2024年12月31日\n基金代码：000001"
        ),
    })

    assert result["date_gap_days"] > 183
    assert result["status"] == "READY"
    assert result["blocked"] is False


def test_summary_flow_requires_summary_and_accepts_periodic_report_role():
    result = preflight_summary_documents({"documents": {
        "product_summary": {"filename": "产品资料概要.pdf", "content": _pdf_bytes("易方达上证50交易型开放式指数证券投资基金基金产品资料概要更新\n编制日期：2026年1月26日\n基金代码：510100")},
        "periodic_report": {"filename": "定期报告.pdf", "content": _pdf_bytes("易方达上证50交易型开放式指数证券投资基金2025年第4季度报告\n报告送出日期：2026年1月22日\n基金代码：510100")},
    }})
    assert result["status"] == "READY"
    assert result["blocked"] is False
    assert result["periodic_report"]["fund_code"] == "510100"
    assert result["periodic_report"]["report_subtype"] == "quarterly"
    assert result["periodic_report"]["report_send_date_interval"] == {"start": "2026-01-22", "end": "2026-01-22"}


def test_summary_preflight_collects_identity_facts_without_deciding_business_match():
    result = preflight_summary_documents({"documents": {
        "product_summary": {"filename": "产品资料概要.pdf", "content": _pdf_bytes("易方达上证50交易型开放式指数证券投资基金基金产品资料概要更新\n编制日期：2026年1月26日\n基金代码：510100")},
        "periodic_report": {"filename": "定期报告.pdf", "content": _pdf_bytes("华夏沪深300交易型开放式指数证券投资基金2025年第4季度报告\n报告送出日期：2026年1月22日\n基金代码：510330")},
    }})
    assert result["status"] == "READY"
    assert result["blocked"] is False
    assert result["product_summary"]["fund_code"] == "510100"
    assert result["periodic_report"]["fund_code"] == "510330"
    assert "identity_conflict_diagnostic" not in result


def test_summary_preflight_collects_dates_without_selecting_source():
    result = preflight_summary_documents({"documents": {
        "product_summary": {"filename": "产品资料概要.pdf", "content": _pdf_bytes(
            "易方达上证50交易型开放式指数证券投资基金基金产品资料概要更新\n编制日期：2026年1月10日\n基金代码：510100"
        )},
        "periodic_report": {"filename": "定期报告.pdf", "content": _pdf_bytes(
            "易方达上证50交易型开放式指数证券投资基金2025年第4季度报告\n报告送出日期：2026年1月22日\n基金代码：510100"
        )},
    }})
    assert result["status"] == "READY"
    assert result["blocked"] is False
    assert result["product_summary"]["date_interval"] == {"start": "2026-01-10", "end": "2026-01-10"}
    assert result["periodic_report"]["report_send_date_interval"] == {"start": "2026-01-22", "end": "2026-01-22"}
    assert "report_not_available_at_compilation" not in result


def test_summary_workflow_preserves_filename_date_separately_from_prospectus_pdf_date():
    facts = {
        "date_interval": {"start": "2025-11-01", "end": "2025-11-30"},
        "date_label": "招募说明书封面日期",
        "filename_date_interval": {"start": "2025-11-24", "end": "2025-11-24"},
        "filename_date_quote": "文件名日期前缀：2025-11-24",
        "filename_date_precision": "day",
        "version_date_interval": {"start": "2025-11-24", "end": "2025-11-24"},
        "version_date_quote": "文件名日期前缀：2025-11-24",
        "version_date_source": "filename",
        "version_date_precision": "day",
        "fund_name": "样本基金",
        "fund_code": None,
        "fund_codes": [],
    }

    metadata = _summary_document_metadata("prospectus", facts)

    assert metadata["date_interval"] == facts["date_interval"]
    assert metadata["filename_date_interval"] == facts["filename_date_interval"]
    assert metadata["filename_date_quote"] == facts["filename_date_quote"]
    assert metadata["version_date"] == "2025-11-24"
    assert metadata["version_date_interval"] == facts["version_date_interval"]
    assert metadata["version_date_source"] == "filename"


def test_prospectus_version_date_uses_compatible_filename_day_when_cover_has_only_month():
    facts = _document_metadata(
        "2025-11-24_样本基金招募说明书.pdf",
        _pdf_bytes("样本基金更新的招募说明书\n2025年11月"),
        "prospectus",
    )

    assert facts["date_interval"] == {"start": "2025-11-01", "end": "2025-11-30"}
    assert facts["version_date_interval"] == {"start": "2025-11-24", "end": "2025-11-24"}
    assert facts["version_date_source"] == "filename"


def test_prospectus_version_date_does_not_use_filename_day_outside_cover_month():
    facts = _document_metadata(
        "2025-12-01_样本基金招募说明书.pdf",
        _pdf_bytes("样本基金更新的招募说明书\n2025年11月"),
        "prospectus",
    )

    assert facts["filename_date_interval"] == {"start": "2025-12-01", "end": "2025-12-01"}
    assert facts["version_date_interval"] is None


def test_summary_a_share_codes_match_report_master_and_front_back_codes():
    summary_path = "assets/华夏成长证券投资基金/2026-07-27_产品资料概要更新_A份额.pdf"
    report_path = "assets/华夏成长证券投资基金/2026-07-21_2026年第2季度报告.pdf"
    result = preflight_summary_documents({"documents": {
        "product_summary": {"filename": summary_path.rsplit("/", 1)[-1], "content": open(summary_path, "rb").read()},
        "periodic_report": {"filename": report_path.rsplit("/", 1)[-1], "content": open(report_path, "rb").read()},
    }})
    assert result["status"] == "READY"
    assert result["blocked"] is False
    assert result["product_summary"]["fund_code"] == "000002"
    assert {"000001", "000002"}.issubset(set(result["product_summary"]["fund_codes"]))
    assert {"000001", "000002"}.issubset(set(result["periodic_report"]["fund_codes"]))


def test_summary_fallback_accepts_same_fund_with_parenthesized_share_alias():
    summary_path = "assets/华夏成长证券投资基金/2026-07-27_产品资料概要更新_A份额.pdf"
    report_path = "assets/华夏成长证券投资基金/2026-07-21_2026年第2季度报告.pdf"
    prospectus_path = "assets/华夏成长证券投资基金/2026-07-27_招募说明书更新.pdf"
    result = preflight_summary_documents({"documents": {
        "product_summary": {"filename": summary_path.rsplit("/", 1)[-1], "content": open(summary_path, "rb").read()},
        "periodic_report": {"filename": report_path.rsplit("/", 1)[-1], "content": open(report_path, "rb").read()},
        "prospectus": {"filename": prospectus_path.rsplit("/", 1)[-1], "content": open(prospectus_path, "rb").read()},
    }})
    assert result["status"] == "READY"
    assert result["blocked"] is False


def test_510300_prospectus_identity_ignores_unrelated_later_sample_market_code():
    product_dir = "assets/华泰柏瑞沪深300交易型开放式指数证券投资基金"
    summary_path = f"{product_dir}/2025-11-24_产品资料概要更新.pdf"
    report_path = f"{product_dir}/2025-10-28_2025年第3季度报告.pdf"
    prospectus_path = f"{product_dir}/2025-11-24_招募说明书更新_2025年第3号.pdf"
    result = preflight_summary_documents({"documents": {
        "product_summary": {"filename": summary_path.rsplit("/", 1)[-1], "content": open(summary_path, "rb").read()},
        "periodic_report": {"filename": report_path.rsplit("/", 1)[-1], "content": open(report_path, "rb").read()},
        "prospectus": {"filename": prospectus_path.rsplit("/", 1)[-1], "content": open(prospectus_path, "rb").read()},
    }})

    assert result["status"] == "READY"
    assert result["blocked"] is False
    assert result["product_summary"]["fund_code"] == "510300"
    assert result["periodic_report"]["fund_code"] == "510300"
    assert result["prospectus"]["fund_name"] == result["product_summary"]["fund_name"]
    assert result["prospectus"]["fund_code"] is None
def test_wrapped_prospectus_title_and_summary_share_class_keep_product_identity():
    from app.preflight import _extract_product_name
    assert _extract_product_name('样本债券型证券投资基金更新的招募\n说明书', 'prospectus') == '样本债券型证券投资基金'
    assert _extract_product_name('样本债券型证券投资基金（C类份额）基金产品资料概要', 'product_summary') == '样本债券型证券投资基金'


def test_periodic_report_distinguishes_report_period_from_short_send_date_label():
    metadata = _document_metadata(
        "基金2025年第4季度报告.pdf",
        _pdf_bytes(
            "样本债券型证券投资基金2025年第4季度报告\n"
            "报告期末：2025年12月31日\n送出日期：2026年1月21日\n"
            "基金代码：000001"
        ),
        "periodic_report",
    )

    assert metadata["date_interval"] == {"start": "2025-12-31", "end": "2025-12-31"}
    assert metadata["report_send_date_interval"] == {"start": "2026-01-21", "end": "2026-01-21"}


def test_periodic_report_identity_codes_are_found_after_front_matter():
    pdf = fitz.open()
    cover = pdf.new_page(width=600, height=800)
    cover.insert_text((40, 60), "博时信用债券投资基金2026年中期报告", fontname="china-s", fontsize=12)
    for _ in range(3):
        pdf.new_page(width=600, height=800)
    identity_page = pdf.new_page(width=600, height=800)
    identity_page.insert_text(
        (40, 60),
        "基金主代码\n050011\n下属分级基金的交易代码\n050011\n050111",
        fontname="china-s", fontsize=12,
    )
    content = pdf.tobytes()
    pdf.close()

    metadata = _document_metadata("2026年中期报告.pdf", content, "periodic_report")

    assert metadata["fund_code"] == "050011"
    assert "050011" in metadata["fund_codes"]


def test_periodic_report_does_not_guess_send_date_from_unlabelled_cover_dates():
    pdf = fitz.open()
    cover = pdf.new_page(width=600, height=800)
    cover.insert_text((40, 60), "样本债券型证券投资基金2025年第4季度报告\n报告期末：2025年12月31日", fontname="china-s", fontsize=12)
    later = pdf.new_page(width=600, height=800)
    later.insert_text((40, 60), "注：C类份额自2015年9月30日起存续。", fontname="china-s", fontsize=12)
    content = pdf.tobytes()
    pdf.close()
    metadata = _document_metadata(
        "基金2025年第4季度报告.pdf",
        content,
        "periodic_report",
    )

    assert metadata["date_interval"] == {"start": "2025-12-31", "end": "2025-12-31"}
    assert metadata["report_send_date_interval"] is None


def test_filename_iso_date_is_exposed_as_separate_candidate_not_pdf_date():
    metadata = _document_metadata(
        "2025-11-24_招募说明书更新.pdf",
        _pdf_bytes("样本债券型证券投资基金更新的招募说明书\n2025年11月"),
        "prospectus",
    )

    assert metadata["date_interval"] == {"start": "2025-11-01", "end": "2025-11-30"}
    assert metadata["filename_date_interval"] == {"start": "2025-11-24", "end": "2025-11-24"}
    assert metadata["filename_date_quote"] == "文件名日期前缀：2025-11-24"
    assert metadata["filename_date_precision"] == "day"


def test_filename_date_candidate_requires_a_valid_iso_prefix():
    for filename in ("说明书_2025-11-24.pdf", "2025-13-40_说明书.pdf", "2025年11月说明书.pdf"):
        metadata = _document_metadata(filename, _pdf_bytes("样本债券型证券投资基金更新的招募说明书"), "prospectus")
        assert metadata["filename_date_interval"] is None
        assert metadata["filename_date_quote"] is None

