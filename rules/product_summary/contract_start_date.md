+++
item_id = "contract_start_date"
module_id = "fund_profile"
target_section_path = ["产品概况"]
label = "基金合同生效日"
value_type = "date"
display_order = 5
excel_row = 5
excel_source_cell = "Sheet1!A6:B6"
search_terms = ["基金合同生效日"]
section_hints_by_role = { product_summary = ["产品概况", "基金产品概况", "基金概况"], periodic_report = ["基金基本情况", "基金产品概况", "基金简介", "产品说明"], prospectus = ["基金合同的生效", "基金的募集", "基金的历史沿革"] }
primary_source = { role = "periodic_report", subtypes = ["quarterly", "interim", "annual"] }
fallback_when = []
period_policy = "applicable_at_summary_compilation"
unit_policy = "preserve_source_unit"
comparison_policy = "semantic_with_identity_and_scope"
+++

核对基金合同生效日期。关注日期本身及其所指基金合同，不把报告发布日期或产品资料概要编制日期混作合同生效日。优先查阅适用定期报告；本项关注概要编制时适用的合同信息。
