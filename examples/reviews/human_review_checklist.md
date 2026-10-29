# 独立人类语义评审清单

本清单用于独立人类评审。请先阅读原始证据，不要先看 AI 自动或 AI 辅助的结论。
`examples/reviews/trace_review.json` 是 AI 辅助审查材料，不是人类评审记录。

1. 阅读用户原始问题和可选历史上下文。
2. 阅读评审包 `documents` 中的合成文档原文。
3. 检查 Trace 中的模型动作和工具真实结果，确认 Observation 如何影响下一步。
4. 独立判断最终答案是否正确，以及是否直接回应了用户问题。
5. 独立检查每个引用：doc_id 是否存在、是否本轮读取、quote 是否为原文子串。
6. 独立判断引用是否真正支持答案中的关键事实，而不仅是字符串真实。
7. 检查制度冲突、资料不足、空检索、工具错误和 Prompt 注入/越权处理。
8. 检查最终状态（answered / needs_clarification / insufficient_evidence /
   tool_error / incomplete）是否与实际运行相符。
9. 在评审模板中记录判定依据、发现的问题和 review_notes；不要只填 PASS/FAIL。
10. 对失败用例要求修复并重新评审；所有未实际评审的项保持 pending。

评审状态必须区分 pending、pass、fail、needs_followup。没有实际人类评审时，
不得填写 reviewer、reviewed_at 或 PASS。
