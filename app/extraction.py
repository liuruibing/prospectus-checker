from __future__ import annotations

import asyncio
import json
import os
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable

import anydoc
import pymupdf as fitz

from app.domain import COMMON_RULE_FILE, MODULE_RULES, normalize_numeric, title_aliases

MAX_PDF_PAGES = int(os.getenv("PROSPECTUS_MAX_PAGES", "240"))
MAX_ANALYSIS_CHARS = int(os.getenv("PROSPECTUS_MAX_ANALYSIS_CHARS", "150000"))
SECTION_SCORE_LOOKAHEAD = 35
SECTION_PARENT_SCORE_TOLERANCE = 5
CONTENTS_LINE_PENALTY = 100


def _trace_event(
    callback: Callable[[dict[str, Any]], None] | None,
    event: str,
    *,
    role: str,
    details: dict[str, Any] | None = None,
) -> None:
    if callback:
        callback({"kind": "event", "event": event, "document_role": role, "details": details or {}})


def _trace_artifact(
    callback: Callable[[dict[str, Any]], None] | None,
    path: str,
    content: Any,
    *,
    role: str,
) -> None:
    if callback:
        callback({"kind": "artifact", "document_role": role, "path": path, "content": content})


def _compact_with_offsets(value: str) -> tuple[str, list[int]]:
    compact: list[str] = []
    offsets: list[int] = []
    for index, char in enumerate(value):
        normalized = unicodedata.normalize("NFKC", char).casefold()
        for item in normalized:
            if item.isspace():
                continue
            compact.append(item)
            offsets.append(index)
    return "".join(compact), offsets


def _alias_spans(line: str, aliases: list[str]) -> list[tuple[int, int]]:
    compact_line, offsets = _compact_with_offsets(line)
    spans: set[tuple[int, int]] = set()
    for alias in sorted((item for item in aliases if item), key=len, reverse=True):
        compact_alias, _ = _compact_with_offsets(alias)
        if not compact_alias:
            continue
        search_from = 0
        while (position := compact_line.find(compact_alias, search_from)) >= 0:
            start = offsets[position]
            end = offsets[position + len(compact_alias) - 1] + 1
            spans.add((start, end))
            search_from = position + len(compact_alias)
    return sorted(spans)


def _heading_depth(text: str) -> int | None:
    value = re.sub(r"</?u>|[*_`]", "", text, flags=re.IGNORECASE).lstrip(" #<>")
    match = re.match(r"(\d+(?:\.\d+)*)(?:\s*[、.．]|\s+)\s*\S", value)
    if match:
        return len(match.group(1).split("."))
    if re.match(r"[一二三四五六七八九十百]+、\s*\S", value):
        return 1
    return None


def _anydoc_sections(markdown: str, rule_texts: dict[str, str]) -> dict[str, str]:
    """Group each module's complete AnyDoc section, excluding TOC hits and the next peer section."""
    lines = markdown.splitlines()
    candidates: list[tuple[int, int, int, str]] = []
    for rule in MODULE_RULES:
        rule_id = rule["rule_id"]
        aliases = title_aliases(rule_texts[rule_id])
        for index, line in enumerate(lines):
            for span in _alias_spans(line, aliases):
                candidates.append((index, span[0], span[1], rule_id))

    def score(candidate: tuple[int, int, int, str]) -> int:
        index, offset, _, rule_id = candidate
        window = lines[index][offset:] + "\n" + "\n".join(lines[index + 1:index + SECTION_SCORE_LOOKAHEAD + 1])
        table_lines = sum("|" in line for line in window.splitlines())
        aliases = _header_aliases(rule_texts[rule_id])
        compact_window = _normal(window)
        recognized_headers = sum(
            1 for values in aliases.values()
            if any(_normal(value) in compact_window for value in values)
        )
        contents_penalty = CONTENTS_LINE_PENALTY if re.search(
            r"(?:\.{3,}|…{2,}|\u2026{2,})\s*\d+\s*$", lines[index][offset:]
        ) else 0
        return table_lines + recognized_headers * 3 - contents_penalty

    selected: dict[str, tuple[int, int, int, str]] = {}
    for rule in MODULE_RULES:
        options = [candidate for candidate in candidates if candidate[3] == rule["rule_id"]]
        if options:
            best_score = max(score(candidate) for candidate in options)
            if best_score > 0:
                # Keep a nearby parent section heading when its child heading
                # scores marginally higher because the table begins below it.
                plausible = [
                    candidate for candidate in options
                    if score(candidate) >= best_score - SECTION_PARENT_SCORE_TOLERANCE
                ]
                selected[rule["rule_id"]] = min(plausible, key=lambda candidate: (candidate[0], candidate[1]))
            else:
                selected[rule["rule_id"]] = max(options, key=lambda candidate: (candidate[0], candidate[1]))

    ordered = sorted(selected.values())
    sections: dict[str, str] = {}
    for start_line, start_offset, _, rule_id in ordered:
        prefix = lines[start_line][:start_offset]
        depth = _heading_depth(lines[start_line]) or _heading_depth(lines[start_line][start_offset:]) or _heading_depth(prefix)
        end_line, end_offset = len(lines), 0
        for index in range(start_line, len(lines)):
            scan_from = start_offset + 1 if index == start_line else 0
            selected_boundary = next(
                ((line_no, offset) for line_no, offset, _, other_id in ordered
                 if other_id != rule_id and (line_no, offset) > (start_line, start_offset)
                 and (line_no, offset) >= (index, scan_from)),
                None,
            )
            generic_boundary = None
            if index > start_line or scan_from == 0:
                candidate_depth = _heading_depth(lines[index][scan_from:])
                if candidate_depth is not None and depth is not None and candidate_depth <= depth:
                    generic_boundary = (index, scan_from)
            boundaries = [item for item in (selected_boundary, generic_boundary) if item]
            if boundaries:
                end_line, end_offset = min(boundaries)
                break
        content = lines[start_line][start_offset:]
        if end_line == start_line:
            content = lines[start_line][start_offset:end_offset]
            if end_line + 1 < len(lines):
                content += "\n" + "\n".join(lines[end_line + 1:])
        else:
            content += "\n" + "\n".join(lines[start_line + 1:end_line])
            if end_offset:
                content += "\n" + lines[end_line][:end_offset]
        content = re.sub(r"\s*(?:<u>\s*)?(?:\*\*)?\d+(?:\.\d+)*(?:、)?(?:\*\*)?(?:</u>)?\s*$", "", content)
        sections[rule_id] = content.strip()
    return sections


