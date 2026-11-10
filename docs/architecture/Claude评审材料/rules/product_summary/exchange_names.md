+++
item_id = "exchange_names"
module_id = "fund_profile"
label = "场内简称与扩位证券简称"
value_type = "code"
display_order = 8
excel_row = 8
excel_source_cell = "Sheet1!A9:B9"
search_terms = ["场内简称", "扩位证券简称"]
primary_source = { role = "periodic_report", subtypes = ["quarterly", "interim", "annual"] }
fallback_when = []
period_policy = "current_identity"
unit_policy = "preserve_source_unit"
comparison_policy = "semantic_with_identity_and_scope"
+++

核对基金的场内简称与扩位证券简称，区分两个名称各自对应的字段，不将其视为可互换的同一标签。以适用定期报告为主，结合交易场所名称披露的上下文确认；关键词只能辅助定位。
