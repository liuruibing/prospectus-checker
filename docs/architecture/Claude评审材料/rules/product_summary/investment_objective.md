+++
item_id = "investment_objective"
module_id = "investment_details"
label = "投资目标"
value_type = "text"
display_order = 9
excel_row = 9
excel_source_cell = "Sheet1!A10:B10"
search_terms = ["投资目标"]
primary_source = { role = "periodic_report", subtypes = ["quarterly", "interim", "annual"] }
fallback_when = []
period_policy = "current_identity"
unit_policy = "preserve_source_unit"
comparison_policy = "semantic_with_identity_and_scope"
+++

核对基金投资目标的实质内容，关注目标对象、目标方向及必要的限定条件。优先查阅适用定期报告；允许不改变业务含义的措辞差异，不因出现相同关键词就认定一致。