def _physical_pages_in_markdown(section: str, pages: list[dict[str, Any]]) -> set[int]:
    """Map AnyDoc's printed footers and post-footer continuation to PDF pages."""
    printed_numbers = {
        int(match.group(1))
        for match in re.finditer(r"第\s*(\d+)\s*页(?:\s*共\s*\d+\s*页)?", section)
    }
    page_numbers: set[int] = set()
    for page in pages:
        physical_text = unicodedata.normalize("NFKC", page["text"])
        for match in re.finditer(r"第\s*(\d+)\s*页(?:\s*共\s*\d+\s*页)?", physical_text):
            if int(match.group(1)) in printed_numbers:
                page_numbers.add(int(page["page"]))
                if int(page["page"]) < len(pages):
                    page_numbers.add(int(page["page"]) + 1)
                break
    return page_numbers


def _chapter_contexts(
    sections: dict[str, str],
    pages: list[dict[str, Any]],
    rule_texts: dict[str, str],
) -> dict[str, dict[str, Any]]:
    """Assemble module content before extraction while retaining physical source blocks."""
    contexts: dict[str, dict[str, Any]] = {}
    for rule in MODULE_RULES:
        rule_id = rule["rule_id"]
        section = sections.get(rule_id, "")
        if not section:
            continue

        page_numbers = _physical_pages_in_markdown(section, pages)
        aliases = title_aliases(rule_texts[rule_id])
        # Printed page footers are the primary mapping. A matching heading is a
        # useful fallback when a parser omits the footer from its Markdown.
        if not page_numbers:
            header_terms = [
                alias
                for values in _header_aliases(rule_texts[rule_id]).values()
                for alias in values
            ]
            page_scores = []
            for page in pages:
                text = _normal(page["text"])
                if any(_normal(alias) in text for alias in aliases if alias):
                    header_score = sum(_normal(term) in text for term in header_terms if term)
                    page_scores.append((header_score, int(page["page"])))
            if page_scores:
                best_score = max(score for score, _ in page_scores)
                best_pages = [number for score, number in page_scores if score == best_score]
                page_numbers.update(best_pages if best_score else [max(best_pages)])
        source_pages = []
        for page in pages:
            if int(page["page"]) not in page_numbers:
                continue
            blocks = page.get("blocks") or [{"bbox": None, "text": page["text"]}]
            source_pages.append({
                "physical_page": int(page["page"]),
                "blocks": [
                    {"bbox": block.get("bbox"), "text": block.get("text", "")}
                    for block in blocks
                    if str(block.get("text", "")).strip()
                ],
            })
        contexts[rule_id] = {
            "section_markdown": section,
            "physical_pages": source_pages,
        }
    return contexts


def _format_chapter_contexts(contexts: dict[str, dict[str, Any]]) -> str:
    """Render complete section structure first, then page-addressable source blocks."""
    rendered = []
    for rule in MODULE_RULES:
        context = contexts.get(rule["rule_id"])
        if not context:
            continue
        source_pages = []
        for page in context["physical_pages"]:
            blocks = "\n".join(
                f"[SOURCE BLOCK] {block['text']}"
                for block in page["blocks"]
            )
            source_pages.append(f"[PHYSICAL PAGE {page['physical_page']}]\n{blocks}")
        source_page_text = "\n\n".join(source_pages)
        rendered.append(
            f"【章节：{rule['rule_id']}】\n"
            "[章节归集内容，包含跨页正文、表格和表注；先依据本模块规则分类]\n"
            f"{context['section_markdown']}\n"
            "[章节对应的 PDF 原文区块，仅用于逐字引用与物理页码核验]\n"
            f"{source_page_text}"
        )
    return "\n\n".join(rendered)


def _normal(value: Any) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or ""))).casefold()


def _read_pdf(content: bytes, filename: str) -> tuple[Any, list[dict[str, Any]]]:
    try:
        document = fitz.open(stream=content, filetype="pdf")
    except Exception as exc:
        raise ValueError(f"PDF_INVALID: {filename}: {exc}") from exc
    if document.is_encrypted:
        document.close()
        raise ValueError(f"PDF_ENCRYPTED: {filename}")
    if document.page_count == 0:
        document.close()
        raise ValueError(f"PDF_EMPTY: {filename}")
    if document.page_count > MAX_PDF_PAGES:
        document.close()
        raise ValueError(f"PDF_TOO_MANY_PAGES: {filename}: {MAX_PDF_PAGES}")
    pages: list[dict[str, Any]] = []
    for index, page in enumerate(document, start=1):
        text = page.get_text("text", sort=True) or ""
        text_blocks = page.get_text("blocks", sort=True) or []
        pages.append({
            "page": index,
            "text": text,
            "blocks": [
                {"bbox": [float(value) for value in block[:4]], "text": str(block[4] or "")}
                for block in text_blocks
                if len(block) >= 5 and str(block[4] or "").strip()
            ],
            "width": float(page.rect.width),
            "height": float(page.rect.height),
        })
    if not any(item["text"].strip() for item in pages):
        document.close()
        raise ValueError(f"PDF_NO_EXTRACTABLE_TEXT: {filename}; OCR is not enabled")
    return document, pages


