# 02 方案设计

## 1. 总体架构

```text
CLI / evaluation harness
        │
        ▼
KnowledgeAgent (agent.py)
  ├─ 构造 messages：系统规则 + 历史 + 当前问题 + Observations
  ├─ LLMClient.chat(JSON Object mode)
  ├─ parse_agent_action（JSON + Pydantic 判别联合）
  ├─ 显式动作路由 + 硬预算
  ├─ tools.search_documents / tools.read_document
  ├─ validator.validate_final_result（引用/冲突/证据）
  └─ TraceRecorder（真实或 FakeLLM 标记）
        │
        ├── retrieval.py → data/documents.json
        └── tracing.py → examples/traces/...
```

## 2. 模块职责

| 模块 | 职责 |
|---|---|
| `config.py` | 从 `.env`/环境变量加载 DeepSeek 配置，密钥不可硬编码、repr 脱敏 |
| `schemas.py` | 动作、引用、`FinalResult` 的 Pydantic 契约 |
| `llm_client.py` | OpenAI 兼容 Chat Completions 客户端、超时、错误分类 |
| `retrieval.py` | 数据加载校验、规范化、同义词扩展、可解释关键词评分 |
| `tools.py` | `search_documents` / `read_document` 及统一异常契约 |
| `agent.py` | 模型驱动 ReAct Loop、动作路由、Observation、预算、状态 |
| `validator.py` | 引用真实性、期限冲突、证据状态和最终结果安全检查 |
| `tracing.py` | 可审计 JSON Trace，记录真实/FakeLLM 模式 |
| `main.py` | CLI 入口、历史文件、Trace 保存 |
| `scripts/run_evaluation.py` | 统一评测执行器、断言、结果和审查挂接 |

## 3. 动作协议与路由

`parse_agent_action` 使用带 `action` 判别字段的 Pydantic 联合；只接受四种动作，
`extra="forbid"`，参数名称/类型/范围由阶段一 Schema 固化。Runtime 使用显式
`isinstance` 路由：

- `SearchDocumentsAction` → `tools.search_documents(query, top_k)`
- `ReadDocumentAction` → `tools.read_document(doc_id)`
- `AskUserAction` → `FinalResult(needs_clarification)`
- `FinishAction` → `validator.validate_final_result` 通过后返回

没有动态函数名、模块名、路径或 Shell 参数。

## 4. ReAct Loop

每轮：

1. 构造包含系统规则、历史、当前问题和已累计 Observation 的消息。
2. 调用真实模型（JSON Object 模式）。
3. 解析 JSON 并做 Pydantic 校验；非法输出不执行工具，反馈错误 Observation。
4. 检查动作与预算。
5. 执行工具或处理终止动作。
6. 将真实结果/错误写入 Observation 和 Trace。
7. 模型基于最新 Observation 继续决策。

预算：

- `MAX_STEPS=6`（每次模型调用计一步，包括格式修复和最终结果重试）
- `MAX_TOOL_CALLS=5`（每次实际工具执行计一次）
- `MAX_FORMAT_REPAIRS=2`（JSON/Schema/动作结构错误后的受限修复请求）
- `MAX_FINAL_VALIDATION_RETRIES=2`（最终结果引用/冲突/证据校验失败后的重试）
- 格式修复与最终校验重试使用独立计数器，互不消耗；工具错误不消耗这两类预算。
- 达到步数或工具上限立即停止；返回 `incomplete`；停止原因分别为
  `max_steps` / `tool_limit`；格式/最终校验预算耗尽为
  `format_budget` / `final_validation_budget`。
- 格式修复只重新请求并执行完整 JSON + Pydantic 校验，不做字符串拼接修复，
  不会绕过工具白名单或引用/冲突校验。

## 5. 错误与 Observation 边界

- 空检索：`ok=true, data=[]`，正常 Observation。
- `InvalidArgumentError`、`DocumentNotFoundError`：业务错误，反馈模型，
  `retryable=true`。
- `DocumentDataError`：数据错误，`retryable=false`，仍受工具预算约束。
- 未知工具异常：`UnexpectedToolError`，不暴露堆栈/原始异常文本。
- 模型服务错误：直接 `tool_error`，停止原因 `model_error`，不做无限重试。
- 任何重试/失败工具调用都计入工具预算；格式/最终校验错误计入步数预算，但分别
  使用独立的格式/最终校验计数器。
- Trace 记录 `repair_kind`（`format` / `final_validation`）以及
  `format_repairs`、`final_validation_retries` 计数，便于区分失败类型。

## 6. 业务安全不变量

`validator.py` 检查：

- 引用文档必须在当前运行中读取；quote 必须是对应原文真实子串；禁止跨文档借用。
- 从实际文档提取 `N日/N天/N个工作日` 期限；不同期限且无优先级/适用范围依据时
  视为未解决冲突。
- `answered` 必须有引用、无未解决冲突、回答中的期限必须来自已知文档。
- `needs_clarification` 冲突场景必须引用冲突双方并包含期限原文。
- `insufficient_evidence` 允许空引用，但必须有 gaps；已知冲突时要求澄清。
- `tool_error` 必须有真实工具/系统错误；空检索不算。
- `incomplete` 必须有实际未完成依据。

字符串验证只证明引用真实性，不证明语义支持；语义由人工/评测流程判断。

## 7. 上下文、Trace 与 CLI

- 历史最多 8 条、4000 字符；与当前问题分开；不是证据。
- Trace 记录动作、参数、工具摘要、错误类型、`tool_calls_made`、`steps_used`、
  最终状态和停止原因；不记录密钥/完整原文。
- CLI 使用真实模型；配置缺失退出码 2；不静默切换 FakeLLM。

## 8. 评测设计

- `examples/evaluation_cases.json` 包含 17 个用例（E01–E15、E12A/B、B01）。
- 真实用例与 FakeLLM 用例分离；Trace 分别写入 `examples/traces/real/` 和 `fake/`。
- 自动断言验证状态、引用、工具调用、预算、错误类型和 Trace 一致性。
- 语义支持由非自动审查记录在 `examples/reviews/trace_review.json`；
  评估报告区分自动通过与语义通过。
- 预算对比使用相同用例和提示词，profile 为 6/5（baseline）和 8/7（candidate）。

## 9. 设计取舍

- 选择硬工具预算而不是“达到上限后仍允许一次模型结束”：更严格、可预测，但可能
  在最后一次证据可用时返回 incomplete。该取舍在评测中明确记录。
- 选择确定性字符串验证而不是语义模型评审：避免把不稳定的语义判断伪装成自动安全，
  但留下了 E11 这类“引用真实、语义不支持”的情况。
- 选择单 Agent、本地 JSON、无向量库：满足考核范围，便于复现和审计。
