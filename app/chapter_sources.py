from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from app.domain import normalize_numeric, normalize_text
from app.source_evidence import evidence_source


def column_unit(field: str, header: str) -> str:
    if "ratio" in field:
        return "%" if any(token in header for token in ("%", "％", "比例")) else ""
    return next((unit for unit in ("亿元", "万元", "元", "万股", "股") if unit in header), "")


def resolve_data_columns(table: dict, header_mapping: dict[str, int], row_index: int, rule_id: str | None = None) -> dict[str, int]:
    """Map semantic header cells to body cells using horizontal cell geometry.

    PDF table extractors can split merged header cells differently from body
    cells. The header cell center is a stable anchor for the corresponding
    body cell; an original column-index fallback is used only if populated.
    """
    matrix, cell_rows = table.get("matrix", []), table.get("cells_by_row", [])
    if row_index >= len(matrix) or row_index >= len(cell_rows):
        return {}
    row_cells, row_values = cell_rows[row_index], matrix[row_index]
    resolved: dict[str, int] = {}
    inherited = table.get("inherited_header_centers", {})
    if rule_id and isinstance(inherited.get(rule_id), dict):
        inherited = inherited[rule_id]
    for field, header_column in header_mapping.items():
        center_x = inherited.get(field)
        for header_row in range(min(3, len(matrix), len(cell_rows))):
            if center_x is not None:
                break
            if header_column >= len(matrix[header_row]) or not str(matrix[header_row][header_column] or "").strip():
                continue
            rects = cell_rows[header_row]
            if header_column < len(rects) and rects[header_column] and len(rects[header_column]) == 4:
                center_x = (float(rects[header_column][0]) + float(rects[header_column][2])) / 2
                break
        candidates = []
        if center_x is not None:
            for column, rect in enumerate(row_cells):
                if rect is None or len(rect) != 4 or column >= len(row_values):
                    continue
                if not str(row_values[column] or "").strip():
                    continue
                if float(rect[0]) - 0.75 <= center_x <= float(rect[2]) + 0.75:
                    candidates.append(column)
        if len(candidates) == 1:
            resolved[field] = candidates[0]
        elif (center_x is None and header_column < len(row_cells) and header_column < len(row_values)
              and row_cells[header_column] is not None
              and str(row_values[header_column] or "").strip()):
            resolved[field] = header_column
    return resolved


def _header_centers(table: dict, header_mapping: dict[str, int]) -> dict[str, float]:
    matrix, cell_rows = table.get("matrix", []), table.get("cells_by_row", [])
    centers = {}
    for field, column in header_mapping.items():
        for row_index in range(min(3, len(matrix), len(cell_rows))):
            if column >= len(matrix[row_index]) or not str(matrix[row_index][column] or "").strip():
                continue
            rects = cell_rows[row_index]
            if column < len(rects) and rects[column] and len(rects[column]) == 4:
                centers[field] = (float(rects[column][0]) + float(rects[column][2])) / 2
                break
    return centers


def _centers_map_uniquely(table: dict, centers: dict[str, float]) -> bool:
    matrix, cell_rows = table.get("matrix", []), table.get("cells_by_row", [])
    if not centers or not matrix or not cell_rows:
        return False
    for row_index in range(min(3, len(matrix), len(cell_rows))):
        row_values, row_rects = matrix[row_index], cell_rows[row_index]
        columns = []
        for center_x in centers.values():
            hits = [column for column, rect in enumerate(row_rects)
                    if rect is not None and len(rect) == 4 and column < len(row_values)
                    and str(row_values[column] or "").strip()
                    and float(rect[0]) - 0.75 <= center_x <= float(rect[2]) + 0.75]
            if len(hits) != 1:
                return False
            columns.append(hits[0])
        if len(set(columns)) != len(columns):
            return False
    return True