def _candidate_pages(pages: list[dict[str, Any]], rule_texts: dict[str, str]) -> set[int]:
    selected = {1, 2, 3, len(pages)}
    for rule in MODULE_RULES:
        text = rule_texts[rule["rule_id"]]
        aliases = title_aliases(text)
        module_matches = []
        for page in pages:
            folded = _normal(page["text"])
            if any(_normal(alias) in folded for alias in aliases if alias):
                module_matches.append(page["page"])
        if not module_matches:
            # Heading variants sometimes omit one word; use the title's unique domain terms.
            terms = {
                "investment.asset_allocation": ("资产组合", "权益投资", "银行存款和结算备付金"),
                "investment.industry_stock_portfolio": ("行业分类", "行业代码", "公允价值", "净值比例"),
                "investment.stock_holdings": ("股票代码", "持有数量", "股票投资明细"),
            }[rule["rule_id"]]
            module_matches = [
                page["page"] for page in pages
                if sum(term in page["text"] for term in terms) >= 2
            ]
        for page_num in module_matches:
            selected.update(range(max(1, page_num - 1), min(len(pages), page_num + 1) + 1))
    return selected


def _json_object(text: str) -> dict[str, Any]:
    value = text.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*|\s*```$", "", value, flags=re.IGNORECASE)
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        start = value.find("{")
        if start < 0:
            raise ValueError("AI_INVALID_JSON: no JSON object returned")
        parsed = None
        depth = 0
        quoted = False
        escaped = False
        end = -1
        for index, char in enumerate(value[start:], start=start):
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
                continue
            if char == '"':
                quoted = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    end = index + 1
                    break
        if end < 0:
            raise ValueError("AI_INVALID_JSON: incomplete JSON object")
        parsed = json.loads(value[start:end])
    if not isinstance(parsed, dict):
        raise ValueError("AI_INVALID_JSON: top-level value must be an object")
    return parsed


def _contains(haystack: str, needle: str) -> bool:
    return bool(_normal(needle)) and _normal(needle) in _normal(haystack)


def _rect_array(rect: fitz.Rect) -> list[float]:
    return [round(float(rect.x0), 2), round(float(rect.y0), 2), round(float(rect.x1), 2), round(float(rect.y1), 2)]


def _header_aliases(rule_text: str) -> dict[str, list[str]]:
    aliases: dict[str, list[str]] = {}
    in_section = False
    for line in rule_text.splitlines():
        if line.startswith("## "):
            in_section = line[3:].strip() == "pdf_header_aliases"
            continue
        if not in_section or not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 2 or cells[0].strip("` ") == "column_key" or set(cells[0].strip()) <= {"-", ":"}:
            continue
        key = cells[0].strip("` ")
        values = re.split(r"[、，,；;]", cells[1].strip("` "))
        aliases[key] = [value.strip(" `。；;，,") for value in values if value.strip(" `。；;，,")]
    return aliases


def _collect_tables(document: Any, page_numbers: set[int]) -> dict[int, list[dict[str, Any]]]:
    result: dict[int, list[dict[str, Any]]] = {}
    for page_number in sorted(page_numbers):
        page = document[page_number - 1]
        try:
            tables = page.find_tables().tables
        except Exception:
            tables = []
        records = []
        for table in tables:
            try:
                matrix = table.extract()
                columns = int(table.col_count)
                cells_by_row = [list(row.cells) for row in table.rows]
            except Exception:
                continue
            if matrix and columns > 0:
                records.append({
                    "matrix": matrix,
                    "columns": columns,
                    "cells_by_row": cells_by_row,
                    "page_width": float(page.rect.width),
                })
        result[page_number] = records
    return result
def _table_bbox(
    tables: dict[int, list[dict[str, Any]]],
    header_maps: dict[str, dict[int, dict[int, dict[str, int]]]],
    page_number: int,
    rule_id: str,
    row_label: str,
    raw_value: str,
    field_key: str | None,
) -> list[float] | None:
    matches: list[fitz.Rect] = []
    page_maps = header_maps.get(rule_id, {}).get(page_number, {})
    for table_index, table in enumerate(tables.get(page_number, [])):
        mapping = page_maps.get(table_index)
        if not mapping or (field_key is not None and field_key not in mapping):
            continue
        matrix = table["matrix"]
        for row_index, row in enumerate(matrix):
            if not any(_normal(cell) == _normal(row_label) for cell in row if cell is not None):
                continue
            row_cells = table["cells_by_row"][row_index]
            if field_key is not None:
                column_index = mapping[field_key]
                if column_index >= len(row) or not _cell_matches(row[column_index], raw_value):
                    continue
                if column_index < len(row_cells) and row_cells[column_index] is not None:
                    matches.append(fitz.Rect(row_cells[column_index]))
            else:
                row_rects = [fitz.Rect(cell) for cell in row_cells if cell is not None]
                if row_rects:
                    matches.append(fitz.Rect(
                        min(rect.x0 for rect in row_rects),
                        min(rect.y0 for rect in row_rects),
                        max(rect.x1 for rect in row_rects),
                        max(rect.y1 for rect in row_rects),
                    ))
    return _rect_array(matches[0]) if len(matches) == 1 else None


