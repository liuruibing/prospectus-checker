"""Generic multi-source comparison for rules declared in Markdown plans."""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from app.comparison import compare_declared_field, evaluate_declared_context
from app.domain import normalize_text, quoted_dates
from app.rule_plan import evaluate_reference_document_filters


def _row_value(row: dict[str, Any], key: str) -> str:
    value = row.get(key)
    if value is None and key == "year":
        # Rule-authored plans may call the identity field ``year`` while a
        # normalized interval stores the same fact as ``period_year``.
        value = row.get("period_year")
    elif value is None and key == "period_year":
        value = row.get("year")
    if value is None and key in {"period_start", "start_date"}:
        value = row.get("start_date") if key == "period_start" else row.get("period_start")
    if value is None and key in {"period_end", "end_date"}:
        value = row.get("end_date") if key == "period_end" else row.get("period_end")
    if value is None:
        value = (row.get("fields") or {}).get(key)
    if isinstance(value, dict):
        value = value.get("raw_text")
    return normalize_text(str(value or ""))


def _share_class(row: dict[str, Any]) -> str:
    value = _row_value(row, "share_class")
    match = re.search(r"(?<![a-z])([a-z])(?:类(?:份额)?|份额)?$", value)
    return match[1] if match else value


def _row_year(row: dict[str, Any]) -> str:
    dates = quoted_dates(row.get("label"))
    if len(dates) == 2:
        return dates[1][:4]
    return _row_value(row, "period_year") or _row_value(row, "period_end")[:4]


def _identity(
    row: dict[str, Any], plan: dict[str, Any], *, single_share: bool = False,
) -> tuple[str, ...] | None:
    matching = plan["row_matching"]
    keys = list(dict.fromkeys([*matching["identity_fields"], *matching["required_scope_fields"]]))
    if ({"period_year", "period_start", "period_end"}.issubset(keys)
            or {"year", "start_date", "end_date"}.issubset(keys)):
        # A period tuple is the factual identity. Free-form model row keys
        # can encode the same tuple in different ways across documents.
        keys = [key for key in keys if key != "row_key"]
    has_period_bounds = (
        {"period_start", "period_end"}.issubset(keys)
        or {"start_date", "end_date"}.issubset(keys)
    )
    def cell(key: str) -> str:
        if key == "share_class":
            share = _share_class(row)
            return share or ("__single__" if single_share else "")
        if key in {"label", "row_label"} and has_period_bounds:
            return _row_year(row) or _row_value(row, key)
        return _row_value(row, key)

    values = tuple(cell(key) for key in keys)
    return values if values and all(values) else None


def _is_iso_date(value: Any) -> bool:
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value or "").strip()))


_ONE_YEAR_LABELS = {"过去一年", "过去1年", "最近一年", "最近1年"}


def _is_non_annual_period_label(label: str) -> bool:
    if not label or label in _ONE_YEAR_LABELS:
        return False
    if any(token in label for token in ("合同生效", "成立以来", "至今")):
        return True
    if re.fullmatch(
        r"(?:过去|最近)(?:[一二两三四五六七八九十\d]+(?:个月|年))|\d{1,2}个月|[一二两三四五六七八九十]+(?:个月|年)",
        label,
    ):
        return True
    dates = quoted_dates(label)
    return len(dates) == 2 and (dates[0][:4] != dates[1][:4] or not dates[0].endswith("-01-01") or not dates[1].endswith("-12-31"))


