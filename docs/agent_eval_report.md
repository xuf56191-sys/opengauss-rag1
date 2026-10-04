# Agent 离线评测报告（自建小样本）

- 评测对象：`POST /agent`（单 Agent + 3 Tool + LangGraph + 证据/拒答门闩）
- 用例数：26（语料内 20 / 库外 6）
- 用例文件：`data/agent_eval_cases.json`；脚本：`src/agent_eval.py`
- **口径：自建小样本离线评测，不代表生产准确率**；评测真实调用 LLM 与检索。

## 指标总览

| 指标 | 结果 | 说明 |
|------|------|------|
| Infrastructure Error Rate | 0.00% | LLM/网络故障题数占比（**不计入模型能力**） |
| Tool Selection Accuracy | 100.00% | 语料内问题是否调用可接受工具（命中任一即可） |
| 零工具调用率 | 0.00% | 语料内问题一次工具都没调的比例（越低越好） |
| Parameter Accuracy | 100.00% | 配置名 / 查询关键词是否出现在工具参数里 |
| Task Success Rate | 100.00% | 拒答行为符合预期 + 答案命中关键信息（仅统计非故障题） |
| Citation Valid Rate | 100.00% | `citation_valid=true` 占比 |
| Citation Coverage | 65.00% | 语料内回答真的带 `[n]` 的比例（ABC-7） |
| OOS Refusal Rate | 100.00% | 库外问题被拒答的比例 |
| 假拒答率（语料内） | 0.00% | 语料内问题被误判「资料不足」的比例（越低越好） |
| Average Tool Calls | 1.54 | 每题平均工具调用次数 |
| Average Steps | 1.81 | 每题平均 LLM 轮数 |
| Average Latency | 7081 ms | 端到端平均耗时 |
| P50 / P95 Latency | 7475 / 12400 ms | 分布差异大时比均值更有意义（ABC-8） |

- 计分题数：26 / 26（排除 0 条基础设施故障）

## 逐题明细

| ID | 类型 | Query | refused | 工具 | 步骤 | 引用 | citation_valid | 任务成功 | 故障 | 延迟(ms) |
|----|------|-------|---------|------|------|------|----------------|----------|------|----------|
| A1 | in_corpus | enable_dolphin_proto 是干什么的？ | False | get_config_detail | 2 | [] | True | True | - | 4354 |
| A2 | in_corpus | dolphin_server_port 控制什么？ | False | get_config_detail,search_docs | 2 | [] | True | True | - | 5574 |
| A3 | in_corpus | dolphin.default_database_name 的作用是什么？ | False | get_config_detail,search_docs,rag_answer | 3 | [] | True | True | - | 15002 |
| A4 | in_corpus | Peewee 怎么连 openGauss B 兼容模式？ | False | rag_answer,search_docs | 2 | [] | True | True | - | 10740 |
| A5 | in_corpus | 为什么 PyMySQL 的 database 参数要和 schema 同名？ | False | rag_answer,search_docs | 2 | [1, 2, 3] | True | True | - | 10829 |
| A6 | in_corpus | gsql 密码和 MySQL native 密码有什么区别？ | False | rag_answer,search_docs | 2 | [2, 3] | True | True | - | 7300 |
| A7 | in_corpus | SHOW INDEX 返回的主键名为什么 Peewee 识别不了？ | False | search_docs,search_docs | 2 | [2, 4] | True | True | - | 12400 |
| A8 | in_corpus | Peewee 为什么识别不出自增字段？ | False | search_docs,rag_answer | 2 | [1, 4, 5] | True | True | - | 10068 |
| A9 | in_corpus | 重复键冲突时 Peewee 应该捕获什么异常？ | False | rag_answer,search_docs | 2 | [1, 2] | True | True | - | 7298 |
| A10 | in_corpus | 批量插入为什么要用 as_rowcount？ | False | search_docs,get_config_detail | 2 | [1, 3] | True | True | - | 5729 |
| A11 | in_corpus | 错误密码时 Peewee 报的错误码 514 是什么情况？ | False | search_docs,rag_answer | 2 | [] | True | True | - | 7891 |
| A12 | in_corpus | 为什么要给连接用户设置 standard_conforming_strings=off？ | False | get_config_detail,search_docs | 2 | [] | True | True | - | 5482 |
| A13 | in_corpus | 用 SQL_ASCII 建库会导致什么字符类型问题？ | False | rag_answer,search_docs | 2 | [1, 2, 3] | True | True | - | 7475 |
| A14 | in_corpus | openGauss 的 B 兼容模式是什么？ | False | rag_answer,search_docs | 2 | [1, 2, 3] | True | True | - | 8084 |
| A15 | in_corpus | 3308 端口是什么端口，和 gsql 端口有什么区别？ | False | rag_answer,search_docs | 2 | [1, 2, 3] | True | True | - | 7542 |
| A16 | in_corpus | DECIMAL 精度丢失问题是怎么定位和修复的？ | False | rag_answer,search_docs | 2 | [1, 2, 3] | True | True | - | 9193 |
| A17 | in_corpus | Peewee 的 atomic 事务回滚怎么验证？ | False | search_docs,rag_answer | 2 | [1, 2, 3] | True | True | - | 9020 |
| A18 | in_corpus | Dolphin 插件在这个任务里做什么？ | False | rag_answer,get_config_detail | 2 | [1, 3] | True | True | - | 8732 |
| A19 | in_corpus | 自增 extra 大小写问题的根因是什么？ | False | rag_answer,search_docs | 2 | [1, 2, 3] | True | True | - | 8662 |
| A20 | in_corpus | openGauss B 库应该用什么字符集建库？ | False | rag_answer,search_docs | 2 | [] | True | True | - | 7728 |
| O1 | oos | 今天西安天气怎么样？ | True | - | 1 | [] | True | True | - | 2453 |
| O2 | oos | Python 的 GIL 是什么？ | True | - | 1 | [] | True | True | - | 2426 |
| O3 | oos | 帮我写一首关于春天的唐诗 | True | - | 1 | [] | True | True | - | 2242 |
| O4 | oos | 小米公司今年营收多少？ | True | - | 1 | [] | True | True | - | 1942 |
| O5 | oos | 红烧肉怎么做才好吃？ | True | - | 1 | [] | True | True | - | 1792 |
| O6 | oos | Kubernetes 的 Pod 和 Deployment 有什么区别？ | True | - | 1 | [] | True | True | - | 4158 |

## 未通过用例（badcase 候选，已排除基础设施故障）

- 本次无未通过用例。
## 基础设施故障（不计入模型能力）

- 本次无。

