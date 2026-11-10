+++
item_id = "fund_managers"
module_id = "fund_profile"
target_section_path = ["产品概况"]
label = "基金经理"
value_type = "text"
display_order = 6
excel_row = 6
excel_source_cell = "Sheet1!A7:B7"
search_terms = ["基金经理", "基金经理简介"]
section_hints = ["基金经理(或基金经理小组)简介"]
section_hints_by_role = { product_summary = ["产品概况", "基金产品概况", "基金概况"], periodic_report = ["基金经理（或基金经理小组）及基金经理助理的简介", "基金经理（或基金经理小组）简介", "基金经理及其助理简介", "基金经理明细", "基金经理简介", "基金管理人及基金经理情况"] }
primary_source = { role = "periodic_report", subtypes = ["quarterly", "interim", "annual"] }
fallback_when = []
period_policy = "current_identity"
unit_policy = "preserve_source_unit"
comparison_policy = "semantic_with_identity_and_scope"
+++

核对产品资料概要列示的基金经理或基金经理小组成员，逐一确认人员身份及其与本基金的关系。优先查阅适用定期报告中基金经理简介等披露；不能仅凭姓名关键词命中确认一致，也应留意报告期间人员变动。

比较两份文件明确披露的本基金经理名单。身份关系及任职状态对应、名单相同且未见离任或变更冲突时，可确认披露内容一致；不因报告截止日早于概要编制日就自动待确认。这一结论不证明报告期后从未发生人员变更。若存在离任、继任、任职区间或名单差异，再结合相应生效时点判断。
