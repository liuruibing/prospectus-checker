import pymupdf as fitz

from app.preflight import preflight_documents, preflight_summary_documents


def _pdf_bytes(text: str) -> bytes:
    document = fitz.open()
    page = document.new_page(width=600, height=800)
    page.insert_text((40, 60), text, fontname="china-s", fontsize=12)
    content = document.tobytes()
    document.close()
    return content


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


def test_summary_flow_blocks_clear_product_identity_mismatch():
    result = preflight_summary_documents({"documents": {
        "product_summary": {"filename": "产品资料概要.pdf", "content": _pdf_bytes("易方达上证50交易型开放式指数证券投资基金基金产品资料概要更新\n编制日期：2026年1月26日\n基金代码：510100")},
        "periodic_report": {"filename": "定期报告.pdf", "content": _pdf_bytes("华夏沪深300交易型开放式指数证券投资基金2025年第4季度报告\n报告送出日期：2026年1月22日\n基金代码：510330")},
    }})
    assert result["status"] == "FUND_MISMATCH"
    assert result["blocked"] is True


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
