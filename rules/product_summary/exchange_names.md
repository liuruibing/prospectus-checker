+++
item_id = "exchange_names"
module_id = "fund_profile"
target_section_path = ["产品概况"]
label = "场内简称与扩位证券简称"
value_type = "code"
display_order = 8
excel_row = 8
excel_source_cell = "Sheet1!A9:B9"
search_terms = ["场内简称", "扩位证券简称"]
section_hints_by_role = { product_summary = ["产品概况", "基金产品概况", "基金概况"], periodic_report = ["基金基本情况", "基金产品概况", "基金简介", "基金份额上市交易", "其他重大事件"], prospectus = ["基金份额的上市交易"] }
primary_source = { role = "periodic_report", subtypes = ["quarterly", "interim", "annual"] }
fallback_when = []
period_policy = "current_identity"
unit_policy = "preserve_source_unit"
comparison_policy = "composite_exchange_names"
+++

核对基金的场内简称与扩位证券简称。概要中的两个简称合并后，与定期报告“场内简称”字段比较；定期报告可能用顿号、逗号或空格连接两个名称，连接符和字段标签不影响比较。两侧合并后的名称内容相同即判一致。以适用定期报告为主，结合交易场所名称披露的上下文确认；关键词只能辅助定位。
