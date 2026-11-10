from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from html import unescape
from pathlib import Path
from typing import Any, Callable
from datetime import date

import pymupdf as fitz

from app.pdf_document import bind_catalogue, verified_conversion
from app.pdf_worker import run_pdf
from app.source_evidence import evidence_source
from app.source_aliases import SourceAliasProvider
from app.model_json import read_model_json
from app.summary_rules import load_markdown_rules, module_groups


RULE_CONFIG, RULES = load_markdown_rules()
RULE_VERSION = str(RULE_CONFIG["version"])
CONTEXT_BATCH_CHARACTERS = 16_000
LOGICAL_TABLE_CHARACTERS = 32_000
MODEL_PAYLOAD_CHARACTERS = 48_000
MODEL_REQUEST_CHARACTERS = 64_000
# Compatibility fields are output/provenance only. New prompts and workflow
# joins use stable item_id; Excel row numbers never identify execution items.
for _rule in RULES:
    _rule.setdefault("key", _rule["item_id"])
    _rule.setdefault("terms", _rule.get("search_terms", []))


def _load_rule_snapshot() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read a consistent rule snapshot when a comparison starts."""
    return load_markdown_rules()


def _normalize_model_item_id(value: dict[str, Any], row_to_item_id: dict[int, str]) -> dict[str, Any] | None:
    item_id = value.get("item_id")
    if isinstance(item_id, str) and item_id:
        return {**value, "item_id": item_id}
    # Read-only compatibility for stored/test responses from the old prompt.
    row = value.get("excel_row")
    if str(row).isdigit() and int(row) in row_to_item_id:
        return {**value, "item_id": row_to_item_id[int(row)]}
    return None


def _heading_info(text: str) -> tuple[str, int] | None:
    """Return a structural heading prefix and depth; bare codes/dates are not headings."""
    text = text.strip()
    if re.search(r"[.．…]{3,}\s*\d+\s*$", text):
        return None
    markdown = re.match(r"^(#{1,6})\s+\S", text)
    if markdown:
        return markdown.group(1), len(markdown.group(1))
    section = re.match(r"^§\s*\d+(?:\.\d+)*\s*\S", text)
    if section:
        return section.group(0), 1
    if re.match(r"^第[一二三四五六七八九十百]+[章节部分]\s*\S", text):
        return text.split()[0], 1
    if re.match(r"^[一二三四五六七八九十]+、\s*\S", text):
        return text.split()[0], 1
    subsection = re.match(r"^[（(][一二三四五六七八九十]+[)）]\s*\S", text)
    if subsection:
        return subsection.group(0), 2
    # Arabic section markers must include punctuation (1. / 1、 / 5.1).
    # Without it, body text such as "2季度，全球经济……" becomes a fake heading.
    match = re.match(r"^(\d+(?:\.\d+)+(?:[、.．])?|\d+[、.．])\s*(?=[\u3400-\u9fffA-Za-z《【（(])", text)
    if match:
        prefix = match.group(1)
        if re.fullmatch(r"\d{4}", prefix):
            return None
        if re.fullmatch(r"\d+[、.．]", prefix):
            return prefix, 3
        return prefix, max(2, prefix.count(".") + 1)
    return None


def _source_heading_info(source: dict[str, Any]) -> tuple[str, int] | None:
    text = source.get("raw_text", "")
    if len(text) > 60 and re.search(r"[，。；]", text):
        return None
    if len(source.get("row_source_ids", [])) > 1 and re.match(r"^\d", text.strip()):
        return None
    return _heading_info(text)


def build_source_catalogue(filename: str, content: bytes, role: str) -> dict[str, Any]:
    """Extract addressable text lines from a PDF. Model output can only cite these IDs."""
    doc_hash = hashlib.sha256(content).hexdigest()
    doc = fitz.open(stream=content, filetype="pdf")
    try:
        pages: list[dict[str, Any]] = []
        sources: list[dict[str, Any]] = []
        for page_number, page in enumerate(doc, start=1):
            page_lines: list[dict[str, Any]] = []
            raw = page.get_text("dict", sort=True)
            for block_index, block in enumerate(raw.get("blocks", [])):
                if block.get("type") != 0:
                    continue
                for line_index, line in enumerate(block.get("lines", [])):
                    text = "".join(span.get("text", "") for span in line.get("spans", [])).strip()
                    if not text:
                        continue
                    text_rect = fitz.Rect(line.get("bbox", block.get("bbox", [])))
                    if page.rotation:
                        text_rect *= page.rotation_matrix
                    rect = [round(float(value), 2) for value in text_rect]
                    source = evidence_source(
                        document_sha256=doc_hash,
                        document_role=role,
                        page=page_number,
                        source_kind="text_line",
                        locator=f"block:{block_index}:line:{line_index}",
                        raw_text=text,
                        bbox=rect,
                        extra={
                        "filename": Path(filename).name,
                        "page_width": float(page.rect.width),
                        "page_height": float(page.rect.height),
                        "rotation": int(page.rotation),
                        "cell_text": text,
                        "cell_source_ids": [],
                        "row_text": text,
                        "row_source_ids": [],
                        "section_path": [],
                        },
                    )
                    source["cell_source_ids"] = [source["source_id"]]
                    source["row_source_ids"] = [source["source_id"]]
                    page_lines.append(source)
                    sources.append(source)
            vertical_groups: list[list[dict[str, Any]]] = []
            for source in sorted(page_lines, key=lambda value: (value["bbox"][0], value["bbox"][1])):
                text = source["raw_text"]
                if not re.fullmatch(r"[\u3400-\u9fff]{1,3}", text):
                    continue
                x0, y0, x1, y1 = source["bbox"]
                group = next((candidate for candidate in reversed(vertical_groups)
                              if (previous := candidate[-1])["bbox"][3] <= y0 + 1.0
                              and y0 - previous["bbox"][3] <= 5.0
                              and min(x1, previous["bbox"][2]) - max(x0, previous["bbox"][0])
                                  >= 0.7 * min(x1 - x0, previous["bbox"][2] - previous["bbox"][0])), None)
                if group is None:
                    vertical_groups.append([source])
                else:
                    group.append(source)
            for group in vertical_groups:
                if len(group) > 1:
                    combined = "".join(source["raw_text"] for source in group)
                    ids = [source["source_id"] for source in group]
                    for source in group:
                        source["cell_text"] = combined
                        source["cell_source_ids"] = ids
            row_groups: list[list[dict[str, Any]]] = []
            for source in sorted(page_lines, key=lambda value: (value["bbox"][1], value["bbox"][0])):
                x0, y0, x1, y1 = source["bbox"]
                center_y = (y0 + y1) / 2
                group = next((candidate for candidate in reversed(row_groups)
                              if abs(center_y - (candidate[0]["bbox"][1] + candidate[0]["bbox"][3]) / 2) <= 3.0), None)
                if group is None:
                    row_groups.append([source])
                else:
                    group.append(source)
            for group in row_groups:
                ordered = sorted(group, key=lambda value: value["bbox"][0])
                ids = [source["source_id"] for source in ordered]
                row_text = " | ".join(source["raw_text"] for source in ordered)
                for source in ordered:
                    source["row_text"] = row_text
                    source["row_source_ids"] = ids
            for source in page_lines:
                center_y = (source["bbox"][1] + source["bbox"][3]) / 2
                nearby = sorted(
                    (candidate for candidate in page_lines
                     if abs((candidate["bbox"][1] + candidate["bbox"][3]) / 2 - center_y) <= 20.0),
                    key=lambda value: (value["bbox"][1], value["bbox"][0]),
                )
                source["layout_context"] = [
                    {"source_id": candidate["source_id"], "raw_text": candidate["raw_text"], "bbox": candidate["bbox"]}
                    for candidate in nearby
                ]
            pages.append({"page": page_number, "width": float(page.rect.width), "height": float(page.rect.height), "rotation": int(page.rotation), "sources": page_lines})

        # Build a lightweight document-level heading tree; retain every source line
        # so chapter aggregation never drops continuation pages or body paragraphs.
        heading_stack: list[tuple[int, str]] = []
        active_heading_source_ids: list[str] = []
        chapter_records: dict[str, dict[str, Any]] = {}
        for source in sources:
            text = source["raw_text"]
            heading = _source_heading_info(source)
            if heading and len(text) <= 120:
                _, depth = heading
                simple = re.match(r"^(\d+)[、.．]\s*[^\d]", text)
                parent = next((match for _, title in heading_stack
                               if (match := re.match(r"^(\d+)\.\d", title))), None)
                if simple and parent and int(simple.group(1)) == int(parent.group(1)) + 1:
                    depth = 1
                heading_stack = [(d, value) for d, value in heading_stack if d < depth]
                active_heading_source_ids = active_heading_source_ids[:len(heading_stack)]
                heading_stack.append((depth, text))
                active_heading_source_ids.append(source["source_id"])
            source["section_path"] = [value for _, value in heading_stack]
            chapter_key = " / ".join(source["section_path"]) or "未分章正文"
            source["section_id"] = hashlib.sha256(f"{doc_hash}|{role}|{chapter_key}".encode()).hexdigest()[:24]
            chapter = chapter_records.setdefault(chapter_key, {
                "section_id": source["section_id"], "path": chapter_key,
                "title": source["section_path"][-1] if source["section_path"] else "未分章正文",
                "heading_source_ids": list(active_heading_source_ids),
                "pages": [], "source_ids": [],
            })
            if source["page"] not in chapter["pages"]:
                chapter["pages"].append(source["page"])
            chapter["source_ids"].append(source["source_id"])

        return {
            "filename": Path(filename).name,
            "role": role,
            "sha256": doc_hash,
            "page_count": doc.page_count,
            "page_dimensions": [{"width": p["width"], "height": p["height"], "rotation": p["rotation"]} for p in pages],
            "pages": pages,
            "chapters": list(chapter_records.values()),
            "source_count": len(sources),
        }
    finally:
        doc.close()


def _norm(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def _metadata_date_interval(metadata: dict[str, Any], interval_key: str, date_key: str) -> tuple[date, date] | None:
    interval = metadata.get(interval_key)
    if isinstance(interval, dict):
        try:
            return date.fromisoformat(interval["start"]), date.fromisoformat(interval["end"])
        except (KeyError, TypeError, ValueError):
            return None
    value = metadata.get(date_key)
    if isinstance(value, str):
        try:
            parsed = date.fromisoformat(value)
            return parsed, parsed
        except ValueError:
            return None
    return None


def _fallback_source_date_status(
    rule: dict[str, Any], documents: dict[str, dict[str, Any]], fallback_role: str,
    *, historical: bool,
) -> tuple[str, str | None]:
    """Check whether a supplemental document can support the target's time point."""
    if not historical:
        return "ELIGIBLE", None
    target = _metadata_date_interval(
        documents.get("product_summary", {}).get("metadata", {}),
        "compiled_date_interval", "compiled_date",
    )
    source = _metadata_date_interval(
        documents.get(fallback_role, {}).get("metadata", {}),
        "date_interval", "date",
    )
    if not target or not source:
        return "DATE_UNCONFIRMED", "无法确认补充文件版本在概要编制时已适用。"
    if source[0] > target[1]:
        return "AFTER_TARGET_DATE", "补充文件版本晚于概要编制日期，不能直接作为当时适用依据。"
    # Coarse date precision (for example, a prospectus dated only by month)
    # can overlap the summary's compilation date without proving the source is
    # a later version. Let evidence and field applicability decide; only a
    # source interval wholly after the target is blocked above.
    return "ELIGIBLE", None


def _fallback_trigger_reason(
    rule: dict[str, Any], fallback_when: list[str], *, lookup_override: str | None,
    primary_not_applicable: bool, primary_fact_verified: bool,
) -> str | None:
    """Resolve the rule-declared fallback policy without indicator-name branches."""
    if primary_fact_verified:
        return None
    conditions = set(fallback_when)
    if lookup_override == "CONFIRMED_ABSENT" and "primary_confirmed_absent" in conditions:
        return "PRIMARY_CONFIRMED_ABSENT"
    if primary_not_applicable and "primary_not_applicable" in conditions:
        return "PRIMARY_NOT_APPLICABLE"
    if ("same_contract_annual_rate" in conditions
            and rule.get("value_type") == "annual_rate"
            and not primary_fact_verified):
        return "PRIMARY_ANNUAL_RATE_NOT_VERIFIED"
    if ("same_period_same_nature_amount" in conditions
            and rule.get("period_policy") == "same_period"
            and lookup_override == "CONFIRMED_ABSENT"
            and not primary_fact_verified):
        return "PRIMARY_PERIOD_AMOUNT_CONFIRMED_ABSENT"
    return None


