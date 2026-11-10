+++
item_id = "custodian"
module_id = "fund_profile"
target_section_path = ["产品概况"]
label = "基金托管人"
value_type = "text"
display_order = 4
excel_row = 4
excel_source_cell = "Sheet1!A5:B5"
search_terms = ["基金托管人：", "基金托管人", "托管人概况", "基金托管人概况"]
section_hints_by_role = { product_summary = ["产品概况", "基金产品概况", "基金概况"], periodic_report = ["基金基本情况", "基金产品概况", "基金简介", "产品说明"], prospectus = ["基金托管人基本情况", "基金托管人概况", "基金托管人"] }
primary_source = { role = "periodic_report", subtypes = ["quarterly", "interim", "annual"] }
fallback_source = { role = "prospectus" }
fallback_when = ["primary_confirmed_absent"]
period_policy = "current_identity"
unit_policy = "preserve_source_unit"
comparison_policy = "semantic_with_identity_and_scope"

+++

核对该基金的基金托管人名称，确认法律主体相同。以适用定期报告为主来源；只有在定期报告已确认未披露该项时，才可用招募说明书补充，并确认资料适用于同一基金。
