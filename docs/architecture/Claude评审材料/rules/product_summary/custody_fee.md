+++
item_id = "custody_fee"
module_id = "fees"
label = "托管费率"
value_type = "annual_rate"
display_order = 14
excel_row = 14
excel_source_cell = "Sheet1!A15:B15"
search_terms = ["托管费", "托管费率"]
primary_source = { role = "periodic_report", subtypes = ["quarterly", "interim", "annual"] }
fallback_source = { role = "prospectus" }
fallback_when = ["primary_confirmed_absent", "same_contract_annual_rate"]
period_policy = "applicable_at_summary_compilation"
unit_policy = "annual_rate"
comparison_policy = "normalized_numeric"
note = "核对基金运作相关费用，Excel 的费用分类标注与样本原文不同"
+++

核对基金托管费率，确认费用项目确为托管费、适用对象和计费口径一致，并比较年费率。优先查阅适用定期报告；若其中已确认未披露该项，或满足同一基金合同下年费率适用条件，可用招募说明书补充。不能把其他费用项目、不同口径或不适用时点的数值作为证据。
