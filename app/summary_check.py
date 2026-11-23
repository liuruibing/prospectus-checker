from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from datetime import date
from difflib import SequenceMatcher
from html import unescape
from pathlib import Path
from typing import Any, Callable
import unicodedata

import pymupdf as fitz

from app.pdf_document import bind_catalogue, verified_conversion
from app.pdf_worker import run_pdf
from app.source_evidence import evidence_source
from app.source_aliases import SourceAliasProvider
from app.model_json import read_model_json
from app.summary_rules import RULE_DIR, load_markdown_rules, module_groups
from app.rule_compiler import capture_rule_set, load_compiled_execution_plan
from app.rule_plan import eligible_fallbacks
from app.comparison import compare_declared_field, evaluate_declared_context


RULE_CONFIG, RULES = load_markdown_rules(require_execution_plans=True)
RULE_VERSION = str(RULE_CONFIG["version"])
CONTEXT_BATCH_CHARACTERS = 16_000
LOGICAL_TABLE_CHARACTERS = 32_000
MODEL_PAYLOAD_CHARACTERS = 48_000
MODEL_REQUEST_CHARACTERS = 64_000
SECTION_HINT_SIMILARITY_THRESHOLD = 0.45
# The stable key is used only for result indexing and presentation.
for _rule in RULES:
    _rule.setdefault("key", _rule["item_id"])


def _load_rule_snapshot() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read a consistent rule snapshot when a comparison starts."""
    return load_markdown_rules(require_execution_plans=True)


def capture_product_summary_rule_snapshot() -> dict[str, Any]:
    """Freeze the parsed rules and their source/execution version for one task."""
    config, rules = _load_rule_snapshot()
    provenance = capture_rule_set("product_summary")
    execution_plans = {rule["item_id"]: rule["execution_plan"] for rule in rules}
    return {
        **provenance,
        "rule_config": config,
        "rules": rules,
        "execution_plans": execution_plans,
    }


def _normalize_model_item_id(value: dict[str, Any]) -> dict[str, Any] | None:
    item_id = value.get("item_id")
    if isinstance(item_id, str) and item_id:
        return {**value, "item_id": item_id}
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
    if source.get("source_kind") == "image_ocr" or "\n" in text.strip():
        return None
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
    search = (item.get("execution_plan") or {}).get("search_intent", {})
    terms = [_search_norm(term) for term in [item.get("label", ""), search.get("target_description", ""),
                                               *search.get("preferred_sections", [])] if term]
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
    search = (item.get("execution_plan") or {}).get("search_intent", {})
    terms = [_search_norm(term) for term in [item.get("label", ""), search.get("target_description", ""),
                                               *search.get("preferred_sections", [])] if term]
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


def _prescribed_sections(catalogue: dict[str, Any], item: dict[str, Any], *, first_only: bool = False) -> list[dict[str, Any]]:
    """Route every applicable prose chapter hint, tolerating heading variation."""
    plan = item.get("execution_plan") or {}
    search_intent = plan.get("search_intent", {})
    hints = search_intent.get("preferred_sections_by_role", {}).get(
        catalogue.get("role"), search_intent.get("preferred_sections", []),
    )
    hints = [_search_norm(hint) for hint in hints if hint]
    sections = _chapter_directory(catalogue)
    selected = []
    seen = set()
    for hint in hints:
        roots = []
        for section in sections:
            title = _search_norm(section["title"])
            title_core = _search_norm(re.sub(
                r"^(?:§\s*\d+(?:\.\d+)*|第[一二三四五六七八九十百]+[章节部分]+|[（(][一二三四五六七八九十]+[)）]|[一二三四五六七八九十]+、|\d+[\d.．、]*)\s*",
                "", section["title"],
            ))
            # A compiled prose hint may contain the chapter title plus
            # explanatory qualifiers. Accept that direction of containment
            # for substantive headings, while keeping short generic labels
            # out of the match to avoid broad routing.
            if hint in title or hint in title_core or (len(title_core) >= 6 and title_core in hint):
                roots.append(section)
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
    if not selected and hints:
        # Rules are written as business language, while chapter directories
        # contain publisher-specific headings. Use generic title similarity as
        # a fallback; exact/contained title matches above remain authoritative.
        scores = [
            (SequenceMatcher(None, hint, _search_norm(section["title"])).ratio(), section)
            for hint in hints for section in sections
        ]
        best = max((score for score, _ in scores), default=0.0)
        if best >= SECTION_HINT_SIMILARITY_THRESHOLD:
            roots = [
                section for score, section in scores
                if score >= max(SECTION_HINT_SIMILARITY_THRESHOLD, best - 0.04)
            ]
            for root in roots:
                for section in sections:
                    if (section["path"] == root["path"] or section["path"].startswith(root["path"] + " / ")) and section["section_id"] not in seen:
                        selected.append(section)
                        seen.add(section["section_id"])
    return selected


def _source_candidates(catalogue: dict[str, Any], item: dict[str, Any]) -> list[dict[str, Any]]:
    sources = [source for page in catalogue.get("pages", []) for source in page.get("sources", [])]
    plan = item.get("execution_plan") or {}
    search_intent = plan.get("search_intent", {})
    is_target_role = catalogue.get("role") == plan.get("target_role")
    short_terms = [_search_norm(term) for term in [
        item.get("label", ""),
        *search_intent.get("preferred_sections", []),
        *search_intent.get("preferred_sections_by_role", {}).get(catalogue.get("role"), []),
    ] if isinstance(term, str) and term]
    desc = search_intent.get("target_description", "")
    terms = [*short_terms, *([_search_norm(desc)] if isinstance(desc, str) and desc else [])]
    prescribed_sections = _prescribed_sections(catalogue, item)
    prescribed_section_ids = {section["section_id"] for section in prescribed_sections}
    selected_ids = {sid for section in prescribed_sections for sid in section["source_ids"]}
    if prescribed_section_ids:
        selected_ids |= {source["source_id"] for source in sources if source.get("section_id") in prescribed_section_ids}

    def matches_rule_text(source: dict[str, Any]) -> bool:
        for key in ("raw_text", "row_text", "cell_text"):
            text = _search_norm(source.get(key, ""))
            if not text:
                continue
            for term in terms:
                if term and term in text:
                    return True
            # Reverse containment helps short table labels match longer rule wording.
            # Restrict non-target docs to short cell labels against concise rule terms
            # so long narrative lines cannot pull unrelated chapters into 5-round scans.
            reverse_terms = terms if is_target_role else short_terms
            max_reverse_len = 60 if is_target_role else 24
            if (is_target_role or key == "cell_text") and 3 <= len(text) <= max_reverse_len:
                for term in reverse_terms:
                    if term and text in term:
                        return True
        return False

    if selected_ids:
        selected = [source for source in sources if source["source_id"] in selected_ids]
        if is_target_role and not any(
            matches_rule_text(source) for source in selected
        ):
            return []
        return selected
    anchors = [source for source in sources
               if matches_rule_text(source)]
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


def _isolated_payload_batches(items: list[dict[str, Any]], budget: int = MODEL_PAYLOAD_CHARACTERS) -> list[list[dict[str, Any]]]:
    """Keep each business item's evidence scope in its own model request."""
    batches = []
    for item in items:
        payload = _shared_context([item])
        if _json_size(payload) > budget:
            raise ValueError(f"CONTEXT_ITEM_EXCEEDS_BUDGET: {item.get('item_id')}")
        batches.append([item])
    return batches


def _merge_fallback_contexts(items: list[dict[str, Any]], budget: int = MODEL_PAYLOAD_CHARACTERS) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Keep one item's related source chapters together when they fit the model budget."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        grouped.setdefault(item["item_id"], []).append(item)

    merged_items = []
    oversized: dict[str, int] = {}
    for item_id, parts in grouped.items():
        merged = dict(parts[0])
        sources = {}
        units = {}
        for part in parts:
            for source in part.get("fallback_candidates", []):
                sources.setdefault(source["source_id"], source)
            for unit in part.get("logical_units", []):
                key = unit.get("unit_id") or json.dumps(unit, ensure_ascii=False, sort_keys=True)
                units.setdefault(key, unit)
        merged["fallback_candidates"] = list(sources.values())
        merged["logical_units"] = list(units.values())
        size = _json_size(_shared_context([merged]))
        if size > budget:
            oversized[item_id] = size
            continue
        merged_items.append(merged)
    return merged_items, oversized


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
                    # Keep the identifier in the shared record as well as its
                    # map key. Models have repeatedly returned plausible IDs
                    # from outside the allowed evidence scope when the ID was
                    # implicit only in a JSON object key.
                    compact_source = dict(source)
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


_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}


def _parse_cn_number(text: str) -> str | None:
    if not text:
        return None
    int_part, _, frac_part = text.partition("点")
    int_val = 0
    if int_part:
        if all(ch in _CN_DIGITS for ch in int_part):
            int_val = int("".join(str(_CN_DIGITS[ch]) for ch in int_part))
        else:
            section = 0
            num = 0
            for ch in int_part:
                if ch in _CN_DIGITS:
                    num = _CN_DIGITS[ch]
                elif ch in _CN_UNITS:
                    unit = _CN_UNITS[ch]
                    if num == 0 and unit == 10:
                        num = 1
                    section += num * unit
                    num = 0
                else:
                    return None
            int_val = section + num
    frac_digits = ""
    if frac_part:
        for ch in frac_part:
            if ch not in _CN_DIGITS:
                return None
            frac_digits += str(_CN_DIGITS[ch])
    return f"{int_val}.{frac_digits}".rstrip(".") if frac_digits else str(int_val)


def _chinese_percent_tokens(text: str) -> set[str]:
    from decimal import Decimal
    tokens: set[str] = set()
    for divisor, match in (
        *((1, m) for m in re.finditer(r"百分之([零〇一二三四五六七八九十百千万点两]+)", text)),
        *((10, m) for m in re.finditer(r"千分之([零〇一二三四五六七八九十百千万点两]+)", text)),
    ):
        parsed = _parse_cn_number(match.group(1))
        if parsed is None:
            continue
        try:
            num = Decimal(parsed) / Decimal(divisor)
            tokens.add(format(num.normalize(), "f") + "%")
        except Exception:
            continue
    return tokens


def _role_grounding_texts(context_obj: dict[str, Any] | None, role: str | None) -> list[str]:
    if not context_obj or not role:
        return []
    metadata = (context_obj.get("document_metadata") or {}).get(role) or {}
    texts: list[str] = []
    for val in metadata.values():
        if val is None or val == "":
            continue
        if isinstance(val, (str, int, float)):
            texts.append(str(val))
        elif isinstance(val, list):
            texts.extend(str(item) for item in val if item is not None and item != "")
        elif isinstance(val, dict):
            texts.extend(str(v) for v in val.values() if isinstance(v, (str, int, float)) and v != "")
    return texts


def _normalize_structured_fields(field_values: Any) -> dict[str, Any] | None:
    if not isinstance(field_values, dict):
        return None
    cleaned: dict[str, Any] = {}
    for key, val in field_values.items():
        if not isinstance(key, str) or not key.strip() or val in (None, ""):
            continue
        if isinstance(val, dict):
            sub = {str(k).strip(): str(v).strip() for k, v in val.items() if str(k).strip() and v not in (None, "")}
            if sub:
                cleaned[key.strip()] = sub
        elif isinstance(val, list):
            items = [str(v).strip() for v in val if v not in (None, "")]
            if items:
                cleaned[key.strip()] = items
        elif isinstance(val, (str, int, float)):
            cleaned[key.strip()] = str(val).strip()
    return cleaned or None


