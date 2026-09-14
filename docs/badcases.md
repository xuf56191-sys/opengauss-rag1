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