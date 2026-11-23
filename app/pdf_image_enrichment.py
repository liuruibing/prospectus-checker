"""MinerU-style VLM figure/chart enrichment for verified PDF Markdown conversions."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Callable

from app.ai import make_provider

ENRICHMENT_VERSION = "pdf-image-enrichment-v1"

IMAGE_OCR_SYSTEM_PROMPT = """你是专业的金融法律文档与基金信息披露图表结构化识别助手。
你的任务是将 PDF 页面中裁切出的图表或图片，结合其前后文标题，准确转换为结构化的 Markdown 内容。

规则：
1. 若图片仅为装饰性图案、公司 Logo、页眉线条、印章或空白无有效信息，请仅输出：<!-- decorative-image -->
2. 若图片包含饼图、柱状图、折线图或图文混排数据：
   - 保留图内标题、副标题、单位（如 %、元、万元）以及数据截止日期（例如：`数据截止日期：2025-12-31`）。
   - 将图例、饼图分类占比、柱状图/折线图数据整理为标准的 Markdown 表格。
   - **极其重要**：若柱状图/折线图下方或内部附带了嵌套数据表，请**严格按数据表中的精确数字、正负号和百分号**输出，绝不要凭柱子高度目测估算。
   - 若为饼图且各分项为占比，请核对数字清晰度与百分号；不要编造图中不存在的文字。
