+++
item_id = "manager_start_dates"
module_id = "fund_profile"
target_section_path = ["产品概况"]
label = "开始担任本基金经理的日期"
value_type = "date"
display_order = 7
excel_row = 7
excel_source_cell = "Sheet1!A8:B8"
search_terms = ["开始担任本基金", "基金经理的日期", "任职日期"]
section_hints = ["基金经理(或基金经理小组)简介"]
section_hints_by_role = { product_summary = ["产品概况", "基金产品概况", "基金概况"], periodic_report = ["基金经理（或基金经理小组）及基金经理助理的简介", "基金经理（或基金经理小组）简介", "基金经理及其助理简介", "基金经理明细", "基金经理简介", "基金管理人及基金经理情况"] }
primary_source = { role = "periodic_report", subtypes = ["quarterly", "interim", "annual"] }
fallback_when = []
period_policy = "current_identity"
unit_policy = "preserve_source_unit"
comparison_policy = "semantic_with_identity_and_scope"
+++

核对各基金经理开始担任本基金经理的日期，按人员分别对应日期。该日期指担任本基金经理的任职起始日，不等同于加入管理人、担任其他基金经理或报告期起始日。优先查阅适用定期报告相关简介，并结合人员身份判断。

任职起始日是历史事件。两侧明确对应同一人员、同一基金和同一次任职，日期相同即可确认披露内容一致，不要求报告期末等于概要编制日；重新聘任、多次任职或身份不能对应时保留待确认。
