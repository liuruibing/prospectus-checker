from collections import OrderedDict
import json

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


def test_historical_report_roles_render_from_task_storage(tmp_path, monkeypatch):
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "第四季度报告")
    pdf_content = document.tobytes()
    document.close()
    task_id = "historical_pdf_task_12345"
    store = TaskAuditStore(tmp_path / "tasks")
    store.create_task(task_id, {
        "job_id": task_id, "run_id": task_id, "status": "completed", "stage": "completed",
        "workflow_id": "historical-performance-verification", "created_at": "2026-11-19T00:00:00+00:00",
        "document_names": {"prospectus": "招募说明书.pdf", "historical_report_001": "2025年第4季度报告.pdf"},
        "result": {"documents": {"prospectus": {"filename": "招募说明书.pdf", "page_count": 1},
                                  "historical_report_001": {"filename": "2025年第4季度报告.pdf", "page_count": 1}}},
    }, {"prospectus": ("招募说明书.pdf", pdf_content), "historical_report_001": ("2025年第4季度报告.pdf", pdf_content)}, {})
    monkeypatch.setattr(main, "AUDIT_STORE", store)
    response = TestClient(main.app).get(f"/api/jobs/{task_id}/documents/historical_report_001/pages/1/image")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"


def test_historical_job_submission_is_removed_from_public_entrypoints():
    document = fitz.open()
    document.new_page().insert_text((72, 72), "样本 PDF")
    pdf_content = document.tobytes()
    document.close()
    response = TestClient(main.app).post("/api/historical-performance/jobs", files=[
        ("prospectus", ("招募说明书.pdf", pdf_content, "application/pdf")),
        ("periodic_reports", ("2024年第4季度报告.pdf", pdf_content, "application/pdf")),
    ])
    assert response.status_code == 404
    assert {item["id"] for item in TestClient(main.app).get("/v1/workflows").json()["workflows"]} == {
        "prospectus-periodic-comparison", "product-summary-verification",
    }


def test_single_report_legacy_rerun_uses_regular_comparison_workflow(tmp_path, monkeypatch):
    store = TaskAuditStore(tmp_path / "tasks")
    pdf_content = b"%PDF-1.4 legacy test"
    task_id = "legacy_single_report_123"
    store.create_task(task_id, {
        "job_id": task_id, "run_id": task_id, "status": "completed", "stage": "completed",
        "workflow_id": "historical-performance-verification", "created_at": "2026-11-19T00:00:00+00:00",
        "document_names": {"prospectus": "招募说明书.pdf", "historical_report_001": "2024年年报.pdf"},
    }, {"prospectus": ("招募说明书.pdf", pdf_content), "historical_report_001": ("2024年年报.pdf", pdf_content)}, {})
    monkeypatch.setattr(main, "AUDIT_STORE", store)
    monkeypatch.setattr(main, "JOBS", {})
    calls = []

    async def regular_workflow(prospectus, periodic_report, force_refresh=False):
        calls.append((prospectus.filename, periodic_report.filename, force_refresh))
        return {"job_id": "new_regular_task", "workflow_id": "prospectus-periodic-comparison"}

    monkeypatch.setattr(main, "create_comparison_job", regular_workflow)
    response = TestClient(main.app).post(f"/api/jobs/{task_id}/rerun")
    assert response.status_code == 202
    assert calls == [("招募说明书.pdf", "2024年年报.pdf", True)]


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


