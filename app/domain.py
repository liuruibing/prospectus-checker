from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

RULE_DIR = Path(__file__).resolve().parent.parent / "rules" / "investment"
MODULE_RULES = [
    {
        "rule_id": "investment.asset_allocation",
        "title": "报告期末基金资产组合情况",
        "file": RULE_DIR / "asset_allocation_资产组合情况.md",
        "fields": {"amount": "金额", "ratio_of_total_assets": "占基金总资产比例"},
    },
    {
        "rule_id": "investment.industry_stock_portfolio",
        "title": "报告期末按行业分类的股票投资组合",
        "file": RULE_DIR / "industry_stock_portfolio_行业分类股票投资组合.md",
        "fields": {"fair_value": "公允价值", "ratio_of_net_assets": "占基金资产净值比例"},
    },
    {
        "rule_id": "investment.stock_holdings",
        "title": "期末按公允价值占基金资产净值比例排序的股票投资明细",
        "file": RULE_DIR / "stock_holdings_股票投资明细.md",
        "fields": {"quantity": "持有数量", "fair_value": "公允价值", "ratio_of_net_assets": "占基金资产净值比例"},
    },
]
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


def title_aliases(rule_text: str) -> list[str]:
    for line in rule_text.splitlines():
        if "标题别名：" in line:
            values = line.split("标题别名：", 1)[1]
            aliases = re.split(r"[、，,；;]", values)
            return [alias.strip(" `。；;，,") for alias in aliases if alias.strip(" `。；;，,")]
    return []
