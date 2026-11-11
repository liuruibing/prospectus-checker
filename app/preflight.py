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


def _extract_labeled_fund_name(text: str, label: str) -> str | None:
    """Read a labelled fund name from document identity tables."""
    lines = [
        re.sub(r"\s+", "", unicodedata.normalize("NFKC", line))
        for line in text.splitlines()
    ]
    stop_labels = (
        "基金简称", "基金全称", "基金名称", "基金代码", "基金主代码", "基金管理人", "基金托管人", "基金合同生效日",
        "上市交易所", "基金类型", "交易币种", "交易代码", "运作方式", "开放频率",
    )
    for index, line in enumerate(lines):
        marker = line.find(label)
        if marker < 0:
            continue
        value = line[marker + len(label):].lstrip("：:")
        if not value:
            for candidate in lines[index + 1:index + 4]:
                if not candidate:
                    continue
                if any(candidate.startswith(label) for label in stop_labels):
                    break
                value = candidate
                break
        if value:
            for label in stop_labels:
                position = value.find(label)
                if position > 0:
                    value = value[:position]
            value = value.strip("：:，,；;。．")
            if len(value) >= 2 and not value.isdigit() and value not in stop_labels:
                return value
    return None


def _extract_fund_short_name(text: str) -> str | None:
    return _extract_labeled_fund_name(text, "基金简称")


def _extract_fund_full_name(text: str) -> str | None:
    return _extract_labeled_fund_name(text, "基金全称") or _extract_labeled_fund_name(text, "基金名称")


def _extract_product_name_from_filename(filename: str) -> str | None:
    stem = unicodedata.normalize("NFKC", filename.rsplit("/", 1)[-1])
    stem = re.sub(r"(?i)\.pdf$", "", stem)
    stem = re.sub(r"^20\d{2}[-_.年]\d{1,2}(?:[-_.月]\d{1,2}日?)?[_\s-]*", "", stem)
    stem = re.sub(r"^(?:定期报告|招募说明书|产品资料概要|基金产品资料概要)[_\s-]*", "", stem)
    match = re.search(
        r"(?P<name>[\u4e00-\u9fffA-Za-z0-9（）()·]{4,120}?基金)"
        r"(?=20\d{2}年|(?:更新的?|更新)?招募说明书|(?:基金)?产品资料概要|"
        r"年度报告|半年度报告|中期报告|第[1-4]季度报告|[_\-.]|$)",
        stem,
    )
    return match.group("name").strip(" _-.") if match else None


def _extract_fund_code(text: str) -> str | None:
    compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))
    match = re.search(r"(?:基金主代码|基金代码|主代码)[:：]?([0-9]{6})", compact)
    return match.group(1) if match else None


def _normalized_product_name(name: str) -> str:
    """Compare the underlying fund name while ignoring a parenthesized share alias."""
    compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", name))
    compact = re.sub(r"[（(][^（）()]*[）)]", "", compact)
    return compact.replace("基金基金", "基金")


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


def _apply_consistent_batch_fund_names(documents: list[dict[str, Any]]) -> None:
    """Use one name field for each coded fund when every batch document has it."""
    by_code: dict[str, list[dict[str, Any]]] = {}
    for document in documents:
        code = document.get("fund_code")
        if code:
            by_code.setdefault(str(code), []).append(document)

    for group in by_code.values():
        if all(document.get("fund_short_name") for document in group):
            field = "fund_short_name"
        elif all(document.get("fund_full_name") for document in group):
            field = "fund_full_name"
        else:
            field = None
        for document in group:
            document["fund_name"] = (
                document.get(field) if field
                else document.get("fund_full_name") or document.get("fund_short_name") or document.get("fund_name")
            )


