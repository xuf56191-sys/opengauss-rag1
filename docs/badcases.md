# 检索 Bad Case 记录

## 2026-09-14 纯向量基线（bge-m3 + Chroma, k=3）

### BC-1 标识符查询被语义冲淡
- Query: `enable_dolphin_proto 是干什么的？`
- Expected: 「2. 连接前提」里关于协议开关的说明
- Actual top1: 「4. 兼容问题故事 A：主键 / SHOW INDEX」dist=0.469
- Actual top2/top3: 连接前提相关片段，但排在后面
- Hypothesis: 向量对「相似语义场景」敏感，对「精确 token `enable_dolphin_proto`」不够敏感
- Next: BM25 + 向量混合（RRF），并提高标识符查询的关键词权重

### BC-2（可选）标题块有时当正文
- Query: Peewee 怎么连 openGauss B 兼容模式？
- Actual top2: 全文标题+用途说明，信息量低
- Hypothesis: 标题块短且与主题词高度重合
- Next: 评测时看是否应降权纯标题块

## 2026-09-14 混合检索（BM25+RRF，pool=10，k=3）

### BC-1 已缓解
- Query: enable_dolphin_proto 是干什么的？
- 纯向量 top1: 兼容问题故事 A（错），dist=0.469
- 混合 top1: 2. 连接前提（对），bm25_rank=1, vec_rank=3
- 结论: BM25 把标识符查询拉回来了

### BC-3 混合导致语义查询回归
- Query: Peewee 怎么连 openGauss B 兼容模式？
- 纯向量 top1: 3.1 连接（合理）
- 混合 top1: 标题+用途说明（信息量低）；3.1 连接未进 top3
- 原因: RRF 偏好双路共识；单路冠军在另一路很后时会掉名次
- Next: 无标识符时偏向向量（加权 RRF 或 fallback 纯向量）

### 加权 RRF 后（无标识符 w_vec=0.75；有标识符 w_vec=0.45）
- Q1: 3.1 连接 回到 #2（纯向量 rank1）；#1 仍为标题块
- Q2: enable_dolphin_proto 仍 top1=连接前提
- Q3: 保持
- 遗留: 短标题块易靠前；后续可对纯标题块降权或增大 pool

## Phase4 检索层拒答阈值校准
- 数据来源：src/peek_signals.py（2026 实测）
- 库内 max_bm25: 4.37 / 13.80 / 14.47；min_dist: 0.27 / 0.35 / 0.47
- 库外天气：max_bm25=1.24；min_dist=0.65
- 规则：no_hits 或 (max_bm25 < 2.0 且 min_dist > 0.55) → 拒，不调 LLM
- 口径：小样本标定，非生产阈值；语料变大后需重跑 peek_signals