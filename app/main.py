from __future__ import annotations

import asyncio
import os
import secrets
import traceback
from collections import OrderedDict
from datetime import date, datetime, timezone
from typing import Literal
from zoneinfo import ZoneInfo
from pathlib import Path
from threading import RLock

import pymupdf as fitz
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.ai import make_provider
from app.audit import TaskAuditStore
from app.runtime import InvestmentComparisonWorkflow, ProductSummaryVerificationWorkflow, WorkflowContext, WorkflowEngine, WorkflowRegistry
from app.summary_check import RULE_VERSION as SUMMARY_RULE_VERSION
from app.rule_store import RuleConflictError, get_rule, list_rules, save_rule
from app.versioning import runtime_source_fingerprint

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(os.getenv("PROSPECTUS_ENV_FILE", str(ROOT / ".env")), override=False)
MAX_UPLOAD_BYTES = int(os.getenv("PROSPECTUS_MAX_UPLOAD_BYTES", str(40 * 1024 * 1024)))
_task_log_root = Path(os.getenv("PROSPECTUS_TASK_LOG_DIR", str(ROOT / "var" / "tasks"))).expanduser()
if not _task_log_root.is_absolute():
    _task_log_root = ROOT / _task_log_root
AUDIT_STORE = TaskAuditStore(_task_log_root)
PREVIEW_LIMIT = 4
PREVIEW_PDFS: OrderedDict[str, tuple[str, bytes]] = OrderedDict()
JOB_LIMIT = 20
JOBS: OrderedDict[str, dict] = OrderedDict(
    (job["job_id"], job)
    for job in reversed(AUDIT_STORE.recent_jobs(JOB_LIMIT))
    if job.get("job_id")
)
JOB_TASKS: dict[str, asyncio.Task] = {}
JOB_LOCK = RLock()
PREVIEW_LOCK = RLock()

def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _recover_interrupted_jobs() -> None:
    for job in AUDIT_STORE.recent_jobs(100):
        if job.get("status") not in {"queued", "running"}:
            continue
        owner_pid = job.get("owner_pid")
        if isinstance(owner_pid, int) and owner_pid > 0:
            try:
                os.kill(owner_pid, 0)
            except ProcessLookupError:
                pass
            except PermissionError:
                continue
            else:
                # Another local service may share this audit directory.
                continue
        job["status"] = "failed"
        job["stage"] = "interrupted"
        job["message"] = "服务重启时任务尚未完成"
        job["error"] = "TASK_INTERRUPTED_BY_RESTART"
        job["completed_at"] = _utc_now()
        job["steps"] = {
            key: ("interrupted" if value in {"queued", "running"} else value)
            for key, value in (job.get("steps") or {}).items()
        }
        AUDIT_STORE.save_job(str(job["job_id"]), job)
        AUDIT_STORE.record_event(str(job["job_id"]), "job_interrupted", {
            "reason": "service_restart",
        })
        JOBS[str(job["job_id"])] = job


_recover_interrupted_jobs()


app = FastAPI(title="招募说明书核对", version="0.1.0")
PROCESS_SOURCE_FINGERPRINT = runtime_source_fingerprint(ROOT)
provider = make_provider()
workflow_registry = WorkflowRegistry([InvestmentComparisonWorkflow(), ProductSummaryVerificationWorkflow()])
workflow_engine = WorkflowEngine(workflow_registry)


class RuleUpdatePayload(BaseModel):
    content: str = Field(max_length=512_000)
    expected_sha256: str = Field(min_length=64, max_length=64)


@app.get("/api/rules")
async def get_rules() -> dict:
    try:
        return {"rules": list_rules()}
    except (OSError, UnicodeError) as exc:
        raise HTTPException(status_code=500, detail="读取规则目录失败") from exc


@app.get("/api/rules/{scope}/{filename}")
async def get_rule_file(scope: str, filename: str) -> dict:
    try:
        return get_rule(scope, filename)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (OSError, UnicodeError) as exc:
        raise HTTPException(status_code=500, detail="读取规则文件失败") from exc


@app.put("/api/rules/{scope}/{filename}")
async def update_rule_file(scope: str, filename: str, payload: RuleUpdatePayload) -> dict:
    try:
        return save_rule(scope, filename, payload.content, payload.expected_sha256)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuleConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (OSError, UnicodeError) as exc:
        raise HTTPException(status_code=500, detail="保存规则文件失败") from exc


