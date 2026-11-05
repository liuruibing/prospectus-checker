# 基金报告章节与表格解析引擎评估

调研日期：2026-11-05

## 当前工作流的业内对标

### 判断

当前方案是**合理的 MVP 分层流程**，但不能称为业内最佳或完整的金融文档处理架构。它已经有几个可靠基础：先按章节归集、模型只给结构化候选、再用 PDF 原文和单元格坐标验证、证据不完整时交人工复核、任务全过程留痕。缺口主要在解析结果的统一结构和经金标数据校准的置信/复核机制，不是再换一个 LLM 就能补齐。

公开的一手资料没有证明存在一个对任意 PDF 都最优的单一解析器。Google Document AI 明确说明 PDF 跨页表格可能仍会拆开；Microsoft Azure Document Intelligence 建议先按页解析、再后处理成一张表。由此可见，跨页关联仍需应用层负责，解析器换代不能替代结构校验。[Google Document AI](https://docs.cloud.google.com/document-ai/docs/layout-parse-chunk)、[Microsoft Azure Document Intelligence](https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/prebuilt/layout?view=doc-intel-4.0.0)

### 与成熟方案的差距

| 能力 | 当前项目 | 成熟架构常见做法 | 评估 |
|---|---|---|---|
| 文档结构 | AnyDoc Markdown 章节内容、PyMuPDF 页面块和表格分别保存 | 用统一文档中间表示，显式关联章节树、段落、表格、单元格、页码、坐标和来源 | 当前结构可追踪，但不同解析产物的实体关联较弱 |
| 表格与续页 | PyMuPDF 建立页内表格/字段列映射，按相邻页结构尝试继承列映射 | 保留页级片段，再根据重复表头、列结构、位置和章节上下文做文档级续表关联 | 方向正确，但仍需以本项目数据验证错误合并率 |
| 证据校验 | 核对原文值、页码和坐标；唯一表格行可以证明行证据 | 单元格级保存原文、行列索引、bbox、来源解析器及置信度；业务校验与原文核验分开 | 当前已有必要证据，缺少完整统一的 cell-level provenance 和置信度 |
| 解析器冗余 | AnyDoc、PyMuPDF 分工，未形成可比较的多解析器适配层 | 按页面质量和解析分歧选择第二解析器；对扫描/低置信页面再运行 OCR 或视觉模型 | 目前对扫描件、文字层异常等输入覆盖不足；不建议所有页面无差别多跑引擎 |
| 复核门槛 | 结构或证据不确定时待复核 | 用带人工真值的测试集，分别校准自动通过、二次解析和人工复核阈值 | 当前尚无系统性的字段级金标指标和校准阈值 |
| 业务闭环 | 保存任务、规则、模型请求/响应、验证和比较结果 | 记录人工修正，回流为带版本的数据集，衡量错误自动放行与复核率 | 任务留痕已具备，人工标注回流和持续评测可继续建设 |

Docling 的 `DoclingDocument` 是统一中间表示的一个开源参考：它把段落、表格、图片、章节/group、页眉页脚、bbox 和 provenance 放入有关联的结构中；Azure 的布局结果也输出 sections、tables、spans 和 bounding regions。它们提供的是可借鉴的表示方式，不代表必须改用相应引擎。[Docling 文档模型](https://github.com/docling-project/docling/blob/main/docs/concepts/docling_document.md)、[Azure 分析结果结构](https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/concept/analyze-document-response?view=doc-intel-4.0.0)

### 建议的目标架构

1. **保留当前章节归集思路。** 先识别文档结构和章节范围，再用模块规则解释章节里的表格、正文、列表或表注；章节规则描述业务语义，具体解析器负责页面结构。
2. **增加规范化中间表示（IR）。** 每个章节节点关联源页；每个文本块、表格片段、逻辑行和单元格保存原文、页码、bbox、表头/行列角色、解析器和版本、校验状态。Markdown 作为给模型阅读的视图，不作为唯一数据事实。
3. **以保守方式拼续表。** 保留所有页级表格片段；利用标题/章节、相邻页、重复或缺失表头、列数和列边界、样式、上下文共同判断。候选不唯一或表格边界/字段映射冲突时不合并，继续复核。
4. **使用选择性交叉验证。** AnyDoc + PyMuPDF 保持当前基线；在章节缺表、字段无法回指、文本层与视觉不符、表格结构异常或跨页关联不确定时，对相关页面调用 MinerU 或 Docling 等第二解析器。不同引擎不是独立真值，不能简单多数投票；必须回到原 PDF 单元格验证。
5. **把业务校验纳入决策。** 在字段来源通过后，再检查行列完整性、汇总/合计、比例分母、单位、报告期和基金身份。校验异常用于发现问题，不能用来篡改原始值。
6. **建立基金语料金标集。** 按数字 PDF、扫描件、续页有/无重复表头、换行标签、合并单元格、横线空值、不同版式等分层标注；比较字段精确率/召回率、表格结构准确率、页/框定位率、错误自动放行率、人工复核率、处理耗时和内存。阈值必须从这些测试结果校准，不能照搬厂商分数。

AWS Textract 官方文档也建议按错误敏感度设置置信门槛和人工审查，并指出财务决策场景可能需要更高阈值；Microsoft 建议结合字段置信度及 OCR 置信度判断是否人工复核；Google Document AI 提供基于标注测试集的 precision/recall 评估。厂商建议仅供阈值设计参考，不能直接当作本项目的验收线。[AWS 置信度最佳实践](https://docs.aws.amazon.com/textract/latest/dg/textract-best-practices.html)、[Microsoft 置信度说明](https://learn.microsoft.com/en-us/azure/ai-services/document-intelligence/concept/accuracy-confidence?view=doc-intel-4.0.0)、[Google 处理器评估](https://docs.cloud.google.com/document-ai/docs/evaluate)

OmniDocBench 提供文本、版面、表格等分项评估和 bbox 标注，说明只看 Markdown 整体观感不足以判断解析效果；但它不是基金招募说明书的专属语料，所以本项目仍需要自己的金标集。[OmniDocBench 官方仓库](https://github.com/opendatalab/OmniDocBench)

### 对本项目的落地顺序

建议先把现在已出现的问题转成固定的验收样本，再比较 AnyDoc+PyMuPDF 与本机 MinerU 的章节覆盖、关键单元格原值、行列结构、跨页拼接和 PDF 框选准确率。若 MinerU 在这些金融关键指标上有明确提升，再将其作为候选解析器接入规范化 IR；否则保留现有快路径，只对结构不确定的页面调用第二引擎。这样比直接宣称某个解析器是“最佳”更可验证，也能控制延时、资源和文档出域风险。

以上对标基于厂商/项目官方文档和公开基准；架构建议是针对本项目的推断。没有对用户的完整基金语料做同条件盲测前，不能声称任何引擎或组合已经是本项目的最佳方案。

## 结论

**推荐先把 MinerU 4.x 纳入 PoC 主候选，保留 PyMuPDF 作为原生 PDF 文本与坐标证据层；用 PaddleOCR PP-StructureV3 做扫描件和低置信表格的对照候选。Docling 适合作为结构化结果模型的对照组，不建议在 PoC 前把三套引擎都接入生产链路。** 无论选哪种解析器，章节范围仍应由文档级章节归集负责，模块规则再解释该章节中的信息；页内解析器给出页面元素、表格和坐标，不能单独保证章节语义正确。

此建议是基于本项目现状和公开能力的**项目推断**，不是厂商对本项目准确率的保证。MinerU 官方文档列出了跨页表格合并能力；但其可选 LLM 辅助单元格续接默认关闭，而且只在既有规则确认续表后判断边界单元格是否续接。必须用基金报告语料实测。

## 本项目当前基线

- 当前 [解析实现](../../app/extraction.py) 用 AnyDoc 生成 Markdown 并归集完整章节；PyMuPDF 提取 PDF 原生文本、物理页码和区块坐标，也按页识别表格。当前提示词要求先按章节识别，再用物理页原文核验引用和页码。
- 这解决了章节边界不等同于物理页边界的问题，但解析器输出和原文证据仍是两条来源。若 AnyDoc 表格结构、PDF 原生文本顺序或页内表格格线有误，章节归集不会自动纠正单元格结构。
- 本项目对照基线应保留 AnyDoc + PyMuPDF。新引擎作为可替换的解析适配器输出页码、块类型、文本、表格单元格/坐标和置信信息，再接入同一章节归集与模块规则流程。

## 候选比较

| 候选 | 官方资料明确提供的能力 | 对本项目的适配判断 |
|---|---|---|
| **MinerU 4.x** | 官方文档描述了 PDF 版面解析、结构化内容、中间 JSON、页码与 bbox；官方更新日志记录了跨页表格合并。其可选 LLM 后处理支持章节标题层级整理和跨页表格边界单元格续接，均默认关闭。支持本地安装及多种 ONNX、Torch、llama.cpp、CUDA/MPS 等运行组合；实际 GPU 后端因系统和安装配置而异。 [输出格式](https://opendatalab.github.io/MinerU/reference/output_files/)、[更新日志](https://opendatalab.github.io/MinerU/reference/changelog/)、[配置及跨页续接](https://opendatalab.github.io/MinerU/usage/quick_usage/)、[运行环境](https://opendatalab.github.io/MinerU/usage/tiers/)、[官方仓库](https://github.com/opendatalab/MinerU) | **最值得优先验证。** 它直接覆盖了当前痛点中的续表和标题层级，中文文档也在官方支持范围内。跨页合并可能合错相邻章节或相似表格；当前解析也要求精确的原文引用与单元格定位，因此不要用合并后 Markdown 取代页级原始证据。LLM 辅助如调用外部服务会带来隐私和费用问题；仅在指向经确认的本地模型服务时纳入本地测试。 |
| **PaddleOCR PP-StructureV3** | 官方管线结合版面检测、OCR 和表格结构识别，可读 PDF，输出按页排序的布局块、块 bbox、OCR 多边形/置信度；SLANeXt 等表格模型区分有线/无线表格。官方支持 CPU/GPU、本地推理，并公布指定硬件与数据集上的耗时指标。 [管线文档](https://www.paddleocr.ai/main/version3.x/pipeline_usage/PP-StructureV3.html)、[表格结构模型](https://www.paddleocr.ai/main/en/version3.x/module_usage/table_structure_recognition.html)、[官方仓库](https://github.com/PaddlePaddle/PaddleOCR) | **适合扫描件、OCR 回退和表格结构交叉校验。** 坐标和中文 OCR 能补当前原生文本路径的空白；但官方列出的输出以单页布局元素为中心，没有足够依据表明它负责基金章节级归集或可通用地合并跨页章节/表格，仍需项目层逻辑。运行时依赖和模型体积高于纯文本提取，需测实际 CPU 环境。 |
| **Docling** | 官方 `DoclingDocument` 保存元素来源页码和 bbox；表格模型保留 cell bbox、行列起止索引、row/column span、表头标记，可导出 JSON/HTML。支持本地 PDF 解析、OCR 引擎选择和 CPU/CUDA/MPS 等加速配置；官方文档提示 OCR 会增加处理时间。 [文档模型](https://docling-project.github.io/docling/reference/docling_document/)、[表格序列化](https://docling-project.github.io/docling/concepts/serialization/)、[OCR 语言](https://docling-project.github.io/docling/concepts/OCR_native/)、[加速配置示例](https://docling-project.github.io/docling//_generated/examples/run_with_accelerator/)、[官方仓库](https://github.com/docling-project/docling) | **结构化中间表示很适合做验证和审计。** cell 几何和合并单元格表达比 Markdown 更适合作为金融表格数据接口；中文 OCR 可选 RapidOCR/PaddleOCR。常规解析能保留元素来源，但不能假定会把跨页表格接成逻辑表；官方模型目录也明确指出某些 VLM preset 不做跨页表格合并。章节归集与续表策略仍须由应用实现。 |

### 速度、成本与隐私

- **厂商数据（仅作量级线索）：** PaddleOCR 官方 PP-StructureV3 基准在其测试集和 NVIDIA V100/A100 环境报告约 1.12–1.77 秒/页（开启图表识别等配置会变化），并列出显存和 CPU/RAM；MinerU 对比值也出现在 PaddleOCR 官方文档内。此结果不是基金报告测试，不可直接外推到本地 Mac/OMP 或本项目页数、模型配置。[PaddleOCR 官方端到端基准](https://www.paddleocr.ai/main/version3.x/pipeline_usage/PP-StructureV3.html)
- **项目推断：** 三者均可在本地处理，文档内容可不离开本机；首次使用仍可能下载模型。应锁定模型版本并提前缓存，断网复跑确认不会触发外部服务。MinerU 启用 LLM 辅助时必须检查服务 URL，只有确认是本机/内网服务才算本地处理。
- **成本边界：** 本地 CPU/GPU 的直接 API 费用可为零，但会消耗部署、模型存储、内存/显存和运行时间；若续表辅助模型调用托管 API，则另计请求费用和数据出域风险。任何时延/费用结论都应按语料实测，而不是用厂商宣传值估算。

## 建议的处理架构

1. **章节归集层：** 依据实际标题、层级、目录与页内顺序构建章节范围，跨页正文、表格、续表和表注都保留在章节上下文中。章节规则定义模块要找什么，不把证券名称、行号、横线或固定页码写成代码分支。
2. **解析候选层：** 先对同一批 PDF 运行现有基线、MinerU、Docling；扫描件和基线/候选结构不一致的页，再运行 PP-StructureV3。每个输出转成统一页级记录，保留原页码、区块类型、bbox、表格 cell、文字及引擎版本。
3. **章节内归属层：** 先按标题和语义把章节归在一起，再让模块规则在章节内识别字段、父子行、金额与比例等信息。模型提取必须回指到单元格原文和所在物理页；无法定位的字段标为不确定。
4. **交叉核验层：** 不需要每页都重复跑多个慢引擎。可先用快速解析；触发条件包括目标章节表格缺失、关键行列数异常、数值/字段映射无法回指、OCR 和原生文本冲突、页间疑似续表。仅对这些页用第二引擎重识别，再按坐标和原文核对。结果冲突时进入人工复核，不用“多数票”覆盖证据。

## PoC 验证指标

用已有语料先建立人工标注的代表集，至少覆盖数字 PDF、扫描件、跨页有/无重复表头、合并单元格、续行标签、破折号空值、版式变化和章节相邻的相似表格。对所有引擎用同一批 PDF、机器和页范围；冻结模型版本与配置。

| 指标 | 计算口径 |
|---|---|
| 章节召回率 / 精确率 | 章节起止与章节内内容块是否覆盖人工标注；特别统计跨页正文和表注遗漏、错归到邻章。 |
| 单元格数值准确率 | 对关键字段比较逐格原文，数字、负号、小数点、百分号及 `-` 单独计错；不能只看整表 Markdown 相似度。 |
| 行列结构准确率 | 行列数、合并单元格、表头字段列与父子行关系；分别报告有线表和无框/弱线表。 |
| 跨页合并精确率 / 召回率 | 统计正确接续、漏接、错误合并三类；错误合并率优先设硬门槛，避免相邻章节/相似表格误连。 |
| 可追溯性 | 关键值是否能回指正确 PDF 页和包含该值的 bbox；报告页码命中率及定位偏差。 |
| 业务效果 | 每份报告的人工复核率、错误自动放行数、漏报差异数；金融数据以错误自动放行为最高风险项。 |
| 资源与稳定性 | 每页耗时及端到端耗时、P50/P95、峰值 RAM/VRAM、CPU/GPU、模型体积、失败/超时率；记录冷启动和模型已缓存两种情况。 |
| 隐私与离线 | 清空模型缓存后安装/预热，再断网处理测试文件；记录所有网络连接，确认 PDF 与中间结果没有上传。 |

建议先以章节归集和单元格来源可追溯作为准入条件，再比较准确率与耗时。准确率/速度阈值应根据现有人工复核基线和用户可接受成本确定，不应在没有标注结果时预设一个虚假的“业内准确率”。

## 参考说明

本笔记将各厂商文档中的功能描述、跑分视为**厂商声明**；其中涉及本项目的适配判断、组合建议、风险和 PoC 设计均为**项目推断**。跨页拆分及应用层后处理的一般背景见[已有跨页续表调研](pdf_cross_page_table_continuation.md)。
