from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import unicodedata
from datetime import date
from html import unescape
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable

import pymupdf as fitz

from app.domain import COMMON_RULE_FILE, MODULE_RULES, normalize_numeric, title_aliases
from app.chapter_sources import ChapterCache, _centers_map_uniquely, _header_centers, bind_sources, column_unit, markdown_rows, render_catalogue, resolve_data_columns, source_catalogue, source_gaps
from app.pdf_worker import run_pdf
from app.pdf_document import page_tables, require_ready, verified_conversion
from app.headings import parse_heading

MAX_PDF_PAGES = int(os.getenv("PROSPECTUS_MAX_PAGES", "240"))
MAX_ANALYSIS_CHARS = int(os.getenv("PROSPECTUS_MAX_ANALYSIS_CHARS", "150000"))
SECTION_SCORE_LOOKAHEAD = 35
SECTION_PARENT_SCORE_TOLERANCE = 5
CONTENTS_LINE_PENALTY = 100
MODEL_IDENTITY_FIELDS = (
    "row_key", "row_id", "row_label", "parent_key", "industry_code",
    "industry_name", "stock_code", "stock_name", "rank", "market_scope",
    "investment_scope",
)


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
    if not spans and (heading := parse_heading(line)) and len(heading.title)>=10:
        title, _ = _compact_with_offsets(heading.title)
        # Match a whole heading, never a similar sentence inside a table or
        # paragraph. Keep the original title, including its disclosure scope.
        matches = [SequenceMatcher(None, _compact_with_offsets(alias)[0], title, autojunk=False)
                   for alias in aliases if len(alias)>=10]
        # Accept small insertions while retaining every character of the
        # configured title. Similarity alone could confuse stocks with bonds.
        compatible = any(match.ratio()>=.9 and all(tag in {'equal','insert'}
                         for tag,*_ in match.get_opcodes()) for match in matches)
        start = compact_line.find(title)
        if start>=0 and compatible:
            spans.add((offsets[start], offsets[start+len(title)-1]+1))
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _heading_depth(text: str) -> int | None:
    heading = parse_heading(text)
    return heading.depth if heading else None


def _anydoc_sections(markdown: str, rule_texts: dict[str, str]) -> dict[str, str]:
    """Group each module's complete AnyDoc section, excluding TOC hits and the next peer section."""
    lines = markdown.splitlines()
    candidates: list[tuple[int, int, int, str]] = []
    for rule in MODULE_RULES:
        rule_id = rule["rule_id"]
        aliases = title_aliases(rule_texts[rule_id])
        for index, line in enumerate(lines):
            for span in _alias_spans(line, aliases):
                start = 0 if parse_heading(line) else span[0]
                candidates.append((index, start, span[1], rule_id))

    def score(candidate: tuple[int, int, int, str]) -> int:
        index, offset, _, rule_id = candidate
        end = min(len(lines), index + SECTION_SCORE_LOOKAHEAD + 1)
        depth = _heading_depth(lines[index])
        if depth is not None:
            for next_index in range(index + 1, end):
                next_depth = _heading_depth(lines[next_index])
                if next_depth is not None and next_depth <= depth:
                    end = next_index
                    break
        window = lines[index][offset:] + "\n" + "\n".join(lines[index + 1:end])
        table_lines = len(markdown_rows(window))
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

    def heading_number(candidate: tuple[int, int, int, str]) -> str | None:
        heading = parse_heading(lines[candidate[0]])
        return heading.number if heading and heading.style=='arabic' else None

    selected: dict[str, list[tuple[int, int, int, str]]] = {}
    for rule in MODULE_RULES:
        options = [candidate for candidate in candidates if candidate[3] == rule["rule_id"]]
        if options:
            best_score = max(score(candidate) for candidate in options)
            if best_score >= 0:
                # Keep a nearby parent section heading when its child heading
                # scores marginally higher because the table begins below it.
                plausible = [
                    candidate for candidate in options
                    if score(candidate) >= best_score - SECTION_PARENT_SCORE_TOLERANCE
                ]
                plausible.sort(key=lambda candidate: (candidate[0], candidate[1]))
                shallowest = min(
                    (_heading_depth(lines[candidate[0]]) or _heading_depth(lines[candidate[0]][candidate[1]:]) or 99)
                    for candidate in plausible
                )
                parent_candidates = [
                    candidate for candidate in plausible
                    if (_heading_depth(lines[candidate[0]]) or _heading_depth(lines[candidate[0]][candidate[1]:]) or 99) == shallowest
                ]
                if shallowest < 99:
                    parent_number = heading_number(parent_candidates[0])
                    if parent_number:
                        parent_prefix = parent_number
                        # A selected parent heading owns all child sections;
                        # otherwise retain sibling module headings under one
                        # numbered parent (for example index and active lists).
                        descendants = [
                            candidate for candidate in plausible
                            if (number := heading_number(candidate)) and number.startswith(parent_prefix + ".")
                        ]
                        selected[rule["rule_id"]] = [parent_candidates[0]] if descendants else [
                            candidate for candidate in plausible
                            if (number := heading_number(candidate))
                            and number.rsplit(".", 1)[0] == parent_prefix.rsplit(".", 1)[0]
                            and (_heading_depth(lines[candidate[0]]) or _heading_depth(lines[candidate[0]][candidate[1]:]) or 99) == shallowest
                        ]
                        if not selected[rule["rule_id"]]:
                            selected[rule["rule_id"]] = [parent_candidates[0]]
                    else:
                        selected[rule["rule_id"]] = [parent_candidates[0]]
                else:
                    selected[rule["rule_id"]] = [plausible[0]]
            else:
                selected[rule["rule_id"]] = [max(options, key=lambda candidate: (candidate[0], candidate[1]))]

    ordered = sorted(candidate for group in selected.values() for candidate in group)
    sections: dict[str, str] = {}
    gathered: dict[str, list[str]] = {}
    for start_line, start_offset, _, rule_id in ordered:
        prefix = lines[start_line][:start_offset]
        depth = _heading_depth(lines[start_line]) or _heading_depth(lines[start_line][start_offset:]) or _heading_depth(prefix)
        boundaries = [(line_no,offset) for line_no,offset,_,other_id in ordered
                      if other_id!=rule_id and (line_no,offset)>(start_line,start_offset)]
        for index in range(start_line+1,len(lines)):
            candidate_depth = _heading_depth(lines[index])
            if candidate_depth is not None and depth is not None and candidate_depth<=depth:
                boundaries.append((index,0))
                break
        end_line,end_offset = min(boundaries,default=(len(lines),0))
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
        gathered.setdefault(rule_id, []).append(content.strip())
    for rule_id, chunks in gathered.items():
        sections[rule_id] = "\n\n".join(chunk for chunk in chunks if chunk)
    return sections


