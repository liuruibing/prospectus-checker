# 招募说明书投资组合核对

独立应用：上传一份基金招募说明书和一份对应的定期报告，按可维护的 Markdown 规则抽取三个投资模块，比较经证据验证的披露字段。左侧按模块列出差异，右侧并排展示两份 PDF 原文。

## 工作流

1. 上传招募说明书与定期报告 PDF。
2. LangGraph `prospectus-periodic-comparison` 执行输入加载、两份文档并行识别、结构化比较；工作流通过 `WorkflowContext`、`WorkflowProtocol.stream()`、`WorkflowRegistry` 与 `WorkflowEngine` 解耦，节点返回底座可消费的业务事件。
3. 模型 provider 可选。默认使用 LangChain `ChatOpenAI` 调用 `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL` 配置的兼容接口；本机验证可设 `PROSPECTUS_LLM_PROVIDER=omp`，通过已登录的 OMP CLI 调用 `PROSPECTUS_OMP_MODEL`（默认 `openai-codex/gpt-6-luna`）。OMP 模式不保存会话、不启用工具、扩展或项目上下文文件，避免把无关本地上下文带入文档抽取。AI 输出结构化候选、原文值、物理页码和逐字引用。
4. AnyDoc 生成整表 Markdown，帮助保留跨页续表的行列结构。其 PDF 输出不带可靠物理页/单元格坐标；程序通过页脚与 PyMuPDF 页面交叉定位，并把续页文本一并交给模型。PyMuPDF 是页码、引用校验、表格单元格和框选坐标的唯一依据；无法唯一验证时不猜坐标。
5. 程序按规则校验基金身份与披露时点；报告期不同作为口径提示但不阻断字段比较，横线不当作 0。行数不同本身不是差异：按资产项目、行业、股票代码等身份对齐行。
6. 左侧按模块列出差异；展开后查看明细，点击差异跳至两侧 PDF 的物理页并在可验证坐标处画框。两侧 PDF 可独立连续滚动。

仅启用当前三项规则：

- [资产组合情况](rules/investment/asset_allocation_资产组合情况.md)
- [行业分类股票投资组合](rules/investment/industry_stock_portfolio_行业分类股票投资组合.md)
- [股票投资明细](rules/investment/stock_holdings_股票投资明细.md)
- [公共证据与比较约定](rules/investment/common_公共规则.md)

## 本地启动

```sh
npm install --prefix frontend
npm run build --prefix frontend
uv sync --extra dev
cp .env.example .env
# 默认使用兼容接口；也可设置 PROSPECTUS_LLM_PROVIDER=omp 使用本机 OMP
uv run uvicorn app.main:app --host 127.0.0.1 --port 8331 --reload
```

OMP 模式需在运行后端的同一用户环境中安装并登录 OMP；`.env` 可设：

```env
PROSPECTUS_LLM_PROVIDER=omp
PROSPECTUS_OMP_MODEL=openai-codex/gpt-6-luna
```

打开 <http://127.0.0.1:8331/>。如直接使用 AI 底座项目现有的模型配置文件，不复制密钥，启动时指定：

```sh
PROSPECTUS_ENV_FILE=/Volumes/onePiece/公司前端项目/AI项目/vmchat-hermes-replacement-python/.env \
  uv run uvicorn app.main:app --host 127.0.0.1 --port 8331 --reload
```

REST 接口：

- `GET /v1/workflows` 返回已注册工作流 ID。
- `POST /v1/workflows/prospectus-periodic-comparison/runs` 用 multipart 字段 `prospectus`、`periodic_report` 上传 PDF 并返回结果。
- `POST /api/compare` 保留同步上传核对接口，供兼容调用。
- `POST /api/jobs` 使用 multipart 字段 `prospectus`、`periodic_report` 提交后台任务，立即返回 HTTP 202 和 `job_id`。
- `GET /api/jobs` 获取近期任务状态；`GET /api/jobs/{job_id}` 在任务完成后返回完整核对结果。
- `GET /api/jobs/{job_id}/trace` 查看任务事件时间线、运行配置、输入文件摘要和追踪产物清单；`GET /api/jobs/{job_id}/artifacts/{path}` 读取具体追踪产物。
- 状态包括 `queued`、`running`、`completed`、`failed`。任务追踪记录保存在本地目录；PDF 预览缓存最多保留 4 份文件，服务重启后需要重新生成预览链接。