def _cross_page_column_tolerance(rule_text: str) -> float:
    in_section = False
    for line in rule_text.splitlines():
        if line.startswith("## "):
            in_section = line[3:].strip() == "cross_page_table_policy"
            continue
        if not in_section or not line.startswith("|"):
            continue
        cells = [cell.strip().strip("` ") for cell in line.strip("|").split("|")]
        if len(cells) >= 2 and cells[0] == "normalized_column_boundary_tolerance":
            return float(cells[1])
    raise ValueError("CROSS_PAGE_TABLE_POLICY_MISSING: normalized_column_boundary_tolerance")


def _header_column_maps(
    tables: dict[int, list[dict[str, Any]]],
    aliases_by_rule: dict[str, dict[str, list[str]]],
    *,
    column_tolerance: float,
) -> dict[str, dict[int, dict[int, dict[str, int]]]]:
    maps: dict[str, dict[int, dict[int, dict[str, int]]]] = {}
    for rule_id, aliases in aliases_by_rule.items():
        maps[rule_id] = {}
        required = {
            "investment.asset_allocation": {"amount", "ratio_of_total_assets"},
            "investment.industry_stock_portfolio": {"industry_code", "industry_name", "fair_value", "ratio_of_net_assets"},
            "investment.stock_holdings": {"quantity", "fair_value", "ratio_of_net_assets"},
        }[rule_id]
        header_tables: dict[int, set[int]] = {}
        for page_number in sorted(tables):
            records = tables[page_number]
            for table_index, table in enumerate(records):
                columns = table["columns"]
                mapping: dict[str, int] = {}
                for key, terms in aliases.items():
                    hits = []
                    for col in range(columns):
                        header_text = " ".join(
                            str(row[col] or "")
                            for row in table["matrix"][:3]
                            if col < len(row)
                        )
                        if any(_normal(term) in _normal(header_text) for term in terms):
                            hits.append(col)
                    if len(hits) == 1:
                        mapping[key] = hits[0]
                if mapping:
                    header_tables.setdefault(page_number, set()).add(table_index)
                if required.issubset(mapping):
                    maps[rule_id].setdefault(page_number, {})[table_index] = mapping

        # Join page fragments like the PyMuPDF pattern: compare adjacent pages,
        # keep the prior schema, and skip repeated headers on the next fragment.
        # For a missing header, only link the prior page's last table to the
        # next page's first headerless table when their column grids agree.
        ordered_pages = sorted(tables)
        for previous_page, page_number in zip(ordered_pages, ordered_pages[1:]):
            if page_number != previous_page + 1:
                continue
            previous_records = tables[previous_page]
            current_records = tables[page_number]
            previous_maps = maps[rule_id].get(previous_page, {})
            current_maps = maps[rule_id].get(page_number, {})
            previous_candidates = []
            if previous_records:
                index = len(previous_records) - 1
                if index in previous_maps:
                    previous_candidates.append((index, previous_records[index], previous_maps[index]))
            current_candidates = []
            if current_records and 0 not in current_maps and 0 not in header_tables.get(page_number, set()):
                current_candidates.append((0, current_records[0]))
            if previous_candidates and current_candidates:
                previous_index, previous_record, mapping = previous_candidates[0]
                current_index, current_record = current_candidates[0]
                if _tables_have_compatible_columns(previous_record, current_record, column_tolerance):
                    maps[rule_id].setdefault(page_number, {})[current_index] = mapping.copy()
    return maps


def _table_column_boundaries(
    table: dict[str, Any],
    tolerance: float,
) -> list[tuple[float, float]] | None:
    columns = table.get("columns")
    page_width = table.get("page_width")
    if not columns or not page_width:
        return None
    candidates = []
    for row_cells in table.get("cells_by_row", []):
        if len(row_cells) != columns or any(cell is None for cell in row_cells):
            continue
        rects = [fitz.Rect(cell) for cell in row_cells]
        if any(rect.width <= 0 for rect in rects):
            continue
        boundaries = [(rect.x0 / page_width, rect.x1 / page_width) for rect in rects]
        if all(boundaries[i][1] <= boundaries[i + 1][0] + tolerance for i in range(len(boundaries) - 1)):
            candidates.append(boundaries)
    if not candidates:
        return None
    return candidates[0]


def _tables_have_compatible_columns(
    left: dict[str, Any],
    right: dict[str, Any],
    tolerance: float,
) -> bool:
    if left.get("columns") != right.get("columns"):
        return False
    left_boundaries = _table_column_boundaries(left, tolerance)
    right_boundaries = _table_column_boundaries(right, tolerance)
    if left_boundaries is None or right_boundaries is None or len(left_boundaries) != len(right_boundaries):
        return False
    return all(
        abs(left_edge - right_edge) <= tolerance and abs(left_end - right_end) <= tolerance
        for (left_edge, left_end), (right_edge, right_end) in zip(left_boundaries, right_boundaries)
    )


def _cell_matches(actual: Any, expected: str) -> bool:
    actual_text = str(actual or "").strip()
    if not actual_text:
        return False
    actual_number = normalize_numeric(actual_text)
    expected_number = normalize_numeric(expected)
    if actual_number["kind"] in {"NUMBER", "DASH"} and expected_number["kind"] in {"NUMBER", "DASH"}:
        return actual_number == expected_number
    return _normal(actual_text) == _normal(expected)




