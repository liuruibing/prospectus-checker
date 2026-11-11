"""Archive verified PDF conversions and bind logical units to existing evidence IDs."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

import pymupdf

from app.pdf_markdown import convert_pdf_bytes, render_markdown, write_conversion


def verified_conversion(content: bytes, root: Path | None = None) -> dict:
    root = root or Path(__file__).resolve().parents[1] / "var" / "pdf_markdown"
    root.mkdir(parents=True, exist_ok=True)
    implementation = b"".join(Path(__file__).with_name(n).read_bytes() for n in ("pdf_document.py", "pdf_markdown.py", "pdf_graphics.py", "headings.py"))
    key = hashlib.sha256(content + hashlib.sha256(implementation).digest()).hexdigest()
    target = root / key
    cached = _load_archive(target)
    if cached is not None:
        return {**cached, "archive_path": str(target), "cache_hit": True}
    if target.exists():
        target.rename(root / ("invalid-"+key+"-"+uuid.uuid4().hex))
    package = convert_pdf_bytes(content)
    with tempfile.TemporaryDirectory(prefix="conversion-", dir=root) as folder:
        stage = Path(folder)
        write_conversion(package, stage)
        hashes = {p.relative_to(stage).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in stage.rglob("*") if p.is_file()}
        (stage / "integrity.json").write_text(json.dumps(hashes), encoding="utf-8")
        try:
            os.rename(stage, target)
        except OSError:
            # A concurrent writer may have published the same immutable conversion.
            if _load_archive(target) is None:
                raise
    return {**package, "archive_path": str(target), "cache_hit": False}


def _load_archive(target: Path) -> dict | None:
    try:
        hashes = json.loads((target/"integrity.json").read_text())
        required = {"manifest.json", "document.md", "reader.md", "source-map.json", "verification.json"}
        if not required.issubset(hashes):
            return None
        if not all(hashlib.sha256((target/name).read_bytes()).hexdigest() == digest for name, digest in hashes.items()):
            return None
        manifest = json.loads((target/"manifest.json").read_text())
        verification = json.loads((target/"verification.json").read_text())
        if not verification["ok"]:
            return None
        return {**manifest, "verification": verification,
                "markdown": (target/"document.md").read_text(), "reader_markdown": (target/"reader.md").read_text(),
                "source_map": json.loads((target/"source-map.json").read_text()),
                "assets": {name: (target/name).read_bytes() for name in hashes if name.startswith("assets/")}}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def require_ready(package: dict) -> None:
    if package["status"] != "READY" or not package["verification"]["ok"]:
        raise ValueError("PDF_MD_REQUIRES_REVIEW: " + json.dumps(package.get("warnings", []), ensure_ascii=False))


def bind_catalogue(catalogue: dict, package: dict) -> dict:
    """Attach complete logical units without creating alternate model evidence identities."""
    require_ready(package)
    doc = package["source_map"]
    page_sources = {p["page"]: p["sources"] for p in catalogue["pages"]}
    dimensions = {p["page"]: p for p in doc["pages"]}
    units = []
    covered = set()
    for index, block in enumerate(doc["blocks"]):
        fragments = block.get("fragments") or [{"page": p, "bbox": block["bbox"]} for p in block["pages"]]
        source_ids = set()
        for f in fragments:
            rect = pymupdf.Rect(f["bbox"])
            for source in page_sources.get(f["page"], []):
                box = pymupdf.Rect(source["bbox"])
                test_rect = rect * pymupdf.Matrix(*dimensions[f["page"]]["rotation_matrix"])
                if block["kind"] == "image":
                    continue
                if (test_rect & box).get_area() >= box.get_area()*.7:
                    source_ids.add(source["source_id"])
        covered.update(source_ids)
        md = render_markdown({**doc, "blocks": [block], "marginalia": []})
        units.append({"unit_id": f"unit-{index}", "kind": block["kind"], "pages": block["pages"],
                      "source_ids": sorted(source_ids), "markdown": re.sub(r'</?span\b[^>]*>', '', md)})
    # Evidence not covered by a logical unit remains addressable; it is never silently discarded.
    return {"provider": package["version"], "status": package["status"], "markdown": package["reader_markdown"],
            "verification": package["verification"], "archive_path": package["archive_path"],
            "cache_hit": package["cache_hit"], "logical_units": units,
            "unbound_source_ids": [s["source_id"] for p in catalogue["pages"] for s in p["sources"] if s["source_id"] not in covered]}


def page_tables(package: dict) -> dict:
    """Expose per-page native cells in the existing table-cache shape."""
    doc = package["source_map"]
    segments = doc["original_segments"]
    dimensions = {p["page"]: p for p in doc["pages"]}
    result = {}
    for page in doc["page_fragments"]:
        records = []
        for table in page["tables"]:
            count = len(table["xs"])-1
            matrix, boxes = [], []
            table_boxes = []
            for row in table["rows"]:
                values, cells = [None]*count, [None]*count
                for c in row:
                    values[c["col"]] = "".join(segments[s]["text"] for s in c["segments"])
                    cells[c["col"]] = c["bbox"]
                    if c.get("bbox"):
                        table_boxes.append(c["bbox"])
                matrix.append(values)
                boxes.append(cells)
            unit_context = _nearby_table_context(page["page"], table_boxes, segments)
            records.append({"matrix": matrix, "cells_by_row": boxes, "columns": count,
                            "page_width": dimensions[page["page"]]["width"],
                            "unit_context": unit_context})
        result[page["page"]] = records
    return result


def _nearby_table_context(page_number: int, cell_boxes: list[list[float]], segments: dict) -> str:
    """Collect text immediately above a table using source geometry, not PDF text-block bounds."""
    boxes = [box for box in cell_boxes if isinstance(box, (list, tuple)) and len(box) == 4]
    if not boxes:
        return ""
    table_left = min(float(box[0]) for box in boxes)
    table_top = min(float(box[1]) for box in boxes)
    table_right = max(float(box[2]) for box in boxes)
    nearby = []
    for segment in segments.values():
        if not isinstance(segment, dict) or segment.get("page") != page_number:
            continue
        bbox = segment.get("bbox")
        text = str(segment.get("text") or "").strip()
        if not text or not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            continue
        left, top, right, bottom = (float(value) for value in bbox)
        vertical_gap = table_top - bottom
        horizontal_overlap = min(table_right, right) - max(table_left, left)
        if 0 <= vertical_gap <= 48 and horizontal_overlap > 0:
            nearby.append((vertical_gap, top, text))
    return " ".join(text for _, _, text in sorted(nearby))