def _physical_pages_in_markdown(section: str, pages: list[dict[str, Any]]) -> set[int]:
    """Map AnyDoc's printed footers and post-footer continuation to PDF pages."""
    explicit = {int(n) for marker in re.findall(r'<!-- source-pages: ([\d,]+) -->', section) for n in marker.split(',')}
    if explicit:
        return explicit & {int(p["page"]) for p in pages}
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
        # Markdown rows retain cell order even when PDF text wraps or interleaves cells.
        # Unique row anchors also locate continuation pages without printed footers.
        page_texts = {int(page["page"]): _normal(page["text"]) for page in pages}
        for cells in markdown_rows(section):
            tokens = [_normal(cell) for cell in cells if _normal(cell)]
            if len(tokens) < 2 or not any(len(token) >= 3 and normalize_numeric(token)["kind"] == "UNRESOLVED" for token in tokens):
                continue
            hits = [number for number, text in page_texts.items() if all(token in text for token in tokens)]
            if len(hits) == 1:
                page_numbers.add(hits[0])
        # Paragraph anchors cover chapters whose continuation is prose, not a table.
        for line in section.splitlines():
            if "|" in line:
                continue
            text = _normal(re.sub(r"</?[^>]+>|[*#`]", "", line))
            if len(text) < 16:
                continue
            hits = [number for number, page_text in page_texts.items() if text in page_text]
            if len(hits) == 1:
                page_numbers.add(hits[0])
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


def _model_scalar(value: Any) -> tuple[str | None, bool]:
    """Normalize a model identity value without ever stringifying a JSON object."""
    wrapped = isinstance(value, dict)
    if wrapped:
        value = value.get("raw_text")
    if value is None:
        return None, wrapped
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None, wrapped
    return str(value).strip(), wrapped


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


def _json_object_after_key(text: str, key: str) -> dict[str, Any] | None:
    """Read a complete object value even when a later part of the response is malformed."""
    match = re.search(rf'"{re.escape(key)}"\s*:\s*\{{', text)
    if not match:
        return None
    start = text.find("{", match.start())
    depth = 0
    quoted = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
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
                try:
                    value = json.loads(text[start:index + 1])
                except json.JSONDecodeError:
                    return None
                return value if isinstance(value, dict) else None
    return None


def _module_retry_schema(rule: dict[str, Any]) -> dict[str, Any]:
    example_fields = {
        field: {"raw_text": "原文值", "unit": "原文单位", "page": 1, "quote": "含原文值的连续原文"}
        for field in rule["fields"]
    }
    return {
        "rule_id": rule["rule_id"],
        "found": True,
        "scope": {},
        "rows": [{
            "row_label": "原文行名",
            "row_key": "可选稳定键",
            "row_page": 1,
            "row_quote": "覆盖行名和身份字段的连续原文",
            "fields": example_fields,
        }],
    }


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


