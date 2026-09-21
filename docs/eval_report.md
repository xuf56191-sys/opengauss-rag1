# 离线评测报告（小样本回归）

- 语料集合：`opengauss_docs`（笔记 + 面试速查，约 120 chunks）
- Chroma：`D:\PPT文档\RAG新项目\opengauss-rag\data\chroma`
- 来源题：8 条，指标 Recall@1 / Recall@3
- 越界题：4 条，指标：代码层拒答是否触发（`should_refuse`）
- 说明：评测**不调用 LLM**，只测检索与拒答；样本来自个人语料切片，**非生产准确率**。

## 检索：纯向量 vs 混合

| 方法 | Recall@1 | Recall@3 |
|------|----------|----------|
| 纯向量 | 87.50% | 100.00% |
| 混合 BM25+向量 | 100.00% | 100.00% |

### 来源题明细

| ID | Query | Vec@1 | Vec@3 | Hyb@1 | Hyb@3 |
|----|-------|-------|-------|-------|-------|
| S1 | enable_dolphin_proto 是干什么的？ | True | True | True | True |
| S2 | Peewee 怎么连 openGauss B 兼容模式？ | True | True | True | True |
| S3 | 为什么 PyMySQL 的 database 参数要和 schema 同名？ | True | True | True | True |
| S4 | SHOW INDEX 返回的主键名称为什么 Peewee 识别不了？ | True | True | True | True |
| S5 | Peewee 为什么读不出自增字段 identity？ | True | True | True | True |
| S6 | 错误密码时 Peewee 认证失败抛什么异常？ | False | True | True | True |
| S7 | 批量插入为什么要用 as_rowcount？ | True | True | True | True |
| S8 | 重复键冲突时 Peewee 应该捕获什么异常？ | True | True | True | True |

## 越界题拒答

- 拒答正确率：**100.00%**（4 题）

| ID | Query | refused | reason | max_bm25 | min_dist | pass |
|----|-------|---------|--------|----------|----------|------|
| O1 | 今天西安天气怎么样？ | True | high_vector_distance | 13.0935 | 0.56 | True |
| O2 | Python 的 GIL 是什么？ | True | high_vector_distance | 6.9777 | 0.533 | True |
| O3 | 帮我写一首关于春天的唐诗 | True | high_vector_distance | 8.1063 | 0.6776 | True |
| O4 | 小米公司今年营收多少？ | True | high_vector_distance | 3.5087 | 0.6189 | True |

## 口径

- Recall@k：Top-k 文本或 `heading_path` 命中任一关键词即算命中。
- 拒答规则：`no_hits` 或 `max_bm25_raw < 2.0` 或 `min_vec_dist > 0.52`（OR；扩语料后由 0.55 收紧）。
- 阈值来自 peek_signals + 本评测集标定；语料扩充后需重标。

