from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Callable
from datetime import date

import pymupdf as fitz
import anydoc

from app.pdf_worker import run_pdf
from app.source_evidence import evidence_source
from app.summary_rules import load_markdown_rules, module_groups


RULE_CONFIG, RULES = load_markdown_rules()
RULE_VERSION = str(RULE_CONFIG["version"])
# Compatibility fields are output/provenance only. New prompts and workflow
# joins use stable item_id; Excel row numbers never identify execution items.
for _rule in RULES:
    _rule.setdefault("key", _rule["item_id"])
    _rule.setdefault("terms", _rule.get("search_terms", []))


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
    if re.match(r"^第[一二三四五六七八九十百]+[章节部分]\s*\S", text):
        return text.split()[0], 1
    if re.match(r"^[一二三四五六七八九十]+、\s*\S", text):
        return text.split()[0], 1
    # Arabic section markers must include punctuation (1. / 1、 / 5.1).
    # Without it, body text such as "2季度，全球经济……" becomes a fake heading.
    match = re.match(r"^(\d+(?:\.\d+)+(?:[、.．])?|\d+[、.．])\s*(?=[\u3400-\u9fffA-Za-z《【（(])", text)
    if match:
        prefix = match.group(1)
        if re.fullmatch(r"\d{4}", prefix):
            return None
        return prefix, prefix.count(".") + 1
    return None


def _source_heading_info(source: dict[str, Any]) -> tuple[str, int] | None:
    text = source.get("raw_text", "")
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
    if source[1] > target[0]:
        return "DATE_UNCONFIRMED", "补充文件版本日期与概要编制日期范围重叠，无法确认先后顺序。"
    return "ELIGIBLE", None


def _fallback_trigger_reason(
    rule: dict[str, Any], fallback_when: list[str], *, lookup_override: str | None,
    primary_not_applicable: bool, primary_conclusion_verified: bool,
) -> str | None:
    """Resolve the rule-declared fallback policy without indicator-name branches."""
    conditions = set(fallback_when)
    if lookup_override == "CONFIRMED_ABSENT" and "primary_confirmed_absent" in conditions:
        return "PRIMARY_CONFIRMED_ABSENT"
    if primary_not_applicable and "primary_not_applicable" in conditions:
        return "PRIMARY_NOT_APPLICABLE"
    if ("same_contract_annual_rate" in conditions
            and rule.get("value_type") == "annual_rate"
            and not primary_conclusion_verified):
        return "PRIMARY_ANNUAL_RATE_NOT_VERIFIED"
    if ("same_period_same_nature_amount" in conditions
            and rule.get("period_policy") == "same_period"
            and lookup_override == "CONFIRMED_ABSENT"
            and not primary_conclusion_verified):
        return "PRIMARY_PERIOD_AMOUNT_CONFIRMED_ABSENT"
    return None


def _model_candidate(source: dict[str, Any]) -> dict[str, Any]:
    candidate = {key: source[key] for key in ("source_id", "page", "bbox", "section_path", "raw_text")}
    if len(source.get("cell_source_ids", [])) > 1:
        candidate["cell_text"] = source["cell_text"]
        candidate["cell_source_ids"] = source["cell_source_ids"]
    if len(source.get("row_source_ids", [])) > 1:
        candidate["row_text"] = source["row_text"]
        candidate["row_source_ids"] = source["row_source_ids"]
    if len(source.get("layout_context", [])) > 1:
        candidate["layout_context"] = source["layout_context"]
    return candidate


def build_document_source_catalogue(filename: str, content: bytes, role: str) -> dict[str, Any]:
    """Combine AnyDoc reading structure with PyMuPDF-addressable evidence."""
    catalogue = build_source_catalogue(filename, content, role)
    try:
        markdown = anydoc.to_markdown_bytes(content, "pdf")
        if isinstance(markdown, bytes):
            markdown = markdown.decode("utf-8", errors="replace")
        catalogue["reader_context"] = {"provider": "AnyDoc", "status": "READY", "markdown": str(markdown)}
    except Exception as exc:
        catalogue["reader_context"] = {
            "provider": "AnyDoc", "status": "FAILED", "error": f"{type(exc).__name__}: {exc}"
        }
    return catalogue


def _markdown_table_context(catalogue: dict[str, Any], item: dict[str, Any]) -> list[str]:
    reader_context = catalogue.get("reader_context") or {}
    if reader_context.get("status") != "READY":
        return []
    terms = [_norm(term) for term in item.get("terms", []) if _norm(term)]
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in str(reader_context.get("markdown") or "").splitlines():
        if "|" in line:
            current.append(line)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return ["\n".join(block) for block in blocks
            if any(term in _norm("\n".join(block)) for term in terms)]


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


