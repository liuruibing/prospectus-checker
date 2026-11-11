"""Compile manager-authored Chinese rule prose into a generic runtime plan.

The Chinese Markdown is the sole business-rule source. The generated artifact
contains only a validated, non-executable plan for shared runtime capabilities.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.model_json import read_model_json
from app.rule_plan import ExecutionPlanError, validate_execution_plan


PROTOCOL_VERSION = "zh-rule-plan-v3"
PROMPT_VERSION = "rule-interpretation-48"
SUPPORTED_PROMPT_VERSIONS = {
    "rule-interpretation-27", "rule-interpretation-28", "rule-interpretation-29",
    "rule-interpretation-30", "rule-interpretation-31", "rule-interpretation-32",
    "rule-interpretation-33", "rule-interpretation-35", "rule-interpretation-36",
    "rule-interpretation-37", "rule-interpretation-38", "rule-interpretation-39",
    "rule-interpretation-40", "rule-interpretation-41", "rule-interpretation-42", "rule-interpretation-43", "rule-interpretation-44", "rule-interpretation-45", "rule-interpretation-46", "rule-interpretation-47", PROMPT_VERSION,
}
ROOT = Path(__file__).resolve().parents[1]
COMPILED_ROOT = ROOT / "var" / "compiled_rules"
MAX_OUTPUT_CHARS = 80_000


class RuleCompilationError(ValueError):
    """The Chinese rule cannot be compiled into a supported execution plan."""


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_rule_path(scope: str, filename: str) -> Path:
    if scope not in {"investment", "product_summary"} or Path(filename).name != filename:
        raise RuleCompilationError("规则身份无效")
    return ROOT / "rules" / scope / filename


def _compiled_path(scope: str, filename: str, source_hash: str) -> Path:
    if Path(filename).name != filename or not re.fullmatch(r"[0-9a-f]{64}", source_hash):
        raise RuleCompilationError("规则版本标识无效")
    return COMPILED_ROOT / scope / filename / f"{source_hash}.json"


def _public_rule_hash(scope: str, overrides: dict[str, str] | None = None) -> str:
    scope_dir = ROOT / "rules" / scope
    files = sorted(scope_dir.glob("common_*.md")) if scope_dir.exists() else []
    digest = hashlib.sha256()
    digest.update(scope.encode("utf-8"))
    for path in files:
        digest.update(path.name.encode("utf-8"))
        content = (overrides or {}).get(path.name)
        digest.update(content.encode("utf-8") if content is not None else path.read_bytes())
    return digest.hexdigest()


def _is_plain_chinese_rule(content: str) -> bool:
    if content.lstrip().startswith("+++"):
        return False
    headings = {
        line[3:].strip() for line in content.splitlines()
        if line.startswith("## ")
    }
    return (bool(re.search(r"(?m)^#\s+\S", content)) and bool(headings)
            and bool(re.search(r"[\u3400-\u9fff]", content)))


def load_compiled_artifact(path: Path, source_content: str | None = None) -> dict[str, Any] | None:
    """Return the version-checked generated artifact for one exact source."""
    source = source_content if source_content is not None else path.read_text(encoding="utf-8")
    if not _is_plain_chinese_rule(source):
        return None
    try:
        scope = path.parent.name
        artifact = _compiled_path(scope, path.name, _sha(source))
        payload = json.loads(artifact.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RuleCompilationError(f"规则“{path.stem}”尚未生成可用的执行版本，请重新保存并完成规则解释。")
    except (OSError, json.JSONDecodeError) as exc:
        raise RuleCompilationError(f"规则“{path.stem}”的执行版本无法读取，请重新保存规则。") from exc
    if (payload.get("source_sha256") != _sha(source)
            or payload.get("protocol_version") != PROTOCOL_VERSION
            or payload.get("prompt_version") not in SUPPORTED_PROMPT_VERSIONS):
        raise RuleCompilationError(f"规则“{path.stem}”的执行版本已过期，请重新保存规则。")
    if payload.get("public_rules_sha256") != _public_rule_hash(scope):
        raise RuleCompilationError(f"规则“{path.stem}”依赖的公共规则已变更，请重新保存并重新解释规则。")
    return payload


def load_compiled_execution_plan(path: Path, source_content: str | None = None) -> dict[str, Any] | None:
    """Load the generic plan when this rule has been migrated to the new protocol."""
    payload = load_compiled_artifact(path, source_content)
    if payload is None or not isinstance(payload.get("execution_plan"), dict):
        return None
    scope = path.parent.name
    expected_identity = _stable_rule_identity(scope, path.name)
    return _validate_plan(scope, path.name, payload["execution_plan"], expected_identity=expected_identity)


def load_investment_rule_definitions(rule_dir: Path | None = None) -> tuple[dict[str, str], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Load every active investment rule from the rule directory.

    The file set is the registry. Runtime field roles and display labels come
    from the compiled plan for each exact Markdown source hash; this function
    contains no chapter IDs, titles, or field lists.
    """
    directory = rule_dir or ROOT / "rules" / "investment"
    texts: dict[str, str] = {}
    plans: dict[str, dict[str, Any]] = {}
    definitions: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.md")):
        if path.name.startswith("common_"):
            continue
        source = path.read_text(encoding="utf-8")
        identity = _stable_rule_identity("investment", path.name)
        plan = load_compiled_execution_plan(path, source)
        if plan is None:
            raise RuleCompilationError(f"规则“{path.name}”尚未生成有效执行计划，不能用于新任务。")
        if plan["rule_identity"] != identity:
            raise RuleCompilationError(f"规则“{path.name}”的执行计划身份不匹配。")
        fields = {
            item["field"]: item["label"]
            for item in plan["comparison"]["fields"]
        }
        if not fields:
            raise RuleCompilationError(f"规则“{path.name}”没有可比较字段。")
        texts[identity] = source
        plans[identity] = plan
        definitions.append({
            "rule_id": identity,
            "title": plan["display_title"],
            "file": path,
            "fields": fields,
        })
    if not definitions:
        raise RuleCompilationError(f"投资规则目录“{directory}”没有可执行规则。")
    return texts, plans, definitions


