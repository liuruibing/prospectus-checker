import json
import asyncio
from pathlib import Path

import pytest

import app.rule_compiler as compiler
from app.rule_plan import ExecutionPlanError, eligible_fallback_roles, eligible_fallbacks, validate_execution_plan


def _prose() -> str:
    return """# 管理费率

## 核对对象
核对产品资料概要披露的管理费年费率。

## 查找内容
优先查适用定期报告的费用章节。

## 来源与适用范围
定期报告优先，招募说明书只在定期报告确认未披露时补充。

## 比较方法
比较年费率，不比较实际发生金额。
"""


class FakeProvider:
    model_name = "test-luna"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.prompts = []

    async def generate_text(self, system_prompt, user_prompt):
        self.calls += 1
        self.prompts.append((system_prompt, user_prompt))
        response = self.responses.pop(0)
        return response


def _valid_execution_plan() -> dict:
    return {
        "protocol_version": 2,
        "rule_identity": "management_fee",
        "rule_scope": "product_summary",
        "display_title": "管理费率",
        "business_group": "费用",
        "target_role": "product_summary",
        "reference_roles": ["periodic_report", "prospectus"],
        "search_intent": {
            "target_description": "基金管理费年费率",
            "preferred_sections": ["基金费用"],
            "preferred_sections_by_role": {
                "product_summary": ["投资本基金涉及的费用"],
                "periodic_report": ["基金费用"],
                "prospectus": ["基金的费用与税收"],
            },
            "include_continuations": True,
        },
        "source_selection": {
            "primary_role": "periodic_report",
            "fallbacks": [{
                "role": "prospectus",
                "when": [{
                    "fact": "primary.status",
                    "operator": "equals",
                    "value": "CONFIRMED_ABSENT",
                }],
            }],
        },
        "row_matching": {
            "identity_description": "本基金的管理费率",
            "identity_fields": ["fund_id", "fee_role"],
            "scope_fields": ["effective_period"],
            "hierarchy_fields": [],
            "fallback_identity_fields": [],
            "target_population": "both_documents",
            "reference_superset_allowed": False,
            "require_complete_source_for_missing": True,
        },
        "comparison": {
            "identity_description": "按同一基金和适用期间核对年费率",
            "scope_conditions": ["基金身份一致", "费率适用期间一致"],
            "context_checks": [{
                "label": "适用时点",
                "target_path": "document.document_info.effective_on",
                "reference_path": "module.scope.applies_from",
                "operation": "date",
                "required": True,
            }],
            "fields": [{"field": "annual_rate", "label": "年费率", "operation": "decimal", "required": True}],
        },
        "evidence_requirements": ["保留双方原文和 PDF 定位"],
        "review_conditions": ["费率生效时间无法确认时待复核"],
    }


def test_compiler_validates_and_caches_generated_plan(tmp_path, monkeypatch):
    monkeypatch.setattr(compiler, "COMPILED_ROOT", tmp_path / "compiled")
    response = json.dumps({"execution_plan": _valid_execution_plan()}, ensure_ascii=False)
    provider = FakeProvider([response])
    source = _prose()

    plan = asyncio.run(compiler.compile_rule(
        scope="product_summary", filename="management_fee.md", source_content=source,
        provider=provider,
    ))
    compiler.persist_compiled_rule("product_summary", "management_fee.md", plan)
    active_path = Path("rules/product_summary/management_fee.md")
    loaded = compiler.load_compiled_execution_plan(active_path, source)
    assert loaded["rule_identity"] == "management_fee"
    assert loaded["comparison"]["fields"][0]["field"] == "annual_rate"
    assert "execution_markdown" not in plan

    cached = asyncio.run(compiler.compile_rule(
        scope="product_summary", filename="management_fee.md", source_content=source,
        provider=provider,
    ))
    assert cached["execution_plan"]["comparison"]["fields"][0]["field"] == "annual_rate"
    assert provider.calls == 1


