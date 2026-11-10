"""Check conversion against independently reviewed PDF rows; never invoke a model."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
import unicodedata
import re

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.pdf_document import verified_conversion


def compact(text):
    return "".join(unicodedata.normalize("NFKC", str(text)).split()).replace("−", "-")


def contains_row(cells, expected):
    number = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?%?|[-—]")
    cells = [compact(x) for x in cells if compact(x)]
    tokens = [compact(x) for x in expected if compact(x)]
    actual_numbers = [x for x in cells if number.fullmatch(x)]
    expected_numbers = [x for x in tokens if number.fullmatch(x)]
    # Some reviewed fixtures represent '其中:股票' as two label tokens and duplicate
    # a total label into the code column. Numeric cells still require exact matches.
    labels = []
    for x in tokens:
        if not number.fullmatch(x) and (not labels or labels[-1] != x):
            labels.append(x)
    actual_label = "".join(x for x in cells if not number.fullmatch(x)).replace(":", "")
    return actual_numbers == expected_numbers and actual_label == "".join(labels).replace(":", "")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", action="append", required=True)
    parser.add_argument("--output", type=Path, default=Path("var/pdf-markdown-validation/independent-rows.json"))
    args = parser.parse_args()
    reports = []
    for case in args.case:
        expected = json.loads((Path("evaluation/expected")/(case+".json")).read_text())
        for entry in expected["documents"]:
            content = Path(entry["path"]).read_bytes()
            assert hashlib.sha256(content).hexdigest() == entry["sha256"]
            started = time.monotonic()
            package = verified_conversion(content)
            document = package["source_map"]
            rows = []
            for table in document["blocks"]:
                if table["kind"] != "table":
                    continue
                for row in table["rows"]:
                    rows.append({"cells": ["".join(document["segments"][s]["text"] for s in cell["segments"]) for cell in row],
                                 "pages": {document["segments"][s]["page"] for cell in row for s in cell["segments"]}})
            checks = []
            for module in expected["modules"]:
                for index, row in enumerate(module["rows"]):
                    evidence = next((e for e in row.get("evidence", []) if e["role"] == entry["role"]), None)
                    if evidence is None:
                        continue
                    values = row.get("reference_values", row["values"]) if entry["role"] != "target" else row["values"]
                    matching = [r for r in rows if contains_row(r["cells"], values)]
                    matched = bool(matching)
                    exact_page = any(evidence["pdf_page"] in r["pages"] for r in matching)
                    checks.append({"module": module["module_id"], "row": index, "page": evidence["pdf_page"],
                                   "values": values, "ok": matched, "expected_page_matches": exact_page,
                                   "actual_pages": sorted({p for r in matching for p in r["pages"]})})
            report = {"case": case, "role": entry["role"], "input": entry["path"], "status": package["status"],
                      "verification": package["verification"], "warnings": package["warnings"],
                      "rows_checked": len(checks), "rows_matched": sum(c["ok"] for c in checks),
                      "failures": [c for c in checks if not c["ok"]], "seconds": round(time.monotonic()-started, 3),
                      "expected_page_disagreements": [c for c in checks if c["ok"] and not c["expected_page_matches"]],
                      "archive": package["archive_path"], "cache_hit": package["cache_hit"]}
            reports.append(report)
            print(json.dumps({k: report[k] for k in ("case", "role", "status", "rows_checked", "rows_matched", "seconds")}), flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(reports, ensure_ascii=False, indent=2))
    return int(any(r["status"] != "READY" or r["failures"] or not r["verification"]["ok"] for r in reports))


if __name__ == "__main__":
    raise SystemExit(main())
