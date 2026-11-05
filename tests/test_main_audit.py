from fastapi.testclient import TestClient
import pymupdf as fitz

from app import main
from app.audit import TaskAuditStore


def test_job_history_and_artifact_endpoints_survive_process_memory_reset(tmp_path, monkeypatch):
    store = TaskAuditStore(tmp_path / "tasks")
    task_id = "history_task_123456"
    job = {
        "job_id": task_id,
        "run_id": task_id,
        "status": "completed",
        "stage": "completed",
        "created_at": "2026-11-05T00:00:00+00:00",
        "result": {"difference_count": 1},
    }
    store.create_task(
        task_id,
        job,
        {"prospectus": ("prospectus.pdf", b"%PDF-test")},
        {"provider": "TestProvider", "model": "test"},
    )
    store.write_artifact(task_id, "prospectus/model_response.txt", "raw model text")
    monkeypatch.setattr(main, "AUDIT_STORE", store)

    client = TestClient(main.app)
    listed = client.get("/api/jobs").json()["jobs"]
    assert listed[0]["job_id"] == task_id
    assert client.get(f"/api/jobs/{task_id}").json()["result"] == {"difference_count": 1}

    trace = client.get(f"/api/jobs/{task_id}/trace").json()
    assert trace["task"]["task_id"] == task_id
    assert any(item["path"] == "prospectus/model_response.txt" for item in trace["artifacts"])
    artifact = client.get(f"/api/jobs/{task_id}/artifacts/prospectus/model_response.txt")
    assert artifact.text == "raw model text"


def test_historical_pdf_page_renders_from_task_storage_after_preview_cache_is_lost(tmp_path, monkeypatch):
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "历史任务 PDF 页面")
    pdf_content = document.tobytes()
    document.close()

    store = TaskAuditStore(tmp_path / "tasks")
    task_id = "pdf_preview_task_12345"
    job = {
        "job_id": task_id,
        "run_id": task_id,
        "status": "completed",
        "stage": "completed",
        "created_at": "2026-11-05T00:00:00+00:00",
        "result": {
            "documents": {
                "prospectus": {
                    "filename": "prospectus.pdf",
                    "page_count": 1,
                    "preview_id": "expired-preview-id",
                },
            },
        },
    }
    store.create_task(
        task_id,
        job,
        {"prospectus": ("prospectus.pdf", pdf_content)},
        {"provider": "TestProvider", "model": "test"},
    )
    monkeypatch.setattr(main, "AUDIT_STORE", store)
    monkeypatch.setattr(main, "PREVIEW_PDFS", {})
    client = TestClient(main.app)

    result = client.get(f"/api/jobs/{task_id}").json()["result"]
    assert result["documents"]["prospectus"]["task_id"] == task_id

    image = client.get(f"/api/jobs/{task_id}/documents/prospectus/pages/1/image")
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"
    assert image.content.startswith(b"\x89PNG\r\n\x1a\n")
