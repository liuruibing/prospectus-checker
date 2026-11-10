"""Inspect corpus chapter routing without model calls or page-based lookup rules."""
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.summary_check import RULES, _context_plan, _json_size, _norm, _prescribed_sections, _source_candidates, build_document_source_catalogue


def main():
    output = ROOT/"var/evaluations/20261107-summary-prescribed-chapters/corpus-profile"
    output.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((ROOT/"evaluation/cases/manifest.json").read_text())
    extra = ROOT/"evaluation/chapter_corpus_extra.json"
    if extra.exists():
        manifest["cases"].extend(json.loads(extra.read_text())["cases"])
    observations = []
    for case in manifest["cases"]:
        if not case["case_id"].endswith("SUMMARY_FALLBACK"):
            continue
        for document in case["documents"]:
            role = {"target": "product_summary", "primary_reference": "periodic_report", "fallback_reference": "prospectus"}[document["role"]]
            path = ROOT/document["path"]
            content = path.read_bytes()
            assert hashlib.sha256(content).hexdigest() == document["sha256"]
            catalogue = build_document_source_catalogue(path.name, content, role)
            for rule in RULES:
                if role == "prospectus" and not rule.get("fallback_source"):
                    continue
                candidates = _source_candidates(catalogue, rule)
                plan = _context_plan(catalogue, candidates, item=rule)
                observations.append({
                    "product": case["product"], "code": case["fund_code"], "role": role,
                    "input": document["path"], "item_id": rule["item_id"], "label": rule["label"],
                    "chapters": [{key: section[key] for key in ("title", "path", "pages")}
                                 for section in _prescribed_sections(catalogue, rule)],
                    "label_in_candidates": any(_norm(term).strip("：:") in _norm(source["raw_text"])
                                               for term in rule["terms"] for source in candidates),
                    "sources": len(candidates), "pages": sorted({source["page"] for source in candidates}),
                    "characters": sum(_json_size(batch) for batch in plan["batches"]),
                    "batches": len(plan["batches"]), "skipped_units": plan["skipped_units"],
                })
            print(case["fund_code"], role, catalogue["page_count"], flush=True)
    (output/"chapter-observations.json").write_text(json.dumps(observations, ensure_ascii=False, indent=2))
    products = len({item["code"] for item in observations})
    documents = len({item["input"] for item in observations})
    lines = ["# 概要指标与候选章节映射", "", f"从{products}种产品的{documents}份真实PDF生成，包含用户原始易方达上证50语料。页码只用于复核，不参与规则定位。章节线索按优先级依次尝试，指标标识与章节路径分开保存。此表用于章节索引，是否可作为指定概要的历史来源，另按披露日期筛选。", "",
             "| 指标 | 概要所在章节 | 定期报告候选章节（按优先级） | 允许的招书补充章节 |", "|---|---|---|---|"]
    for rule in RULES:
        hints = rule["section_hints_by_role"]
        lines.append("| "+" | ".join([rule["label"], "/".join(rule["target_section_path"]),
                         "、".join(hints.get("periodic_report", [])),
                         "、".join(hints.get("prospectus", [])) if rule.get("fallback_source") else "未配置补充来源"])+" |")
    lines += ["", "## 语料中实际命中的章节", "", "以下是目录匹配和标签召回观察，不是模型正确率。没有标签命中不等于已确认全文未披露。物理页仅为检查原文的依据。", "",
              "| 产品代码 | 文档角色 | 指标 | 命中章节 | 标签在候选中 |", "|---|---|---|---|---|"]
    for item in observations:
        roots = [section for section in item["chapters"] if not any(
            section["path"].startswith(parent["path"]+" / ") for parent in item["chapters"] if parent is not section)]
        titles = "；".join(dict.fromkeys(section["title"] for section in roots)) or "未命中预设标题，使用有限字段候选"
        lines.append(f"| {item['code']} | {item['role']} | {item['label']} | {titles} | {'是' if item['label_in_candidates'] else '否'} |")
    (ROOT/"docs/research/2026-11-07-概要指标候选章节映射.md").write_text("\n".join(lines)+"\n")


if __name__ == "__main__":
    main()
