"""Compile manager-authored Chinese rule prose into a validated runtime plan.

The generated Markdown is an internal compatibility representation. Managers
edit only the source prose; execution consumers resolve the generated version
by the source file hash and fail closed when it is missing or stale.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.model_json import read_model_json


PROTOCOL_VERSION = "zh-rule-compiler-v1"
PROMPT_VERSION = "rule-interpretation-5"
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
    if scope == "product_summary" and not files:
        digest.update(b"product-summary-global-contract-v1")
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
    return len(headings & {
        "核对对象", "查找内容", "来源与适用范围", "比较方法", "特殊披露", "需要复核的情况",
    }) >= 3


def load_compiled_rule(path: Path, source_content: str | None = None) -> str | None:
    """Return the compiled runtime Markdown matching this exact source hash."""
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
    if payload.get("source_sha256") != _sha(source) or payload.get("protocol_version") != PROTOCOL_VERSION:
        raise RuleCompilationError(f"规则“{path.stem}”的执行版本已过期，请重新保存规则。")
    if payload.get("public_rules_sha256") != _public_rule_hash(scope):
        raise RuleCompilationError(f"规则“{path.stem}”依赖的公共规则已变更，请重新保存并重新解释规则。")
    compiled = payload.get("execution_markdown")
    if not isinstance(compiled, str) or not compiled.strip():
        raise RuleCompilationError(f"规则“{path.stem}”的执行版本内容为空，请重新保存规则。")
    return compiled


def effective_rule_text(path: Path) -> str:
    """Resolve manager prose to its compiled runtime representation if present."""
    source = path.read_text(encoding="utf-8")
    if not _is_plain_chinese_rule(source):
        return source
    compiled = load_compiled_rule(path, source)
    if compiled is None:
        raise RuleCompilationError(f"规则“{path.stem}”没有已激活的执行版本。")
    return compiled


def capture_rule_set(scope: str) -> dict[str, Any]:
    """Freeze rule source/execution fingerprints for one task."""
    if scope not in {"investment", "product_summary"}:
        raise RuleCompilationError("不支持的规则类别")
    directory = ROOT / "rules" / scope
    files: dict[str, dict[str, str]] = {}
    for path in sorted(directory.glob("*.md")):
        source = path.read_text(encoding="utf-8")
        execution = effective_rule_text(path)
        files[path.name] = {
            "source_sha256": _sha(source),
            "execution_sha256": _sha(execution),
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


def _validate_execution_markdown(
    scope: str, filename: str, generated: str, *, validate_summary_set: bool = True,
) -> None:
    if len(generated) > MAX_OUTPUT_CHARS:
        raise RuleCompilationError("生成的内部规则超出长度限制，请缩短中文规则后重试。")
    if scope == "product_summary":
        from app.summary_rules import load_markdown_rules

        rule_dir = ROOT / "rules" / scope
        import tempfile

        with tempfile.TemporaryDirectory(prefix="rule-compile-") as temp:
            target = Path(temp)
            for path in (rule_dir.glob("*.md") if validate_summary_set else []):
                if path.name != filename:
                    content = path.read_text(encoding="utf-8")
                    # Existing plain-language rules are resolved to their current
                    # compiled representation so the entire rule set is validated.
                    if _is_plain_chinese_rule(content):
                        content = load_compiled_rule(path, content) or ""
                    (target / path.name).write_text(content, encoding="utf-8")
            (target / filename).write_text(generated, encoding="utf-8")
            try:
                _, rules = load_markdown_rules(target)
            except (ValueError, OSError) as exc:
                prefix = generated[:100].replace("\n", "\\n")
                raise RuleCompilationError(f"生成的执行规则未通过完整性检查：{exc}；返回内容开头={prefix!r}") from exc
            generated_rule = next((rule for rule in rules if rule.get("rule_path") == filename), None)
            if generated_rule is None:
                raise RuleCompilationError("生成的执行规则未保留稳定规则身份。")
    elif scope == "investment":
        required_sections = ("selector", "scope", "row_policy", "comparison", "pdf_header_aliases")
        missing = [section for section in required_sections if not re.search(
            rf"^##\s+{re.escape(section)}\s*$", generated, re.MULTILINE | re.IGNORECASE
        )]
        if missing:
            raise RuleCompilationError("生成的投资规则缺少必要执行区域：" + ", ".join(missing))
        aliases = _markdown_section(generated, "pdf_header_aliases")
        rows = [line for line in aliases.splitlines() if line.startswith("|")]
        if len(rows) < 3:
            raise RuleCompilationError("生成的投资规则没有保留可用的 PDF 表头语义映射。")
    else:
        raise RuleCompilationError("不支持的规则类别")


def _markdown_section(markdown: str, name: str) -> str:
    markdown = _separate_internal_headings(markdown)
    match = re.search(
        rf"^##\s+{re.escape(name)}\s*$([\s\S]*?)(?=^##\s+|\Z)",
        markdown,
        re.MULTILINE | re.IGNORECASE,
    )
    return match.group(1).strip() if match else ""


def _separate_internal_headings(markdown: str) -> str:
    """Repair adjacent internal section headings returned without line breaks."""
    return re.sub(r"(?<!\n)(?=##\s+[A-Za-z_])", "\n\n", markdown)


def _normalize_model_markdown(markdown: str) -> str:
    """Accept JSON-escaped Markdown returned as a string by compatible models."""
    if "\n" not in markdown and "\\n" in markdown:
        markdown = (markdown.replace("\\r\\n", "\n").replace("\\n", "\n")
                    .replace("\\t", "\t").replace(r'\"', '"'))
    return _separate_internal_headings(markdown)


def _preserve_parser_aliases(existing: str, generated: str) -> str:
    """Keep the stable parser vocabulary outside the manager-authored prose.

    Column names in the PDF are a parser compatibility concern. A language
    model may summarize or paraphrase a business rule, but it must not rewrite
    the existing table-header vocabulary used to locate PDF cells.
    """
    existing = _separate_internal_headings(existing)
    generated = _separate_internal_headings(generated)
    section = _markdown_section(existing, "pdf_header_aliases")
    if not section or len([line for line in section.splitlines() if line.startswith("|")]) < 3:
        raise RuleCompilationError("当前投资规则缺少可用的表头映射，无法安全激活中文规则。")
    replacement = re.compile(
        r"^##\s+pdf_header_aliases\s*$[\s\S]*?(?=^##\s+|\Z)",
        re.MULTILINE | re.IGNORECASE,
    )
    preserved = "## pdf_header_aliases\n\n" + section + "\n\n"
    if replacement.search(generated):
        return replacement.sub(lambda _: preserved, generated, count=1)
    return generated.rstrip() + "\n\n" + preserved + "\n"


def _validate_stable_identity(scope: str, filename: str, existing: str, generated: str) -> None:
    if scope == "product_summary":
        def identity_fields(markdown: str) -> tuple[str, str]:
            lines = markdown.splitlines()
            if not lines or lines[0].strip() != "+++":
                raise RuleCompilationError("产品概要执行计划缺少 TOML 身份区。")
            try:
                end = next(i for i in range(1, len(lines)) if lines[i].strip() == "+++")
                metadata = tomllib.loads("\n".join(lines[1:end]))
            except (StopIteration, tomllib.TOMLDecodeError) as exc:
                raise RuleCompilationError("产品概要执行计划的身份区格式无效。") from exc
            return str(metadata.get("item_id", "")), str(metadata.get("module_id", ""))

        before = dict(zip(("item_id", "module_id"), identity_fields(existing)))
        after = dict(zip(("item_id", "module_id"), identity_fields(generated)))
        fields = ("item_id", "module_id")
        if any(before.get(field) != after.get(field) for field in fields):
            raise RuleCompilationError("内部执行计划不能改变稳定规则身份或所属业务分类。")
    elif scope == "investment":
        pattern = r"(?m)^-\s*`rule_id`:\s*`([^`]+)`"
        before = re.search(pattern, existing)
        after = re.search(pattern, generated)
        if not before or not after or before.group(1) != after.group(1):
            raise RuleCompilationError("内部执行计划不能改变投资模块稳定身份。")


async def compile_rule(
    *, scope: str, filename: str, source_content: str, existing_execution: str,
    provider: Any, common_rules: str = "", validate_summary_set: bool = True,
    public_rule_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Interpret Chinese prose as an internal Markdown plan and validate it."""
    if not _is_plain_chinese_rule(source_content):
        raise RuleCompilationError("只有中文分区规则需要执行解释")
    source_path = _safe_rule_path(scope, filename)
    source_hash = _sha(source_content)
    existing_hash = _sha(existing_execution)
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
                    and cached.get("model") == model_name):
                _validate_execution_markdown(
                    scope, filename, str(cached.get("execution_markdown", "")),
                    validate_summary_set=validate_summary_set,
                )
                return cached
        except (OSError, json.JSONDecodeError, RuleCompilationError):
            pass

    identity = {
        "scope": scope,
        "filename": filename,
        "stable_rule_id": source_path.stem,
    }
    system = (
        "你是金融基金披露核对规则编译器。把管理人员提供的中文规则解释为系统内部执行规则。"
        "中文规则正文是唯一业务依据；旧执行规则只用于理解现有程序需要的字段结构和稳定输出身份，"
        "不得覆盖或补造中文正文没有表达的业务要求。若新旧要求冲突，以中文正文为准。"
        "仅输出 JSON：{\"execution_markdown\":\"...\"}。"
        "禁止输出可执行代码、Python、SQL、脚本、文件操作或自由表达式。"
    )
    if scope == "product_summary":
        format_instruction = (
            "execution_markdown 必须是现有产品概要规则可加载的 Markdown，包含 TOML front matter，"
            "并在结束 front matter 的 +++ 后附上非空 Markdown 业务说明正文。"
            "保留稳定 item_id 和 module_id；label 使用中文规则主标题。"
            "primary_source、fallback_source、fallback_when、期间、单位和比较字段只能由中文规则推导；"
            "不能确定时不要猜默认值，应在 meaning 中明确标记“无法编译：需要补充的规则”。"
            "内部字段必须严格使用支持值：value_type 为 amount/date/annual_rate/code/text；period_policy 为 "
            "same_period/current_identity/applicable_at_summary_compilation；unit_policy 为 "
            "same_currency_amount_and_period/annual_rate/preserve_source_unit；comparison_policy 为 "
            "role_aware_identifier/normalized_numeric/semantic_with_identity_and_scope；fallback_when 只能包含 "
            "primary_confirmed_absent/primary_not_applicable/same_contract_annual_rate/same_period_same_nature_amount。"
            "含义相近也必须映射到这些精确值，不能创造新枚举。"
            "保留原 Excel 来源标记仅作追溯元数据，不得据此分组或决定业务逻辑。"
        )
    else:
        format_instruction = (
            "execution_markdown 必须保留现有投资模块内部可读取的 Markdown 结构，包含 selector、scope、"
            "row_policy、column_policy/bindings、comparison、pdf_header_aliases 等区域。"
            "pdf_header_aliases 必须保留并维护每个数据列角色对应的真实表头语义，不能为空或省略。"
            "可将中文规则表达转换为结构化内部描述，"
            "但不允许丢失行身份、父子关系、列语义、合计、明确未持有、证据和跨页完整性要求。"
            "解析器表格内容属于生成结果，不得要求管理人员直接维护。"
        )
    user = (
        f"规则身份（仅用于稳定关联）：{json.dumps(identity, ensure_ascii=False)}\n\n"
        f"管理人员维护的中文规则：\n{source_content}\n\n"
        f"适用的公共规则（若存在，须一并遵守）：\n{common_rules or '无独立公共规则正文'}\n\n"
        f"当前有效的内部兼容结构（只参考程序接口，不得把旧业务判断盖过新中文规则）：\n"
        f"{existing_execution}\n\n{format_instruction}\n"
        "请生成完整内部规则。保留没有被新正文修改的通用证据字段和接口字段；不得凭空增加业务要求。"
    )
    generated = ""
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            retry_note = ""
            if attempt:
                retry_note = (
                    "\n上一次生成未通过系统校验，错误如下：" + str(last_error)
                    + "。请修正格式后重试。产品概要内部规则必须严格使用 +++ 包围的 TOML front matter，"
                    + "不能使用 YAML 的 --- 分隔符；结束 front matter 的 +++ 后必须输出非空 Markdown 正文，"
                    + "正文放业务规则说明，不要省略或塞进 TOML 字段。必须逐项保留当前有效规则中的全部必需元数据字段，"
                    + "包括 value_type、period_policy、unit_policy、comparison_policy 和来源信息；不得省略。只返回要求的 JSON。"
                    if scope == "product_summary" else
                    "\n上一次生成未通过系统校验，错误如下：" + str(last_error)
                    + "。请修正后重试，并使用以下精确的二级标题行（独占一行）："
                    + "## selector、## scope、## row_policy、## comparison、## pdf_header_aliases。"
                    + "必须保留稳定身份和所有既有字段接口。只返回要求的 JSON。"
                )
            raw = await provider.generate_text(system, user + retry_note)
            parsed, _ = read_model_json(str(raw))
            generated = parsed.get("execution_markdown")
            if not isinstance(generated, str) or not generated.strip():
                raise RuleCompilationError("模型未返回完整的内部执行规则。")
            if generated.lstrip().startswith("```markdown"):
                generated = re.sub(r"^```markdown\s*|\s*```$", "", generated.strip(), flags=re.IGNORECASE)
            generated = _normalize_model_markdown(generated)
            if scope == "investment":
                generated = _preserve_parser_aliases(existing_execution, generated)
            _validate_execution_markdown(
                scope, filename, generated, validate_summary_set=validate_summary_set,
            )
            _validate_stable_identity(scope, filename, existing_execution, generated)
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
        "base_sha256": existing_hash,
        "public_rules_sha256": public_hash,
        "model": model_name,
        "compiled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "execution_markdown": generated,
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
