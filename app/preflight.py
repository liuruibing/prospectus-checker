from __future__ import annotations

import calendar
import os
import re
import unicodedata
from datetime import date
from difflib import SequenceMatcher
from typing import Any

import pymupdf as fitz


_DATE_PATTERN = re.compile(
    r"(?P<year>20\d{2}|[〇零一二三四五六七八九]{4})\s*年\s*"
    r"(?P<month>\d{1,2}|[一二三四五六七八九十]{1,3})\s*月"
    r"(?:\s*(?P<day>\d{1,2}|[一二三四五六七八九十]{1,3})\s*日)?"
)
_CHINESE_DIGITS = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _chinese_integer(value: str) -> int:
    if value.isdigit():
        return int(value)
    if all(char in _CHINESE_DIGITS for char in value):
        return int("".join(str(_CHINESE_DIGITS[char]) for char in value))
    if "十" in value:
        left, _, right = value.partition("十")
        tens = _CHINESE_DIGITS.get(left, 1) if left else 1
        ones = _CHINESE_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    raise ValueError(f"UNSUPPORTED_CHINESE_NUMBER: {value}")


def _date_interval(match: re.Match[str]) -> tuple[date, date] | None:
    try:
        year = _chinese_integer(match.group("year"))
        month = _chinese_integer(match.group("month"))
        day_text = match.group("day")
        if day_text:
            parsed = date(year, month, _chinese_integer(day_text))
            return parsed, parsed
        return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
    except (ValueError, OverflowError):
        return None


def _serialize_interval(interval: tuple[date, date] | None) -> dict[str, str] | None:
    if not interval:
        return None
    return {"start": interval[0].isoformat(), "end": interval[1].isoformat()}


def _matching_date(
    text: str,
    labels: tuple[str, ...] = (),
) -> tuple[tuple[date, date] | None, str | None]:
    for label in labels:
        position = text.find(label)
        if position < 0:
            continue
        snippet = text[position:position + 120]
        match = _DATE_PATTERN.search(snippet)
        if match:
            interval = _date_interval(match)
            if interval:
                return interval, snippet[:match.end()].strip()
    matches = list(_DATE_PATTERN.finditer(text))
    for match in reversed(matches):
        interval = _date_interval(match)
        if interval:
            return interval, match.group(0)
    return None, None


def _extract_product_name(text: str, role: str) -> str | None:
    compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))
    if role == "prospectus":
        match = re.search(r"(?P<name>[\u4e00-\u9fffA-Za-z0-9（）()·]{3,100}?基金)(?:更新的)?招募说明书", compact)
    else:
        match = re.search(
            r"(?P<name>[\u4e00-\u9fffA-Za-z0-9（）()·]{3,100}?基金)20\d{2}年"
            r"(?:第[1-4]季度|半年度|年度|中期)?报告",
            compact,
        )
    return match.group("name") if match else None


def _extract_fund_code(text: str) -> str | None:
    compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))
    match = re.search(r"(?:基金主代码|基金代码|主代码)[:：]?([0-9]{6})", compact)
    return match.group(1) if match else None


