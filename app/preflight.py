from __future__ import annotations

import calendar
import re
import unicodedata
from datetime import date
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
    *,
    allow_unlabelled: bool = True,
) -> tuple[tuple[date, date] | None, str | None]:
    label_positions = sorted(
        (position, label)
        for label in labels
        if (position := text.find(label)) >= 0
    )
    for position, _label in label_positions:
        snippet = text[position:position + 120]
        match = _DATE_PATTERN.search(snippet)
        if match:
            interval = _date_interval(match)
            if interval:
                return interval, snippet[:match.end()].strip()
    if not allow_unlabelled:
        return None, None
    matches = list(_DATE_PATTERN.finditer(text))
    for match in reversed(matches):
        interval = _date_interval(match)
        if interval:
            return interval, match.group(0)
    return None, None


def _extract_product_name(text: str, role: str) -> str | None:
    compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))
    if role == "product_summary":
        match = re.search(r"(?P<name>[\u4e00-\u9fff][\u4e00-\u9fffA-Za-z0-9（）()·]{2,100}?基金)(?:\([^)]{1,30}份额\))?(?:基金)?产品资料概要", compact)
        return match.group("name") if match else None
    if role == "prospectus":
        lines = [re.sub(r"\s+", "", unicodedata.normalize("NFKC", line)) for line in text.splitlines()]
        lines = [line for line in lines if line]
        for index, line in enumerate(lines):
            marker = line.find("招募说明书")
            if marker < 0:
                continue
            title_prefix = line[:marker].removesuffix("更新的").removesuffix("更新")
            if title_prefix.endswith("基金") and len(title_prefix) >= 4:
                return title_prefix
            # Many prospectus covers put the fund name and document title on
            # adjacent lines. Prefer that title block over the running header,
            # which may be followed by a page number and manager name.
            if index:
                previous = lines[index - 1]
                if previous.endswith("基金") and len(previous) >= 4:
                    return previous
        wrapped = re.search(
            r"(?P<name>[\u4e00-\u9fffA-Za-z0-9（）()·]{3,120}?基金)(?:更新的|更新)?招募说明书",
            compact,
        )
        return wrapped.group("name") if wrapped else None
    if role == "periodic_report":
        match = re.search(
            r"(?P<name>[\u4e00-\u9fffA-Za-z0-9（）()·]{3,100}?基金)20\d{2}年"
            r"(?:第[1-4]季度|半年度|年度|中期)?报告",
            compact,
        )
        return match.group("name") if match else None
    return None


def _extract_fund_code(text: str) -> str | None:
    compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))
    match = re.search(r"(?:基金主代码|基金代码|主代码)[:：]?([0-9]{6})", compact)
    return match.group(1) if match else None


def _extract_fund_codes(text: str) -> list[str]:
    """Collect explicitly labelled master/share codes and front/back-end codes."""
    compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))
    labels = list(re.finditer(r"基金主代码|下属基金代码|基金代码|主代码|交易代码", compact))
    codes: list[str] = []
    for index, match in enumerate(labels):
        end = labels[index + 1].start() if index + 1 < len(labels) else min(len(compact), match.end() + 32)
        value = re.search(r"[0-9]{6}", compact[match.end():end])
        if value:
            codes.append(value.group(0))
    codes.extend(re.findall(r"([0-9]{6})(?=[（(](?:前端|后端)[）)])", compact))
    return list(dict.fromkeys(codes))


def _filename_date_candidate(filename: str) -> tuple[date, str] | None:
    """Read only an explicit ISO date prefix; keep it separate from PDF dates."""
    basename = filename.rsplit("/", 1)[-1]
    match = re.match(r"^(20\d{2})[-_.](\d{1,2})[-_.](\d{1,2})(?=[_ .-]|$)", basename)
    if not match:
        return None
    try:
        parsed = date(*(int(part) for part in match.groups()))
    except ValueError:
        return None
    return parsed, match.group(0)


