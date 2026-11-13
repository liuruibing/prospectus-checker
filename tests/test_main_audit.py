from fastapi.testclient import TestClient
import pymupdf as fitz

from app import main
from app.audit import TaskAuditStore


def test_other_service_recovery_keeps_live_owner_and_recovers_dead_owner(tmp_path, monkeypatch):
    import os
    store = TaskAuditStore(tmp_path / 'tasks')
    monkeypatch.setattr(main, 'AUDIT_STORE', store)
    monkeypatch.setattr(main, 'JOBS', {})
    for task_id in ('live_owner_123', 'dead_owner_123'):
        job = {'job_id': task_id, 'status': 'running', 'steps': {'comparison': 'running'}}
        store.create_task(task_id, job, {}, {})
    real_kill = os.kill
    def check_owner(pid, signal):
        if pid == 999999999:
            raise ProcessLookupError()
        return real_kill(pid, signal)
    dead = store.load_job('dead_owner_123')
    dead['owner_pid'] = 999999999
    store.save_job('dead_owner_123', dead)
    monkeypatch.setattr(main.os, 'kill', check_owner)
    main._recover_interrupted_jobs()
    assert store.load_job('live_owner_123')['status'] == 'running'
    assert store.load_job('dead_owner_123')['status'] == 'failed'


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


def test_task_query_covers_all_history_and_supports_filters_and_pagination(tmp_path, monkeypatch):
    store = TaskAuditStore(tmp_path / "tasks")
    for index in range(105):
        task_id = f"task_history_{index:04d}"
        job = {"job_id": task_id, "status": "failed" if index == 0 else "completed",
               "created_at": "2026-11-05T16:15:00+00:00" if index == 0 else "2026-11-06T02:00:00+00:00",
               "error": "测试失败原因" if index == 0 else None,
               "result": None if index == 0 else {"status": "MATCH", "difference_count": 0}}
        store.create_task(task_id, job, {"prospectus": (f"基金{index}招募书.pdf", b"pdf"),
                                       "periodic_report": ("定期报告.pdf", b"pdf")}, {})
    # Older task directories have only metadata and job files.
    (store.task_dir("task_history_0000") / "summary.json").unlink()
    monkeypatch.setattr(main, "AUDIT_STORE", store)
    monkeypatch.setattr(main, "JOBS", {})
    client = TestClient(main.app)
    response = client.get("/api/tasks", params={"page": 11, "page_size": 10}).json()
    assert response["total"] == 105 and len(response["tasks"]) == 5
    failed = client.get("/api/tasks", params={"q": "task_history_0000", "status": "failed",
                                             "start_date": "2026-11-06", "end_date": "2026-11-06"}).json()
    assert failed["total"] == 1
    row = failed["tasks"][0]
    assert row["document_names"]["prospectus"] == "基金0招募书.pdf"
    assert row["error"] == "测试失败原因" and "result" not in row
    assert client.get("/api/tasks", params={"q": "基金104招募书"}).json()["total"] == 1
    assert client.get("/api/tasks", params={"result_status": "MATCH"}).json()["total"] == 104
    assert client.get("/api/tasks", params={"q": "不存在"}).json()["total"] == 0
    assert client.get("/api/tasks", params={"page": 0}).status_code == 422
    assert client.get("/api/tasks", params={"start_date": "2026-11-07", "end_date": "2026-11-06"}).status_code == 422
    assert client.get("/api/jobs/task_history_0000").json()["status"] == "failed"


def test_rerun_creates_new_job_id_from_stored_input_pdfs(tmp_path, monkeypatch):
    store = TaskAuditStore(tmp_path / "tasks")
    monkeypatch.setattr(main, "AUDIT_STORE", store)
    monkeypatch.setattr(main, "JOBS", {})
    monkeypatch.setattr(main, "JOB_TASKS", {})
    pdf_a = fitz.open()
    pdf_a.new_page()
    bytes_a = pdf_a.tobytes()
    pdf_a.close()
    original_job = {
        "job_id": "orig_task_1234",
        "workflow_id": "prospectus-periodic-comparison",
        "status": "completed",
        "document_names": {"prospectus": "样本招书.pdf", "periodic_report": "样本季报.pdf"},
        "created_at": "2026-11-11T00:00:00+00:00",
        "result": {"status": "UNRESOLVED", "difference_count": 0},
    }
    store.create_task(
        "orig_task_1234",
        original_job,
        {"prospectus": ("样本招书.pdf", bytes_a), "periodic_report": ("样本季报.pdf", bytes_a)},
        {},
    )
    captured = {}

    async def fake_create_comparison_job(prospectus, periodic_report, force_refresh=False):
        captured["prospectus_name"] = prospectus.filename
        captured["periodic_name"] = periodic_report.filename
        captured["prospectus_bytes"] = await prospectus.read()
        captured["force_refresh"] = force_refresh
        return {"job_id": "new_task_5678", "status": "queued", "workflow_id": "prospectus-periodic-comparison"}

    monkeypatch.setattr(main, "create_comparison_job", fake_create_comparison_job)
    client = TestClient(main.app)
    resp = client.post("/api/jobs/orig_task_1234/rerun")
    assert resp.status_code == 202
    assert resp.json()["job_id"] == "new_task_5678"
    assert captured["prospectus_name"] == "样本招书.pdf"
    assert captured["periodic_name"] == "样本季报.pdf"
    assert captured["prospectus_bytes"] == bytes_a
    assert captured["force_refresh"] is True
    assert client.post("/api/jobs/missing_task_999/rerun").status_code == 404