def read_rule_source(path: Path) -> str:
    """Read the authoritative manager-authored Markdown source."""
    source = path.read_text(encoding="utf-8")
    if not _is_plain_chinese_rule(source):
        raise RuleCompilationError(f"规则“{path.name}”必须使用中文 Markdown 正文。")
    return source


def capture_rule_set(scope: str) -> dict[str, Any]:
    """Freeze rule source/execution fingerprints for one task."""
    if scope not in {"investment", "product_summary"}:
        raise RuleCompilationError("不支持的规则类别")
    directory = ROOT / "rules" / scope
    files: dict[str, dict[str, str]] = {}
    for path in sorted(directory.glob("*.md")):
        source = path.read_text(encoding="utf-8")
        if path.name.startswith("common_"):
            files[path.name] = {"source_sha256": _sha(source), "execution_plan_sha256": "shared-rule"}
            continue
        artifact = load_compiled_artifact(path, source)
        execution_plan = (artifact or {}).get("execution_plan")
        if not isinstance(execution_plan, dict):
            raise RuleCompilationError(f"规则“{path.name}”没有当前版本的执行计划，不能启动新任务。")
        execution_plan = _validate_plan(
            scope, path.name, execution_plan, expected_identity=_stable_rule_identity(scope, path.name),
        )
        files[path.name] = {
            "source_sha256": _sha(source),
            "execution_plan_sha256": _sha(json.dumps(execution_plan, ensure_ascii=False, sort_keys=True)),
        }
    if not files:
        raise RuleCompilationError(f"规则目录“{scope}”没有可用规则")
    encoded = json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = _sha(f"{scope}\0{PROTOCOL_VERSION}\0{PROMPT_VERSION}\0{encoded}")
    return {
        "scope": scope,
        "rules_version": f"{scope}-{digest[:20]}",
        "protocol_version": PROTOCOL_VERSION,
        "prompt_version": PROMPT_VERSION,
        "files": files,
    }


