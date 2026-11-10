+++
item_id = "management_fee"
module_id = "fees"
target_section_path = ["投资本基金涉及的费用", "基金运作相关费用"]
label = "管理费率"
value_type = "annual_rate"
display_order = 13
excel_row = 13
excel_source_cell = "Sheet1!A14:B14"
search_terms = ["管理费", "管理费率"]
section_hints_by_role = { product_summary = ["基金运作相关费用", "基金运作费用", "投资本基金涉及的费用", "基金费用", "基金销售相关费用"], periodic_report = ["基金管理费", "管理人报酬", "管理费", "关联方报酬"], prospectus = ["基金管理人的管理费", "基金管理费", "管理费的计提", "与基金运作有关费用的计提方法", "基金费用计提方法", "基金的费用与税收", "基金费用与税收", "基金费用"] }
primary_source = { role = "periodic_report", subtypes = ["quarterly", "interim", "annual"] }
fallback_source = { role = "prospectus" }
fallback_when = ["primary_confirmed_absent", "same_contract_annual_rate"]
period_policy = "applicable_at_summary_compilation"
unit_policy = "annual_rate"
comparison_policy = "normalized_numeric"
note = "核对基金运作相关费用，Excel 的费用分类标注与样本原文不同"
+++

核对基金管理费率，确认费用项目确为管理费、适用对象和计费口径一致，并比较年费率。优先查阅适用定期报告；若其中已确认未披露该项，或满足同一基金合同下年费率适用条件，可用招募说明书补充。不能把其他费用项目、不同口径或不适用时点的数值作为证据。

本项只核对约定的年费率百分比。“固定比例/固定费率”与“按前一日基金资产净值以约定年费率计提”可对应同一费率，计算基数和实际费用金额不作为本项比较值。已选主报告满足日期筛选，同一费用项目和适用对象的百分比相同，且两侧没有变更提示时，可确认披露值一致；不要求报告期末等于概要编制日。存在费率变更说明、适用对象冲突或不同百分比时，须进一步确认适用时间，不能仅凭期间不同判差异。