def test_compiler_assigns_each_condition_one_authoritative_execution_path(tmp_path, monkeypatch):
    monkeypatch.setattr(compiler, "COMPILED_ROOT", tmp_path / "compiled")
    response = json.dumps({"execution_plan": _valid_execution_plan()}, ensure_ascii=False)
    provider = FakeProvider([response])

    asyncio.run(compiler.compile_rule(
        scope="product_summary", filename="management_fee.md", source_content=_prose(),
        provider=provider,
    ))

    prompt = provider.prompts[0][1]
    assert "review_conditions 只保留不能由 comparison.context_checks 或 comparison.context_check_groups 完整表达的语义条件" in prompt
    assert "context_checks_by_reference_role" in prompt
    assert "不得重复写入 review_conditions" in prompt
    assert "它只能包含候选检索前已能取得的条件" in prompt
    assert "若候选必须先从备用来源原文核实其身份、合同关系、费率性质、生效日期、目标时点适用性或口径，则使用 candidate_review_when_any 打开审核" in prompt
    assert "中文规则明确允许在主来源状态未知时独立审查备用来源" in prompt
    assert "属于结果表述约束" in prompt
    assert compiler.PROMPT_VERSION == "rule-interpretation-48"


def test_compiler_rejects_unloadable_plan_without_activation(tmp_path, monkeypatch):
    monkeypatch.setattr(compiler, "COMPILED_ROOT", tmp_path / "compiled")
    provider = FakeProvider([
        json.dumps({"execution_plan": {"unexpected": "invalid"}}, ensure_ascii=False),
        json.dumps({"legacy_rule": "not allowed", "execution_plan": _valid_execution_plan()}, ensure_ascii=False),
    ])
    with pytest.raises(compiler.RuleCompilationError):
        asyncio.run(compiler.compile_rule(
            scope="product_summary", filename="management_fee.md", source_content=_prose(),
            provider=provider,
        ))

    assert provider.calls == 2
    target = compiler._compiled_path("product_summary", "management_fee.md", compiler._sha(_prose()))
    assert not target.exists()


def test_execution_plan_accepts_new_rule_without_business_python_branch():
    plan = _valid_execution_plan()
    plan["rule_identity"] = "synthetic_distribution_metric"
    plan["display_title"] = "合成分红指标"
    plan["row_matching"]["identity_fields"] = ["fund_id", "share_class"]
    plan["comparison"]["fields"] = [{
        "field": "distribution_amount", "label": "分红金额", "operation": "decimal", "required": True,
    }]

    validated = validate_execution_plan(plan, expected_identity="synthetic_distribution_metric")

    assert validated["rule_identity"] == "synthetic_distribution_metric"
    assert validated["comparison"]["fields"][0]["field"] == "distribution_amount"


def test_compiler_normalizes_generic_current_workflow_label_to_requested_scope():
    for label in ("current_workflow", "当前工作流"):
        plan = _valid_execution_plan()
        plan["rule_scope"] = label
        validated = compiler._validate_plan("product_summary", "management_fee.md", plan)
        assert validated["rule_scope"] == "product_summary"


def test_execution_plan_accepts_generic_exact_context_comparison():
    plan = _valid_execution_plan()
    plan["comparison"]["context_checks"][0]["operation"] = "equals"

    validated = validate_execution_plan(plan)

    assert validated["comparison"]["context_checks"][0]["operation"] == "equals"


def test_execution_plan_accepts_context_checks_scoped_to_declared_reference_roles():
    plan = _valid_execution_plan()
    plan["reference_roles"] = ["periodic_report", "prospectus"]
    plan["comparison"]["context_checks_by_reference_role"] = {
        "prospectus": [{
            "label": "版本日期不晚于目标日期",
            "target_path": "document.metadata.compiled_date",
            "reference_path": "document.metadata.filename_date_quote",
            "operation": "reference_before_or_equal",
            "required": True,
        }]
    }

    validated = validate_execution_plan(plan)

    assert validated["comparison"]["context_checks_by_reference_role"]["prospectus"][0]["required"] is True


