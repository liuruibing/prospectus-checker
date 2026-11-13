from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from threading import RLock
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)


class TaskAuditStore:
    """Local, human-readable audit records for background comparison jobs."""

    SUMMARY_VERSION = 3

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self._lock = RLock()

    def task_dir(self, task_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", task_id):
            raise ValueError("TASK_ID_INVALID")
        return self.root / task_id

    def create_task(
        self,
        task_id: str,
        job: dict[str, Any],
        documents: dict[str, tuple[str, bytes]],
        runtime: dict[str, Any],
    ) -> None:
        job["owner_pid"] = os.getpid()
        directory = self.task_dir(task_id)
        with self._lock:
            directory.mkdir(parents=True, exist_ok=False, mode=0o700)
            directory.chmod(0o700)
            document_meta = {}
            for role, (filename, content) in documents.items():
                relative = f"inputs/{role}.pdf"
                target = directory / relative
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                target.parent.chmod(0o700)
                target.write_bytes(content)
                target.chmod(0o600)
                document_meta[role] = {
                    "filename": Path(filename).name,
                    "stored_as": relative,
                    "size_bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            metadata = {
                "task_id": task_id,
                "created_at": job.get("created_at") or _now(),
                "owner_id": None,
                "workflow_id": job.get("workflow_id", "prospectus-periodic-comparison"),
                "runtime": runtime,
                "documents": document_meta,
                "storage": "local filesystem",
            }
            _atomic_json(directory / "metadata.json", metadata)
            _atomic_json(directory / "job.json", job)
            _atomic_json(directory / "summary.json", self.summarize(job, metadata))
        self.record_event(task_id, "task_created", {"status": job.get("status"), "stage": job.get("stage")})

    def save_job(self, task_id: str, job: dict[str, Any]) -> None:
        with self._lock:
            _atomic_json(self.task_dir(task_id) / "job.json", job)
            _atomic_json(self.task_dir(task_id) / "summary.json", self.summarize(job))

    def summarize(self, job: dict[str, Any], metadata: dict | None = None) -> dict:
        fields = ("job_id", "run_id", "workflow_id", "historical", "status", "stage", "message", "created_at", "started_at", "updated_at", "completed_at", "steps", "error")
        summary = {key: job.get(key) for key in fields}
        summary["_summary_version"] = self.SUMMARY_VERSION
        result = job.get("result") or {}
        preflight = result.get("preflight") or {}
        documents = result.get("documents") or {}
        names = dict(job.get("document_names") or {})
        if not names:
            if metadata is None:
                try:
                    metadata = json.loads((self.task_dir(job["job_id"]) / "metadata.json").read_text(encoding="utf-8"))
                except (OSError, ValueError, KeyError):
                    metadata = {}
            names = {role: value.get("filename") for role, value in (metadata.get("documents") or {}).items()}
        for role, document in documents.items():
            if document.get("filename"):
                names[role] = document["filename"]
        document_dates = {}
        for role in ("prospectus", "periodic_report", "product_summary"):
            preflight_info = preflight.get(role) or {}
            document_info = (documents.get(role) or {}).get("document_info") or {}
            interval = (
                preflight_info.get("display_date_interval")
                or document_info.get("display_date_interval")
                or preflight_info.get("date_interval")
                or document_info.get("date_interval")
            )
            if not isinstance(interval, dict) and role == "periodic_report":
                period_end = document_info.get("period_end")
                if period_end:
                    interval = {"start": period_end, "end": period_end}
            if isinstance(interval, dict) and interval.get("start"):
                document_dates[role] = {
                    "label": (
                        preflight_info.get("display_date_label")
                        or document_info.get("display_date_label")
                        or preflight_info.get("date_label")
                        or document_info.get("date_label")
                    ),
                    "start": interval.get("start"),
                    "end": interval.get("end") or interval.get("start"),
                }
        identity_role = "product_summary" if job.get("workflow_id") == "product-summary-verification" else "prospectus"
        info = (documents.get(identity_role) or {}).get("document_info") or preflight.get(identity_role) or preflight.get("prospectus") or {}
        unresolved_count = result.get("unresolved_count")
        if unresolved_count is None and isinstance(result.get("modules"), list):
            unresolved_count = 0
            for module in result["modules"]:
                diffs = module.get("differences") or []
                unresolved_diffs = sum(
                    item.get("status") in {"UNRESOLVED", "UNRESOLVED_ROW_IDENTITY", "UNRESOLVED_STRUCTURE", "NOT_CHECKED"}
                    for item in diffs
                )
                if unresolved_diffs:
                    unresolved_count += unresolved_diffs
                elif module.get("status") not in {None, "MATCH", "MISMATCH", "MISSING_ROW", "MISSING_FIELD", "MISSING_MODULE", "RANK_ORDER_CHANGED"}:
                    unresolved_count += 1
        summary.update(document_names=names, document_dates=document_dates, fund_name=info.get("fund_name"), fund_code=info.get("fund_code"),
                       result_status=result.get("status"), difference_count=result.get("difference_count"),
                       unresolved_count=unresolved_count, not_checked_count=result.get("not_checked_count"),
                       preflight_blocked=bool(preflight.get("blocked")), preflight_message=preflight.get("message"))
        return summary

    def job_summaries(self) -> list[dict[str, Any]]:
        summaries = []
        for job_path in self.root.glob("*/job.json"):
            summary_path = job_path.with_name("summary.json")
            try:
                if summary_path.exists() and summary_path.stat().st_mtime_ns >= job_path.stat().st_mtime_ns:
                    summary = json.loads(summary_path.read_text(encoding="utf-8"))
                else:
                    summary = {}
                if summary.get("_summary_version") != self.SUMMARY_VERSION:
                    summary = self.summarize(json.loads(job_path.read_text(encoding="utf-8")))
                    with self._lock:
                        _atomic_json(summary_path, summary)
                if summary.get("job_id"):
                    summary.pop("_summary_version", None)
                    summaries.append(summary)
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        return sorted(summaries, key=lambda job: (str(job.get("created_at") or ""), job["job_id"]), reverse=True)

    def record_event(self, task_id: str, event: str, details: dict[str, Any] | None = None) -> None:
        record = {"timestamp": _now(), "event": event, "details": details or {}}
        directory = self.task_dir(task_id)
        with self._lock:
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(0o700)
            events_path = directory / "events.jsonl"
            with events_path.open("a", encoding="utf-8") as output:
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
            events_path.chmod(0o600)

    def write_artifact(self, task_id: str, relative_path: str, value: Any) -> str:
        relative = PurePosixPath(relative_path)
        if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError("TASK_ARTIFACT_PATH_INVALID")
        directory = self.task_dir(task_id)
        target = (directory / Path(*relative.parts)).resolve()
        if directory not in target.parents:
            raise ValueError("TASK_ARTIFACT_PATH_INVALID")
        with self._lock:
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            target.parent.chmod(0o700)
            if isinstance(value, bytes):
                temporary = target.with_suffix(target.suffix + ".tmp")
                temporary.write_bytes(value)
                temporary.chmod(0o600)
                os.replace(temporary, target)
            elif isinstance(value, str):
                temporary = target.with_suffix(target.suffix + ".tmp")
                temporary.write_text(value, encoding="utf-8")
                temporary.chmod(0o600)
                os.replace(temporary, target)
            else:
                _atomic_json(target, value)
        return relative.as_posix()

    def load_job(self, task_id: str) -> dict[str, Any] | None:
        path = self.task_dir(task_id) / "job.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None

    def delete_task(self, task_id: str) -> bool:
        directory = self.task_dir(task_id)
        with self._lock:
            if not directory.exists():
                return False
            shutil.rmtree(directory)
            return True

    def recent_jobs(self, limit: int) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        jobs = []
        for path in self.root.glob("*/job.json"):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            jobs.append(job)
        jobs.sort(key=lambda job: str(job.get("created_at") or ""), reverse=True)
        return jobs[:limit]

    def task_trace(self, task_id: str) -> dict[str, Any] | None:
        directory = self.task_dir(task_id)
        metadata_path = directory / "metadata.json"
        if not metadata_path.is_file():
            return None
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        events = []
        events_path = directory / "events.jsonl"
        if events_path.is_file():
            for line in events_path.read_text(encoding="utf-8").splitlines():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        artifacts = []
        for path in sorted(directory.rglob("*")):
            if not path.is_file() or path.name.endswith(".tmp") or path.name == "job.json":
                continue
            artifacts.append({
                "path": path.relative_to(directory).as_posix(),
                "size_bytes": path.stat().st_size,
            })
        try:
            job = json.loads((directory / "job.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            job = None
        return {"task": metadata, "job": job, "events": events, "artifacts": artifacts}

    def artifact_path(self, task_id: str, relative_path: str) -> Path | None:
        directory = self.task_dir(task_id)
        relative = PurePosixPath(relative_path)
        if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError("TASK_ARTIFACT_PATH_INVALID")
        path = (directory / Path(*relative.parts)).resolve()
        if directory not in path.parents or not path.is_file():
            return None
        return path
