+++
item_id = "risk_return"
module_id = "investment_details"
label = "风险收益特征"
value_type = "text"
display_order = 12
excel_row = 12
excel_source_cell = "Sheet1!A13:B13"
search_terms = ["风险收益特征"]
primary_source = { role = "periodic_report", subtypes = ["quarterly", "interim", "annual"] }
fallback_when = []
period_policy = "current_identity"
unit_policy = "preserve_source_unit"
comparison_policy = "semantic_with_identity_and_scope"
+++

核对基金风险收益特征的披露，关注产品类型、风险等级或文字描述及其适用范围。优先查阅适用定期报告，结合上下文比较实质含义；相同标签或关键词本身不足以证明完整描述一致。