def _save_preview(filename: str, content: bytes) -> str:
    preview_id = secrets.token_urlsafe(18)
    with PREVIEW_LOCK:
        PREVIEW_PDFS[preview_id] = (filename, content)
        while len(PREVIEW_PDFS) > PREVIEW_LIMIT:
            PREVIEW_PDFS.popitem(last=False)
    return preview_id


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {
        "status": "ok", "workflow": workflow_registry.list_ids()[0],
        "source_fingerprint": PROCESS_SOURCE_FINGERPRINT,
    }


@app.get("/v1/workflows")
async def list_workflows() -> dict[str, list[dict[str, str]]]:
    return {"workflows": [{"id": workflow_id} for workflow_id in workflow_registry.list_ids()]}


async def read_pdf(upload: UploadFile) -> bytes:
    filename = Path(upload.filename or "").name
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=415, detail=f"{filename or '文件'}不是 PDF")
    data = await upload.read(MAX_UPLOAD_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail=f"{filename}为空")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"{filename}超过单文件大小限制")
    if not data.startswith(b"%PDF-"):
        raise HTTPException(status_code=415, detail=f"{filename}文件内容不是有效 PDF")
    return data


async def _prepare_comparison(
    workflow_id: str,
    prospectus: UploadFile,
    periodic_report: UploadFile,
) -> WorkflowContext:
    if workflow_id not in workflow_registry.list_ids():
        raise HTTPException(status_code=404, detail=f"UNKNOWN_WORKFLOW: {workflow_id}")
    prospectus_content, periodic_content = await asyncio.gather(
        read_pdf(prospectus), read_pdf(periodic_report)
    )
    prospectus_name = Path(prospectus.filename or "招募说明书.pdf").name
    periodic_name = Path(periodic_report.filename or "定期报告.pdf").name
    return WorkflowContext(
        input_val={
            "prospectus_filename": prospectus_name,
            "prospectus_content": prospectus_content,
            "prospectus_preview_id": _save_preview(prospectus_name, prospectus_content),
            "periodic_filename": periodic_name,
            "periodic_content": periodic_content,
            "periodic_preview_id": _save_preview(periodic_name, periodic_content),
        },
        provider=provider,
        run_id=secrets.token_urlsafe(18),
    )


async def _run_comparison(
    workflow_id: str,
    context: WorkflowContext,
    on_event=None,
) -> dict:
    result = None
    async for event in workflow_engine.stream(workflow_id, context):
        if on_event:
            on_event(event)
        if event.get("event") == "workflow.completed":
            result = event.get("data", {}).get("result")
    if not isinstance(result, dict):
        raise RuntimeError("WORKFLOW_RESULT_MISSING")
    result["run_id"] = context.run_id
    return result


def _comparison_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, RuntimeError):
        message = str(exc)
        code = 503 if message.startswith("AI_NOT_CONFIGURED:") else 422 if message.startswith(("PDF_", "UNRESOLVED_")) else 502
        return HTTPException(status_code=code, detail=message)
    if isinstance(exc, ValueError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=502, detail=f"核对失败：{type(exc).__name__}: {exc}")


async def _compare_uploads(
    workflow_id: str,
    prospectus: UploadFile,
    periodic_report: UploadFile,
) -> dict:
    try:
        context = await _prepare_comparison(workflow_id, prospectus, periodic_report)
        return await _run_comparison(workflow_id, context)
    except HTTPException:
        raise
    except Exception as exc:
        raise _comparison_http_error(exc) from exc


def _job_view(job: dict, include_result: bool = False) -> dict:
    view = AUDIT_STORE.summarize(job)
    view["trace_url"] = f"/api/jobs/{job.get('job_id')}/trace"
    if include_result and job.get("result") is not None:
        result = dict(job["result"])
        documents = result.get("documents")
        if isinstance(documents, dict):
            result["documents"] = {
                role: {**document, "task_id": job.get("job_id")}
                if isinstance(document, dict) else document
                for role, document in documents.items()
            }
        view["result"] = result
    return view


def _update_job(job_id: str, **updates) -> None:
    with JOB_LOCK:
        job = JOBS.get(job_id)
        if job:
            job.update(updates)
            job["updated_at"] = _utc_now()
            AUDIT_STORE.save_job(job_id, job)


def _record_task_trace(job_id: str, item: dict) -> None:
    if item.get("kind") == "artifact":
        relative_path = AUDIT_STORE.write_artifact(job_id, str(item["path"]), item.get("content"))
        file_path = AUDIT_STORE.artifact_path(job_id, relative_path)
        AUDIT_STORE.record_event(job_id, "artifact_saved", {
            "document_role": item.get("document_role"),
            "path": relative_path,
            "size_bytes": file_path.stat().st_size if file_path else None,
        })
        return
    event = str(item.get("event") or "trace_event")
    details = dict(item.get("details") or {})
    if item.get("document_role"):
        details["document_role"] = item["document_role"]
    AUDIT_STORE.record_event(job_id, event, details)