def _recovery_candidates(
    item: dict[str, Any], side: str, preferred_ids: list[str] | None = None, *, limit: int = 80,
) -> list[dict[str, Any]]:
    """Repair citations within a stated excerpt; never use it to infer absence."""
    plan = item.get(f"{side}_context_plan")
    if plan is not None:
        batches = plan["batches"]
        batch = next((batch for batch in batches if any(source["source_id"] in (preferred_ids or [])
                                                       for source in batch["candidates"])), batches[0] if batches else {})
        result = batch.get("candidates", [])
        item[f"{side}_repair_units"] = batch.get("logical_units", [])
        item[f"{side}_excerpt_coverage"] = {
            "source_count": plan["source_count"], "sent_source_count": len(result),
            "coverage_complete": plan["coverage_complete"] and len(batches) == 1,
            "skipped_unit_count": len(plan["skipped_units"]), "purpose": "citation_repair",
        }
        return result
    candidates = item.get(f"{side}_candidates", [])
    terms = [_search_norm(term) for term in [item.get("label", ""), *item.get("terms", [])] if term]
    anchors = {index for index, source in enumerate(candidates)
               if any(term in _search_norm(source.get(key, ""))
                      for term in terms for key in ("raw_text", "row_text", "cell_text"))}
    neighbors = {index for anchor in anchors for index in range(max(0, anchor - 2), min(len(candidates), anchor + 3))}
    preferred = [index for index, source in enumerate(candidates) if source["source_id"] in (preferred_ids or [])]
    ranked = list(dict.fromkeys([*preferred, *sorted(anchors), *sorted(neighbors), *range(len(candidates))]))
    excerpt = []
    for index in ranked:
        source = _prompt_source(candidates[index])
        if len(excerpt) < limit and _json_size([*excerpt, source]) <= CONTEXT_BATCH_CHARACTERS:
            excerpt.append(source)
    item[f"{side}_excerpt_coverage"] = {"source_count": len(candidates), "sent_source_count": len(excerpt),
                                       "coverage_complete": len(excerpt) == len(candidates), "purpose": "citation_repair"}
    return excerpt


def build_document_source_catalogue(filename: str, content: bytes, role: str) -> dict[str, Any]:
    """Bind a verified conversion to the existing PDF evidence identities."""
    catalogue = build_source_catalogue(filename, content, role)
    package = verified_conversion(content)
    catalogue["reader_context"] = bind_catalogue(catalogue, package)
    return catalogue


def _complete_section_sources(catalogue: dict[str, Any], anchor: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect a whole structural section across pages, stopping at its next peer."""
    flattened = [source for page in catalogue["pages"] for source in page["sources"]]
    anchor_index = next((index for index, source in enumerate(flattened)
                         if source["source_id"] == anchor["source_id"]), None)
    path = anchor.get("section_path") or []
    if anchor_index is None or not path:
        return [source for source in flattened if source["page"] == anchor["page"]]

    title = path[-1]
    start = next((index for index in range(anchor_index, -1, -1)
                  if flattened[index]["raw_text"] == title and _source_heading_info(flattened[index])), None)
    if start is None:
        return [source for source in flattened if source["page"] == anchor["page"]]

    heading = _source_heading_info(flattened[start])
    depth = heading[1] if heading else 1
    end = next((index for index in range(start + 1, len(flattened))
                if (next_heading := _source_heading_info(flattened[index]))
                and next_heading[1] <= depth), len(flattened))
    return flattened[start:end]


def _markdown_table_context(catalogue: dict[str, Any], item: dict[str, Any]) -> list[str]:
    reader = catalogue.get("reader_context") or {}
    if reader.get("status") != "READY":
        return []
    markdown = str(reader.get("markdown") or "")
    tables = []
    depth = 0
    start = 0
    for tag in re.finditer(r"<(/?)table\b[^>]*>", markdown, re.IGNORECASE):
        if not tag.group(1):
            if depth == 0:
                start = tag.start()
            depth += 1
        elif depth:
            depth -= 1
            if depth == 0:
                tables.append(markdown[start:tag.end()])
    without_html_tables = re.sub(r"<table\b.*?</table>", "", markdown, flags=re.IGNORECASE | re.DOTALL)
    current = []
    for line in [*without_html_tables.splitlines(), ""]:
        if "|" in line:
            current.append(line)
        elif current:
            tables.append("\n".join(current))
            current = []
    terms = [_search_norm(term) for term in [item.get("label", ""), *item.get("terms", [])] if term]
    return [table for table in dict.fromkeys(tables)
            if any(term in _search_norm(unescape(re.sub(r"<[^>]*>", "", table))) for term in terms)]


def _search_norm(value: str) -> str:
    return re.sub(r"[^\w\u3400-\u9fff]", "", str(value)).casefold()


def _chapter_directory(catalogue: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep source IDs locally; the model receives only titles and page ranges."""
    sources_by_id = {source["source_id"]: source for page in catalogue.get("pages", [])
                     for source in page.get("sources", [])}
    sections = catalogue.get("chapters") or [
        {"section_id": f"page:{page['page']}", "title": f"第{page['page']}页（未分章）",
         "path": "未分章正文", "pages": [page["page"]],
         "source_ids": [source["source_id"] for source in page.get("sources", [])]}
        for page in catalogue.get("pages", [])
    ]
    directory = []
    for section in sections:
        source_ids = [sid for sid in section.get("source_ids", []) if sid in sources_by_id]
        if source_ids:
            directory.append({
                "section_id": section["section_id"], "path": section.get("path") or section.get("title", ""),
                "title": section.get("title", ""), "pages": section.get("pages", []),
                "source_count": len(source_ids), "source_ids": source_ids,
            })
    return directory


def _prescribed_sections(catalogue: dict[str, Any], item: dict[str, Any], *, first_only: bool = True) -> list[dict[str, Any]]:
    """Prefer configured chapter titles; ancestor matches must not pull in siblings."""
    hints = item.get("section_hints_by_role", {}).get(catalogue.get("role"), item.get("section_hints", []))
    if not hints and catalogue.get("role") == "product_summary":
        hints = item.get("target_section_path", [])
    hints = [_search_norm(hint) for hint in hints if hint]
    sections = _chapter_directory(catalogue)
    selected = []
    seen = set()
    for hint in hints:
        roots = [section for section in sections if hint in _search_norm(section["title"])]
        exact = [section for section in roots if hint == _search_norm(re.sub(
            r"^(?:§\s*\d+(?:\.\d+)*|第[一二三四五六七八九十百]+[章节部分]+|[（(][一二三四五六七八九十]+[)）]|[一二三四五六七八九十]+、|\d+[\d.．、]*)\s*",
            "", section["title"]))]
        roots = exact or roots
        for root in roots:
            for section in sections:
                if (section["path"] == root["path"] or section["path"].startswith(root["path"]+" / ")) and section["section_id"] not in seen:
                    selected.append(section)
                    seen.add(section["section_id"])
        if roots and first_only:
            break
    return selected


def _source_candidates(catalogue: dict[str, Any], item: dict[str, Any]) -> list[dict[str, Any]]:
    sources = [source for page in catalogue.get("pages", []) for source in page.get("sources", [])]
    terms = [_search_norm(term) for term in item.get("terms", item.get("search_terms", [])) if term]
    selected_ids = {sid for section in _prescribed_sections(catalogue, item) for sid in section["source_ids"]}
    if selected_ids:
        selected = [source for source in sources if source["source_id"] in selected_ids]
        if catalogue.get("role") == "product_summary" and not any(
            term in _search_norm(source.get(key, ""))
            for source in selected for term in terms for key in ("raw_text", "row_text", "cell_text")
        ):
            return []
        return selected
    anchors = [source for source in sources
               if any(term in _search_norm(source.get(key, ""))
                      for term in terms for key in ("raw_text", "row_text", "cell_text"))]
    selected_ids = {source["source_id"] for anchor in anchors
                    for source in _complete_section_sources(catalogue, anchor)}
    return [source for source in sources if source["source_id"] in selected_ids]


def _prompt_source(source: dict[str, Any]) -> dict[str, Any]:
    # Raw lines are evidence. Nearby text is recoverable through its own source ID.
    return {**{key: source[key] for key in ("source_id", "page", "bbox", "raw_text") if key in source},
            **({"role": source["document_role"]} if source.get("document_role") else {})}


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False))


def _context_plan(
    catalogue: dict[str, Any], candidates: list[dict[str, Any]], *,
    item: dict[str, Any] | None = None, budget: int | None = None,
) -> dict[str, Any]:
    """Pack complete units. An oversized table stays unexamined and is reported."""
    table_budget = LOGICAL_TABLE_CHARACTERS if budget is None else budget
    budget = CONTEXT_BATCH_CHARACTERS if budget is None else budget
    sources = {source["source_id"]: source for source in candidates}
    units = []
    assigned = set()
    skipped = []
    reader = catalogue.get("reader_context") or {}
    catalogue_sources = {source["source_id"]: source for page in catalogue.get("pages", [])
                         for source in page.get("sources", [])}
    source_order = {source_id: index for index, source_id in enumerate(catalogue_sources)}
    for unit in catalogue.get("logical_units", reader.get("logical_units", [])):
        ids = [sid for sid in unit.get("source_ids", []) if sid in sources]
        if not ids:
            continue
        # A selected fragment cannot turn a cross-page table into a partial table.
        all_ids = sorted(set(unit.get("source_ids", [])), key=lambda sid: source_order.get(sid, len(source_order)))
        for sid in all_ids:
            if sid in catalogue_sources:
                sources[sid] = catalogue_sources[sid]
        ids = [sid for sid in all_ids if sid in sources]
        if len(ids) != len(all_ids):
            skipped.append({"unit_id": unit.get("unit_id"), "kind": unit.get("kind", "table"),
                            "source_ids": all_ids, "reason": "LOGICAL_UNIT_SOURCE_IDS_UNBOUND"})
            assigned.update(ids)
            continue
        units.append({"unit_id": unit.get("unit_id"), "kind": unit.get("kind", "table"),
                      "source_ids": ids, "markdown": unit.get("markdown", ""),
                      "pages": unit.get("pages", [])})
        assigned.update(ids)
    section_groups: dict[str, list[str]] = {}
    for source in sources.values():
        if source["source_id"] not in assigned:
            key = source.get("section_id") or f"page:{source['page']}"
            section_groups.setdefault(key, []).append(source["source_id"])
    for section_id, ids in section_groups.items():
        whole = {"unit_id": section_id, "kind": "section", "source_ids": ids}
        if _json_size({"unit": whole, "candidates": [_prompt_source(sources[sid]) for sid in ids]}) <= budget:
            units.append(whole)
            continue
        # Without a linked table index, raw rows are the smallest known units.
        row_groups: dict[tuple[str, ...], list[str]] = {}
        for sid in ids:
            row_ids = tuple(source_id for source_id in sources[sid].get("row_source_ids", [sid]) if source_id in ids)
            row_groups.setdefault(row_ids or (sid,), []).append(sid)
        for index, row_ids in enumerate(row_groups.values()):
            units.append({"unit_id": f"{section_id}:row:{index}", "section_id": section_id,
                          "kind": "source_row", "source_ids": row_ids})
    batches = []
    current = {"candidates": [], "logical_units": []}
    for unit in units:
        unit_sources = [_prompt_source(sources[sid]) for sid in unit["source_ids"]]
        unit_info = {key: value for key, value in unit.items() if key != "source_ids"}
        packet = {"candidates": unit_sources, "logical_units": [unit_info]}
        packet_size = _json_size(packet)
        if budget < packet_size <= table_budget and unit["kind"] == "table":
            if current["candidates"]:
                batches.append(current)
                current = {"candidates": [], "logical_units": []}
            batches.append(packet)
            continue
        if packet_size > budget:
            skipped.append({"unit_id": unit["unit_id"], "kind": unit["kind"],
                            "source_ids": unit["source_ids"], "reason": "LOGICAL_UNIT_EXCEEDS_BUDGET"})
            continue
        combined = {"candidates": [*current["candidates"], *unit_sources],
                    "logical_units": [*current["logical_units"], unit_info]}
        if current["candidates"] and _json_size(combined) > budget:
            batches.append(current)
            current = {"candidates": [], "logical_units": []}
        current["candidates"].extend(unit_sources)
        current["logical_units"].append(unit_info)
    if current["candidates"]:
        batches.append(current)
    hint_skips = []
    if item and not reader.get("logical_units") and not catalogue.get("logical_units"):
        for table in _markdown_table_context(catalogue, item):
            if batches and _json_size({**batches[0], "table_context": [*batches[0].get("table_context", []), table]}) <= budget:
                batches[0].setdefault("table_context", []).append(table)
            else:
                hint_skips.append({"reason": "UNLINKED_TABLE_HINT_EXCEEDS_BUDGET", "characters": len(table)})
    all_ids = list(sources)
    sent_ids = {source["source_id"] for batch in batches for source in batch["candidates"]}
    return {"batches": batches, "skipped_units": skipped, "skipped_structure_hints": hint_skips,
            "source_ids": all_ids, "source_count": len(all_ids), "planned_source_count": len(sent_ids),
            "coverage_complete": not skipped and len(sent_ids) == len(all_ids), "budget_characters": budget,
            "table_budget_characters": table_budget}


