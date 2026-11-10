+++
item_id = "fund_managers"
module_id = "fund_profile"
label = "基金经理"
value_type = "text"
display_order = 6
excel_row = 6
excel_source_cell = "Sheet1!A7:B7"
search_terms = ["基金经理", "基金经理简介"]
section_hints = ["基金经理(或基金经理小组)简介"]
primary_source = { role = "periodic_report", subtypes = ["quarterly", "interim", "annual"] }
fallback_when = []
period_policy = "current_identity"
unit_policy = "preserve_source_unit"
comparison_policy = "semantic_with_identity_and_scope"
+++

核对产品资料概要列示的基金经理或基金经理小组成员，逐一确认人员身份及其与本基金的关系。优先查阅适用定期报告中基金经理简介等披露；不能仅凭姓名关键词命中确认一致，也应留意报告期间人员变动。