def _chapter_directory(catalogue: dict[str, Any]) -> list[dict[str, Any]]:
    """Build a compact semantic routing index without sending the whole report."""
    sources_by_id = {
        source["source_id"]: source
        for page in catalogue.get("pages", [])
        for source in page.get("sources", [])
    }
    directory = []
    for section in catalogue.get("chapters", []):
        source_ids = [source_id for source_id in section.get("source_ids", []) if source_id in sources_by_id]
        sources = [sources_by_id[source_id] for source_id in source_ids]
        if not sources:
            continue
        if len(sources) <= 10:
            preview_sources = sources
        else:
            sample_indexes = sorted({round(index * (len(sources) - 1) / 9) for index in range(10)})
            preview_sources = [sources[index] for index in sample_indexes]
        preview = [source.get("raw_text", "")[:200] for source in preview_sources if source.get("raw_text")]
        directory.append({
            "section_id": section.get("section_id"),
            "path": section.get("path"),
            "title": section.get("title"),
            "pages": section.get("pages", []),
            "source_count": len(sources),
            "preview": preview,
        })
    return directory


def _source_candidates(catalogue: dict[str, Any], item: dict[str, Any]) -> list[dict[str, Any]]:
    terms = [_norm(term) for term in item.get("terms", []) if _norm(term)]
    section_hints = [_norm(term) for term in item.get("section_hints", [])]
    def search_norm(value: str) -> str:
        return re.sub(r"[\s|｜,，;；:：]+", "", value).casefold()

    def matches(source_text: str, term: str) -> bool:
        return search_norm(term) in search_norm(source_text)
    # Gather complete sections across physical pages. If no structural heading
    # can be established, keep the whole matching page rather than clipping a
    # fixed number of lines that may split a table or paragraph.
    result: list[dict[str, Any]] = []
    for page in catalogue["pages"]:
        lines = page["sources"]
        anchors = [source for source in lines
                   if any(matches(text, term)
                          for text in (source.get("raw_text", ""), source.get("row_text", ""),
                                       source.get("cell_text", ""), " ".join(source.get("section_path", [])))
                          for term in terms + section_hints)]
        if not anchors:
            continue
        for anchor in anchors:
            result.extend(_complete_section_sources(catalogue, anchor))
    unique = {source["source_id"]: source for source in result}
    return list(unique.values())