def test_execution_plan_removes_global_copies_of_role_scoped_context_checks():
    plan = _valid_execution_plan()
    check = {
        "label": "来源日期",
        "target_path": "document.metadata.compiled_date",
        "reference_path": "document.metadata.version_date",
        "operation": "reference_before_or_equal",
        "required": True,
    }
    plan["comparison"]["context_checks"] = [check]
    plan["comparison"]["context_checks_by_reference_role"] = {
        "prospectus": [check, {**check, "label": "重复的来源日期检查"}],
    }

    validated = validate_execution_plan(plan)

    assert validated["comparison"]["context_checks"] == []
    assert len(validated["comparison"]["context_checks_by_reference_role"]["prospectus"]) == 1


def test_execution_plan_accepts_generic_target_context_match():
    plan = _valid_execution_plan()
    plan["comparison"]["context_checks"][0]["operation"] = "matches_target"

    validated = validate_execution_plan(plan)

    assert validated["comparison"]["context_checks"][0]["operation"] == "matches_target"


def test_execution_plan_accepts_rule_declared_date_order_context():
    plan = _valid_execution_plan()
    plan["comparison"]["context_checks"][0] = {
        "label": "来源时点",
        "target_path": "document.metadata.compiled_date",
        "reference_path": "document.metadata.sent_date",
        "operation": "before_or_equal",
        "required": True,
    }

    validated = validate_execution_plan(plan)

    assert validated["comparison"]["context_checks"][0]["operation"] == "before_or_equal"


def test_execution_plan_accepts_reference_date_order_context():
    plan = _valid_execution_plan()
    plan["comparison"]["context_checks"][0] = {
        "label": "来源时点",
        "target_path": "document.metadata.compiled_date",
        "reference_path": "document.metadata.sent_date",
        "operation": "reference_before_or_equal",
        "required": True,
    }

    validated = validate_execution_plan(plan)

    assert validated["comparison"]["context_checks"][0]["operation"] == "reference_before_or_equal"


def test_execution_plan_keeps_pdf_header_semantics_with_each_compared_field():
    plan = _valid_execution_plan()
    plan["comparison"]["fields"][0]["source_headers"] = ["年费率", "管理费率"]

    validated = validate_execution_plan(plan)

    assert validated["comparison"]["fields"][0]["source_headers"] == ["年费率", "管理费率"]


def test_execution_plan_keeps_identity_header_semantics_without_rule_specific_code():
    plan = _valid_execution_plan()
    plan["row_matching"]["identity_fields"] = ["asset_type"]
    plan["row_matching"]["identity_headers"] = {"asset_type": ["资产类别", "项目"]}

    validated = validate_execution_plan(plan)

    assert validated["row_matching"]["identity_headers"] == {
        "asset_type": ["资产类别", "项目"],
    }

    plan["row_matching"]["identity_headers"] = {"additional_identity": ["名称"]}
    validated = validate_execution_plan(plan)
    assert "additional_identity" in validated["row_matching"]["identity_fields"]


def test_execution_plan_compiles_only_explicit_identity_equivalence_groups():
    plan = _valid_execution_plan()
    plan["row_matching"]["identity_equivalence_groups"] = [{
        "canonical_identity": "项目甲",
        "equivalent_labels": ["项目甲别名"],
    }]

    validated = validate_execution_plan(plan)

    assert validated["row_matching"]["identity_equivalence_groups"] == plan["row_matching"]["identity_equivalence_groups"]
    # Existing compiled plans remain valid and simply declare no explicit name equivalences.
    del plan["row_matching"]["identity_equivalence_groups"]
    assert validate_execution_plan(plan)["row_matching"]["identity_equivalence_groups"] == []


def test_execution_plan_rejects_overlapping_identity_equivalence_groups():
    plan = _valid_execution_plan()
    plan["row_matching"]["identity_equivalence_groups"] = [
        {"canonical_identity": "项目甲", "equivalent_labels": ["项目甲别名"]},
        {"canonical_identity": "项目乙", "equivalent_labels": ["项目甲别名"]},
    ]

    with pytest.raises(ExecutionPlanError, match="重复"):
        validate_execution_plan(plan)


def test_compiler_retries_malformed_model_json(tmp_path, monkeypatch):
    monkeypatch.setattr(compiler, "COMPILED_ROOT", tmp_path / "compiled")
    valid = json.dumps({"execution_plan": _valid_execution_plan()}, ensure_ascii=False)
    provider = FakeProvider(['{"execution_plan": ', valid])

    compiled = asyncio.run(compiler.compile_rule(
        scope="product_summary", filename="management_fee.md", source_content=_prose(),
        provider=provider,
    ))

    assert compiled["execution_plan"]["rule_identity"] == "management_fee"
    assert provider.calls == 2


