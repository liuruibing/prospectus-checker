# 模块规则：报告期末基金资产组合情况

- `rule_id`: `investment.asset_allocation`
- `version`: 1
- `table_kind`: `fixed`（固定行键 + 可选行；父子结构显式保留）
- 公共契约：[`common_公共规则.md`](common_公共规则.md)

## selector

- 标题别名：`报告期末基金资产组合情况`、`期末基金资产组合情况`、`报告期末基金资产组合`、`期末基金资产组合`。
- 标题编号仅为提示。还须匹配表头/表体，排除相邻的“其他资产构成”等表。
- 必需列角色：`item_name`、`amount`、`ratio_of_total_assets`。列序可变；金额单位和比例分母须从表头/表注核实。

## scope

- `fund_id` + `period_end` + 投资组合报告范围。
- 招书与定期报告的 `period_end` 必须一致，否则只报 `NOT_COMPARABLE_PERIOD`。

## row_policy

按业务行键匹配，不以序号或出现顺序作为身份。原文行名保留，别名归一只在上下文和层级吻合时生效。全部实际行都抽取；下列行均可选，未出现不自动判错。

| `row_key` | 常见原文名称 | `parent_key` | `role` |
|---|---|---|---|
| `equity_investment` | 权益投资 | null | parent |
| `stock_investment` | 其中：股票 | equity_investment | component |
| `fixed_income_investment` | 固定收益投资 | null | parent |
| `bond_investment` | 其中：债券 | fixed_income_investment | component |
| `asset_backed_securities` | 资产支持证券 | fixed_income_investment（若原表显示为子项） | component |
| `precious_metals` | 贵金属投资 | null | item |
| `warrants` | 权证投资 | null | item |
| `financial_derivatives` | 金融衍生品投资 | null | item |
| `resale_financial_assets` | 买入返售金融资产 | null | parent |
| `resale_asset_buyout_repo` | 其中：买断式回购的买入返售金融资产 | resale_financial_assets | component |
| `bank_deposits_and_settlement_reserve` | 银行存款和结算备付金合计 | null | item |
| `other_assets` | 其他资产、其他各项资产 | null | item |
| `total` | 合计 | null | total |

未能归一的披露行仍须保留为 `unmapped_row`，附原文证据，不得丢弃或强塞进最相近的行键。

## column_policy / bindings

| `column_key` | 语义 | `value_type` | 单位/分母 |
|---|---|---|---|
| `amount` | 金额 | money | 原文单位，常见元 |
| `ratio_of_total_assets` | 占基金总资产比例 | ratio | 百分数；分母必须为基金总资产 |
| `display_order` | 序号 | ordinal | 可空；只保留展示顺序，不作为行身份 |

每个 cell 绑定其 `row_key`、`column_key`、原文行/列表头、raw/normalized 值和 source_ref。不得把子项重复计入父项；父子数值是否相同也不得据此合并。
若原表存在序号列，父项记录其数字序号；“其中：股票”“其中：债券”等子项及续行没有序号时记为 `null`。序号列必须保留原文位置，但不得用于跨文档行匹配。

## comparison

1. 双方以 `row_key` 对齐，逐行比较金额与总资产比例；其次才允许以原文行名 + 父子关系匹配。
2. 单侧行产生 `MISSING_ROW`；行存在但字段未识别时为 `UNRESOLVED_STRUCTURE`，不能判字段缺失。
3. 合计行单独核对。行加总关系可作为辅助审计告警，不得用计算结果改写披露值或代替双文档比较。
4. 备注（例如权益投资包含可退替代款估值增值）作为表级 evidence 保存，不作为金额行，也不忽略其可能影响口径的提示。

## pdf_header_aliases

| column_key | alias_terms |
|---|---|
| `amount` | `金额` |
| `ratio_of_total_assets` | `占基金总资产的比例`、`总资产比例` |

解析器按此别名定位列，实际表头仍须保留为证据；未唯一识别时进入人工复核。
