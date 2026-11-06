# 模块规则：报告期末按行业分类的股票投资组合

- `rule_id`: `investment.industry_stock_portfolio`
- `version`: 2
- `table_kind`: `detail`（市场分组内重复行业行）
- 公共契约：[`common_公共规则.md`](common_公共规则.md)

## selector

- 标题别名：`报告期末按行业分类的股票投资组合`、`按行业分类的股票投资组合`、`报告期末按行业分类的境内股票投资组合`、`报告期末按行业分类的港股通投资股票投资组合`。
- 必需列角色：`industry_code` 或 `industry_name`、`fair_value`、`ratio_of_net_assets`。
- 表可能有总标题及多个子表；各市场子表分别实例化，不能仅凭同一总标题合并。

## scope

- `fund_id` + `period_end` + `market_scope` + `investment_scope`。
- `market_scope` 候选：`domestic`、`hk_connect`、`other`、`unknown`。必须由子标题、表头或明确上下文确定；不确定时保留 `unknown` 并阻止自动匹配。
- 两侧期末日期、市场范围和投资范围必须一致才允许比较。

## row_policy

- `row_identity = market_scope + industry_code`；代码缺失时可用 `market_scope + normalized_industry_name`，并降低为人工确认候选。
- 行业名用于交叉核验；不能仅靠文本相似度合并不同行业。代码相同但名称冲突时 `UNRESOLVED_ROW_IDENTITY`。
- 行业名称在单元格内自动换行时仍是一条逻辑行，不得拆成多行。应将代码、完整行业名称、公允价值和净值比例归属到同一表格行；PDF 文本流可能把代码或数值插在换行的名称片段之间，不能要求整行名称在页面文本中连续出现。
- 行身份和字段证据优先依据同一表格行的单元格关系核验。行文本引用只需忠实覆盖页面原文，不因列间文本顺序或单元格换行而拒绝已能唯一定位的行与字段。
- 抽取时先确定行业代码与行业名称所属的逻辑行，再按实际表头将该行的公允价值和净值比例分别绑定到对应单元格；不得仅按扁平文本中数字的先后顺序分配字段，也不得把名称、金额、比例混成同一字段。
- 字段 `raw_text` 必须逐字对应其目标单元格。`quote` 优先引用从行业代码/名称到目标单元格的连续行文本，并保留中间单元格内容；不能为缩短引用而跳过金额或比例，也不能拼接不连续片段。若引用遗漏中间内容，但行业行、列标题和目标值仍能由表格单元格唯一对应，按单元格证据验证该值，不把引用瑕疵误判为数值缺失；无法唯一对应时才标记待复核。
- 跨页续表若未重复表头，只有在公共规则确认属于同一逻辑表、且前页列映射能唯一延续时，才可沿用该列语义；续页自身单元格和值仍须单独核对。不能因为续页行序相同就推定列含义相同。
- 抽取全部披露行业行，包括横线行、合计行及跨页续表行。A–S 仅是常见代码范围，不是必需完整清单。
- `total` 是独立汇总行，不作为行业身份。

## row_completeness

| `row_label` | `recovery_policy` |
|---|---|
| `合计` | `recover_from_unique_mapped_source_row_if_model_omits` |

只有当 PyMuPDF 在本章节对应页中找到行标签、且表头映射能唯一确认所有必需字段列时，才可从该源表补回模型漏掉的合计行。存在多个候选或列映射不完整时不得猜测，模块转待复核。

## column_policy / bindings

| `column_key` | 语义 | `value_type` | 单位/分母 |
|---|---|---|---|
| `fair_value` | 公允价值 | money | 元或原文标注单位 |
| `ratio_of_net_assets` | 占基金资产净值比例 | ratio | 百分数；分母为基金资产净值 |

行身份字段：`industry_code`、`industry_name`；表级身份字段：`market_scope`。每个字段保存源单元格坐标和表头证据。

## comparison

1. 以 `market_scope + industry_code` 双向匹配；代码缺失时使用名称精确规范化匹配，无法唯一匹配即人工复核。
2. 逐行比较公允价值和净值比例。行顺序变化不构成差异。
3. 单侧行业行记为 `MISSING_ROW`。一侧境内表、另一侧境内/港股通合并表，或市场范围不明时为 `UNRESOLVED_STRUCTURE`；禁止把跨市场行业金额相加后视作同表一致。
4. 合计行按 `total` 独立核对。总计行计算校验只作辅助，不替代逐行业比较。

## pdf_header_aliases

| column_key | alias_terms |
|---|---|
| `industry_code` | `代码` |
| `industry_name` | `行业类别`、`行业名称` |
| `fair_value` | `公允价值` |
| `ratio_of_net_assets` | `占基金资产净值比例`、`净值比例` |

解析器按此别名定位列，实际表头仍须保留为证据；未唯一识别时进入人工复核。
