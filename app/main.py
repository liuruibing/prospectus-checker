from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import time
import traceback
from collections import OrderedDict
from datetime import date, datetime, timezone
from io import BytesIO
from pathlib import Path
from threading import RLock
from typing import Literal
from zoneinfo import ZoneInfo

import pymupdf as fitz
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.ai import make_provider
from app.audit import TaskAuditStore
from app.comparison import compare_documents
from app.runtime import InvestmentComparisonWorkflow, ProductSummaryVerificationWorkflow, WorkflowContext, WorkflowEngine, WorkflowRegistry
from app.summary_check import RULE_VERSION as SUMMARY_RULE_VERSION
from app.summary_check import capture_product_summary_rule_snapshot
from app.extraction import capture_investment_rule_snapshot
from app.preflight import _apply_consistent_batch_fund_names, _document_metadata
from app.rule_store import RuleConflictError, get_rule, list_rules, save_rule
from app.rule_compiler import (
    RuleCompilationError, _is_plain_chinese_rule,
    compile_rule, load_investment_rule_definitions, persist_compiled_rule,
)
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
INTAKE_BATCHES: OrderedDict[str, dict] = OrderedDict()
INTAKE_TTL_SECONDS = 30 * 60
INTAKE_BATCH_LIMIT = 3
INTAKE_FILE_LIMIT = 100
INTAKE_TOTAL_BYTES_LIMIT = 160 * 1024 * 1024
INTAKE_LOCK = RLock()


def _intake_identity_matches(core: dict, report: dict) -> bool:
    left = core.get("metadata") or {}
    right = report.get("metadata") or {}
    left_code, right_code = left.get("fund_code"), right.get("fund_code")
    if left_code and right_code:
        return left_code == right_code

    def normalize(value: str | None) -> str:
        without_parentheses = re.sub(r"[（(][^）)]*[）)]", "", str(value or ""))
        return re.sub(r"基金|\s+", "", without_parentheses)

    left_name, right_name = normalize(left.get("fund_name")), normalize(right.get("fund_name"))
    return bool(left_name and right_name and left_name == right_name)


def _intake_date(metadata: dict) -> str:
    interval = metadata.get("display_date_interval") or metadata.get("date_interval") or {}
    return str(interval.get("end") or interval.get("start") or "") if isinstance(interval, dict) else ""


def _intake_portfolio_period_end(metadata: dict) -> str:
    interval = metadata.get("portfolio_period_end_interval") or {}
    return str(interval.get("end") or interval.get("start") or "") if isinstance(interval, dict) else ""


def _intake_report_period_end(metadata: dict) -> str:
    if metadata.get("display_date_label") == "定期报告送出日期":
        return ""
    return _intake_date(metadata)


def _intake_report_send_date(metadata: dict) -> str:
    interval = metadata.get("report_send_date_interval") or {}
    if isinstance(interval, dict) and (interval.get("end") or interval.get("start")):
        return str(interval.get("end") or interval.get("start") or "")
    if metadata.get("display_date_label") == "定期报告送出日期":
        return _intake_date(metadata)
    return ""


def _summary_report_send_eligible(compiled_date: str, report: dict) -> bool:
    if not compiled_date:
        return True
    metadata = report.get("metadata") or {}
    send_date = _intake_report_send_date(metadata)
    if send_date:
        return send_date <= compiled_date
    period_date = _intake_report_period_end(metadata)
    return not period_date or period_date <= compiled_date