def _parse_json(text: str) -> dict[str, Any]:
    clean = text.strip()
    clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", clean, flags=re.I)
    start, end = clean.find("{"), clean.rfind("}")
    parsed = json.loads(clean[start:end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("AI_INVALID_JSON: summary result must be an object")
    return parsed


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
    terms = [_norm(term) for term in item.get("terms", []) if _norm(term) and _norm(term) not in {"7.4.7.18", "其他费用"}]
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
    if trace:
        trace({"kind": "artifact", "path": "rules/product_summary_markdown.json", "content": RULE_CONFIG})
        if source_catalogues is None:
            for role, catalogue in catalogues.items():
                trace({"kind": "artifact", "document_role": role, "path": f"{role}/source_catalogue.json", "content": catalogue})
    if progress and source_catalogues is None:
        progress("source_index", "completed", "章节证据目录已建立")

    items = []
    warnings = []
    for item in RULES:
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
        elif role in {"periodic_report", "annual_report"} and historical and report_interval[1] > summary_interval[0]:
            primary_source_eligibility = "DATE_UNCONFIRMED"
            eligibility_reason = "定期报告日期与概要编制日期范围重叠，无法确认先后顺序。"
            primary_reference = []
            warnings.append(f"{role} 与概要编制日期先后无法确认，未作为历史口径依据。")
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
            "business_rule_markdown": item.get("meaning"),
            "value_type": item.get("value_type"),
            "period_policy": item.get("period_policy"),
            "unit_policy": item.get("unit_policy"),
            "comparison_policy": item.get("comparison_policy"),
            "amount_period": item.get("amount_period"),
            "primary_role": role,
            "fallback_role": fallback_role,
            "fallback_when": item.get("fallback_when", []),
            "primary_source_eligibility": primary_source_eligibility,
            "primary_source_eligibility_reason": eligibility_reason,
            "primary_lookup_status": primary_source_eligibility if primary_source_eligibility != "ELIGIBLE" else "CANDIDATES_FOUND" if primary_reference else "NO_CANDIDATES_UNCONFIRMED",
            "primary_not_applicable": primary_not_applicable,
            "summary_table_context": _markdown_table_context(catalogues["product_summary"], item) if "product_summary" in catalogues else [],
            "primary_table_context": _markdown_table_context(catalogues[role], item) if role in catalogues else [],
            "summary_candidates": [_model_candidate(source) for source in primary],
            "primary_candidates": [_model_candidate(source) for source in primary_reference],
            "fallback_candidates": [_model_candidate(source) for source in fallback_candidates],
        }
        items.append(item_payload)
    if progress:
        progress("primary_lookup", "running", "初步定位完成，正在核实候选未命中的指标")

    system = (
        "你是基金产品资料核对助手。只根据给定原文证据判断字段含义和一致性。"
        "必须逐项返回所有 item_id，不能使用Excel行号识别指标。先只检查主要定期报告证据。只有相关完整章节已检查且明确确认字段未披露时才返回 SOURCE_MISSING；候选未命中、证据不足、输出漏项或引用失败都必须返回 UNRESOLVED。主要来源存在有效证据时必须以其比较，即使结果不一致也不能改用补充文件。只能引用输入中存在的 source_id，不得编造文字、来源或坐标。"
        "value只能填写被引用原文直接支持的字段值或必要的规范化值，不能把推导的日期、期间或解释拼进value；解释放在reason。每个数值、日期和代码都必须能在对应引用原文中完整找到。"
        "相同数字若业务身份不同不能认定一致；费率、金额、代码、日期需保持字段角色。"
        "每项都提供业务含义、值类型、期间、单位和比较策略，必须按该规则核对。current_identity表示核对当前产品身份；applicable_at_summary_compilation要求依据在概要编制时已适用；same_period要求两侧期间相同。annual_rate只比较年费率，不得拿费率与实际费用金额比较；same_currency_amount_and_period要求币种、期间和费用性质都一致，任何一项不明确即UNRESOLVED。"
        "单位策略要求保留原单位或按规则归一，不得因为数字碰巧相同就忽略元/万元或比例/金额差别。"
        "只有字段在概要和至少一个指定来源中都能找到时才比较。字段在一侧或两侧均未披露时，对该来源返回 SOURCE_MISSING，不得据此判差异；证据存在但无法确认或引用时返回 UNRESOLVED。primary_not_applicable为true时，禁止比较该定期报告，只提取概要侧目标事实并将主来源标为SOURCE_MISSING/NOT_APPLICABLE。"
        "候选包含 PyMuPDF 原文坐标、row_text、cell_text，以及 AnyDoc 提供的 Markdown 表格结构线索。AnyDoc 仅供理解表格行列关系，引用仍必须来自 PyMuPDF source_id；不得引用 Markdown 文本作为来源 ID。必须结合行列关系绑定字段和值，禁止把同一行其他字段的值错配。"
        "layout_context 是同页近邻原文的结构提示，不能单独作为证据。字段标签和值分开时，必须同时引用包含标签的 source_id 与对应值的 source_id；不得只引用近邻提示。代码字段按规则匹配实际基金份额/交易代码，不把一组主代码和前后端代码拼成单一值。"
        "文本可做语义等价判断，但保留否定、比例、限制条件和适用对象。"
        "严格按 JSON 返回，不要输出 Markdown。"
    )
    schema = {
        "items": [{"item_id": "stable-rule-id", "summary_value": "原文值或摘要", "summary_source_ids": [], "references": [{"role": "primary role", "status": "MATCH|MISMATCH|UNRESOLVED|SOURCE_MISSING", "value": "原文值或摘要", "source_ids": [], "reason": "简短依据"}], "reason": "简短依据"}]
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
        if not group_items:
            if progress:
                progress(group_id, "completed", "没有双方共同定位的核对项，已跳过")
            return group_id, [], None
        if progress:
            progress(group_id, "running", "正在核对" + group_id.replace("_", " "))
        primary_only_items = [
            {key: value for key, value in {**item, "fallback_candidates": []}.items()
             if key not in {"excel_row", "excel_source_cell"}}
            for item in group_items
        ]
        prompt = (
            f"按当前主题逐项处理本组 {len(group_items)} 个核对项，必须完整返回每个item_id。候选为主要定期报告的相关章节原文，可跨页引用多个 source_id。某项primary_candidates为空时，仍须单独提取并验证概要侧事实；主要报告侧返回UNRESOLVED，不能因此省略整个指标。不得使用补充来源候选。"
            "只比较本组中的字段，不扩展到其他字段。\nJSON结构：\n"
            + json.dumps(schema, ensure_ascii=False)
            + "\n字段与证据包：\n"
            + json.dumps(primary_only_items, ensure_ascii=False)
        )
        if trace:
            trace({"kind": "artifact", "path": f"comparison/{group_id}/model_request.json", "content": {"rules_version": RULE_VERSION, "prompt": prompt}})
        parse_error = None
        for attempt in range(2):
            attempt_prompt = prompt if attempt == 0 else prompt + "\n上一次响应不是完整有效 JSON。请重新完整返回本组所有核对项，不要输出 Markdown。"
            try:
                response = await provider.generate_text(system, attempt_prompt)
                raw = str(response)
                if trace:
                    trace({"kind": "artifact", "path": f"comparison/{group_id}/attempt_{attempt + 1}/model_response.txt", "content": raw})
                parsed_group = _parse_json(raw)
                group_results = parsed_group.get("items") if isinstance(parsed_group.get("items"), list) else []
                group_results = [normalized for value in group_results if isinstance(value, dict)
                                 if (normalized := _normalize_model_item_id(value, row_to_item_id)) is not None
                                 and normalized.get("item_id") in known_item_ids]
                if progress:
                    progress(group_id, "completed", group_id.replace("_", " ") + "核对完成")
                return group_id, group_results, None
            except Exception as exc:
                parse_error = f"{type(exc).__name__}: {exc}"
                if trace:
                    trace({"kind": "event", "event": "summary_compare_group_retry", "details": {"group": group_id, "attempt": attempt + 1, "error": parse_error}})
        if progress:
            progress(group_id, "failed", "该主题未能完成识别")
        return group_id, [], parse_error

    group_outcomes = await asyncio.gather(*(analyze_group(group_id, group_items) for group_id, group_items in groups.items()))
    raw_model_items = [item for _, group_items, _ in group_outcomes for item in group_items]
    failed_groups = [{"group": group_id, "error": error} for group_id, _, error in group_outcomes if error]
    valid_sources = {source["source_id"]: source for catalogue in catalogues.values() for page in catalogue["pages"] for source in page["sources"]}
    rule_by_id = {rule["item_id"]: rule for rule in RULES}
    item_by_id = {item["item_id"]: item for item in items}

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
            recovery_items.append({
                key: item[key] for key in ("item_id", "key", "label", "primary_role", "summary_candidates", "primary_candidates", "business_rule_markdown")
            } | {
                "failed_sides": [side for side, valid in (("target", summary_valid), ("primary_reference", primary_valid)) if not valid],
                "verified_target": {"value": summary_value, "source_ids": summary_ids} if summary_valid else None,
                "verified_primary_reference": {"value": primary_value, "source_ids": primary_ids,
                                                "status": primary_ref.get("status")} if primary_valid else None,
            })
    if recovery_items:
        recovery_prompt = (
            "只恢复failed_sides列出的失败侧字段证据。verified_target或verified_primary_reference存在时视为锁定事实，必须原样保留其中值和source_id，不能重新提取或改写。候选包含同页bbox坐标、row_text和cell_text结构提示；标签和值可能分处不同单元格或不同行，必须引用标签与对应值的原始source_id，不能选择相邻的其他指标。"
            "恢复后有有效事实即可返回；事实无法确认则保持该侧空值，并将比较状态设为UNRESOLVED。禁止返回SOURCE_MISSING或确认未披露。不得查看补充招募说明书。"
            "严格返回JSON：{\"items\":[{\"item_id\":\"stable-rule-id\",\"summary_value\":\"\",\"summary_source_ids\":[],\"primary_value\":\"\",\"primary_source_ids\":[],\"comparison_status\":\"MATCH|MISMATCH|UNRESOLVED\",\"reason\":\"\"}]}\n"
            + json.dumps({"items": recovery_items}, ensure_ascii=False)
        )
        if trace:
            trace({"kind": "artifact", "path": "comparison/evidence_recovery/model_request.json", "content": {"prompt": recovery_prompt, "item_count": len(recovery_items)}})
        try:
            recovery_raw = str(await provider.generate_text(system, recovery_prompt))
            if trace:
                trace({"kind": "artifact", "path": "comparison/evidence_recovery/model_response.txt", "content": recovery_raw})
            recovered = _parse_json(recovery_raw).get("items", [])
            recovered_by_id = {normalized["item_id"]: normalized for value in recovered if isinstance(value, dict)
                               if (normalized := _normalize_model_item_id(value, row_to_item_id)) is not None
                               and normalized.get("item_id") in known_item_ids}
            recovery_input_by_id = {item["item_id"]: item for item in recovery_items}
            for item_id in {item["item_id"] for item in recovery_items}:
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
                summary_allowed = {source["source_id"] for source in item["summary_candidates"]}
                summary_evidence = [valid_sources[source_id] for source_id in summary_ids if source_id in valid_sources and source_id in summary_allowed and valid_sources[source_id]["document_role"] == "product_summary"]
                summary_value = str((locked_target or {}).get("value") or found.get("summary_value") or "")
                primary_ids = (locked_primary.get("source_ids") if locked_primary else
                               found.get("primary_source_ids") if isinstance(found.get("primary_source_ids"), list) else [])
                primary_allowed = {source["source_id"] for source in item["primary_candidates"]}
                primary_evidence = [valid_sources[source_id] for source_id in primary_ids if source_id in valid_sources and source_id in primary_allowed and valid_sources[source_id]["document_role"] == item["primary_role"]]
                primary_value = str((locked_primary or {}).get("value") or found.get("primary_value") or "")
                if (len(summary_evidence) != len(set(summary_ids)) or not summary_evidence
                        or not _numbers_are_grounded(summary_value, summary_evidence)
                        or not _field_evidence_is_substantive(rule, summary_evidence)
                        or len(primary_evidence) != len(set(primary_ids)) or not primary_evidence
                        or not _numbers_are_grounded(primary_value, primary_evidence)
                        or not _field_evidence_is_substantive(rule, primary_evidence)):
                    continue
                status = found.get("comparison_status")
                status = status if status in {"MATCH", "MISMATCH", "UNRESOLVED"} else "UNRESOLVED"
                # Exact agreement between independently validated facts is a safe
                # deterministic lower bound; do not preserve a contradictory model label.
                if _norm(summary_value) and _norm(summary_value) == _norm(primary_value):
                    status = "MATCH"
                raw_model_items = [value for value in raw_model_items if value.get("item_id") != item_id]
                raw_model_items.append({
                    "item_id": item_id, "summary_value": summary_value, "summary_source_ids": summary_ids,
                    "references": [{"role": item["primary_role"], "status": status, "value": primary_value,
                                    "source_ids": primary_ids, "reason": str(found.get("reason") or "经局部证据恢复确认。" )}],
                    "reason": str(found.get("reason") or "经局部证据恢复确认。"),
                })
        except Exception as exc:
            failed_groups.append({"group": "evidence_recovery", "error": f"{type(exc).__name__}: {exc}"})
            if trace:
                trace({"kind": "event", "event": "summary_evidence_recovery_failed", "details": {"error": f"{type(exc).__name__}: {exc}"}})
    # Lexical misses are not absence. Route only unresolved indicators to complete
    # logical sections, then inspect those sections with explicit coverage records.
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
        valid_fact = (ref.get("status") in {"MATCH", "MISMATCH"}
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
        report_catalogue = catalogues.get("periodic_report")
        section_directory = _chapter_directory(report_catalogue) if report_catalogue else []
        route_prompt = (
            "你负责为未定位的核对项选择定期报告中的完整章节。根据指标含义、概要证据和章节标题/摘要，选择可能披露该指标的一个或多个 section_id；可以多选，最多8个。"
            "不能因目录摘要未出现指标词就判断未披露，也不能输出章节目录外的ID。此步骤只做章节路由，不提取值、不比较、不确认缺失。没有把握时返回空数组。严格返回JSON："
            "{\"items\":[{\"item_id\":\"stable-rule-id\",\"section_ids\":[],\"reason\":\"\"}]}\n"
            + json.dumps({
                "items": [{"item_id": item["item_id"], "label": item["label"],
                           "business_rule": item["business_rule_markdown"],
                           "target_candidates": [_model_candidate(source) for source in item["summary_candidates"][:24]],
                           "initial_reference_candidates": [_model_candidate(source) for source in item["primary_candidates"][:24]]}
                          for item in missing_primary_items],
                "sections": section_directory,
            }, ensure_ascii=False)
        )
        if trace:
            trace({"kind": "artifact", "path": "comparison/primary_section_routing/model_request.json",
                   "content": {"prompt": route_prompt, "section_count": len(section_directory),
                               "item_ids": [item["item_id"] for item in missing_primary_items]}})
        try:
            routing_raw = str(await provider.generate_text(system, route_prompt))
            if trace:
                trace({"kind": "artifact", "path": "comparison/primary_section_routing/model_response.txt", "content": routing_raw})
            routing_result = _parse_json(routing_raw)
            section_by_id = {section["section_id"]: section for section in section_directory if section.get("section_id")}
            report_sources_by_id = {source["source_id"]: source
                                    for page in (report_catalogue or {}).get("pages", [])
                                    for source in page.get("sources", [])}
            routed: dict[str, list[str]] = {}
            route_reasons: dict[str, str] = {}
            for route in routing_result.get("items", []):
                if not isinstance(route, dict):
                    continue
                normalized = _normalize_model_item_id(route, row_to_item_id)
                if not normalized or normalized.get("item_id") not in {item["item_id"] for item in missing_primary_items}:
                    continue
                selected_ids = route.get("section_ids") if isinstance(route.get("section_ids"), list) else []
                selected_ids = [section_id for section_id in selected_ids if section_id in section_by_id][:8]
                routed[normalized["item_id"]] = list(dict.fromkeys(selected_ids))
                route_reasons[normalized["item_id"]] = str(route.get("reason") or "")

            lookup_pack = []
            route_coverage = []
            for item in missing_primary_items:
                item_id = item["item_id"]
                selected_ids = routed.get(item_id, [])
                existing_section_ids = {source.get("section_id") for source in item["primary_candidates"]}
                selected_ids = list(dict.fromkeys([*selected_ids, *[sid for sid in existing_section_ids if sid in section_by_id]]))
                selected_sources = [report_sources_by_id[source_id]
                                    for section_id in selected_ids
                                    for source_id in section_by_id[section_id].get("source_ids", [])
                                    if source_id in report_sources_by_id]
                candidate_sources = {source["source_id"]: source for source in [*item["primary_candidates"], *selected_sources]}
                item["primary_candidates"] = list(candidate_sources.values())
                pages = sorted({source["page"] for source in item["primary_candidates"]})
                chars = sum(len(source.get("raw_text", "")) for source in item["primary_candidates"])
                coverage_complete = bool(selected_ids) and chars <= 180_000
                item["primary_routing"] = {
                    "selected_section_ids": selected_ids,
                    "selected_sections": [section_by_id[sid]["path"] for sid in selected_ids],
                    "route_reason": route_reasons.get(item_id),
                    "source_count": len(item["primary_candidates"]),
                    "pages": pages,
                    "coverage_complete": coverage_complete,
                }
                route_coverage.append({"item_id": item_id, **item["primary_routing"]})
                if coverage_complete:
                    lookup_pack.append({
                        "item_id": item_id, "label": item["label"],
                        "business_rule": item["business_rule_markdown"],
                        "summary_candidates": [_model_candidate(source) for source in item["summary_candidates"]],
                        "selected_sections": item["primary_routing"]["selected_sections"],
                        "primary_candidates": [_model_candidate(source) for source in item["primary_candidates"]],
                        "coverage_complete": True,
                    })
                else:
                    existing = next((value for value in raw_model_items if value.get("item_id") == item_id), {})
                    existing_refs = [ref for ref in existing.get("references", [])
                                     if isinstance(ref, dict) and ref.get("role") == item["primary_role"]]
                    unresolved_reference = dict(existing_refs[0]) if existing_refs else {
                        "role": item["primary_role"], "value": "", "source_ids": []}
                    unresolved_reference.update({"status": "UNRESOLVED",
                                                 "reason": "章节路由不足以证明该指标已被完整检查。"})
                    raw_model_items = [value for value in raw_model_items if value.get("item_id") != item_id]
                    raw_model_items.append({
                        **existing, "item_id": item_id,
                        "summary_value": existing.get("summary_value", ""),
                        "summary_source_ids": existing.get("summary_source_ids", []),
                        "primary_lookup_status_override": "UNRESOLVED",
                        "primary_lookup_reason_code": "NO_CONFIDENT_SECTION_ROUTE" if not selected_ids else "SECTION_CONTEXT_EXCEEDS_LIMIT",
                        "references": [unresolved_reference],
                        "reason": "章节路由不足以证明该指标已被完整检查。",
                    })

            if trace:
                trace({"kind": "artifact", "path": "comparison/primary_section_routing/coverage.json", "content": route_coverage})
            lookup_prompt = (
                "仅检查每项列出的完整章节原文，不得推断未提供章节中的内容。先提取概要侧目标事实，再在已路由的定期报告章节中找同一字段。"
                "仅当 coverage_complete 为true且所列章节原文已完整检查、明确没有该字段时返回CONFIRMED_ABSENT；若找到字段返回FOUND；章节含义不符、引用不清或无法确定则UNRESOLVED。"
                "primary_value必须是被引source_id直接支持的定期报告字段原文，不要把报告季度、报告期间或推导结论拼进primary_value；期间解释写入reason。"
                "标签和值可能分处不同单元格或跨页，结合完整章节上下文关联，但引用必须使用原始source_id。此步骤不查看招募说明书，不允许Excel行号作为指标ID。严格返回JSON："
                "{\"items\":[{\"item_id\":\"stable-rule-id\",\"summary_value\":\"\",\"summary_source_ids\":[],\"primary_status\":\"FOUND|CONFIRMED_ABSENT|UNRESOLVED\",\"primary_value\":\"\",\"primary_source_ids\":[],\"comparison_status\":\"MATCH|MISMATCH|UNRESOLVED\",\"reason\":\"\"}]}\n"
                + json.dumps({"items": lookup_pack}, ensure_ascii=False)
            )
            if not lookup_pack:
                lookup_result = {"items": []}
            else:
                if trace:
                    trace({"kind": "artifact", "path": "comparison/primary_section_lookup/model_request.json",
                           "content": {"prompt": lookup_prompt, "source_count": sum(len(item["primary_candidates"]) for item in lookup_pack),
                                       "items": [item["item_id"] for item in lookup_pack]}})
                lookup_raw = str(await provider.generate_text(system, lookup_prompt))
                if trace:
                    trace({"kind": "artifact", "path": "comparison/primary_section_lookup/model_response.txt", "content": lookup_raw})
                lookup_result = _parse_json(lookup_raw)
            for found in lookup_result.get("items", []):
                if not isinstance(found, dict):
                    continue
                normalized = _normalize_model_item_id(found, row_to_item_id)
                if not normalized or normalized.get("item_id") not in known_item_ids:
                    continue
                found = normalized
                primary_status = found.get("primary_status")
                mapped_status = {"FOUND": found.get("comparison_status"), "CONFIRMED_ABSENT": "SOURCE_MISSING", "NOT_APPLICABLE": "SOURCE_MISSING"}.get(primary_status, "UNRESOLVED")
                previous = next((value for value in raw_model_items if value.get("item_id") == found["item_id"]), {})
                found_summary_ids = found.get("summary_source_ids") if isinstance(found.get("summary_source_ids"), list) else []
                found_summary_value = str(found.get("summary_value") or "")
                if not found_summary_ids or not found_summary_value:
                    found_summary_ids = previous.get("summary_source_ids", [])
                    found_summary_value = str(previous.get("summary_value") or "")
                raw_model_items = [value for value in raw_model_items if value.get("item_id") != found["item_id"]]
                raw_model_items.append({
                    **previous,
                    "item_id": found["item_id"], "summary_value": found_summary_value,
                    "summary_source_ids": found_summary_ids,
                    "primary_lookup_status_override": primary_status,
                    "primary_lookup_reason_code": "ROUTED_SECTION_UNRESOLVED" if primary_status == "UNRESOLVED" else None,
                    "primary_routing": item_by_id[found["item_id"]].get("primary_routing"),
                    "references": [{"role": "periodic_report", "status": mapped_status,
                                    "value": found.get("primary_value", ""), "source_ids": found.get("primary_source_ids", []),
                                    "reason": found.get("reason", "") }],
                    "reason": found.get("reason", ""),
                })
        except Exception as exc:
            failed_groups.append({"group": "primary_section_lookup", "error": f"{type(exc).__name__}: {exc}"})
            for item in missing_primary_items:
                existing = next((value for value in raw_model_items if value.get("item_id") == item["item_id"]), {})
                existing_refs = [ref for ref in existing.get("references", [])
                                 if isinstance(ref, dict) and ref.get("role") == item["primary_role"]]
                unresolved_reference = dict(existing_refs[0]) if existing_refs else {
                    "role": item["primary_role"], "status": "UNRESOLVED", "value": "", "source_ids": []}
                unresolved_reference["status"] = "UNRESOLVED"
                raw_model_items = [value for value in raw_model_items if value.get("item_id") != item["item_id"]]
                raw_model_items.append({
                    **existing, "item_id": item["item_id"],
                    "summary_value": existing.get("summary_value", ""),
                    "summary_source_ids": existing.get("summary_source_ids", []),
                    "primary_lookup_status_override": "UNRESOLVED",
                    "primary_lookup_reason_code": "SECTION_ROUTING_FAILED",
                    "primary_routing": item.get("primary_routing"),
                    "references": [unresolved_reference],
                })
            if trace:
                trace({"kind": "event", "event": "summary_section_lookup_failed", "details": {"error": f"{type(exc).__name__}: {exc}"}})
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
    for rule in RULES:
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
        primary_conclusion_verified = (
            primary_status in {"MATCH", "MISMATCH"}
            and len(primary_evidence) == len(set(primary_ids))
            and bool(primary_evidence)
            and _numbers_are_grounded(str(primary_ref.get("value") or ""), primary_evidence)
            and _field_evidence_is_substantive(rule, primary_evidence)
        )
        fallback_reason = _fallback_trigger_reason(
            rule, rule_input["fallback_when"],
            lookup_override=value.get("primary_lookup_status_override"),
            primary_not_applicable=not_applicable,
            primary_conclusion_verified=primary_conclusion_verified,
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
            "fallback_candidates": rule_input["fallback_candidates"],
        })
    if fallback_requests:
        fallback_prompt = (
            "按每项已验证目标事实及其Markdown规则，在允许的补充文件中查找同一业务字段。same_contract_annual_rate只允许查同一基金合同的年费率；不得把定期报告里已验证的主来源差异替换掉。不得重新提取、改写verified_target的值或source_id。"
            "补充文件的版本日期若晚于或无法与概要编制日期确认先后，只能返回其原文证据，comparison_status必须为UNRESOLVED。"
            "候选包含同页bbox坐标。表格标签和值可能分处相邻单元格或不同行，请结合坐标与列关系绑定；必须引用字段标签与对应值的原始source_id。"
            "找到补充来源时返回FOUND和补充值及source_id；字段身份、适用口径或引用不清时返回UNRESOLVED。严格返回JSON："
            "{\"items\":[{\"item_id\":\"stable-rule-id\",\"status\":\"FOUND|UNRESOLVED\",\"comparison_status\":\"MATCH|MISMATCH|UNRESOLVED\",\"value\":\"\",\"source_ids\":[],\"reason\":\"\"}]}\n"
            + json.dumps({"items": fallback_requests}, ensure_ascii=False)
        )
        if trace:
            trace({"kind": "artifact", "path": "comparison/fallback_batch/model_request.json",
                   "content": {"prompt": fallback_prompt, "item_ids": [item["item_id"] for item in fallback_requests]}})
        try:
            fallback_raw = str(await provider.generate_text(system, fallback_prompt))
            if trace:
                trace({"kind": "artifact", "path": "comparison/fallback_batch/model_response.txt", "content": fallback_raw})
            response_by_id = {value["item_id"]: value for value in _parse_json(fallback_raw).get("items", [])
                              if isinstance(value, dict) and value.get("item_id") in {item["item_id"] for item in fallback_requests}}
            for request in fallback_requests:
                item_id = request["item_id"]
                response = response_by_id.get(item_id, {})
                fallback_role = request["fallback_role"]
                fallback_ids = response.get("source_ids") if isinstance(response.get("source_ids"), list) else []
                evidence = [valid_sources[source_id] for source_id in fallback_ids
                            if source_id in valid_sources and source_id in allowed_fallback_ids[item_id]
                            and valid_sources[source_id]["document_role"] == fallback_role]
                fallback_value = str(response.get("value") or "")
                if (response.get("status") != "FOUND" or len(evidence) != len(set(fallback_ids)) or not evidence
                        or not _numbers_are_grounded(fallback_value, evidence)
                        or not _field_evidence_is_substantive(rule_by_id[item_id], evidence)):
                    fallback_outcomes[item_id] = {"status": "UNRESOLVED", "reason": str(response.get("reason") or "补充来源未能确认字段。")}
                    continue
                target = request["verified_target"]
                target_evidence = [valid_sources[source_id] for source_id in target["source_ids"]]
                fallback_outcomes[item_id] = {
                    "status": "FOUND", "reason": "补充来源找到可引用证据。",
                    "comparison_status": response.get("comparison_status"),
                    "date_status": request["fallback_date_status"],
                    "date_reason": request["fallback_date_reason"],
                    "value": fallback_value, "evidence": evidence,
                    "summary_value": target["value"], "summary_evidence": target_evidence,
                }
        except Exception as exc:
            for request in fallback_requests:
                fallback_outcomes[request["item_id"]] = {"status": "EXTRACTION_FAILED", "reason": f"补充来源识别失败：{type(exc).__name__}"}
    results = []
    target_fact_records = []
    selected_reference_fact_records = []
    for rule in RULES:
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

        if _can_compare_numeric_deterministically(rule, ref_status) and summary_evidence and reference_evidence:
            percent = True
            summary_number = _normalized_numeric_value(summary_value, percent=percent)
            reference_number = _normalized_numeric_value(reference_value, percent=percent)
            if summary_number is None or reference_number is None:
                ref_status = "UNRESOLVED"
            else:
                ref_status = "MATCH" if summary_number == reference_number else "MISMATCH"
        if rule.get("comparison_policy") in {"role_aware_identifier", "code_set_intersection"} and summary_evidence and reference_evidence:
            code_status = _compare_code_sets(summary_value, reference_value)
            ref_status = code_status or "UNRESOLVED"
        if selected_role == fallback_role and reference_evidence and not _can_compare_numeric_deterministically(rule, ref_status):
            # The fallback lookup response includes a separate semantic comparison decision.
            ref_status = ref_status if ref_status in {"MATCH", "MISMATCH", "UNRESOLVED"} else "UNRESOLVED"
        if rule_input["primary_source_eligibility"] != "ELIGIBLE":
            status, reason_code = "NOT_CHECKED", "PRIMARY_SOURCE_DATE_UNCONFIRMED" if rule_input["primary_source_eligibility"] == "DATE_UNCONFIRMED" else "PRIMARY_SOURCE_AFTER_TARGET_DATE"
        elif fallback_selected and fallback_outcome.get("date_status") != "ELIGIBLE":
            status, reason_code = "UNRESOLVED", "FALLBACK_SOURCE_DATE_UNCONFIRMED"
        elif not rule_input["summary_candidates"] and not rule_input["primary_candidates"]:
            status, reason_code = "NOT_CHECKED", "NO_CANDIDATES_UNCONFIRMED"
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
        elif ref_status in {"MATCH", "MISMATCH"} and reference_evidence:
            status, reason_code = ref_status, None
        else:
            status, reason_code = "UNRESOLVED", "EVIDENCE_OR_COMPARISON_UNRESOLVED"
        primary_reference = {"role": primary_role, "status": "FOUND" if primary_lookup_status == "FOUND" else primary_lookup_status,
                             "evidence": reference_evidence if selected_role == primary_role else [], "raw_text": reference_value if selected_role == primary_role else None}
        selected_reference = {"role": selected_role, "raw_text": reference_value if reference_evidence else None, "evidence": reference_evidence,
                              "status": status, "reason": str(ref.get("reason") or "根据选定来源完成核对。")}
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
            "candidate_source_ids": summary_ids, "verified_sources": summary_evidence,
            "status": "VERIFIED" if summary_evidence else "CANDIDATE_UNVERIFIED" if summary_value or summary_ids else "NOT_FOUND_UNCONFIRMED",
            "reason_code": None if summary_evidence else "SOURCE_OR_FIELD_VALIDATION_FAILED" if summary_value or summary_ids else "NO_MODEL_CANDIDATE",
        }
        selected_fact_ids = ([source["source_id"] for source in reference_evidence]
                             if selected_role != primary_role else reference_ids)
        selected_reference_fact_record = {
            "item_id": item_id, "role": selected_role, "value": reference_value or None,
            "candidate_source_ids": selected_fact_ids, "verified_sources": reference_evidence,
            "status": "VERIFIED" if reference_evidence else "CANDIDATE_UNVERIFIED" if reference_value or selected_fact_ids else "NOT_FOUND_UNCONFIRMED",
            "reason_code": None if reference_evidence else "SOURCE_OR_FIELD_VALIDATION_FAILED" if reference_value or selected_fact_ids else "NO_MODEL_CANDIDATE",
        }
        target_fact_records.append(target_fact_record)
        selected_reference_fact_records.append(selected_reference_fact_record)
        results.append({
            "item_id": item_id, "module_id": rule["module_id"],
            "excel_row": rule.get("excel_row"), "key": rule["key"], "label": rule["label"], "status": status,
            "extraction_status": extraction_status,
            "applicability_status": applicability_status,
            "comparison_status": status if status in {"MATCH", "MISMATCH", "UNRESOLVED"} else "NOT_CHECKED",
            "evidence_status": evidence_status,
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
                else value.get("reason") or rule_input["primary_source_eligibility_reason"]
                or ("主要来源尚不能确认是否披露。" if status == "NOT_CHECKED" else "未提供可验证的比较依据")
            ),
        })
    if trace:
        trace({"kind": "artifact", "path": "comparison/facts/target_facts.json", "content": target_fact_records})
        trace({"kind": "artifact", "path": "comparison/facts/selected_reference_facts.json", "content": selected_reference_fact_records})
        trace({"kind": "artifact", "path": "comparison/source_selection.json", "content": {
            "rules_version": RULE_VERSION,
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
        "rules_version": RULE_VERSION,
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
