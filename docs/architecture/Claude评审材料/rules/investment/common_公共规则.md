# 投资组合核对规则：公共契约

- 版本：v1
- 适用模块：资产组合、行业股票组合、股票投资明细。
- 模块细节分别见 `asset_allocation_资产组合情况.md`、`industry_stock_portfolio_行业分类股票投资组合.md`、`stock_holdings_股票投资明细.md`。

## 1. 规则组织约定

每个模块规则遵循同一结构：`rule_id`、`table_kind`、`scope`、`selector`、`row_policy`、`column_policy`、`bindings`、`normalization`、`comparison`、`evidence`、`failure_policy`。规则文档负责定义业务语义；AI 可以提出行、字段、原文值、页码和引用候选，但这些都是待验证声明。解析器必须回到 PDF 原文和表格单元格验证行列归属及原始值，并生成规范化值、物理页码和坐标；AI 不得猜测或生成坐标、单元格索引等定位证据。

### 章节内容归集

- 先依据模块规则中的标题别名定位正文中的章节，再把该章节内跨页的正文、表格、列表、表注和子标题按阅读顺序归集；页码变化不表示章节结束。
- 目录命中只作为定位线索，不能直接当作正文。章节边界优先由文档标题层级及下一同级/上级标题确定；不同模块标题可作为明确边界。
- AnyDoc 的章节结构用于保留表格关系和章节顺序；PyMuPDF 的原文块用于提供逐字引用和物理页定位。章节内容与原文块须保持可追溯关联，不能把跨页内容压成失去行列关系的纯文本。
- 归集完成后，才按对应模块规则识别范围、表实例、行身份和字段。章节归属或证据映射不确定时保留 `UNRESOLVED_STRUCTURE`，不得把不确定内容判为缺失或差异。

- `fixed`：有稳定业务行集合的表；缺省行可选，不因此自动报错。
- `detail`：由重复业务行组成的明细表；必须定义行身份，不以行号/排名作主键。
- `matrix`：具有多层列/行分组的矩阵表；期间区块和业务分组须分别识别。

## 2. 文档范围与报告选择

1. 识别文档角色、基金身份、招书披露日期、报告类型、报告期起止和期末日期、定期报告披露日期。
2. 基金代码相同优先确认基金身份；代码缺失时须由基金全称、合同生效日等独立证据共同确认。名称相近不能单独证明同一基金。
3. 只从招书披露日前已经披露的定期报告中选披露日期最近的一份；晚于招书披露日的报告不得用来校验招书。
4. 这些模块比较的是报告期末快照。两边期末日期不同，结果为 `NOT_COMPARABLE_PERIOD`；日期不明为 `UNRESOLVED_PERIOD`，不能把期间变化解释为披露差异。

## 3. 标准输出与来源

```json
{
  "document_role": "prospectus|periodic_report",
  "fund": {"name": "", "code": "", "contract_effective_date": ""},
  "report": {
    "type": "quarterly|semiannual|annual|unknown",
    "disclosure_date": "YYYY-MM-DD",
    "period_start": "YYYY-MM-DD",
    "period_end": "YYYY-MM-DD"
  },
  "rule_id": "investment.asset_allocation",
  "table_instance": {
    "table_id": "parser-issued-id",
    "table_kind": "fixed|detail|matrix",
    "scope": {},
    "headers": [],
    "rows": [],
    "evidence": []
  },
  "status": "EXTRACTED|UNRESOLVED_STRUCTURE|UNRESOLVED_PERIOD"
}
```

每个值/表格单元格的最终证据至少包含：`raw_text`、`normalized_value`、`unit`、`page`、`bbox`、`table_id`、`row_index`、`column_index`、`source_ref`。页码是 PDF 物理页码（从 1 开始）；坐标系统须与 PDF viewer 一致。LLM 可以给出候选页码、引用和原文值，但它们只是定位线索；最终页码、原文值及 bbox/表格索引必须由解析器/OCR 对照 PDF 原文和单元格确认，规范化值由程序根据已确认的原文值计算，不能采信 LLM 自报的坐标或索引。

跨页表格按页保留原始片段。优先用相邻页重复的表头名称验证续表；续页没有表头时，仅把前页最后一个表格片段与后页第一个无表头片段作为候选，并核对列数、列边界和候选唯一性。列边界归一化容差由下方 `cross_page_table_policy` 配置。条件不满足时保留 `UNRESOLVED_STRUCTURE`，不得仅凭相同行名或相同取值拼接。续页单元格坐标仍须取自续页自身。

## cross_page_table_policy

| setting | value |
|---|---|
| `normalized_column_boundary_tolerance` | `0.015` |

## 4. 公共归一化与比较语义

- 使用十进制精确解析数值；处理千位分隔符、全角数字/标点和已识别单位换算。
- 比例保留原始展示值和规范化百分比值；表头决定比例分母，不能把总资产比例替换成净资产比例。
- `-`、`—`、空白、0、缺失行、未抽取是不同状态。只有规则确认横线代表无持仓/无数据时，规范化为 `NO_HOLDING`，不转成数值 0。
- 默认对 PDF 可见披露值严格比较。舍入容差若以后需要，必须作为显式版本化配置，不允许静默容差。
- LLM 提取结果须校验行列数、表头角色、来源坐标、数值原文与单元格原文一致性；校验失败不进入确定性比较。
- 表格字段以“逻辑行 + 表头定义的列 + 原始单元格”作为验证链。不能只凭扁平文本里数值出现的先后顺序推断字段，也不能把行业名称、金额、比例等不同单元格拼成一个值。
- 引用应尽量覆盖目标逻辑行中从行标识到目标单元格的连续原文，并保留中间单元格内容；不得把页面上不连续的片段拼接成一句引用。若模型引用漏了中间单元格，不能仅据此否定字段值：解析器仍须检查目标原值是否存在于唯一匹配的行列单元格。行、表头、单元格均能唯一确认时可验证；任一关系有歧义时保留 `UNRESOLVED_STRUCTURE`。

## 5. 核对状态

- `MATCH`：期间一致、结构/身份可确认、全部可比字段相同。
- `MISMATCH`：结构和身份均可靠，至少一个字段不同。
- `MISSING_MODULE` / `MISSING_ROW` / `MISSING_FIELD`：一侧明确缺少模块、业务行或字段。
- `NOT_COMPARABLE_PERIOD`：统计期末日期不同。
- `FUND_MISMATCH`：基金身份不匹配。
- `UNRESOLVED_PERIOD` / `UNRESOLVED_STRUCTURE` / `UNRESOLVED_ROW_IDENTITY`：证据不足，转人工复核。

不得将 `UNRESOLVED_*` 判成 `MISMATCH`。每个差异须保留招书与定期报告双方证据，支持 PDF 跳页和框选；UI 状态须同时有文字说明，不能只用颜色。

## 6. PDF 标注约定

- 招书原文使用实线框标记；定期报告证据可由结果详情跳转，并使用不同颜色区分两侧证据。
- 红色：已确认值差异；橙色：仅一侧出现/行列结构待核；灰色：期间不可比或未能判断。
- OCR 低置信、跨页表头/表格续接不确定、字段列分母不明时，保留证据并进入人工复核，不展示成已确认差异。
