+++
item_id = "fund_code"
module_id = "fund_profile"
target_section_path = ["产品概况"]
label = "基金代码"
value_type = "code"
display_order = 2
excel_row = 2
excel_source_cell = "Sheet1!A3:B3"
search_terms = ["基金主代码", "基金代码"]
section_hints_by_role = { product_summary = ["产品概况", "基金产品概况", "基金概况"], periodic_report = ["基金基本情况", "基金产品概况", "基金简介", "产品说明"] }
primary_source = { role = "periodic_report", subtypes = ["quarterly", "interim", "annual"] }
fallback_when = []
period_policy = "current_identity"
unit_policy = "preserve_source_unit"
comparison_policy = "role_aware_identifier"
+++

核对产品资料概要实际披露的基金代码，并区分基金主代码、下属份额代码、A/B份额及前端/后端交易代码。以概要实际列出的份额与代码角色为待核对范围，优先依据适用定期报告逐项对应，不要求概要重复报告中与目标无关的其他份额代码。

“基金代码”是通用标签，不因报告使用“基金主代码”或“交易代码”就自动视为不同业务字段。同一基金、同一目标份额的范围已明确，概要仅披露一个通用基金代码，报告的主代码或相应交易代码与其相同、且没有份额或前后端角色冲突时，可以确认该目标代码一致；不额外要求原文标签逐字相同。

主基金身份关联只能说明代码属于同一基金，不能证明不同份额类别代码相等。若概要列出多个代码角色而报告未覆盖全部目标角色，已明确对应的角色可以确认，但整组代码不得判为完全一致；未披露的目标角色标记待确认，只有原文明确给出冲突代码时才判差异。不得以共同的主代码代替未披露的目标份额或后端代码，也不得拿其他份额的相近代码补齐。
