+++
item_id = "investment_objective"
module_id = "investment_details"
target_section_path = ["基金投资与净值表现"]
label = "投资目标"
value_type = "text"
display_order = 9
excel_row = 9
excel_source_cell = "Sheet1!A10:B10"
search_terms = ["投资目标"]
section_hints_by_role = { product_summary = ["基金投资与净值表现", "基金投资"], periodic_report = ["基金产品说明", "产品说明", "基金产品概况", "基金简介", "基金基本情况"] }
primary_source = { role = "periodic_report", subtypes = ["quarterly", "interim", "annual"] }
fallback_when = []
period_policy = "current_identity"
unit_policy = "preserve_source_unit"
comparison_policy = "semantic_with_identity_and_scope"
+++

核对基金投资目标的实质内容，关注目标对象、目标方向及必要的限定条件。优先查阅适用定期报告；允许不改变业务含义的措辞差异，不因出现相同关键词就认定一致。只有来源明确披露互斥目标或相反条件时才判差异；定期报告只给概括表述、未复述概要中的量化限制或附加条件时，不能推定为相反，标记待确认并保留缺少的具体内容。
