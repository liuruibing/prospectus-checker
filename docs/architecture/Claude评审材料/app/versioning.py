"""Build identifiers for comparing an evaluator worktree with a running API."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


RUNTIME_ROOTS = ("app", "rules", "prompts", "static", "frontend/src")
RUNTIME_FILES = ("pyproject.toml", "requirements.txt", "frontend/package.json", "frontend/package-lock.json")
IGNORED_PARTS = {".git", ".venv", "venv", "node_modules", "__pycache__"}


def runtime_source_manifest(root: Path) -> list[dict[str, str]]:
    files: set[Path] = set()
    for relative_root in RUNTIME_ROOTS:
        candidate = root / relative_root
        if candidate.is_dir():
            files.update(path for path in candidate.rglob("*") if path.is_file())
    files.update(root / relative for relative in RUNTIME_FILES if (root / relative).is_file())
    manifest = []
    for path in files:
        relative = path.relative_to(root)
        if any(part in IGNORED_PARTS for part in relative.parts) or relative.name.startswith(".env"):
            continue
        content = path.read_bytes()
        manifest.append({"path": relative.as_posix(), "sha256": hashlib.sha256(content).hexdigest()})
    return sorted(manifest, key=lambda item: item["path"])


def runtime_source_fingerprint(root: Path) -> str:
    manifest = runtime_source_manifest(root)
    serialized = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()
