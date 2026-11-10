import asyncio
from io import BytesIO

import pymupdf as fitz
import pytest
from fastapi import HTTPException, UploadFile

import app.main as main
from app.main import create_product_summary_job


def _pdf() -> bytes:
    doc = fitz.open()
    doc.new_page().insert_text((40, 40), "产品资料概要 / 定期报告", fontname="china-s")
    data = doc.tobytes()
    doc.close()
    return data


def _upload(name: str) -> UploadFile:
    return UploadFile(file=BytesIO(_pdf()), filename=name)


def test_summary_task_requires_periodic_report():
    async def submit():
        return await create_product_summary_job(
            product_summary=_upload("概要.pdf"),
            periodic_report=UploadFile(file=BytesIO(b""), filename=""),
        )

    with pytest.raises(HTTPException) as error:
        asyncio.run(submit())
    assert error.value.status_code == 422
    assert error.value.detail == "产品资料概要核对必须上传定期报告"


def test_summary_task_accepts_required_pair_and_optional_prospectus(monkeypatch):
    async def no_process(*args, **kwargs):
        return None

    saved = {}
    monkeypatch.setattr(main, "_process_job", no_process)
    monkeypatch.setattr(main.AUDIT_STORE, "create_task", lambda job_id, job, docs, runtime: saved.update(job_id=job_id, job=job, docs=docs))
    result = asyncio.run(create_product_summary_job(
        product_summary=_upload("概要.pdf"),
        periodic_report=_upload("定期报告.pdf"),
        prospectus=None,
    ))
    assert result["workflow_id"] == "product-summary-verification"
    assert set(saved["docs"]) == {"product_summary", "periodic_report"}
    assert saved["job"]["steps"]["fallback_lookup"] == "queued"


def test_summary_task_accepts_optional_prospectus(monkeypatch):
    async def no_process(*args, **kwargs):
        return None

    saved = {}
    monkeypatch.setattr(main, "_process_job", no_process)
    monkeypatch.setattr(main.AUDIT_STORE, "create_task", lambda job_id, job, docs, runtime: saved.update(docs=docs))
    asyncio.run(create_product_summary_job(
        product_summary=_upload("概要.pdf"),
        periodic_report=_upload("定期报告.pdf"),
        prospectus=_upload("招募说明书.pdf"),
    ))
    assert set(saved["docs"]) == {"product_summary", "periodic_report", "prospectus"}