def _runtime_trace_info() -> dict:
    return {
        "provider": type(provider).__name__,
        "model": getattr(provider, "model_name", None),
        "reasoning_effort": getattr(provider, "reasoning_effort", None),
        "request_interval_seconds": getattr(provider, "request_interval", None),
        "python": os.sys.version.split()[0],
        "source_fingerprint": PROCESS_SOURCE_FINGERPRINT,
        "summary_rules_version": SUMMARY_RULE_VERSION,
    }


async def _process_job(job_id: str, workflow_id: str, context: WorkflowContext) -> None:
    steps = dict(context.input_val.get("steps") or {"preflight": "queued", "prospectus": "queued", "periodic_report": "queued", "comparison": "queued"})
    active_stage = None
    _update_job(
        job_id, status="running", stage="starting", message="后台核对已开始",
        started_at=_utc_now(), steps=dict(steps),
    )
    AUDIT_STORE.record_event(job_id, "job_started", {"workflow_id": workflow_id})

    def record_progress(event: dict) -> None:
        if event.get("event") != "workflow.progress":
            return
        data = event.get("data", {})
        nonlocal active_stage
        stage = data.get("stage", "running")
        step_status = data.get("step_status")
        if stage in steps and step_status:
            steps[stage] = step_status
            if step_status == "running":
                active_stage = stage
        _update_job(job_id, stage=stage, message=data.get("message", "正在核对"), steps=dict(steps))
        AUDIT_STORE.record_event(job_id, "workflow_stage", {
            "stage": stage,
            "step_status": step_status,
            "message": data.get("message"),
        })

    try:
        result = await _run_comparison(workflow_id, context, record_progress)
        blocked = bool((result.get("preflight") or {}).get("blocked"))
        final_steps = dict(steps)
        if not blocked:
            final_steps["comparison"] = "completed"
        _update_job(
            job_id,
            status="completed",
            stage="completed",
            message="文件不适配，无法比较" if blocked else "核对完成，点击查看报告结果",
            completed_at=_utc_now(),
            steps=final_steps,
            result=result,
        )
        AUDIT_STORE.record_event(job_id, "job_completed", {
            "difference_count": result.get("difference_count"),
            "warning_count": len(result.get("warnings", [])),
            "preflight_status": (result.get("preflight") or {}).get("status"),
        })
    except Exception as exc:
        AUDIT_STORE.write_artifact(job_id, "errors/traceback.txt", traceback.format_exc())
        error = _comparison_http_error(exc)
        if active_stage in steps:
            steps[active_stage] = "failed"
        for step, step_status in steps.items():
            if step_status == "running":
                steps[step] = "interrupted"
        _update_job(
            job_id,
            status="failed",
            stage="failed",
            message="核对失败",
            steps=dict(steps),
            error=error.detail,
            completed_at=_utc_now(),
        )
        AUDIT_STORE.record_event(job_id, "job_failed", {
            "error": error.detail,
            "exception_type": type(exc).__name__,
        })
    finally:
        with JOB_LOCK:
            JOB_TASKS.pop(job_id, None)


@app.post("/api/jobs", status_code=202)
async def create_comparison_job(
    prospectus: UploadFile = File(...),
    periodic_report: UploadFile = File(...),
    force_refresh: bool = Form(False),
) -> dict:
    try:
        context = await _prepare_comparison("prospectus-periodic-comparison", prospectus, periodic_report)
        context.input_val["force_refresh"] = force_refresh
    except HTTPException:
        raise
    except Exception as exc:
        raise _comparison_http_error(exc) from exc

    job_id = context.run_id
    now = _utc_now()
    with JOB_LOCK:
        while len(JOBS) >= JOB_LIMIT:
            completed_id = next(
                (key for key, job in JOBS.items() if job["status"] in ("completed", "failed")),
                None,
            )
            if completed_id is None:
                raise HTTPException(status_code=429, detail="后台核对任务过多，请稍后再提交")
            JOBS.pop(completed_id)
        job = {
            "job_id": job_id,
            "run_id": job_id,
            "workflow_id": "prospectus-periodic-comparison",
            "status": "queued",
            "stage": "queued",
            "message": "已加入后台核对队列",
            "created_at": now,
            "started_at": None,
            "updated_at": now,
            "completed_at": None,
            "steps": {"preflight": "queued", "prospectus": "queued", "periodic_report": "queued", "comparison": "queued"},
            "error": None,
            "result": None,
            "document_names": {"prospectus": context.input_val["prospectus_filename"],
                               "periodic_report": context.input_val["periodic_filename"]},
        }
        context.trace_callback = lambda item: _record_task_trace(job_id, item)
        try:
            AUDIT_STORE.create_task(
                job_id,
                job,
                {
                    "prospectus": (
                        context.input_val["prospectus_filename"],
                        context.input_val["prospectus_content"],
                    ),
                    "periodic_report": (
                        context.input_val["periodic_filename"],
                        context.input_val["periodic_content"],
                    ),
                },
                _runtime_trace_info(),
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"任务追踪记录初始化失败：{type(exc).__name__}: {exc}") from exc
        JOBS[job_id] = job
        JOB_TASKS[job_id] = asyncio.create_task(
            _process_job(job_id, "prospectus-periodic-comparison", context)
        )
    return _job_view(job)