3. 若图片本身是扫描表格或截图文字，请完整还原为 Markdown 表格或段落，忠实保留原文与数字。
4. 只输出最终的 Markdown 内容（或 `<!-- decorative-image -->`），不要添加解释性前后缀或 ```markdown 代码块包裹。"""


def _ocr_enabled() -> bool:
    raw = os.getenv("PROSPECTUS_PDF_IMAGE_OCR", "true").strip().casefold()
    return raw not in {"0", "false", "no", "off"}


def _block_plain_text(block: dict[str, Any], segments: dict[str, Any]) -> str:
    if block.get("kind") == "table":
        rows = []
        for row in block.get("rows") or []:
            cells = ["".join(segments[s]["text"] for s in cell.get("segments", []) if s in segments).strip() for cell in row]
            if any(cells):
                rows.append(" | ".join(cells))
        return "\n".join(rows)
    return "".join(segments[s]["text"] for s in block.get("segments", []) if s in segments).strip()


def _surrounding_context(blocks: list[dict[str, Any]], segments: dict[str, Any], index: int) -> dict[str, str]:
    before: list[str] = []
    for cursor in range(index - 1, -1, -1):
        candidate = blocks[cursor]
        if candidate.get("kind") == "image":
            continue
        text = _block_plain_text(candidate, segments)
        if text:
            before.append(text[:300])
        if len(before) >= 2:
            break
    before.reverse()

    after: list[str] = []
    for cursor in range(index + 1, len(blocks)):
        candidate = blocks[cursor]
        if candidate.get("kind") == "image":
            continue
        text = _block_plain_text(candidate, segments)
        if text:
            after.append(text[:300])
            break
    return {"before": "\n".join(before), "after": "\n".join(after)}


def _normalize_ocr_markdown(text: str) -> str:
    cleaned = text.strip()
    fence = re.match(r"^```(?:markdown|md)?\s*\n(.*?)\n```\s*$", cleaned, re.DOTALL | re.IGNORECASE)
    if fence:
        cleaned = fence.group(1).strip()
    if not cleaned or "<!-- decorative-image -->" in cleaned:
        return ""
    return cleaned


def _backfill_image_markdown(markdown: str, extractions_by_asset: dict[str, str]) -> str:
    if not markdown or not extractions_by_asset:
        return markdown

    def _replace(match: re.Match[str]) -> str:
        full = match.group(0)
        asset = match.group(1)
        extracted = extractions_by_asset.get(asset, "").strip()
        if not extracted:
            return full
        return (
            f"{full}\n\n"
            f"<!-- image-ocr-start: {asset} -->\n"
            f"{extracted}\n"
            f"<!-- image-ocr-end: {asset} -->"
        )

    return re.sub(r"!\[[^\]]*\]\((assets/[^)]+)\)", _replace, markdown)


def _cache_dir_for_package(package: dict[str, Any], cache_root: Path | None) -> Path:
    if cache_root is not None:
        return cache_root
    archive_path = package.get("archive_path")
    if archive_path:
        return Path(archive_path) / "image_ocr"
    return Path(__file__).resolve().parents[1] / "var" / "cache" / "image_ocr"


def _cache_key(image_bytes: bytes, context: dict[str, str], namespace: dict[str, Any]) -> str:
    payload = json.dumps({
        "version": ENRICHMENT_VERSION,
        "image_sha256": hashlib.sha256(image_bytes).hexdigest(),
        "context": context,
        "namespace": namespace,
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def _recognize_single_image(
    *,
    asset: str,
    page: int,
    bbox: list[float] | None,
    image_bytes: bytes,
    context: dict[str, str],
    provider: Any,
    cache_dir: Path,
) -> dict[str, Any]:
    namespace = getattr(provider, "cache_namespace", {"provider": type(provider).__name__})
    key = _cache_key(image_bytes, context, namespace)
    cache_file = cache_dir / f"{key}.json"
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and "markdown" in cached:
                return {**cached, "asset": asset, "page": page, "bbox": bbox, "cached": True}
        except (OSError, ValueError):
            pass

    prompt_parts = [f"物理页码：第 {page} 页", f"图片资源：{asset}"]
    if context.get("before"):
        prompt_parts.append(f"图片上方相邻文本/标题：\n{context['before']}")
    if context.get("after"):
        prompt_parts.append(f"图片下方相邻文本：\n{context['after']}")
    prompt_parts.append("请识别该图片中的图表、图例、数据截止日期、数据表或文字，并转换为准确的 Markdown。")
    user_prompt = "\n\n".join(prompt_parts)

    try:
        raw = await provider.generate_multimodal_text(IMAGE_OCR_SYSTEM_PROMPT, user_prompt, [image_bytes])
        extracted = _normalize_ocr_markdown(str(raw))
        record = {
            "asset": asset,
            "page": page,
            "bbox": bbox,
            "status": "DECORATIVE" if not extracted else "EXTRACTED",
            "markdown": extracted,
            "cached": False,
        }
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({
            "version": ENRICHMENT_VERSION,
            "status": record["status"],
            "markdown": extracted,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        return record
    except Exception as exc:
        return {
            "asset": asset,
            "page": page,
            "bbox": bbox,
            "status": "FAILED",
            "markdown": "",
            "error": str(exc),
            "cached": False,
        }


async def enrich_package_images(
    package: dict[str, Any],
    *,
    provider: Any | None = None,
    cache_root: Path | None = None,
    trace_callback: Callable[[dict[str, Any]], None] | None = None,
    role: str | None = None,
) -> dict[str, Any]:
    """Recognize chart/figure assets via VLM and backfill structured Markdown."""
    raw_reader = str(package.get("reader_markdown") or "")
    raw_markdown = str(package.get("markdown") or "")
    assets = package.get("assets") or {}
    source_map = package.get("source_map") or {}
    blocks = source_map.get("blocks") or []
    segments = source_map.get("original_segments") or {}

    image_jobs: list[tuple[dict[str, Any], dict[str, str], bytes]] = []
    for index, block in enumerate(blocks):
        if block.get("kind") != "image":
            continue
        asset = str(block.get("asset") or "")
        image_bytes = assets.get(asset)
        if not asset or not image_bytes:
            continue
        image_jobs.append((block, _surrounding_context(blocks, segments, index), image_bytes))

    if not image_jobs or not _ocr_enabled():
        return {
            **package,
            "raw_reader_markdown": raw_reader,
            "enriched_markdown": raw_markdown,
            "image_extractions": [],
            "image_ocr_status": "SKIPPED" if image_jobs else "NO_IMAGES",
        }

    active_provider = provider or make_provider()
    try:
        active_provider.require_configured()
    except Exception as exc:
        return {
            **package,
            "raw_reader_markdown": raw_reader,
            "enriched_markdown": raw_markdown,
            "image_extractions": [],
            "image_ocr_status": f"SKIPPED: {exc}",
        }

    if not hasattr(active_provider, "generate_multimodal_text"):
        return {
            **package,
            "raw_reader_markdown": raw_reader,
            "enriched_markdown": raw_markdown,
            "image_extractions": [],
            "image_ocr_status": "SKIPPED: UNSUPPORTED_PROVIDER",
        }

    cache_dir = _cache_dir_for_package(package, cache_root)
    extractions = await asyncio.gather(*(
        _recognize_single_image(
            asset=str(block["asset"]),
            page=int(block.get("page") or 0),
            bbox=block.get("bbox"),
            image_bytes=image_bytes,
            context=context,
            provider=active_provider,
            cache_dir=cache_dir,
        )
        for block, context, image_bytes in image_jobs
    ))

    extractions_by_asset = {
        item["asset"]: item["markdown"]
        for item in extractions
        if item.get("markdown")
    }
    enriched_reader = _backfill_image_markdown(raw_reader, extractions_by_asset)
    enriched_markdown = _backfill_image_markdown(raw_markdown, extractions_by_asset)
    failed_count = sum(1 for item in extractions if item.get("status") == "FAILED")
    ocr_status = "PARTIAL" if failed_count and failed_count < len(extractions) else ("FAILED" if failed_count else "COMPLETED")

    archive_path = package.get("archive_path")
    if archive_path:
        archive_dir = Path(archive_path)
        try:
            archive_dir.mkdir(parents=True, exist_ok=True)
            (archive_dir / "enriched_reader.md").write_text(enriched_reader, encoding="utf-8")
            (archive_dir / "image_ocr.json").write_text(
                json.dumps(extractions, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    if trace_callback:
        prefix = f"{role}/" if role else ""
        trace_callback({
            "kind": "artifact",
            "path": f"{prefix}pdf_markdown/enriched_reader.md",
            "content": enriched_reader,
            **({"role": role} if role else {}),
        })
        trace_callback({
            "kind": "artifact",
            "path": f"{prefix}pdf_markdown/image_ocr.json",
            "content": extractions,
            **({"role": role} if role else {}),
        })

    return {
        **package,
        "raw_reader_markdown": raw_reader,
        "reader_markdown": enriched_reader,
        "enriched_markdown": enriched_markdown,
        "image_extractions": extractions,
        "image_ocr_status": ocr_status,
    }


def _nearest_preceding_source(sources: list[dict[str, Any]], bbox: list[float] | None) -> tuple[int, dict[str, Any] | None]:
    if not sources:
        return 0, None
    if not bbox or len(bbox) < 2:
        return len(sources), sources[-1]
    chart_top = float(bbox[1])
    insert_at = 0
    nearest: dict[str, Any] | None = None
    for idx, src in enumerate(sources):
        src_bbox = src.get("bbox")
        src_top = float(src_bbox[1]) if isinstance(src_bbox, (list, tuple)) and len(src_bbox) >= 2 else 0.0
        if src_top <= chart_top + 4.0:
            nearest = src
            insert_at = idx + 1
    return insert_at, nearest or sources[0]


_SECTION_HEADING_RE = re.compile(r"^(?:[一二三四五六七八九十]+[、.]|\([一二三四五六七八九十]+\)|（[一二三四五六七八九十]+）)")


def _bbox_area(bbox: list[float] | tuple[float, ...]) -> float:
    return max(0.0, float(bbox[2]) - float(bbox[0])) * max(0.0, float(bbox[3]) - float(bbox[1]))


def _bbox_overlap_ratio(outer: list[float], inner: list[float]) -> float:
    ix0 = max(float(outer[0]), float(inner[0]))
    iy0 = max(float(outer[1]), float(inner[1]))
    ix1 = min(float(outer[2]), float(inner[2]))
    iy1 = min(float(outer[3]), float(inner[3]))
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inner_area = _bbox_area(inner)
    if inner_area <= 0.0:
        return 0.0
    return ((ix1 - ix0) * (iy1 - iy0)) / inner_area


def _deduplicate_page_extractions(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop smaller nested chart sub-crops when a full chart image on the same page exists."""
    valid = [item for item in items if isinstance(item.get("bbox"), (list, tuple)) and len(item["bbox"]) >= 4]
    ordered = sorted(valid, key=lambda item: _bbox_area(item["bbox"]), reverse=True)
    kept: list[dict[str, Any]] = []
    for item in ordered:
        bbox = [float(v) for v in item["bbox"][:4]]
        if any(_bbox_overlap_ratio([float(v) for v in k["bbox"][:4]], bbox) >= 0.85 for k in kept):
            continue
        kept.append(item)
    return kept


def _is_chart_internal_fragment(src: dict[str, Any], chart_bboxes: list[list[float]]) -> bool:
    if src.get("source_kind") == "image_ocr":
        return False
    bbox = src.get("bbox")
    if not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
        return False
    raw = str(src.get("raw_text") or "").strip()
    if _SECTION_HEADING_RE.match(raw):
        return False
    cx = (float(bbox[0]) + float(bbox[2])) / 2.0
    cy = (float(bbox[1]) + float(bbox[3])) / 2.0
    for chart_bbox in chart_bboxes:
        if (chart_bbox[0] - 2.0 <= cx <= chart_bbox[2] + 2.0
                and chart_bbox[1] - 2.0 <= cy <= chart_bbox[3] + 2.0):
            return True
    return False


def _inject_image_sources(
    catalogue: dict[str, Any],
    extractions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    pages = catalogue.get("pages")
    chapters = catalogue.get("chapters")
    chapter_list = [dict(ch) for ch in chapters] if isinstance(chapters, list) else []
    if not isinstance(pages, list) or not extractions:
        total = sum(len(p.get("sources") or []) for p in pages) if isinstance(pages, list) else 0
        return (pages if isinstance(pages, list) else []), chapter_list, total

    by_page: dict[int, list[dict[str, Any]]] = {}
    for item in extractions:
        if item.get("markdown") and item.get("page") and item.get("bbox"):
            by_page.setdefault(int(item["page"]), []).append(item)
    if not by_page:
        total = sum(len(p.get("sources") or []) for p in pages)
        return pages, chapter_list, total

    chapter_by_id = {str(ch.get("section_id")): ch for ch in chapter_list if ch.get("section_id")}
    doc_role = str(catalogue.get("role") or "")
    doc_filename = str(catalogue.get("filename") or "")
    updated_pages: list[dict[str, Any]] = []
    for page_entry in pages:
        page_num = int(page_entry.get("page") or 0)
        raw_items = by_page.get(page_num)
        if not raw_items:
            updated_pages.append(page_entry)
            continue
        items = _deduplicate_page_extractions(raw_items)
        chart_bboxes = [[float(v) for v in item["bbox"][:4]] for item in items]

        raw_sources = list(page_entry.get("sources") or [])
        removed_ids: set[str] = set()
        sources: list[dict[str, Any]] = []
        for src in raw_sources:
            if _is_chart_internal_fragment(src, chart_bboxes):
                if src.get("source_id"):
                    removed_ids.add(str(src["source_id"]))
                continue
            sources.append(src)
        if removed_ids:
            for ch in chapter_list:
                ch_ids = ch.get("source_ids")
                if isinstance(ch_ids, list):
                    ch["source_ids"] = [sid for sid in ch_ids if str(sid) not in removed_ids]

        existing_ids = {str(s.get("source_id")) for s in sources}
        role_value = doc_role or str((sources[0].get("document_role") if sources else "") or "document")
        filename_value = doc_filename or str((sources[0].get("filename") if sources else "") or "")

        for item in items:
            asset = str(item["asset"])
            source_id = hashlib.sha256(f"{role_value}:{page_num}:{asset}".encode("utf-8")).hexdigest()[:24]
            if source_id in existing_ids:
                continue
            bbox = [round(float(v), 2) for v in item["bbox"][:4]]
            insert_at, anchor = _nearest_preceding_source(sources, bbox)
            if anchor is None and raw_sources:
                _, anchor = _nearest_preceding_source(raw_sources, bbox)
            section_id = (anchor or {}).get("section_id")
            markdown_text = str(item["markdown"]).strip()
            new_source = {
                "source_id": source_id,
                "document_role": role_value,
                "source_kind": "image_ocr",
                "locator": asset,
                "page": page_num,
                "bbox": bbox,
                "raw_text": markdown_text,
                "filename": filename_value,
                "page_width": page_entry.get("width"),
                "page_height": page_entry.get("height"),
                "rotation": page_entry.get("rotation", 0),
                "cell_text": markdown_text,
                "cell_source_ids": [source_id],
                "row_text": markdown_text,
                "row_source_ids": [source_id],
                "section_path": list((anchor or {}).get("section_path") or []),
                "section_id": section_id,
            }
            sources.insert(insert_at, new_source)
            existing_ids.add(source_id)
            if section_id and str(section_id) in chapter_by_id:
                ch = chapter_by_id[str(section_id)]
                ch_ids = list(ch.get("source_ids") or [])
                if source_id not in ch_ids:
                    ch_ids.append(source_id)
                    ch["source_ids"] = ch_ids

        updated_pages.append({**page_entry, "sources": sources})
    total_sources = sum(len(p.get("sources") or []) for p in updated_pages)
    return updated_pages, chapter_list, total_sources


async def enrich_catalogue_reader_context(
    catalogue: dict[str, Any],
    package: dict[str, Any],
    *,
    provider: Any | None = None,
) -> dict[str, Any]:
    """Backfill VLM chart/figure Markdown into a bound reader_context."""
    reader_context = catalogue.get("reader_context")
    if not isinstance(reader_context, dict):
        return catalogue
    enriched = await enrich_package_images(package, provider=provider)
    extractions = enriched.get("image_extractions", [])
    extractions_by_asset = {
        item["asset"]: item["markdown"]
        for item in extractions
        if item.get("markdown")
    }
    if not extractions_by_asset:
        return {
            **catalogue,
            "reader_context": {
                **reader_context,
                "image_extractions": extractions,
                "image_ocr_status": enriched.get("image_ocr_status"),
            },
        }

    updated_units = []
    for unit in reader_context.get("logical_units") or []:
        if unit.get("kind") == "image":
            updated_units.append({
                **unit,
                "markdown": _backfill_image_markdown(str(unit.get("markdown") or ""), extractions_by_asset),
            })
        else:
            updated_units.append(unit)

    updated_pages, updated_chapters, total_sources = _inject_image_sources(catalogue, extractions)
    return {
        **catalogue,
        "pages": updated_pages,
        **({"chapters": updated_chapters} if "chapters" in catalogue else {}),
        **({"source_count": total_sources} if "source_count" in catalogue else {}),
        "reader_context": {
            **reader_context,
            "markdown": enriched["reader_markdown"],
            "logical_units": updated_units,
            "image_extractions": extractions,
            "image_ocr_status": enriched.get("image_ocr_status"),
        },
    }
