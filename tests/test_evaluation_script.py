import asyncio
import subprocess
from pathlib import Path

import httpx

from scripts.evaluate_cases import _source_file_manifest, check_health, git_fingerprint, score_summary_result


def test_evaluation_health_supports_api_prefixed_route():
    def handler(request):
        if request.url.path == "/health":
            return httpx.Response(404)
        if request.url.path == "/api/health":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert asyncio.run(check_health(client, "http://local")) == "/api/health"
    finally:
        asyncio.run(client.aclose())


def test_source_manifest_includes_untracked_code_and_rules(tmp_path: Path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "tracked.py").write_text("VALUE = 1\n")
    subprocess.run(["git", "add", "app/tracked.py"], cwd=tmp_path, check=True)
    (tmp_path / "app" / "new_module.py").write_text("VALUE = 2\n")
    (tmp_path / "rules").mkdir()
    (tmp_path / "rules" / "business.md").write_text("Rule version one\n")

    first = git_fingerprint(tmp_path)
    files = {item["path"] for item in first["source_files"]}
    assert "app/new_module.py" in files
    assert "rules/business.md" in files
    assert first["source_file_count"] == len(_source_file_manifest(tmp_path))

    (tmp_path / "app" / "new_module.py").write_text("VALUE = 3\n")
    second = git_fingerprint(tmp_path)
    assert second["source_tree_sha256"] != first["source_tree_sha256"]


def test_source_manifest_excludes_pdf_corpus_and_task_artifacts(tmp_path: Path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    for relative in ("app/run.py", "assets/input.pdf", "var/tasks/task/result.json", ".env"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
    manifest = _source_file_manifest(tmp_path)
    assert [item["path"] for item in manifest] == ["app/run.py"]


def test_summary_scorer_only_scores_confirmed_items_and_flags_unconfirmed_overclaims():
    expected = {"items": [
        {"item_id": "same", "status": "CONFIRMED", "expected_comparison_status": "MATCH"},
        {"item_id": "different", "status": "CONFIRMED", "expected_comparison_status": "MISMATCH"},
        {"item_id": "uncertain", "status": "EXPECTED_UNCONFIRMED", "expected_comparison_status": "UNRESOLVED"},
        {"item_id": "n_a", "status": "NOT_APPLICABLE"},
    ]}
    actual = {"items": [
        {"item_id": "same", "status": "MATCH", "evidence_status": "VERIFIED"},
        {"item_id": "different", "status": "UNRESOLVED", "evidence_status": "MISSING"},
        {"item_id": "uncertain", "status": "MISMATCH", "evidence_status": "VERIFIED"},
        {"item_id": "n_a", "status": "NOT_CHECKED"},
    ]}

    score = score_summary_result(expected, actual)

    assert score["confirmed_expected"] == 2
    assert score["confirmed_correct"] == 1
    assert score["confirmed_incorrect"] == 1
    assert score["unconfirmed_overclaims"] == 1
    assert score["not_applicable_correct"] == 1