async def _read_optional_pdf(upload: UploadFile | None, role: str) -> tuple[str, bytes] | None:
    if upload is None or not upload.filename:
        return None
    content = await read_pdf(upload)
    return Path(upload.filename).name, content


@app.post("/api/product-summary/jobs", status_code=202)
async def create_product_summary_job(
    product_summary: UploadFile = File(...),
    periodic_report: UploadFile = File(...),
    prospectus: UploadFile | None = File(None),
) -> dict:
    if not periodic_report or not periodic_report.filename:
        raise HTTPException(status_code=422, detail="产品资料概要核对必须上传定期报告")
    uploads = {"product_summary": product_summary, "periodic_report": periodic_report, "prospectus": prospectus}
    loaded = await asyncio.gather(*(_read_optional_pdf(upload, role) for role, upload in uploads.items()))
    source_docs: dict[str, dict] = {}
    stored_docs: dict[str, tuple[str, bytes]] = {}
    for role, value in zip(uploads, loaded):
        if value is None:
            continue
        filename, content = value
        source_docs[role] = {
            "filename": filename, "content": content,
            "preview_id": _save_preview(filename, content),
            "metadata": {},
        }
        stored_docs[role] = (filename, content)
    if "product_summary" not in source_docs or "periodic_report" not in source_docs:
        raise HTTPException(status_code=422, detail="产品资料概要和定期报告为必需文件")
    run_id = secrets.token_urlsafe(18)
    context = WorkflowContext(
        input_val={"documents": source_docs, "input_contract_version": "product-summary-input-v2",
                   "rules_version": SUMMARY_RULE_VERSION, "result_schema_version": 3,
                   "steps": {"preflight": "queued", "source_index": "queued", "primary_lookup": "queued", "fallback_lookup": "queued", "comparison": "queued"}},
        provider=provider, run_id=run_id,
    )
    job_id, now = run_id, _utc_now()
    steps = dict(context.input_val["steps"])
    job = {
        "job_id": job_id, "run_id": run_id, "workflow_id": "product-summary-verification",
        "input_contract_version": "product-summary-input-v2", "rules_version": SUMMARY_RULE_VERSION,
        "result_schema_version": 3,
        "status": "queued", "stage": "queued", "message": "已加入后台核对队列",
        "created_at": now, "started_at": None, "updated_at": now, "completed_at": None,
        "steps": steps, "error": None, "result": None,
        "document_names": {role: value[0] for role, value in stored_docs.items()},
    }
    context.trace_callback = lambda item: _record_task_trace(job_id, item)
    with JOB_LOCK:
        while len(JOBS) >= JOB_LIMIT:
            completed_id = next((key for key, value in JOBS.items() if value["status"] in ("completed", "failed")), None)
            if completed_id is None:
                raise HTTPException(status_code=429, detail="后台核对任务过多，请稍后再提交")
            JOBS.pop(completed_id)
        try:
            AUDIT_STORE.create_task(job_id, job, stored_docs, _runtime_trace_info())
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"任务追踪记录初始化失败：{type(exc).__name__}: {exc}") from exc
        JOBS[job_id] = job
        JOB_TASKS[job_id] = asyncio.create_task(_process_job(job_id, "product-summary-verification", context))
    return _job_view(job)


@app.get("/api/jobs")
async def list_comparison_jobs() -> dict:
    return {"jobs": [_job_view(job) for job in AUDIT_STORE.recent_jobs(100)]}