def markdown_rows(markdown: str) -> list[list[str]]:
    class TableReader(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.rows, self.row, self.cell = [], None, None
        def handle_starttag(self, tag, attrs):
            if tag == "tr":
                self.row = []
            if tag in {"td", "th"}:
                self.cell = ""
            if tag == "br" and self.cell is not None:
                self.cell += "\n"
        def handle_data(self, data):
            if self.cell is not None:
                self.cell += data
        def handle_endtag(self, tag):
            if tag in {"td", "th"} and self.row is not None:
                self.row.append((self.cell or "").strip())
                self.cell = None
            if tag == "tr" and self.row is not None:
                self.rows.append(self.row)
                self.row = None
    reader = TableReader()
    reader.feed(markdown)
    rows = reader.rows
    for line in markdown.splitlines():
        if "|" not in line or "<tr>" in line:
            continue
        cells = [unescape(re.sub(r"</?[^>]+>|[*`]", "", cell)).strip() for cell in line.strip().strip("|").split("|")]
        if cells and not all(not cell or set(cell) <= {"-", ":"} for cell in cells):
            rows.append(cells)
    return rows


def source_catalogue(context: dict, rule: dict, tables: dict, header_maps: dict,
                    document_sha256: str | None = None, document_role: str = "periodic_report") -> dict[str, dict]:
    """Only expose mapped numeric rows whose table is supported by this chapter."""
    records = {}
    md_rows = markdown_rows(context.get("section_markdown", ""))
    if not document_sha256:
        document_sha256 = hashlib.sha256(
            str(context.get("section_markdown", "")).encode("utf-8")
        ).hexdigest()
    plain_section = normalize_text(re.sub(r"</?[^>]+>|[|*`]", "", context.get("section_markdown", "")))
    for source in context.get("physical_pages", []):
        page = source["physical_page"]
        for table_index, table in enumerate(tables.get(page, [])):
            mapping = header_maps.get(rule["rule_id"], {}).get(page, {}).get(table_index, {})
            if not set(rule["fields"]).issubset(mapping):
                continue
            # Matching a whole row avoids assigning a nearby table to this chapter.
            if not any(
                (
                    any([normalize_text(cell) for cell in row if normalize_text(cell)]
                        == [normalize_text(cell) for cell in md_row if normalize_text(cell)] for md_row in md_rows)
                    or normalize_text("".join(str(cell) for cell in row if cell is not None)) in plain_section
                )
                and set(rule["fields"]).issubset(row_mapping)
                and all(normalize_numeric(row[row_mapping[key]])["kind"] in {"NUMBER", "DASH"}
                        for key in rule["fields"])
                for row_index, row in enumerate(table["matrix"])
                for row_mapping in [resolve_data_columns(table, mapping, row_index, rule["rule_id"])]
            ):
                continue
            for index, cells in enumerate(table["matrix"]):
                row_mapping = resolve_data_columns(table, mapping, index, rule["rule_id"])
                values = {}
                for field in rule["fields"]:
                    column = row_mapping.get(field)
                    if column is None:
                        break
                    raw = str(cells[column] if cells[column] is not None else "").strip() if column < len(cells) else ""
                    if normalize_numeric(raw)["kind"] not in {"NUMBER", "DASH"}:
                        break
                    header = " ".join(str(row[column] or "") for row in table["matrix"][:3] if column < len(row))
                    unit = table.get("column_units", {}).get(rule["rule_id"], {}).get(field, column_unit(field, header))
                    values[field] = {"raw_text": raw, "unit": unit, "page": page,
                                     "quote": " ".join(str(cell).strip() for cell in cells if cell is not None),
                                     "source_ref": {"table": table_index, "row": index, "mapping": row_mapping}}
                if len(values) != len(rule["fields"]):
                    continue
                row_cells = table.get("cells_by_row", [])
                cell_rects = row_cells[index] if index < len(row_cells) else []
                valid_rects = [rect for rect in cell_rects if rect is not None and len(rect) == 4]
                bbox = ([min(float(rect[0]) for rect in valid_rects), min(float(rect[1]) for rect in valid_rects),
                         max(float(rect[2]) for rect in valid_rects), max(float(rect[3]) for rect in valid_rects)]
                        if valid_rects else None)
                source_ref = {"table": table_index, "row": index, "mapping": row_mapping}
                common = evidence_source(
                    document_sha256=document_sha256,
                    document_role=document_role,
                    page=page,
                    source_kind="table_row",
                    locator=f"table:{table_index}:row:{index}",
                    raw_text=" ".join(str(cell).strip() for cell in cells if cell is not None),
                    bbox=bbox,
                    extra={"source_ref": source_ref},
                )
                source_id = common["source_id"]
                records[source_id] = {**common, "page": page, "source_ref": source_ref,
                                      "cells": [str(c if c is not None else "").strip() for c in cells],
                                      "mapping": row_mapping, "fields": values}
    return records


def render_catalogue(records: dict[str, dict]) -> str:
    return json.dumps([
        {"source_id": key, "page": value["page"], "cells": value["cells"]}
        for key, value in records.items()
    ], ensure_ascii=False, separators=(",", ":"))


def source_gaps(context: dict, records: dict[str, dict], rule: dict) -> list[str]:
    missing = []
    for cells in markdown_rows(context.get("section_markdown", "")):
        tokens = [normalize_text(cell) for cell in cells if normalize_text(cell)]
        if sum(normalize_numeric(token)["kind"] in {"NUMBER", "DASH"} for token in tokens) < len(rule["fields"]):
            continue
        if not any(normalize_numeric(token)["kind"] == "UNRESOLVED" for token in tokens):
            continue
        if not any(tokens == [normalize_text(cell) for cell in source["cells"] if normalize_text(cell)] for source in records.values()):
            missing.append(" | ".join(cells))

    # AnyDoc may render a continued table's final row as plain text after the
    # printed page footer. Check configured row names in the full chapter text
    # so a detached total or continuation row cannot disappear just because it
    # is outside a Markdown pipe table / PyMuPDF table grid.
    rule_path = rule.get("file")
    rule_text = rule_path.read_text(encoding="utf-8") if isinstance(rule_path, Path) else ""
    aliases = []
    in_row_policy = False
    for line in rule_text.splitlines():
        if line.startswith("## "):
            in_row_policy = line[3:].strip() == "row_policy"
            continue
        if not in_row_policy or not line.startswith("|"):
            continue
        cells = [cell.strip().strip("` ") for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2 or cells[0] == "row_key" or set(cells[0]) <= {"-", ":"}:
            continue
        aliases.extend(value.strip(" `。；;，,") for value in re.split(r"[、/；;]", cells[1]) if value.strip(" `。；;，,"))
    known_source_labels = {
        normalize_text(cell)
        for source in records.values()
        for cell in source.get("cells", [])
        if normalize_text(cell)
    }
    for line in context.get("section_markdown", "").splitlines():
        if "|" in line or "<tr>" in line:
            continue
        line_without_footer = re.sub(r"第\s*\d+\s*页(?:\s*共\s*\d+\s*页)?", "", line)
        plain = normalize_text(line_without_footer)
        if not plain:
            continue
        matching_aliases = [alias for alias in aliases if len(normalize_text(alias)) >= 2
                            and normalize_text(alias) in plain]
        numeric_count = sum(
            normalize_numeric(token)["kind"] in {"NUMBER", "DASH"}
            for token in re.findall(r"(?<![\w])(?:[-—–－]|\d[\d,，]*(?:\.\d+)?%?)(?![\w])", line_without_footer)
        )
        if numeric_count < len(rule.get("fields", [])):
            continue
        if matching_aliases and not any(normalize_text(alias) in known_source_labels for alias in matching_aliases):
            missing.append(line.strip())
    return missing


def bind_sources(module: dict, records: dict[str, dict], rule: dict) -> dict:
    """Resolve source IDs locally; never accept model-generated numeric replacements."""
    result = dict(module)
    result["rule_id"] = rule["rule_id"]
    rows = []
    issues = []
    used = set()
    for raw in module.get("rows", []) if isinstance(module.get("rows"), list) else []:
        if not isinstance(raw, dict):
            issues.append("模型返回了无效行结构。")
            continue
        source_id = raw.get("source_id")
        if not isinstance(source_id, str) or source_id not in records or source_id in used:
            issues.append("模型返回未知或重复的来源行，需复核。")
            continue
        used.add(source_id)
        source = records[source_id]
        row = {key: value for key, value in raw.items() if key not in {"fields", "row_page", "row_quote"}}
        row["row_page"] = source["page"]
        row["source_ref"] = source.get("source_ref")
        row["row_quote"] = " ".join(source["cells"])
        row["fields"] = source["fields"]
        for key in ("industry_code", "industry_name", "stock_code", "stock_name", "rank"):
            column = source["mapping"].get(key)
            if column is not None:
                row[key] = source["cells"][column] or None
        if row.get("industry_name") or row.get("stock_name"):
            row["row_label"] = row.get("industry_name") or row["stock_name"]
        rows.append(row)
    missing = sorted(set(records) - used)
    if missing:
        issues.append(f"源表有 {len(missing)} 行未被模型确认：{', '.join(missing)}。")
    if records and not module.get("found"):
        issues.append("已找到章节源表，模型却返回未发现章节。")
    result.update(rows=rows, found=bool(module.get("found") or records), source_warnings=issues,
                  source_coverage={"expected": len(records), "covered": len(used), "missing": missing})
    return result


class ChapterCache:
    """Cache only verified chapters; file permissions match task audit artifacts."""

    def __init__(self) -> None:
        root = Path(__file__).resolve().parent.parent
        self.root = Path(os.getenv("PROSPECTUS_CACHE_DIR", str(root / "var" / "cache"))).expanduser()

    def key(self, namespace: dict, system: str, prompt: str) -> str:
        version = hashlib.sha256()
        for name in ("extraction.py", "chapter_sources.py", "pdf_document.py", "pdf_markdown.py", "pdf_graphics.py", "headings.py", "source_evidence.py", "domain.py", "ai.py"):
            version.update(Path(__file__).with_name(name).read_bytes())
        value = json.dumps([version.hexdigest(), namespace, system, prompt], sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(value.encode()).hexdigest()

    def load(self, key: str) -> dict | None:
        try:
            record = json.loads((self.root / f"{key}.json").read_text(encoding="utf-8"))
            return record if record.get("key") == key and isinstance(record.get("response"), str) else None
        except (OSError, ValueError, AttributeError):
            return None

    def save(self, key: str, response: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.root, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({"key": key, "response": response}, stream, ensure_ascii=False)
        try:
            os.replace(temporary, self.root / f"{key}.json")
        finally:
            temporary.unlink(missing_ok=True)
