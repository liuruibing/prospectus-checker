+++
item_id = "disclosure_fee"
module_id = "fees"
label = "信息披露费"
value_type = "amount"
display_order = 16
excel_row = 16
excel_source_cell = "Sheet1!A17:B17"
search_terms = ["7.4.7.18", "其他费用", "信息披露费"]
primary_source = { role = "periodic_report", subtypes = ["annual"] }
fallback_source = { role = "prospectus" }
fallback_when = ["primary_confirmed_absent", "primary_not_applicable", "same_period_same_nature_amount"]
period_policy = "same_period"
unit_policy = "same_currency_amount_and_period"
comparison_policy = "normalized_numeric"
amount_period = "current_period"
+++

核对本期信息披露费金额，确认该金额对应信息披露费这一费用性质，并与概要所指期间一致。主来源为适用年度报告中的相关费用披露；只有年度报告已确认该项未披露或不适用时，才考虑招募说明书，并要求金额属于同一期间且费用性质相同。单位换算后比较金额，不把其他费用合计或其他期间金额当作同一数值。