def _stable_rule_identity(scope: str, filename: str) -> str:
    """Derive system-owned identity from the protected rule filename convention.

    Chinese suffixes are display labels only. The ASCII snake-case prefix is
    the stable key and remains independent from the editable Markdown title.
    """
    if scope not in {"investment", "product_summary"} or Path(filename).name != filename:
        raise RuleCompilationError(f"规则“{filename}”的稳定身份无效。")
    stem = Path(filename).stem
    match = re.match(r"^([a-z][a-z0-9_]*?)(?=_[\u3400-\u9fff]|$)", stem)
    if not match:
        raise RuleCompilationError(f"规则“{filename}”缺少稳定的 ASCII 文件名前缀。")
    key = match.group(1).rstrip("_")
    if not key:
        raise RuleCompilationError(f"规则“{filename}”的稳定身份为空。")
    return f"investment.{key}" if scope == "investment" else key


def _validate_plan(
    scope: str, filename: str, plan: Any, *, expected_identity: str | None = None,
) -> dict[str, Any]:
    if isinstance(plan, dict) and plan.get("rule_scope") in {"current_workflow", "当前工作流"}:
        # Normalize the model's generic phrase for the workflow declared by
        # the request; this does not select chapter or metric behavior.
        plan = {**plan, "rule_scope": scope}
    try:
        validated = validate_execution_plan(
            plan, expected_identity=expected_identity or Path(filename).stem,
        )
    except ExecutionPlanError as exc:
        raise RuleCompilationError(f"生成的通用执行计划未通过校验：{exc}") from exc
    if validated["rule_scope"] != scope:
        raise RuleCompilationError("执行计划的工作流类别与规则文件不一致")
    return validated


