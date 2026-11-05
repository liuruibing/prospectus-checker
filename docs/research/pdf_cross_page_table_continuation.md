# PDF 跨页续表识别：业内做法与本项目建议

调研日期：2026-11-05

## 结论

PDF 解析器通常先在单页内识别表格。跨页表格需要文档级的续表关联和后处理；仅更换解析器不能保证自动合并。推荐保留每页的原始单元格和坐标，再关联相邻页表格片段，并在证据不足时继续要求人工复核。

## 业内做法

- Microsoft Azure Document Intelligence 明确建议：遇到跨多页的表格，可按页拆分 PDF 分析，再后处理为一张表。这说明跨页合并是解析结果之后的结构恢复步骤，而非默认由页内表格识别完成。来源：[Azure Layout 文档](https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/prebuilt/layout?view=doc-intel-4.0.0)
- PyMuPDF 官方示例专门演示把跨页表格片段连接起来；`Page.find_tables()` 提供页内表格与单元格信息，跨页拼接由应用层完成。来源：[PyMuPDF join_tables 示例](https://github.com/pymupdf/PyMuPDF-Utilities/blob/master/table-analysis/join_tables.ipynb)、[PyMuPDF 表格 API](https://pymupdf.readthedocs.io/en/latest/page.html)
- Google Document AI 文档也明确提示，PDF 中跨页表格可能被拆成两个表。更换解析服务会改善识别，但不能假设它消除了跨页结构问题。来源：[Google Document AI PDF 支持限制](https://docs.cloud.google.com/document-ai/docs/layout-parse-chunk)
- 研究和数据集工作也强调表格要保留行、列、单元格及表头结构，并通过一致化与质量控制减少结构歧义。来源：[PubTables-1M 论文](https://openaccess.thecvf.com/content/CVPR2022/papers/Smock_PubTables-1M_Towards_Comprehensive_Table_Extraction_From_Unstructured_Documents_CVPR_2022_paper.pdf)

## 本项目现状

- `app/extraction.py` 使用 AnyDoc 的 Markdown 辅助章节定位，再用 PyMuPDF `Page.find_tables()` 获取逐页表格矩阵和单元格坐标。
- `_header_column_maps()` 当前只从同页表头建立字段到列的映射；没有跨页续表识别和表头映射继承。
- 因此，当下一页接着上页的表格、又没有重复表头时，续页的数据文字可能存在，但无法可靠绑定到“金额”“占比”等字段。比较层据此保留待复核是合理的安全结果。
- 本例的核心不是两个 `-` 的规范化，而是先证明它们来自同一逻辑表格的同一字段列。相邻页另有其他表格，单纯按列数继承会误连。

## 建议的通用处理

1. **保留页级片段。** 每个表格片段保留物理页码、表格边界、行/列边界、单元格文本和坐标、已识别表头、章节上下文。不要先把整页 Markdown 当作唯一结构事实。
2. **在相邻页之间识别续表候选。** 优先对比重复表头。续页缺少表头时，保守地只比较前页最后一个片段与后页第一个片段，再核对列数、归一化列边界和章节/行上下文。
3. **只在候选唯一且结构证据足够时继承 schema。** 继承的是已验证的列语义映射，不是伪造下一页的表头或坐标。若列线不匹配、章节改变或行归属不清，停止自动继承并保留待复核。列边界容差作为规则配置维护，不用分散的代码常数。
4. **分开记录证据状态。** 行身份、列归属、值文本和坐标分别保存来源页及置信状态。跨页关联成功后，仍以续页自身的单元格坐标作为差异定位证据。
5. **比较前先确认结构。** 只有行身份和字段列均已确认后，才比较 `-`、数字等值。相同横线不能单独证明两个字段相同；列映射不确定时也不应显示成数据差异。

续表判断应采用多项结构证据和唯一候选约束，不按具体证券名称、行号或横线值写分支。模块规则可以表达“本模块表格可能跨页、续页可能不重复表头、需要保留父子行和字段列语义”；通用解析层负责依据坐标与结构完成片段关联。

## 对本例的判断

定期报告第 8 页有表头，第 9 页顶部继续出现同列数但无表头的片段；解析器把页 9 表格视为独立页内表格，因此没有“金额”和“占比”的列映射。该模式与业内文档描述的跨页表格拆分一致。当前实现参考 PyMuPDF 的“逐页抽片段、用结构校验后连接”思路，并针对金融报告没有重复表头的情况，限制候选为前页最后一个表格到后页第一个无表头表格，再验证列边界和唯一性。

## 参考

- [Microsoft Azure Document Intelligence：Layout](https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/prebuilt/layout?view=doc-intel-4.0.0)
- [PyMuPDF：Joining tables across pages](https://github.com/pymupdf/PyMuPDF-Utilities/blob/master/table-analysis/join_tables.ipynb)
- [PyMuPDF：Page.find_tables API](https://pymupdf.readthedocs.io/en/latest/page.html)
- [Google Document AI：Layout parser limitations](https://docs.cloud.google.com/document-ai/docs/layout-parse-chunk)
- [Smock et al., PubTables-1M, CVPR 2022](https://openaccess.thecvf.com/content/CVPR2022/papers/Smock_PubTables-1M_Towards_Comprehensive_Table_Extraction_From_Unstructured_Documents_CVPR_2022_paper.pdf)