def test_execution_plan_rejects_per_rule_algorithm_extensions():
    plan = _valid_execution_plan()
    plan["compare_when_rule_id_is_management_fee"] = "special handling"

    with pytest.raises(ExecutionPlanError, match="未支持的参数"):
        validate_execution_plan(plan)


def test_execution_plan_validates_generic_source_fallback_contract():
    plan = _valid_execution_plan()
    validated = validate_execution_plan(plan)

    assert validated["source_selection"]["primary_role"] == "periodic_report"
    assert validated["source_selection"]["fallbacks"][0]["when"] == [{
        "fact": "primary.status", "operator": "equals", "value": "CONFIRMED_ABSENT",
    }]
    assert validated["source_selection"]["fallbacks"][0]["when_any"] == []

    plan["source_selection"]["fallbacks"][0]["when"][0]["fact"] = "audit_fee.same_nature"
    with pytest.raises(ExecutionPlanError, match="不属于通用来源事实"):
        validate_execution_plan(plan)


def test_execution_plan_accepts_alternative_context_evidence_with_conflict_detection_contract():
    plan = _valid_execution_plan()
    plan["comparison"]["context_check_groups"] = [{
        "label": "基金身份",
        "mode": "all_available_match",
        "minimum_matches": 1,
        "checks": [
            {"label": "基金名称", "target_path": "document.document_info.fund_name",
             "reference_path": "document.document_info.fund_name", "operation": "text"},
            {"label": "基金代码", "target_path": "document.document_info.fund_code",
             "reference_path": "document.document_info.fund_code", "operation": "identifier"},
        ],
    }]

    validated = validate_execution_plan(plan)

    group = validated["comparison"]["context_check_groups"][0]
    assert group["minimum_matches"] == 1
    assert len(group["checks"]) == 2

    plan["comparison"]["context_check_groups"][0]["minimum_matches"] = 0
    with pytest.raises(ExecutionPlanError, match="minimum_matches"):
        validate_execution_plan(plan)


def test_execution_plan_requires_required_scope_fields_to_be_declared_dimensions():
    plan = _valid_execution_plan()
    plan["row_matching"]["scope_fields"] = ["share_class"]
    plan["row_matching"]["required_scope_fields"] = ["share_class"]
    assert validate_execution_plan(plan)["row_matching"]["required_scope_fields"] == ["share_class"]

    plan["row_matching"]["required_scope_fields"] = ["market_scope"]
    with pytest.raises(ExecutionPlanError, match="必须属于 scope_fields"):
        validate_execution_plan(plan)


def test_execution_plan_accepts_only_explicit_identity_prefix_normalization():
    plan = _valid_execution_plan()
    plan["row_matching"]["identity_prefixes_to_ignore"] = ["其中"]

    validated = validate_execution_plan(plan)

    assert validated["row_matching"]["identity_prefixes_to_ignore"] == ["其中"]


def test_generic_source_selection_uses_only_declared_evidence_facts():
    plan = validate_execution_plan(_valid_execution_plan())

    assert eligible_fallback_roles(plan, {"primary.status": "CONFIRMED_ABSENT"}) == ["prospectus"]
    assert eligible_fallback_roles(plan, {"primary.status": "UNRESOLVED"}) == []
    assert eligible_fallback_roles(plan, {}) == []


def test_fallback_condition_path_alias_normalizes_to_generic_fact_path():
    plan = _valid_execution_plan()
    plan["source_selection"]["fallbacks"] = [{
        "role": "prospectus",
        "when": [{"path": "primary.status", "operator": "equals", "value": "CONFIRMED_ABSENT"}],
    }]
    validated = validate_execution_plan(plan)
    assert validated["source_selection"]["fallbacks"][0]["when"] == [{
        "fact": "primary.status", "operator": "equals", "value": "CONFIRMED_ABSENT",
    }]
    assert validated["source_selection"]["fallbacks"][0]["when_any"] == []

    plan["source_selection"]["fallbacks"][0]["when"] = [{
        "fact": "scope.period", "operator": "matches_target", "value": "target.scope.period",
    }]
    assert eligible_fallback_roles(plan, {
        "scope.period": "2025-12-31", "target.scope.period": "2025-12-31",
    }) == ["prospectus"]
    assert eligible_fallback_roles(plan, {
        "scope.period": "2025-12-31", "target.scope.period": "2025-06-30",
    }) == []