def _annual_reference_row(
    row: dict[str, Any], document: dict[str, Any],
    known_share_starts: dict[str, str], *, single_share: bool = False,
) -> dict[str, Any]:
    """Anchor a disclosed one-year return or calendar-year column to an evidenced period-end and share history."""
    info = document.get("document_info") or {}
    end_proof = (document.get("metadata_evidence") or {}).get("period_end") or {}
    doc_end = str(info.get("period_end") or "")
    label = normalize_text(row.get("label"))
    if _is_non_annual_period_label(label):
        return {**row, "period_year": None, "period_start": None, "period_end": None, "start_date": None, "end_date": None}
    if label in _ONE_YEAR_LABELS:
        if (not re.fullmatch(r"\d{4}-12-31", doc_end)
                or end_proof.get("status") not in {"LOCATED", "QUOTE_ONLY"}
                or doc_end not in quoted_dates(end_proof.get("quote"))
                or label not in normalize_text((row.get("row_evidence") or {}).get("quote"))):
            return row
        year = doc_end[:4]
    elif not (_is_iso_date(row.get("period_start")) and _is_iso_date(row.get("period_end"))):
        year_match = re.fullmatch(r"(\d{4})\s*年?", label)
        year = year_match[1] if year_match else _row_value(row, "period_year")
        if (not re.fullmatch(r"\d{4}", year)
                or not _is_iso_date(doc_end)
                or end_proof.get("status") not in {"LOCATED", "QUOTE_ONLY"}
                or doc_end not in quoted_dates(end_proof.get("quote"))):
            return row
        if int(year) > int(doc_end[:4]) or (int(year) == int(doc_end[:4]) and not doc_end.endswith("-12-31")):
            return row
    else:
        raw_start = str(row.get("period_start") or row.get("start_date") or "").strip()
        raw_end = str(row.get("period_end") or row.get("end_date") or "").strip()
        if (_is_iso_date(raw_start) and _is_iso_date(raw_end)
                and raw_start[:4] == raw_end[:4]
                and raw_start.endswith("-01-01") and raw_end.endswith("-12-31")
                and _is_iso_date(doc_end)
                and end_proof.get("status") in {"LOCATED", "QUOTE_ONLY"}
                and doc_end in quoted_dates(end_proof.get("quote"))
                and int(raw_start[:4]) <= int(doc_end[:4])
                and not (int(raw_start[:4]) == int(doc_end[:4]) and not doc_end.endswith("-12-31"))):
            year = raw_start[:4]
        else:
            return row
    start = f"{year}-01-01"
    end = f"{year}-12-31"
    share_key = _share_class(row) or ("__single__" if single_share else "")
    known_start = known_share_starts.get(share_key)
    if not known_start or known_start > start:
        return row
    return {
        **row,
        "period_year": year,
        "period_start": start,
        "period_end": end,
        "start_date": start,
        "end_date": end,
        "period_evidence": {"reference_end": end_proof, "known_share_start": known_start},
    }


def _located_field_count(row: dict[str, Any], field_specs: list[dict[str, Any]]) -> int:
    fields = row.get("fields") or {}
    return sum(
        1 for spec in field_specs
        if (fields.get(spec["field"]) or {}).get("evidence", {}).get("status") == "LOCATED"
    )


