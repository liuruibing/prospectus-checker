+++
item_id = "fund_short_name"
module_id = "fund_profile"
label = "基金简称"
value_type = "text"
display_order = 1
excel_row = 1
excel_source_cell = "Sheet1!A2:B2"
search_terms = ["基金简称", "基金产品概况"]
primary_source = { role = "periodic_report", subtypes = ["quarterly", "interim", "annual"] }
fallback_when = []
period_policy = "current_identity"
unit_policy = "preserve_source_unit"
comparison_policy = "semantic_with_identity_and_scope"
+++

核对产品资料概要所列基金简称，确认其指向同一只基金。简称可能因表述或标点略有差异，应结合基金身份和上下文判断。优先查阅适用的定期报告（季度报告、中期报告或年度报告）；仅凭关键词命中不足以认定内容一致。