def _structured_field_text(field_values: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, val in field_values.items():
        if isinstance(val, dict):
            parts.append("；".join(f"{k}：{v}" for k, v in val.items()))
        elif isinstance(val, list):
            parts.append("；".join(str(v) for v in val))
        else:
            parts.append(f"{key}：{val}")
    return "；".join(parts)


def _verified_fact(
    value: Any,
    source_ids: Any,
    candidates: list[dict[str, Any]],
    rule: dict[str, Any],
    *,
    field_values: Any = None,
    extra_texts: list[str] | None = None,
) -> dict[str, Any] | None:
    ids = list(source_ids) if isinstance(source_ids, list) else []
    by_id = {source["source_id"]: source for source in candidates}
    evidence = [by_id[sid] for sid in ids if sid in by_id]
    if not (evidence and len(evidence) == len(set(ids)) and _field_evidence_is_substantive(rule, evidence)):
        return None
    cleaned_fields = _normalize_structured_fields(field_values)
    fields_grounded = bool(
        cleaned_fields
        and _numbers_are_grounded(_structured_field_text(cleaned_fields), evidence, extra_texts=extra_texts)
    )
    value_text = str(value or "").strip()
    if value_text and _numbers_are_grounded(value_text, evidence, extra_texts=extra_texts):
        return {
            "value": value_text,
            "source_ids": ids,
            **({"field_values": cleaned_fields} if cleaned_fields and fields_grounded else {}),
        }
    if fields_grounded and cleaned_fields is not None:
        return {
            "value": _structured_field_text(cleaned_fields),
            "source_ids": ids,
            "field_values": cleaned_fields,
        }
    return None


def _bind_model_fact_sources(values: list[dict[str, Any]], inputs: dict[str, dict[str, Any]]) -> None:
    """Bind a quoted value to an unambiguous heading within its allowed chapter."""
    for value in values:
        item = inputs.get(value.get("item_id"))
        if not item:
            continue
        target_role = (item.get("execution_plan") or {}).get("target_role")
        target = _verified_fact(
            value.get("summary_value"), value.get("summary_source_ids"), item["summary_candidates"], item,
            field_values=value.get("summary_fields"),
            extra_texts=_role_grounding_texts(item, target_role),
        )
        if target:
            value.update(summary_value=target["value"], summary_source_ids=target["source_ids"])
            if target.get("field_values") is not None:
                value["summary_fields"] = target["field_values"]
            if target.get("citation_binding"):
                value["summary_citation_binding"] = target["citation_binding"]
        for ref in value.get("references", []):
            if isinstance(ref, dict) and ref.get("role") == item["primary_role"]:
                primary = _verified_fact(
                    ref.get("value"), ref.get("source_ids"), item["primary_candidates"], item,
                    field_values=ref.get("fields"),
                    extra_texts=_role_grounding_texts(item, item.get("primary_role")),
                )
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


def _ungrounded_number_tokens(
    value: str,
    evidence: list[dict[str, Any]],
    *,
    extra_texts: list[str] | None = None,
) -> set[str]:
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
        return set()

    primary_texts: list[str] = []
    context_texts: list[str] = []
    for source in evidence:
        for key in ("raw_text", "row_text", "cell_text"):
            text = source.get(key)
            if isinstance(text, str) and text:
                primary_texts.append(text)
        section_path = source.get("section_path")
        if isinstance(section_path, list):
            context_texts.extend(str(part) for part in section_path if part)
        for layout_item in source.get("layout_context") or []:
            if isinstance(layout_item, dict) and layout_item.get("raw_text"):
                context_texts.append(str(layout_item["raw_text"]))
    extra_list = [str(text) for text in (extra_texts or []) if text]
    all_texts = [*primary_texts, *context_texts, *extra_list]

    observed: set[str] = set()
    standalone_stems: set[str] = set()
    for text in all_texts:
        observed.update(_chinese_percent_tokens(text))
        for match in token_pattern.finditer(text):
            norm = normalize(match.group(1))
            observed.add(norm)
            if not norm.endswith("%"):
                start, end = match.span(1)
                prev_ch = text[start - 1] if start > 0 else ""
                next_ch = text[end] if end < len(text) else ""
                both_cjk = ("\u4e00" <= prev_ch <= "\u9fff") and ("\u4e00" <= next_ch <= "\u9fff")
                if not both_cjk and (not prev_ch or prev_ch not in "第") and (not next_ch or next_ch not in "年月日位名只家股份条章节页期季"):
                    standalone_stems.add(norm)

    has_percent_unit = any(
        marker in text
        for text in all_texts
        for marker in ("%", "％", "百分比", "比例", "费率", "收益率", "增长率")
    )
    missing: set[str] = set()
    for token in expected:
        if token in observed:
            continue
        if token.endswith("%") and has_percent_unit and token[:-1] in standalone_stems:
            continue
        missing.add(token)
    return missing


def _numbers_are_grounded(
    value: str,
    evidence: list[dict[str, Any]],
    *,
    extra_texts: list[str] | None = None,
) -> bool:
    return not _ungrounded_number_tokens(value, evidence, extra_texts=extra_texts)


def _field_evidence_is_substantive(item: dict[str, Any], evidence: list[dict[str, Any]]) -> bool:
    del item
    # Candidate membership already constrains citations to the plan-selected
    # sections. This validator checks source integrity and rejects headings;
    # business relevance is described in Markdown and evaluated against the
    # bounded source context, never against a Python-maintained keyword list.
    return any(
        any(str(source.get(key) or "").strip() for key in ("raw_text", "row_text", "cell_text"))
        and _source_heading_info(source) is None
        for source in evidence
    )


def _review_conditions_are_clear(
    rule: dict[str, Any], assessment: dict[str, Any],
    allowed_source_ids: set[str], valid_source_ids: set[str],
    *, available_metadata_refs: set[str] | None = None,
) -> bool:
    """Require each declared condition to cite source text or trusted document metadata."""
    conditions = (rule.get("execution_plan") or {}).get("review_conditions", [])
    if not conditions:
        return True
    available_metadata_refs = available_metadata_refs or set()
    assessments = assessment.get("review_condition_assessments")
    if not isinstance(assessments, list) or len(assessments) < len(conditions):
        return False
    by_index = {}
    for item in assessments:
        if not isinstance(item, dict):
            return False
        index = item.get("condition_index")
        if not isinstance(index, int) or isinstance(index, bool) or index in by_index:
            return False
        source_ids = item.get("source_ids")
        metadata_refs = item.get("metadata_refs", [])
        status = item.get("status")
        if (status not in {"CLEAR", "NOT_APPLICABLE"} or not isinstance(source_ids, list)
                or not isinstance(metadata_refs, list)):
            return False
        if any(not isinstance(source_id, str) for source_id in source_ids):
            return False
        if any(not isinstance(ref, str) for ref in metadata_refs):
            return False
        if status == "CLEAR" and not source_ids and not metadata_refs:
            return False
        if status == "NOT_APPLICABLE" and (source_ids or metadata_refs):
            return False
        if not str(item.get("reason") or "").strip():
            return False
        if not set(source_ids) <= allowed_source_ids & valid_source_ids:
            return False
        if not set(metadata_refs) <= available_metadata_refs:
            return False
        by_index[index] = item
    return set(range(len(conditions))) <= set(by_index)


def _available_metadata_reference_ids(
    documents: dict[str, dict[str, Any]], roles: set[str] | None = None,
) -> set[str]:
    """Return role-qualified references for metadata facts present in this task."""
    return {
        f"{role}.{key}"
        for role, document in documents.items()
        if roles is None or role in roles
        for key, value in (document.get("metadata") or {}).items()
        if value is not None and value != ""
    }


def _metadata_facts_for_roles(
    documents: dict[str, dict[str, Any]], roles: set[str],
) -> dict[str, dict[str, Any]]:
    """Limit model-visible metadata to the documents allowed in this workflow stage."""
    metadata_keys = (
        "compiled_date", "compiled_date_interval", "compiled_date_quote", "period_end", "period_end_interval",
        "period_end_quote", "sent_date", "sent_date_interval", "sent_date_quote", "date_interval", "date_label",
        "filename_date_interval", "filename_date_quote", "filename_date_precision", "version_date",
        "version_date_interval", "version_date_quote", "version_date_source", "version_date_precision",
        "report_subtype", "fund_name", "fund_code", "fund_codes",
    )
    return {
        role: {key: value for key in metadata_keys
               if (value := (document.get("metadata") or {}).get(key)) is not None}
        for role, document in documents.items() if role in roles
    }


def _filter_unavailable_metadata_refs(
    assessment: dict[str, Any], available_metadata_refs: set[str],
) -> dict[str, Any]:
    """Keep valid evidence refs and retain rejected metadata refs for audit."""
    normalized = dict(assessment)
    conditions = assessment.get("review_condition_assessments")
    if not isinstance(conditions, list):
        return normalized
    normalized_conditions = []
    for condition in conditions:
        if not isinstance(condition, dict):
            normalized_conditions.append(condition)
            continue
        item = dict(condition)
        refs = item.get("metadata_refs")
        if item.get("status") == "CLEAR" and isinstance(refs, list):
            rejected = [ref for ref in refs if not isinstance(ref, str) or ref not in available_metadata_refs]
            if rejected:
                item["metadata_refs"] = [ref for ref in refs if isinstance(ref, str) and ref in available_metadata_refs]
                item["rejected_metadata_refs"] = rejected
        normalized_conditions.append(item)
    normalized["review_condition_assessments"] = normalized_conditions
    return normalized


def _primary_lookup_reference(
    rule: dict[str, Any], found: dict[str, Any], primary_fact: dict[str, Any], *, coverage_complete: bool,
) -> dict[str, Any]:
    """Keep the model's applicability and rule-condition decisions with a routed fact."""
    status = found.get("comparison_status")
    if status not in {"MATCH", "MISMATCH", "UNRESOLVED"}:
        status = "UNRESOLVED"
    applicability = found.get("applicability_status")
    if applicability not in {"CONFIRMED", "NOT_REQUIRED", "UNRESOLVED"}:
        applicability = "UNRESOLVED"
    assessments = found.get("review_condition_assessments")
    if not isinstance(assessments, list):
        assessments = []
    uncovered = found.get("uncovered_aspects")
    if not isinstance(uncovered, list):
        uncovered = []
    raw_coverage = found.get("coverage_status")
    coverage_value = "NOT_FOUND" if raw_coverage == "NOT_FOUND" else ("COMPLETE" if coverage_complete else "PARTIAL")
    return {
        "role": rule["primary_role"], "status": status,
        "applicability_status": applicability,
        "coverage_status": coverage_value,
        "uncovered_aspects": [str(value) for value in uncovered if str(value).strip()][:20],
        "review_condition_assessments": assessments,
        **primary_fact,
        "reason": str(found.get("reason") or "章节检查已找到可核验事实。"),
    }


_BARE_DECIMAL_RE = re.compile(r"^[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$")


def _field_declares_percent(field_spec: dict[str, Any]) -> bool:
    label = str(field_spec.get("label") or "")
    field_name = str(field_spec.get("field") or "").casefold()
    source_headers = [str(header or "") for header in (field_spec.get("source_headers") or [])]
    return (
        "%" in label
        or "％" in label
        or "percent" in field_name
        or any("%" in header or "％" in header for header in source_headers)
    )


def _with_declared_percent_unit(
    field_spec: dict[str, Any], left_text: str, right_text: str,
) -> tuple[str, str]:
    """Normalize bare percentage-point decimals only when field_spec declares '%'."""
    if field_spec.get("operation") != "decimal" or not _field_declares_percent(field_spec):
        return left_text, right_text
    left = left_text.strip()
    right = right_text.strip()
    if left.endswith("％"):
        left = f"{left[:-1]}%"
    if right.endswith("％"):
        right = f"{right[:-1]}%"
    if left.endswith("%") and _BARE_DECIMAL_RE.fullmatch(right):
        right = f"{right}%"
    elif right.endswith("%") and _BARE_DECIMAL_RE.fullmatch(left):
        left = f"{left}%"
    elif _BARE_DECIMAL_RE.fullmatch(left) and _BARE_DECIMAL_RE.fullmatch(right):
        left = f"{left}%"
        right = f"{right}%"
    return left, right


def _compare_scalar_entry(
    field_spec: dict[str, Any], left_text: str, right_text: str, model_status: str | None,
) -> str:
    left_val, right_val = _with_declared_percent_unit(field_spec, left_text, right_text)
    return compare_declared_field(
        field_spec, {"raw_text": left_val}, {"raw_text": right_val}, semantic_status=model_status,
    )


_ASSET_SUBITEM_PARENTS = {
    "普通股": "权益投资",
    "存托凭证": "权益投资",
    "优先股": "权益投资",
    "房地产信托": "权益投资",
    "债券": "固定收益投资",
    "资产支持证券": "固定收益投资",
    "买断式回购的买入返售金融资产": "买入返售金融资产",
}


def _normalize_structured_row_key(raw_key: str, row_matching: dict[str, Any]) -> str:
    text = str(raw_key or "").strip()
    for prefix in sorted((row_matching.get("identity_prefixes_to_ignore") or []), key=len, reverse=True):
        if prefix and text.startswith(prefix):
            text = text[len(prefix):].strip()
            break
    norm = _search_norm(text)
    for group in (row_matching.get("identity_equivalence_groups") or []):
        canonical = _search_norm(str(group.get("canonical_identity") or ""))
        labels = {_search_norm(str(label)) for label in (group.get("equivalent_labels") or []) if str(label).strip()}
        if canonical and (norm == canonical or norm in labels):
            return canonical
    return norm


def _is_covered_asset_subitem(
    raw_key: str,
    norm_key: str,
    left_keys: set[str],
    row_matching: dict[str, Any],
) -> bool:
    if not (row_matching.get("hierarchy_fields") or row_matching.get("identity_prefixes_to_ignore")):
        return False
    stripped = str(raw_key or "").strip()
    if stripped.startswith(("其中：", "其中:")):
        stripped = stripped[3:].strip()
    sub_norm = _search_norm(stripped)
    parent = _ASSET_SUBITEM_PARENTS.get(sub_norm) or _ASSET_SUBITEM_PARENTS.get(norm_key)
    if not parent:
        return False
    return _normalize_structured_row_key(parent, row_matching) in left_keys


def _structured_extra_reference_keys(
    rule: dict[str, Any],
    target_fields: dict[str, Any] | None,
    reference_fields: dict[str, Any] | None,
) -> list[str]:
    """Return non-empty reference keys absent from target when superset is disallowed."""
    row_matching = (rule.get("execution_plan") or {}).get("row_matching") or {}
    if row_matching.get("reference_superset_allowed", True):
        return []
    if not isinstance(target_fields, dict) or not isinstance(reference_fields, dict):
        return []
    fields = (rule.get("execution_plan") or {}).get("comparison", {}).get("fields", [])
    dash_values = {"", "-", "—", "–"}
    extras_by_norm: dict[str, str] = {}
    for field_spec in fields:
        key = field_spec.get("field") or field_spec.get("label")
        label = field_spec.get("label")
        left_entry = target_fields.get(key) if key in target_fields else target_fields.get(label)
        right_entry = reference_fields.get(key) if key in reference_fields else reference_fields.get(label)
        if not isinstance(left_entry, dict) or not isinstance(right_entry, dict):
            continue
        left_keys = {
            _normalize_structured_row_key(str(k), row_matching)
            for k in left_entry if str(k).strip()
        }
        for right_key, right_value in right_entry.items():
            right_key_str = str(right_key).strip()
            if not right_key_str or str(right_value).strip() in dash_values:
                continue
            norm_key = _normalize_structured_row_key(right_key_str, row_matching)
            if norm_key not in left_keys and not _is_covered_asset_subitem(right_key_str, norm_key, left_keys, row_matching):
                extras_by_norm.setdefault(norm_key, right_key_str)
    return [extras_by_norm[key] for key in sorted(extras_by_norm)]


_CHART_CUTOFF_RE = re.compile(
    r"(?:数据)?截止(?:日期|日)\s*[：:]?\s*(20\d{2})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})\s*日?"
)
_BARE_YMD_DATE_RE = re.compile(
    r"(20\d{2})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})\s*日?"
)


def _extract_dates_from_text(text: str | None, *, require_cutoff_label: bool = True) -> set[date]:
    raw = unicodedata.normalize("NFKC", str(text or ""))
    if not raw:
        return set()
    pattern = _CHART_CUTOFF_RE if require_cutoff_label else _BARE_YMD_DATE_RE
    found: set[date] = set()
    for match in pattern.finditer(raw):
        try:
            found.add(date(int(match.group(1)), int(match.group(2)), int(match.group(3))))
        except ValueError:
            continue
    return found


def _extract_chart_cutoff_date(
    *,
    summary_fields: dict[str, Any] | None,
    summary_value: str | None,
    summary_evidence: list[dict[str, Any]],
    summary_candidates: list[dict[str, Any]],
    section_markers: tuple[str, ...],
) -> date | None:
    """Resolve the unambiguous chart data cutoff date from verified summary facts or OCR."""
    found: set[date] = set()
    cutoff_raw = (
        summary_fields.get("data_cutoff_date") or summary_fields.get("date")
        if isinstance(summary_fields, dict)
        else None
    )
    if cutoff_raw:
        found.update(_extract_dates_from_text(str(cutoff_raw), require_cutoff_label=False))
    if not found and summary_value:
        found.update(_extract_dates_from_text(summary_value, require_cutoff_label=True))
    if not found:
        ocr_records = [ev for ev in summary_evidence if ev.get("source_kind") == "image_ocr"]
        other_records = [ev for ev in summary_evidence if ev.get("source_kind") != "image_ocr"]
        for group in (ocr_records, other_records):
            for record in group:
                for key in ("raw_text", "cell_text", "row_text"):
                    found.update(_extract_dates_from_text(record.get(key), require_cutoff_label=True))
            if found:
                break
    if not found:
        for source in summary_candidates:
            if source.get("source_kind") != "image_ocr":
                continue
            section_text = " ".join(str(part) for part in (source.get("section_path") or []))
            raw_text = str(source.get("raw_text") or "")
            if any(marker in section_text or marker in raw_text for marker in section_markers):
                found.update(_extract_dates_from_text(raw_text, require_cutoff_label=True))
    if len(found) == 1:
        return next(iter(found))
    return None


def _nearest_year_end(value: date) -> date:
    """Return the most recent calendar year-end on or before the given date."""
    return value if (value.month, value.day) == (12, 31) else date(value.year - 1, 12, 31)


def _apply_chart_cutoff_year_consistency(
    results: list[dict[str, Any]],
    chart_cutoffs: dict[str, date | None],
) -> None:
    """Mark both chart items UNRESOLVED if (3)'s year differs from (2)'s nearest year-end."""
    upper_date = chart_cutoffs.get("asset_allocation_chart")
    lower_date = chart_cutoffs.get("annual_nav_performance_chart")
    if not upper_date or not lower_date:
        return
    expected_year_end = _nearest_year_end(upper_date)
    if lower_date.year == expected_year_end.year:
        return
    reason = (
        f"「(二) 投资组合资产配置图」的数据截止日期为 {upper_date.isoformat()}"
        f"（对应的最近一个年末为 {expected_year_end.isoformat()}，年份为 {expected_year_end.year}），"
        f"而「(三) 每年净值增长率及业绩比较基准图」的数据截止日期为 {lower_date.isoformat()}"
        f"（年份为 {lower_date.year}），不符合下方图表年份应与上方图表最近一个年末年份相同的要求，"
        "两侧图表核对结果均标记为待复核。"
    )
    boilerplate_reasons = {
        "双方原文及引用已核验，并按规则声明的比较口径判定一致。",
        "双方原文及引用已核验，并按规则声明的比较口径判定存在差异。",
    }
    for item in results:
        if item.get("item_id") not in {"asset_allocation_chart", "annual_nav_performance_chart"}:
            continue
        if item.get("status") not in {"MATCH", "MISMATCH", "UNRESOLVED"}:
            continue
        item["status"] = "UNRESOLVED"
        item["comparison_status"] = "UNRESOLVED"
        item["reason_code"] = "CHART_CUTOFF_YEAR_INCONSISTENT"
        existing = str(item.get("reason") or "").strip()
        if not existing or existing in boilerplate_reasons:
            updated_reason = reason
        elif reason not in existing:
            updated_reason = f"{existing} {reason}"
        else:
            updated_reason = existing
        item["reason"] = updated_reason
        if isinstance(item.get("reference"), dict):
            item["reference"]["status"] = "UNRESOLVED"
            item["reference"]["reason"] = updated_reason


def _compare_structured_entry(
    field_spec: dict[str, Any],
    left_entry: Any,
    right_entry: Any,
    model_status: str | None,
    *,
    reference_superset_allowed: bool = True,
    row_matching: dict[str, Any] | None = None,
) -> str:
    if isinstance(left_entry, dict) and isinstance(right_entry, dict):
        matching = row_matching or {}
        left_map = {
            _normalize_structured_row_key(str(k), matching): str(v).strip()
            for k, v in left_entry.items() if str(k).strip()
        }
        right_map: dict[str, str] = {}
        for k, v in right_entry.items():
            key_str = str(k).strip()
            if not key_str:
                continue
            norm_key = _normalize_structured_row_key(key_str, matching)
            if norm_key not in left_map and _is_covered_asset_subitem(key_str, norm_key, set(left_map), matching):
                continue
            right_map[norm_key] = str(v).strip()
        dash_values = {"", "-", "—", "–"}
        common_keys = set(left_map) & set(right_map)
        extra_left = {k for k in set(left_map) - common_keys if left_map[k] not in dash_values}
        extra_right = {k for k in set(right_map) - common_keys if right_map[k] not in dash_values}
        if not common_keys:
            return "UNRESOLVED"
        for key in sorted(common_keys):
            entry_status = _compare_scalar_entry(field_spec, left_map[key], right_map[key], model_status)
            if entry_status != "MATCH":
                return entry_status
        if not extra_left and not extra_right:
            return "MATCH"
        if extra_right and not reference_superset_allowed:
            return "UNRESOLVED"
        if model_status == "MISMATCH":
            return "MISMATCH"
        if model_status == "MATCH":
            return "MATCH"
        return "UNRESOLVED"
    if isinstance(left_entry, list) and isinstance(right_entry, list) and left_entry and len(left_entry) == len(right_entry):
        for l_val, r_val in zip(left_entry, right_entry):
            entry_status = _compare_scalar_entry(field_spec, str(l_val), str(r_val), model_status)
            if entry_status != "MATCH":
                return entry_status
        return "MATCH"
    return _compare_scalar_entry(field_spec, str(left_entry), str(right_entry), model_status)


def _compare_by_execution_plan(
    rule: dict[str, Any], target_value: str, reference_value: str,
    model_status: str | None, applicability_status: str | None = None,
    review_conditions_clear: bool = True, declared_context_clear: bool = True,
    *,
    target_fields: dict[str, Any] | None = None,
    reference_fields: dict[str, Any] | None = None,
) -> str:
    """Execute the fact operations declared by this summary rule plan."""
    # Compare values only after source applicability, declared context, and
    # every required review condition have passed.
    if applicability_status == "UNRESOLVED" or not review_conditions_clear or not declared_context_clear:
        return "UNRESOLVED"
    fields = (rule.get("execution_plan") or {}).get("comparison", {}).get("fields", [])
    row_matching = (rule.get("execution_plan") or {}).get("row_matching") or {}
    reference_superset_allowed = bool(row_matching.get("reference_superset_allowed", True))
    if not fields:
        return "UNRESOLVED"
    if isinstance(target_fields, dict) and isinstance(reference_fields, dict) and target_fields and reference_fields:
        saw_required = False
        for field_spec in fields:
            key = field_spec.get("field") or field_spec.get("label")
            label = field_spec.get("label")
            left_entry = target_fields.get(key) if key in target_fields else target_fields.get(label)
            right_entry = reference_fields.get(key) if key in reference_fields else reference_fields.get(label)
            if left_entry in (None, "") or right_entry in (None, ""):
                if field_spec.get("required", True):
                    return "UNRESOLVED"
                continue
            saw_required = True
            field_status = _compare_structured_entry(
                field_spec, left_entry, right_entry, model_status,
                reference_superset_allowed=reference_superset_allowed,
                row_matching=row_matching,
            )
            if field_status != "MATCH":
                return field_status
        if saw_required:
            return "MATCH"
    if len(fields) > 1:
        # Multiple facts are extracted together with their source labels.
        # The model may resolve their declared semantic/text relationships,
        # but deterministic numeric/date/identifier comparisons require
        # field-separated values and therefore fail closed here.
        if all(field.get("operation") in {"text", "semantic"} for field in fields):
            return model_status if model_status in {"MATCH", "MISMATCH"} else "UNRESOLVED"
        return "UNRESOLVED"
    return compare_declared_field(
        fields[0], {"raw_text": target_value}, {"raw_text": reference_value},
        semantic_status=model_status,
    )


_TECHNICAL_REASON_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?:报告|主来源)?\s*coverage\s*(?:为|是|:|：)?\s*batch_count\s*[=：:]?\s*0", re.IGNORECASE), "未定位到可核对的参考章节正文"),
    (re.compile(r"coverage\s*批次数\s*(?:为|是|:|：)?\s*0", re.IGNORECASE), "未定位到可核对的参考章节正文"),
    (re.compile(r"batch_count\s*[=：:]?\s*0", re.IGNORECASE), "未定位到可核对的参考章节正文"),
    (re.compile(r"coverage[_\s-]*status", re.IGNORECASE), "章节覆盖状态"),
    (re.compile(r"\bcoverage\b", re.IGNORECASE), "章节覆盖范围"),
    (re.compile(r"\bbatch_count\b", re.IGNORECASE), "候选批次"),
    (re.compile(r"\blogical_units?\b", re.IGNORECASE), "章节内容单元"),
    (re.compile(r"\bsource_ids?\b", re.IGNORECASE), "原文证据"),
    (re.compile(r"\bmetadata_refs?\b", re.IGNORECASE), "文档元数据"),
    (re.compile(r"\bperiodic_report\.sent_date\b", re.IGNORECASE), "定期报告送出日"),
    (re.compile(r"\bproduct_summary\.compiled_date\b", re.IGNORECASE), "概要编制日"),
    (re.compile(r"\bproduct_summary\.sent_date\b", re.IGNORECASE), "概要送出日"),
    (re.compile(r"\bprospectus\.version_date\b", re.IGNORECASE), "招募说明书版本日"),
    (re.compile(r"\bperiodic_report\.period_end\b", re.IGNORECASE), "定期报告期末"),
    (re.compile(r"\b(?:product_summary|periodic_report|prospectus)\.(?:fund_name|fund_code|fund_codes)\b", re.IGNORECASE), "文档基金信息"),
    (re.compile(r"\bCONFIRMED_ABSENT\b"), "已确认未披露"),
    (re.compile(r"\bNOT_FOUND(?:_IN_SCOPE)?\b"), "未找到"),
    (re.compile(r"\bNOT_APPLICABLE\b"), "不适用"),
    (re.compile(r"\bUNRESOLVED\b"), "待复核"),
    (re.compile(r"\bPARTIAL\b"), "部分覆盖"),
    (re.compile(r"\bCOMPLETE\b"), "完整覆盖"),
    (re.compile(r"\bUNKNOWN\b"), "未知"),
)


