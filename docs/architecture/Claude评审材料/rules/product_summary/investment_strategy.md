+++
item_id = "investment_strategy"
module_id = "investment_details"
label = "主要投资策略"
value_type = "text"
display_order = 10
excel_row = 10
excel_source_cell = "Sheet1!A11:B11"
search_terms = ["投资策略", "主要投资策略"]
primary_source = { role = "periodic_report", subtypes = ["quarterly", "interim", "annual"] }
fallback_when = []
period_policy = "current_identity"
unit_policy = "preserve_source_unit"
comparison_policy = "semantic_with_identity_and_scope"
+++

核对基金主要投资策略，关注资产类别、投资方法、配置方式及重要限制等实质要点。优先查阅适用定期报告，按上下文和完整表述比较，不以单个策略关键词作为唯一证据。