def _evidence(
    document: Any,
    pages: list[dict[str, Any]],
    source: Any,
    *,
    table_cache: dict[int, list[dict[str, Any]]] | None = None,
    header_maps: dict[str, dict[int, dict[int, dict[str, int]]]] | None = None,
    rule_id: str | None = None,
    row_label: str | None = None,
    field_key: str | None = None,
) -> dict[str, Any]:
    if not isinstance(source, dict):
        return {"page": None, "quote": "", "bbox": None, "page_width": None, "page_height": None, "status": "UNVERIFIED"}
    try:
        page_number = int(source.get("page"))
    except (TypeError, ValueError):
        page_number = 0
    quote = str(source.get("quote") or "").strip()
    if not 1 <= page_number <= len(pages) or not quote:
        return {"page": None, "quote": quote, "bbox": None, "page_width": None, "page_height": None, "status": "UNVERIFIED"}
    page_info = pages[page_number - 1]
    raw_value = str(source.get("raw_text") or "").strip()
    if table_cache is not None and header_maps is not None and rule_id and row_label:
        if field_key is None:
            # Table row text is often reordered by PDF reading order: wrapped
            # labels can be interleaved with values from neighboring cells.
            # The exact row label and a unique mapped table row are stronger
            # evidence than requiring the model's quote to be contiguous.
            bbox = _table_bbox(table_cache, header_maps, page_number, rule_id, row_label, raw_value, field_key)
            if bbox:
                return {"page": page_number, "quote": quote, "bbox": bbox, "page_width": page_info["width"], "page_height": page_info["height"], "status": "LOCATED"}
        elif raw_value and _contains(quote, raw_value):
            bbox = _table_bbox(table_cache, header_maps, page_number, rule_id, row_label, raw_value, field_key)
            if bbox:
                return {"page": page_number, "quote": quote, "bbox": bbox, "page_width": page_info["width"], "page_height": page_info["height"], "status": "LOCATED"}
    if not _contains(page_info["text"], quote):
        return {"page": page_number, "quote": quote, "bbox": None, "page_width": page_info["width"], "page_height": page_info["height"], "status": "UNVERIFIED"}
    if field_key is None and row_label and not _contains(quote, row_label):
        return {"page": page_number, "quote": quote, "bbox": None, "page_width": page_info["width"], "page_height": page_info["height"], "status": "UNVERIFIED"}
    if field_key is not None and raw_value and not _contains(quote, raw_value):
        return {"page": page_number, "quote": quote, "bbox": None, "page_width": page_info["width"], "page_height": page_info["height"], "status": "UNVERIFIED"}
    page = document[page_number - 1]
    rects: list[fitz.Rect] = []
    if raw_value and raw_value not in {"-", "—", "–", "－"}:
        rects = page.search_for(raw_value, quads=False)
    if len(rects) != 1:
        quote_rects = page.search_for(quote, quads=False)
        rects = quote_rects if len(quote_rects) == 1 else []
    if len(rects) != 1:
        return {"page": page_number, "quote": quote, "bbox": None, "page_width": page_info["width"], "page_height": page_info["height"], "status": "QUOTE_ONLY"}
    return {"page": page_number, "quote": quote, "bbox": _rect_array(rects[0]), "page_width": page_info["width"], "page_height": page_info["height"], "status": "LOCATED"}


def _verify_field(
    document: Any,
    pages: list[dict[str, Any]],
    value: Any,
    field_name: str,
    *,
    rule_id: str,
    row_label: str,
    table_cache: dict[int, list[dict[str, Any]]],
    header_maps: dict[str, dict[int, dict[int, dict[str, int]]]],
) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        value = {"raw_text": value}
    raw = str(value.get("raw_text") or "").strip()
    if not raw:
        return None
    source = {"page": value.get("page"), "quote": value.get("quote"), "raw_text": raw}
    evidence = _evidence(
        document, pages, source, table_cache=table_cache, header_maps=header_maps,
        rule_id=rule_id, row_label=row_label, field_key=field_name,
    )
    normalized = normalize_numeric(raw, percent=field_name.endswith("ratio"))
    return {
        "raw_text": raw,
        "normalized_value": normalized["value"],
        "value_kind": normalized["kind"],
        "unit": str(value.get("unit") or ""),
        "evidence": evidence,
    }


def _iso_dates(text: str) -> set[str]:
    dates = set()
    pattern = re.compile(r"(20\d{2})\s*[年/.-]\s*(\d{1,2})\s*[月/.-]\s*(\d{1,2})\s*日?")
    for year, month, day in pattern.findall(unicodedata.normalize("NFKC", text)):
        try:
            dates.add(f"{int(year):04d}-{int(month):02d}-{int(day):02d}")
        except ValueError:
            continue
    return dates


def _metadata_value_matches(key: str, value: str, quote: str) -> bool:
    if key in {"disclosure_date", "period_end"}:
        target = value.replace("/", "-").strip()
        return bool(re.fullmatch(r"20\d{2}-\d{2}-\d{2}", target) and target in _iso_dates(quote))
    return _contains(quote, value)


