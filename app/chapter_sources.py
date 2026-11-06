from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from app.domain import normalize_numeric, normalize_text


def column_unit(field: str, header: str) -> str:
    if "ratio" in field:
        return "%" if any(token in header for token in ("%", "％", "比例")) else ""
    return next((unit for unit in ("亿元", "万元", "元", "万股", "股") if unit in header), "")


def markdown_rows(markdown: str) -> list[list[str]]:
    rows = []
    for line in markdown.splitlines():
        if "|" not in line:
            continue
        cells = [re.sub(r"</?[^>]+>|[*`]", "", cell).strip() for cell in line.strip().strip("|").split("|")]
        if cells and not all(not cell or set(cell) <= {"-", ":"} for cell in cells):
            rows.append(cells)
    return rows


def source_catalogue(context: dict, rule: dict, tables: dict, header_maps: dict) -> dict[str, dict]:
    """Only expose mapped numeric rows whose table is supported by this chapter."""
    records = {}
    md_rows = markdown_rows(context.get("section_markdown", ""))
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
                and all(column < len(row) and normalize_numeric(row[column])["kind"] in {"NUMBER", "DASH"}
                        for key, column in mapping.items() if key in rule["fields"])
                for row in table["matrix"]
            ):
                continue
            for index, cells in enumerate(table["matrix"]):
                values = {}
                for field in rule["fields"]:
                    column = mapping[field]
                    raw = str(cells[column] if cells[column] is not None else "").strip() if column < len(cells) else ""
                    if normalize_numeric(raw)["kind"] not in {"NUMBER", "DASH"}:
                        break
                    header = " ".join(str(row[column] or "") for row in table["matrix"][:3] if column < len(row))
                    unit = table.get("column_units", {}).get(rule["rule_id"], {}).get(field, column_unit(field, header))
                    values[field] = {"raw_text": raw, "unit": unit, "page": page,
                                     "quote": " ".join(str(cell).strip() for cell in cells if cell is not None),
                                     "source_ref": {"table": table_index, "row": index}}
                if len(values) != len(rule["fields"]):
                    continue
                source_id = f"p{page}t{table_index}r{index}"
                records[source_id] = {"source_id": source_id, "page": page, "source_ref": {"table": table_index, "row": index},
                                      "cells": [str(c if c is not None else "").strip() for c in cells],
                                      "mapping": mapping, "fields": values}
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
        for name in ("extraction.py", "chapter_sources.py", "domain.py", "ai.py"):
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
