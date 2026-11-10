from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.versioning import runtime_source_manifest

CASE_MANIFEST = ROOT / "evaluation" / "cases" / "manifest.json"


def load_cases(case_ids: list[str]) -> list[dict[str, Any]]:
    manifest = json.loads(CASE_MANIFEST.read_text(encoding="utf-8"))
    by_id = {case["case_id"]: case for case in manifest["cases"]}
    selected = [by_id[case_id] for case_id in case_ids] if case_ids else manifest["cases"]
    for case in selected:
        for document in case["documents"]:
            path = ROOT / document["path"]
            if not path.is_file():
                raise FileNotFoundError(f"{case['case_id']}: input missing: {document['path']}")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != document["sha256"]:
                raise ValueError(f"{case['case_id']}: input hash changed: {document['path']}")
    return selected


def _source_file_manifest(root: Path = ROOT) -> list[dict[str, str]]:
    """Hash loaded source/rule files from both the index and the worktree.

    Git's diff omits untracked files, while this project often runs new workflow
    modules before they are staged. Keep the evaluation fingerprint tied to the
    files that can affect a run, without hashing PDFs or task artifacts.
    """
    def run(*args: str) -> str | None:
        try:
            return subprocess.check_output(args, cwd=root, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.CalledProcessError):
            return None

    raw_paths = run("git", "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    if raw_paths is None:
        raise RuntimeError(f"Cannot enumerate worktree files for version fingerprint: {root}")
    excluded = {".git", ".venv", "venv", "node_modules", "assets", "var", "tmp", "outputs", "work", "__pycache__"}
    manifest: list[dict[str, str]] = []
    for raw_path in raw_paths.split(b"\0"):
        if not raw_path:
            continue
        relative = Path(os.fsdecode(raw_path))
        if any(part in excluded for part in relative.parts) or relative.name.startswith(".env"):
            continue
        path = root / relative
        if path.is_symlink():
            content = os.fsencode(os.readlink(path))
        elif path.is_file():
            content = path.read_bytes()
        else:
            manifest.append({"path": relative.as_posix(), "sha256": "MISSING"})
            continue
        manifest.append({"path": relative.as_posix(), "sha256": hashlib.sha256(content).hexdigest()})
    manifest.sort(key=lambda item: item["path"])
    return manifest


def git_fingerprint(root: Path = ROOT) -> dict[str, Any]:
    def run(*args: str) -> str | None:
        try:
            return subprocess.check_output(args, cwd=root, text=True, stderr=subprocess.DEVNULL).strip() or None
        except (OSError, subprocess.CalledProcessError):
            return None

    diff = run("git", "diff", "--binary", "HEAD") or ""
    source_manifest = _source_file_manifest(root)
    canonical_manifest = json.dumps(source_manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    runtime_manifest = runtime_source_manifest(root)
    runtime_serialized = json.dumps(runtime_manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "branch": run("git", "branch", "--show-current"),
        "head": run("git", "rev-parse", "HEAD"),
        "tracked_diff_sha256": hashlib.sha256(diff.encode()).hexdigest(),
        "source_tree_sha256": hashlib.sha256(canonical_manifest.encode()).hexdigest(),
        "source_file_count": len(source_manifest),
        "source_files": source_manifest,
        "runtime_source_sha256": hashlib.sha256(runtime_serialized.encode()).hexdigest(),
        "status": run("git", "status", "--short"),
    }


def task_status(job: dict[str, Any]) -> str:
    return str(job.get("status") or job.get("job", {}).get("status") or "unknown")


def count_results(result: dict[str, Any] | None) -> dict[str, int]:
    result = result or {}
    items = result.get("items") or result.get("modules") or []
    counts: dict[str, int] = {}
    for item in items:
        status = str(item.get("status") or item.get("comparison_status") or "UNKNOWN")
        counts[status] = counts.get(status, 0) + 1
    return counts


async def check_health(client: httpx.AsyncClient, base_url: str) -> str:
    """Return the working health route; support both current API prefixes."""
    for path in ("/health", "/api/health"):
        response = await client.get(f"{base_url}{path}", timeout=10)
        if response.is_success:
            response.raise_for_status()
            return path
        if response.status_code != 404:
            response.raise_for_status()
    response.raise_for_status()
    raise RuntimeError("No health route found")


async def submit_case(
    client: httpx.AsyncClient,
    base_url: str,
    case: dict[str, Any],
    run_number: int,
    repeat_index: int,
    output_dir: Path,
    poll_seconds: float,
    timeout_seconds: float,
    force_refresh: bool,
    version_fingerprint: dict[str, Any],
) -> dict[str, Any]:
    files: dict[str, tuple[str, bytes, str]] = {}
    role_map = {
        "target": "prospectus" if case["task_type"] == "prospectus_compare" else "product_summary",
        "primary_reference": "periodic_report",
        "fallback_reference": "prospectus",
    }
    for document in case["documents"]:
        path = ROOT / document["path"]
        files[role_map[document["role"]]] = (path.name, path.read_bytes(), "application/pdf")

    if case["task_type"] == "prospectus_compare":
        files_payload = {key: files[key] for key in ("prospectus", "periodic_report")}
        response = await client.post(
            f"{base_url}/api/jobs",
            files=files_payload,
            data={"force_refresh": "true" if force_refresh else "false"},
            timeout=timeout_seconds,
        )
    else:
        response = await client.post(
            f"{base_url}/api/product-summary/jobs",
            files=files,
            timeout=timeout_seconds,
        )

    response.raise_for_status()
    created = response.json()
    task_id = str(created.get("job_id") or created.get("run_id") or "")
    if not task_id:
        raise RuntimeError(f"Task API response did not include a task ID: {created}")

    started = time.monotonic()
    latest = created
    while task_status(latest) in {"queued", "running"}:
        if time.monotonic() - started > timeout_seconds:
            break
        await asyncio.sleep(poll_seconds)
        response = await client.get(f"{base_url}/api/jobs/{task_id}", timeout=30)
        response.raise_for_status()
        latest = response.json()

    duration = round((time.monotonic() - started) * 1000)
    status = task_status(latest)
    result = latest.get("result") or {}
    record = {
        "case_id": case["case_id"], "product": case["product"],
        "workflow": case["workflow"], "run_number": run_number,
        "repeat_index": repeat_index, "task_id": task_id,
        "status": status, "result_status": result.get("status"),
        "result_counts": count_results(result), "duration_ms": duration,
        "timed_out": status in {"queued", "running"},
        "error": latest.get("error"), "created_at": latest.get("created_at"),
        "document_names": latest.get("document_names"),
        "expected_path": case["expected_path"],
        "ground_truth_status": case["ground_truth_status"],
        "input_documents": [
            {"role": document["role"], "path": document["path"], "sha256": document["sha256"]}
            for document in case["documents"]
        ],
        "version_fingerprint": version_fingerprint,
    }
    (output_dir / f"{task_id}.json").write_text(
        json.dumps(latest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    trace_response = await client.get(f"{base_url}/api/jobs/{task_id}/trace", timeout=60)
    if trace_response.is_success:
        (output_dir / f"{task_id}.trace.json").write_text(
            json.dumps(trace_response.json(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return record


async def run(args: argparse.Namespace) -> int:
    cases = load_cases(args.case)
    if args.validate_only:
        print(json.dumps({"case_count": len(cases), "cases": [case["case_id"] for case in cases],
                          "inputs_hash_verified": True}, ensure_ascii=False))
        return 0
    batch_id = args.batch_id or datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    output_dir = Path(args.output_dir) if args.output_dir else ROOT / "var" / "evaluations" / batch_id
    output_dir.mkdir(parents=True, exist_ok=False)
    manifest = {
        "batch_id": batch_id, "started_at": datetime.now(timezone.utc).isoformat(),
        "base_url": args.base_url, "repeat_count": args.repeat,
        "force_refresh": args.force_refresh, "git": git_fingerprint(),
        "cases": [case["case_id"] for case in cases],
        "ground_truth_ready": all(case["ground_truth_status"] == "READY" for case in cases),
        "warning": "预期文件未完成人工原文核验；本批次只记录任务结果，不计算准确率。"
        if any(case["ground_truth_status"] != "READY" for case in cases) else None,
    }
    results_path = output_dir / "runs.jsonl"
    transport = httpx.AsyncHTTPTransport(retries=0)
    manifest_path = output_dir / "batch_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    async with httpx.AsyncClient(transport=transport) as client:
        try:
            manifest["health_path"] = await check_health(client, args.base_url)
            health_response = await client.get(f"{args.base_url}{manifest['health_path']}", timeout=10)
            health_response.raise_for_status()
            runtime = health_response.json()
            manifest["runtime"] = runtime
            expected_runtime_fingerprint = manifest["git"]["runtime_source_sha256"]
            actual_runtime_fingerprint = runtime.get("source_fingerprint") if isinstance(runtime, dict) else None
            manifest["runtime_version_matches_worktree"] = actual_runtime_fingerprint == expected_runtime_fingerprint
            if not manifest["runtime_version_matches_worktree"] and not args.allow_runtime_mismatch:
                manifest["health_error"] = "RUNTIME_VERSION_MISMATCH: running API fingerprint differs from current runtime files"
                manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                print(json.dumps({"batch_id": batch_id, "health_error": manifest["health_error"],
                                  "expected": expected_runtime_fingerprint, "actual": actual_runtime_fingerprint}, ensure_ascii=False), flush=True)
                return 3
        except Exception as exc:
            manifest["health_error"] = f"{type(exc).__name__}: {exc}"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"batch_id": batch_id, "health_error": manifest["health_error"]}, ensure_ascii=False), flush=True)
            return 2
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        total = len(cases) * args.repeat
        run_number = 0
        for repeat_index in range(1, args.repeat + 1):
            for case in cases:
                run_number += 1
                print(f"[{run_number}/{total}] {case['case_id']} run {repeat_index}", flush=True)
                try:
                    record = await submit_case(
                        client, args.base_url, case, run_number, repeat_index,
                        output_dir, args.poll_seconds, args.timeout_seconds,
                        args.force_refresh, manifest["git"],
                    )
                except Exception as exc:
                    record = {
                        "case_id": case["case_id"], "product": case["product"],
                        "workflow": case["workflow"], "run_number": run_number,
                        "repeat_index": repeat_index, "status": "request_failed",
                        "error": f"{type(exc).__name__}: {exc}",
                        "ground_truth_status": case["ground_truth_status"],
                    }
                with results_path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                    stream.flush()
                print(json.dumps(record, ensure_ascii=False), flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="按固定产品案例创建并追踪真实核对任务。")
    parser.add_argument("--case", action="append", default=[], help="case_id，可重复指定；默认全部案例")
    parser.add_argument("--repeat", type=int, default=1, help="每个案例运行轮数，按轮交错")
    parser.add_argument("--base-url", default=os.getenv("PROSPECTUS_EVAL_BASE_URL", "http://127.0.0.1:8331"))
    parser.add_argument("--batch-id", default=None)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--timeout-seconds", type=float, default=900.0)
    parser.add_argument("--force-refresh", action="store_true", help="投资组合核对绕过章节缓存")
    parser.add_argument("--allow-runtime-mismatch", action="store_true", help="仅供开发诊断；允许请求与当前工作树指纹不一致的旧服务")
    parser.add_argument("--validate-only", action="store_true", help="只检查案例配对和输入哈希，不创建任务")
    args = parser.parse_args()
    if args.repeat < 1 or args.poll_seconds <= 0 or args.timeout_seconds <= 0:
        parser.error("repeat/poll-seconds/timeout-seconds 必须大于 0")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
