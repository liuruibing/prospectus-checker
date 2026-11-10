from __future__ import annotations

import hashlib
import json
from typing import Any


def stable_source_id(
    document_sha256: str,
    document_role: str,
    page: int,
    source_kind: str,
    locator: str,
) -> str:
    """Create a task-independent source address for a physical PDF fragment."""
    identity = json.dumps(
        [document_sha256, document_role, int(page), source_kind, locator],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def evidence_source(
    *,
    document_sha256: str,
    document_role: str,
    page: int,
    source_kind: str,
    locator: str,
    raw_text: str,
    bbox: list[float] | None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the shared, JSON-safe base record used by text and table evidence."""
    record = {
        "source_id": stable_source_id(document_sha256, document_role, page, source_kind, locator),
        "document_role": document_role,
        "source_kind": source_kind,
        "locator": locator,
        "page": int(page),
        "bbox": [round(float(value), 2) for value in bbox] if bbox is not None else None,
        "raw_text": str(raw_text),
    }
    if extra:
        record.update(extra)
    return record
