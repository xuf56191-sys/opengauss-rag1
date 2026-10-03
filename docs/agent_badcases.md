# Agent Bad Case 记录（阶段 3–4 起）

> 格式：现象 → 原因 → 修改 → 修改后结果。数字均来自本机真实运行。

## ABC-1：模型跳过工具、直接凭记忆回答

- 问题：`今天西安天气怎么样？`（阶段 3 实测）
- 原始行为：`steps=1, tools_used=[]`，模型直接用自己的话拒答「我是 openGauss 配置助手…」；系统仍标 `refused=False`
- 原因：`tool_choice="auto"` 只是建议；模型也可不调工具。更危险的是领域问题（如「某配置默认值是多少」）可能被凭记忆编造，绕过检索、拒答与引用校验
- 修改：阶段 4 加「guard 门闩」——零证据时，领域问题回退 `rag_answer`，非领域直接拒答（`no_evidence`）
- 修改后：天气题 `refused=True, reason=no_evidence`，模型话术被固定拒答句覆盖

## ABC-2：拒答口径依赖模型自觉

- 问题：`refused` 原先只来自 `rag_answer` 工具返回（阶段 3）
- 原始行为：模型自己说「无法回答」时，系统字段仍是 `refused=False`
- 原因：没有把「检索/校验层」的信号做成最终输出的唯一权威
- 修改：guard 规则 1——工具层一旦判拒答，用 `answer6.REFUSE_MESSAGE` 覆盖模型输出，并清空 citations
- 修改后：`refused=True, refusal_reason=high_vector_distance`，答案与 RAG 链路一致

## ABC-3：领域问题被模型「裸答」时的证据回退

- 问题：领域问题但模型未调工具
- 修改：guard 规则 4b——确定性回退调用 `rag_answer`；它若有据回答则替换模型答案，若判拒答则拒答，若自身失败则拒答（`fallback_failed:*`）
- 修改后：单测覆盖三条分支；`tool_calls` 里以 `origin=guard_fallback` 标记，便于评测区分「模型主动调用」与「门闩补救」

## ABC-4（待处理）：无引用被当作「引用合法」

- 现象：答案没有任何 `[n]` 时 `citation_valid=True`
- 原因：当前 `parse_citations` 只校验「越界」，不校验「必须引用」
- 计划：阶段 5/6 在评测中单独统计「有据回答的引用覆盖率」，避免把「空引用」误算成通过

## ABC-5（环境类）：临时故障与网络

- 现象：DeepSeek Key 失效导致 401；SiliconFlow 偶发 SSLError/Timeout；httpx 走系统代理访问本机 502
- 修改：Agent LLM 可切换 provider（`AGENT_LLM_*`）；`embed_client1` 退避重试；smoke 用 `trust_env=False`
- 修改后：评测/冒烟可完整跑完；Key 问题时可用备用 provider 继续验证
