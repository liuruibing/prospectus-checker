from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

RULE_DIR = Path(__file__).resolve().parent.parent / "rules" / "investment"
COMMON_RULE_FILE = RULE_DIR / "common_公共规则.md"
DASH_VALUES = {"-", "—", "–", "－", "−"}


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    return re.sub(r"[\s\u3000]+", "", text)


def normalize_identifier(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().upper()
    return re.sub(r"[\s\-_.]+", "", text)


def normalize_numeric(raw: Any, *, percent: bool = False) -> dict[str, Any]:
    text = unicodedata.normalize("NFKC", str(raw or "")).strip()
    if text in DASH_VALUES:
        return {"kind": "DASH", "value": None}
    if not text:
        return {"kind": "EMPTY", "value": None}
    numeric = re.sub(r"\s+", "", text.replace(",", "").replace("，", ""))
    if numeric.endswith("%"):
        numeric = numeric[:-1]
    if numeric.startswith("(") and numeric.endswith(")"):
        numeric = "-" + numeric[1:-1]
    try:
        value = Decimal(numeric)
    except InvalidOperation:
        return {"kind": "UNRESOLVED", "value": None}
    if not value.is_finite():
        return {"kind": "UNRESOLVED", "value": None}
    return {"kind": "NUMBER", "value": format(value.normalize(), "f")}


def normalize_stock_code(value: Any) -> str:
    return normalize_identifier(value)
