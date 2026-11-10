+++
item_id = "benchmark"
module_id = "investment_details"
label = "业绩比较基准"
value_type = "text"
display_order = 11
excel_row = 11
excel_source_cell = "Sheet1!A12:B12"
search_terms = ["业绩比较基准"]
primary_source = { role = "periodic_report", subtypes = ["quarterly", "interim", "annual"] }
fallback_when = []
period_policy = "current_identity"
unit_policy = "preserve_source_unit"
comparison_policy = "semantic_with_identity_and_scope"
+++

核对业绩比较基准的完整构成、指数或收益率名称、权重及连接关系等要素。优先查阅适用定期报告；不得因仅有部分指数名称或相同关键词而忽略权重、顺序或其他构成差异。若概要说明基准在特定日期变更，应结合该日期与定期报告所描述的报告期判断两侧是否适用于同一时点；报告期早于新基准生效日时，不得仅因旧报告记载与新概要不同就判定披露错误。无法确认变更日期、报告期间或过渡口径时标记待复核；只有确认两侧针对同一适用时点仍不一致，才能判定差异。