async def compile_rule(
    *, scope: str, filename: str, source_content: str,
    provider: Any, common_rules: str = "",
    public_rule_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Interpret Chinese prose as a generic, validated execution plan."""
    if not _is_plain_chinese_rule(source_content):
        raise RuleCompilationError("只有中文分区规则需要执行解释")
    source_path = _safe_rule_path(scope, filename)
    source_hash = _sha(source_content)
    public_hash = _public_rule_hash(scope, public_rule_overrides)
    model_name = getattr(provider, "model_name", type(provider).__name__)
    cached_path = _compiled_path(scope, filename, source_hash)
    if cached_path.is_file():
        try:
            cached = json.loads(cached_path.read_text(encoding="utf-8"))
            if (cached.get("source_sha256") == source_hash
                    and cached.get("public_rules_sha256") == public_hash
                    and cached.get("protocol_version") == PROTOCOL_VERSION
                    and cached.get("prompt_version") == PROMPT_VERSION
                    and cached.get("model") == model_name
                    and isinstance(cached.get("execution_plan"), dict)):
                _validate_plan(
                    scope, filename, cached["execution_plan"],
                    expected_identity=_stable_rule_identity(scope, filename),
                )
                return cached
        except (OSError, json.JSONDecodeError, RuleCompilationError):
            pass

    stable_rule_id = _stable_rule_identity(scope, filename)
    identity = {
        "scope": scope,
        "filename": filename,
        "stable_rule_id": stable_rule_id,
    }
    system = (
        "你是金融披露核对规则解释器。把管理人员维护的中文业务规则解释为受约束的通用执行计划。"
        "中文 Markdown 是唯一业务依据；不得参考旧规则、历史参数或程序惯例补造业务要求。"
        "仅输出 JSON 对象 {\"execution_plan\": {...}}。"
        "禁止输出可执行代码、Python、SQL、文件操作、任意表达式、兼容版 Markdown 或第二份业务规则。"
    )
    user = (
        f"规则身份（仅用于稳定关联）：{json.dumps(identity, ensure_ascii=False)}\n\n"
        "执行器上下文数据契约：投资组合文档的已核验元数据位于 document.document_info；产品资料概要流程的文档元数据位于 document.metadata。概要流程 metadata 可用字段为 fund_name、fund_code、fund_codes、compiled_date、sent_date、period_end、date_interval、date_quote、date_precision、filename_date_interval、filename_date_quote、filename_date_precision、version_date、version_date_interval、version_date_quote、version_date_source、version_date_precision。目标概要编制日读取目标文档 document.metadata.compiled_date；定期报告送出日读取 periodic_report 对应文档的 document.metadata.sent_date；招募说明书版本日期读取 prospectus 对应文档的预检归并字段 document.metadata.version_date；该字段仅在 PDF 内有明确日级日期或文件名日期落入 PDF 日期范围时提供。缺失时不得把月份区间起点当作准确版本日。身份事实读取双方 document.metadata.fund_name / fund_code。上下文检查只比较 document 级事实，不能引用 module 路径；项目指标字段应放在 comparison.fields，不得放进 context_checks。来源角色专属条件必须放在 comparison.context_checks_by_reference_role，不得放入适用于所有来源的 context_checks。\n\n"
        f"管理人员维护的中文规则：\n{source_content}\n\n"
        f"适用的公共规则（若存在，须一并遵守）：\n{common_rules or '无独立公共规则正文'}\n\n"
        "\n请仅根据以上中文规则和适用公共规则生成 execution_plan，严格遵守以下通用结构："
        f"protocol_version=2；rule_identity 必须逐字等于 {stable_rule_id!r}，只能取规则身份对象中的 stable_rule_id，不能使用文件名或展示标题；"
        "rule_scope 为当前工作流；"
        "display_title 为规则中文标题；target_role 和 reference_roles 只使用 prospectus、periodic_report、product_summary；"
        "business_group 是从中文规则归纳出的展示与批处理分类，不参与业务判断；"
        "source_selection 含 primary_role 和 fallbacks；每个备用 role 在 fallbacks 中只能出现一次。备用来源对象只允许 role、when、when_any、candidate_review_when_any 四项。when 是必须同时满足的自动切换条件列表（AND）；when_any 是可任选一组满足的自动切换条件列表（组间 OR，组内 AND）。如果中文规则表达‘满足任一条件即可自动切换’，必须用 when_any 表达，不能重复写同一 role。若候选必须先从备用来源原文核实其身份、合同关系、费率性质、生效日期、目标时点适用性或口径，则使用 candidate_review_when_any 打开审核，不得把同一条件组同时放入 when/when_any；候选审核条件与自动切换条件相同会被拒绝。candidate_review_when_any 只表示主来源状态满足时可以检查候选，不表示主来源未披露，也不直接选用候选；审核后，若中文规则允许且候选证据满足规则声明的身份、时点、适用范围和口径条件，执行器可以选用该候选。不得把‘主来源状态未知，不能声称已确认未披露’解释成‘不得使用通过独立审核的候选’，也不得把该流程提醒重复编译为 review_conditions 阻断候选选择。若中文规则明确允许在主来源状态未知时独立审查备用来源，则此分支只检查备用来源是否满足其自身适用性条件；不得要求先找到主来源证据、确认主来源已披露或确认其未披露。‘主来源未知时不得声称已确认未披露’属于结果表述约束，由执行器保留主来源状态并如实展示，不是备用候选的业务适用性条件，不能生成对应 review_conditions。candidate_review_when_any 只用于中文规则明确允许由备用来源自身证据建立适用性的情形。它只能包含候选检索前已能取得的条件；若中文规则允许以备用来源自身条款证明在目标文件时点仍适用，即使主来源状态为 UNRESOLVED，也必须把 primary.status equals UNRESOLVED 作为独立候选审核组加入 candidate_review_when_any。此条件只打开审核，不表示主来源未披露，也不能单独授权选择备用来源。候选审核条件与自动来源切换条件不得重复；一个备用来源可以使用自动切换条件，或使用候选审核条件，不能重复同一触发组。条件使用通用事实路径 primary.status、primary.coverage、primary.applicability、scope.fund_identity、scope.period、scope.fact_nature、scope.unit、scope.effective_time，通用 operator equals 或 matches_target，以及非空 value。primary.status 可使用 FOUND、CONFIRMED_ABSENT、UNRESOLVED、NOT_APPLICABLE；primary.coverage 可使用 COMPLETE、PARTIAL、NOT_FOUND、UNKNOWN；primary.applicability 可使用 CONFIRMED、NOT_REQUIRED、UNRESOLVED。确认主来源明确未披露时，使用 primary.status equals CONFIRMED_ABSENT；不能用空值或含糊的“已确认”条件。目标侧事实比较必须用 matches_target，不得把 target.scope.* 写成 equals 的文字值。candidate_review_when_any 的触发只打开适用性审核，不视为主来源未披露；不支持的候选适用性应拒绝编译，不能默默改成普通来源切换。没有回退来源时 fallbacks 为空数组。来源顺序和触发条件必须来自中文原文，不能从旧参数自动继承；"
        "search_intent 含 target_description、preferred_sections（字符串数组）、preferred_sections_by_role（按文档角色提供章节语义线索的对象）、include_continuations（布尔值）；"
        "row_matching 只描述逐行表格事实的行身份、层级和逐行范围；它不承载文档来源适用条件、来源先后或目标日期判断。纯段落/单一事实指标没有逐行身份与范围字段时，identity_fields、scope_fields 和其他无关行匹配字段应为空数组；绝不能把 source_selection 条件路径（例如 scope.fund_identity、scope.effective_time）写入 row_matching.scope_fields，这些条件仅能按中文规则放进 source_selection 或 comparison。row_matching 是对象，含 identity_description、identity_fields、identity_prefixes_to_ignore、scope_fields、required_scope_fields、hierarchy_fields、fallback_identity_fields、identity_headers、"
        "identity_equivalence_groups；identity_equivalence_groups 是由中文规则中明确陈述的同义项目/等价行名归纳出的列表，每项含 canonical_identity 与 equivalent_labels。"
        "只记录中文规则明确写出的等价关系，不能依据金额相同、常识猜测或文件名推断；没有明确等价描述时返回空列表。canonical_identity 与 equivalent_labels 代表同一个项目身份，运行时仍须验证唯一性及范围/层级兼容。"
        "target_population、reference_superset_allowed、require_complete_source_for_missing；这些字段必须全部嵌套在 row_matching 内。"
        "表格事实的通用输出结构为 module.scope 与 module.rows；每个 row 使用稳定的 row_key、原文 label、可选 parent_key，"
        "并可包含执行计划声明的身份字段、范围字段、辅助事实和 fields（仅本规则实际比较值）。"
        "identity_fields 及 fallback_identity_fields 必须引用该规则声明且输入结构中实际可提取的身份字段；优先使用稳定 row_key，"
        "其他身份字段必须来自该规则的业务说明及原文证据，不预设特定章节或行业字段。"
        "identity_prefixes_to_ignore 是中文规则明确说明不属于名称身份的行首提示词列表；仅在比较身份时移除，原始行名和引用保持不变；没有明确提示词规则时为空数组。"
        "禁止生成与规则无关的字段。scope_fields 只能引用该规则明确需要区分且可从逐行原文确认的范围字段；required_scope_fields 是其中必须有双方原文证据才能比较的子集，只在中文规则明确说范围必须确认时声明；字段缺失或未知时该行待复核，不能当作双方同范围。"
        "comparison 是另一个独立对象，含 identity_description、scope_conditions（字符串数组）、"
        "context_checks（适用于所有参考文件的上下文检查列表，每项含 label、target_path、reference_path、operation、required）、"
        "context_checks_by_reference_role（可选对象，键必须是已声明的 reference_roles；值为仅适用于该来源角色的同结构上下文检查列表），"
        "context_check_groups（可选，表示一组可替代但不能相互冲突的上下文证据，每组含 label、mode、minimum_matches、checks；mode 使用 all_available_match，checks 中每项含 label、target_path、reference_path、operation）；"
        "凡中文规则明确规定两个可用日期之间的先后关系，都必须编译为对应参考角色的结构化上下文检查，不得只留在 review_conditions。日期条件的方向必须准确：‘参考来源日期不得晚于目标编制日’只能用 reference_before_or_equal，其中 target_path 是目标 compiled_date、reference_path 是来源日期。定期报告来源日期使用 sent_date；招募说明书版本日期使用预检归并的 version_date，不能用 filename_date_quote、date_quote 文本或月份区间起点代替日级版本日期。上述来源时点检查若是来源适用的必要条件，required 必须为 true。review_conditions 只保留结构化检查无法判断的语义条件，例如文件名日期与封面月份是否相容、来源内容明确冲突或条款另定未来生效日；不得重复裁决已由结构化检查覆盖的日期先后关系。可由上述结构化上下文检查直接判断的条件不得重复写入 review_conditions；review_conditions 仅保留需要模型结合业务原文理解、且不能由通用路径比较完整表达的条件，避免同一事实出现两个裁决来源。"
        "operation 使用 decimal、identifier、text、date、semantic、equals、matches_target、before_or_equal 或 reference_before_or_equal；before_or_equal 表示 target_path 的日期不得晚于 reference_path；reference_before_or_equal 表示 reference_path 的日期不得晚于 target_path；日期方向必须与规则原意一致；"
        "matches_target 表示按归一化后的值核验两个声明路径是否相同；来源事实与目标事实必须是同一维度，唯一允许映射为 scope.fund_identity↔target.scope.fund_identity、scope.period↔target.scope.period、scope.fact_nature↔target.scope.fact_nature、scope.unit↔target.scope.unit、scope.effective_time↔target.scope.effective_time，严禁交叉匹配。日期先后运算不得用于身份或其他事实；例如来源须在目标文件编制/披露时点前可用，应使用 reference_before_or_equal，不能用 before_or_equal。数据路径从 document 或 module 开始，例如 document.document_info.period_end、"
        "module.scope 中该规则声明的范围事实；投资组合流程的 document.document_info 仅包含 fund_name、fund_code、disclosure_date、period_end 四个已核验字符串字段；"
        "基金身份、基金代码、披露期间和报告披露日期属于文档级事实，必须从 document.document_info 读取，不得从 module.scope 推断；module.scope 仅用于该模块本身明确披露的适用范围。"
        "row_matching.scope_fields 只能引用逐行输出中实际存在的范围字段，不能把文档级身份或期间放入逐行字段；只声明本规则需要且输入结构确有的上下文，不得发明字段）、"
        "identity_headers 是身份字段到 PDF 表格身份表头语义列表的映射，仅覆盖本规则声明的身份字段；"
        "comparison.fields 是逐项比较值列表，至少一项，每项含 field、label、operation、required、source_headers；只列本规则要求逐行或逐事实比较的实际字段值。明确未持有、无数据、范围标签等披露状态放入相应通用披露/范围结构，不得制造 holding_status、disclosure_status 等虚构字段塞入逐行比较字段；"
        "表格中的合计、小计、净额等汇总行通常仍是具有原文行名和单元格的普通数据行，不得仅因其行名代表汇总就新增 summary/total 类比较字段；只有中文规则明确要求把某个汇总值作为独立事实核对时，才单独声明对应字段。"
        "source_headers 是该字段在 PDF 表格中可能出现的表头原文或语义等价表达数组，不适用表格时为空数组；"
        "operation 只能是 decimal、identifier、text、date、semantic、equals；equals 表示归一化后完全相等；"
        "身份字段不能重复放进 fields）；"
        "evidence_requirements 与 review_conditions 均为字符串数组。review_conditions 只保留不能由 comparison.context_checks 或 comparison.context_check_groups 完整表达的语义条件；若某条件已被同一计划中的结构化上下文检查完整表达，则由该结构化检查作为唯一执行依据，不得再把同一条件重复交给后续模型自由判断。若结构化检查只覆盖条件的一部分，review_conditions 仅保留未覆盖的语义部分。source_selection 已表达的来源路由触发、候选审核分支及其对‘未确认未披露’的限定，不得重复放入 review_conditions 阻断所选来源；‘不得把未解决主来源表述为已确认未披露’属于结果准确性约束，不是来源适用性复核条件，应由保留主来源状态的通用输出处理。review_conditions 应只说明被选来源及目标事实仍须满足的业务条件。不得因去重而丢弃中文规则要求的条件，所有条件仍须在执行计划中可追溯。字段名使用事实结构中的通用字段角色，不能填章节 ID、文件名、Excel 坐标、代码或表达式。"
        "来源条件对象的精确结构为 {\"fact\": \"primary.status\", \"operator\": \"equals\", \"value\": \"CONFIRMED_ABSENT\"}；fact 也可写作 path，系统会规范成 fact。matches_target 的 value 只能是 target.scope.fund_identity 或 target.scope.period。when_any 和 candidate_review_when_any 的精确结构均为 [[条件对象, ...], [条件对象, ...]]。"
        "row_matching.target_population 只能是英文枚举 target_document 或 both_documents，绝不能填业务描述；"
        "target_document 表示只以目标文件披露项目为核对范围，both_documents 表示双方披露项目都纳入核对。"
        "参考来源披露比目标文件多出的项目是否忽略，单独由 reference_superset_allowed 布尔值表达；来源结构完整性由 require_complete_source_for_missing 布尔值表达。"
        "row_matching 必须遵循形状：{\"identity_description\":\"中文说明\",\"identity_fields\":[],\"identity_prefixes_to_ignore\":[],\"scope_fields\":[],\"required_scope_fields\":[],\"hierarchy_fields\":[],\"fallback_identity_fields\":[],\"identity_headers\":{},\"identity_equivalence_groups\":[],\"target_population\":\"target_document\",\"reference_superset_allowed\":false,\"require_complete_source_for_missing\":true}。"
        "公共规则与当前指标规则同等有效。公共规则明确要求的基金身份、报告期间或来源时点条件，必须转成 context_checks 或 context_check_groups 才能执行，不能只写进 scope_conditions。若公共规则允许用多个身份事实交叉确认，必须用一个 context_check_groups 组表达该身份政策，并按 all_available_match 处理共同披露的事实；身份名称或代码不能作为 required=true 的独立 context_check，否则缺少单一身份事实会错误阻断任务。也不能把同一身份字段同时放进身份组和必需检查。若要求来源在目标编制/披露时点前可用，应编码来源日期不晚于目标日期，且选对日期方向。不同日期概念不得互设为相等。context_checks 仅在中文规则明确要求时建立，不得从规则之外发明条件。若中文规则允许以多个身份事实交叉确认，且缺少其中某项不应单独阻断比较，应使用 context_check_groups：mode=all_available_match 表示双方均有值的身份事实必须全部匹配，缺失的单项跳过，但匹配数量必须达到 minimum_matches；任一已提供身份事实冲突仍待复核。此组应来自中文规则，不可自行假设身份替代关系。产品概要编制日、文件披露日、定期报告期末是不同概念，不能互设为必须相等。"
        "生成后自行逐项复核：来源切换每组条件不得重复；不得把搜索失败、引用失败或状态不确定解释为已确认未披露；比较字段必须是规则实际要求核对的披露值，不得加入用于描述‘未持有/无披露/待复核’的虚构字段；通用字段映射不清楚时用 review_conditions 说明缺口，不得猜测。"
        "比较字段、来源条件和上下文检查都只能来自中文规则正文及公共规则，禁止从模型记忆、旧执行参数、文件名、指标 ID 或相邻规则推断。若规则没有声明两侧必须比较的事实，就不能创建相应 context_check。"
        "comparison 必须遵循形状：{\"identity_description\":\"中文说明\",\"scope_conditions\":[],\"context_checks\":[],\"context_check_groups\":[{\"label\":\"身份事实\",\"mode\":\"all_available_match\",\"minimum_matches\":1,\"checks\":[{\"label\":\"基金名称\",\"target_path\":\"document.document_info.fund_name\",\"reference_path\":\"document.document_info.fund_name\",\"operation\":\"text\"}]}],\"fields\":[{\"field\":\"通用事实字段名\",\"label\":\"中文字段名\",\"operation\":\"semantic\",\"required\":true,\"source_headers\":[]}]}。无此需求时 context_check_groups 使用空数组。"
        "JSON 外层结构必须是 {\"execution_plan\": {所有顶层身份字段, \"search_intent\": {...}, \"source_selection\": {...}, \"row_matching\": {前述行匹配字段}, \"comparison\": {前述比较字段}, \"evidence_requirements\": [...], \"review_conditions\": [...]}}。"
        "target_population、reference_superset_allowed、require_complete_source_for_missing 只能放在 row_matching 内；fields 只能放在 comparison.fields 内。"
        "这个执行计划是规则语义到共享执行能力的声明，不得复制旧参数模板来覆盖中文规则；"
        "若某段业务要求无法映射到上述通用能力，按该能力边界拒绝编译，不可增加新的按规则 ID 执行分支。\n"
        "请生成完整内部规则。保留没有被新正文修改的通用证据字段和接口字段；不得凭空增加业务要求。"
    )
    generated = ""
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            retry_note = ("\n上一次生成未通过通用结构校验，错误如下：" + str(last_error)
                          + f"。请仅修正 execution_plan，rule_identity 必须严格为 {stable_rule_id!r}；"
                          + ("校验器发现同一候选条件同时用于自动来源切换和候选适用性审核。请按中文规则判断该条件属于哪一种：自动切换条件仅放在 when/when_any；候选审核条件仅放在 candidate_review_when_any；不得把内容相同的条件组同时放在两处。如果规则未明确要求在主来源未确认时审核备用来源，就删除候选审核条件。"
                             if "候选审核条件不能同时作为自动来源切换条件" in str(last_error) else "")
                          + "不能引入规则 ID 专属逻辑或旧格式兼容内容。只返回 JSON。") if attempt else ""
            raw = await provider.generate_text(system, user + retry_note)
            try:
                parsed, _ = read_model_json(str(raw))
            except (json.JSONDecodeError, ValueError) as exc:
                raw_text = str(raw)
                position = getattr(exc, "pos", 0)
                excerpt = raw_text[max(0, position - 100):position + 180].replace("\n", " ")
                raise RuleCompilationError(
                    f"模型返回的执行规则 JSON 无效：{exc}；错误位置附近：{excerpt!r}。只输出一个完整 JSON 对象，不能附加第二个对象或解释文本。"
                ) from exc
            if set(parsed) != {"execution_plan"}:
                raise RuleCompilationError("模型只能返回系统生成的 execution_plan，不接受旧规则或额外业务配置。")
            generated_execution_plan = parsed.get("execution_plan")
            generated_execution_plan = _validate_plan(
                scope, filename, generated_execution_plan, expected_identity=_stable_rule_identity(scope, filename),
            )
            break
        except RuleCompilationError as exc:
            last_error = exc
            if attempt:
                raise
        except Exception as exc:
            raise RuleCompilationError(f"规则解释失败：{type(exc).__name__}: {exc}") from exc
    else:
        raise RuleCompilationError(f"规则解释未通过校验：{last_error}")

    plan = {
        "protocol_version": PROTOCOL_VERSION,
        "prompt_version": PROMPT_VERSION,
        "rule_identity": identity,
        "source_sha256": source_hash,
        "public_rules_sha256": public_hash,
        "model": model_name,
        "compiled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "execution_plan": generated_execution_plan,
    }
    return plan


def persist_compiled_rule(scope: str, filename: str, plan: dict[str, Any]) -> Path:
    """Atomically persist a generated plan before its source prose is activated."""
    target = _compiled_path(scope, filename, str(plan.get("source_sha256", "")))
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(plan, ensure_ascii=False, indent=2).encode("utf-8")
    temporary = target.with_suffix(".tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(encoded)
            stream.flush()
            import os
            os.fsync(stream.fileno())
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target