def _merge_same_document_rows(
    rows: list[dict[str, Any]], field_specs: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Merge non-conflicting rows for the same identity from one report, preferring fuller rows."""
    if len(rows) == 1:
        return rows[0]
    ranked = sorted(rows, key=lambda item: _located_field_count(item, field_specs), reverse=True)
    merged_fields = dict(ranked[0].get("fields") or {})
    for other in ranked[1:]:
        other_fields = other.get("fields") or {}
        for spec in field_specs:
            field = spec["field"]
            base_val = merged_fields.get(field)
            other_val = other_fields.get(field)
            base_located = (base_val or {}).get("evidence", {}).get("status") == "LOCATED"
            other_located = (other_val or {}).get("evidence", {}).get("status") == "LOCATED"
            if base_located and other_located:
                if compare_declared_field(spec, base_val, other_val) != "MATCH":
                    return None
            elif other_located and not base_located:
                merged_fields[field] = other_val
    return {**ranked[0], "fields": merged_fields}


def _select_strictly_fuller_source(
    sources: list[dict[str, Any]], field_specs: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Resolve overlap across reports when all located values agree and one source is fuller or primary."""
    if len(sources) <= 1 or any(src.get("conflict") for src in sources):
        return None

    def located_keys(src: dict[str, Any]) -> set[str]:
        fields = src["row"].get("fields") or {}
        return {
            spec["field"] for spec in field_specs
            if (fields.get(spec["field"]) or {}).get("evidence", {}).get("status") == "LOCATED"
        }

    key_sets = [(located_keys(src), src) for src in sources]
    # Every overlapping located field across all candidate sources must agree.
    for index, (left_keys, left_src) in enumerate(key_sets):
        left_fields = left_src["row"].get("fields") or {}
        for right_keys, right_src in key_sets[index + 1:]:
            right_fields = right_src["row"].get("fields") or {}
            shared = left_keys & right_keys
            for spec in field_specs:
                field = spec["field"]
                if field in shared and compare_declared_field(spec, left_fields.get(field), right_fields.get(field)) != "MATCH":
                    return None

    max_count = max(len(keys) for keys, _ in key_sets)
    top = [(keys, src) for keys, src in key_sets if len(keys) == max_count]
    if len(top) == 1:
        best_keys, best_src = top[0]
        if all(keys < best_keys for keys, src in key_sets if src is not best_src):
            return best_src
        return None

    # Equal-coverage agreement (for instance, multi-year 3.1 tables across annual reports):
    # prefer the designated primary periodic_report, or break ties when report periods are distinct.
    top_key_sets = {frozenset(keys) for keys, _ in top}
    if len(top_key_sets) != 1:
        return None
    canonical_keys = top[0][0]
    if not all(keys <= canonical_keys for keys, _ in key_sets):
        return None
    primary = [src for _, src in top if src.get("role") == "periodic_report"]
    if len(primary) == 1:
        return primary[0]
    annual = [src for _, src in top if src.get("report_subtype") == "annual"]
    if len(annual) == 1 and all(src.get("report_subtype") == "quarterly" for _, src in top if src is not annual[0]):
        return annual[0]
    period_ends = [str(src.get("period_end") or "") for _, src in top]
    if all(period_ends) and len(set(period_ends)) == len(period_ends):
        return min((src for _, src in top), key=lambda src: (str(src.get("period_end") or ""), str(src.get("role") or "")))
    return None


def _calendar_years(document: dict[str, Any], count: int) -> range | None:
    end = (document.get("document_info") or {}).get("period_end")
    proof = (document.get("metadata_evidence") or {}).get("period_end") or {}
    if (not isinstance(end, str) or proof.get("status") not in {"LOCATED", "QUOTE_ONLY"}
            or end not in quoted_dates(proof.get("quote"))):
        return None
    anchor = date.fromisoformat(end)
    last_year = anchor.year - ((anchor.month, anchor.day) != (12, 31))
    return range(last_year - count + 1, last_year + 1)


def compare_historical_documents(
    target: dict[str, Any], references: list[dict[str, Any]],
    execution_plans: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Compare target rows to evidence from individually identified documents.

    Matching dimensions, fields and comparison operations are read from the
    activated execution plan. Each reference retains its own document identity
    and evidence, so page numbers from separate PDFs cannot collide.
    """
    modules_by_id = {item.get("rule_id"): item for item in target.get("modules", [])}
    documents: dict[str, Any] = {
        "prospectus": {
            key: value for key, value in target.items()
            if key in {"filename", "page_count", "page_dimensions", "document_info", "metadata_evidence", "preview_id"}
        }
    }
    output_modules = []
    for rule_id, plan in execution_plans.items():
        target_module = modules_by_id.get(rule_id)
        target_rows = (target_module or {}).get("rows", [])
        field_specs = plan["comparison"]["fields"]
        year_window = plan["comparison"].get("calendar_year_window")
        years = _calendar_years(target, year_window) if year_window is not None else None
        rows_by_identity: dict[tuple[str, ...], dict[str, Any]] = {}
        invalid_target_rows = []
        if year_window is not None and years is None:
            invalid_target_rows.append({"review_message": "招募说明书业绩截止日无法核验，不能确定年度范围。"})
        target_single_share = bool(target_rows) and not any(_share_class(row) for row in target_rows)
        known_share_starts: dict[str, str] = {}
        for row in target_rows:
            evidence = row.get("row_evidence") or {}
            dates = quoted_dates(row.get("label"))
            row_year = _row_year(row)
            verified = evidence.get("status") == "LOCATED" and not row.get("schema_warnings")
            share_key = _share_class(row) or ("__single__" if target_single_share else "")
            if verified and len(dates) == 2 and share_key:
                known_share_starts[share_key] = min(known_share_starts.get(share_key, dates[0]), dates[0])
            if year_window is not None and years is None:
                continue
            if years is not None and row_year.isdecimal() and int(row_year) not in years:
                continue
            if not verified:
                invalid_target_rows.append(row)
                continue
            identity = _identity(row, plan, single_share=target_single_share)
            if identity is None:
                # Retain unclassified rows separately from explicit cumulative rows,
                # which have no equivalent annual identity.
                if len(dates) == 2 or not dates:
                    invalid_target_rows.append(row)
                continue
            if identity in rows_by_identity:
                invalid_target_rows.append(row)
                continue
            rows_by_identity[identity] = row

        comparisons: dict[tuple[str, ...], dict[str, Any]] = {
            identity: {
                "identity": list(identity), "target": row,
                "status": "UNRESOLVED", "fields": {}, "sources": [],
                "review_message": "尚未取得可按规则身份和范围对应的参考证据。",
            }
            for identity, row in rows_by_identity.items()
        }
        reference_errors: list[dict[str, Any]] = []
        for reference in references:
            role = str(reference.get("role") or "")
            declared_role = plan["source_selection"]["primary_role"]
            document = reference.get("document") or {}
            modules = {item.get("rule_id"): item for item in document.get("modules", [])}
            reference_module = modules.get(rule_id)
            documents[role] = {
                key: value for key, value in document.items()
                if key in {"filename", "page_count", "page_dimensions", "document_info", "metadata_evidence", "preview_id"}
            }
            context_ok, context_checks = evaluate_declared_context(
                plan, target, document, reference_role=declared_role,
            )
            filters_ok, document_filters = evaluate_reference_document_filters(
                plan, declared_role, document,
            )
            if (not context_ok or not filters_ok or not reference_module
                    or reference_module.get("status") != "EXTRACTED"):
                reference_errors.append({
                    "role": role, "filename": document.get("filename"),
                    "status": "UNRESOLVED", "context_checks": context_checks,
                    "document_filters": document_filters,
                    "message": (reference_module or {}).get("review_message")
                    or "参考文件身份、报告时点或章节结构未满足规则要求。",
                })
                continue
            ref_single_share = target_single_share and not any(_share_class(r) for r in reference_module.get("rows", []))
            doc_rows_by_identity: dict[tuple[str, ...], list[dict[str, Any]]] = {}
            for row in reference_module.get("rows", []):
                if row.get("row_evidence", {}).get("status") != "LOCATED" or row.get("schema_warnings"):
                    continue
                reference_row = _annual_reference_row(
                    row, document, known_share_starts, single_share=ref_single_share,
                )
                identity = _identity(reference_row, plan, single_share=ref_single_share)
                if identity and identity in comparisons:
                    doc_rows_by_identity.setdefault(identity, []).append(reference_row)
            for identity, matched_rows in doc_rows_by_identity.items():
                merged = _merge_same_document_rows(matched_rows, field_specs)
                doc_period_end = (document.get("document_info") or {}).get("period_end")
                doc_subtype = (document.get("document_info") or {}).get("report_subtype")
                if merged is not None:
                    comparisons[identity]["sources"].append({
                        "role": role,
                        "filename": document.get("filename"),
                        "period_end": doc_period_end,
                        "report_subtype": doc_subtype,
                        "row": merged,
                    })
                else:
                    for row in matched_rows:
                        comparisons[identity]["sources"].append({
                            "role": role,
                            "filename": document.get("filename"),
                            "period_end": doc_period_end,
                            "report_subtype": doc_subtype,
                            "row": row,
                            "conflict": True,
                        })

        for comparison in comparisons.values():
            sources = comparison["sources"]
            selected = sources[0] if len(sources) == 1 else _select_strictly_fuller_source(sources, field_specs)
            if selected is None:
                if any(src.get("conflict") for src in sources):
                    comparison["review_message"] = "同一份参考报告内对同一年度和份额存在冲突数据。"
                elif sources:
                    comparison["review_message"] = "同一规则身份对应多份参考证据，无法唯一确定来源。"
                else:
                    comparison["review_message"] = "未找到通过规则范围及身份核验的参考证据。"
                continue
            else:
                comparison["selected_source"] = selected
            ref_row = selected["row"]
            target_row = comparison["target"]
            field_results = {}
            for spec in field_specs:
                field = spec["field"]
                left_value = (target_row.get("fields") or {}).get(field)
                right_value = (ref_row.get("fields") or {}).get(field)
                located = all((value or {}).get("evidence", {}).get("status") == "LOCATED"
                              for value in (left_value, right_value))
                status = compare_declared_field(spec, left_value, right_value) if located else "UNRESOLVED"
                field_results[field] = {
                    "label": spec["label"], "status": status,
                    "target": left_value, "reference": right_value,
                }
            comparison["fields"] = field_results
            statuses = {item["status"] for item in field_results.values()}
            comparison["status"] = (
                "MISMATCH" if "MISMATCH" in statuses
                else "MATCH" if statuses and statuses == {"MATCH"}
                else "UNRESOLVED"
            )
            if comparison["status"] != "UNRESOLVED":
                comparison["review_message"] = None
            else:
                unresolved_labels = [res["label"] for res in field_results.values() if res["status"] == "UNRESOLVED"]
                comparison["review_message"] = (
                    f"{'、'.join(unresolved_labels)}缺少可比较数值或证据，其余已核验字段见明细。"
                    if unresolved_labels and "MATCH" in statuses
                    else "至少一个必需字段缺少可比较数值或证据。"
                )

        statuses = {item["status"] for item in comparisons.values()}
        if invalid_target_rows or reference_errors:
            statuses.add("UNRESOLVED")
        module_status = (
            "MISMATCH" if "MISMATCH" in statuses
            else "UNRESOLVED" if "UNRESOLVED" in statuses or not statuses
            else "MATCH"
        )
        output_modules.append({
            "rule_id": rule_id, "title": plan["display_title"],
            "status": module_status, "rows": list(comparisons.values()),
            "reference_errors": reference_errors,
            "unresolved_target_rows": invalid_target_rows,
        })
    all_statuses = {module["status"] for module in output_modules}
    status = "MISMATCH" if "MISMATCH" in all_statuses else "UNRESOLVED" if "UNRESOLVED" in all_statuses else "MATCH"
    unresolved_rows = (
        sum(row["status"] == "UNRESOLVED" for module in output_modules for row in module["rows"])
        + sum(len(module["unresolved_target_rows"]) for module in output_modules)
    )
    reference_error_count = sum(len(module["reference_errors"]) for module in output_modules)
    return {
        "workflow_id": "historical-performance-verification",
        "status": status,
        "difference_count": sum(row["status"] == "MISMATCH" for module in output_modules for row in module["rows"]),
        "unresolved_count": unresolved_rows + reference_error_count,
        "documents": documents,
        "modules": output_modules,
        "coverage": {
            "target_rows": sum(len(module["rows"]) + len(module["unresolved_target_rows"]) for module in output_modules),
            "matched_rows": sum(row["status"] == "MATCH" for module in output_modules for row in module["rows"]),
            "mismatched_rows": sum(row["status"] == "MISMATCH" for module in output_modules for row in module["rows"]),
            "unresolved_rows": unresolved_rows,
        },
    }


def performance_modules_for_report(
    prospectus: dict[str, Any], report: dict[str, Any] | list[dict[str, Any]], plans: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Render annual facts in the same indicator format as the other checks."""
    references = (
        report if isinstance(report, list)
        else [{"role": "periodic_report", "document": report}]
    )
    comparison = compare_historical_documents(prospectus, references, plans)
    modules = []
    for item in comparison["modules"]:
        checks = []
        right_rows = []
        uncovered_identities: set[tuple[str, str]] = set()
        for row in item["rows"]:
            target = row["target"]
            share = target.get("share_class")
            share_text = "" if not share or share == "__single__" else str(share).strip()
            target_year = _row_year(target)
            t_start = _row_value(target, "period_start")
            t_end = _row_value(target, "period_end")
            is_cumulative = _is_iso_date(t_start) and _is_iso_date(t_end) and t_start[:4] != t_end[:4]
            if not row["sources"] and target_year.isdecimal():
                if not is_cumulative:
                    uncovered_identities.add((target_year, share_text))
                continue
            row_label = f"{target_year} 年" if target_year else target.get("label") or "年度区间待核实"
            if share_text:
                row_label += f" {share_text} 类" if len(share_text) == 1 else f" {share_text}"
            row_id = "|".join(row["identity"])
            selected_source = row.get("selected_source") or (row["sources"][0] if len(row["sources"]) == 1 else None)
            source_meta = (
                {
                    "document_role": selected_source["role"],
                    "document_filename": selected_source.get("filename"),
                }
                if selected_source is not None and selected_source.get("role")
                else {}
            )
            if selected_source is not None:
                right_rows.append(selected_source["row"])
            for field, result in row["fields"].items():
                checks.append({
                    "row_id": row_id, "row_label": row_label, "field_key": field,
                    "field_label": result["label"], "status": result["status"],
                    "prospectus": result["target"], "periodic_report": result["reference"],
                    **source_meta,
                })
            if not row["fields"]:
                checks.append({
                    "row_id": row_id, "row_label": row_label, "field_key": "row", "field_label": "基金业绩",
                    "status": "UNRESOLVED", "message": row["review_message"],
                    "prospectus": {"raw_text": target.get("label"), "evidence": target.get("row_evidence")},
                    "periodic_report": None,
                    **source_meta,
                })
        messages = []
        if item["unresolved_target_rows"]:
            messages.append(item["unresolved_target_rows"][0].get("review_message") or "部分业绩行的原文证据尚未确认。")
        if item["reference_errors"]:
            messages.append(item["reference_errors"][0]["message"])
        if uncovered_identities:
            identities_text = "、".join(
                f"{year} 年 {share if share.endswith('类') else share + ' 类'}" if share else f"{year} 年"
                for year, share in sorted(uncovered_identities)
            )
            messages.append(f"所选报告未找到以下年度和份额的可核验对照数据：{identities_text}。")
        if not messages and not item["rows"]:
            messages.append("未找到可按规则核对的年度业绩数据。")
        modules.append({
            "rule_id": item["rule_id"], "title": item["title"], "status": item["status"],
            "checks": checks, "differences": [entry for entry in checks if entry["status"] != "MATCH"],
            "prospectus_rows": [row["target"] for row in item["rows"]],
            "periodic_rows": right_rows, "review_message": "".join(messages) or None,
        })
    return modules
