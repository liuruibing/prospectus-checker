+++
item_id = "investment_strategy"
module_id = "investment_details"
target_section_path = ["基金投资与净值表现"]
label = "主要投资策略"
value_type = "text"
display_order = 10
excel_row = 10
excel_source_cell = "Sheet1!A11:B11"
search_terms = ["投资策略", "主要投资策略"]
section_hints_by_role = { product_summary = ["基金投资与净值表现", "基金投资"], periodic_report = ["基金产品说明", "产品说明", "基金产品概况", "基金简介", "基金基本情况", "投资策略和运作分析", "投资策略和业绩表现"] }
primary_source = { role = "periodic_report", subtypes = ["quarterly", "interim", "annual"] }
fallback_when = []
period_policy = "current_identity"
unit_policy = "preserve_source_unit"
comparison_policy = "semantic_with_identity_and_scope"
+++

核对基金主要投资策略，关注资产类别、投资方法、配置方式及重要限制等实质要点。优先查阅适用定期报告，按上下文和完整表述比较，不以单个策略关键词作为唯一证据。

区分实质策略内容与目录性信息。仅列举编号子标题、条款名称或“详见某章节”，但未给出具体方法、比例、条件或限制的提纲，不作为额外待核对指标；不能仅因主报告未重复这些标题，就将已完整对应的实质策略判为待确认。若概要另有具体方法、数值或限制而主报告未覆盖，则仍保留待确认，不把概括披露当作完整覆盖。