def test_intake_preview_suggest_and_submit_enforce_exact_portfolio_period_for_investment(monkeypatch):
    def make_pdf(*lines: str) -> bytes:
        doc = fitz.open()
        page = doc.new_page(width=600, height=900)
        for idx, line in enumerate(lines):
            page.insert_text((40, 50 + idx * 20), line, fontname="china-s", fontsize=11)
        data = doc.tobytes()
        doc.close()
        return data

    files_payload = [
        (
            "files",
            (
                "样本基金更新招募说明书.pdf",
                make_pdf(
                    "样本交易型开放式指数证券投资基金更新的招募说明书",
                    "二〇二六年一月",
                    "基金主代码：510100",
                    "投资组合报告截止日为2025年06月30日，本报告数据源于《样本基金2025年第2季度报告》。",
                ),
                "application/pdf",
            ),
        ),
        (
            "files",
            (
                "样本基金2025年第2季度报告.pdf",
                make_pdf(
                    "样本交易型开放式指数证券投资基金",
                    "2025年第2季度报告",
                    "2025年6月30日",
                    "基金主代码\n510100",
                    "送出日期：2025年7月21日",
                ),
                "application/pdf",
            ),
        ),
        (
            "files",
            (
                "样本基金2025年中期报告.pdf",
                make_pdf(
                    "样本交易型开放式指数证券投资基金",
                    "2025年中期报告",
                    "2025年6月30日",
                    "基金主代码\n510100",
                    "送出日期：2025年8月29日",
                ),
                "application/pdf",
            ),
        ),
        (
            "files",
            (
                "样本基金2025年第3季度报告.pdf",
                make_pdf(
                    "样本交易型开放式指数证券投资基金",
                    "2025年第3季度报告",
                    "2025年9月30日",
                    "基金主代码\n510100",
                    "送出日期：2025年10月28日",
                ),
                "application/pdf",
            ),
        ),
        (
            "files",
            (
                "样本基金2025年第4季度报告.pdf",
                make_pdf(
                    "样本交易型开放式指数证券投资基金",
                    "2025年第4季度报告",
                    "2025年12月31日",
                    "基金主代码\n510100",
                    "送出日期：2026年1月22日",
                ),
                "application/pdf",
            ),
        ),
        (
            "files",
            (
                "样本基金2025年年度报告.pdf",
                make_pdf(
                    "样本交易型开放式指数证券投资基金",
                    "2025年年度报告",
                    "2025年12月31日",
                    "基金主代码\n510100",
                    "送出日期：2026年3月31日",
                ),
                "application/pdf",
            ),
        ),
        (
            "files",
            (
                "样本基金产品资料概要更新.pdf",
                make_pdf(
                    "样本交易型开放式指数证券投资基金基金产品资料概要更新",
                    "编制日期：2026年1月26日",
                    "基金代码：510100",
                ),
                "application/pdf",
            ),
        ),
    ]

    class WrongAiProvider:
        async def generate_text(self, system_prompt: str, user_prompt: str) -> str:
            payload = json.loads(user_prompt)
            ai_calls.append(len(payload.get("core_documents", [])))
            matches = []
            for core in payload.get("core_documents", []):
                if core.get("role") == "prospectus":
                    matches.append({
                        "core_file_id": core["id"],
                        "periodic_file_id": doc_by_name["样本基金2025年第3季度报告.pdf"]["id"],
                        "same_product": True,
                        "confidence": 0.95,
                        "reason": "AI误配不同报告期末",
                    })
                else:
                    matches.append({
                        "core_file_id": core["id"],
                        "periodic_file_id": doc_by_name["样本基金2025年年度报告.pdf"]["id"],
                        "same_product": True,
                        "confidence": 0.95,
                        "reason": "AI误配晚于编制日期的年度报告",
                    })
            return json.dumps({"matches": matches}, ensure_ascii=False)

    doc_by_name = {}
    ai_calls = []
    monkeypatch.setattr(main, "INTAKE_BATCHES", OrderedDict())
    monkeypatch.setattr(main, "provider", WrongAiProvider())

    async def fake_compare(prospectus, periodic_report, force_refresh=False):
        return {"job_id": f"cmp_{periodic_report.filename}", "status": "queued"}

    async def fake_summary(product_summary, periodic_report, prospectus=None):
        return {"job_id": f"sum_{periodic_report.filename}", "status": "queued"}

    monkeypatch.setattr(main, "create_comparison_job", fake_compare)
    monkeypatch.setattr(main, "create_product_summary_job", fake_summary)

    client = TestClient(main.app)
    preview_resp = client.post("/api/intake/preview", files=files_payload)
    assert preview_resp.status_code == 200
    preview = preview_resp.json()
    doc_by_name = {doc["filename"]: doc for doc in preview["documents"]}

    pros = doc_by_name["样本基金更新招募说明书.pdf"]
    q2 = doc_by_name["样本基金2025年第2季度报告.pdf"]
    interim = doc_by_name["样本基金2025年中期报告.pdf"]
    q3 = doc_by_name["样本基金2025年第3季度报告.pdf"]
    q4 = doc_by_name["样本基金2025年第4季度报告.pdf"]
    annual = doc_by_name["样本基金2025年年度报告.pdf"]
    summary = doc_by_name["样本基金产品资料概要更新.pdf"]

    assert pros["role"] == "prospectus"
    assert pros["portfolio_period_end_interval"] == {"start": "2025-06-30", "end": "2025-06-30"}
    assert pros["portfolio_report_subtype"] == "quarterly"
    assert q4["report_send_date_interval"] == {"start": "2026-01-22", "end": "2026-01-22"}
    assert annual["report_send_date_interval"] == {"start": "2026-03-31", "end": "2026-03-31"}
    assert preview["suggested_matches"] == []

    # Verify AI suggestions violating portfolio_period_end or compiled_date >= report_send_date are blocked.
    unresolved_docs = {
        pros["id"]: {
            "id": pros["id"], "role": "prospectus", "filename": pros["filename"], "relative_path": pros["filename"],
            "metadata": {"fund_name": "招书未对齐名称", "portfolio_period_end_interval": pros["portfolio_period_end_interval"]},
        },
        summary["id"]: {
            "id": summary["id"], "role": "product_summary", "filename": summary["filename"], "relative_path": summary["filename"],
            "metadata": {"fund_name": "概要未对齐名称", "date_interval": summary["date_interval"]},
        },
        q3["id"]: {
            "id": q3["id"], "role": "periodic_report", "filename": q3["filename"], "relative_path": q3["filename"],
            "metadata": {"fund_name": "报告名称", "date_interval": q3["date_interval"], "report_send_date_interval": q3["report_send_date_interval"]},
        },
        annual["id"]: {
            "id": annual["id"], "role": "periodic_report", "filename": annual["filename"], "relative_path": annual["filename"],
            "metadata": {"fund_name": "报告名称", "date_interval": annual["date_interval"], "report_send_date_interval": annual["report_send_date_interval"]},
        },
    }
    import asyncio
    assert asyncio.run(main._suggest_intake_matches(unresolved_docs)) == []
    assert ai_calls == [2]

    # Reject investment submission with mismatched period_end (2025-06-30 vs 2025-09-30) and summary with compiled_date < report_send_date (2026-01-26 < 2026-03-31)
    bad_submit = client.post("/api/intake/submit", json={
        "batch_id": preview["batch_id"],
        "groups": [
            {
                "workflow": "investment",
                "core_file_id": pros["id"],
                "periodic_file_id": q3["id"],
            },
            {
                "workflow": "summary",
                "core_file_id": summary["id"],
                "periodic_file_id": annual["id"],
            },
        ],
    })
    assert bad_submit.status_code == 200
    bad_data = bad_submit.json()
    assert bad_data["jobs"] == []
    assert len(bad_data["errors"]) == 2
    assert "2025-06-30" in bad_data["errors"][0]["message"]
    assert "2025-09-30" in bad_data["errors"][0]["message"]
    assert "2026-01-26" in bad_data["errors"][1]["message"]
    assert "2026-03-31" in bad_data["errors"][1]["message"]

    # Accept matching same-period investment and summary compiled >= report_send_date (2026-01-26 >= 2026-01-22)
    good_submit = client.post("/api/intake/submit", json={
        "batch_id": preview["batch_id"],
        "groups": [
            {
                "workflow": "investment",
                "core_file_id": pros["id"],
                "periodic_file_id": interim["id"],
            },
            {
                "workflow": "summary",
                "core_file_id": summary["id"],
                "periodic_file_id": q4["id"],
            },
        ],
    })
    assert good_submit.status_code == 200
    good_data = good_submit.json()
    assert good_data["errors"] == []
    assert [job["job_id"] for job in good_data["jobs"]] == [
        "cmp_样本基金2025年中期报告.pdf",
        "sum_样本基金2025年第4季度报告.pdf",
    ]
    assert q2["role"] == "periodic_report" and interim["role"] == "periodic_report" and q3["role"] == "periodic_report" and summary["role"] == "product_summary"


