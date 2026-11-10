import json

import pytest

from app.audit import TaskAuditStore


def test_task_audit_keeps_inputs_events_artifacts_and_job_state(tmp_path):
    store = TaskAuditStore(tmp_path / "tasks")
    task_id = "test_task_12345678"
    job = {
        "job_id": task_id,
        "status": "queued",
        "stage": "queued",
        "created_at": "2026-11-05T00:00:00+00:00",
        "result": None,
    }

    store.create_task(
        task_id,
        job,
        {
            "prospectus": ("prospectus.pdf", b"%PDF-prospectus"),
            "periodic_report": ("report.pdf", b"%PDF-report"),
        },
        {"provider": "TestProvider", "model": "test-model"},
    )
    store.record_event(task_id, "model_response_received", {"duration_ms": 42})
    store.write_artifact(task_id, "prospectus/model_response.txt", "raw response")
    job["status"] = "completed"
    store.save_job(task_id, job)

    trace = store.task_trace(task_id)
    assert trace["task"]["documents"]["prospectus"]["sha256"]
    assert trace["job"]["status"] == "completed"
    assert [event["event"] for event in trace["events"]] == [
        "task_created", "model_response_received",
    ]
    assert store.artifact_path(task_id, "prospectus/model_response.txt").read_text() == "raw response"
    assert store.load_job(task_id)["status"] == "completed"
    assert json.loads((tmp_path / "tasks" / task_id / "metadata.json").read_text())["owner_id"] is None


def test_task_audit_rejects_invalid_ids_and_artifact_traversal(tmp_path):
    store = TaskAuditStore(tmp_path / "tasks")

    with pytest.raises(ValueError, match="TASK_ID_INVALID"):
        store.task_dir("../bad")
    with pytest.raises(ValueError, match="TASK_ARTIFACT_PATH_INVALID"):
        store.write_artifact("test_task_12345678", "../outside.txt", "no")