def _row_completeness_specs(rule_text: str) -> list[tuple[str, str]]:
    """Read optional row-recovery requirements from a module rule table."""
    specs = []
    in_section = False
    for line in rule_text.splitlines():
        if line.startswith("## "):
            in_section = line[3:].strip() == "row_completeness"
            continue
        if not in_section or not line.startswith("|"):
            continue
        cells = [cell.strip().strip("` ") for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2 or cells[0] == "row_label" or set(cells[0]) <= {"-", ":"}:
            continue
        if cells[0] and cells[1]:
            specs.append((cells[0], cells[1]))
    return specs


def _recover_required_source_rows(
    raw_module: dict[str, Any],
    rule: dict[str, Any],
    rule_text: str,
    source_pages: set[int],
    section_markdown: str,
    table_cache: dict[int, list[dict[str, Any]]],
    header_maps: dict[str, dict[int, dict[int, dict[str, int]]]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Recover a model-omitted row only from a unique, header-mapped PDF table row."""
    specs = [
        (label, policy)
        for label, policy in _row_completeness_specs(rule_text)
        if policy == "recover_from_unique_mapped_source_row_if_model_omits"
    ]
    if not specs or not source_pages:
        return [], []

    existing_rows = raw_module.get("rows") if isinstance(raw_module.get("rows"), list) else []
    existing_labels = {
        _normal(row.get("row_label") or row.get("industry_name") or row.get("label"))
        for row in existing_rows if isinstance(row, dict)
    }
    scope = raw_module.get("scope") if isinstance(raw_module.get("scope"), dict) else {}
    market_scope = scope.get("market_scope")
    recovered: list[dict[str, Any]] = []
    warnings: list[str] = []
    # A chapter may continue immediately after its last mapped printed-page
    # footer. Restrict this adjacency window to pages already selected for
    # document analysis, then rely on the module's mapped table columns below.
    module_table_pages = {
        page_number
        for page_number in table_cache
        if any(abs(page_number - source_page) <= 1 for source_page in source_pages)
    }

    for label, _ in specs:
        if _normal(label) in existing_labels:
            continue
        candidates = []
        ambiguous_source_rows = 0
        for page_number in sorted(module_table_pages):
            page_maps = header_maps.get(rule["rule_id"], {}).get(page_number, {})
            for table_index, table in enumerate(table_cache.get(page_number, [])):
                mapping = page_maps.get(table_index, {})
                if not set(rule["fields"]).issubset(mapping):
                    continue
                for row_index, matrix_row in enumerate(table["matrix"]):
                    if not any(_normal(cell) == _normal(label) for cell in matrix_row if cell is not None):
                        continue
                    values = {}
                    for field_key in rule["fields"]:
                        column = mapping[field_key]
                        raw_text = str(matrix_row[column] or "").strip() if column < len(matrix_row) else ""
                        value_kind = normalize_numeric(raw_text, percent=field_key.endswith("ratio"))["kind"]
                        if value_kind not in {"NUMBER", "DASH"}:
                            break
                        values[field_key] = raw_text
                    if len(values) != len(rule["fields"]):
                        ambiguous_source_rows += 1
                        continue
                    row_quote = " ".join(str(cell).strip() for cell in matrix_row if cell is not None and str(cell).strip())
                    candidates.append({
                        "page": page_number,
                        "table_index": table_index,
                        "row_index": row_index,
                        "quote": row_quote,
                        "values": values,
                        "table": table,
                        "mapping": mapping,
                    })
        markdown_row_present = _markdown_has_required_row(section_markdown, label, len(rule["fields"]))
        if not candidates and not ambiguous_source_rows:
            if markdown_row_present:
                warnings.append(f"章节内容包含“{label}”行，但 PyMuPDF 未能确认其表格列，需复核。")
            continue
        if len(candidates) != 1 or ambiguous_source_rows or market_scope not in {"domestic", "hk_connect", "other"}:
            warnings.append(f"源表包含“{label}”行，但其候选行或市场范围无法唯一确认。")
            continue

        source = candidates[0]
        fields = {}
        for field_key, raw_text in source["values"].items():
            column = source["mapping"][field_key]
            header = " ".join(
                str(header_row[column] or "")
                for header_row in source["table"]["matrix"][:3]
                if column < len(header_row)
            )
            unit = "%" if field_key.endswith("ratio") else ""
            if field_key.endswith("ratio") and not any(symbol in header for symbol in ("%", "％", "比例")):
                unit = ""
            elif "亿元" in header:
                unit = "亿元"
            elif "万元" in header:
                unit = "万元"
            elif "元" in header:
                unit = "元"
            unit = source["table"].get("column_units", {}).get(rule["rule_id"], {}).get(field_key, unit)
            fields[field_key] = {
                "raw_text": raw_text,
                "unit": unit,
                "page": source["page"],
                "quote": source["quote"],
            }
        recovered.append({
            "row_key": "total",
            "row_label": label,
            "industry_name": label,
            "market_scope": market_scope,
            "row_page": source["page"],
            "row_quote": source["quote"],
            "fields": fields,
            "source_recovered": True,
        })
    return recovered, warnings


def _markdown_has_required_row(section_markdown: str, label: str, metric_count: int) -> bool:
    """Detect a standalone configured row in Markdown so parser misses cannot pass silently."""
    for line in section_markdown.splitlines():
        cleaned = re.sub(r"</?[^>]+>", "", line).strip()
        if not cleaned:
            continue
        cells = [cell.strip() for cell in cleaned.strip("|").split("|")]
        if any(_normal(cell) == _normal(label) for cell in cells):
            numeric_cells = sum(
                normalize_numeric(cell)["kind"] in {"NUMBER", "DASH"}
                for cell in cells if _normal(cell) != _normal(label)
            )
            if numeric_cells >= metric_count:
                return True
            continue
        if not re.search(rf"(?:^|\s){re.escape(label)}(?=\s|$)", cleaned):
            continue
        tokens = re.findall(r"(?<![\w])(?:[-—–－]|\d[\d,，]*(?:\.\d+)?%?)(?![\w])", cleaned)
        if sum(normalize_numeric(token)["kind"] in {"NUMBER", "DASH"} for token in tokens) >= metric_count:
            return True
    return False


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
                column_index = resolve_data_columns(table, mapping, row_index, rule_id).get(field_key)
                if column_index is None:
                    continue
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
                    table.setdefault("column_units", {})[rule_id] = {
                        key: column_unit(key, " ".join(str(row[column] or "") for row in table["matrix"][:3] if column < len(row)))
                        for key, column in mapping.items()
                    }

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
                centers = _header_centers(previous_record, mapping)
                if _tables_have_compatible_columns(previous_record, current_record, column_tolerance):
                    maps[rule_id].setdefault(page_number, {})[current_index] = mapping.copy()
                    current_record.setdefault("column_units", {})[rule_id] = previous_record.get("column_units", {}).get(rule_id, {}).copy()
                elif _centers_map_uniquely(current_record, centers):
                    # A PDF table may split its merged header into more grid
                    # cells than the headerless continuation. Preserve the
                    # confirmed semantic x-centers and resolve each continued
                    # body row against its own cell geometry.
                    maps[rule_id].setdefault(page_number, {})[current_index] = mapping.copy()
                    current_record.setdefault("inherited_header_centers", {})[rule_id] = centers
                    current_record.setdefault("column_units", {})[rule_id] = previous_record.get("column_units", {}).get(rule_id, {}).copy()
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


def _referenced_bbox(tables, header_maps, page, rule_id, label, raw_value, field_key, reference):
    if not isinstance(reference, dict):
        return None
    table_index, row_index = reference.get("table"), reference.get("row")
    if type(table_index) is not int or type(row_index) is not int or min(table_index, row_index) < 0:
        return None
    records = tables.get(page, [])
    if table_index >= len(records):
        return None
    table = records[table_index]
    mapping = reference.get("mapping") or header_maps.get(rule_id, {}).get(page, {}).get(table_index)
    if not mapping or row_index >= len(table["matrix"]):
        return None
    row = table["matrix"][row_index]
    if not any(_normal(cell) == _normal(label) for cell in row if cell is not None):
        return None
    cells = table["cells_by_row"][row_index]
    if field_key is not None:
        column = mapping.get(field_key)
        if column is None or column >= len(row) or column >= len(cells) or not _cell_matches(row[column], raw_value):
            return None
        return _rect_array(fitz.Rect(cells[column])) if cells[column] is not None else None
    rects = [fitz.Rect(cell) for cell in cells if cell is not None]
    if not rects:
        return None
    return _rect_array(fitz.Rect(min(rect.x0 for rect in rects), min(rect.y0 for rect in rects),
                                 max(rect.x1 for rect in rects), max(rect.y1 for rect in rects)))




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
        bbox = _referenced_bbox(table_cache, header_maps, page_number, rule_id, row_label, raw_value,
                                field_key, source.get("source_ref"))
        if bbox:
            return {"page": page_number, "quote": quote, "bbox": bbox, "page_width": page_info["width"],
                    "page_height": page_info["height"], "status": "LOCATED", "source_ref": source["source_ref"]}
        if source.get("source_ref") is not None:
            # A stale row index/table index is not itself proof that the cited
            # value is wrong. Recover only when the existing table map identifies
            # one cell for the exact row label, semantic column and value.
            recovered_bbox = _table_bbox(
                table_cache, header_maps, page_number, rule_id, row_label, raw_value, field_key,
            )
            if recovered_bbox:
                return {"page": page_number, "quote": quote, "bbox": recovered_bbox,
                        "page_width": page_info["width"], "page_height": page_info["height"],
                        "status": "LOCATED", "source_ref_recovered": "unique_row_column_value"}
            return {"page": page_number, "quote": quote, "bbox": None, "page_width": page_info["width"],
                    "page_height": page_info["height"], "status": "UNVERIFIED"}
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
    source = {"page": value.get("page"), "quote": value.get("quote"), "raw_text": raw, "source_ref": value.get("source_ref")}
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
            dates.add(date(int(year), int(month), int(day)).isoformat())
        except ValueError:
            continue
    # A range may omit the repeated year in its end date.
    ranges = re.findall(
        r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*起?\s*(?:至|到)\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日",
        unicodedata.normalize("NFKC", text),
    )
    for year, start_month, start_day, month, day in ranges:
        try:
            end = date(int(year), int(month), int(day))
            if end >= date(int(year), int(start_month), int(start_day)):
                dates.add(end.isoformat())
        except ValueError:
            pass
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
    rule_text: str,
    source_pages: set[int],
    section_markdown: str,
    table_cache: dict[int, list[dict[str, Any]]],
    header_maps: dict[str, dict[int, dict[int, dict[str, int]]]],
) -> dict[str, Any]:
    if not isinstance(raw_module, dict) or not raw_module.get("found"):
        return {
            "rule_id": rule["rule_id"], "title": rule["title"], "status": "MISSING_MODULE",
            "scope": {}, "rows": [],
            "schema_normalization": {"identity_fields_unwrapped": 0, "identity_fields_invalid": 0},
            "evidence": [],
        }
    rows = raw_module.get("rows")
    if not isinstance(rows, list):
        return {
            "rule_id": rule["rule_id"], "title": rule["title"], "status": "UNRESOLVED_STRUCTURE",
            "scope": {}, "rows": [],
            "schema_normalization": {"identity_fields_unwrapped": 0, "identity_fields_invalid": 1},
            "evidence": [],
        }
    recovered_rows, completeness_warnings = _recover_required_source_rows(
        raw_module, rule, rule_text, source_pages, section_markdown, table_cache, header_maps,
    )
    rows = [*rows, *recovered_rows]
    scope_raw = raw_module.get("scope") if isinstance(raw_module.get("scope"), dict) else {}
    validated_rows = []
    completeness_warnings.extend(raw_module.get("source_warnings") or [])
    disclosure_warnings: list[str] = []
    verified_disclosures = _verify_disclosures(
        raw_module.get("disclosures"), section_markdown, pages, source_pages,
        diagnostics=disclosure_warnings, document=document,
    )
    completeness_warnings.extend(disclosure_warnings)
    unresolved = bool(completeness_warnings) or (not rows and not verified_disclosures)
    normalized_identity_fields = 0
    invalid_identity_fields = 0
    for index, raw_row in enumerate(rows):
        if not isinstance(raw_row, dict):
            unresolved = True
            invalid_identity_fields += 1
            continue
        identity: dict[str, Any] = {}
        row_warnings: list[str] = []
        for key in MODEL_IDENTITY_FIELDS:
            if key not in raw_row:
                identity[key] = None
                continue
            value, was_wrapped = _model_scalar(raw_row[key])
            if was_wrapped:
                normalized_identity_fields += 1
            if (was_wrapped and value is None) or (raw_row[key] is not None and value is None):
                unresolved = True
                invalid_identity_fields += 1
                row_warnings.append(key)
            identity[key] = value

        row_label = identity["row_label"] or ""
        row_quote_value, row_quote_wrapped = _model_scalar(raw_row.get("row_quote"))
        if row_quote_wrapped or (raw_row.get("row_quote") is not None and row_quote_value is None):
            unresolved = True
            invalid_identity_fields += 1
            row_warnings.append("row_quote")
        row_quote = row_quote_value or ""
        row_page = raw_row.get("row_page")
        fields_in = raw_row.get("fields") if isinstance(raw_row.get("fields"), dict) else {}
        fields: dict[str, Any] = {}
        for field_key in rule["fields"]:
            field_value = fields_in.get(field_key)
            if field_value is None:
                unresolved = True
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
            {"page": row_page, "quote": row_quote, "raw_text": row_label, "source_ref": raw_row.get("source_ref")},
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
        identity_values = [
            identity["industry_code"], identity["stock_code"], identity["rank"],
        ]
        identity_values.extend(
            value
            for value in (identity["industry_name"], identity["stock_name"])
            if value and _normal(value) != _normal(row_label)
        )
        # A located row label alone does not prove that its code/name/rank came
        # from that row. Always validate every supplied identity token against
        # the row quote so a bad key cannot silently become a single-sided row.
        if any(value and _normal(value) not in _normal(row_quote) for value in identity_values):
            unresolved = True
            row_warnings.append("row_quote_identity_mismatch")
        row_key = identity["row_key"] or ""
        if rule["rule_id"] == "investment.asset_allocation":
            allowed_keys = set(re.findall(r"^\|\s*`([^`]+)`\s*\|", Path(rule["file"]).read_text(encoding="utf-8"), re.MULTILINE))
            if row_key and row_key not in allowed_keys:
                row_key = ""
                # The rule explicitly allows source-backed rows outside its
                # known key registry. Keep the verified raw label so the
                # comparer can use its unique label/parent fallback.
                if row_evidence["status"] != "LOCATED":
                    unresolved = True
        validated_rows.append({
            "row_id": row_key or identity["row_id"] or f"unmapped:{index}",
            "row_key": row_key,
            "label": row_label,
            "parent_key": identity["parent_key"] or None,
            "rank": identity["rank"],
            "rank_evidence": row_evidence,
            "row_evidence": row_evidence,
            "market_scope": identity["market_scope"] or scope_raw.get("market_scope") or "unknown",
            "investment_scope": identity["investment_scope"] or scope_raw.get("investment_scope") or "unknown",
            "industry_code": identity["industry_code"] or "",
            "industry_name": identity["industry_name"] or "",
            "stock_code": identity["stock_code"] or "",
            "stock_name": identity["stock_name"] or "",
            "fields": fields,
            "schema_warnings": sorted(set(row_warnings)),
            "source_recovered": bool(raw_row.get("source_recovered")),
        })
    required_fields = tuple(rule["fields"])
    if (not validated_rows and not verified_disclosures) or any(not all(key in row["fields"] for key in required_fields) for row in validated_rows):
        unresolved = True
    scope = {str(key): value for key, value in scope_raw.items() if value is not None}
    return {
        "rule_id": rule["rule_id"],
        "title": rule["title"],
        "status": "UNRESOLVED_STRUCTURE" if unresolved else "EXTRACTED",
        "scope": scope,
        "rows": validated_rows,
        "disclosures": verified_disclosures,
        "completeness_warnings": completeness_warnings,
        "source_coverage": raw_module.get("source_coverage"),
        "schema_normalization": {
            "identity_fields_unwrapped": normalized_identity_fields,
            "identity_fields_invalid": invalid_identity_fields,
        },
        "evidence": [],
    }


def _quote_rects_in_section(pdf_page, quote: str, markdown: str, page_text: str):
    """Locate a repeated quote only within its uniquely identified subsection."""
    chapter = _normal(_chapter_plain_text(markdown))
    needle = _normal(quote)
    if not needle or chapter.count(needle) != 1:
        return []
    quote_start = chapter.index(needle)
    headings = []
    offset = 0
    for line in markdown.splitlines():
        heading = parse_heading(line)
        if heading and offset <= quote_start:
            headings.append(heading)
        offset += len(_normal(_chapter_plain_text(line)))

    body = _normal(page_text)
    page_headings = []
    offset = 0
    for line in page_text.splitlines():
        heading = parse_heading(line)
        if heading:
            page_headings.append((offset, heading))
        offset += len(_normal(line))

    for heading in reversed(headings):
        title = _normal(heading.title)
        if not title or body.count(title) != 1:
            continue
        start = body.index(title) + len(title)
        following = next(((pos, item) for pos, item in page_headings if pos >= start), None)
        end = following[0] if following else len(body)
        if body[start:end].count(needle) != 1:
            continue
        anchors = pdf_page.search_for(heading.title)
        if not anchors:
            continue
        lower = max(rect.y1 for rect in anchors)
        upper = pdf_page.rect.height
        if following:
            boundary = pdf_page.search_for(following[1].title)
            if not boundary or body.count(_normal(following[1].title)) != 1:
                continue
            upper = min(rect.y0 for rect in boundary)
        rects = [rect for rect in pdf_page.search_for(quote)
                 if rect.y0 >= lower - .5 and rect.y1 <= upper + .5]
        if rects:
            return [*anchors, *rects]
    return []


def _verify_disclosures(
    raw: Any,
    section_markdown: str,
    pages: list[dict[str, Any]],
    source_pages: set[int],
    *,
    diagnostics: list[str] | None = None,
    document: Any = None,
) -> list[dict[str, Any]]:
    """Keep a model claim only when its exact quote exists in both chapter and cited page."""
    if not isinstance(raw, list):
        return []
    page_text = {int(page["page"]): str(page.get("text", "")) for page in pages}
    verified = []
    chapter_text = _chapter_plain_text(section_markdown)
    for index, item in enumerate(raw):
        def reject(reason: str) -> None:
            if diagnostics is not None:
                diagnostics.append(f"披露证据第 {index + 1} 条未通过：{reason}")
        if not isinstance(item, dict) or item.get("status") not in {"NO_DATA", "NOT_DISCLOSED", "UNCERTAIN"}:
            reject("披露状态格式无效")
            continue
        quote = str(item.get("quote") or "").strip()
        try:
            page = int(item["page"])
        except (KeyError, TypeError, ValueError):
            reject("物理页码无效")
            continue
        if not quote or page not in source_pages or page not in page_text:
            reject("缺少引用或引用页不属于当前章节")
            continue
        if not _contains(chapter_text, quote):
            reject("引文未在归集章节中找到")
            continue
        if not _contains(page_text[page], quote):
            reject("引文未在所引 PDF 页中找到")
            continue
        scope = item.get("scope") if isinstance(item.get("scope"), dict) else {}
        period_end = str(item.get("period_end") or "").strip()
        evidence = {"page": page, "quote": quote, "bbox": None, "status": "QUOTE_ONLY"}
        if document is not None:
            pdf_page = document[page - 1]
            rects = pdf_page.search_for(quote)
            unique_quote = _normal(page_text[page]).count(_normal(quote)) == 1
            if rects and not unique_quote:
                rects = _quote_rects_in_section(pdf_page, quote, section_markdown, page_text[page])
                unique_quote = bool(rects)
            if rects and unique_quote:
                box = fitz.Rect(rects[0])
                for rect in rects[1:]:
                    box |= rect
                evidence.update(bbox=_rect_array(box), status="LOCATED")
            evidence.update(page_width=pdf_page.rect.width, page_height=pdf_page.rect.height)
        verified.append({
            "status": item["status"],
            "period_end": period_end or None,
            "scope": {str(key): str(value).strip() for key, value in scope.items() if value is not None},
            "page": page,
            "quote": quote,
            "evidence": evidence,
        })
    return verified


def _chapter_plain_text(markdown: str) -> str:
    """Remove serialization marks while retaining all quoted text and numbers."""
    text = re.sub(r"<!--.*?-->", "", markdown, flags=re.S)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"^\s*#{1,6}\s+", "", text, flags=re.M)
    return unescape(text).replace("**", "").replace("`", "")


async def _extract_chapters(
    filename, role, provider, contexts, pages, rule_texts, tables, header_maps,
    candidate_numbers, trace_callback, force_refresh, document_hash, document,
):
    cache = ChapterCache()
    namespace = getattr(provider, "cache_namespace", None)
    use_cache = isinstance(namespace, dict) and os.getenv("PROSPECTUS_CACHE_ENABLED", "1") != "0"
    metadata_pages = {1, 2, 3}
    chapter_pages = [page["physical_page"] for context in contexts.values() for page in context.get("physical_pages", [])]
    if chapter_pages:
        metadata_pages.add(max(1, min(chapter_pages) - 1))
    metadata_text = "\n\n".join(
        f"[PHYSICAL PAGE {page['page']}]\n{page['text']}"
        for page in pages if page["page"] in metadata_pages
    )

    async def extract(rule):
        rule_id = rule["rule_id"]
        context = contexts.get(rule_id, {})
        records = source_catalogue(context, rule, tables, header_maps, document_hash, role)
        gaps = source_gaps(context, records, rule)
        source_mode = bool(records) and not gaps
        scope = {"market_scope": "unknown", "investment_scope": "unknown",
                 "disclosure_scope": "unknown", "limit": None} if rule is not MODULE_RULES[0] else {}
        schema = {"rule_id": rule_id, "found": True, "scope": scope, "rows": []}
        schema["disclosures"] = [{
            "status": "NO_DATA | NOT_DISCLOSED | UNCERTAIN",
            "period_end": "原文期间，优先 YYYY-MM-DD；不能确认则 null",
            "scope": {"market_scope": "all | domestic | hk_connect | other | unknown",
                      "investment_scope": "all | index | active | unknown",
                      "disclosure_scope": "all | top_n | unknown", "limit": None},
            "page": 1,
            "quote": "支持该状态的连续原文",
        }]
        if source_mode:
            schema["rows"] = [{"source_id": "必须来自源表", "row_label": "原文行名", "row_key": None,
                               "parent_key": None, "market_scope": "unknown", "investment_scope": "unknown"}]
            source_text = (
                "【章节 Markdown，包含续页、表注与子章节】\n" + context.get("section_markdown", "")
                + "\n【PDF 源表行】\n" + render_catalogue(records)
            )
            task = (
                "源表行已由程序确认字段列。你只确认业务行身份、父子关系和统计范围。"
                "每个源表 source_id 恰好返回一次，不合并、不遗漏、不补造。"
                "无需生成 fields、金额、比例、页码或引用，程序会从源表取回。"
                "行业代码、股票代码、股票名称和排名也由程序从源表取回。"
                "row_key 按模块规则归类；row_label 须为该行原文标签。"
            )
        else:
            schema.update(_module_retry_schema(rule))
            numbers = {page["physical_page"] for page in context.get("physical_pages", [])} or candidate_numbers
            source_text = _format_chapter_contexts({rule_id: context}) if context else ""
            if not context:
                source_text += "\n".join(f"[PHYSICAL PAGE {page['page']}]\n{page['text']}" for page in pages if page["page"] in numbers)
            task = (
                "本章节未能建立完整源表目录，按示例返回行和 fields。每个单元格包含原文 raw_text、unit、page、quote。"
                "逐字引用必须来自对应物理页且连续；row_quote 覆盖行标签及身份字段，不生成坐标。"
                "industry_code、industry_name、stock_code、stock_name、rank、row_key 等身份字段必须是标量，不能是对象。"
            )
        if rule is MODULE_RULES[0]:
            schema["document_info"] = {key: {"value": "原文值或 null", "page": 1, "quote": "逐字原文"}
                                       for key in ("fund_name", "fund_code", "disclosure_date", "period_end")}
            task += "附带 document_info：基金名、基金代码、披露日期、报告期末；日期用 YYYY-MM-DD。招书的 period_end 来自投资组合报告期，不是封面更新日期。找不到写 null。"
        source_text += "\n【文档元数据原文，仅用于确认报告期间，不作为本章节的披露引文】\n" + metadata_text
        system = (
            "你是基金披露文件章节信息识别器。只处理指定章节，先读完整章节再按规则分类。"
            "章节可跨页，行序和物理页不决定业务身份。横线保持为横线，不变为零。"
            "逐行覆盖所有实际披露内容，包括合计和续页；不推断、不重算、不把目录当正文。"
            "若章节以文字明确说明某范围没有披露数据，也要在 disclosures 返回事实状态、期间、适用范围、原文页码和逐字引用；不得因为 rows 为空而省略。"
            "status 只能是 NO_DATA（原文明确表示该范围无数据）、NOT_DISCLOSED（该范围未披露）、UNCERTAIN（无法判断）；每条引用必须来自本章节。"
            "disclosures.scope 必须使用示例中的固定字段和语义值，不新增中文字段名或用整段标题作为键值。"
            "不按市场拆分的章节范围用 all；不区分指数/积极投资时 investment_scope 用 all；前N名用 top_n 且 limit 为N，其余完整披露用 all。"
            "period_end 根据章节期间或文档元数据原文确定，不用送出日期代替；正文未复述年份可结合明确的报告期间。"
            "规则中的比对条件由后续程序执行，你仅提取当前文件的披露信息。"
            "有表格数据时也必须识别顶层 scope：按模块规则确定投资范围、全量或前N名及limit，并填写每行的investment_scope；不要把整体投资误当成范围未知。"
            "只返回一个 JSON 对象，不输出 Markdown。" + task
        )
        prompt = f"文档角色：{role}；文件：{filename}。\n模块：{rule_id}。\n【模块规则】\n{rule_texts[rule_id]}\n【JSON 结构】\n{json.dumps(schema, ensure_ascii=False)}\n{source_text}"
        if len(system) + len(prompt) > MAX_ANALYSIS_CHARS:
            raise ValueError(f"PDF_ANALYSIS_LIMIT_EXCEEDED: {rule_id}; no content was truncated")
        safe_id = rule_id.replace(".", "_")
        prefix = f"{role}/chapters/{safe_id}"
        _trace_artifact(trace_callback, f"{prefix}/sources.json", {"rows": records, "unmapped_markdown_rows": gaps}, role=role)
        key = cache.key({**namespace, "document_hash": document_hash,
                         "common_rules": hashlib.sha256(COMMON_RULE_FILE.read_bytes()).hexdigest()}, system, prompt) if use_cache else None
        hit = cache.load(key) if key and not force_refresh else None
        request = {"provider": type(provider).__name__, "model": getattr(provider, "model_name", None),
                   "system_prompt": system, "user_prompt": prompt, "source_mode": source_mode, "cache_key": key}
        _trace_artifact(trace_callback, f"{prefix}/model_request.json", request, role=role)
        last_module, last_info = None, {}
        for attempt in range(2):
            started = time.monotonic()
            _trace_artifact(trace_callback, f"{prefix}/attempt_{attempt + 1}/model_request.json",
                            {**request, "user_prompt": prompt}, role=role)
            _trace_event(trace_callback, "chapter_request_started", role=role, details={"rule_id": rule_id, "attempt": attempt + 1,
                         "prompt_chars": len(system) + len(prompt), "source_rows": len(records), "cache_hit": bool(hit)})
            try:
                response = hit["response"] if hit else await provider.generate_text(system, prompt)
                _trace_artifact(trace_callback, f"{prefix}/attempt_{attempt + 1}/model_response.txt", str(response), role=role)
                _trace_event(trace_callback, "chapter_response_received", role=role, details={"rule_id": rule_id,
                             "attempt": attempt + 1, "duration_ms": round((time.monotonic() - started) * 1000),
                             "response_chars": len(response), "cache_hit": bool(hit), **getattr(response, "metrics", {})})
                parsed = _json_object(response)
                module = parsed.get("module", parsed)
                if isinstance(parsed.get("modules"), list):
                    module = next((item for item in parsed["modules"] if item.get("rule_id") == rule_id), {})
                if not isinstance(module, dict) or not isinstance(module.get("found"), bool) or not isinstance(module.get("rows"), list):
                    raise ValueError("AI_INVALID_RESPONSE: expected found boolean and rows list")
                last_info = parsed.get("document_info", {})
                if source_mode:
                    module = bind_sources(module, records, rule)
                    last_module = module
                    if module["source_warnings"]:
                        raise ValueError("AI_SOURCE_COVERAGE: " + " ".join(module["source_warnings"]))
                else:
                    module["rule_id"] = rule_id
                    if gaps:
                        module["source_warnings"] = [f"章节有 {len(gaps)} 行未建立完整源表映射，需复核。"]
                    if context and not module["found"]:
                        module["found"] = True
                        module["source_warnings"] = [*module.get("source_warnings", []), "章节原文已定位，但模型未能提取内容。"]
                checked = _verify_module(document, pages, module, rule, rule_texts[rule_id],
                                         {p["physical_page"] for p in context.get("physical_pages", [])},
                                         context.get("section_markdown", ""), tables, header_maps)
                if source_mode and checked["status"] != "EXTRACTED":
                    raise ValueError("AI_EVIDENCE: 行身份、父子键或源单元格证据未通过校验，请按原文重返全部行")
                if not source_mode and module.get("disclosures") and checked.get("completeness_warnings"):
                    raise ValueError("AI_EVIDENCE: " + " ".join(checked["completeness_warnings"]))
                info, _ = _verify_metadata(document, pages, last_info)
                if key and not hit and checked["status"] == "EXTRACTED" and (rule is not MODULE_RULES[0] or info.get("period_end")):
                    try:
                        cache.save(key, str(response))
                    except OSError as cache_error:
                        _trace_event(trace_callback, "chapter_cache_write_failed", role=role,
                                     details={"rule_id": rule_id, "error": str(cache_error)})
                return module, last_info
            except Exception as exc:
                hit = None
                _trace_event(trace_callback, "chapter_attempt_failed", role=role, details={"rule_id": rule_id, "attempt": attempt + 1,
                             "error": f"{type(exc).__name__}: {exc}"})
                if attempt == 1:
                    partial = last_module or {"rule_id": rule_id, "found": True, "rows": []}
                    partial["source_warnings"] = [*partial.get("source_warnings", []), f"章节识别未完成：{exc}"]
                    return partial, last_info
                # Only this chapter is retried; successful siblings remain intact.
                prompt += "\n上次输出未通过校验，请完整重新返回本章节：" + str(exc)
        raise AssertionError("unreachable")

    tasks = [asyncio.create_task(extract(rule)) for rule in MODULE_RULES]
    try:
        outcomes = await asyncio.gather(*tasks)
    except BaseException:
        # Finish cancellation before analyze_document closes the shared PDF.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    return {"document_info": outcomes[0][1], "modules": [outcome[0] for outcome in outcomes]}


async def analyze_document(
    *, filename: str, content: bytes, role: str, provider: Any,
    trace_callback: Callable[[dict[str, Any]], None] | None = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Verify continuous PDF Markdown before chapter/model extraction."""
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
        conversion_started = time.monotonic()
        _trace_event(trace_callback, "pdf_markdown_started", role=role, details={"filename": Path(filename).name})
        converted = await run_pdf(verified_conversion, content)
        _trace_artifact(trace_callback, f"{role}/pdf_markdown/document.md", converted["markdown"], role=role)
        _trace_artifact(trace_callback, f"{role}/pdf_markdown/reader.md", converted["reader_markdown"], role=role)
        _trace_artifact(trace_callback, f"{role}/pdf_markdown/verification.json", {
            "verification": converted["verification"], "warnings": converted["warnings"],
            "archive_path": converted["archive_path"], "cache_hit": converted["cache_hit"], "version": converted["version"]}, role=role)
        require_ready(converted)
        markdown = converted["reader_markdown"]
        _trace_event(trace_callback, "pdf_markdown_completed", role=role, details={
            "markdown_chars": len(markdown),
            "archive_path": converted["archive_path"], "cache_hit": converted["cache_hit"],
            "duration_ms": round((time.monotonic() - conversion_started) * 1000),
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
        for context in chapter_contexts.values():
            candidate_numbers.update(page["physical_page"] for page in context.get("physical_pages", []))
        for section in markdown_sections.values():
            candidate_numbers.update(_physical_pages_in_markdown(section, pages))
        tables_started = time.monotonic()
        native_tables = page_tables(converted)
        table_cache = {p: native_tables.get(p, []) for p in candidate_numbers}
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
        raw_result = await _extract_chapters(
            filename, role, provider, chapter_contexts, pages, rule_texts,
            table_cache, header_maps, candidate_numbers, trace_callback, force_refresh,
            hashlib.sha256(content).hexdigest(), document,
        )
        _trace_artifact(trace_callback, f"{role}/model_candidate.json", raw_result, role=role)
        by_id = {item["rule_id"]: item for item in raw_result["modules"]}
        verification_started = time.monotonic()
        document_info, metadata_evidence = _verify_metadata(document, pages, raw_result.get("document_info"))
        modules = []
        for rule in MODULE_RULES:
            module_context = chapter_contexts.get(rule["rule_id"], {})
            source_pages = {
                int(source_page["physical_page"])
                for source_page in module_context.get("physical_pages", [])
                if source_page.get("physical_page") is not None
            }
            modules.append(_verify_module(
                document, pages, by_id.get(rule["rule_id"]), rule,
                rule_texts[rule["rule_id"]], source_pages,
                module_context.get("section_markdown", ""), table_cache, header_maps,
            ))
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
            "identity_fields_unwrapped": sum(module.get("schema_normalization", {}).get("identity_fields_unwrapped", 0) for module in modules),
            "identity_fields_invalid": sum(module.get("schema_normalization", {}).get("identity_fields_invalid", 0) for module in modules),
            "duration_ms": round((time.monotonic() - verification_started) * 1000),
            "located_fields": sum(
                1
                for module in modules
                for row in module.get("rows", [])
                for field in row.get("fields", {}).values()
                if field.get("evidence", {}).get("status") == "LOCATED"
            ),
            "source_recovered_rows": sum(
                1
                for module in modules
                for row in module.get("rows", [])
                if row.get("source_recovered")
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