async def _suggest_intake_matches(documents: dict[str, dict]) -> list[dict]:
    cores = [item for item in documents.values() if item["role"] in {"prospectus", "product_summary"}]
    reports = [item for item in documents.values() if item["role"] == "periodic_report"]
    unresolved = []
    for core in cores:
        strict = [report for report in reports if _intake_identity_matches(core, report)]
        if core["role"] == "prospectus":
            portfolio_period = _intake_portfolio_period_end(core["metadata"])
            if strict:
                # When fund identity already matches locally and the prospectus declares a
                # portfolio period_end, only exact same-period reports qualify; do not ask
                # AI to bridge across different reporting periods.
                if portfolio_period or any(
                    _intake_report_period_end(report["metadata"]) for report in strict
                ):
                    continue
                if len(strict) == 1:
                    continue
        else:
            core_date = _intake_date(core["metadata"])
            dated = [
                report for report in strict
                if _intake_report_send_date(report["metadata"]) or _intake_date(report["metadata"])
            ]
            has_local_recommendation = bool(strict) and (
                not core_date
                or any(_summary_report_send_eligible(core_date, report) for report in dated)
                or (not dated and len(strict) == 1)
            )
            if has_local_recommendation:
                continue
        unresolved.append(core)
    if not unresolved or not reports:
        return []

    def document_summary(item: dict) -> dict:
        metadata = item.get("metadata") or {}
        return {
            "id": item["id"],
            "role": item.get("role"),
            "filename": item["filename"],
            "relative_path": item["relative_path"],
            "fund_name": metadata.get("fund_name"),
            "fund_code": metadata.get("fund_code"),
            "fund_codes": metadata.get("fund_codes") or [],
            "document_date": _intake_date(metadata),
            "report_send_date": _intake_report_send_date(metadata),
            "date_label": metadata.get("display_date_label") or metadata.get("date_label"),
            "portfolio_period_end": _intake_portfolio_period_end(metadata),
            "portfolio_report_subtype": metadata.get("portfolio_report_subtype"),
            "report_subtype": metadata.get("report_subtype"),
            "cover_text": item.get("ai_excerpt", ""),
        }

    request_data = {"core_documents": [document_summary(item) for item in unresolved],
                    "periodic_reports": [document_summary(item) for item in reports]}
    system_prompt = (
        "你负责为基金文件建立候选关联。只根据输入的文件名、基金名称、代码和日期判断是否属于同一基金。"
        "当核心文件为招募说明书（prospectus）时，若其披露了 portfolio_period_end（投资组合报告期末），"
        "只能关联同基金且定期报告期末（document_date）与 portfolio_period_end 完全相同的定期报告，不得跨报告期匹配；"
        "当核心文件为产品概要（product_summary）时，定期报告的报告送出日期（report_send_date）必须不晚于概要编制日期（document_date >= report_send_date），报告送出日期较近且符合条件的报告优先。"
        "代码不同但名称或文件上下文能说明是同一基金时可以匹配；信息不足、可能是不同基金或招募说明书报告期末不相等时不要猜。"
        "只输出 JSON：{\"matches\":[{\"core_file_id\":\"...\","
        "\"periodic_file_id\":\"...\",\"same_product\":true,\"confidence\":0.0,\"reason\":\"简短中文理由\"}]}。"
        "每个核心文件最多给一个候选；ID 必须来自输入。"
    )
    try:
        raw = await asyncio.wait_for(
            provider.generate_text(system_prompt, json.dumps(request_data, ensure_ascii=False)),
            timeout=30,
        )
        text = str(raw).strip()
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S | re.I)
        if fenced:
            text = fenced.group(1)
        else:
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end <= start:
                return []
            text = text[start:end + 1]
        parsed = json.loads(text)
    except Exception as exc:
        print(f"intake AI matching skipped: {type(exc).__name__}")
        return []

    core_by_id = {item["id"]: item for item in unresolved}
    report_by_id = {item["id"]: item for item in reports}
    suggestions = []
    for match in parsed.get("matches", []) if isinstance(parsed, dict) else []:
        if not isinstance(match, dict):
            continue
        core_id, report_id = str(match.get("core_file_id", "")), str(match.get("periodic_file_id", ""))
        try:
            confidence = float(match.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0
        if (core_id not in core_by_id or report_id not in report_by_id or not match.get("same_product")
                or confidence < 0.75):
            continue
        core_doc = core_by_id[core_id]
        report_doc = report_by_id[report_id]
        if core_doc.get("role") == "prospectus":
            expected_period = _intake_portfolio_period_end(core_doc.get("metadata") or {})
            actual_period = _intake_report_period_end(report_doc.get("metadata") or {})
            if expected_period and actual_period and expected_period != actual_period:
                continue
        elif core_doc.get("role") == "product_summary":
            compiled_date = _intake_date(core_doc.get("metadata") or {})
            send_date = _intake_report_send_date(report_doc.get("metadata") or {})
            if compiled_date and send_date and compiled_date < send_date:
                continue
        suggestions.append({"core_file_id": core_id, "periodic_file_id": report_id,
                            "same_product": True, "confidence": confidence,
                            "reason": str(match.get("reason") or "AI 根据基金信息和报告日期建议关联")[:120]})
    return suggestions


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


class BatchTaskGroup(BaseModel):
    workflow: Literal["investment", "summary"]
    core_file_id: str = Field(min_length=1, max_length=12)
    periodic_file_id: str = Field(min_length=1, max_length=12)
    prospectus_file_id: str | None = Field(default=None, max_length=12)
    historical_file_ids: list[str] = Field(default_factory=list, max_length=20)


class BatchTaskSubmit(BaseModel):
    batch_id: str = Field(min_length=16, max_length=80)
    roles: dict[str, Literal["prospectus", "periodic_report", "product_summary", "unknown"]] = Field(default_factory=dict)
    groups: list[BatchTaskGroup] = Field(min_length=1, max_length=50)


class BatchJobDeletePayload(BaseModel):
    job_ids: list[str] = Field(min_length=1, max_length=200)


def _expire_intake_batches() -> None:
    expired = [batch_id for batch_id, batch in INTAKE_BATCHES.items() if batch["expires_at"] <= time.monotonic()]
    for batch_id in expired:
        INTAKE_BATCHES.pop(batch_id, None)


def _safe_relative_path(value: str, filename: str) -> str:
    parts = [part for part in value.replace("\\", "/").split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return filename
    return "/".join(parts)


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
        current = get_rule(scope, filename)
        if current["sha256"] != payload.expected_sha256:
            raise RuleConflictError("文件已被其他操作更新，请重新载入后再保存")
        if filename.startswith("common_"):
            public_override = {filename: payload.content}
            scope_dir = ROOT / "rules" / scope
            module_paths = sorted(path for path in scope_dir.glob("*.md")
                                  if not path.name.startswith("common_"))
            public_rules = []
            for common_path in sorted(scope_dir.glob("common_*.md")):
                public_rules.append(
                    payload.content if common_path.name == filename
                    else common_path.read_text(encoding="utf-8")
                )
            combined_common_rules = "\n\n".join(public_rules)
            concurrency = max(1, int(os.getenv("PROSPECTUS_MODEL_CONCURRENCY", "3")))
            semaphore = asyncio.Semaphore(concurrency)

            async def _compile_module(module_path: Path) -> tuple[str, dict]:
                module_source = module_path.read_text(encoding="utf-8")
                async with semaphore:
                    plan = await compile_rule(
                        scope=scope, filename=module_path.name, source_content=module_source,
                        provider=provider,
                        common_rules=combined_common_rules, public_rule_overrides=public_override,
                    )
                return module_path.name, plan

            tasks = [asyncio.create_task(_compile_module(path)) for path in module_paths]
            try:
                pending_plans = list(await asyncio.gather(*tasks))
            except Exception:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
            versions = {plan["public_rules_sha256"] for _, plan in pending_plans}
            if len(pending_plans) != len(module_paths) or len(versions) != 1:
                raise RuleCompilationError("公共规则依赖项未能整体生成同一版本，未激活任何修改。")
            # Compiled artifacts are staged under the pending public-rule hash.
            # Saving the common text last switches the complete set atomically.
            for module_filename, plan in pending_plans:
                persist_compiled_rule(scope, module_filename, plan)
            saved = save_rule(scope, filename, payload.content, payload.expected_sha256)
            saved["execution_version"] = {
                "protocol": pending_plans[0][1]["protocol_version"],
                "source_sha256": saved["sha256"],
                "compiled_rules": len(pending_plans),
                "compiled_at": max(plan["compiled_at"] for _, plan in pending_plans),
            }
            return saved
        if _is_plain_chinese_rule(payload.content):
            common_rules = "\n\n".join(
                path.read_text(encoding="utf-8")
                for path in sorted((ROOT / "rules" / scope).glob("common_*.md"))
            )
            plan = await compile_rule(
                scope=scope, filename=filename, source_content=payload.content,
                provider=provider, common_rules=common_rules,
            )
            persist_compiled_rule(scope, filename, plan)
            saved = save_rule(scope, filename, payload.content, payload.expected_sha256)
            saved["execution_version"] = {
                "protocol": plan["protocol_version"],
                "source_sha256": plan["source_sha256"],
                "model": plan["model"],
                "compiled_at": plan["compiled_at"],
            }
            return saved
        return save_rule(scope, filename, payload.content, payload.expected_sha256)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuleConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuleCompilationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
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
    historical_reports: list[UploadFile] | None = None,
) -> WorkflowContext:
    if workflow_id not in workflow_registry.list_ids():
        raise HTTPException(status_code=404, detail=f"UNKNOWN_WORKFLOW: {workflow_id}")
    valid_historicals = [item for item in (historical_reports or []) if item is not None and item.filename]
    loaded = await asyncio.gather(
        read_pdf(prospectus),
        read_pdf(periodic_report),
        *(read_pdf(item) for item in valid_historicals),
    )
    prospectus_content, periodic_content = loaded[0], loaded[1]
    prospectus_name = Path(prospectus.filename or "招募说明书.pdf").name
    periodic_name = Path(periodic_report.filename or "定期报告.pdf").name
    historical_entries = [
        {
            "role": f"historical_report_{index:03}",
            "filename": Path(item.filename or f"historical_report_{index:03}.pdf").name,
            "content": content,
            "preview_id": _save_preview(
                Path(item.filename or f"historical_report_{index:03}.pdf").name,
                content,
            ),
        }
        for index, (item, content) in enumerate(zip(valid_historicals, loaded[2:]), start=1)
    ]
    rule_snapshot = capture_investment_rule_snapshot()
    return WorkflowContext(
        input_val={
            "prospectus_filename": prospectus_name,
            "prospectus_content": prospectus_content,
            "prospectus_preview_id": _save_preview(prospectus_name, prospectus_content),
            "periodic_filename": periodic_name,
            "periodic_content": periodic_content,
            "periodic_preview_id": _save_preview(periodic_name, periodic_content),
            "historical_reports": historical_entries,
            "rule_snapshot": rule_snapshot,
            "rules_version": rule_snapshot["rules_version"],
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
        if job.get("workflow_id") == "prospectus-periodic-comparison":
            modules = result.get("modules") or []
            if any("checks" not in module for module in modules):
                result["modules"] = _restore_investment_checks(job["job_id"], result, modules)
        documents = result.get("documents")
        if isinstance(documents, dict):
            result["documents"] = {
                role: {**document, "task_id": job.get("job_id")}
                if isinstance(document, dict) else document
                for role, document in documents.items()
            }
        view["result"] = result
    return view


def _restore_investment_checks(job_id: str, result: dict, modules: list[dict]) -> list[dict]:
    """Rebuild display details from archived facts when the task's rule snapshot is available."""
    snapshot_path = AUDIT_STORE.artifact_path(job_id, "rules/rule_snapshot.json")
    if snapshot_path is None:
        return modules
    try:
        archived = json.loads(snapshot_path.read_text(encoding="utf-8"))
        if not isinstance(archived, dict) or archived.get("rules_version") != result.get("rules_version"):
            return modules
        active = capture_investment_rule_snapshot()
        if archived.get("rules_version") != active["rules_version"] or archived.get("files") != active["files"]:
            return modules
        documents = {}
        for role in ("prospectus", "periodic_report"):
            path = AUDIT_STORE.artifact_path(job_id, f"{role}/verified_result.json")
            if path is None:
                return modules
            facts = json.loads(path.read_text(encoding="utf-8"))
            documents[role] = {**facts, "document_info": result["documents"][role]["document_info"]}
        replay = compare_documents(**documents, execution_plans=active["execution_plans"])
    except (OSError, ValueError, TypeError, KeyError, RuleCompilationError):
        return modules

    replay_by_rule = {module["rule_id"]: module for module in replay["modules"]}
    restored = []
    for module in modules:
        candidate = replay_by_rule.get(module["rule_id"], {})
        if ("checks" not in module and "checks" in candidate
                and all(module.get(key) == candidate[key] for key in (
                    "status", "differences", "prospectus_rows", "periodic_rows",
                ))):
            module = {**module, "checks": candidate["checks"]}
        restored.append(module)
    return restored


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
    rule_snapshot = context.input_val.get("rule_snapshot")
    if isinstance(rule_snapshot, dict) and context.trace_callback:
        context.trace_callback({"kind": "artifact", "path": "rules/rule_snapshot.json", "content": {
            "scope": rule_snapshot.get("scope"),
            "rules_version": rule_snapshot.get("rules_version"),
            "protocol_version": rule_snapshot.get("protocol_version"),
            "prompt_version": rule_snapshot.get("prompt_version"),
            "files": rule_snapshot.get("files", {}),
        }})

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
    historical_reports: list[UploadFile] | None = File(default=None),
) -> dict:
    extra_uploads = historical_reports if isinstance(historical_reports, list) else []
    try:
        context = await _prepare_comparison(
            "prospectus-periodic-comparison", prospectus, periodic_report, extra_uploads
        )
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
        historical_entries = context.input_val.get("historical_reports") or []
        document_names = {
            "prospectus": context.input_val["prospectus_filename"],
            "periodic_report": context.input_val["periodic_filename"],
            **{item["role"]: item["filename"] for item in historical_entries},
        }
        stored_inputs = {
            "prospectus": (
                context.input_val["prospectus_filename"],
                context.input_val["prospectus_content"],
            ),
            "periodic_report": (
                context.input_val["periodic_filename"],
                context.input_val["periodic_content"],
            ),
            **{
                item["role"]: (item["filename"], item["content"])
                for item in historical_entries
            },
        }
        job = {
            "job_id": job_id,
            "run_id": job_id,
            "workflow_id": "prospectus-periodic-comparison",
            "rules_version": context.input_val["rules_version"],
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
            "document_names": document_names,
        }
        context.trace_callback = lambda item: _record_task_trace(job_id, item)
        try:
            AUDIT_STORE.create_task(
                job_id,
                job,
                stored_inputs,
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
    rule_snapshot = capture_product_summary_rule_snapshot()
    run_id = secrets.token_urlsafe(18)
    context = WorkflowContext(
        input_val={"documents": source_docs, "input_contract_version": "product-summary-input-v2",
                   "rules_version": rule_snapshot["rules_version"], "rule_snapshot": rule_snapshot,
                   "result_schema_version": 3,
                   "steps": {"preflight": "queued", "source_index": "queued", "primary_lookup": "queued", "fallback_lookup": "queued", "comparison": "queued"}},
        provider=provider, run_id=run_id,
    )
    job_id, now = run_id, _utc_now()
    steps = dict(context.input_val["steps"])
    job = {
        "job_id": job_id, "run_id": run_id, "workflow_id": "product-summary-verification",
        "input_contract_version": "product-summary-input-v2", "rules_version": rule_snapshot["rules_version"],
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


@app.post("/api/intake/preview")
async def preview_upload_batch(
    files: list[UploadFile] = File(...),
    relative_paths: list[str] = Form(default=[]),
) -> dict:
    if len(files) > INTAKE_FILE_LIMIT:
        raise HTTPException(status_code=413, detail=f"一次最多上传 {INTAKE_FILE_LIMIT} 个 PDF")
    documents = {}
    response_documents = []
    total_bytes = 0
    role_labels = {
        "prospectus": "招募说明书",
        "periodic_report": "定期报告",
        "product_summary": "产品概要",
        "unknown": "未识别",
    }
    for index, upload in enumerate(files):
        content = await read_pdf(upload)
        total_bytes += len(content)
        if total_bytes > INTAKE_TOTAL_BYTES_LIMIT:
            raise HTTPException(status_code=413, detail="这批文件总大小不能超过 160 MiB")
        filename = Path(upload.filename or f"文件{index + 1}.pdf").name
        try:
            with fitz.open(stream=content, filetype="pdf") as source_pdf:
                ai_excerpt = "\n".join(source_pdf[page].get_text() for page in range(min(2, source_pdf.page_count)))
            ai_excerpt = re.sub(r"\s+", " ", ai_excerpt).strip()[:1500]
        except Exception:
            ai_excerpt = ""
        try:
            metadata = await asyncio.to_thread(_document_metadata, filename, content, "auto")
        except Exception:
            metadata = {"detected_roles": [], "fund_name": None, "fund_code": None,
                        "display_date_interval": None, "display_date_label": None}
        detected_roles = metadata.get("detected_roles") or []
        role = detected_roles[0] if len(detected_roles) == 1 else "unknown"
        relative_path = relative_paths[index] if index < len(relative_paths) else filename
        item = {
            "id": str(index), "filename": filename,
            "relative_path": _safe_relative_path(relative_path, filename),
            "role": role, "metadata": metadata, "content": content, "ai_excerpt": ai_excerpt,
        }
        documents[item["id"]] = item
        response_documents.append({
            "id": item["id"], "filename": filename, "relative_path": item["relative_path"],
            "role": role, "role_label": role_labels.get(role, "未识别"),
            "role_candidates": detected_roles,
            "fund_name": metadata.get("fund_name"),
            "fund_full_name": metadata.get("fund_full_name"),
            "fund_short_name": metadata.get("fund_short_name"),
            "fund_code": metadata.get("fund_code"),
            "report_subtype": metadata.get("report_subtype"),
            "date_interval": metadata.get("display_date_interval") or metadata.get("date_interval"),
            "date_label": metadata.get("display_date_label") or metadata.get("date_label"),
            "report_send_date_interval": metadata.get("report_send_date_interval"),
            "report_send_date_quote": metadata.get("report_send_date_quote"),
            "portfolio_period_end_interval": metadata.get("portfolio_period_end_interval"),
            "portfolio_period_end_quote": metadata.get("portfolio_period_end_quote"),
            "portfolio_report_subtype": metadata.get("portfolio_report_subtype"),
        })
    if not documents:
        raise HTTPException(status_code=400, detail="请先选择 PDF 文件")
    _apply_consistent_batch_fund_names(response_documents)
    suggested_matches = await _suggest_intake_matches(documents)
    batch_id = secrets.token_urlsafe(24)
    with INTAKE_LOCK:
        _expire_intake_batches()
        while len(INTAKE_BATCHES) >= INTAKE_BATCH_LIMIT:
            INTAKE_BATCHES.popitem(last=False)
        INTAKE_BATCHES[batch_id] = {
            "documents": documents,
            "expires_at": time.monotonic() + INTAKE_TTL_SECONDS,
        }
    return {"batch_id": batch_id, "expires_in_seconds": INTAKE_TTL_SECONDS,
            "total_bytes": total_bytes, "documents": response_documents,
            "suggested_matches": suggested_matches}


@app.post("/api/intake/submit")
async def submit_upload_batch(payload: BatchTaskSubmit) -> dict:
    with INTAKE_LOCK:
        _expire_intake_batches()
        batch = INTAKE_BATCHES.get(payload.batch_id)
        if batch is None:
            raise HTTPException(status_code=410, detail="上传预览已过期，请重新选择文件")
        INTAKE_BATCHES.move_to_end(payload.batch_id)
        documents = batch["documents"]

    def selected_document(file_id: str, expected_role: str) -> dict:
        document = documents.get(file_id)
        if document is None:
            raise ValueError("所选文件已不存在，请重新扫描文件")
        selected_role = payload.roles.get(file_id, document["role"])
        if selected_role != expected_role:
            raise ValueError(f"{document['filename']} 应选择为{role_labels[expected_role]}")
        return document

    role_labels = {
        "prospectus": "招募说明书",
        "periodic_report": "定期报告",
        "product_summary": "产品概要",
    }
    seen_cores = set()
    created = []
    errors = []
    for index, group in enumerate(payload.groups, start=1):
        try:
            if group.core_file_id in seen_cores:
                raise ValueError("同一核心文件不能重复创建任务")
            seen_cores.add(group.core_file_id)
            core_role = "prospectus" if group.workflow == "investment" else "product_summary"
            core = selected_document(group.core_file_id, core_role)
            periodic = selected_document(group.periodic_file_id, "periodic_report")
            if group.core_file_id == group.periodic_file_id:
                raise ValueError("核心文件和定期报告不能是同一个文件")
            if group.workflow == "investment":
                prospectus_period = _intake_portfolio_period_end(core.get("metadata") or {})
                report_period = _intake_report_period_end(periodic.get("metadata") or {})
                if prospectus_period and report_period and prospectus_period != report_period:
                    raise ValueError(
                        f"招募说明书投资组合报告截止日（{prospectus_period}）与定期报告期末（{report_period}）不一致，仅允许匹配同报告期末的定期报告"
                    )
                historical_uploads: list[UploadFile] = []
                seen_historical: set[str] = set()
                for file_id in group.historical_file_ids:
                    if file_id in seen_historical:
                        continue
                    if file_id in {group.core_file_id, group.periodic_file_id}:
                        raise ValueError("历年业绩补充报告不能与招募说明书或主定期报告重复")
                    seen_historical.add(file_id)
                    extra = selected_document(file_id, "periodic_report")
                    historical_uploads.append(
                        UploadFile(filename=extra["filename"], file=BytesIO(extra["content"]))
                    )
                prospectus_upload = UploadFile(filename=core["filename"], file=BytesIO(core["content"]))
                periodic_upload = UploadFile(filename=periodic["filename"], file=BytesIO(periodic["content"]))
                job = await (
                    create_comparison_job(prospectus_upload, periodic_upload, False, historical_uploads)
                    if historical_uploads
                    else create_comparison_job(prospectus_upload, periodic_upload, False)
                )
            else:
                compiled_date = _intake_date(core.get("metadata") or {})
                send_date = _intake_report_send_date(periodic.get("metadata") or {})
                if compiled_date and send_date and compiled_date < send_date:
                    raise ValueError(
                        f"产品概要编制日期（{compiled_date}）必须大于等于定期报告送出日期（{send_date}），仅允许匹配送出日期不晚于编制日期的定期报告"
                    )
                prospectus = None
                if group.prospectus_file_id:
                    if group.prospectus_file_id in {group.core_file_id, group.periodic_file_id}:
                        raise ValueError("补充招募说明书不能与核心文件或定期报告重复")
                    supplement = selected_document(group.prospectus_file_id, "prospectus")
                    prospectus = UploadFile(filename=supplement["filename"], file=BytesIO(supplement["content"]))
                summary_upload = UploadFile(filename=core["filename"], file=BytesIO(core["content"]))
                periodic_upload = UploadFile(filename=periodic["filename"], file=BytesIO(periodic["content"]))
                job = await create_product_summary_job(summary_upload, periodic_upload, prospectus)
            created.append(job)
        except (ValueError, HTTPException) as exc:
            detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
            errors.append({"group": index, "message": detail})
        except Exception as exc:
            errors.append({"group": index, "message": f"创建失败：{type(exc).__name__}"})
    if created:
        with INTAKE_LOCK:
            INTAKE_BATCHES.pop(payload.batch_id, None)
    return {"jobs": created, "errors": errors}


@app.post("/api/jobs/{job_id}/rerun", status_code=202)
async def rerun_comparison_job(job_id: str, force_refresh: bool = Query(True)) -> dict:
    with JOB_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        try:
            job = AUDIT_STORE.load_job(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if job is None:
        raise HTTPException(status_code=404, detail="核对任务不存在")

    def stored_upload(role: str, default_name: str, *, required: bool = True) -> UploadFile | None:
        try:
            pdf_path = AUDIT_STORE.artifact_path(job_id, f"inputs/{role}.pdf")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if pdf_path is None:
            if required:
                raise HTTPException(status_code=404, detail="原任务输入 PDF 文件不存在或已清理，无法重新核对")
            return None
        doc_names = job.get("document_names") or {}
        filename = str(doc_names.get(role) or default_name)
        return UploadFile(filename=filename, file=BytesIO(pdf_path.read_bytes()))

    if job.get("workflow_id") == "product-summary-verification":
        summary_upload = stored_upload("product_summary", "产品资料概要.pdf")
        periodic_upload = stored_upload("periodic_report", "定期报告.pdf")
        prospectus_upload = stored_upload("prospectus", "招募说明书.pdf", required=False)
        return await create_product_summary_job(summary_upload, periodic_upload, prospectus_upload)

    if job.get("workflow_id") == "historical-performance-verification":
        history_roles = sorted(
            role for role in (job.get("document_names") or {})
            if role.startswith("historical_report_")
        )
        history_uploads = [stored_upload(role, role + ".pdf") for role in history_roles]
        # The first version of this screen accepted one report and exposed a
        # separate performance-only result.  A single-report rerun now uses
        # the normal prospectus/report workflow so its investment modules and
        # annual-performance module are rendered together.  Keep the original
        # multi-report workflow for tasks that genuinely contain a report set.
        if len(history_uploads) == 1:
            return await create_comparison_job(
                stored_upload("prospectus", "招募说明书.pdf"),
                history_uploads[0],
                force_refresh,
            )
        raise HTTPException(
            status_code=409,
            detail="该任务是旧版多报告业绩专项，只保留历史结果查看。请在普通核对入口重新上传一份招募说明书和对应定期报告。",
        )

    prospectus_upload = stored_upload("prospectus", "招募说明书.pdf")
    periodic_upload = stored_upload("periodic_report", "定期报告.pdf")
    extra_roles = sorted(
        role for role in (job.get("document_names") or {})
        if role.startswith("historical_report_")
    )
    extra_uploads = [
        item for item in (stored_upload(role, role + ".pdf", required=False) for role in extra_roles)
        if item is not None
    ]
    if extra_uploads:
        return await create_comparison_job(prospectus_upload, periodic_upload, force_refresh, extra_uploads)
    return await create_comparison_job(prospectus_upload, periodic_upload, force_refresh)


def _delete_job_record(job_id: str) -> None:
    with JOB_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        try:
            job = AUDIT_STORE.load_job(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if job is None:
        raise HTTPException(status_code=404, detail="核对任务不存在")
    if job.get("status") in {"queued", "running"}:
        raise HTTPException(status_code=409, detail="任务正在执行中，无法删除")
    with JOB_LOCK:
        JOBS.pop(job_id, None)
    AUDIT_STORE.delete_task(job_id)


@app.delete("/api/jobs/{job_id}")
async def delete_comparison_job(job_id: str) -> dict:
    _delete_job_record(job_id)
    return {"deleted": True, "job_id": job_id}


@app.post("/api/jobs/batch-delete")
async def batch_delete_comparison_jobs(payload: BatchJobDeletePayload) -> dict:
    deleted_ids: list[str] = []
    skipped: list[dict[str, str]] = []
    seen: set[str] = set()
    for job_id in payload.job_ids:
        if job_id in seen:
            continue
        seen.add(job_id)
        try:
            _delete_job_record(job_id)
            deleted_ids.append(job_id)
        except HTTPException as exc:
            skipped.append({"job_id": job_id, "reason": str(exc.detail)})
    return {"deleted_ids": deleted_ids, "skipped": skipped}


@app.get("/api/jobs")
async def list_comparison_jobs() -> dict:
    return {"jobs": [_job_view(job) for job in AUDIT_STORE.recent_jobs(100)]}


@app.get("/api/tasks")
async def query_comparison_tasks(
    q: str = Query("", max_length=200),
    workflow_id: Literal["prospectus-periodic-comparison", "product-summary-verification"] | None = None,
    status: Literal["queued", "running", "completed", "failed"] | None = None,
    result_status: str | None = Query(None, max_length=40),
    start_date: date | None = None,
    end_date: date | None = None,
    report_start: date | None = None,
    report_end: date | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
) -> dict:
    if start_date and end_date and start_date > end_date:
        raise HTTPException(status_code=422, detail="开始日期不能晚于结束日期")
    if report_start and report_end and report_start > report_end:
        raise HTTPException(status_code=422, detail="报告区间开始日期不能晚于结束日期")
    keyword = q.strip().casefold()
    matches = []
    for job in AUDIT_STORE.job_summaries():
        if workflow_id and (job.get("workflow_id") or "prospectus-periodic-comparison") != workflow_id:
            continue
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
        if report_start or report_end:
            report_dates = ((role, interval) for role, interval in (job.get("document_dates") or {}).items()
                            if role == "periodic_report" or role.startswith("historical_report_"))
            overlaps = False
            for _, interval in report_dates:
                try:
                    interval_start = date.fromisoformat(str(interval["start"]))
                    interval_end = date.fromisoformat(str(interval.get("end") or interval["start"]))
                except (ValueError, TypeError, KeyError):
                    continue
                if (not report_end or interval_start <= report_end) and (not report_start or interval_end >= report_start):
                    overlaps = True
                    break
            if not overlaps:
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
    if document_role not in {"prospectus", "periodic_report", "product_summary", "annual_report"} and not re.fullmatch(r"historical_report_\d{3}", document_role):
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
