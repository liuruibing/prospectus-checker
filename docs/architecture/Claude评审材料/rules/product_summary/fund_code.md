+++
item_id = "fund_code"
module_id = "fund_profile"
label = "基金代码"
value_type = "code"
display_order = 2
excel_row = 2
excel_source_cell = "Sheet1!A3:B3"
search_terms = ["基金主代码", "基金代码"]
primary_source = { role = "periodic_report", subtypes = ["quarterly", "interim", "annual"] }
fallback_when = []
period_policy = "current_identity"
unit_policy = "preserve_source_unit"
comparison_policy = "role_aware_identifier"
+++

核对产品资料概要披露的基金代码，并区分基金主代码与具体份额类别代码。优先依据适用定期报告披露的代码及其标签逐项核对；主基金身份关联只能说明代码属于同一基金，不能证明不同份额类别代码相等。不得仅因主代码或其他份额代码相同、相近而判定目标代码一致。