def _payload_batches(items: list[dict[str, Any]], budget: int = MODEL_PAYLOAD_CHARACTERS) -> list[list[dict[str, Any]]]:
    chapters: dict[tuple, list[dict[str, Any]]] = {}
    for item in items:
        if _json_size(_shared_context([item])) > budget:
            raise ValueError(f"CONTEXT_ITEM_EXCEEDS_BUDGET: {item.get('item_id')}")
        context = tuple((key, tuple(source["source_id"] for source in value))
                        for key, value in sorted(item.items()) if key.endswith("_candidates") and isinstance(value, list))
        chapters.setdefault(context, []).append(item)
    blocks = []
    for group in chapters.values():
        ids = [item.get("item_id") for item in group]
        if len(ids) == len(set(ids)) and _json_size(_shared_context(group)) <= budget:
            blocks.append(group)
        else:
            blocks.extend([item] for item in group)
    batches = []
    current = []
    for block in blocks:
        existing_ids = {item.get("item_id") for item in current if item.get("item_id")}
        if current and (_json_size(_shared_context([*current, *block])) > budget
                        or any(item.get("item_id") in existing_ids for item in block)):
            batches.append(current)
            current = []
        current.extend(block)
    if current:
        batches.append(current)
    return batches


def _shared_context(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Send repeated chapter/table sources once, with per-item citation scopes."""
    pools: dict[str, dict[str, Any]] = {"sources": {}, "logical_units": {}, "tables": {}}
    packed = []
    for item in items:
        value = dict(item)
        for key, content in item.items():
            if not isinstance(content, list) or not content:
                continue
            if key.endswith("_candidates") and all(isinstance(source, dict) and "source_id" in source for source in content):
                ids = []
                for source in content:
                    source_id = source["source_id"]
                    compact_source = {key: value for key, value in source.items() if key != "source_id"}
                    if source_id in pools["sources"] and pools["sources"][source_id] != compact_source:
                        raise ValueError("SOURCE_CONTEXT_ID_COLLISION")
                    pools["sources"][source_id] = compact_source
                    ids.append(source_id)
                value.pop(key)
                value[f"allowed_{key.removesuffix('_candidates')}_source_ids"] = ids
            elif key.endswith("logical_units") or key.endswith("table_context"):
                pool_name = "logical_units" if key.endswith("logical_units") else "tables"
                ids = []
                for unit in content:
                    unit_id = hashlib.sha256(json.dumps(unit, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]
                    pools[pool_name][unit_id] = unit
                    ids.append(unit_id)
                value.pop(key)
                value[f"allowed_{key}_ids"] = ids
        packed.append(value)
    shared = {"items": packed, "context_pool": {key: value for key, value in pools.items() if value}}
    plain = {"items": items}
    return shared if _json_size(shared) < _json_size(plain) else plain


def _verified_fact(value: Any, source_ids: Any, candidates: list[dict[str, Any]], rule: dict[str, Any]) -> dict[str, Any] | None:
    ids = list(source_ids) if isinstance(source_ids, list) else []
    by_id = {source["source_id"]: source for source in candidates}
    evidence = [by_id[sid] for sid in ids if sid in by_id]
    bound = []
    if evidence and len(evidence) == len(set(ids)) and not _field_evidence_is_substantive(rule, evidence):
        section_ids = {source.get("section_id") for source in evidence}
        terms = [_norm(term) for term in rule.get("terms", []) if _norm(term)]
        if len(section_ids) == 1 and None not in section_ids:
            headings = [source for source in candidates if source.get("section_id") in section_ids
                        and _source_heading_info(source)
                        and any(term in _norm(source["raw_text"]) for term in terms)]
            if len(headings) == 1 and headings[0]["source_id"] not in ids:
                bound = [headings[0]["source_id"]]
                ids.extend(bound)
                evidence.append(headings[0])
    if (value and evidence and len(evidence) == len(set(ids))
            and _numbers_are_grounded(str(value), evidence) and _field_evidence_is_substantive(rule, evidence)):
        fact = {"value": str(value), "source_ids": ids}
        if bound:
            fact["citation_binding"] = {"method": "unique_section_heading", "added_source_ids": bound}
        return fact
    return None


def _bind_model_fact_sources(values: list[dict[str, Any]], inputs: dict[str, dict[str, Any]]) -> None:
    """Bind a quoted value to an unambiguous heading within its allowed chapter."""
    for value in values:
        item = inputs.get(value.get("item_id"))
        if not item:
            continue
        target = _verified_fact(value.get("summary_value"), value.get("summary_source_ids"), item["summary_candidates"], item)
        if target:
            value.update(summary_source_ids=target["source_ids"])
            if target.get("citation_binding"):
                value["summary_citation_binding"] = target["citation_binding"]
        for ref in value.get("references", []):
            if isinstance(ref, dict) and ref.get("role") == item["primary_role"]:
                primary = _verified_fact(ref.get("value"), ref.get("source_ids"), item["primary_candidates"], item)
                if primary:
                    ref.update(primary)


def _parse_json(text: str) -> dict[str, Any]:
    return read_model_json(text)[0]


async def _generate_model_text(
    provider: Any, system_prompt: str, user_prompt: str, *, stage: str,
    trace: Callable[[dict[str, Any]], None] | None,
) -> str:
    """Call the configured model and leave privacy-safe size/latency telemetry."""
    if len(system_prompt) + len(user_prompt) > MODEL_REQUEST_CHARACTERS:
        raise ValueError("MODEL_CONTEXT_EXCEEDS_BUDGET")
    started = time.monotonic()
    response = await provider.generate_text(system_prompt, user_prompt)
    metrics = getattr(response, "metrics", None)
    if trace:
        trace({"kind": "event", "event": "model_call_completed", "details": {
            "workflow": "product_summary", "stage": stage,
            "prompt_characters": len(user_prompt), "response_characters": len(str(response)),
            "wire_prompt_characters": metrics.get("wire_prompt_characters", len(user_prompt)) if isinstance(metrics, dict) else len(user_prompt),
            "wire_system_characters": metrics.get("wire_system_characters", len(system_prompt)) if isinstance(metrics, dict) else len(system_prompt),
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "provider_metrics": metrics if isinstance(metrics, dict) else None,
        }})
    return str(response)


def _normalized_numeric_value(value: str, *, percent: bool) -> str | None:
    pattern = r"(?<!\d)[+-]?\d[\d,]*(?:\.\d+)?\s*%?"
    matches = re.findall(pattern, value or "")
    if percent:
        matches = [match for match in matches if "%" in match]
    if len(matches) != 1:
        return None
    token = matches[0].replace(",", "").replace("%", "").replace(" ", "")
    try:
        from decimal import Decimal
        return format(Decimal(token).normalize(), "f")
    except Exception:
        return None


def _numbers_are_grounded(value: str, evidence: list[dict[str, Any]]) -> bool:
    # CJK labels can touch a numeric value without whitespace (年费率0.15%).
    # Treat ASCII identifier characters as token boundaries, but allow Chinese
    # prose next to a number; this still rejects numeric substrings in codes.
    token_pattern = re.compile(r"(?<![A-Za-z0-9_.])([+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?)(?![A-Za-z0-9_.])")

    def normalize(token: str) -> str:
        token = token.replace(",", "").replace(" ", "")
        percent = token.endswith("%")
        if percent:
            token = token[:-1]
        sign = "-" if token.startswith("-") else ""
        token = token.lstrip("+-")
        # Leading zeroes can be meaningful in fund/security identifiers and dates.
        if re.fullmatch(r"0\d+", token):
            number = sign + token
        else:
            from decimal import Decimal
            try:
                number = format(Decimal(sign + token).normalize(), "f")
            except Exception:
                number = sign + token
        return number + ("%" if percent else "")

    expected = {normalize(match.group(1)) for match in token_pattern.finditer(value or "")}
    if not expected:
        return True
    observed = {
        normalize(match.group(1))
        for source in evidence
        for match in token_pattern.finditer(source.get("raw_text", ""))
    }
    return expected <= observed


def _identifier_tokens(value: str) -> set[str]:
    return set(re.findall(r"(?<!\d)\d{6}(?!\d)", value or ""))


def _compare_code_sets(summary_value: str, reference_value: str) -> str | None:
    summary_codes = _identifier_tokens(summary_value)
    reference_codes = _identifier_tokens(reference_value)
    if not summary_codes or not reference_codes:
        return None
    if summary_codes == reference_codes:
        return "MATCH"
    # A shared master code does not establish that the selected share-class
    # fields are equivalent. Multiple distinct identifiers need role-aware
    # extraction; return unresolved instead of allowing an intersection match.
    if len(summary_codes) == len(reference_codes) == 1:
        return "MISMATCH"
    return None


def _field_evidence_is_substantive(item: dict[str, Any], evidence: list[dict[str, Any]]) -> bool:
    if not evidence:
        return False
    terms = [_norm(term) for term in item.get("terms", []) if _norm(term)]
    evidence_text = _norm(" ".join(source["raw_text"] for source in evidence))
    if terms and not any(term in evidence_text for term in terms):
        cited_ids = {source.get("source_id") for source in evidence}
        cited_layout_terms = {
            neighbor.get("source_id")
            for source in evidence
            for neighbor in source.get("layout_context", [])
            if any(term in _norm(neighbor.get("raw_text", "")) for term in terms)
        }
        if not cited_ids & cited_layout_terms:
            return False
    # Evidence validity comes from the field label and source association, not
    # the character count of the value. Codes and names are often very short.
    return any(source.get("raw_text", "").strip() and _source_heading_info(source) is None
               for source in evidence)


def _can_compare_numeric_deterministically(rule: dict[str, Any], status: str) -> bool:
    """Only compare fixed contractual rates numerically after both sides are evidenced."""
    return (
        status in {"MATCH", "MISMATCH"}
        and rule.get("value_type") == "annual_rate"
        and rule.get("unit_policy") == "annual_rate"
        and rule.get("period_policy") == "applicable_at_summary_compilation"
        and rule.get("comparison_policy") == "normalized_numeric"
    )


async def compare_product_summary(
    *, documents: dict[str, dict[str, Any]], provider: Any,
    source_catalogues: dict[str, dict[str, Any]] | None = None,
    trace: Callable[[dict[str, Any]], None] | None = None,
    progress: Callable[[str, str, str], None] | None = None,
    historical: bool = True,
) -> dict[str, Any]:
    """Associate fields by business meaning, then validate model citations against PDF sources."""
    rule_config, rules = _load_rule_snapshot()
    rule_version = str(rule_config["version"])
    for rule in rules:
        rule.setdefault("key", rule["item_id"])
        rule.setdefault("terms", rule.get("search_terms", []))
    if source_catalogues is None:
        catalogue_values = await asyncio.gather(*(
            run_pdf(build_document_source_catalogue, value["filename"], value["content"], role)
            for role, value in documents.items()
        ))
        catalogues = dict(zip(documents, catalogue_values))
    else:
        missing_catalogues = set(documents) - set(source_catalogues)
        if missing_catalogues:
            raise ValueError("SOURCE_CATALOGUE_MISSING: " + ", ".join(sorted(missing_catalogues)))
        catalogues = {role: source_catalogues[role] for role in documents}
    provider = SourceAliasProvider(provider, catalogues, trace, request_limit=MODEL_REQUEST_CHARACTERS)
    if trace:
        trace({"kind": "artifact", "path": "rules/product_summary_markdown.json", "content": rule_config})
        if source_catalogues is None:
            for role, catalogue in catalogues.items():
                trace({"kind": "artifact", "document_role": role, "path": f"{role}/source_catalogue.json", "content": catalogue})
    if progress and source_catalogues is None:
        progress("source_index", "completed", "章节证据目录已建立")

    items = []
    warnings = []
    for item in rules:
        primary = _source_candidates(catalogues["product_summary"], item) if "product_summary" in catalogues else []
        role = item["primary_source"]["role"]
        primary_reference = _source_candidates(catalogues[role], item) if role in catalogues else []
        summary_meta = documents.get("product_summary", {}).get("metadata", {})
        report_meta = documents.get(role, {}).get("metadata", {})
        summary_interval = _metadata_date_interval(summary_meta, "compiled_date_interval", "compiled_date")
        report_interval = _metadata_date_interval(report_meta, "sent_date_interval", "sent_date")
        primary_source_eligibility = "ELIGIBLE"
        eligibility_reason = None
        if role in {"periodic_report", "annual_report"} and historical and (not summary_interval or not report_interval):
            primary_source_eligibility = "DATE_UNCONFIRMED"
            eligibility_reason = "概要编制日期或定期报告送出日期无法确认。"
            primary_reference = []
            warnings.append(f"{role} 日期无法确认，未作为概要历史口径依据。")
        elif role in {"periodic_report", "annual_report"} and historical and report_interval[0] > summary_interval[1]:
            primary_source_eligibility = "AFTER_TARGET_DATE"
            eligibility_reason = "定期报告送出日晚于概要编制日期。"
            primary_reference = []
            warnings.append(f"{role} 送出日晚于概要编制日，历史口径下不作为有效来源。")
        fallback = item.get("fallback_source") or {}
        fallback_role = fallback.get("role")
        fallback_candidates = _source_candidates(catalogues[fallback_role], item) if fallback_role in catalogues else []
        required_subtypes = item.get("primary_source", {}).get("subtypes", [])
        report_subtype = documents.get(role, {}).get("metadata", {}).get("report_subtype")
        primary_not_applicable = bool(required_subtypes and report_subtype and report_subtype not in required_subtypes)
        if primary_not_applicable:
            primary_reference = []
        item_payload = {
            "item_id": item["item_id"], "key": item["key"], "label": item["label"],
            "module_id": item["module_id"],
            "excel_row": item.get("excel_row"), "excel_source_cell": item.get("excel_source_cell"), "note": item.get("note"),
            "meaning": item.get("meaning"),
            "target_section_path": item.get("target_section_path", []),
            "business_rule_markdown": item.get("meaning"),
            "value_type": item.get("value_type"),
            "period_policy": item.get("period_policy"),
            "unit_policy": item.get("unit_policy"),
            "comparison_policy": item.get("comparison_policy"),
            "amount_period": item.get("amount_period"),
            "terms": item.get("terms", []),
            "section_hints": item.get("section_hints", []),
            "section_hints_by_role": item.get("section_hints_by_role", {}),
            "primary_role": role,
            "fallback_role": fallback_role,
            "fallback_when": item.get("fallback_when", []),
            "primary_source_eligibility": primary_source_eligibility,
            "primary_source_eligibility_reason": eligibility_reason,
            "primary_lookup_status": primary_source_eligibility if primary_source_eligibility != "ELIGIBLE" else "CANDIDATES_FOUND" if primary_reference else "NO_CANDIDATES_UNCONFIRMED",
            "primary_not_applicable": primary_not_applicable,
            "summary_candidates": primary,
            "primary_candidates": primary_reference,
            "fallback_candidates": fallback_candidates,
        }
        for side, source_role in (("summary", "product_summary"), ("primary", role), ("fallback", fallback_role)):
            catalogue = catalogues.get(source_role, {})
            plan = _context_plan(catalogue, item_payload[f"{side}_candidates"], item=item_payload)
            item_payload[f"{side}_context_plan"] = plan
            by_id = {source["source_id"]: source for page in catalogue.get("pages", []) for source in page.get("sources", [])}
            item_payload[f"{side}_candidates"] = [by_id[sid] for sid in plan["source_ids"] if sid in by_id]
        items.append(item_payload)
    if trace:
        trace({"kind": "artifact", "path": "comparison/chapter_plan.json", "content": [
            {"item_id": item["item_id"], "label": item["label"], "module_id": item["module_id"],
             "target_section_path": item["target_section_path"],
             "sources": {side: {"role": role,
                                "chapters": [{key: section[key] for key in ("section_id", "title", "path")}
                                             for section in _prescribed_sections(catalogues.get(role, {}), item)],
                                "planned_batches": len(item[f"{side}_context_plan"]["batches"]),
                                "source_count": item[f"{side}_context_plan"]["source_count"]}
                         for side, role in (("summary", "product_summary"), ("primary", item["primary_role"]),
                                            ("fallback", item["fallback_role"]))}}
            for item in items]})
    if progress:
        progress("primary_lookup", "running", "初步定位完成，正在核实候选未命中的指标")

    system = (
        "你是基金产品资料核对助手。只根据给定原文证据判断字段含义和一致性。"
        "target_section_path是指标的章节定位路径，label才是待提取业务字段；不得把章节名当成核对值。同章多个指标共享原文，但分别确认各自标签、数值和口径。"
        "若输入含context_pool，从sources与logical_units/tables共享池读取该指标allowed_*_ids指定的内容；每项只能引用其allowed_summary_source_ids、allowed_primary_source_ids或allowed_fallback_source_ids列出的原始source_id，不能借用同批其他指标的范围。没有共享池时读取该项candidates和logical_units。"
        "必须逐项返回所有 item_id，不能使用Excel行号识别指标。先只检查主要定期报告证据。只有相关完整章节已检查且明确确认字段未披露时才返回 SOURCE_MISSING；候选未命中、证据不足、输出漏项或引用失败都必须返回 UNRESOLVED。主要来源存在有效证据时必须以其比较，即使结果不一致也不能改用补充文件。只能引用输入中存在的 source_id，不得编造文字、来源或坐标。"
        "value只能填写被引用原文直接支持的字段值或必要的规范化值，不能把推导的日期、期间或解释拼进value；解释放在reason。每个数值、日期和代码都必须能在对应引用原文中完整找到。"
        "相同数字若业务身份不同不能认定一致；费率、金额、代码、日期需保持字段角色。role_aware_identifier以概要实际列出的份额及代码角色为核对范围，允许同义标签：同一基金、同一份额范围明确且无角色冲突时，通用基金代码可对应报告主代码或相应交易代码，不因标签不同就UNRESOLVED。概要列出前端/后端等多个目标角色时须逐项覆盖；仅对上主代码不能推断整组代码一致，也不能把别的份额代码补成缺失编号。"
        "每项都提供业务含义、值类型、期间、单位和比较策略，必须按该规则核对。current_identity表示核对当前产品身份；applicable_at_summary_compilation要求依据在概要编制时已适用；same_period要求两侧期间相同。annual_rate只比较年费率，不得拿费率与实际费用金额比较；same_currency_amount_and_period要求币种、期间和费用性质都一致，任何一项不明确即UNRESOLVED。"
        "单位策略要求保留原单位或按规则归一，不得因为数字碰巧相同就忽略元/万元或比例/金额差别。"
        "金额项目另须返回comparison_basis对象，same_period、same_nature、same_unit分别表示期间、费用性质（如年度预估或本期实际发生）、币种及单位可比性，仅在原文或给定日期有依据时填true；不同填false，未知填null。三项不是全部true时，金额比较状态只能为UNRESOLVED，不得填写MATCH或MISMATCH。非金额项目可不填。"
        "annual_rate按约定年费率百分比核对：可用主报告与概要的费用项目、适用对象相同，百分比相等且两侧未提示变更，可判披露值MATCH，不额外要求报告期末等于概要编制日。固定比例/固定费率与按净资产约定年费率计提可以对应，基数每日变化不代表年费率浮动。"
        "核对的是两份文件披露的字段内容，不是证明报告期后未发生任何变化。身份及字段角色明确、两侧披露值相同且没有变更或离任等冲突证据时，可判披露内容MATCH；文件日期不同本身不构成待确认理由。任职起始日等历史事实只需确认同一人员、同一基金及同一事件，不能要求报告期覆盖概要编制日。"
        "若时点敏感字段的两侧值不同，且概要编制日与定期报告截止日不同，必须确认目标信息的生效日或报告期适用范围。主报告较早、原文未证明新旧口径针对同一时点时，不能仅因两侧值不同判MISMATCH，应返回UNRESOLVED并说明缺少的时点依据；只有确认同一适用时点或存在明确互斥披露时才可判差异。费用金额仍须按same_period和费用性质判断，不得因数值相同忽略期间或口径。"
        "只有字段在概要和至少一个指定来源中都能找到时才比较。字段在一侧或两侧均未披露时，对该来源返回 SOURCE_MISSING，不得据此判差异；证据存在但无法确认或引用时返回 UNRESOLVED。primary_not_applicable为true时，禁止比较该定期报告，只提取概要侧目标事实并将主来源标为SOURCE_MISSING/NOT_APPLICABLE。"
        "来源条目包含source_id、page、bbox、raw_text。logical_units或table_context中的Markdown/HTML是本项目经完整性验证的PDF几何恢复结构，仅供理解完整表格行列与跨页续接关系；引用仍必须使用原始source_id，不能把unit_id或Markdown文字作为来源ID。必须结合原文与行列关系绑定字段和值，禁止错配同一行其他字段。"
        "字段标签和值分开时，必须同时引用包含标签与对应值的原始source_id。代码字段按规则匹配实际基金份额/交易代码，不把一组主代码和前后端代码拼成单一值。"
        "文本可做语义等价判断，但保留否定、比例、限制条件和适用对象。MISMATCH必须有主要来源的明确反向陈述或互斥事实作为证据；来源只披露概括内容、没有复述目标中的附加限定/数值/条件，不足以判MISMATCH，应返回UNRESOLVED并说明未被主来源覆盖。不得把‘没有披露’推断成‘披露相反’。"
        "严格按 JSON 返回，不要输出 Markdown。"
    )
    document_dates = {role: {key: doc.get("metadata", {}).get(key) for key in (
        "compiled_date", "compiled_date_interval", "compiled_date_quote", "period_end", "period_end_interval",
        "period_end_quote", "sent_date", "sent_date_interval", "sent_date_quote", "date_interval", "date_label", "report_subtype",
    ) if doc.get("metadata", {}).get(key) is not None} for role, doc in documents.items()}
    system += "\n程序预检确认的文档时点如下。缺失日期不得推断为相同，所选报告可用不代表所有时点敏感字段都能跨期判差异：\n" + json.dumps(document_dates, ensure_ascii=False)
    if trace:
        trace({"kind": "artifact", "path": "comparison/model_system_prompt.txt", "content": system})
    schema = {
        "items": [{"item_id": "stable-rule-id", "summary_value": "原文值或摘要", "summary_source_ids": [], "references": [{"role": "primary role", "status": "MATCH|MISMATCH|UNRESOLVED|SOURCE_MISSING", "value": "原文值或摘要", "source_ids": [], "comparison_basis": {"same_period": None, "same_nature": None, "same_unit": None}, "reason": "简短依据"}], "reason": "简短依据"}]
    }
    # Extract every available target fact even when lexical matching found no
    # primary-reference candidate. Source routing happens afterward; otherwise a
    # missing report keyword can prevent the target side from being captured.
    eligible_items = [
        item for item in items
        if item["summary_candidates"]
        and (item["primary_source_eligibility"] == "ELIGIBLE" or item["primary_not_applicable"])
    ]
    groups = module_groups(eligible_items)
    row_to_item_id = {int(item["excel_row"]): item["item_id"] for item in items if item.get("excel_row") is not None}
    known_item_ids = {item["item_id"] for item in items}
    started = time.monotonic()

    async def analyze_group(group_id: str, group_items: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]], str | None]:
        if progress:
            progress(group_id, "running", "正在核对" + group_id.replace("_", " "))
        found_by_id: dict[str, dict[str, Any]] = {}
        batch_errors = []
        rounds = max((len(item[f"{side}_context_plan"]["batches"])
                      for item in group_items for side in ("summary", "primary")), default=0)
        for round_index in range(rounds):
            requests = []
            for item in group_items:
                previous = found_by_id.get(item["item_id"], {})
                target = _verified_fact(previous.get("summary_value"), previous.get("summary_source_ids"),
                                        item["summary_candidates"], item)
                reference = next((ref for ref in previous.get("references", [])
                                  if ref.get("role") == item["primary_role"]), {})
                primary_fact = _verified_fact(reference.get("value"), reference.get("source_ids"),
                                              item["primary_candidates"], item)
                if target and (primary_fact or item["primary_not_applicable"]):
                    continue
                payload = {key: item[key] for key in (
                    "item_id", "key", "label", "target_section_path", "business_rule_markdown", "value_type", "period_policy",
                    "unit_policy", "comparison_policy", "amount_period", "terms", "primary_role", "primary_not_applicable",
                )}
                payload.update({"verified_target": target, "verified_primary_reference": primary_fact,
                                "fallback_candidates": [], "context_round": round_index + 1})
                has_sources = False
                for side, locked in (("summary", target), ("primary", primary_fact)):
                    plan = item[f"{side}_context_plan"]
                    batch = plan["batches"][round_index] if not locked and round_index < len(plan["batches"]) else {}
                    payload[f"{side}_candidates"] = batch.get("candidates", [])
                    payload[f"{side}_logical_units"] = batch.get("logical_units", [])
                    payload[f"{side}_table_context"] = batch.get("table_context", [])
                    payload[f"{side}_coverage"] = {"batch": round_index + 1, "batch_count": len(plan["batches"]),
                                                     "coverage_complete": plan["coverage_complete"],
                                                     "skipped_unit_count": len(plan["skipped_units"])}
                    has_sources = has_sources or bool(batch.get("candidates"))
                    item.setdefault(f"{side}_examined_source_ids", set()).update(source["source_id"] for source in batch.get("candidates", []))
                if has_sources:
                    requests.append(payload)
            for batch_index, batch in enumerate(_payload_batches(requests)):
                shared = _shared_context(batch)
                prompt = (
                    f"按当前主题逐项处理本组 {len(batch)} 个核对项，必须完整返回每个item_id。"
                    "当前包包含完整章节或显式逻辑单元；context_round及coverage说明分包范围，未提供单元不代表未披露。"
                    "verified_target和verified_primary_reference是已验证锁定事实，必须原样保留其值和source_id，不得重提或改写。"
                    "某项primary_candidates为空时，仍须单独提取概要事实；无已验证主来源时返回UNRESOLVED。不得使用补充来源。"
                    "只比较本组字段，不得因当前包未命中返回SOURCE_MISSING。\nJSON结构：\n"
                    + json.dumps(schema, ensure_ascii=False) + "\n字段与证据包：\n"
                    + json.dumps(shared if "context_pool" in shared else batch, ensure_ascii=False)
                )
                stage = f"{group_id}/round_{round_index + 1}/batch_{batch_index + 1}"
                if trace:
                    trace({"kind": "artifact", "path": f"comparison/{stage}/model_request.json",
                           "content": {"rules_version": rule_version, "prompt": prompt}})
                last_error = None
                for attempt in range(2):
                    try:
                        response = await _generate_model_text(provider, system, prompt, stage=stage, trace=trace)
                        if trace:
                            trace({"kind": "artifact", "path": f"comparison/{stage}/model_response.txt", "content": response})
                        for value in _parse_json(response).get("items", []):
                            found = _normalize_model_item_id(value, row_to_item_id) if isinstance(value, dict) else None
                            request = next((request for request in batch if found and request["item_id"] == found["item_id"]), None)
                            if not request:
                                continue
                            source_item = next(item for item in group_items if item["item_id"] == found["item_id"])
                            target = request["verified_target"]
                            if target:
                                found.update(summary_value=target["value"], summary_source_ids=target["source_ids"])
                            elif any(source_id in {source["source_id"] for source in source_item["summary_candidates"]}
                                     and source_id not in {source["source_id"] for source in request["summary_candidates"]}
                                     for source_id in found.get("summary_source_ids", [])):
                                found["summary_source_ids"] = []
                            primary_fact = request["verified_primary_reference"]
                            if primary_fact:
                                refs = [ref for ref in found.get("references", []) if isinstance(ref, dict)]
                                ref = next((ref for ref in refs if ref.get("role") == request["primary_role"]), {})
                                found["references"] = [{**ref, "role": request["primary_role"], **primary_fact}]
                            else:
                                allowed = {source["source_id"] for source in request["primary_candidates"]}
                                known = {source["source_id"] for source in source_item["primary_candidates"]}
                                for ref in found.get("references", []):
                                    if isinstance(ref, dict) and any(source_id in known and source_id not in allowed for source_id in ref.get("source_ids", [])):
                                        ref.update(status="UNRESOLVED", source_ids=[])
                            previous = found_by_id.get(found["item_id"], {})
                            found_by_id[found["item_id"]] = {**previous, **found}
                        last_error = None
                        break
                    except Exception as exc:
                        last_error = f"{type(exc).__name__}: {exc}"
                        if trace:
                            trace({"kind": "event", "event": "summary_compare_group_retry",
                                   "details": {"group": group_id, "attempt": attempt + 1, "error": last_error}})
                if last_error:
                    batch_errors.append(last_error)
        if progress:
            progress(group_id, "failed" if batch_errors else "completed",
                     "主题分包调用失败" if batch_errors else "主题分包检查完成")
        return group_id, list(found_by_id.values()), "; ".join(batch_errors) or None

    group_outcomes = await asyncio.gather(*(analyze_group(group_id, group_items) for group_id, group_items in groups.items()))
    raw_model_items = [item for _, group_items, _ in group_outcomes for item in group_items]
    failed_groups = [{"group": group_id, "error": error} for group_id, _, error in group_outcomes if error]
    if failed_groups and not raw_model_items:
        raise RuntimeError("SUMMARY_MODEL_FAILED: " + "; ".join(item["error"] for item in failed_groups))
    valid_sources = {source["source_id"]: source for catalogue in catalogues.values() for page in catalogue["pages"] for source in page["sources"]}
    rule_by_id = {rule["item_id"]: rule for rule in rules}
    item_by_id = {item["item_id"]: item for item in items}
    _bind_model_fact_sources(raw_model_items, item_by_id)

    # Recover invalid or missing citations once, using only the target and primary
    # candidates. This repairs split table label/value citations without allowing
    # the recovery call to declare a source absent or inspect a fallback document.
    raw_by_id = {value["item_id"]: value for value in raw_model_items if value.get("item_id") in known_item_ids}
    recovery_items = []
    for item_id, item in item_by_id.items():
        if not item["summary_candidates"] or not item["primary_candidates"]:
            continue
        model_item = raw_by_id.get(item_id, {})
        rule = rule_by_id[item_id]
        summary_ids = model_item.get("summary_source_ids") if isinstance(model_item.get("summary_source_ids"), list) else []
        summary_evidence = [valid_sources[source_id] for source_id in summary_ids if source_id in valid_sources and source_id in {source["source_id"] for source in item["summary_candidates"]}]
        summary_value = str(model_item.get("summary_value") or "")
        summary_valid = (len(summary_evidence) == len(set(summary_ids)) and bool(summary_evidence)
                         and _numbers_are_grounded(summary_value, summary_evidence)
                         and _field_evidence_is_substantive(rule, summary_evidence))
        refs = {ref.get("role"): ref for ref in model_item.get("references", []) if isinstance(ref, dict)}
        primary_ref = refs.get(item["primary_role"], {})
        primary_ids = primary_ref.get("source_ids") if isinstance(primary_ref.get("source_ids"), list) else []
        primary_evidence = [valid_sources[source_id] for source_id in primary_ids if source_id in valid_sources and source_id in {source["source_id"] for source in item["primary_candidates"]}]
        primary_value = str(primary_ref.get("value") or "")
        primary_valid = (len(primary_evidence) == len(set(primary_ids)) and bool(primary_evidence)
                         and _numbers_are_grounded(primary_value, primary_evidence)
                         and _field_evidence_is_substantive(rule, primary_evidence))
        if not summary_valid or not primary_valid:
            preferred_summary_ids = summary_ids if not summary_valid else []
            preferred_primary_ids = primary_ids if not primary_valid else []
            recovery_items.append({
                key: item[key] for key in ("item_id", "key", "label", "primary_role", "business_rule_markdown", "terms")
            } | {
                "failed_sides": [side for side, valid in (("target", summary_valid), ("primary_reference", primary_valid)) if not valid],
                "summary_candidates": _recovery_candidates(item, "summary", preferred_summary_ids) if not summary_valid else [],
                "primary_candidates": _recovery_candidates(item, "primary", preferred_primary_ids) if not primary_valid else [],
                "summary_coverage": item.get("summary_excerpt_coverage"),
                "primary_coverage": item.get("primary_excerpt_coverage"),
                "summary_logical_units": item.get("summary_repair_units", []),
                "primary_logical_units": item.get("primary_repair_units", []),
                "verified_target": {"value": summary_value, "source_ids": summary_ids} if summary_valid else None,
                "verified_primary_reference": {"value": primary_value, "source_ids": primary_ids,
                                                "status": primary_ref.get("status")} if primary_valid else None,
            })
    for recovery_index, recovery_batch in enumerate(_payload_batches(recovery_items), start=1):
        recovery_prompt = (
            "只恢复failed_sides列出的失败侧字段证据。verified_target或verified_primary_reference存在时视为锁定事实，必须原样保留其中值和source_id，不能重新提取或改写。候选包含原文、bbox和完整逻辑单元；标签和值可能分处不同单元格或不同行，必须引用标签与对应值的原始source_id，不能选择相邻的其他指标。"
            "恢复后有有效事实即可返回；事实无法确认则保持该侧空值，并将比较状态设为UNRESOLVED。禁止返回SOURCE_MISSING或确认未披露。不得查看补充招募说明书。"
            "金额项目仍须给出comparison_basis的same_period、same_nature、same_unit。严格返回JSON：{\"items\":[{\"item_id\":\"stable-rule-id\",\"summary_value\":\"\",\"summary_source_ids\":[],\"primary_value\":\"\",\"primary_source_ids\":[],\"comparison_status\":\"MATCH|MISMATCH|UNRESOLVED\",\"comparison_basis\":{},\"reason\":\"\"}]}\n"
            + json.dumps(_shared_context(recovery_batch), ensure_ascii=False)
        )
        if trace:
            trace({"kind": "artifact", "path": f"comparison/evidence_recovery/batch_{recovery_index}/model_request.json", "content": {"prompt": recovery_prompt, "item_count": len(recovery_batch)}})
        try:
            recovery_raw = await _generate_model_text(
                provider, system, recovery_prompt, stage="evidence_recovery", trace=trace,
            )
            if trace:
                trace({"kind": "artifact", "path": f"comparison/evidence_recovery/batch_{recovery_index}/model_response.txt", "content": recovery_raw})
            recovered = _parse_json(recovery_raw).get("items", [])
            recovered_by_id = {normalized["item_id"]: normalized for value in recovered if isinstance(value, dict)
                               if (normalized := _normalize_model_item_id(value, row_to_item_id)) is not None
                               and normalized.get("item_id") in known_item_ids}
            recovery_input_by_id = {item["item_id"]: item for item in recovery_batch}
            for item_id in {item["item_id"] for item in recovery_batch}:
                found = recovered_by_id.get(item_id)
                item = item_by_id[item_id]
                rule = rule_by_id[item_id]
                recovery_input = recovery_input_by_id[item_id]
                if not found:
                    continue
                locked_target = recovery_input.get("verified_target")
                locked_primary = recovery_input.get("verified_primary_reference")
                summary_ids = (locked_target.get("source_ids") if locked_target else
                               found.get("summary_source_ids") if isinstance(found.get("summary_source_ids"), list) else [])
                summary_allowed = set(locked_target["source_ids"]) if locked_target else {source["source_id"] for source in recovery_input["summary_candidates"]}
                summary_evidence = [valid_sources[source_id] for source_id in summary_ids if source_id in valid_sources and source_id in summary_allowed and valid_sources[source_id]["document_role"] == "product_summary"]
                summary_value = str((locked_target or {}).get("value") or found.get("summary_value") or "")
                primary_ids = (locked_primary.get("source_ids") if locked_primary else
                               found.get("primary_source_ids") if isinstance(found.get("primary_source_ids"), list) else [])
                primary_allowed = set(locked_primary["source_ids"]) if locked_primary else {source["source_id"] for source in recovery_input["primary_candidates"]}
                primary_evidence = [valid_sources[source_id] for source_id in primary_ids if source_id in valid_sources and source_id in primary_allowed and valid_sources[source_id]["document_role"] == item["primary_role"]]
                primary_value = str((locked_primary or {}).get("value") or found.get("primary_value") or "")
                target_valid = _verified_fact(summary_value, summary_ids, summary_evidence, rule)
                reference_valid = _verified_fact(primary_value, primary_ids, primary_evidence, rule)
                if not target_valid and not reference_valid:
                    continue
                previous = next((value for value in raw_model_items if value.get("item_id") == item_id), {})
                if not target_valid or not reference_valid:
                    updated = {**previous, "item_id": item_id}
                    if target_valid:
                        updated.update(summary_value=summary_value, summary_source_ids=summary_ids)
                    if reference_valid:
                        updated["references"] = [{"role": item["primary_role"], "status": "UNRESOLVED",
                                                  "value": primary_value, "source_ids": primary_ids}]
                    raw_model_items = [value for value in raw_model_items if value.get("item_id") != item_id]
                    raw_model_items.append(updated)
                    continue
                status = found.get("comparison_status")
                status = status if status in {"MATCH", "MISMATCH", "UNRESOLVED"} else "UNRESOLVED"
                if any(locked and found.get(key) and _norm(str(found[key])) != _norm(locked["value"])
                       for locked, key in ((locked_target, "summary_value"), (locked_primary, "primary_value"))):
                    status = "UNRESOLVED"
                    found["reason"] = "局部恢复尝试改写已验证事实，比较结论未采用。"
                raw_model_items = [value for value in raw_model_items if value.get("item_id") != item_id]
                raw_model_items.append({
                    "item_id": item_id, "summary_value": summary_value, "summary_source_ids": summary_ids,
                    "references": [{"role": item["primary_role"], "status": status, "value": primary_value,
                                    "source_ids": primary_ids, "comparison_basis": found.get("comparison_basis"),
                                    "reason": str(found.get("reason") or "经局部证据恢复确认。" )}],
                    "reason": str(found.get("reason") or "经局部证据恢复确认。"),
                })
        except Exception as exc:
            failed_groups.append({"group": "evidence_recovery", "error": f"{type(exc).__name__}: {exc}"})
            if trace:
                trace({"kind": "event", "event": "summary_evidence_recovery_failed", "details": {"error": f"{type(exc).__name__}: {exc}"}})
    # Lexical misses are not absence. Route only unresolved indicators to complete
    # logical sections, then inspect those sections with explicit coverage records.
    _bind_model_fact_sources(raw_model_items, item_by_id)
    raw_model_ids = {value["item_id"] for value in raw_model_items if value.get("item_id") in known_item_ids}
    source_missing_ids = {
        value["item_id"]
        for value in raw_model_items
        if value.get("item_id") in known_item_ids
        and any(ref.get("role") == "periodic_report" and ref.get("status") == "SOURCE_MISSING"
                for ref in value.get("references", []) if isinstance(ref, dict))
    }
    unresolved_primary_ids = set(known_item_ids) - raw_model_ids
    for value in raw_model_items:
        item_id = value.get("item_id")
        if item_id not in known_item_ids:
            continue
        rule = rule_by_id[item_id]
        refs = [ref for ref in value.get("references", [])
                if isinstance(ref, dict) and ref.get("role") == rule["primary_source"]["role"]]
        ref = refs[0] if refs else {}
        source_ids = ref.get("source_ids") if isinstance(ref.get("source_ids"), list) else []
        allowed_ids = {source["source_id"] for source in item_by_id[item_id]["primary_candidates"]}
        evidence = [valid_sources[source_id] for source_id in source_ids
                    if source_id in valid_sources and source_id in allowed_ids]
        valid_fact = (ref.get("status") in {"MATCH", "MISMATCH", "UNRESOLVED"}
                      and len(evidence) == len(set(source_ids)) and bool(evidence)
                      and _numbers_are_grounded(str(ref.get("value") or ""), evidence)
                      and _field_evidence_is_substantive(rule, evidence))
        if not valid_fact:
            unresolved_primary_ids.add(item_id)
    missing_primary_items = [item for item in items if item["summary_candidates"]
                             and item["primary_source_eligibility"] == "ELIGIBLE"
                             and not item["primary_not_applicable"]
                             and item["item_id"] in (unresolved_primary_ids | source_missing_ids)]
    if missing_primary_items:
        report_catalogue = catalogues.get("periodic_report", {})
        section_directory = _chapter_directory(report_catalogue)
        section_by_id = {section["section_id"]: section for section in section_directory}
        report_sources = {source["source_id"]: source for page in report_catalogue.get("pages", [])
                          for source in page.get("sources", [])}
        routed = {item["item_id"]: [] for item in missing_primary_items}
        route_requests = []
        for item in missing_primary_items:
            prescribed = _prescribed_sections(report_catalogue, item, first_only=False)
            if prescribed:
                routed[item["item_id"]] = [section["section_id"] for section in prescribed]
                continue
            previous = next((value for value in raw_model_items if value.get("item_id") == item["item_id"]), {})
            target = _verified_fact(previous.get("summary_value"), previous.get("summary_source_ids"),
                                    item["summary_candidates"], item)
            route_requests.append({"item_id": item["item_id"], "label": item["label"],
                                   "business_rule": item["business_rule_markdown"],
                                   "section_hints": item["section_hints_by_role"].get(item["primary_role"], []),
                                   "verified_target": target})
        directory_pages = _payload_batches([{key: value for key, value in section.items() if key != "source_ids"}
                                           for section in section_directory], budget=18_000)
        for directory_index, directory in enumerate(directory_pages):
            for requests in _payload_batches(route_requests, budget=18_000):
                route_prompt = (
                    "你负责为未定位的核对项选择定期报告中的完整章节。根据指标含义、预设章节提示和章节标题选择可能披露该指标的section_id，最多8个。"
                    "本次仅提供目录的一部分，未出现不代表未披露；不能输出目录外ID。只做路由，不提取事实、不比较、不确认缺失。"
                    "没有把握时返回空数组。严格返回JSON：{\"items\":[{\"item_id\":\"stable-rule-id\",\"section_ids\":[],\"reason\":\"\"}]}\n"
                    + json.dumps({"items": requests, "sections": directory, "directory_batch": directory_index + 1,
                                  "directory_batch_count": len(directory_pages)}, ensure_ascii=False)
                )
                try:
                    if trace:
                        trace({"kind": "artifact", "path": f"comparison/primary_section_routing/{directory_index + 1}/model_request.json",
                               "content": {"prompt": route_prompt}})
                    response = await _generate_model_text(provider, system, route_prompt, stage="primary_section_routing", trace=trace)
                    for route in _parse_json(response).get("items", []):
                        found = _normalize_model_item_id(route, row_to_item_id) if isinstance(route, dict) else None
                        if not found or found.get("item_id") not in {request["item_id"] for request in requests}:
                            continue
                        ids = route.get("section_ids") if isinstance(route.get("section_ids"), list) else []
                        allowed = {section["section_id"] for section in directory}
                        routed[found["item_id"]].extend(sid for sid in ids if isinstance(sid, str) and sid in allowed)
                except Exception as exc:
                    failed_groups.append({"group": "primary_section_routing", "error": f"{type(exc).__name__}: {exc}"})
        searches = {}
        for item in missing_primary_items:
            existing_ids = {source.get("section_id") for source in item["primary_candidates"]}
            selected = list(dict.fromkeys([*routed[item["item_id"]],
                                           *(section["section_id"] for section in section_directory if section["section_id"] in existing_ids)]))
            coverage = {"selected_section_ids": selected, "selected_sections": [section_by_id[sid]["path"] for sid in selected],
                        "examined_source_ids": [], "unexamined_units": [], "coverage_complete": False,
                        "requests": 0, "request_characters": 0}
            item["primary_routing"] = coverage
            packets = []
            for sid in selected:
                plan = _context_plan(report_catalogue, [report_sources[source_id] for source_id in section_by_id[sid]["source_ids"]], item=item)
                coverage["unexamined_units"].extend(plan["skipped_units"])
                known = {source["source_id"] for source in item["primary_candidates"]}
                item["primary_candidates"].extend(report_sources[source_id] for source_id in plan["source_ids"] if source_id not in known)
                for batch_index, batch in enumerate(plan["batches"]):
                    packets.append({"section_id": sid, "section_path": section_by_id[sid]["path"],
                                    "section_batch": batch_index + 1, "section_batch_count": len(plan["batches"]), **batch})
            searches[item["item_id"]] = {"item": item, "packets": packets, "absent": 0, "found": False, "failed": False}
        rounds = max((len(search["packets"]) for search in searches.values()), default=0)
        for round_index in range(rounds):
            requests = []
            for item_id, search in searches.items():
                if search["found"] or round_index >= len(search["packets"]):
                    continue
                item = search["item"]
                previous = next((value for value in raw_model_items if value.get("item_id") == item_id), {})
                target = _verified_fact(previous.get("summary_value"), previous.get("summary_source_ids"), item["summary_candidates"], item)
                packet = search["packets"][round_index]
                requests.append({"item_id": item_id, "label": item["label"], "business_rule": item["business_rule_markdown"],
                                 "verified_target": target, "summary_candidates": [] if target else _recovery_candidates(item, "summary"),
                                 "selected_sections": [packet["section_path"]], "primary_candidates": packet["candidates"],
                                 "logical_units": packet["logical_units"], "table_context": packet.get("table_context", []),
                                 "section_batch": packet["section_batch"], "section_batch_count": packet["section_batch_count"],
                                 "coverage_complete": True})
            for request_batch in _payload_batches(requests):
                lookup_prompt = (
                    "仅检查每项列出的完整章节原文或完整逻辑单元。verified_target存在时是锁定事实，禁止重新提取或修改，summary_candidates为空是正常情况。"
                    "找到字段返回FOUND；当前包未披露返回NOT_FOUND_IN_SCOPE；字段身份或证据不清返回UNRESOLVED。"
                    "分包未命中不代表整章或全文未披露，程序会在所有分包检查后汇总覆盖。标签和值可跨页，必须引用原始source_id。"
                    "primary_value只能写引用直接支持的字段值；期间或适用性说明写reason。金额项目仍须给出comparison_basis的same_period、same_nature、same_unit。不要查看招募说明书。严格返回JSON："
                    "{\"items\":[{\"item_id\":\"stable-rule-id\",\"summary_value\":\"\",\"summary_source_ids\":[],\"primary_status\":\"FOUND|NOT_FOUND_IN_SCOPE|UNRESOLVED\",\"primary_value\":\"\",\"primary_source_ids\":[],\"comparison_status\":\"MATCH|MISMATCH|UNRESOLVED\",\"comparison_basis\":{},\"reason\":\"\"}]}\n"
                    + json.dumps(_shared_context(request_batch), ensure_ascii=False)
                )
                try:
                    if trace:
                        trace({"kind": "artifact", "path": f"comparison/primary_section_lookup/round_{round_index + 1}/model_request.json",
                               "content": {"prompt": lookup_prompt}})
                    response = await _generate_model_text(provider, system, lookup_prompt, stage="primary_section_lookup", trace=trace)
                    found_by_id = {found["item_id"]: found for value in _parse_json(response).get("items", []) if isinstance(value, dict)
                                   if (found := _normalize_model_item_id(value, row_to_item_id)) is not None}
                    for request in request_batch:
                        item_id = request["item_id"]
                        search = searches[item_id]
                        item = search["item"]
                        coverage = item["primary_routing"]
                        coverage["requests"] += 1
                        coverage["request_characters"] += len(lookup_prompt)
                        coverage["examined_source_ids"].extend(source["source_id"] for source in request["primary_candidates"])
                        found = found_by_id.get(item_id, {})
                        previous = next((value for value in raw_model_items if value.get("item_id") == item_id), {})
                        target = request["verified_target"] or _verified_fact(found.get("summary_value"), found.get("summary_source_ids"),
                                                                            request["summary_candidates"], item)
                        primary = _verified_fact(found.get("primary_value"), found.get("primary_source_ids"),
                                                 request["primary_candidates"], item)
                        if found.get("primary_status") in {"CONFIRMED_ABSENT", "NOT_FOUND_IN_SCOPE"} and not found.get("primary_source_ids"):
                            search["absent"] += 1
                        if found.get("primary_status") == "FOUND" and primary:
                            search["found"] = True
                            status = found.get("comparison_status")
                            reference = {"role": item["primary_role"], "status": status if status in {"MATCH", "MISMATCH"} else "UNRESOLVED",
                                         **primary, "comparison_basis": found.get("comparison_basis"), "reason": found.get("reason", "")}
                        else:
                            reference = next((ref for ref in previous.get("references", []) if ref.get("role") == item["primary_role"]),
                                             {"role": item["primary_role"], "status": "UNRESOLVED", "value": "", "source_ids": []})
                        updated = {**previous, "item_id": item_id, "primary_routing": coverage,
                                   "primary_lookup_status_override": "FOUND" if search["found"] else "UNRESOLVED",
                                   "references": [reference], "reason": str(found.get("reason") or previous.get("reason") or "章节检查未能确认字段。")}
                        if target:
                            updated.update(summary_value=target["value"], summary_source_ids=target["source_ids"])
                        raw_model_items = [value for value in raw_model_items if value.get("item_id") != item_id]
                        raw_model_items.append(updated)
                except Exception as exc:
                    failed_groups.append({"group": "primary_section_lookup", "error": f"{type(exc).__name__}: {exc}"})
                    for request in request_batch:
                        searches[request["item_id"]]["failed"] = True
        for item_id, search in searches.items():
            item = search["item"]
            coverage = item["primary_routing"]
            coverage["examined_source_ids"] = list(dict.fromkeys(coverage["examined_source_ids"]))
            coverage["coverage_complete"] = (bool(search["packets"]) and not coverage["unexamined_units"]
                                              and not search["failed"] and coverage["requests"] == len(search["packets"]))
            if search["found"]:
                continue
            previous = next((value for value in raw_model_items if value.get("item_id") == item_id), {})
            absent = coverage["coverage_complete"] and search["absent"] == len(search["packets"])
            reason_code = (coverage["unexamined_units"][0]["reason"] if coverage["unexamined_units"] else
                           "NO_CONFIDENT_SECTION_ROUTE" if not coverage["selected_section_ids"] else "ROUTED_SECTION_UNRESOLVED")
            updated = {**previous, "item_id": item_id, "primary_routing": coverage,
                       "primary_lookup_status_override": "CONFIRMED_ABSENT" if absent else "UNRESOLVED",
                       "primary_lookup_reason_code": None if absent else reason_code}
            if absent:
                updated["references"] = [{"role": item["primary_role"], "status": "SOURCE_MISSING", "value": "", "source_ids": [],
                                           "reason": "所选相关章节的全部逻辑单元均已检查，未披露该字段。"}]
            raw_model_items = [value for value in raw_model_items if value.get("item_id") != item_id]
            raw_model_items.append(updated)
        if trace:
            trace({"kind": "artifact", "path": "comparison/primary_section_routing/coverage.json",
                   "content": [{"item_id": item["item_id"], **item["primary_routing"]} for item in missing_primary_items]})

    if progress:
        progress("primary_lookup", "completed", "主要报告指标定位与未命中核实完成")
        progress("comparison", "running", "正在比较已选定的单一来源")
    if trace:
        trace({"kind": "artifact", "path": "comparison/model_groups.json", "content": {"groups": {key: [item["item_id"] for item in value] for key, value in groups.items()}, "failed": failed_groups}})
        trace({"kind": "event", "event": "summary_compare_model_completed", "details": {"duration_ms": round((time.monotonic() - started) * 1000), "model": getattr(provider, "model_name", None), "groups": len(groups), "failed_groups": len(failed_groups)}})
    allowed_summary_ids = {item["item_id"]: {source["source_id"] for source in item["summary_candidates"]} for item in items}
    allowed_primary_ids = {item["item_id"]: {source["source_id"] for source in item["primary_candidates"]} for item in items}
    allowed_fallback_ids = {item["item_id"]: {source["source_id"] for source in item["fallback_candidates"]} for item in items}
    rule_inputs = {item["item_id"]: item for item in items}
    raw_items = {value["item_id"]: value for value in raw_model_items if value.get("item_id") in known_item_ids}

    fallback_outcomes: dict[str, dict[str, Any]] = {}
    fallback_requests = []
    for rule in rules:
        item_id = rule["item_id"]
        rule_input = rule_inputs[item_id]
        value = raw_items.get(item_id, {})
        fallback_role = rule_input["fallback_role"]
        not_applicable = rule_input["primary_not_applicable"]
        primary_ref = next((ref for ref in value.get("references", [])
                            if isinstance(ref, dict) and ref.get("role") == rule_input["primary_role"]), {})
        primary_status = primary_ref.get("status")
        primary_ids = primary_ref.get("source_ids") if isinstance(primary_ref.get("source_ids"), list) else []
        primary_evidence = [valid_sources[source_id] for source_id in primary_ids
                            if source_id in valid_sources and source_id in allowed_primary_ids[item_id]
                            and valid_sources[source_id]["document_role"] == rule_input["primary_role"]]
        primary_fact_verified = (
            primary_status in {"MATCH", "MISMATCH", "UNRESOLVED"}
            and len(primary_evidence) == len(set(primary_ids))
            and bool(primary_evidence)
            and _numbers_are_grounded(str(primary_ref.get("value") or ""), primary_evidence)
            and _field_evidence_is_substantive(rule, primary_evidence)
        )
        fallback_reason = _fallback_trigger_reason(
            rule, rule_input["fallback_when"],
            lookup_override=value.get("primary_lookup_status_override"),
            primary_not_applicable=not_applicable,
            primary_fact_verified=primary_fact_verified,
        )
        triggered = fallback_reason is not None
        if rule_input["primary_source_eligibility"] != "ELIGIBLE":
            fallback_outcomes[item_id] = {"status": "NOT_TRIGGERED", "reason": rule_input["primary_source_eligibility_reason"]}
            continue
        if not triggered or not fallback_role or not rule_input["fallback_when"]:
            fallback_outcomes[item_id] = {"status": "NOT_TRIGGERED", "reason": "主要来源尚未确认未披露。"}
            continue
        if fallback_role not in documents:
            fallback_outcomes[item_id] = {"status": "NOT_PROVIDED", "reason": "规则允许补充该指标，但本任务未上传招募说明书。"}
            continue
        if not allowed_fallback_ids[item_id]:
            fallback_outcomes[item_id] = {"status": "NO_CANDIDATES_UNCONFIRMED", "reason": "已触发补充查找，但当前候选不足以确认是否披露。"}
            continue
        if not rule_input["summary_candidates"]:
            fallback_outcomes[item_id] = {"status": "UNRESOLVED", "reason": "概要侧没有可核验的字段候选。"}
            continue

        summary_ids = value.get("summary_source_ids") if isinstance(value.get("summary_source_ids"), list) else []
        summary_allowed = allowed_summary_ids[item_id]
        summary_evidence = [valid_sources[source_id] for source_id in summary_ids if source_id in valid_sources and source_id in summary_allowed and valid_sources[source_id]["document_role"] == "product_summary"]
        summary_value = str(value.get("summary_value") or "")
        if (len(summary_evidence) != len(set(summary_ids)) or not summary_evidence
                or not _numbers_are_grounded(summary_value, summary_evidence)
                or not _field_evidence_is_substantive(rule, summary_evidence)):
            fallback_outcomes[item_id] = {"status": "UNRESOLVED", "reason": "概要侧目标事实尚未通过证据校验，补充来源不重复提取目标值。"}
            continue

        fallback_date_status, fallback_date_reason = _fallback_source_date_status(
            rule, documents, fallback_role, historical=historical,
        )
        plan = rule_input["fallback_context_plan"]
        if not plan["batches"]:
            fallback_outcomes[item_id] = {"status": "UNRESOLVED", "reason": "补充来源逻辑单元超出上下文预算，未检查。",
                                          "unexamined_units": plan["skipped_units"]}
        for batch in plan["batches"]:
            fallback_requests.append({
            "item_id": item_id, "label": rule["label"], "business_rule_markdown": rule_input["business_rule_markdown"],
            "fallback_role": fallback_role,
            "fallback_when": rule_input["fallback_when"],
            "lookup_basis": fallback_reason,
            "fallback_date_status": fallback_date_status,
            "fallback_date_reason": fallback_date_reason,
            "target_compiled_date": documents.get("product_summary", {}).get("metadata", {}).get("compiled_date"),
            "fallback_document_date": documents.get(fallback_role, {}).get("metadata", {}).get("date"),
            "verified_target": {"value": summary_value, "source_ids": summary_ids},
            "fallback_candidates": batch["candidates"],
            "logical_units": batch["logical_units"], "table_context": batch.get("table_context", []),
            "coverage": {"coverage_complete": plan["coverage_complete"], "batch_count": len(plan["batches"]),
                         "skipped_unit_count": len(plan["skipped_units"])},
            })
    for fallback_index, fallback_batch in enumerate(_payload_batches(fallback_requests), start=1):
        fallback_batch = [request for request in fallback_batch
                          if fallback_outcomes.get(request["item_id"], {}).get("status") != "FOUND"]
        if not fallback_batch:
            continue
        fallback_prompt = (
            "按每项已验证目标事实及其Markdown规则，在允许的补充文件中查找同一业务字段。same_contract_annual_rate只允许查同一基金合同的年费率；不得把定期报告里已验证的主来源差异替换掉。不得重新提取、改写verified_target的值或source_id。"
            "只有补充文件版本日期区间明确晚于概要编制日期区间时，才不得用其形成当时适用结论。日期精度为年月等导致区间相交时，不得仅凭区间相交认定来源无效或强制UNRESOLVED；仍须核对同一产品、字段含义、适用对象、原文证据及是否存在变更提示。若版本明确晚于概要编制日期，只能记录原文证据并将comparison_status设为UNRESOLVED。"
            "候选包含同页bbox坐标。表格标签和值可能分处相邻单元格或不同行，请结合坐标与列关系绑定；必须引用字段标签与对应值的原始source_id。"
            "找到补充来源时返回FOUND和补充值及source_id；字段身份、适用口径或引用不清时返回UNRESOLVED。金额项目仍须给出comparison_basis的same_period、same_nature、same_unit。严格返回JSON："
            "{\"items\":[{\"item_id\":\"stable-rule-id\",\"status\":\"FOUND|UNRESOLVED\",\"comparison_status\":\"MATCH|MISMATCH|UNRESOLVED\",\"comparison_basis\":{},\"value\":\"\",\"source_ids\":[],\"reason\":\"\"}]}\n"
            + json.dumps(_shared_context(fallback_batch), ensure_ascii=False)
        )
        if trace:
            trace({"kind": "artifact", "path": f"comparison/fallback_batch/batch_{fallback_index}/model_request.json",
                   "content": {"prompt": fallback_prompt, "item_ids": [item["item_id"] for item in fallback_batch]}})
        try:
            fallback_raw = await _generate_model_text(
                provider, system, fallback_prompt, stage="fallback_batch", trace=trace,
            )
            if trace:
                trace({"kind": "artifact", "path": f"comparison/fallback_batch/batch_{fallback_index}/model_response.txt", "content": fallback_raw})
            response_by_id = {value["item_id"]: value for value in _parse_json(fallback_raw).get("items", [])
                              if isinstance(value, dict) and value.get("item_id") in {item["item_id"] for item in fallback_batch}}
            for request in fallback_batch:
                item_id = request["item_id"]
                rule_inputs[item_id].setdefault("fallback_examined_source_ids", set()).update(
                    source["source_id"] for source in request["fallback_candidates"])
                response = response_by_id.get(item_id, {})
                fallback_role = request["fallback_role"]
                fallback_ids = response.get("source_ids") if isinstance(response.get("source_ids"), list) else []
                evidence = [valid_sources[source_id] for source_id in fallback_ids
                            if source_id in valid_sources and source_id in {source["source_id"] for source in request["fallback_candidates"]}
                            and valid_sources[source_id]["document_role"] == fallback_role]
                fallback_value = str(response.get("value") or "")
                fact = _verified_fact(fallback_value, fallback_ids, request["fallback_candidates"], rule_by_id[item_id])
                if fact:
                    fallback_ids = fact["source_ids"]
                    evidence = [valid_sources[source_id] for source_id in fallback_ids]
                if (response.get("status") != "FOUND" or len(evidence) != len(set(fallback_ids)) or not evidence
                        or not _numbers_are_grounded(fallback_value, evidence)
                        or not _field_evidence_is_substantive(rule_by_id[item_id], evidence)):
                    fallback_outcomes[item_id] = {"status": "UNRESOLVED", "reason": str(response.get("reason") or "补充来源未能确认字段。")}
                    continue
                target = request["verified_target"]
                target_evidence = [valid_sources[source_id] for source_id in target["source_ids"]]
                fallback_outcomes[item_id] = {
                    "status": "FOUND", "reason": "补充来源找到可引用证据。",
                    "comparison_reason": str(response.get("reason") or ""),
                    "comparison_status": response.get("comparison_status"),
                    "comparison_basis": response.get("comparison_basis"),
                    "date_status": request["fallback_date_status"],
                    "date_reason": request["fallback_date_reason"],
                    "value": fallback_value, "evidence": evidence,
                    "citation_binding": (fact or {}).get("citation_binding"),
                    "summary_value": target["value"], "summary_evidence": target_evidence,
                }
        except Exception as exc:
            for request in fallback_batch:
                fallback_outcomes[request["item_id"]] = {"status": "EXTRACTION_FAILED", "reason": f"补充来源识别失败：{type(exc).__name__}"}
    results = []
    target_fact_records = []
    selected_reference_fact_records = []
    for rule in rules:
        item_id = rule["item_id"]
        value = raw_items.get(item_id, {})
        rule_input = rule_inputs[item_id]
        primary_role = rule_input["primary_role"]
        fallback_role = rule_input["fallback_role"]
        summary_ids = value.get("summary_source_ids") if isinstance(value.get("summary_source_ids"), list) else []
        summary_evidence = [valid_sources[source_id] for source_id in summary_ids if source_id in valid_sources and source_id in allowed_summary_ids[item_id] and valid_sources[source_id]["document_role"] == "product_summary"]
        summary_value = str(value.get("summary_value") or "")
        if (len(summary_evidence) != len(set(summary_ids)) or not _numbers_are_grounded(summary_value, summary_evidence)
                or not _field_evidence_is_substantive(rule, summary_evidence)):
            summary_evidence = []
        refs_by_role = {ref.get("role"): ref for ref in value.get("references", []) if isinstance(ref, dict)}
        selected_role = primary_role
        ref = refs_by_role.get(primary_role, {})
        ref_status = ref.get("status") if ref.get("status") in {"MATCH", "MISMATCH", "UNRESOLVED", "SOURCE_MISSING"} else "UNRESOLVED"
        model_omitted_item = item_id not in raw_items and bool(rule_input["primary_candidates"])
        if model_omitted_item:
            ref_status = "UNRESOLVED"
        reference_ids = ref.get("source_ids") if isinstance(ref.get("source_ids"), list) else []
        reference_evidence = [valid_sources[source_id] for source_id in reference_ids if source_id in valid_sources and source_id in allowed_primary_ids[item_id] and valid_sources[source_id]["document_role"] == primary_role]
        reference_value = str(ref.get("value") or "")
        if (len(reference_evidence) != len(set(reference_ids)) or not _numbers_are_grounded(reference_value, reference_evidence)
                or (ref_status != "SOURCE_MISSING" and not _field_evidence_is_substantive(rule, reference_evidence))):
            ref_status, reference_evidence = "UNRESOLVED", []
        report_meta = documents.get(primary_role, {}).get("metadata", {})
        required_subtypes = rule.get("primary_source", {}).get("subtypes", [])
        report_subtype = report_meta.get("report_subtype")
        primary_not_applicable = rule_input["primary_not_applicable"]
        lookup_override = value.get("primary_lookup_status_override")
        if reference_evidence:
            primary_lookup_status = "FOUND"
        elif lookup_override:
            primary_lookup_status = lookup_override
        elif model_omitted_item:
            primary_lookup_status = "EXTRACTION_FAILED"
        elif primary_not_applicable:
            primary_lookup_status = "NOT_APPLICABLE"
        else:
            primary_lookup_status = rule_input["primary_lookup_status"]
        fallback_lookup = fallback_outcomes.get(item_id, {"status": "NOT_TRIGGERED", "reason": "主要来源尚未确认未披露。"})
        if lookup_override == "CONFIRMED_ABSENT":
            primary_lookup_status = "CONFIRMED_ABSENT"
        elif (ref_status == "SOURCE_MISSING" and lookup_override not in {"NOT_APPLICABLE", "CONFIRMED_ABSENT"}
              and not primary_not_applicable):
            # Candidate-level absence is not enough: the bounded full-report
            # lookup must confirm that the field is genuinely undisclosed.
            ref_status = "UNRESOLVED"
            primary_lookup_status = "UNRESOLVED"
        elif lookup_override == "NOT_APPLICABLE":
            primary_lookup_status = "NOT_APPLICABLE"

        # A model's explicit missing result is actionable only when it cites no fabricated source
        # and the rule permits that fallback. Extraction gaps never trigger source substitution.
        fallback_triggered = lookup_override == "CONFIRMED_ABSENT" or (primary_not_applicable and "primary_not_applicable" in rule_input["fallback_when"])
        fallback_outcome = fallback_outcomes.get(item_id, {})
        fallback_selected = fallback_outcome.get("status") == "FOUND"
        if fallback_outcome.get("status") == "FOUND":
            selected_role = fallback_role
            ref_status = fallback_outcome.get("comparison_status") if fallback_outcome.get("comparison_status") in {"MATCH", "MISMATCH", "UNRESOLVED"} else "UNRESOLVED"
            if fallback_outcome.get("date_status") != "ELIGIBLE":
                ref_status = "UNRESOLVED"
            reference_evidence, reference_value = fallback_outcome["evidence"], fallback_outcome["value"]
            summary_evidence, summary_value = fallback_outcome["summary_evidence"], fallback_outcome["summary_value"]
        elif fallback_triggered and fallback_outcome.get("status") in {"UNRESOLVED", "EXTRACTION_FAILED"}:
            ref_status = "UNRESOLVED"

        comparison_basis = (fallback_outcome if fallback_selected else ref).get("comparison_basis")
        basis_unconfirmed = (rule.get("value_type") == "amount" and rule.get("period_policy") == "same_period"
                             and (not isinstance(comparison_basis, dict) or not all(
                                 comparison_basis.get(key) is True for key in ("same_period", "same_nature", "same_unit"))))
        if basis_unconfirmed and ref_status in {"MATCH", "MISMATCH"}:
            ref_status = "UNRESOLVED"
        if _can_compare_numeric_deterministically(rule, ref_status) and summary_evidence and reference_evidence:
            percent = True
            summary_number = _normalized_numeric_value(summary_value, percent=percent)
            reference_number = _normalized_numeric_value(reference_value, percent=percent)
            if summary_number is None or reference_number is None:
                ref_status = "UNRESOLVED"
            else:
                ref_status = "MATCH" if summary_number == reference_number else "MISMATCH"
        if (rule.get("comparison_policy") in {"role_aware_identifier", "code_set_intersection"}
                and ref_status in {"MATCH", "MISMATCH"} and summary_evidence and reference_evidence):
            code_status = _compare_code_sets(summary_value, reference_value)
            ref_status = code_status or "UNRESOLVED"
        if selected_role == fallback_role and reference_evidence and not _can_compare_numeric_deterministically(rule, ref_status):
            # The fallback lookup response includes a separate semantic comparison decision.
            ref_status = ref_status if ref_status in {"MATCH", "MISMATCH", "UNRESOLVED"} else "UNRESOLVED"
        if rule_input["primary_source_eligibility"] != "ELIGIBLE":
            status, reason_code = "NOT_CHECKED", "PRIMARY_SOURCE_DATE_UNCONFIRMED" if rule_input["primary_source_eligibility"] == "DATE_UNCONFIRMED" else "PRIMARY_SOURCE_AFTER_TARGET_DATE"
        elif fallback_selected and fallback_outcome.get("date_status") != "ELIGIBLE":
            status, reason_code = "UNRESOLVED", "FALLBACK_SOURCE_DATE_UNCONFIRMED"
        elif not rule_input["summary_candidates"]:
            status, reason_code = "NOT_CHECKED", "TARGET_FIELD_NOT_FOUND_IN_PRESET_SCOPE"
        elif not summary_evidence and primary_lookup_status in {"NO_CANDIDATES_UNCONFIRMED", "NOT_APPLICABLE"}:
            status, reason_code = "NOT_CHECKED", "FIELD_CANDIDATES_NOT_CONFIRMED"
        elif not summary_evidence:
            status, reason_code = "UNRESOLVED", "TARGET_NOT_LOCATED"
        elif not fallback_selected and primary_lookup_status == "UNRESOLVED" and value.get("primary_lookup_reason_code"):
            status, reason_code = "UNRESOLVED", value["primary_lookup_reason_code"]
        elif not fallback_selected and primary_lookup_status in {"NO_CANDIDATES_UNCONFIRMED", "NOT_APPLICABLE"} and ref_status not in {"MATCH", "MISMATCH"}:
            status, reason_code = "NOT_CHECKED", "PRIMARY_LOOKUP_UNCONFIRMED"
        elif not fallback_selected and ref_status == "SOURCE_MISSING":
            status, reason_code = "NOT_CHECKED", "NO_VALID_REFERENCE"
        elif basis_unconfirmed and summary_evidence and reference_evidence:
            status, reason_code = "UNRESOLVED", "COMPARISON_BASIS_UNCONFIRMED"
        elif ref_status in {"MATCH", "MISMATCH"} and reference_evidence:
            status, reason_code = ref_status, None
        else:
            status, reason_code = "UNRESOLVED", "EVIDENCE_OR_COMPARISON_UNRESOLVED"
        primary_reference = {"role": primary_role, "status": "FOUND" if primary_lookup_status == "FOUND" else primary_lookup_status,
                             "evidence": reference_evidence if selected_role == primary_role else [], "raw_text": reference_value if selected_role == primary_role else None}
        selected_reference = {"role": selected_role, "raw_text": reference_value if reference_evidence else None, "evidence": reference_evidence,
                              "status": status, "reason": str(
                                  (fallback_outcome.get("comparison_reason") or "根据补充招募说明书原文完成核对。")
                                  if fallback_selected else ref.get("reason") or "根据选定来源完成核对。")}
        references_result = {primary_role: primary_reference}
        if fallback_role:
            references_result[fallback_role] = {"role": fallback_role, "status": "FOUND" if selected_role == fallback_role and reference_evidence else fallback_lookup["status"],
                                                "raw_text": reference_value if selected_role == fallback_role and reference_evidence else None,
                                                "evidence": reference_evidence if selected_role == fallback_role else [], "reason": fallback_lookup["reason"]}
        extraction_status = {
            "target": "FOUND" if summary_evidence else "UNRESOLVED" if rule_input["summary_candidates"] else "NOT_FOUND_UNCONFIRMED",
            "primary_reference": primary_lookup_status,
            "selected_reference": "FOUND" if reference_evidence else "UNRESOLVED" if status == "UNRESOLVED" else "NOT_FOUND",
        }
        applicability_status = (
            rule_input["primary_source_eligibility"]
            if rule_input["primary_source_eligibility"] != "ELIGIBLE"
            else "NOT_APPLICABLE" if primary_not_applicable else "ELIGIBLE"
        )
        evidence_status = (
            "VERIFIED" if summary_evidence and reference_evidence
            else "TARGET_ONLY" if summary_evidence
            else "REFERENCE_ONLY" if reference_evidence
            else "MISSING"
        )
        target_fact_record = {
            "item_id": item_id, "value": summary_value or None,
            "citation_binding": value.get("summary_citation_binding"),
            "candidate_source_ids": summary_ids, "verified_sources": summary_evidence,
            "status": "VERIFIED" if summary_evidence else "CANDIDATE_UNVERIFIED" if summary_value or summary_ids else "NOT_FOUND_UNCONFIRMED",
            "reason_code": None if summary_evidence else "SOURCE_OR_FIELD_VALIDATION_FAILED" if summary_value or summary_ids else "NO_MODEL_CANDIDATE",
        }
        selected_fact_ids = ([source["source_id"] for source in reference_evidence]
                             if selected_role != primary_role else reference_ids)
        selected_reference_fact_record = {
            "item_id": item_id, "role": selected_role, "value": reference_value or None,
            "citation_binding": fallback_outcome.get("citation_binding") if fallback_selected else ref.get("citation_binding"),
            "candidate_source_ids": selected_fact_ids, "verified_sources": reference_evidence,
            "status": "VERIFIED" if reference_evidence else "CANDIDATE_UNVERIFIED" if reference_value or selected_fact_ids else "NOT_FOUND_UNCONFIRMED",
            "reason_code": None if reference_evidence else "SOURCE_OR_FIELD_VALIDATION_FAILED" if reference_value or selected_fact_ids else "NO_MODEL_CANDIDATE",
        }
        target_fact_records.append(target_fact_record)
        selected_reference_fact_records.append(selected_reference_fact_record)
        results.append({
            "context_coverage": {
                side: {"source_count": rule_input[f"{side}_context_plan"]["source_count"],
                       "planned_batches": len(rule_input[f"{side}_context_plan"]["batches"]),
                       "sent_source_count": len(rule_input.get(f"{side}_examined_source_ids", [])),
                       "skipped_units": rule_input[f"{side}_context_plan"]["skipped_units"],
                       "skipped_structure_hints": rule_input[f"{side}_context_plan"]["skipped_structure_hints"]}
                for side in ("summary", "primary", "fallback")
            },
            "item_id": item_id, "module_id": rule["module_id"],
            "excel_row": rule.get("excel_row"), "key": rule["key"], "label": rule["label"], "status": status,
            "extraction_status": extraction_status,
            "applicability_status": applicability_status,
            "comparison_status": status if status in {"MATCH", "MISMATCH", "UNRESOLVED"} else "NOT_CHECKED",
            "evidence_status": evidence_status,
            "comparison_basis": comparison_basis,
            "facts": {"target": target_fact_record, "selected_reference": selected_reference_fact_record},
            "summary": {"raw_text": summary_value if summary_evidence else None, "evidence": summary_evidence},
            "reference": selected_reference, "references": references_result,
            "primary_lookup": {
                "status": primary_lookup_status, "role": primary_role,
                "reason_code": value.get("primary_lookup_reason_code"),
                "routing": value.get("primary_routing") or rule_input.get("primary_routing"),
            },
            "fallback_lookup": fallback_lookup,
            "selected_reference": {"role": selected_role, "evidence": reference_evidence},
            "selected_source_date_status": fallback_outcome.get("date_status") if fallback_selected else "ELIGIBLE" if reference_evidence else None,
            "source_selection_reason": (
                rule_input["primary_source_eligibility_reason"]
                if rule_input["primary_source_eligibility"] != "ELIGIBLE"
                else "主要定期报告有证据" if selected_role == primary_role and reference_evidence
                else "主要报告返回了候选结论，但引用未通过原文校验" if primary_lookup_status == "FOUND"
                else fallback_outcome.get("date_reason") or fallback_lookup["reason"]
            ),
            "reason_code": reason_code,
            "reason": str(
                fallback_outcome.get("date_reason") if fallback_selected and fallback_outcome.get("date_status") != "ELIGIBLE"
                else "概要指定章节未命中该指标，未执行比较。" if reason_code == "TARGET_FIELD_NOT_FOUND_IN_PRESET_SCOPE"
                else "期间、费用性质或币种单位未确认一致，未作金额差异判断。" if reason_code == "COMPARISON_BASIS_UNCONFIRMED"
                else (fallback_outcome.get("comparison_reason") or "已使用招募说明书原文完成本项核对。") if fallback_selected
                else ref.get("reason") or value.get("reason") or rule_input["primary_source_eligibility_reason"]
                or ("双方字段原文及引用已通过核验，核对一致。" if status == "MATCH" else None)
                or ("主要来源尚不能确认是否披露。" if status == "NOT_CHECKED" else "未提供可验证的比较依据")
            ),
        })
    if trace:
        trace({"kind": "artifact", "path": "comparison/facts/target_facts.json", "content": target_fact_records})
        trace({"kind": "artifact", "path": "comparison/facts/selected_reference_facts.json", "content": selected_reference_fact_records})
        trace({"kind": "artifact", "path": "comparison/source_selection.json", "content": {
            "rules_version": rule_version,
            "items": [{"key": item["key"], "primary_lookup": item["primary_lookup"],
                       "fallback_lookup": item["fallback_lookup"], "selected_reference": item["selected_reference"],
                       "reason_code": item["reason_code"]} for item in results],
        }})
    if progress:
        fallback_used = any(item["selected_reference"]["role"] == "prospectus" for item in results)
        progress("fallback_lookup", "completed", "已完成允许的补充查找" if fallback_used else "没有触发补充查找")
        progress("comparison", "completed", "指标核对和引用校验完成")
    difference_count = sum(item["status"] == "MISMATCH" for item in results)
    unresolved_count = sum(item["status"] == "UNRESOLVED" for item in results)
    compared_count = sum(item["status"] in {"MATCH", "MISMATCH"} for item in results)
    skipped_count = sum(item["status"] in {"SKIPPED", "NOT_CHECKED"} for item in results)
    overall_status = (
        "MISMATCH" if difference_count else
        "UNRESOLVED" if unresolved_count else
        "NOT_CHECKED" if skipped_count else
        "MATCH" if compared_count else
        "SKIPPED"
    )
    return {
        "schema_version": 3,
        "workflow_id": "product-summary-verification",
        "rules_version": rule_version,
        "status": overall_status,
        "difference_count": difference_count,
        "unresolved_count": unresolved_count,
        "compared_count": compared_count,
        "skipped_count": skipped_count,
        "not_checked_count": sum(item["status"] == "NOT_CHECKED" for item in results),
        "no_common_content_count": sum(item["status"] == "NO_COMMON_CONTENT" for item in results),
        "matched_count": sum(item["status"] == "MATCH" for item in results),
        "source_usage": {"periodic_report": sum(item.get("selected_reference", {}).get("role") == "periodic_report" and bool(item.get("selected_reference", {}).get("evidence")) for item in results),
                         "prospectus": sum(item.get("selected_reference", {}).get("role") == "prospectus" and bool(item.get("selected_reference", {}).get("evidence")) for item in results)},
        "warnings": sorted(set(warnings + [f"{item['group']} 主题未能完成识别：{item['error']}" for item in failed_groups])),
        "documents": {role: {"filename": catalogue["filename"], "page_count": catalogue["page_count"], "page_dimensions": catalogue["page_dimensions"], "source_count": catalogue["source_count"]} for role, catalogue in catalogues.items()},
        "chapters": {role: catalogue["chapters"] for role, catalogue in catalogues.items()},
        "items": results,
    }