def test_batch_delete_jobs_deletes_finished_and_skips_active_or_missing(tmp_path, monkeypatch):
    store = TaskAuditStore(tmp_path)
    monkeypatch.setattr(main, "AUDIT_STORE", store)
    monkeypatch.setattr(main, "JOBS", {})

    done_job = {
        "job_id": "job_done_1",
        "run_id": "job_done_1",
        "status": "completed",
        "stage": "completed",
        "created_at": "2026-11-16T00:00:00+00:00",
        "document_names": {"prospectus": "a.pdf", "periodic_report": "b.pdf"},
    }
    running_job = {
        "job_id": "job_running_1",
        "run_id": "job_running_1",
        "status": "running",
        "stage": "running",
        "created_at": "2026-11-16T00:01:00+00:00",
        "document_names": {"prospectus": "c.pdf", "periodic_report": "d.pdf"},
    }
    store.create_task("job_done_1", done_job, {"prospectus": ("a.pdf", b"%PDF-1.4")}, {})
    store.create_task("job_running_1", running_job, {"prospectus": ("c.pdf", b"%PDF-1.4")}, {})
    main.JOBS["job_done_1"] = done_job
    main.JOBS["job_running_1"] = running_job

    client = TestClient(main.app)
    resp = client.post("/api/jobs/batch-delete", json={"job_ids": ["job_done_1", "job_running_1", "job_missing"]})
    assert resp.status_code == 200
    data = resp.json()
    assert data["deleted_ids"] == ["job_done_1"]
    assert {item["job_id"] for item in data["skipped"]} == {"job_running_1", "job_missing"}
    assert store.load_job("job_done_1") is None
    assert store.load_job("job_running_1") is not None