后台上传任务会把任务状态、阶段事件、原始 PDF、AnyDoc Markdown、章节上下文、PyMuPDF 页面/表格结果、实际模型提示词与原始响应、验证结果及最终比较结果保存在本机 `var/tasks/{job_id}/`。任务 ID 与现有 `run_id` 相同；任务记录重启后仍可按 ID 查询，服务重启时未完成任务会记为中断。文件和提示词可能包含完整报告内容，目录仅对当前操作系统用户开放；测试数据默认不自动清理。可通过 `PROSPECTUS_TASK_LOG_DIR` 更改保存位置。

当前项目只包含投资组合核对工作流；VMChat 的问数、委托风控、聊天、任务画布等无关流程没有复制进来。`InvestmentComparisonWorkflow` 实现与 AI 底座相同的 `id` + `stream(context)` 约定，可注册到其 `WorkflowRegistry`，由 `WorkflowEngine.stream()` 调用。底座集成时将 `context.document_ids` 按“招募说明书、定期报告”排序，并设置 `context.document_bytes_loader = make_document_bytes_loader(document_service)`；该适配器从底座 `DocumentService.require_ready()` 取得原 PDF 文件路径。底座的 `document_loader` 只返回解析文本，不能作为坐标证据。

依赖通过 `uv.lock` 锁定；AnyDoc 固定为 `firecrawl-anydoc==0.2.4`，默认仅本地解析，OCR 保持拒绝策略，不会把用户 PDF 发往托管 OCR 服务。

## 前端技术栈

前端使用 Vue 2.5、Element UI 2.12、Vue Router 3 与 webpack 4，保持与信批项目相同的 Vue 主栈；此仓库的前端独立实现，未复制参考项目页面。上传后以单页连续方式查看 PDF，并按各页真实尺寸延迟加载页面图。


## 本地验证

```sh
uv run --extra dev pytest -q
```

## 边界与复核

- 操作人员选择本次定期报告。当前应用没有接入外部公告数据库，不能证明这份文件是招募说明书披露日前“最近一次”披露；需由业务人员按披露日期选择。
- 两份文件的基金代码须一致（若两侧都有可验证代码）；报告期末可以不同，系统仍逐项比较已定位字段并提示期间差异。报告期末缺失时需人工复核；定期报告披露晚于招书披露日期时不作为校验来源。
- 当前 PDF 文本层解析不含 OCR。扫描件或无法逐字验证的字段不会伪造坐标。
- 上传文件在当前服务进程内短暂保留用于 PDF 页图预览；重启服务后预览链接失效。
- 默认单 PDF 上限 40 MiB、240 页；可通过 `PROSPECTUS_MAX_UPLOAD_BYTES`、`PROSPECTUS_MAX_PAGES` 调整。

## 参考

- AI 底座：`liuruibing/vmchat-hermes-replacement-python` 的 `work/performance-query-2026-10-27`（提交 `3917b7feb4862b025121f9d74303bda101b66e5d`）。按其 LangChain Provider、LangGraph 工作流、`WorkflowContext`/`WorkflowRegistry`/`WorkflowEngine` 与 REST 约定实现兼容接口；未复制底座仓库及无关 workflows。
- UI 参考：`/Volumes/onePiece/公司前端项目/信批信创/datadriver-ann-reform-vue` 的 Vue 技术栈与浅色管理后台风格；本项目实现独立双栏工作台，没有复制其整套前端/品牌/无关页面。
