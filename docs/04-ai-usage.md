# 04 AI 使用说明

## 1. 如实声明

本项目的主要代码、测试、评测脚本和文档由 AI 编程助手（Claude Code）在当前工作区
中生成和修改。人类操作者提供了分阶段任务说明、项目约束，并在本地 `.env` 中配置了
DeepSeek 凭证；助手未读取、打印或提交凭证值。

本文件不声称存在未被记录的人工代码贡献，也不声称 `examples/reviews/trace_review.json`
中的语义审查由独立人类完成。该审查是基于实际 Trace 的**非自动语义判断**，由 AI
助手完成；最终交付前仍需要独立人类复核。

## 2. AI 辅助范围

- 阅读和修改 `src/knowledge_agent/` 下的生产代码；
- 编写和更新 pytest 测试（当前 231 个通过用例）；
- 编写 `scripts/run_evaluation.py` 评测器；
- 运行自动化测试、FakeLLM 评测和真实 DeepSeek 端到端运行；
- 生成评测用例、结果、Trace 和人工语义审查记录；
- 编写 README、需求/方案/评测/复盘文档。

## 3. 人类操作者的已知角色

- 提供「小影校招 AI Agent 开发考核」各阶段任务说明；



## 4. 验证方式

- 自动化测试：`python -m pytest -q` → 231 passed / 0 failed / 0 skipped
  （阶段六初始 216，follow-up 新增 7 个语义支持测试、1 个安全测试和 2 个重试
  计数语义测试）。
- 评测入口：`python scripts/run_evaluation.py`，18 个用例（E01–E15 + E13R + B01），
  真实与 FakeLLM 分离。
- 真实运行：DeepSeek `deepseek-flash`，多次 CLI/评测调用，Trace 位于
  `examples/traces/real/` 和历史根目录。
- 安全扫描：`.env` 被 `.gitignore` 排除；Trace 未发现 API Key、Authorization
  或请求头。
- 评测结果和人工审查记录保留原始文件，不只用汇总百分比。

## 5. 局限

- AI 可能对语义支持、模型非确定性和预算取舍产生盲点；
- 语义审查不是独立人类评审；
- 真实样本少，不能声称统计显著；
- 文档和结论应结合 `examples/evaluation_results.json`、Trace 和审查记录一起阅读，
  不应只看最终状态或单次通过结果。

最终交付前建议由独立人类复核 E01/E03/E11、关键安全边界和所有真实 Trace。

## 6 阶段六补充

- AI 助手实现了独立格式/最终校验预算、重复采样入口、统计汇总和人类评审包生成脚本。
- 真实重复采样由 AI 助手调用 DeepSeek 完成：25 次真实运行、5 次 E11 FakeLLM
  采样、3 次 E12B 候选预算采样；最终验收前 follow-up 又完成 E12B 两组各追加 5 次
  （累计 8+8）、E13R 4 次真实注入、E01/E02/E03 各 1 次真实回归。
- `examples/reviews/human_review_packet/`、`human_review_template.json`、
  `human_review_checklist.md` 是 AI 生成的评审材料；`semantic_claims.json` 的
  claim/quote 来自实际运行。
- 用户已提供 8 个用例的人类评审结果，记录在
  `examples/reviews/human_review_results.json`：5 pass / 1 fail / 2 needs_followup。
  E11 引用真实性 pass、语义支持 fail；E12B/E13 需要 follow-up。
- `trace_review.json` 仍是 AI 辅助审查（`ai_assisted_not_human`），不得作为独立
  人类结论；不得把用户的人类评审与 AI 审查混写。
- 自动化测试 231 passed；这不等于业务验收或所有语义判断通过。
- 本阶段未执行 Git commit/push；用户已提供 E11/E12B/E13 的 follow-up 带限制
  判断，记录在 `followup_human_review_results.json`，最终提交仍需人工确认。

## 7 最终验收前 follow-up 补充

- AI 助手实现了 `src/knowledge_agent/semantic.py` 的确定性 claim/quote 语义支持
  守卫，并把它与引用真实性校验分离；语义检查不调用模型、不增加 `MAX_STEPS`。
- AI 助手新增并执行 E13R 真实模型 Prompt 注入用例：4 次真实运行均实际检索并读取
  恶意文档，安全不变量 4/4；2 次正常结束、2 次因保守语义校验/步数预算
  `incomplete`。用户随后给出 `partial` 判断：未观察到安全绕过，但不能证明普遍
  免疫，安全与完成率分开评估。
- AI 助手新增 E12B 两组预算各 5 次真实采样并合并为 8+8 描述统计；默认组 7/8、
  候选组 8/8。AI 助手没有据此修改默认 6/5，也没有声称统计显著性。
- 复核 E13R 的 `final_validation_retries` 时发现旧计数把触发停止的终止失败也算作
  重试。AI 助手已把计数拆为 failures（所有失败）和 retries/repairs（实际发出的
  重试），新增 2 个回归测试，并从历史 Trace 步骤序列推导实际重试数；没有改写
  历史 Trace，也没有改变模型调用次数或预算。
- AI 助手生成 `examples/reviews/followup_review_packet/` 和
  `followup_review_status.json`，只追加新材料，没有覆盖
  `human_review_results.json` 中的历史 E11 FAIL。
- 用户已提供 follow-up 带限制判断（E11 `pass_with_limits`、E12B
  `accepted_no_budget_change`、E13 `partial`），记录在
  `followup_human_review_results.json`；AI 自动断言、AI 汇总和 AI 辅助审查都不能
  冒充人类结论，也没有把任何新结果标成全面 PASS。最终验收仍开放。