def _document_metadata(filename: str, content: bytes, role: str) -> dict[str, Any]:
    filename_date = _filename_date_candidate(filename)
    document = fitz.open(stream=content, filetype="pdf")
    try:
        first_pages = [document[index].get_text() for index in range(min(3, document.page_count))]
        cover_text = first_pages[0] if first_pages else ""
        first_text = "\n".join(first_pages)
        all_text = "\n".join(page.get_text() for page in document)
        compact_first_text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", first_text))
        compact_cover_text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", cover_text))
        is_product_summary = "产品资料概要" in compact_cover_text and any(
            marker in compact_cover_text for marker in ("编制日期", "送出日期", "更新")
        )
        is_periodic_report = bool(re.search(r"20\d{2}年(?:第[1-4]季度|半年度|中期|年度)报告", compact_cover_text)) and not is_product_summary
        is_prospectus = "招募说明书" in compact_cover_text and not is_product_summary and not is_periodic_report
        report_subtype = (
            "annual" if re.search(r"年度报告", compact_cover_text)
            else "interim" if re.search(r"半年度报告|中期报告", compact_cover_text)
            else "quarterly" if re.search(r"季度报告", compact_cover_text)
            else None
        )
        product_name = _extract_product_name(cover_text, role)
        # Periodic reports may place the labelled fund-identity table after
        # cover, contents, and introductory pages. Search the complete report
        # text for explicitly labelled identity codes; prospectuses and
        # summaries keep the narrower opening context to avoid picking up
        # unrelated codes from examples later in those documents.
        identity_text = all_text if role == "periodic_report" else first_text
        code = _extract_fund_code(identity_text)
        identity_codes = _extract_fund_codes(identity_text)
        if code and code not in identity_codes:
            identity_codes.insert(0, code)
        if role == "prospectus":
            date_interval, date_quote = _matching_date(cover_text)
            date_label = "招募说明书封面日期"
            display_date_interval, display_date_quote, display_date_label = (
                date_interval, date_quote, date_label
            )
            report_send_date_interval = None
            report_send_date_quote = None
        elif role == "product_summary":
            date_interval, date_quote = _matching_date(first_text, labels=("编制日期", "编制日", "送出日期"))
            date_label = "产品资料概要编制日期"
            display_date_interval, display_date_quote, display_date_label = date_interval, date_quote, date_label
            report_send_date_interval = None
            report_send_date_quote = None
        else:
            period_text = cover_text
            send_date_labels = (
                "报告送出日期", "送出日期", "报告披露日期", "披露日期", "公告日期",
            )
            send_date_positions = [
                position for label in send_date_labels
                if (position := period_text.find(label)) >= 0
            ]
            if send_date_positions:
                period_text = period_text[:min(send_date_positions)]
            display_date_interval, display_date_quote = _matching_date(period_text)
            display_date_label = "报告期末"
            report_send_date_interval, report_send_date_quote = _matching_date(
                first_text,
                labels=send_date_labels,
                allow_unlabelled=False,
            )
            date_interval = display_date_interval or report_send_date_interval
            date_quote = display_date_quote or report_send_date_quote
            date_label = "报告期末" if display_date_interval else "定期报告送出日期"
            if not display_date_interval:
                display_date_interval, display_date_quote = report_send_date_interval, report_send_date_quote
                display_date_label = date_label
        version_date_interval = None
        version_date_quote = None
        version_date_source = None
        if role == "prospectus" and date_interval:
            if date_interval[0] == date_interval[1]:
                version_date_interval = date_interval
                version_date_quote = date_quote
                version_date_source = "pdf"
            elif filename_date and date_interval[0] <= filename_date[0] <= date_interval[1]:
                version_date_interval = (filename_date[0], filename_date[0])
                version_date_quote = f"文件名日期前缀：{filename_date[1]}"
                version_date_source = "filename"
        return {
            "filename": filename,
            "filename_date_interval": _serialize_interval((filename_date[0], filename_date[0]) if filename_date else None),
            "filename_date_quote": f"文件名日期前缀：{filename_date[1]}" if filename_date else None,
            "filename_date_precision": "day" if filename_date else None,
            "page_count": document.page_count,
            "is_product_summary": is_product_summary,
            "is_prospectus": is_prospectus,
            "is_periodic_report": is_periodic_report,
            "report_subtype": report_subtype,
            "fund_name": product_name,
            "fund_code": code,
            "fund_codes": identity_codes,
            "date_label": date_label,
            "date_interval": _serialize_interval(date_interval),
            "date_quote": date_quote,
            "date_precision": "day" if date_interval and date_interval[0] == date_interval[1] else "month",
            "version_date_interval": _serialize_interval(version_date_interval),
            "version_date_quote": version_date_quote,
            "version_date_source": version_date_source,
            "version_date_precision": "day" if version_date_interval and version_date_interval[0] == version_date_interval[1] else None,
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
    """Validate document roles and collect identity/date facts for rule execution."""
    prospectus = _document_metadata(
        str(payload["prospectus_filename"]), payload["prospectus_content"], "prospectus"
    )
    periodic_report = _document_metadata(
        str(payload["periodic_filename"]), payload["periodic_content"], "periodic_report"
    )
    status = "READY"
    message = None
    if periodic_report.get("is_product_summary"):
        status = "WRONG_DOCUMENT_TYPE"
        message = "右侧文件识别为产品资料概要，请切换到“产品资料概要核对”模式后重新提交。"
    left_interval = _read_interval(prospectus.get("date_interval"))
    right_interval = _read_interval(periodic_report.get("date_interval"))
    date_gap_days = _days_between_intervals(left_interval, right_interval) if left_interval and right_interval else None

    return {
        "status": status,
        "blocked": status != "READY",
        "message": message,
        # Diagnostic only: this compares a prospectus cover date with a
        # report-period date, which are different business facts. The
        # applicability decision belongs to the compiled Markdown rules.
        "date_gap_days": date_gap_days,
        "prospectus": prospectus,
        "periodic_report": periodic_report,
    }


def preflight_summary_documents(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate summary task roles and collect identity/date facts for rule execution."""
    documents = payload["documents"]
    summary = _document_metadata(documents["product_summary"]["filename"], documents["product_summary"]["content"], "product_summary")
    report = _document_metadata(documents["periodic_report"]["filename"], documents["periodic_report"]["content"], "periodic_report")
    status, message = "READY", None
    if not summary.get("is_product_summary"):
        status, message = "WRONG_TARGET_TYPE", "左侧文件未识别为产品资料概要，请检查上传文件。"
    elif report.get("is_product_summary"):
        status, message = "WRONG_REFERENCE_TYPE", "主要依据文件识别为产品资料概要，应上传定期报告。"
    elif report.get("is_prospectus") or not report.get("is_periodic_report"):
        status, message = "WRONG_REFERENCE_TYPE", "主要依据文件未识别为定期报告，请检查上传文件。"
    if "prospectus" in documents:
        prospectus = _document_metadata(documents["prospectus"]["filename"], documents["prospectus"]["content"], "prospectus")
        if status == "READY" and not prospectus.get("is_prospectus"):
            status, message = "WRONG_SUPPLEMENT_TYPE", "补充依据文件未识别为招募说明书，请检查上传文件。"
    else:
        prospectus = None
    return {
        "status": status,
        "blocked": status != "READY",
        "message": message,
        "product_summary": summary,
        "periodic_report": report,
        "prospectus": prospectus,
    }
