"""Export PDF to a continuous, source-addressable Markdown without calling AI."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.pdf_markdown import convert_pdf_bytes, write_conversion


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    package = convert_pdf_bytes(args.pdf.read_bytes())
    write_conversion(package, args.output)
    print(json.dumps({"output": str(args.output.resolve()), "status": package["status"],
        "verification": package["verification"], "events": len(package["source_map"]["events"]),
        "merged": sum(bool(e.get("merged")) for e in package["source_map"]["events"])}, ensure_ascii=False))
    return 0 if package["status"] == "READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