def test_generic_fallback_can_express_alternative_conditions_without_duplicate_roles():
    plan = _valid_execution_plan()
    plan["source_selection"]["fallbacks"] = [{
        "role": "prospectus",
        "when_any": [
            [{"fact": "primary.status", "operator": "equals", "value": "CONFIRMED_ABSENT"}],
            [{"fact": "primary.applicability", "operator": "equals", "value": "NOT_REQUIRED"}],
        ],
    }]
    normalized = validate_execution_plan(plan)

    assert eligible_fallback_roles(normalized, {"primary.status": "CONFIRMED_ABSENT"}) == ["prospectus"]
    assert eligible_fallback_roles(normalized, {"primary.applicability": "NOT_REQUIRED"}) == ["prospectus"]
    assert eligible_fallback_roles(normalized, {"primary.status": "UNRESOLVED"}) == []


def test_candidate_review_is_distinct_from_automatic_source_switch():
    plan = _valid_execution_plan()
    plan["source_selection"]["fallbacks"] = [{
        "role": "prospectus",
        "when": [{"fact": "primary.status", "operator": "equals", "value": "CONFIRMED_ABSENT"}],
        "candidate_review_when_any": [[
            {"fact": "primary.status", "operator": "equals", "value": "UNRESOLVED"},
        ]],
    }]
    normalized = validate_execution_plan(plan)

    assert eligible_fallbacks(normalized, {"primary.status": "CONFIRMED_ABSENT"}) == [
        {"role": "prospectus", "mode": "select"},
    ]
    assert eligible_fallbacks(normalized, {"primary.status": "UNRESOLVED"}) == [
        {"role": "prospectus", "mode": "candidate_review"},
    ]
    assert eligible_fallbacks(normalized, {}) == []

    plan["source_selection"]["fallbacks"][0]["candidate_review_when_any"] = [[
        {"fact": "chapter.fee", "operator": "equals", "value": "current"},
    ]]
    with pytest.raises(ExecutionPlanError, match="不属于通用来源事实"):
        validate_execution_plan(plan)

    plan = _valid_execution_plan()
    target_condition = {"fact": "scope.fund_identity", "operator": "equals",
                        "value": "target.scope.fund_identity"}
    plan["source_selection"]["fallbacks"][0]["when"] = [target_condition]
    with pytest.raises(ExecutionPlanError, match="matches_target"):
        validate_execution_plan(plan)

    plan["source_selection"]["fallbacks"][0]["when"] = [{
        "fact": "primary.status", "operator": "equals", "value": "UNRESOLVED",
    }]
    plan["source_selection"]["fallbacks"][0]["candidate_review_when_any"] = [[{
        "fact": "primary.status", "operator": "equals", "value": "UNRESOLVED",
    }]]
    with pytest.raises(ExecutionPlanError, match="不能同时作为自动来源切换"):
        validate_execution_plan(plan)


def test_source_condition_deduplicates_repeated_predicates_inside_one_alternative():
    plan = _valid_execution_plan()
    condition = {"fact": "primary.status", "operator": "equals", "value": "CONFIRMED_ABSENT"}
    plan["source_selection"]["fallbacks"][0]["when_any"] = [[condition, dict(condition)]]

    validated = validate_execution_plan(plan)
    assert validated["source_selection"]["fallbacks"][0]["when_any"] == [[condition]]


def test_source_condition_cannot_match_different_scope_dimensions():
    plan = _valid_execution_plan()
    plan["source_selection"]["fallbacks"][0]["when"] = [{
        "fact": "scope.fact_nature",
        "operator": "matches_target",
        "value": "target.scope.period",
    }]

    with pytest.raises(ExecutionPlanError, match="维度不对应"):
        validate_execution_plan(plan)