def _document_metadata(filename: str, content: bytes, role: str) -> dict[str, Any]:
    document = fitz.open(stream=content, filetype="pdf")
    try:
        first_pages = [document[index].get_text() for index in range(min(3, document.page_count))]
        cover_text = first_pages[0] if first_pages else ""
        first_text = "\n".join(first_pages)
        compact_first_text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", first_text))
        compact_cover_text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", cover_text))
        auto_mode = role == "auto"
        classification_text = compact_first_text if auto_mode else compact_cover_text
        filename_text = unicodedata.normalize("NFKC", filename)
        filename_is_summary = "产品资料概要" in filename_text
        filename_is_prospectus = "招募说明书" in filename_text
        summary_position = classification_text.find("产品资料概要")
        prospectus_position = classification_text.find("招募说明书")
        prospectus_title_precedes_summary = (
            prospectus_position >= 0
            and (summary_position < 0 or prospectus_position < summary_position)
        )
        has_summary_context = summary_position >= 0 and any(
            marker in classification_text for marker in ("编制日期", "送出日期", "更新")
        )
        is_product_summary = filename_is_summary or (
            has_summary_context
            and not filename_is_prospectus
            and not prospectus_title_precedes_summary
        )
        is_periodic_report = bool(re.search(
            r"20\d{2}年(?:第[1-4]季度|半年度|中期|年度)报告", classification_text
        )) and not is_product_summary
        is_prospectus = filename_is_prospectus or (
            "招募说明书" in classification_text
            and not is_product_summary
            and not is_periodic_report
        )
        detected_roles = [
            name for name, detected in (
                ("prospectus", is_prospectus),
                ("periodic_report", is_periodic_report),
                ("product_summary", is_product_summary),
            ) if detected
        ]
        if role == "auto":
            role = detected_roles[0] if len(detected_roles) == 1 else "unknown"
        report_subtype = (
            "annual" if re.search(r"年度报告", compact_cover_text)
            else "interim" if re.search(r"半年度报告|中期报告", compact_cover_text)
            else "quarterly" if re.search(r"季度报告", compact_cover_text)
            else None
        )
        name_text = first_text if auto_mode else cover_text
        full_product_name = (
            _extract_product_name(name_text, role)
            or _extract_fund_full_name(name_text)
            or _extract_product_name_from_filename(filename)
        )
        short_product_name = _extract_fund_short_name(name_text)
        product_name = full_product_name or short_product_name
        # Only document identity pages define the fund code. Later prospectus
        # examples can contain labelled but unrelated ETF market codes (for
        # example, a sample primary-market redemption list).
        code = _extract_fund_code(first_text)
        identity_codes = _extract_fund_codes(first_text)
        if code and code not in identity_codes:
            identity_codes.insert(0, code)
        if role == "prospectus":
            date_interval, date_quote = _matching_date(first_text if auto_mode else cover_text)
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
        elif role == "periodic_report":
            period_text = first_text if auto_mode else cover_text
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
        else:
            date_interval = date_quote = report_send_date_interval = report_send_date_quote = None
            display_date_interval = display_date_quote = None
            date_label = display_date_label = ""
        return {
            "filename": filename,
            "page_count": document.page_count,
            "is_product_summary": is_product_summary,
            "is_prospectus": is_prospectus,
            "is_periodic_report": is_periodic_report,
            "detected_roles": detected_roles,
            "report_subtype": report_subtype,
            "fund_name": product_name,
            "fund_full_name": full_product_name,
            "fund_short_name": short_product_name,
            "fund_code": code,
            "fund_codes": identity_codes,
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
    if prospectus.get("is_product_summary") or prospectus.get("is_periodic_report"):
        status = "WRONG_TARGET_TYPE"
        message = "左侧文件识别为定期报告或产品资料概要，请上传招募说明书。"
    elif periodic_report.get("is_product_summary"):
        status = "WRONG_DOCUMENT_TYPE"
        message = "右侧文件识别为产品资料概要，请切换到“产品资料概要核对”模式后重新提交。"
    elif periodic_report.get("is_prospectus"):
        status = "WRONG_DOCUMENT_TYPE"
        message = "右侧文件识别为招募说明书，请上传定期报告。"
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
        and SequenceMatcher(None, _normalized_product_name(names[0]), _normalized_product_name(names[1])).ratio() < 0.82
    )
    if status == "READY" and (code_mismatch or name_mismatch):
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


def preflight_summary_documents(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate summary task roles, identities and publication timing."""
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
    identities = [("产品资料概要", summary), ("定期报告", report)]
    if "prospectus" in documents:
        prospectus = _document_metadata(documents["prospectus"]["filename"], documents["prospectus"]["content"], "prospectus")
        identities.append(("招募说明书", prospectus))
        if status == "READY" and not prospectus.get("is_prospectus"):
            status, message = "WRONG_SUPPLEMENT_TYPE", "补充依据文件未识别为招募说明书，请检查上传文件。"
    else:
        prospectus = None
    baseline_name = summary.get("fund_name")
    baseline_code = summary.get("fund_code")
    baseline_codes = set(summary.get("fund_codes") or ([baseline_code] if baseline_code else []))
    mismatch = None
    for label, metadata in identities[1:]:
        candidate_codes = set(metadata.get("fund_codes") or ([metadata.get("fund_code")] if metadata.get("fund_code") else []))
        if baseline_codes and candidate_codes and baseline_codes.isdisjoint(candidate_codes):
            mismatch = f"产品身份不一致：产品资料概要代码 {baseline_code}，{label}代码 {metadata['fund_code']}。"
            break
        if (not (baseline_codes and candidate_codes) and baseline_name and metadata.get("fund_name")
                and SequenceMatcher(None, _normalized_product_name(baseline_name), _normalized_product_name(metadata["fund_name"])).ratio() < 0.82):
            mismatch = f"产品身份不一致：产品资料概要与{label}的基金名称不匹配。"
            break
    if status == "READY" and mismatch:
        status, message = "FUND_MISMATCH", mismatch
    if status == "READY" and not all(metadata.get("fund_code") or metadata.get("fund_name") for _, metadata in identities):
        status, message = "IDENTITY_UNCONFIRMED", "至少一份文件的基金身份未能确认，结果需人工复核。"
    summary_date = _read_interval(summary.get("date_interval"))
    report_send_date = _read_interval(report.get("report_send_date_interval"))
    future_report = bool(summary_date and report_send_date and report_send_date[0] > summary_date[1])
    if status == "READY" and future_report:
        status, message = "REFERENCE_NOT_AVAILABLE_AT_CUTOFF", "定期报告送出日晚于概要编制日，不作为该时点的有效依据。"
    return {
        "status": status,
        "blocked": status not in {"READY", "IDENTITY_UNCONFIRMED"},
        "message": message,
        "product_summary": summary,
        "periodic_report": report,
        "prospectus": prospectus,
        "identity_unconfirmed": status == "IDENTITY_UNCONFIRMED",
        "report_not_available_at_compilation": future_report,
    }