def _document_metadata(filename: str, content: bytes, role: str) -> dict[str, Any]:
    document = fitz.open(stream=content, filetype="pdf")
    try:
        first_pages = [document[index].get_text() for index in range(min(3, document.page_count))]
        cover_text = first_pages[0] if first_pages else ""
        first_text = "\n".join(first_pages)
        all_text = "\n".join(page.get_text() for page in document)
        product_name = _extract_product_name(cover_text, role)
        code = _extract_fund_code(all_text)
        if role == "prospectus":
            date_interval, date_quote = _matching_date(cover_text)
            date_label = "招募说明书封面日期"
            display_date_interval, display_date_quote, display_date_label = (
                date_interval, date_quote, date_label
            )
            report_send_date_interval = None
            report_send_date_quote = None
        else:
            period_text = cover_text
            for send_date_label in ("报告送出日期", "报告披露日期", "公告日期"):
                label_position = period_text.find(send_date_label)
                if label_position >= 0:
                    period_text = period_text[:label_position]
                    break
            display_date_interval, display_date_quote = _matching_date(period_text)
            display_date_label = "报告期末"
            report_send_date_interval, report_send_date_quote = _matching_date(
                first_text,
                labels=("报告送出日期", "报告披露日期", "公告日期"),
            )
            date_interval = display_date_interval or report_send_date_interval
            date_quote = display_date_quote or report_send_date_quote
            date_label = "报告期末" if display_date_interval else "定期报告送出日期"
            if not display_date_interval:
                display_date_interval, display_date_quote = report_send_date_interval, report_send_date_quote
                display_date_label = date_label
        return {
            "filename": filename,
            "page_count": document.page_count,
            "fund_name": product_name,
            "fund_code": code,
            "date_label": date_label,
            "date_interval": _serialize_interval(date_interval),
            "date_quote": date_quote,
            "date_precision": "day" if date_interval and date_interval[0] == date_interval[1] else "month",
            "report_send_date_interval": _serialize_interval(report_send_date_interval),
            "report_send_date_quote": report_send_date_quote,
            "display_date_label": display_date_label,
            "display_date_interval": _serialize_interval(display_date_interval),
            "display_date_quote": display_date_quote,
            "display_date_precision": (
                "day" if display_date_interval and display_date_interval[0] == display_date_interval[1]
                else "month"
            ),
        }
    finally:
        document.close()


def _days_between_intervals(
    left: tuple[date, date], right: tuple[date, date],
) -> int:
    if left[0] <= right[1] and right[0] <= left[1]:
        return 0
    if left[1] < right[0]:
        return (right[0] - left[1]).days
    return (left[0] - right[1]).days


def _read_interval(value: dict[str, str] | None) -> tuple[date, date] | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value["start"]), date.fromisoformat(value["end"])
    except (KeyError, TypeError, ValueError):
        return None


def preflight_documents(payload: dict[str, Any]) -> dict[str, Any]:
    """Use cover metadata to reject clearly incompatible document pairs before AI extraction."""
    prospectus = _document_metadata(
        str(payload["prospectus_filename"]), payload["prospectus_content"], "prospectus"
    )
    periodic_report = _document_metadata(
        str(payload["periodic_filename"]), payload["periodic_content"], "periodic_report"
    )
    status = "READY"
    message = None
    code_mismatch = bool(
        prospectus.get("fund_code")
        and periodic_report.get("fund_code")
        and prospectus["fund_code"] != periodic_report["fund_code"]
    )
    names = (prospectus.get("fund_name"), periodic_report.get("fund_name"))
    name_mismatch = bool(
        not code_mismatch
        and not (prospectus.get("fund_code") and periodic_report.get("fund_code"))
        and names[0]
        and names[1]
        and SequenceMatcher(None, names[0], names[1]).ratio() < 0.82
    )
    if code_mismatch or name_mismatch:
        status = "FUND_MISMATCH"
        left_identity = prospectus.get("fund_code") or prospectus.get("fund_name") or "未识别"
        right_identity = periodic_report.get("fund_code") or periodic_report.get("fund_name") or "未识别"
        message = f"产品不一致，无法比较。招募说明书识别为 {left_identity}，定期报告识别为 {right_identity}。"

    max_gap_days = max(0, int(os.getenv("PROSPECTUS_MAX_DATE_GAP_DAYS", "183")))
    left_interval = _read_interval(prospectus.get("date_interval"))
    right_interval = _read_interval(periodic_report.get("date_interval"))
    date_gap_days = _days_between_intervals(left_interval, right_interval) if left_interval and right_interval else None
    if status == "READY" and date_gap_days is not None and date_gap_days > max_gap_days:
        status = "DATE_MISMATCH"
        message = (
            f"招募说明书与定期报告日期相差 {date_gap_days} 天，超过允许的 {max_gap_days} 天，无法比较。"
        )

    return {
        "status": status,
        "blocked": status != "READY",
        "message": message,
        "max_date_gap_days": max_gap_days,
        "date_gap_days": date_gap_days,
        "prospectus": prospectus,
        "periodic_report": periodic_report,
    }