def test_investment_submit_and_rerun_preserve_historical_reports(tmp_path, monkeypatch):
    from collections import OrderedDict
    import fitz

    store = TaskAuditStore(tmp_path / "tasks")
    monkeypatch.setattr(main, "AUDIT_STORE", store)
    monkeypatch.setattr(main, "JOBS", {})
    monkeypatch.setattr(main, "JOB_TASKS", {})
    monkeypatch.setattr(main, "INTAKE_BATCHES", OrderedDict())

    def pdf_bytes():
        doc = fitz.open()
        doc.new_page()
        raw = doc.tobytes()
        doc.close()
        return raw

    raw_pdf = pdf_bytes()
    main.INTAKE_BATCHES["batch_hist_01234567"] = {
        "expires_at": main.time.monotonic() + 300,
        "documents": {
            "f0": {"id": "f0", "role": "prospectus", "filename": "招书.pdf", "content": raw_pdf, "metadata": {"portfolio_period_end_interval": {"start": "2025-12-31", "end": "2025-12-31"}}},
            "f1": {"id": "f1", "role": "periodic_report", "filename": "2025年报.pdf", "content": raw_pdf, "metadata": {"date_interval": {"start": "2025-12-31", "end": "2025-12-31"}}},
            "f2": {"id": "f2", "role": "periodic_report", "filename": "2024年报.pdf", "content": raw_pdf, "metadata": {"date_interval": {"start": "2024-12-31", "end": "2024-12-31"}}},
        },
    }

    calls = []

    async def fake_create_comparison_job(prospectus, periodic_report, force_refresh=False, historical_reports=None):
        extra_names = [item.filename for item in (historical_reports or [])]
        calls.append({
            "prospectus": prospectus.filename,
            "periodic": periodic_report.filename,
            "force_refresh": force_refresh,
            "historicals": extra_names,
        })
        job_id = f"cmp_job_{len(calls)}"
        doc_names = {"prospectus": prospectus.filename, "periodic_report": periodic_report.filename}
        inputs = {"prospectus": (prospectus.filename, raw_pdf), "periodic_report": (periodic_report.filename, raw_pdf)}
        for idx, item in enumerate(historical_reports or [], start=1):
            role = f"historical_report_{idx:03}"
            doc_names[role] = item.filename
            inputs[role] = (item.filename, raw_pdf)
        job = {"job_id": job_id, "run_id": job_id, "workflow_id": "prospectus-periodic-comparison", "status": "queued", "document_names": doc_names}
        store.create_task(job_id, job, inputs, {})
        main.JOBS[job_id] = job
        return job

    monkeypatch.setattr(main, "create_comparison_job", fake_create_comparison_job)
    client = TestClient(main.app)

    resp = client.post("/api/intake/submit", json={
        "batch_id": "batch_hist_01234567",
        "groups": [{
            "workflow": "investment",
            "core_file_id": "f0",
            "periodic_file_id": "f1",
            "historical_file_ids": ["f2"],
        }],
    })
    assert resp.status_code == 200
    assert resp.json()["errors"] == []
    assert calls[0]["historicals"] == ["2024年报.pdf"]

    rerun_resp = client.post("/api/jobs/cmp_job_1/rerun")
    assert rerun_resp.status_code == 202
    assert calls[1]["historicals"] == ["2024年报.pdf"]