def _verify_metadata(document: Any, pages: list[dict[str, Any]], raw: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    info: dict[str, Any] = {}
    proofs: dict[str, Any] = {}
    if not isinstance(raw, dict):
        raw = {}
    for key in ("fund_name", "fund_code", "disclosure_date", "period_end"):
        item = raw.get(key)
        if isinstance(item, dict):
            value = str(item.get("value") or "").strip()
            quote = str(item.get("quote") or "").strip()
            evidence = _evidence(document, pages, {"page": item.get("page"), "quote": quote, "raw_text": value})
            if value and evidence["status"] in {"LOCATED", "QUOTE_ONLY"} and _metadata_value_matches(key, value, quote):
                info[key] = value.replace("/", "-") if key in {"disclosure_date", "period_end"} else value
                proofs[key] = evidence
            else:
                info[key] = None
        else:
            info[key] = None
    return info, proofs



def _verify_module(
    document: Any,
    pages: list[dict[str, Any]],
    raw_module: Any,
    rule: dict[str, Any],
    table_cache: dict[int, list[dict[str, Any]]],
    header_maps: dict[str, dict[int, dict[int, dict[str, int]]]],
) -> dict[str, Any]:
    if not isinstance(raw_module, dict) or not raw_module.get("found"):
        return {"rule_id": rule["rule_id"], "title": rule["title"], "status": "MISSING_MODULE", "scope": {}, "rows": [], "evidence": []}
    rows = raw_module.get("rows")
    if not isinstance(rows, list):
        return {"rule_id": rule["rule_id"], "title": rule["title"], "status": "UNRESOLVED_STRUCTURE", "scope": {}, "rows": [], "evidence": []}
    validated_rows = []
    unresolved = False
    for index, raw_row in enumerate(rows):
        if not isinstance(raw_row, dict):
            unresolved = True
            continue
        row_label = str(raw_row.get("row_label") or "").strip()
        row_quote = str(raw_row.get("row_quote") or "").strip()
        row_page = raw_row.get("row_page")
        fields_in = raw_row.get("fields") if isinstance(raw_row.get("fields"), dict) else {}
        fields: dict[str, Any] = {}
        for field_key in rule["fields"]:
            field_value = fields_in.get(field_key)
            if field_value is None:
                continue
            validated = _verify_field(
                document, pages, field_value, str(field_key),
                rule_id=rule["rule_id"], row_label=row_label,
                table_cache=table_cache, header_maps=header_maps,
            )
            if validated is None:
                unresolved = True
                continue
            if validated["value_kind"] == "UNRESOLVED" or validated["evidence"]["status"] != "LOCATED":
                unresolved = True
            fields[str(field_key)] = validated
        row_evidence = _evidence(
            document, pages,
            {"page": row_page, "quote": row_quote, "raw_text": row_label},
            table_cache=table_cache, header_maps=header_maps,
            rule_id=rule["rule_id"], row_label=row_label,
        )
        total_row_with_located_metrics = (
            rule["rule_id"] == "investment.industry_stock_portfolio"
            and _normal(row_label) == "合计"
            and all(
                key in fields and fields[key]["evidence"]["status"] == "LOCATED"
                for key in rule["fields"]
            )
        )
        if not row_label or (row_evidence["status"] != "LOCATED" and not total_row_with_located_metrics):
            unresolved = True
        identity_values = (
            raw_row.get("industry_code"), raw_row.get("industry_name"),
            raw_row.get("stock_code"), raw_row.get("stock_name"),
        )
        if row_evidence["status"] != "LOCATED" and any(value and _normal(value) not in _normal(row_quote) for value in identity_values):
            unresolved = True
        row_key = str(raw_row.get("row_key") or "")
        if rule["rule_id"] == "investment.asset_allocation":
            allowed_keys = set(re.findall(r"^\|\s*`([^`]+)`\s*\|", Path(rule["file"]).read_text(encoding="utf-8"), re.MULTILINE))
            if row_key and row_key not in allowed_keys:
                row_key = ""
                unresolved = True
        validated_rows.append({
            "row_id": row_key or str(raw_row.get("row_id") or f"unmapped:{index}"),
            "row_key": row_key,
            "label": row_label,
            "parent_key": str(raw_row.get("parent_key") or "") or None,
            "rank": raw_row.get("rank"),
            "rank_evidence": row_evidence,
            "row_evidence": row_evidence,
            "market_scope": str(raw_row.get("market_scope") or raw_module.get("scope", {}).get("market_scope") or "unknown"),
            "investment_scope": str(raw_row.get("investment_scope") or raw_module.get("scope", {}).get("investment_scope") or "unknown"),
            "industry_code": str(raw_row.get("industry_code") or "").strip(),
            "industry_name": str(raw_row.get("industry_name") or "").strip(),
            "stock_code": str(raw_row.get("stock_code") or "").strip(),
            "stock_name": str(raw_row.get("stock_name") or "").strip(),
            "fields": fields,
        })
    required_fields = tuple(rule["fields"])
    if not validated_rows or any(not all(key in row["fields"] for key in required_fields) for row in validated_rows):
        unresolved = True
    scope_raw = raw_module.get("scope") if isinstance(raw_module.get("scope"), dict) else {}
    scope = {str(key): value for key, value in scope_raw.items() if value is not None}
    return {
        "rule_id": rule["rule_id"],
        "title": rule["title"],
        "status": "UNRESOLVED_STRUCTURE" if unresolved else "EXTRACTED",
        "scope": scope,
        "rows": validated_rows,
        "evidence": [],
    }


async def analyze_document(
    *, filename: str, content: bytes, role: str, provider: Any,
    trace_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Use AnyDoc for table structure and PyMuPDF for physical-page evidence."""
    if role not in {"prospectus", "periodic_report"}:
        raise ValueError("DOCUMENT_ROLE_INVALID")
    pdf_started = time.monotonic()
    _trace_event(trace_callback, "pymupdf_started", role=role, details={"filename": Path(filename).name})
    document, pages = _read_pdf(content, filename)
    _trace_event(trace_callback, "pymupdf_completed", role=role, details={
        "page_count": len(pages),
        "text_chars": sum(len(page["text"]) for page in pages),
        "duration_ms": round((time.monotonic() - pdf_started) * 1000),
    })
    _trace_artifact(trace_callback, f"{role}/pymupdf_pages.json", pages, role=role)
    try:
        rule_texts = {rule["rule_id"]: Path(rule["file"]).read_text(encoding="utf-8") for rule in MODULE_RULES}
        common_rules = COMMON_RULE_FILE.read_text(encoding="utf-8")
        _trace_artifact(trace_callback, "rules/common_公共规则.md", common_rules, role=role)
        for rule_id, rule_text in rule_texts.items():
            _trace_artifact(
                trace_callback,
                f"rules/{rule_id.replace('.', '_')}.md",
                rule_text,
                role=role,
            )
        anydoc_started = time.monotonic()
        _trace_event(trace_callback, "anydoc_started", role=role, details={"filename": Path(filename).name})
        try:
            markdown = await asyncio.to_thread(anydoc.to_markdown_bytes, content, "pdf")
        except Exception as exc:
            _trace_event(trace_callback, "anydoc_failed", role=role, details={"error": f"{type(exc).__name__}: {exc}"})
            raise ValueError(f"ANYDOC_PDF_PARSE_FAILED: {filename}: {exc}") from exc
        if isinstance(markdown, bytes):
            markdown = markdown.decode("utf-8", errors="replace")
        markdown = str(markdown)
        _trace_artifact(trace_callback, f"{role}/anydoc.md", markdown, role=role)
        _trace_event(trace_callback, "anydoc_completed", role=role, details={
            "markdown_chars": len(markdown),
            "duration_ms": round((time.monotonic() - anydoc_started) * 1000),
        })
        chapter_started = time.monotonic()
        markdown_sections = _anydoc_sections(markdown, rule_texts)
        chapter_contexts = _chapter_contexts(markdown_sections, pages, rule_texts)
        _trace_artifact(trace_callback, f"{role}/chapter_contexts.json", chapter_contexts, role=role)
        _trace_event(trace_callback, "chapter_aggregation_completed", role=role, details={
            "sections": {key: len(value.get("section_markdown", "")) for key, value in chapter_contexts.items()},
            "duration_ms": round((time.monotonic() - chapter_started) * 1000),
        })
        candidate_numbers = _candidate_pages(pages, rule_texts)
        for section in markdown_sections.values():
            candidate_numbers.update(_physical_pages_in_markdown(section, pages))
        tables_started = time.monotonic()
        table_cache = _collect_tables(document, candidate_numbers)
        aliases_by_rule = {rule["rule_id"]: _header_aliases(rule_texts[rule["rule_id"]]) for rule in MODULE_RULES}
        header_maps = _header_column_maps(
            table_cache,
            aliases_by_rule,
            column_tolerance=_cross_page_column_tolerance(common_rules),
        )
        table_snapshot = {
            str(page_number): [
                {
                    "matrix": table["matrix"],
                    "columns": table["columns"],
                    "cells_by_row": [
                        [list(map(float, cell)) if cell is not None else None for cell in row]
                        for row in table["cells_by_row"]
                    ],
                }
                for table in tables
            ]
            for page_number, tables in table_cache.items()
        }
        _trace_artifact(trace_callback, f"{role}/pymupdf_tables.json", {
            "candidate_pages": sorted(candidate_numbers),
            "tables": table_snapshot,
            "header_maps": header_maps,
        }, role=role)
        _trace_event(trace_callback, "table_extraction_completed", role=role, details={
            "candidate_pages": len(candidate_numbers),
            "tables_found": sum(len(tables) for tables in table_cache.values()),
            "duration_ms": round((time.monotonic() - tables_started) * 1000),
        })
        page_map = {page["page"]: page for page in pages}
        candidate_text = "\n\n".join(
            f"[PHYSICAL PAGE {number}]\n{page_map[number]['text']}"
            for number in sorted(candidate_numbers)
        )
        chapter_context = _format_chapter_contexts(chapter_contexts)
        if len(candidate_text) + len(chapter_context) > MAX_ANALYSIS_CHARS:
            raise ValueError(
                f"PDF_ANALYSIS_LIMIT_EXCEEDED: {filename}: relevant page/table context exceeds "
                f"{MAX_ANALYSIS_CHARS} characters; no content was truncated"
            )
        module_rules = "\n\n".join(f"## {rule['rule_id']}\n{rule_texts[rule['rule_id']]}" for rule in MODULE_RULES)
        system_prompt = """你是基金定期报告与招募说明书信息结构化读取器。工作顺序必须是：先阅读按章节归集的完整上下文，再按对应模块规则识别该章节中的信息。章节可能跨页，章节内容可能包含正文、表格、表注和列表；不要把物理页边界当作章节边界，也不要只因相邻页就合并不同章节。AnyDoc Markdown 提供章节内结构与顺序；PyMuPDF 原文区块提供物理页码、逐字引用和坐标核验。只能从原文提取，不推断、不补全、不重算、不把目录文字当实际披露内容。章节边界或行列关系不确定时标记 unknown。严格只返回一个 JSON 对象，不要 Markdown。每个单元格必须带原文 raw_text、physical page、exact quote；quote 必须是对应 [PHYSICAL PAGE N] 上连续、逐字的原文片段并包含 raw_text，不要把不连续文字拼接在一起；为包含目标值而跨过同一行的其他单元格时，中间文字也要照录。每行还必须返回 row_page 和逐字 row_quote，row_quote 覆盖行标签及身份字段。AI 不得输出坐标。横线保持原样，不能转为 0。没找到写 found=false。基金名、基金代码、披露日期、报告期末日期仅在原文可证时返回；所有元数据也带 page 与 quote。"""
        expected = {
            "document_info": {
                "fund_name": {"value": "", "page": 1, "quote": ""},
                "fund_code": {"value": "", "page": 1, "quote": ""},
                "disclosure_date": {"value": "YYYY-MM-DD", "page": 1, "quote": ""},
                "period_end": {"value": "YYYY-MM-DD", "page": 1, "quote": ""},
            },
            "modules": [
                {"rule_id": "investment.asset_allocation", "found": False, "scope": {}, "rows": [
                    {"row_key": "equity_investment", "row_label": "权益投资", "parent_key": None, "row_page": 55, "row_quote": "权益投资 4,722,393,105.64 99.13",
                     "fields": {"amount": {"raw_text": "4,722,393,105.64", "unit": "元", "page": 55,
                                            "quote": "权益投资 4,722,393,105.64 99.13"},
                                "ratio_of_total_assets": {"raw_text": "99.13", "unit": "%", "page": 55,
                                                          "quote": "权益投资 4,722,393,105.64 99.13"}}}
                ]},
                {"rule_id": "investment.industry_stock_portfolio", "found": False, "scope": {"market_scope": "domestic"}, "rows": [
                    {"row_label": "制造业", "industry_code": "C", "industry_name": "制造业", "market_scope": "domestic", "row_page": 56, "row_quote": "C 制造业 100.00 2.00",
                     "fields": {"fair_value": {"raw_text": "100.00", "unit": "元", "page": 56, "quote": "C 制造业 100.00 2.00"},
                                "ratio_of_net_assets": {"raw_text": "2.00", "unit": "%", "page": 56, "quote": "C 制造业 100.00 2.00"}}}
                ]},
                {"rule_id": "investment.stock_holdings", "found": False, "scope": {"investment_scope": "ordinary"}, "rows": [
                    {"row_label": "示例股票", "stock_code": "600000", "stock_name": "示例股票", "rank": 1, "row_page": 56, "row_quote": "1 600000 示例股票 10 100 1.0",
                     "fields": {"quantity": {"raw_text": "10", "unit": "股", "page": 56, "quote": "1 600000 示例股票 10"},
                                "fair_value": {"raw_text": "100", "unit": "元", "page": 56, "quote": "1 600000 示例股票 10 100 1.0"},
                                "ratio_of_net_assets": {"raw_text": "1.0", "unit": "%", "page": 56, "quote": "1 600000 示例股票 10 100 1.0"}}}
                ]}
            ]
        }
        user_prompt = (
            f"文档角色：{role}。上传文件名：{filename}。\n"
            "下面是输出结构示例，字段只作结构示例，不能照抄示例值；最终必须按实际页面输出。"
            f"\nJSON 结构：{json.dumps(expected, ensure_ascii=False)}\n"
            f"\n【公共规则】\n{common_rules}\n【三个模块规则】\n{module_rules}\n"
            f"\n【按章节归集的内容与来源区块】\n{chapter_context}\n"
            f"\n【其他候选页原文，主要用于文档元数据和章节定位】\n{candidate_text}"
        )
        _trace_artifact(trace_callback, f"{role}/model_request.json", {
            "provider": type(provider).__name__,
            "model": getattr(provider, "model_name", None),
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
        }, role=role)
        _trace_event(trace_callback, "model_request_started", role=role, details={
            "provider": type(provider).__name__,
            "model": getattr(provider, "model_name", None),
            "prompt_chars": len(system_prompt) + len(user_prompt),
        })
        model_started = time.monotonic()
        model_response = await provider.generate_text(system_prompt, user_prompt)
        _trace_artifact(trace_callback, f"{role}/model_response.txt", model_response, role=role)
        _trace_event(trace_callback, "model_response_received", role=role, details={
            "response_chars": len(model_response),
            "duration_ms": round((time.monotonic() - model_started) * 1000),
        })
        raw_result = _json_object(model_response)
        _trace_artifact(trace_callback, f"{role}/model_candidate.json", raw_result, role=role)
        module_items = raw_result.get("modules") if isinstance(raw_result.get("modules"), list) else []
        by_id = {str(item.get("rule_id")): item for item in module_items if isinstance(item, dict)}
        verification_started = time.monotonic()
        document_info, metadata_evidence = _verify_metadata(document, pages, raw_result.get("document_info"))
        modules = [
            _verify_module(document, pages, by_id.get(rule["rule_id"]), rule, table_cache, header_maps)
            for rule in MODULE_RULES
        ]
        result = {
            "filename": Path(filename).name,
            "page_count": len(pages),
            "page_dimensions": [{"width": page["width"], "height": page["height"]} for page in pages],
            "document_info": document_info,
            "metadata_evidence": metadata_evidence,
            "extraction_provenance": {
                "table_structure": "AnyDoc",
                "chapter_aggregation": "AnyDoc section content with PyMuPDF source blocks",
                "page_and_bbox_evidence": "PyMuPDF",
                "cross_page_markdown_sections": sorted(markdown_sections),
            },
            "modules": modules,
        }
        _trace_artifact(trace_callback, f"{role}/verified_result.json", result, role=role)
        _trace_event(trace_callback, "evidence_verification_completed", role=role, details={
            "modules": {module["rule_id"]: module["status"] for module in modules},
            "duration_ms": round((time.monotonic() - verification_started) * 1000),
            "located_fields": sum(
                1
                for module in modules
                for row in module.get("rows", [])
                for field in row.get("fields", {}).values()
                if field.get("evidence", {}).get("status") == "LOCATED"
            ),
        })
        return result
    except Exception as exc:
        _trace_event(trace_callback, "document_analysis_failed", role=role, details={
            "error": f"{type(exc).__name__}: {exc}",
        })
        raise
    finally:
        document.close()