@app.get("/api/tasks")
async def query_comparison_tasks(
    q: str = Query("", max_length=200),
    status: Literal["queued", "running", "completed", "failed"] | None = None,
    result_status: str | None = Query(None, max_length=40),
    start_date: date | None = None,
    end_date: date | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
) -> dict:
    if start_date and end_date and start_date > end_date:
        raise HTTPException(status_code=422, detail="开始日期不能晚于结束日期")
    keyword = q.strip().casefold()
    matches = []
    for job in AUDIT_STORE.job_summaries():
        if status and job.get("status") != status:
            continue
        if result_status and job.get("result_status") != result_status:
            continue
        search = [job.get("job_id"), job.get("fund_name"), job.get("fund_code"), *(job.get("document_names") or {}).values()]
        if keyword and not any(keyword in str(value or "").casefold() for value in search):
            continue
        if start_date or end_date:
            try:
                created = datetime.fromisoformat(str(job["created_at"]).replace("Z", "+00:00"))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                local_date = created.astimezone(ZoneInfo("Asia/Shanghai")).date()
            except (ValueError, TypeError, KeyError):
                continue
            if (start_date and local_date < start_date) or (end_date and local_date > end_date):
                continue
        matches.append(job)
    offset = (page - 1) * page_size
    return {"tasks": matches[offset:offset + page_size], "total": len(matches), "page": page, "page_size": page_size}


@app.get("/api/jobs/{job_id}/trace")
async def get_comparison_job_trace(job_id: str) -> dict:
    try:
        trace = AUDIT_STORE.task_trace(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if trace is None:
        raise HTTPException(status_code=404, detail="任务追踪记录不存在")
    return trace


@app.get("/api/jobs/{job_id}/artifacts/{artifact_path:path}")
async def get_comparison_job_artifact(job_id: str, artifact_path: str) -> FileResponse:
    try:
        path = AUDIT_STORE.artifact_path(job_id, artifact_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if path is None:
        raise HTTPException(status_code=404, detail="任务产物不存在")
    return FileResponse(path, filename=path.name)


@app.get("/api/jobs/{job_id}")
async def get_comparison_job(job_id: str) -> dict:
    with JOB_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        try:
            job = AUDIT_STORE.load_job(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if job is None:
        raise HTTPException(status_code=404, detail="核对任务不存在或已过期")
    return _job_view(job, include_result=True)


@app.post("/v1/workflows/{workflow_id}/runs")
async def create_workflow_run(
    workflow_id: str,
    prospectus: UploadFile = File(...),
    periodic_report: UploadFile = File(...),
) -> dict:
    """REST run endpoint; the workflow itself implements the base stream contract."""
    return await _compare_uploads(workflow_id, prospectus, periodic_report)


@app.get("/api/documents/{preview_id}/pages/{page_number}/image")
async def render_page(preview_id: str, page_number: int, scale: float = 1.5) -> Response:
    with PREVIEW_LOCK:
        entry = PREVIEW_PDFS.get(preview_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="PDF 预览已失效，请重新上传")
    filename, content = entry
    try:
        document = fitz.open(stream=content, filetype="pdf")
        if not 1 <= page_number <= document.page_count:
            document.close()
            raise HTTPException(status_code=404, detail="PDF 页码超出范围")
        page = document[page_number - 1]
        pixmap = page.get_pixmap(matrix=fitz.Matrix(max(0.6, min(float(scale), 2.5)), max(0.6, min(float(scale), 2.5))), alpha=False)
        image = pixmap.tobytes("png")
        document.close()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"PDF 页面渲染失败：{filename}") from exc
    return Response(content=image, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.get("/api/jobs/{job_id}/documents/{document_role}/pages/{page_number}/image")
async def render_job_page(job_id: str, document_role: str, page_number: int, scale: float = 1.5) -> Response:
    if document_role not in {"prospectus", "periodic_report", "product_summary", "annual_report"}:
        raise HTTPException(status_code=404, detail="任务文档不存在")
    try:
        pdf_path = AUDIT_STORE.artifact_path(job_id, f"inputs/{document_role}.pdf")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if pdf_path is None:
        raise HTTPException(status_code=404, detail="任务原始 PDF 不存在或已清理")
    try:
        document = fitz.open(pdf_path)
        if not 1 <= page_number <= document.page_count:
            document.close()
            raise HTTPException(status_code=404, detail="PDF 页码超出范围")
        page = document[page_number - 1]
        factor = max(0.6, min(float(scale), 2.5))
        pixmap = page.get_pixmap(matrix=fitz.Matrix(factor, factor), alpha=False)
        image = pixmap.tobytes("png")
        document.close()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"PDF 页面渲染失败：{pdf_path.name}") from exc
    return Response(content=image, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})

app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")