def test_summary_rule_cannot_require_document_report_period_equality():
    plan = _valid_execution_plan()
    plan["comparison"]["context_checks"] = [{
        "label": "报告期末",
        "target_path": "document.document_info.period_end",
        "reference_path": "document.document_info.period_end",
        "operation": "date",
        "required": True,
    }]

    with pytest.raises(ExecutionPlanError, match="不能把产品资料概要与参考文件的文档报告期末"):
        validate_execution_plan(plan)


def test_repeated_fallback_role_entries_are_normalized_as_generic_alternatives():
    plan = _valid_execution_plan()
    plan["source_selection"]["fallbacks"].append({
        "role": "prospectus",
        "when": [{"fact": "primary.applicability", "operator": "equals", "value": "NOT_REQUIRED"}],
    })
    normalized = validate_execution_plan(plan)

    assert len(normalized["source_selection"]["fallbacks"]) == 1
    assert eligible_fallback_roles(normalized, {"primary.status": "CONFIRMED_ABSENT"}) == ["prospectus"]
    assert eligible_fallback_roles(normalized, {"primary.applicability": "NOT_REQUIRED"}) == ["prospectus"]
    assert eligible_fallback_roles(normalized, {"primary.status": "UNRESOLVED"}) == []


def test_compiled_plan_is_bound_to_source_and_public_rule_versions(tmp_path, monkeypatch):
    monkeypatch.setattr(compiler, "COMPILED_ROOT", tmp_path / "compiled")
    path = Path("rules/product_summary/management_fee.md")
    source = _prose()
    with pytest.raises(compiler.RuleCompilationError, match="尚未生成"):
        compiler.load_compiled_execution_plan(path, source)

    stale = {
        "protocol_version": compiler.PROTOCOL_VERSION,
        "source_sha256": compiler._sha(source),
        "public_rules_sha256": "0" * 64,
        "prompt_version": compiler.PROMPT_VERSION,
        "execution_plan": _valid_execution_plan(),
    }
    compiler.persist_compiled_rule("product_summary", "management_fee.md", stale)
    with pytest.raises(compiler.RuleCompilationError, match="公共规则已变更"):
        compiler.load_compiled_execution_plan(path, source)


def test_manager_authored_rule_is_detected_by_chinese_sections():
    assert compiler._is_plain_chinese_rule(_prose())


def test_rule_source_change_requires_a_matching_plan_before_task_start(tmp_path, monkeypatch):
    rules = tmp_path / "rules" / "investment"
    rules.mkdir(parents=True)
    (rules / "common_公共规则.md").write_text("公共约定 A", encoding="utf-8")
    module = rules / "module.md"
    module.write_text("# 模块\n\n## 核对对象\n核对项目\n", encoding="utf-8")
    monkeypatch.setattr(compiler, "ROOT", tmp_path)
    monkeypatch.setattr(compiler, "COMPILED_ROOT", tmp_path / "compiled")

    plan = _valid_execution_plan()
    plan.update(rule_identity="investment.module", rule_scope="investment")
    plan["target_role"] = "prospectus"
    plan["reference_roles"] = ["periodic_report"]
    plan["search_intent"]["preferred_sections_by_role"] = {
        "prospectus": ["基金的费用与税收"], "periodic_report": ["基金费用"],
    }
    plan["source_selection"] = {"primary_role": "periodic_report", "fallbacks": []}
    source = module.read_text(encoding="utf-8")
    compiler.persist_compiled_rule("investment", "module.md", {
        "protocol_version": compiler.PROTOCOL_VERSION,
        "prompt_version": compiler.PROMPT_VERSION,
        "source_sha256": compiler._sha(source),
        "public_rules_sha256": compiler._public_rule_hash("investment"),
        "execution_plan": plan,
    })
    before = compiler.capture_rule_set("investment")
    module.write_text("# 模块\n\n## 核对对象\n核对项目与适用范围\n", encoding="utf-8")

    with pytest.raises(compiler.RuleCompilationError, match="尚未生成可用的执行版本"):
        compiler.capture_rule_set("investment")
    assert before["files"]["module.md"]["source_sha256"] == compiler._sha(source)
