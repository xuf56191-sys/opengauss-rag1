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

## ABC-4（已量化）：无引用被当作「引用合法」

- 现象：答案没有任何 `[n]` 时 `citation_valid=True`
- 原因：`parse_citations` 只校验「越界」，不校验「必须引用」
- 阶段 6 量化：Agent Eval 26 题中，语料内 20 题的 **Citation Coverage = 75%**，即 A1/A2/A3/A4/A11 五题的回答**没有任何 `[n]`**
- 根因（比 ABC-4 原判断更具体）：Agent 直接基于 `search_docs` / `get_config_detail` 的观察结果自由作答，**没有经过 `answer6` 那套「编号资料 + 强制引用」的 Prompt**
- 计划：`final` 阶段引入统一的「有据但无引用」标记（例如新增 `grounded_no_citation`），评测中单独统计；或对这类回答补一轮受约束的引用标注

## ABC-6：带点号的配置名被 get_config_detail 拒绝

- 问题：`dolphin.default_database_name 的作用是什么？`（A3）
- 原始行为：模型正确选择了 `get_config_detail`，参数 `dolphin.default_database_name`，工具返回
  `ok=false, error_code=invalid_config_name`（耗时 0ms，说明在校验阶段就被拒绝）
- 原因：`tools.py` 的配置名校验正则 `^[A-Za-z_][A-Za-z0-9_]*$` **不允许点号**，而该配置项名里含 `.`
- 模型的自救：自动降级为两次 `search_docs`，并在答案里**主动说明**「我尝试用 get_config_detail 查询被拒绝，所以内容来自文档检索」——这是结构化错误码（`error_code`）带来的正面效果
- 修改：未改代码（保留真实边界）；已列入改进项：允许 `[A-Za-z0-9_.]+` 或提供「配置项别名解析」
- 修改后结果：该题仍判为任务成功（答案命中关键词），但工具选择与延迟受影响（steps=3、29.9s）

## ABC-7：Agent 回答的引用覆盖率低于 /ask

- 现象：同一问题 `/ask` 通常带 `[1][2]`，`/agent` 有 25% 的语料内回答完全无引用
- 原因：`/ask` 的 Prompt 强制编号引用；Agent 的 final 答案来自模型对工具观察的自由总结，未强制
- 影响：可追溯性下降；评测里 `citation_valid=True` 有「空验证」成分
- 修改：阶段 6 新增 **Citation Coverage** 指标把它显性化（不掩盖）
- 修改后结果：指标可量化（75%），后续优化方向明确

## ABC-8：延迟分布极不均衡

- 现象：Agent Eval 平均 9160ms，但最短 654ms、最长 34518ms
- 原因：① 工具调用次数不同（1 vs 2~3）；② `rag_answer` 内部含一次完整 LLM 生成，单次可达 16.7s（A11）；③ 部分题目模型连续两次 `search_docs`
- 影响：只看平均值会误导（P50 远小于均值）
- 计划：报告中补 P50/P95；可对 `max_steps` 与工具选择做成本控制


## ABC-5（环境类）：临时故障与网络

- 现象：DeepSeek Key 失效导致 401；SiliconFlow 偶发 SSLError/Timeout；httpx 走系统代理访问本机 502
- 修改：Agent LLM 可切换 provider（`AGENT_LLM_*`）；`embed_client1` 退避重试；smoke 用 `trust_env=False`
- 修改后：评测/冒烟可完整跑完；Key 问题时可用备用 provider 继续验证