def _humanize_user_reason(reason: str | None) -> str:
    """Translate any leaked internal field names or enums in model reasons into plain Chinese."""
    text = str(reason or "").strip()
    if not text:
        return ""
    for pattern, replacement in _TECHNICAL_REASON_REPLACEMENTS:
        text = pattern.sub(replacement, text)
    text = re.sub(r"[（(]\s*[、,，;；\s]*[)）]", "", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return text


def _reason_for_final_status(
    status: str, existing_reason: str | None = None, *,
    model_status: str | None = None, applicability_status: str | None = None,
    review_conditions_clear: bool = True, declared_context_clear: bool = True,
) -> str:
    """Keep user-facing explanation aligned with the validated final status."""
    if status == "MATCH":
        return "双方原文及引用已核验，并按规则声明的比较口径判定一致。"
    if status == "MISMATCH":
        return "双方原文及引用已核验，并按规则声明的比较口径判定存在差异。"
    if status == "UNRESOLVED" and model_status in {"MATCH", "MISMATCH"}:
        if not review_conditions_clear:
            return "模型给出了比较结论，但规则要求的复核条件或证据引用未全部核验通过，保留待复核。"
        if not declared_context_clear:
            return "模型给出了比较结论，但所选来源的上下文条件未全部通过，保留待复核。"
        if applicability_status == "UNRESOLVED":
            return "模型给出了比较结论，但参考来源适用性尚未确认，保留待复核。"
    return _humanize_user_reason(existing_reason) or "规则未能确认可比较的参考事实。"


async def compare_product_summary(
    *, documents: dict[str, dict[str, Any]], provider: Any,
    source_catalogues: dict[str, dict[str, Any]] | None = None,
    trace: Callable[[dict[str, Any]], None] | None = None,
    progress: Callable[[str, str, str], None] | None = None,
    historical: bool = True,
    rule_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Associate fields by business meaning, then validate model citations against PDF sources."""
    active_rules = rule_snapshot or capture_product_summary_rule_snapshot()
    if active_rules.get("scope") != "product_summary":
        raise ValueError("PRODUCT_SUMMARY_RULE_SNAPSHOT_INVALID")
    rule_config = active_rules.get("rule_config")
    rules = active_rules.get("rules")
    if not isinstance(rule_config, dict) or not isinstance(rules, list):
        raise ValueError("PRODUCT_SUMMARY_RULE_SNAPSHOT_INVALID")
    rule_version = str(active_rules["rules_version"])
    del historical  # Applicability is declared in Markdown and interpreted from its plan.
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
        trace({"kind": "artifact", "path": "rules/rule_snapshot.json", "content": {
            "scope": active_rules.get("scope"),
            "rules_version": active_rules.get("rules_version"),
            "protocol_version": active_rules.get("protocol_version"),
            "prompt_version": active_rules.get("prompt_version"),
            "files": active_rules.get("files", {}),
        }})
        if source_catalogues is None:
            for role, catalogue in catalogues.items():
                trace({"kind": "artifact", "document_role": role, "path": f"{role}/source_catalogue.json", "content": catalogue})
    if progress and source_catalogues is None:
        progress("source_index", "completed", "章节证据目录已建立")

    items = []
    warnings = []
    for item in rules:
        execution_plan = item.get("execution_plan")
        source_selection = execution_plan.get("source_selection") if isinstance(execution_plan, dict) else None
        if not source_selection or not source_selection.get("primary_role"):
            raise ValueError(f"RULE_PLAN_PRIMARY_SOURCE_MISSING: {item['item_id']}")
        target_role = execution_plan.get("target_role")
        if target_role not in catalogues:
            raise ValueError(f"RULE_TARGET_DOCUMENT_MISSING: {item['item_id']}:{target_role}")
        primary = _source_candidates(catalogues[target_role], item)
        role = source_selection["primary_role"]
        primary_reference = _source_candidates(catalogues[role], item) if role in catalogues else []
        planned_fallbacks = (source_selection or {}).get("fallbacks") or []
        fallback_role = planned_fallbacks[0].get("role") if planned_fallbacks else None
        fallback_candidates = _source_candidates(catalogues[fallback_role], item) if fallback_role in catalogues else []
        item_payload = {
            "item_id": item["item_id"], "key": item["key"], "label": item["label"],
            "module_id": item["module_id"],
            "meaning": item.get("business_rule_markdown"),
            "business_rule_markdown": item.get("business_rule_markdown"),
            "execution_plan": execution_plan,
            "primary_role": role,
            "fallback_role": fallback_role,
            "document_metadata": _metadata_facts_for_roles(documents, {target_role, role}),
            "metadata_refs": sorted(_available_metadata_reference_ids(documents, {target_role, role})),
            "planned_fallbacks": planned_fallbacks,
            "source_selection": source_selection,
            "primary_lookup_status": "CANDIDATES_FOUND" if primary_reference else "NO_CANDIDATES_UNCONFIRMED",
            "summary_candidates": primary,
            "primary_candidates": primary_reference,
            "fallback_candidates": fallback_candidates,
        }
        for side, source_role in (("summary", target_role), ("primary", role), ("fallback", fallback_role)):
            catalogue = catalogues.get(source_role, {})
            plan = _context_plan(catalogue, item_payload[f"{side}_candidates"], item=item_payload)
            item_payload[f"{side}_context_plan"] = plan
            by_id = {source["source_id"]: source for page in catalogue.get("pages", []) for source in page.get("sources", [])}
            item_payload[f"{side}_candidates"] = [by_id[sid] for sid in plan["source_ids"] if sid in by_id]
        items.append(item_payload)
    if trace:
        trace({"kind": "artifact", "path": "comparison/chapter_plan.json", "content": [
            {"item_id": item["item_id"], "label": item["label"], "module_id": item["module_id"],
             "target_section_path": item["execution_plan"]["search_intent"].get("preferred_sections_by_role", {}).get(
                 item["execution_plan"]["target_role"],
                 item["execution_plan"]["search_intent"].get("preferred_sections", []),
             ),
             "sources": {side: {"role": role,
                                "chapters": [{key: section[key] for key in ("section_id", "title", "path")}
                                             for section in _prescribed_sections(catalogues.get(role, {}), item)],
                                "planned_batches": len(item[f"{side}_context_plan"]["batches"]),
                                "source_count": item[f"{side}_context_plan"]["source_count"]}
                         for side, role in (("summary", item["execution_plan"]["target_role"]), ("primary", item["primary_role"]),
                                            ("fallback", item["fallback_role"]))}}
            for item in items]})
    if progress:
        progress("primary_lookup", "running", "初步定位完成，正在核实候选未命中的指标")

    system = (
        "你是文档核对的通用事实抽取器。业务含义、来源次序、范围、比较要求和待复核条件只来自每个项目携带的中文规则及其执行计划；不得从历史习惯或其他项目推导业务政策。"
        "只使用输入提供的章节、来源记录和逻辑表上下文。来源记录的source_id是唯一可引用依据；不得编造或改写来源ID、原文或坐标。跨页逻辑单元仅用于理解上下文，引用仍须指向原始source_id。"
        "filename_date_*是从文件名开头规范日期读取的独立候选元数据，不等同PDF内文日期；仅在对应中文规则允许、且与文件内容相容时作为辅助依据，并用metadata_refs引用，不能覆盖PDF内文中的相反日期。version_date_*是预检根据PDF内可确认版本日期或符合PDF日期范围的文件名日期归并出的招募说明书版本日期事实；仅在该项中文规则允许使用版本日期时引用，并用metadata_refs引用。"
        "日期条件须按规则声明的精度比较。若规则允许日期精确到日并规定同日可用，且两日期元数据均精确到日、日期值相同，则该日期条件为CLEAR；不得因缺少日内时分或无法推断先后而标UNRESOLVED。若日期精度不同、值不同或存在相反原文证据，仍按中文规则处理。"
        "逐项返回输入中的item_id。summary_value/value/primary_value只写直接摘自原文的事实值，严禁写入自行加总或推算的数字（例如‘分项合计100%’），算术验算和说明统一放入reason。字段值必须由所引用的原文（含所属表格表头/单位与可见文档元数据）直接支持；标签和值分开时应引用能共同证明二者关系的来源记录。候选未命中、证据不足、分包未覆盖或引用无法核实，都不能当作确认未披露。"
        "如果规则execution_plan.comparison.fields声明了多个字段或需要拆分日期、数值、年份/类别明细，请同时按fields[*].field名填充summary_fields和references[*].fields（如{\"data_cutoff_date\":\"2025-12-31\",\"percentage\":{\"权益投资\":\"99.13%\"}}）；若日期由表格标题与输入提供的结构化文档元数据（如periodic_report.period_end）共同支持，亦须一并填入data_cutoff_date等对应日期字段；当表头/单位为（%）时为比例数值统一保留%；子项字典只填入双方同一层级的实际披露分项，忽略合计行与占比为‘-’的空行；所有必需字段一致才可总体MATCH，确认任一必需字段冲突才可总体MISMATCH，否则UNRESOLVED。"
        "若参考章节已完整查看但内容确不含规则要求的可比口径（如季度报告仅披露滚动区间收益率、缺少自然年度拆分数据），将coverage_status设为NOT_FOUND并在reason中说明原因。"
        "逐条检查每项自身execution_plan.review_conditions中的N个复核条件，并按原顺序严格只返回这N条（condition_index严格为0至N-1，严禁多返回超出N-1的索引或借用同批其他核对项的条件）。复核条件由PDF原文支持时标CLEAR并提供有效source_ids；复核条件由输入提供的结构化文档元数据支持时标CLEAR，并在metadata_refs中引用准确的‘文档角色.字段名’，例如periodic_report.sent_date；元数据中的*_quote字段是该元数据事实对应的PDF原文引文。两者都可提供。metadata_refs只能引用当前输入中实际提供的字段。严禁在source_ids与metadata_refs都为空时标CLEAR；无法给出有效引用时必须改标UNRESOLVED。若条件仅由已提供且可引用的文档元数据决定，不得仅因其不是普通source_id而标REVIEW_REQUIRED。该条件明确属于本次未采用的来源或未发生的流程分支时标NOT_APPLICABLE，source_ids和metadata_refs均留空并说明分支依据；条件触发或证据不足分别使用REVIEW_REQUIRED或UNRESOLVED。不得把‘未发现相关信息’当作条件已满足。只要任一复核条件既非CLEAR也非NOT_APPLICABLE，适用性必须为UNRESOLVED，比较结论不得为MATCH或MISMATCH。"
        "目标事实和参考事实均须分别提取；仅在执行计划声明的比较要求得到证据支持时，才返回MATCH或MISMATCH。明确缺失、证据不足及无法判断应使用不同状态并说明原因。"
        "所有面向业务用户展示的reason字段（包含顶层、references与review_condition_assessments中的reason）必须用自然流畅的中文业务语言说明依据，严禁出现任何英文字段名、程序变量名、技术术语或英文枚举值（例如coverage、batch_count、source_id、metadata_refs、logical_units、periodic_report.sent_date、FOUND、UNRESOLVED、PARTIAL、UNKNOWN等）。"
        "严格按提示给出的JSON结构返回，不要输出Markdown。"
    )
    if trace:
        trace({"kind": "artifact", "path": "comparison/model_system_prompt.txt", "content": system})
    schema = {
        "items": [{"item_id": "stable-rule-id", "summary_value": "原文值或摘要", "summary_fields": {}, "summary_source_ids": [], "references": [{"role": "declared reference role", "status": "MATCH|MISMATCH|UNRESOLVED|SOURCE_MISSING", "applicability_status": "CONFIRMED|NOT_REQUIRED|UNRESOLVED", "coverage_status": "COMPLETE|PARTIAL|NOT_FOUND|UNKNOWN", "uncovered_aspects": [], "review_condition_assessments": [{"condition_index": 0, "status": "CLEAR|NOT_APPLICABLE|REVIEW_REQUIRED|UNRESOLVED", "source_ids": [], "metadata_refs": [], "reason": "简短依据"}], "value": "原文值或摘要", "fields": {}, "source_ids": [], "reason": "简短依据"}], "reason": "简短依据"}]
    }
    # Extract every available target fact even when lexical matching found no
    # primary-reference candidate. Source routing happens afterward; otherwise a
    # missing report keyword can prevent the target side from being captured.
    eligible_items = [
        item for item in items
        if item["summary_candidates"]
    ]
    groups = module_groups(eligible_items)
    known_item_ids = {item["item_id"] for item in items}
    started = time.monotonic()

    async def analyze_group(group_id: str, group_items: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]], str | None]:
        if progress:
            progress(group_id, "running", "正在核对" + group_id.replace("_", " "))
        found_by_id: dict[str, dict[str, Any]] = {}
        batch_errors: list[str] = []
        rounds = max((len(item[f"{side}_context_plan"]["batches"])
                      for item in group_items for side in ("summary", "primary")), default=0)

        def _build_round_requests(round_index: int) -> list[dict[str, Any]]:
            requests: list[dict[str, Any]] = []
            for item in group_items:
                previous = found_by_id.get(item["item_id"], {})
                target_role = item["execution_plan"].get("target_role")
                target = _verified_fact(
                    previous.get("summary_value"), previous.get("summary_source_ids"),
                    item["summary_candidates"], item,
                    field_values=previous.get("summary_fields"),
                    extra_texts=_role_grounding_texts(item, target_role),
                )
                reference = next((ref for ref in previous.get("references", [])
                                  if ref.get("role") == item["primary_role"]), {})
                primary_fact = _verified_fact(
                    reference.get("value"), reference.get("source_ids"),
                    item["primary_candidates"], item,
                    field_values=reference.get("field_values") or reference.get("fields"),
                    extra_texts=_role_grounding_texts(item, item["primary_role"]),
                )
                if target and primary_fact:
                    continue
                payload = {key: item[key] for key in (
                    "item_id", "key", "label", "business_rule_markdown", "execution_plan",
                    "primary_role", "document_metadata", "metadata_refs",
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
            return requests

        async def run_batch(round_index: int, batch_index: int, batch: list[dict[str, Any]]) -> tuple[int, list[dict[str, Any]], str | None]:
            shared = _shared_context(batch)
            prompt = (
                f"逐项处理本组 {len(batch)} 个核对项，完整返回每个item_id。业务含义、来源要求和比较方法仅依据该项中文规则与执行计划。"
                "当前包的coverage说明分包范围；未提供的单元不代表未披露。已核验的verified_target和verified_primary_reference是锁定事实，必须原样保留。"
                "候选未命中、证据不足或引用无法核验时返回UNRESOLVED，不得推断为未披露。\nJSON结构：\n"
                + json.dumps(schema, ensure_ascii=False) + "\n字段与证据包：\n"
                + json.dumps(shared if "context_pool" in shared else batch, ensure_ascii=False)
            )
            stage = f"{group_id}/round_{round_index + 1}/batch_{batch_index + 1}"
            if trace:
                trace({"kind": "artifact", "path": f"comparison/{stage}/model_request.json",
                       "content": {"rules_version": rule_version, "prompt": prompt}})
            last_error = None
            parsed_records: list[dict[str, Any]] = []
            for attempt in range(2):
                try:
                    response = await _generate_model_text(provider, system, prompt, stage=stage, trace=trace)
                    if trace:
                        trace({"kind": "artifact", "path": f"comparison/{stage}/model_response.txt", "content": response})
                    for value in _parse_json(response).get("items", []):
                        found = _normalize_model_item_id(value) if isinstance(value, dict) else None
                        request = next((request for request in batch if found and request["item_id"] == found["item_id"]), None)
                        if not request:
                            continue
                        source_item = next(item for item in group_items if item["item_id"] == found["item_id"])
                        target = request["verified_target"]
                        if target:
                            found.update(summary_value=target["value"], summary_source_ids=target["source_ids"])
                            if target.get("field_values") is not None:
                                found["summary_fields"] = target["field_values"]
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
                        parsed_records.append(found)
                    last_error = None
                    break
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    if trace:
                        trace({"kind": "event", "event": "summary_compare_group_retry",
                               "details": {"group": group_id, "attempt": attempt + 1, "error": last_error}})
            return round_index, parsed_records, last_error

        def _merge_batch_records(outputs: list[tuple[int, list[dict[str, Any]], str | None]]) -> None:
            for _, records, err in sorted(outputs, key=lambda row: row[0]):
                if err:
                    batch_errors.append(err)
                for found in records:
                    item_id = found["item_id"]
                    source_item = next(item for item in group_items if item["item_id"] == item_id)
                    previous = found_by_id.get(item_id, {})
                    if not previous:
                        found_by_id[item_id] = found
                        continue
                    merged = {**previous, **found}
                    target_role = source_item["execution_plan"].get("target_role")
                    prev_target = _verified_fact(
                        previous.get("summary_value"), previous.get("summary_source_ids"),
                        source_item["summary_candidates"], source_item,
                        field_values=previous.get("summary_fields"),
                        extra_texts=_role_grounding_texts(source_item, target_role),
                    )
                    new_target = _verified_fact(
                        found.get("summary_value"), found.get("summary_source_ids"),
                        source_item["summary_candidates"], source_item,
                        field_values=found.get("summary_fields"),
                        extra_texts=_role_grounding_texts(source_item, target_role),
                    )
                    if prev_target and not new_target:
                        merged.update(summary_value=prev_target["value"], summary_source_ids=prev_target["source_ids"])
                        if prev_target.get("field_values") is not None:
                            merged["summary_fields"] = prev_target["field_values"]
                    prev_ref = next((ref for ref in previous.get("references", []) if isinstance(ref, dict) and ref.get("role") == source_item["primary_role"]), {})
                    new_ref = next((ref for ref in found.get("references", []) if isinstance(ref, dict) and ref.get("role") == source_item["primary_role"]), {})
                    prev_primary = _verified_fact(
                        prev_ref.get("value"), prev_ref.get("source_ids"),
                        source_item["primary_candidates"], source_item,
                        field_values=prev_ref.get("field_values") or prev_ref.get("fields"),
                        extra_texts=_role_grounding_texts(source_item, source_item["primary_role"]),
                    )
                    new_primary = _verified_fact(
                        new_ref.get("value"), new_ref.get("source_ids"),
                        source_item["primary_candidates"], source_item,
                        field_values=new_ref.get("field_values") or new_ref.get("fields"),
                        extra_texts=_role_grounding_texts(source_item, source_item["primary_role"]),
                    )
                    if prev_primary and not new_primary:
                        merged["references"] = [prev_ref]
                    found_by_id[item_id] = merged

        if rounds > 0:
            round_0_requests = _build_round_requests(0)
            round_0_outputs = await asyncio.gather(*(
                run_batch(0, batch_index, batch)
                for batch_index, batch in enumerate(_payload_batches(round_0_requests))
            ))
            _merge_batch_records(list(round_0_outputs))
        if rounds > 1:
            # Run all remaining unverified batches across rounds 2..N concurrently
            # rather than waiting round-by-round serially.
            remaining_tasks = []
            for round_index in range(1, rounds):
                round_requests = _build_round_requests(round_index)
                for batch_index, batch in enumerate(_payload_batches(round_requests)):
                    remaining_tasks.append(run_batch(round_index, batch_index, batch))
            if remaining_tasks:
                remaining_outputs = await asyncio.gather(*remaining_tasks)
                _merge_batch_records(list(remaining_outputs))

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
        target_role = item["execution_plan"].get("target_role")
        summary_ids = model_item.get("summary_source_ids") if isinstance(model_item.get("summary_source_ids"), list) else []
        summary_evidence = [valid_sources[source_id] for source_id in summary_ids if source_id in valid_sources and source_id in {source["source_id"] for source in item["summary_candidates"]}]
        summary_value = str(model_item.get("summary_value") or "")
        summary_extra = _role_grounding_texts(item, target_role)
        summary_fact = _verified_fact(
            summary_value, summary_ids, item["summary_candidates"], item,
            field_values=model_item.get("summary_fields"), extra_texts=summary_extra,
        )
        summary_valid = bool(summary_fact)
        refs = {ref.get("role"): ref for ref in model_item.get("references", []) if isinstance(ref, dict)}
        primary_ref = refs.get(item["primary_role"], {})
        primary_ids = primary_ref.get("source_ids") if isinstance(primary_ref.get("source_ids"), list) else []
        primary_evidence = [valid_sources[source_id] for source_id in primary_ids if source_id in valid_sources and source_id in {source["source_id"] for source in item["primary_candidates"]}]
        primary_value = str(primary_ref.get("value") or "")
        primary_extra = _role_grounding_texts(item, item["primary_role"])
        primary_fact = _verified_fact(
            primary_value, primary_ids, item["primary_candidates"], item,
            field_values=primary_ref.get("field_values") or primary_ref.get("fields"), extra_texts=primary_extra,
        )
        primary_valid = bool(primary_fact)
        if not summary_valid or not primary_valid:
            grounding_hints = []
            if not summary_valid and summary_evidence:
                missing_sum = sorted(_ungrounded_number_tokens(summary_value, summary_evidence, extra_texts=summary_extra))
                if missing_sum:
                    grounding_hints.append(f"概要侧未在引用原文中找到的数字：{missing_sum}")
            if not primary_valid and primary_evidence:
                missing_pri = sorted(_ungrounded_number_tokens(primary_value, primary_evidence, extra_texts=primary_extra))
                if missing_pri:
                    grounding_hints.append(f"主参考侧未在引用原文中找到的数字：{missing_pri}")
            preferred_summary_ids = summary_ids if not summary_valid else []
            preferred_primary_ids = primary_ids if not primary_valid else []
            recovery_items.append({
                key: item[key] for key in ("item_id", "key", "label", "primary_role", "business_rule_markdown", "execution_plan")
            } | {
                "document_metadata": item["document_metadata"],
                "failed_sides": [side for side, valid in (("target", summary_valid), ("primary_reference", primary_valid)) if not valid],
                "grounding_hints": grounding_hints,
                "summary_candidates": _recovery_candidates(item, "summary", preferred_summary_ids) if not summary_valid else [],
                "primary_candidates": _recovery_candidates(item, "primary", preferred_primary_ids) if not primary_valid else [],
                "summary_coverage": item.get("summary_excerpt_coverage"),
                "primary_coverage": item.get("primary_excerpt_coverage"),
                "summary_logical_units": item.get("summary_repair_units", []),
                "primary_logical_units": item.get("primary_repair_units", []),
                "verified_target": summary_fact if summary_valid else None,
                "verified_primary_reference": ({**primary_fact, "status": primary_ref.get("status")} if primary_valid and primary_fact else None),
            })

    async def _run_recovery_batch(recovery_index: int, recovery_batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
        recovery_prompt = (
            "只恢复failed_sides列出的失败侧字段证据。verified_target或verified_primary_reference存在时视为锁定事实，必须原样保留其中值和source_id，不能重新提取或改写。候选包含原文、bbox和完整逻辑单元；标签和值可能分处不同单元格或不同行，必须引用标签与对应值的原始source_id，不能选择相邻的其他指标。"
            "若grounding_hints提示前次存在未落入原文的数字，必须把自行加总或推算数字移入reason，只在value/fields保留原文实际出现的事实并补齐表头/单位source_id。"
            "恢复后有有效事实即可返回；事实无法确认则保持该侧空值，并将比较状态设为UNRESOLVED。禁止返回SOURCE_MISSING或确认未披露。不得查看补充招募说明书。"
            "只恢复规则声明的目标事实、参考事实和比较状态；引用值必须由对应原文直接支持。严格返回JSON：{\"items\":[{\"item_id\":\"stable-rule-id\",\"summary_value\":\"\",\"summary_fields\":{},\"summary_source_ids\":[],\"primary_value\":\"\",\"primary_fields\":{},\"primary_source_ids\":[],\"comparison_status\":\"MATCH|MISMATCH|UNRESOLVED\",\"reason\":\"\"}]}\n"
            + json.dumps(_shared_context(recovery_batch), ensure_ascii=False)
        )
        if trace:
            trace({"kind": "artifact", "path": f"comparison/evidence_recovery/batch_{recovery_index}/model_request.json", "content": {"prompt": recovery_prompt, "item_count": len(recovery_batch)}})
        updates: list[dict[str, Any]] = []
        try:
            recovery_raw = await _generate_model_text(
                provider, system, recovery_prompt, stage="evidence_recovery", trace=trace,
            )
            if trace:
                trace({"kind": "artifact", "path": f"comparison/evidence_recovery/batch_{recovery_index}/model_response.txt", "content": recovery_raw})
            recovered = _parse_json(recovery_raw).get("items", [])
            recovered_by_id = {normalized["item_id"]: normalized for value in recovered if isinstance(value, dict)
                               if (normalized := _normalize_model_item_id(value)) is not None
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
                summary_evidence = [valid_sources[source_id] for source_id in summary_ids if source_id in valid_sources and source_id in summary_allowed and valid_sources[source_id]["document_role"] == item["execution_plan"]["target_role"]]
                summary_value = str((locked_target or {}).get("value") or found.get("summary_value") or "")
                summary_fields = (locked_target or {}).get("field_values") if locked_target else found.get("summary_fields")
                primary_ids = (locked_primary.get("source_ids") if locked_primary else
                               found.get("primary_source_ids") if isinstance(found.get("primary_source_ids"), list) else [])
                primary_allowed = set(locked_primary["source_ids"]) if locked_primary else {source["source_id"] for source in recovery_input["primary_candidates"]}
                primary_evidence = [valid_sources[source_id] for source_id in primary_ids if source_id in valid_sources and source_id in primary_allowed and valid_sources[source_id]["document_role"] == item["primary_role"]]
                primary_value = str((locked_primary or {}).get("value") or found.get("primary_value") or "")
                primary_fields = (locked_primary or {}).get("field_values") if locked_primary else (found.get("primary_fields") or found.get("fields"))
                target_valid = _verified_fact(
                    summary_value, summary_ids, summary_evidence, rule,
                    field_values=summary_fields,
                    extra_texts=_role_grounding_texts(item, item["execution_plan"]["target_role"]),
                )
                reference_valid = _verified_fact(
                    primary_value, primary_ids, primary_evidence, rule,
                    field_values=primary_fields,
                    extra_texts=_role_grounding_texts(item, item["primary_role"]),
                )
                if not target_valid and not reference_valid:
                    continue
                updates.append({
                    "item_id": item_id,
                    "target_valid": target_valid,
                    "reference_valid": reference_valid,
                    "locked_target": locked_target,
                    "locked_primary": locked_primary,
                    "found": found,
                })
        except Exception as exc:
            failed_groups.append({"group": "evidence_recovery", "error": f"{type(exc).__name__}: {exc}"})
            if trace:
                trace({"kind": "event", "event": "summary_evidence_recovery_failed", "details": {"error": f"{type(exc).__name__}: {exc}"}})
        return updates

    if recovery_items:
        recovery_batches_outputs = await asyncio.gather(*(
            _run_recovery_batch(recovery_index, recovery_batch)
            for recovery_index, recovery_batch in enumerate(_payload_batches(recovery_items), start=1)
        ))
        for batch_updates in recovery_batches_outputs:
            for upd in batch_updates:
                item_id = upd["item_id"]
                item = item_by_id[item_id]
                target_valid = upd["target_valid"]
                reference_valid = upd["reference_valid"]
                locked_target = upd["locked_target"]
                locked_primary = upd["locked_primary"]
                found = upd["found"]
                previous = next((value for value in raw_model_items if value.get("item_id") == item_id), {})
                prev_refs = [
                    r for r in (previous.get("references") or [])
                    if isinstance(r, dict) and r.get("role") == item["primary_role"]
                ]
                prev_ref = prev_refs[0] if prev_refs else {}
                if not target_valid or not reference_valid:
                    updated = {**previous, "item_id": item_id}
                    if target_valid:
                        updated.update(summary_value=target_valid["value"], summary_source_ids=target_valid["source_ids"])
                        if target_valid.get("field_values") is not None:
                            updated["summary_fields"] = target_valid["field_values"]
                    if reference_valid:
                        updated["references"] = [{"role": item["primary_role"], "status": "UNRESOLVED",
                                                  **reference_valid}]
                    raw_model_items = [value for value in raw_model_items if value.get("item_id") != item_id]
                    raw_model_items.append(updated)
                    continue
                status = found.get("comparison_status")
                status = status if status in {"MATCH", "MISMATCH", "UNRESOLVED"} else "UNRESOLVED"
                if any(locked and found.get(key) and _norm(str(found[key])) != _norm(locked["value"])
                       for locked, key in ((locked_target, "summary_value"), (locked_primary, "primary_value"))):
                    status = "UNRESOLVED"
                    found["reason"] = "局部恢复尝试改写已验证事实，比较结论未采用。"
                recovered_ref = {
                    "role": item["primary_role"],
                    "status": status,
                    "applicability_status": (
                        found.get("applicability_status")
                        or prev_ref.get("applicability_status")
                        or ("CONFIRMED" if status in {"MATCH", "MISMATCH"} else "UNRESOLVED")
                    ),
                    "coverage_status": found.get("coverage_status") or prev_ref.get("coverage_status") or "COMPLETE",
                    "uncovered_aspects": found.get("uncovered_aspects") if isinstance(found.get("uncovered_aspects"), list) else prev_ref.get("uncovered_aspects", []),
                    "review_condition_assessments": (
                        found.get("review_condition_assessments")
                        if isinstance(found.get("review_condition_assessments"), list)
                        else prev_ref.get("review_condition_assessments", [])
                    ),
                    **reference_valid,
                    "reason": str(found.get("reason") or "经局部证据恢复确认。"),
                }
                raw_model_items = [value for value in raw_model_items if value.get("item_id") != item_id]
                raw_model_items.append({
                    "item_id": item_id,
                    "summary_value": target_valid["value"],
                    "summary_source_ids": target_valid["source_ids"],
                    **({"summary_fields": target_valid["field_values"]} if target_valid.get("field_values") is not None else {}),
                    "references": [recovered_ref],
                    "reason": str(found.get("reason") or "经局部证据恢复确认。"),
                })
    # Lexical misses are not absence. Route only unresolved indicators to complete
    # logical sections, then inspect those sections with explicit coverage records.
    _bind_model_fact_sources(raw_model_items, item_by_id)
    raw_model_ids = {value["item_id"] for value in raw_model_items if value.get("item_id") in known_item_ids}
    source_missing_ids = {
        value["item_id"]
        for value in raw_model_items
        if value.get("item_id") in known_item_ids
        and any(ref.get("role") == item_by_id[value["item_id"]]["primary_role"] and ref.get("status") == "SOURCE_MISSING"
                for ref in value.get("references", []) if isinstance(ref, dict))
    }
    unresolved_primary_ids = set(known_item_ids) - raw_model_ids
    for value in raw_model_items:
        item_id = value.get("item_id")
        if item_id not in known_item_ids:
            continue
        rule = rule_by_id[item_id]
        item_cfg = item_by_id[item_id]
        refs = [ref for ref in value.get("references", [])
                if isinstance(ref, dict) and ref.get("role") == item_cfg["primary_role"]]
        ref = refs[0] if refs else {}
        source_ids = ref.get("source_ids") if isinstance(ref.get("source_ids"), list) else []
        primary_extra = _role_grounding_texts(item_cfg, item_cfg["primary_role"])
        verified_primary = _verified_fact(
            ref.get("value"), source_ids, item_cfg["primary_candidates"], rule,
            field_values=ref.get("field_values") or ref.get("fields"),
            extra_texts=primary_extra,
        )
        valid_fact = ref.get("status") in {"MATCH", "MISMATCH", "UNRESOLVED"} and bool(verified_primary)
        if valid_fact and verified_primary:
            ref["value"] = verified_primary["value"]
            ref["source_ids"] = verified_primary["source_ids"]
            if verified_primary.get("field_values") is not None:
                ref["field_values"] = verified_primary["field_values"]
        if not valid_fact:
            unresolved_primary_ids.add(item_id)
    missing_primary_items = [item for item in items if item["summary_candidates"]
                             and item["item_id"] in (unresolved_primary_ids | source_missing_ids)]
    if missing_primary_items:
        primary_roles = {item["primary_role"] for item in missing_primary_items}
        if len(primary_roles) != 1:
            raise ValueError("SUMMARY_PRIMARY_ROUTING_REQUIRES_ROLE_BATCHING")
        report_role = next(iter(primary_roles))
        report_catalogue = catalogues.get(report_role, {})
        if not report_catalogue:
            raise ValueError(f"SUMMARY_PRIMARY_SOURCE_MISSING: {report_role}")
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
            target_role = item["execution_plan"].get("target_role")
            target = _verified_fact(
                previous.get("summary_value"), previous.get("summary_source_ids"),
                item["summary_candidates"], item,
                field_values=previous.get("summary_fields"),
                extra_texts=_role_grounding_texts(item, target_role),
            )
            route_requests.append({"item_id": item["item_id"], "label": item["label"],
                                   "business_rule": item["business_rule_markdown"],
                                   "execution_plan": item["execution_plan"],
                                   "verified_target": target})
        if route_requests:
            directory_pages = _payload_batches([{key: value for key, value in section.items() if key != "source_ids"}
                                               for section in section_directory], budget=18_000)
            route_batches = _payload_batches(route_requests, budget=18_000)

            async def _run_route_batch(
                directory_index: int, directory: list[dict[str, Any]],
                batch_index: int, requests: list[dict[str, Any]],
            ) -> list[tuple[str, list[str]]]:
                route_prompt = (
                    "你负责为未定位的核对项选择参考文件中的完整章节。根据该项中文规则、执行计划和章节标题选择可能披露该事实的section_id，最多8个。"
                    "本次仅提供目录的一部分，未出现不代表未披露；不能输出目录外ID。只做路由，不提取事实、不比较、不确认缺失。"
                    "没有把握时返回空数组。严格返回JSON：{\"items\":[{\"item_id\":\"stable-rule-id\",\"section_ids\":[],\"reason\":\"\"}]}\n"
                    + json.dumps({"items": requests, "sections": directory, "directory_batch": directory_index + 1,
                                  "directory_batch_count": len(directory_pages)}, ensure_ascii=False)
                )
                found_routes: list[tuple[str, list[str]]] = []
                allowed = {section["section_id"] for section in directory}
                valid_req_ids = {request["item_id"] for request in requests}
                try:
                    if trace:
                        trace({"kind": "artifact",
                               "path": f"comparison/primary_section_routing/dir_{directory_index + 1}_batch_{batch_index + 1}/model_request.json",
                               "content": {"prompt": route_prompt}})
                    response = await _generate_model_text(provider, system, route_prompt, stage="primary_section_routing", trace=trace)
                    if trace:
                        trace({"kind": "artifact",
                               "path": f"comparison/primary_section_routing/dir_{directory_index + 1}_batch_{batch_index + 1}/model_response.txt",
                               "content": response})
                    for route in _parse_json(response).get("items", []):
                        found = _normalize_model_item_id(route) if isinstance(route, dict) else None
                        if not found or found.get("item_id") not in valid_req_ids:
                            continue
                        ids = route.get("section_ids") if isinstance(route.get("section_ids"), list) else []
                        valid_ids = [sid for sid in ids if isinstance(sid, str) and sid in allowed]
                        if valid_ids:
                            found_routes.append((found["item_id"], valid_ids))
                except Exception as exc:
                    failed_groups.append({"group": "primary_section_routing", "error": f"{type(exc).__name__}: {exc}"})
                return found_routes

            route_outputs = await asyncio.gather(*(
                _run_route_batch(directory_index, directory, batch_index, requests)
                for directory_index, directory in enumerate(directory_pages)
                for batch_index, requests in enumerate(route_batches)
            ))
            for pairs in route_outputs:
                for item_id, valid_ids in pairs:
                    routed[item_id].extend(valid_ids)
        searches = {}
        for item in missing_primary_items:
            examined_primary_ids = set(item.get("primary_examined_source_ids", ()))
            existing_ids = {source.get("section_id") for source in item["primary_candidates"]}
            if routed[item["item_id"]]:
                selected = list(dict.fromkeys(routed[item["item_id"]]))
            else:
                selected = [
                    section["section_id"]
                    for section in section_directory
                    if section["section_id"] in existing_ids
                    and (set(section.get("source_ids", [])) - examined_primary_ids)
                ]
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
        max_packets = max((len(search["packets"]) for search in searches.values()), default=0)
        lookup_wave_size = 4
        max_waves = (max_packets + lookup_wave_size - 1) // lookup_wave_size
        for wave_index in range(max_waves):
            wave_requests = []
            for item_id, search in searches.items():
                if search["found"]:
                    continue
                start_packet = wave_index * lookup_wave_size
                end_packet = min(len(search["packets"]), start_packet + lookup_wave_size)
                if start_packet >= end_packet:
                    continue
                item = search["item"]
                previous = next((value for value in raw_model_items if value.get("item_id") == item_id), {})
                target_role = item["execution_plan"].get("target_role")
                target = _verified_fact(
                    previous.get("summary_value"), previous.get("summary_source_ids"),
                    item["summary_candidates"], item,
                    field_values=previous.get("summary_fields"),
                    extra_texts=_role_grounding_texts(item, target_role),
                )
                for packet_index in range(start_packet, end_packet):
                    packet = search["packets"][packet_index]
                    wave_requests.append({
                        "_packet_index": packet_index,
                        "item_id": item_id, "label": item["label"], "business_rule": item["business_rule_markdown"],
                        "execution_plan": item["execution_plan"],
                        "document_metadata": item["document_metadata"], "metadata_refs": item["metadata_refs"],
                        "verified_target": target, "summary_candidates": [] if target else _recovery_candidates(item, "summary"),
                        "selected_sections": [packet["section_path"]], "primary_candidates": packet["candidates"],
                        "logical_units": packet["logical_units"], "table_context": packet.get("table_context", []),
                        "section_batch": packet["section_batch"], "section_batch_count": packet["section_batch_count"],
                        "routed_batch_index": packet_index + 1, "routed_batch_count": len(search["packets"]),
                    })
            if not wave_requests:
                continue

            async def run_lookup_batch(batch_index: int, request_batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
                payload_batch = [{key: val for key, val in req.items() if key != "_packet_index"} for req in request_batch]
                lookup_prompt = (
                    "仅检查每项列出的完整章节原文或完整逻辑单元。verified_target存在时是锁定事实，禁止重新提取或修改，summary_candidates为空是正常情况。"
                    "找到字段返回FOUND；当前包未披露返回NOT_FOUND_IN_SCOPE；字段身份或证据不清返回UNRESOLVED。"
                    "分包未命中不代表整章或全文未披露，程序会在所有分包检查后汇总覆盖。若参考章节已完整查看但内容确不含规则要求的可比口径，将coverage_status设为NOT_FOUND并在reason说明原因。标签和值可跨页，必须引用原始source_id。"
                    "只按执行计划指向的参考文档角色查找，并遵循该项中文规则。未完整检查所有分包/章节前不能确认未披露。逐条返回执行计划中的全部复核条件；若条件只针对未采用的来源或未发生的流程分支，标NOT_APPLICABLE，source_ids和metadata_refs均留空并说明原因；CLEAR必须引用支持条件判断的source_id或metadata_refs。不要比较候选以外内容；若字段拆分为日期/数值/明细，请同时按fields[*].field名填充summary_fields和primary_fields；所有reason须用自然中文业务语言表达，严禁出现coverage、batch_count、source_id、metadata_refs、英文字段路径或英文状态名。严格返回JSON："
                    "{\"items\":[{\"item_id\":\"stable-rule-id\",\"summary_value\":\"\",\"summary_fields\":{},\"summary_source_ids\":[],\"primary_status\":\"FOUND|NOT_FOUND_IN_SCOPE|CONFIRMED_ABSENT|UNRESOLVED\",\"primary_value\":\"\",\"primary_fields\":{},\"primary_source_ids\":[],\"applicability_status\":\"CONFIRMED|NOT_REQUIRED|UNRESOLVED\",\"coverage_status\":\"COMPLETE|PARTIAL|NOT_FOUND|UNKNOWN\",\"uncovered_aspects\":[],\"review_condition_assessments\":[{\"condition_index\":0,\"status\":\"CLEAR|NOT_APPLICABLE|REVIEW_REQUIRED|UNRESOLVED\",\"source_ids\":[],\"metadata_refs\":[],\"reason\":\"\"}],\"comparison_status\":\"MATCH|MISMATCH|UNRESOLVED\",\"reason\":\"\"}]}\n"
                    + json.dumps(_shared_context(payload_batch), ensure_ascii=False)
                )
                batch_outcomes: list[dict[str, Any]] = []
                try:
                    if trace:
                        trace({"kind": "artifact", "path": f"comparison/primary_section_lookup/wave_{wave_index + 1}_batch_{batch_index + 1}/model_request.json",
                               "content": {"prompt": lookup_prompt}})
                    response = await _generate_model_text(provider, system, lookup_prompt, stage="primary_section_lookup", trace=trace)
                    if trace:
                        trace({"kind": "artifact", "path": f"comparison/primary_section_lookup/wave_{wave_index + 1}_batch_{batch_index + 1}/model_response.txt",
                               "content": response})
                    found_by_id = {found["item_id"]: found for value in _parse_json(response).get("items", []) if isinstance(value, dict)
                                   if (found := _normalize_model_item_id(value)) is not None}
                    for request in request_batch:
                        item_id = request["item_id"]
                        item = searches[item_id]["item"]
                        found = found_by_id.get(item_id, {})
                        target_role = item["execution_plan"].get("target_role")
                        target = request["verified_target"] or _verified_fact(
                            found.get("summary_value"), found.get("summary_source_ids"),
                            request["summary_candidates"], item,
                            field_values=found.get("summary_fields"),
                            extra_texts=_role_grounding_texts(item, target_role),
                        )
                        primary = _verified_fact(
                            found.get("primary_value"), found.get("primary_source_ids"),
                            request["primary_candidates"], item,
                            field_values=found.get("primary_fields") or found.get("fields"),
                            extra_texts=_role_grounding_texts(item, item["primary_role"]),
                        )
                        batch_outcomes.append({
                            "item_id": item_id,
                            "packet_index": request["_packet_index"],
                            "request_chars": len(lookup_prompt),
                            "examined_ids": [source["source_id"] for source in request["primary_candidates"]],
                            "found": found,
                            "target": target,
                            "primary": primary,
                            "failed": False,
                        })
                except Exception as exc:
                    failed_groups.append({"group": "primary_section_lookup", "error": f"{type(exc).__name__}: {exc}"})
                    for request in request_batch:
                        batch_outcomes.append({
                            "item_id": request["item_id"],
                            "packet_index": request["_packet_index"],
                            "request_chars": len(lookup_prompt),
                            "examined_ids": [],
                            "found": {},
                            "target": None,
                            "primary": None,
                            "failed": True,
                        })
                return batch_outcomes

            wave_payload_batches = _payload_batches(wave_requests)
            wave_batch_results = await asyncio.gather(*(
                run_lookup_batch(batch_index, request_batch)
                for batch_index, request_batch in enumerate(wave_payload_batches)
            ))
            ordered_outcomes = sorted(
                (entry for batch_list in wave_batch_results for entry in batch_list),
                key=lambda entry: (entry["item_id"], entry["packet_index"]),
            )
            for entry in ordered_outcomes:
                item_id = entry["item_id"]
                search = searches[item_id]
                item = search["item"]
                coverage = item["primary_routing"]
                if entry["failed"]:
                    search["failed"] = True
                    continue
                coverage["requests"] += 1
                coverage["request_characters"] += entry["request_chars"]
                coverage["examined_source_ids"].extend(entry["examined_ids"])
                found = entry["found"]
                previous = next((value for value in raw_model_items if value.get("item_id") == item_id), {})
                target = entry["target"]
                primary = entry["primary"]
                if found.get("primary_status") in {"CONFIRMED_ABSENT", "NOT_FOUND_IN_SCOPE"} and not found.get("primary_source_ids"):
                    search["absent"] += 1
                if not search["found"]:
                    if found.get("primary_status") == "FOUND" and primary:
                        search["found"] = True
                        reference = _primary_lookup_reference(
                            item, found, primary, coverage_complete=False,
                        )
                    else:
                        reference = next((ref for ref in previous.get("references", []) if ref.get("role") == item["primary_role"]),
                                         {"role": item["primary_role"], "status": "UNRESOLVED", "value": "", "source_ids": []})
                    updated = {**previous, "item_id": item_id, "primary_routing": coverage,
                               "primary_lookup_status_override": "FOUND" if search["found"] else "UNRESOLVED",
                               "references": [reference], "reason": str(found.get("reason") or previous.get("reason") or "章节检查未能确认字段。")}
                    if target:
                        updated.update(summary_value=target["value"], summary_source_ids=target["source_ids"])
                        if target.get("field_values") is not None:
                            updated["summary_fields"] = target["field_values"]
                    raw_model_items = [value for value in raw_model_items if value.get("item_id") != item_id]
                    raw_model_items.append(updated)
                elif target and not previous.get("summary_source_ids"):
                    updated = {**previous, "summary_value": target["value"], "summary_source_ids": target["source_ids"]}
                    if target.get("field_values") is not None:
                        updated["summary_fields"] = target["field_values"]
                    raw_model_items = [value for value in raw_model_items if value.get("item_id") != item_id]
                    raw_model_items.append(updated)
        for item_id, search in searches.items():
            item = search["item"]
            coverage = item["primary_routing"]
            coverage["examined_source_ids"] = list(dict.fromkeys(coverage["examined_source_ids"]))
            coverage["coverage_complete"] = (bool(search["packets"]) and not coverage["unexamined_units"]
                                              and not search["failed"] and coverage["requests"] == len(search["packets"]))
            if search["found"]:
                previous = next((value for value in raw_model_items if value.get("item_id") == item_id), {})
                refs = previous.get("references", [])
                for ref in refs:
                    if isinstance(ref, dict) and ref.get("role") == item["primary_role"]:
                        if ref.get("coverage_status") == "NOT_FOUND" and coverage["coverage_complete"]:
                            ref["coverage_status"] = "NOT_FOUND"
                        else:
                            ref["coverage_status"] = "COMPLETE" if coverage["coverage_complete"] else "PARTIAL"
                        if not coverage["coverage_complete"] and not ref.get("uncovered_aspects"):
                            ref["uncovered_aspects"] = ["选定章节仍有未检查的分包或逻辑单元。"]
                raw_model_items = [value for value in raw_model_items if value.get("item_id") != item_id]
                raw_model_items.append(previous)
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
        primary_ref = next((ref for ref in value.get("references", [])
                            if isinstance(ref, dict) and ref.get("role") == rule_input["primary_role"]), {})
        primary_status = primary_ref.get("status")
        primary_ids = primary_ref.get("source_ids") if isinstance(primary_ref.get("source_ids"), list) else []
        primary_extra = _role_grounding_texts(rule_input, rule_input["primary_role"])
        verified_primary_ref = _verified_fact(
            primary_ref.get("value"), primary_ids, rule_input["primary_candidates"], rule,
            field_values=primary_ref.get("field_values") or primary_ref.get("fields"),
            extra_texts=primary_extra,
        )
        primary_evidence = (
            [valid_sources[source_id] for source_id in verified_primary_ref["source_ids"]]
            if verified_primary_ref else []
        )
        primary_fact_verified = primary_status in {"MATCH", "MISMATCH", "UNRESOLVED"} and bool(verified_primary_ref)
        primary_coverage_status = primary_ref.get("coverage_status")
        if primary_coverage_status not in {"COMPLETE", "PARTIAL", "NOT_FOUND", "UNKNOWN"}:
            primary_coverage_status = "UNKNOWN"
        lookup_override = value.get("primary_lookup_status_override")
        primary_applicability = primary_ref.get("applicability_status", "UNRESOLVED")
        target_role = rule_input["execution_plan"]["target_role"]
        target_document = documents.get(target_role, {})
        primary_document = documents.get(rule_input["primary_role"], {})
        primary_context_ok, primary_context_assessments = evaluate_declared_context(
            rule_input["execution_plan"], target_document, primary_document,
            reference_role=rule_input["primary_role"],
        )
        if not primary_context_ok:
            primary_applicability = "UNRESOLVED"
        # Only treat primary extraction as FOUND when it either reached a
        # substantive comparison conclusion, confirmed applicability, or closed
        # coverage with NOT_FOUND; otherwise keep UNRESOLVED so candidate_review
        # fallbacks can run as declared by the rule plan.
        primary_resolved = (
            primary_fact_verified
            and (
                primary_status in {"MATCH", "MISMATCH"}
                or primary_applicability in {"CONFIRMED", "NOT_REQUIRED"}
                or primary_coverage_status == "NOT_FOUND"
            )
        )
        if lookup_override in {"CONFIRMED_ABSENT", "UNRESOLVED"}:
            primary_state = lookup_override
        else:
            primary_state = "FOUND" if primary_resolved else "UNRESOLVED"
        source_facts = {
            "primary.status": primary_state,
            "primary.coverage": primary_coverage_status,
            "primary.applicability": primary_applicability,
            "scope.fund_identity": documents.get(rule_input["primary_role"], {}).get("metadata", {}).get("fund_code"),
            "scope.period": documents.get(rule_input["primary_role"], {}).get("metadata", {}).get("period_end"),
            "scope.fact_nature": primary_ref.get("fact_nature"),
            "scope.unit": primary_ref.get("unit"),
            "scope.effective_time": primary_applicability,
            "target.scope.fund_identity": documents.get(target_role, {}).get("metadata", {}).get("fund_code"),
            "target.scope.period": documents.get(target_role, {}).get("metadata", {}).get("period_end"),
            "target.scope.fact_nature": primary_ref.get("fact_nature"),
            "target.scope.unit": primary_ref.get("unit"),
            "target.scope.effective_time": primary_applicability,
        }
        eligible_sources = eligible_fallbacks(
            {"source_selection": rule_input["source_selection"]}, source_facts,
        )
        fallback_reason = "规则声明的补充来源条件已满足。" if eligible_sources else None
        fallback_role = eligible_sources[0]["role"] if eligible_sources else None
        fallback_mode = eligible_sources[0]["mode"] if eligible_sources else None
        triggered = fallback_reason is not None
        if not triggered or not fallback_role:
            fallback_outcomes[item_id] = {"status": "NOT_TRIGGERED", "reason": "规则声明的来源切换条件尚未满足。"}
            continue
        if fallback_role not in documents:
            fallback_outcomes[item_id] = {"status": "NOT_PROVIDED", "reason": f"规则允许补充来源“{fallback_role}”，但本任务未提供该文件。"}
            continue
        if not allowed_fallback_ids[item_id]:
            fallback_outcomes[item_id] = {"status": "NO_CANDIDATES_UNCONFIRMED", "reason": "已触发补充查找，但当前候选不足以确认是否披露。"}
            continue
        if not rule_input["summary_candidates"]:
            fallback_outcomes[item_id] = {"status": "UNRESOLVED", "reason": "概要侧没有可核验的字段候选。"}
            continue

        summary_ids = value.get("summary_source_ids") if isinstance(value.get("summary_source_ids"), list) else []
        target_role = rule_input["execution_plan"]["target_role"]
        target_extra = _role_grounding_texts(rule_input, target_role)
        verified_summary = _verified_fact(
            value.get("summary_value"), summary_ids, rule_input["summary_candidates"], rule,
            field_values=value.get("summary_fields"), extra_texts=target_extra,
        )
        if not verified_summary:
            fallback_outcomes[item_id] = {"status": "UNRESOLVED", "reason": "概要侧目标事实尚未通过证据校验，补充来源不重复提取目标值。"}
            continue
        summary_value = verified_summary["value"]
        summary_ids = verified_summary["source_ids"]
        summary_fields = verified_summary.get("field_values")
        summary_evidence_by_id = {source_id: valid_sources[source_id] for source_id in summary_ids if source_id in valid_sources}
        # Preserve OCR chart Markdown sources on fallback so chart values are never lost
        # when round-1 cited only nearby titles or axis labels.
        for candidate in rule_input["summary_candidates"]:
            if candidate.get("source_kind") == "image_ocr":
                summary_evidence_by_id.setdefault(candidate["source_id"], candidate)
        if item_id == "annual_nav_performance_chart":
            target_cat = catalogues.get(target_role) or {}
            for page_rec in target_cat.get("pages") or []:
                for src in page_rec.get("sources") or []:
                    if src.get("source_kind") != "image_ocr":
                        continue
                    sec_text = " ".join(str(part) for part in (src.get("section_path") or []))
                    if "资产配置" in sec_text or "投资组合资产配置" in str(src.get("raw_text") or ""):
                        summary_evidence_by_id.setdefault(src["source_id"], src)
        summary_evidence = list(summary_evidence_by_id.values())

        plan = rule_input["fallback_context_plan"]
        if not plan["batches"]:
            fallback_outcomes[item_id] = {"status": "UNRESOLVED", "reason": "补充来源逻辑单元超出上下文预算，未检查。",
                                          "unexamined_units": plan["skipped_units"]}
        for batch in plan["batches"]:
            fallback_requests.append({
                "item_id": item_id, "label": rule["label"], "business_rule_markdown": rule_input["business_rule_markdown"],
                "fallback_role": fallback_role,
                "fallback_mode": fallback_mode,
                "planned_fallbacks": rule_input["planned_fallbacks"],
                "execution_plan": rule_input["execution_plan"],
                "lookup_basis": fallback_reason,
                "primary_coverage_status": primary_coverage_status,
                "primary_uncovered_aspects": primary_ref.get("uncovered_aspects", []),
                "verified_primary_reference": {"value": str(primary_ref.get("value") or ""), "source_ids": primary_ids},
                "verified_target": {
                    "value": summary_value,
                    "source_ids": summary_ids,
                    **({"field_values": summary_fields} if summary_fields is not None else {}),
                },
                # Candidate review must see the already-verified target wording,
                # not only opaque IDs. This lets the model assess source identity,
                # scope, and applicability without re-extracting or inventing it.
                "verified_target_candidates": summary_evidence,
                "fallback_candidates": batch["candidates"],
                "document_metadata": _metadata_facts_for_roles(
                    documents, {rule_input["execution_plan"].get("target_role"), rule_input.get("primary_role"), fallback_role},
                ),
                "metadata_refs": sorted(_available_metadata_reference_ids(
                    documents, {rule_input["execution_plan"].get("target_role"), rule_input.get("primary_role"), fallback_role},
                )),
                "logical_units": batch["logical_units"], "table_context": batch.get("table_context", []),
                "coverage": {"coverage_complete": plan["coverage_complete"], "batch_count": len(plan["batches"]),
                             "skipped_unit_count": len(plan["skipped_units"])},
            })
    fallback_requests, oversized_fallback_contexts = _merge_fallback_contexts(fallback_requests)
    for item_id, size in oversized_fallback_contexts.items():
        fallback_outcomes[item_id] = {
            "status": "UNRESOLVED",
            "reason": f"该项相关来源章节合并后超出模型上下文预算（{size} 字符），未分开依据不完整的片段作结论。",
        }
    # Fallback items can point at overlapping chapters with different meanings
    # (for example, a fee list and the fee-rate calculation clauses). Keep each
    # item in its own request so a model cannot cite a neighboring item's
    # otherwise-valid source ID. Evidence validation remains item-scoped below.
    active_fallback_requests = [
        request for request in fallback_requests
        if fallback_outcomes.get(request["item_id"], {}).get("status") != "FOUND"
    ]
    fallback_batches = list(_isolated_payload_batches(active_fallback_requests))

    async def _run_fallback_batch(fallback_index: int, fallback_batch: list[dict[str, Any]]) -> list[tuple[str, list[str], dict[str, Any]]]:
        fallback_prompt = (
            "按每项的中文规则和执行计划，在规则声明的补充来源中查找目标事实。不得把主来源中已经核实的事实、值或引用改写，也不得用补充来源覆盖主来源明确报告的相反事实。mode=select 表示规则声明的来源切换条件已满足；mode=candidate_review 表示主来源状态尚未确认，需要独立审核补充来源本身是否满足中文规则。candidate_review 不是拒绝使用该来源的标志：若规则允许在主来源未确认时独立核验补充来源，且候选原文、目标原文和适用上下文满足规则要求，应返回 status=FOUND、applicability_status=CONFIRMED，执行器会据此采用该来源；若规则要求先确认主来源缺失，或候选自身适用性无法确认，则返回 UNRESOLVED。不得仅因主来源状态为UNKNOWN或UNRESOLVED就否定一个规则允许独立核验的候选来源，也不得把主来源状态不确定表述成已确认未披露。候选适用性审核仍须依据原文及规则要求确认身份、合同关系、生效时点和口径；只有适用性确认后才可采用。"
            "顶层source_ids只用于证明value来自本请求的fallback_candidates，必须且只能引用这些补充来源的source_id；不得把verified_target_candidates或verified_primary_reference的source_id混入顶层source_ids。目标侧证据已由执行器预先核验，不需要在顶层重复引用；review_condition_assessments.source_ids则可引用该条件实际需要的目标、主来源或补充来源证据，但只能引用本请求中存在的source_id。引用必须逐字复制该项允许来源中的source_id，并能在来源记录中找到；source_id同时作为context_pool.sources的键和来源记录字段。候选可能分处相邻单元格或跨页逻辑单元，请结合完整上下文绑定标签和值，但只引用原始证据ID。作出‘候选未提供该事实’或‘没有该费率/数据’的结论前，必须检查该项全部fallback_candidates中的raw_text、row_text、cell_text及logical_units；只要其中存在直接支持目标事实的原文，就应引用该source_id并评估适用性，不能声称候选没有提供。"
            "filename_date_*是从文件名开头规范日期读取的独立候选元数据，不等同PDF内文日期；仅在对应中文规则允许、且与文件内容相容时作为辅助依据，并用metadata_refs引用，不能覆盖PDF内文中的相反日期。version_date_*是预检根据PDF内可确认版本日期或符合PDF日期范围的文件名日期归并出的招募说明书版本日期事实；仅在该项中文规则允许使用版本日期时引用，并用metadata_refs引用。"
            "日期条件须按规则声明的精度比较。若规则允许日期精确到日并规定同日可用，且两日期元数据均精确到日、日期值相同，则该日期条件为CLEAR；不得因缺少日内时分或无法推断先后而标UNRESOLVED。若日期精度不同、值不同或存在相反原文证据，仍按中文规则处理。"
            "适用期间、身份、单位、性质及比较口径均依该项中文规则判断；不确定时保持UNRESOLVED。数字、日期、代码等事实值须由引用原文（含表格表头/单位与可见文档元数据）直接支持。"
            "如果规则execution_plan.comparison.fields声明了多个字段或需要拆分日期、数值、年份/类别明细，请同时按fields[*].field名填充fields（如{\"data_cutoff_date\":\"2025-12-31\",\"percentage\":{\"权益投资\":\"99.13%\"}}）；子项字典只填入双方同一层级的实际披露分项，忽略合计行与占比为‘-’的空行。"
            "逐条评估执行计划review_conditions。复核条件由原文支持时标CLEAR并引用source_id；由输入提供的结构化文档元数据支持时标CLEAR，并在metadata_refs中引用准确的‘文档角色.字段名’；只针对未采用来源或未发生流程分支时标NOT_APPLICABLE，source_ids和metadata_refs均留空并说明原因；条件触发或信息不足时分别标REVIEW_REQUIRED或UNRESOLVED。metadata_refs只能引用当前输入中实际提供的字段；所有reason须用自然中文业务语言表达，严禁出现coverage、batch_count、source_id、metadata_refs、英文字段路径或英文状态名。严格返回JSON：{\"items\":[{\"item_id\":\"stable-rule-id\",\"status\":\"FOUND|UNRESOLVED\",\"applicability_status\":\"CONFIRMED|NOT_REQUIRED|UNRESOLVED\",\"comparison_status\":\"MATCH|MISMATCH|UNRESOLVED\",\"review_condition_assessments\":[{\"condition_index\":0,\"status\":\"CLEAR|NOT_APPLICABLE|REVIEW_REQUIRED|UNRESOLVED\",\"source_ids\":[],\"metadata_refs\":[],\"reason\":\"\"}],\"value\":\"\",\"fields\":{},\"source_ids\":[],\"reason\":\"\"}]}\n"
            + json.dumps(_shared_context(fallback_batch), ensure_ascii=False)
        )
        if trace:
            trace({"kind": "artifact", "path": f"comparison/fallback_batch/batch_{fallback_index}/model_request.json",
                   "content": {"prompt": fallback_prompt, "item_ids": [item["item_id"] for item in fallback_batch]}})
        batch_outcomes: list[tuple[str, list[str], dict[str, Any]]] = []
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
                examined_ids = [source["source_id"] for source in request["fallback_candidates"]]
                response = response_by_id.get(item_id, {})
                fallback_role = request["fallback_role"]
                fallback_ids = response.get("source_ids") if isinstance(response.get("source_ids"), list) else []
                fallback_value = str(response.get("value") or "")
                fact = _verified_fact(
                    fallback_value, fallback_ids, request["fallback_candidates"], rule_by_id[item_id],
                    field_values=response.get("fields"),
                    extra_texts=_role_grounding_texts(request, fallback_role),
                )
                evidence = [valid_sources[source_id] for source_id in fact["source_ids"]] if fact else []
                applicability_status = response.get("applicability_status", "UNRESOLVED")
                verified_payload = (
                    {
                        "value": fact["value"],
                        "evidence": evidence,
                        "field_values": fact.get("field_values"),
                        "citation_binding": fact.get("citation_binding"),
                    }
                    if fact else {}
                )
                if response.get("status") != "FOUND" or not fact:
                    batch_outcomes.append((item_id, examined_ids, {
                        "status": "UNRESOLVED",
                        "reason": str(response.get("reason") or "补充来源未能确认字段。"),
                        "fallback_mode": request.get("fallback_mode", "select"),
                        "applicability_status": applicability_status,
                        "comparison_status": response.get("comparison_status"),
                        "review_condition_assessments": response.get("review_condition_assessments"),
                        **verified_payload,
                    }))
                    continue
                if request.get("fallback_mode") == "candidate_review" and applicability_status != "CONFIRMED":
                    batch_outcomes.append((item_id, examined_ids, {
                        "status": "UNRESOLVED",
                        "reason": str(response.get("reason") or "候选来源的合同适用性未能确认。"),
                        "fallback_mode": request.get("fallback_mode", "candidate_review"),
                        "applicability_status": applicability_status,
                        "comparison_status": response.get("comparison_status"),
                        "review_condition_assessments": response.get("review_condition_assessments"),
                        **verified_payload,
                    }))
                    continue
                target = request["verified_target"]
                target_evidence = [valid_sources[source_id] for source_id in target["source_ids"]]
                batch_outcomes.append((item_id, examined_ids, {
                    "status": "FOUND", "reason": "补充来源找到可引用证据。",
                    "fallback_mode": request.get("fallback_mode", "select"),
                    "comparison_reason": str(response.get("reason") or ""),
                    "comparison_status": response.get("comparison_status"),
                    "applicability_status": applicability_status,
                    "review_condition_assessments": response.get("review_condition_assessments"),
                    "value": fact["value"], "evidence": evidence,
                    "field_values": fact.get("field_values"),
                    "citation_binding": fact.get("citation_binding"),
                    "summary_value": target["value"], "summary_evidence": target_evidence,
                    "summary_fields": target.get("field_values"),
                }))
        except Exception as exc:
            for request in fallback_batch:
                batch_outcomes.append((request["item_id"], [], {
                    "status": "EXTRACTION_FAILED", "reason": f"补充来源识别失败：{type(exc).__name__}",
                }))
        return batch_outcomes

    if fallback_batches:
        fallback_batch_results = await asyncio.gather(*(
            _run_fallback_batch(fallback_index, fallback_batch)
            for fallback_index, fallback_batch in enumerate(fallback_batches, start=1)
        ))
        for batch_outcomes in fallback_batch_results:
            for item_id, examined_ids, outcome in batch_outcomes:
                if examined_ids:
                    rule_inputs[item_id].setdefault("fallback_examined_source_ids", set()).update(examined_ids)
                fallback_outcomes[item_id] = outcome
    results = []
    target_fact_records = []
    selected_reference_fact_records = []
    chart_cutoffs: dict[str, date | None] = {}
    for rule in rules:
        item_id = rule["item_id"]
        value = raw_items.get(item_id, {})
        rule_input = rule_inputs[item_id]
        primary_role = rule_input["primary_role"]
        fallback_role = rule_input["fallback_role"]
        summary_ids = value.get("summary_source_ids") if isinstance(value.get("summary_source_ids"), list) else []
        target_role = rule_input["execution_plan"]["target_role"]
        target_extra = _role_grounding_texts(rule_input, target_role)
        verified_summary = _verified_fact(
            value.get("summary_value"), summary_ids, rule_input["summary_candidates"], rule,
            field_values=value.get("summary_fields"), extra_texts=target_extra,
        )
        summary_fields = verified_summary.get("field_values") if verified_summary else None
        if verified_summary:
            summary_ids = verified_summary["source_ids"]
            summary_evidence = [valid_sources[source_id] for source_id in summary_ids]
            summary_value = verified_summary["value"]
        else:
            summary_evidence = []
            summary_value = str(value.get("summary_value") or "")
        refs_by_role = {ref.get("role"): ref for ref in value.get("references", []) if isinstance(ref, dict)}
        selected_role = primary_role
        ref = refs_by_role.get(primary_role, {})
        ref_status = ref.get("status") if ref.get("status") in {"MATCH", "MISMATCH", "UNRESOLVED", "SOURCE_MISSING"} else "UNRESOLVED"
        model_omitted_item = item_id not in raw_items and bool(rule_input["primary_candidates"])
        if model_omitted_item:
            ref_status = "UNRESOLVED"
        reference_ids = ref.get("source_ids") if isinstance(ref.get("source_ids"), list) else []
        primary_extra = _role_grounding_texts(rule_input, primary_role)
        verified_primary = _verified_fact(
            ref.get("value"), reference_ids, rule_input["primary_candidates"], rule,
            field_values=ref.get("field_values") or ref.get("fields"), extra_texts=primary_extra,
        )
        reference_fields = verified_primary.get("field_values") if verified_primary else None
        if verified_primary:
            reference_ids = verified_primary["source_ids"]
            reference_evidence = [valid_sources[source_id] for source_id in reference_ids]
            reference_value = verified_primary["value"]
        else:
            reference_evidence = []
            reference_value = str(ref.get("value") or "")
            if ref_status != "SOURCE_MISSING":
                ref_status = "UNRESOLVED"
        # Keep the verified primary-source evidence separate from the eventual
        # selected reference. A fallback may supplement a partial finding, but
        # must never erase the evidence and value already found in the report.
        primary_reference_value = reference_value if reference_evidence else None
        primary_evidence = list(reference_evidence)
        primary_coverage_status = ref.get("coverage_status")
        if primary_coverage_status not in {"COMPLETE", "PARTIAL", "NOT_FOUND", "UNKNOWN"}:
            primary_coverage_status = "UNKNOWN"
        primary_uncovered_aspects = ref.get("uncovered_aspects")
        if not isinstance(primary_uncovered_aspects, list):
            primary_uncovered_aspects = []
        primary_uncovered_aspects = [str(aspect).strip() for aspect in primary_uncovered_aspects
                                     if isinstance(aspect, str) and aspect.strip()][:20]
        if not primary_evidence:
            primary_coverage_status = "UNKNOWN"
            primary_uncovered_aspects = []
        lookup_override = value.get("primary_lookup_status_override")
        if reference_evidence:
            primary_lookup_status = "FOUND"
        elif lookup_override:
            primary_lookup_status = lookup_override
        elif model_omitted_item:
            primary_lookup_status = "EXTRACTION_FAILED"
        else:
            primary_lookup_status = rule_input["primary_lookup_status"]
        fallback_outcome = fallback_outcomes.get(item_id, {"status": "NOT_TRIGGERED", "reason": "规则声明的来源切换条件尚未满足。"})
        fallback_lookup = {
            **fallback_outcome,
            **({"reason": _humanize_user_reason(fallback_outcome.get("reason"))}
               if fallback_outcome.get("reason") else {}),
            **({"comparison_reason": _humanize_user_reason(fallback_outcome.get("comparison_reason"))}
               if fallback_outcome.get("comparison_reason") else {}),
        }
        fallback_triggered = fallback_outcome.get("status") != "NOT_TRIGGERED"
        fallback_selected = fallback_outcome.get("status") == "FOUND"
        fallback_evidence = fallback_outcome.get("evidence") or []
        fallback_located = bool(fallback_evidence) and bool(fallback_role)
        use_fallback_reference = fallback_selected or fallback_located
        applicability_status = (
            fallback_outcome.get("applicability_status", "UNRESOLVED")
            if use_fallback_reference
            else ref.get("applicability_status", "UNRESOLVED")
        )
        if fallback_selected:
            selected_role = fallback_role
            ref_status = fallback_outcome.get("comparison_status") if fallback_outcome.get("comparison_status") in {"MATCH", "MISMATCH", "UNRESOLVED"} else "UNRESOLVED"
            allowed_applicability = ({"CONFIRMED"} if fallback_outcome.get("fallback_mode") == "candidate_review"
                                     else {"CONFIRMED", "NOT_REQUIRED"})
            if fallback_outcome.get("applicability_status") not in allowed_applicability:
                ref_status = "UNRESOLVED"
            reference_evidence, reference_value = fallback_outcome["evidence"], fallback_outcome["value"]
            reference_fields = fallback_outcome.get("field_values")
            summary_evidence, summary_value = fallback_outcome["summary_evidence"], fallback_outcome["summary_value"]
            if "summary_fields" in fallback_outcome and fallback_outcome["summary_fields"] is not None:
                summary_fields = fallback_outcome["summary_fields"]
        elif fallback_located:
            # Keep verified fallback evidence and values visible for review when
            # applicability (for example, prospectus version_date precision) blocks a match.
            selected_role = fallback_role
            ref_status = "UNRESOLVED"
            reference_evidence = fallback_evidence
            reference_value = str(fallback_outcome.get("value") or "")
            reference_fields = fallback_outcome.get("field_values")
        elif fallback_triggered:
            ref_status = "UNRESOLVED"
        if summary_evidence and reference_evidence:
            selected_document = documents.get(selected_role, {})
            context_ok, context_assessments = evaluate_declared_context(
                rule_input["execution_plan"], target_document, selected_document,
                reference_role=selected_role,
            )
            if not context_ok:
                ref_status = "UNRESOLVED"
        else:
            context_ok = primary_context_ok
            context_assessments = primary_context_assessments
        condition_assessment = fallback_outcome if use_fallback_reference else ref
        assessment_roles = {
            rule_input["execution_plan"].get("target_role"),
            rule_input.get("primary_role", primary_role),
        }
        if use_fallback_reference and fallback_role:
            assessment_roles.add(fallback_role)
        assessment_roles.discard(None)
        item_metadata_refs = _available_metadata_reference_ids(documents, assessment_roles)
        condition_assessment = _filter_unavailable_metadata_refs(
            condition_assessment, item_metadata_refs,
        )
        if summary_evidence and reference_evidence:
            condition_source_ids = (
                allowed_summary_ids[item_id] | allowed_primary_ids[item_id] | allowed_fallback_ids[item_id]
                if use_fallback_reference else allowed_summary_ids[item_id] | allowed_primary_ids[item_id]
            )
            review_conditions_clear = _review_conditions_are_clear(
                rule, condition_assessment, condition_source_ids, set(valid_sources),
                available_metadata_refs=item_metadata_refs,
            )
            ref_status = _compare_by_execution_plan(
                rule, summary_value, reference_value, ref_status, applicability_status,
                review_conditions_clear, context_ok,
                target_fields=summary_fields, reference_fields=reference_fields,
            )
        extra_reference_keys = (
            _structured_extra_reference_keys(rule, summary_fields, reference_fields)
            if summary_evidence and reference_evidence
            else []
        )
        if fallback_triggered and not fallback_selected:
            ref_status = "UNRESOLVED"
        comparison_basis = None
        if not rule_input["summary_candidates"]:
            status, reason_code = "NOT_CHECKED", "TARGET_FIELD_NOT_FOUND_IN_PRESET_SCOPE"
        elif not summary_evidence and primary_lookup_status in {"NO_CANDIDATES_UNCONFIRMED", "NOT_APPLICABLE"}:
            status, reason_code = "NOT_CHECKED", "FIELD_CANDIDATES_NOT_CONFIRMED"
        elif not summary_evidence:
            status, reason_code = "UNRESOLVED", "TARGET_NOT_LOCATED"
        elif not use_fallback_reference and primary_lookup_status == "UNRESOLVED" and value.get("primary_lookup_reason_code"):
            status, reason_code = "UNRESOLVED", value["primary_lookup_reason_code"]
        elif not use_fallback_reference and primary_lookup_status in {"NO_CANDIDATES_UNCONFIRMED", "NOT_APPLICABLE"} and ref_status not in {"MATCH", "MISMATCH"}:
            status, reason_code = "NOT_CHECKED", "PRIMARY_LOOKUP_UNCONFIRMED"
        elif not use_fallback_reference and ref_status == "SOURCE_MISSING":
            status, reason_code = "NOT_CHECKED", "NO_VALID_REFERENCE"
        elif ref_status in {"MATCH", "MISMATCH"} and reference_evidence:
            status, reason_code = ref_status, None
        else:
            status, reason_code = "UNRESOLVED", "EVIDENCE_OR_COMPARISON_UNRESOLVED"
        existing_reason = (
            (fallback_outcome.get("comparison_reason") or "补充来源已按规则完成核对。")
            if fallback_selected
            else (fallback_outcome.get("reason") or "已检查补充来源，但适用性或复核条件尚待确认。")
            if (fallback_located or (not reference_evidence and fallback_triggered and fallback_outcome.get("status") not in {"NOT_PROVIDED", None} and fallback_outcome.get("reason")))
            else ref.get("reason") or value.get("reason") or "规则未能确认可比较的参考事实。"
        )
        if extra_reference_keys and status == "UNRESOLVED":
            existing_reason = re.sub(r"[^。；\n]*额外披露[^。；\n]*不纳入比较[。；]?", "", existing_reason).strip()
        final_reason = _reason_for_final_status(
            status, existing_reason,
            model_status=(fallback_outcome.get("comparison_status") if fallback_selected else ref.get("status")),
            applicability_status=applicability_status,
            review_conditions_clear=review_conditions_clear if summary_evidence and reference_evidence else False,
            declared_context_clear=context_ok,
        )
        if extra_reference_keys and status == "UNRESOLVED":
            final_reason = re.sub(r"[^。；\n]*额外披露[^。；\n]*不纳入比较[。；]?", "", final_reason).strip()
            extra_note = (
                f"参考来源比产品资料概要多出额外年份/项目（{'、'.join(extra_reference_keys)}），"
                "不能直接判为一致，已标记为待复核。"
            )
            if extra_note not in final_reason:
                final_reason = f"{final_reason} {extra_note}".strip()
        if item_id == "asset_allocation_chart":
            chart_cutoffs[item_id] = _extract_chart_cutoff_date(
                summary_fields=summary_fields,
                summary_value=summary_value,
                summary_evidence=summary_evidence,
                summary_candidates=rule_input["summary_candidates"],
                section_markers=("资产配置", "区域配置"),
            )
        elif item_id == "annual_nav_performance_chart":
            chart_cutoffs[item_id] = _extract_chart_cutoff_date(
                summary_fields=summary_fields,
                summary_value=summary_value,
                summary_evidence=summary_evidence,
                summary_candidates=rule_input["summary_candidates"],
                section_markers=("净值增长率", "业绩比较基准"),
            )
        primary_reference = {"role": primary_role, "status": "FOUND" if primary_lookup_status == "FOUND" else primary_lookup_status,
                             "evidence": primary_evidence, "raw_text": primary_reference_value,
                             "coverage_status": primary_coverage_status,
                             "uncovered_aspects": primary_uncovered_aspects}
        selected_reference = {"role": selected_role, "raw_text": reference_value if reference_evidence else None, "evidence": reference_evidence,
                              "status": status, "reason": final_reason}
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
            "citation_binding": fallback_outcome.get("citation_binding") if use_fallback_reference else ref.get("citation_binding"),
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
            "key": rule["key"], "label": rule["label"], "status": status,
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
            "selected_source_applicability": applicability_status,
            "context_check_assessments": context_assessments,
            "review_condition_assessments": condition_assessment.get("review_condition_assessments", []),
            "source_selection_reason": (
                "参考来源证据已核验" if selected_role == primary_role and reference_evidence
                else fallback_lookup["reason"] if selected_role == fallback_role
                else "参考来源返回了候选结论，但引用未通过原文校验" if primary_lookup_status == "FOUND"
                else fallback_lookup["reason"]
            ),
            "reason_code": reason_code,
            "reason": final_reason,
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
        fallback_used = any(
            item["selected_reference"]["role"] == "prospectus"
            or item["fallback_lookup"].get("status") not in {"NOT_TRIGGERED", "NOT_PROVIDED", None}
            for item in results
        )
        progress("fallback_lookup", "completed", "已完成允许的补充查找" if fallback_used else "没有触发补充查找")
        progress("comparison", "completed", "指标核对和引用校验完成")
    _apply_chart_cutoff_year_consistency(results, chart_cutoffs)
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
