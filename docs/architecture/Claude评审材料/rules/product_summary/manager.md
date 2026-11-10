+++
item_id = "manager"
module_id = "fund_profile"
label = "基金管理人"
value_type = "text"
display_order = 3
excel_row = 3
excel_source_cell = "Sheet1!A4:B4"
search_terms = ["基金管理人：", "基金管理人", "管理人基本情况", "三、基金管理人"]
primary_source = { role = "periodic_report", subtypes = ["quarterly", "interim", "annual"] }
fallback_source = { role = "prospectus" }
fallback_when = ["primary_confirmed_absent"]
period_policy = "current_identity"
unit_policy = "preserve_source_unit"
comparison_policy = "semantic_with_identity_and_scope"
+++

核对管理该基金的基金管理人名称，关注法律主体是否相同，并结合规范简称、历史名称或上下文识别表述差异。以适用定期报告为主来源；只有在定期报告已确认未披露该项时，才可用招募说明书补充，且补充信息须适用于同一基金及相关时点。
