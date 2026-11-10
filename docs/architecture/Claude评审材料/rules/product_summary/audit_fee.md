+++
item_id = "audit_fee"
module_id = "fees"
label = "审计费用"
value_type = "amount"
display_order = 15
excel_row = 15
excel_source_cell = "Sheet1!A16:B16"
search_terms = ["7.4.7.18", "其他费用", "审计费用"]
primary_source = { role = "periodic_report", subtypes = ["annual"] }
fallback_source = { role = "prospectus" }
fallback_when = ["primary_confirmed_absent", "primary_not_applicable", "same_period_same_nature_amount"]
period_policy = "same_period"
unit_policy = "same_currency_amount_and_period"
comparison_policy = "normalized_numeric"
amount_period = "current_period"
+++

核对本期审计费用金额，确认该金额对应审计费用这一费用性质，并与概要所指期间一致。主来源为适用年度报告中的相关费用披露；只有年度报告已确认该项未披露或不适用时，才考虑招募说明书，并要求金额属于同一期间且费用性质相同。单位换算后比较金额，不把其他费用合计或其他期间金额当作同一数值。
