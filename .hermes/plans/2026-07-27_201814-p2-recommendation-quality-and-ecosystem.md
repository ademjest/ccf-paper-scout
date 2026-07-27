# P2 推荐质量与生态扩展实施方案

> **For Hermes:** P0/P1 稳定后分阶段实施；每项功能必须有离线评测或明确用户价值，不为“AI 化”而堆依赖。

**目标：** 在保持默认低资源模式的同时，提高语义相关性、多样性、早期发现能力、反馈学习和多渠道使用体验，形成可扩展的 `v0.3+` 产品路线。

**架构：** 默认 lexical 排序器继续零强制 ML 依赖；通过 extras 提供 ONNX/embedding；候选采用多路召回和 MMR；反馈写入统一状态库；数据源和投递通过 adapter 扩展；正式核验与早期候选严格分频道。

**技术栈：** Python、SQLite、可选 ONNX/SentenceTransformers、OpenReview/Semantic Scholar/Papers with Code API、邮件/Telegram/飞书。

---

## 完成定义

- 推荐质量有离线基准和用户反馈指标，而非主观调参。
- 报告能解释“与哪些 Zotero 论文相似”和“命中哪个兴趣方向”。
- Top 20 不被单一 venue/主题垄断，可配置探索比例。
- 支持 early 与 verified 两类候选，绝不混淆录用状态。
- 用户反馈可持续影响后续排序。
- 可选语义模型不会破坏默认零依赖运行方式。

## Task 1：建立离线推荐评测集

**目标：** 为算法改进提供可重复的质量基线。

**文件：**
- 创建：`evaluation/README.md`
- 创建：`evaluation/sample_judgments.jsonl`
- 创建：`scripts/evaluate_ranking.py`
- 创建：`tests/test_evaluation.py`

**数据格式：**

```json
{"profile_id":"rl-agent","paper_id":"...","relevance":3,"reason":"agentic RL"}
```

**指标：** Precision@5/10/20、Recall@K、nDCG@K、MRR、venue/topic coverage、重复率、摘要补全覆盖率。

**要求：** 评测样本不得包含私人 Zotero 数据；提供合成或经授权的公开样本。

## Task 2：实现标准 lexical baseline

**目标：** 用可解释且规范的算法替换未校准的词项贡献求和。

**候选：**

- TF-IDF cosine；或
- BM25（完整 `k1/b` 与文档长度归一化）。

**要求：**

- 统一连字符、大小写、单复数和常见缩写；
- 显式兴趣短语配置化；
- 分数归一化；
- 输出 top contributing terms；
- 与旧算法在离线集上对比，指标不下降才切默认。

## Task 3：Top-K Zotero 论文依据

**目标：** 不把整个文库压成一个混合画像。

**实现：** 候选分别与 Zotero 条目比较，取最相关 3–10 篇，并结合时间权重。

**报告：**

```text
推荐依据：
- 与 ReAct 相似度 0.82
- 与 Toolformer 相似度 0.76
- 命中显式方向：LLM Agents
```

**隐私：** 相似度在本地计算，默认报告可只显示标题；允许隐藏私人标题。

## Task 4：多路召回与 enrichment 公平性

**目标：** 避免标题 top-N 自强化偏置。

**召回池：**

```text
lexical top-N
每个显式兴趣方向 top-N
每个 venue 新鲜候选 top-N
作者/引用图谱候选 top-N
随机探索候选 N
```

合并去重后再补摘要和统一排序。记录每篇候选的 recall channel，评估各通道贡献。

## Task 5：MMR 多样性和配额

**配置：**

```json
"diversity": {
  "enabled": true,
  "lambda": 0.75,
  "max_per_venue": 5,
  "max_per_topic": 5,
  "exploration_ratio": 0.15
}
```

**验收：** relevance 指标基本不下降，venue/topic coverage 提升，报告避免 20 篇全部来自同一小主题。

## Task 6：可选语义 embedding 后端

**安装：**

```bash
pip install 'ccf-paper-scout[semantic]'
```

**后端：** 优先轻量 ONNX/BGE-small/MiniLM；向量增量缓存到 SQLite。

**接口：**

```python
class Ranker(Protocol):
    def rank(self, profile, candidates) -> list[ScoredPaper]: ...
```

保留 `lexical` 默认后端；CI 至少验证接口契约，不在普通 PR 下载大模型。

## Task 7：显式反馈闭环

**反馈类型：** like、dislike、save、read、not_relevant、exclude_topic。

**入口：**

```bash
ccf-paper-scout feedback <paper-id> like
```

邮件/Telegram/飞书使用签名反馈 URL 或按钮。状态库记录反馈来源、时间和 run ID。

**学习策略：**

1. 少量反馈：规则权重与正负关键词；
2. 足够样本：Logistic Regression 或轻量 pairwise ranker；
3. 始终保留探索比例，避免 filter bubble。

## Task 8：早期发现频道

**目标：** 缓解 DBLP 正式索引延迟，但不降低资格表述可信度。

**数据源：** OpenReview、官方 accepted lists、arXiv（只作预印本元数据）。

**频道：**

- `verified`：DBLP/官方 proceedings 已核验；
- `early-accepted`：官方/OpenReview 显示 accepted，DBLP 未收录；
- `preprint-watch`：仅主题相关，不声称 CCF-A 录用。

报告必须显著展示 evidence 和 verified_at。

## Task 9：期刊增量和引用图谱

**期刊：** OpenAlex source ID + ISSN-L + Crossref DOI/online-first 日期；明确 online-first 与卷期发表的差异。

**图谱扩展：** Semantic Scholar references/citations/related papers；扩展结果再次经过 venue 资格门。

**新论文引用信号：** 按年份/领域 percentile 归一化，引用只作小权重，不压倒新鲜度。

## Task 10：代码、数据与可复现性信号

**数据源：** Papers with Code、论文官方仓库、开放数据链接。

**报告字段：** code URL、dataset、benchmark、license、是否官方实现、最近更新时间。

这些是实用性信号，不包装成论文质量证明。

## Task 11：多投递渠道与交互报告

**适配器：** SMTP、Telegram、飞书、企业微信、静态 HTML。

**要求：**

- 共用同一事务性 delivery interface；
- 支持按钮反馈；
- Markdown/HTML 邮件中展示双语标题、摘要、证据和推荐依据；
- 投递失败可重试；
- 同一 run 的 idempotency key 防重复。

## Task 12：Zotero 写回

**可选功能：** 将用户确认的论文写入指定 Zotero collection，而不是自动污染主库。

**安全：** 默认只读；写权限必须显式启用并使用独立 API key；执行前显示目标 collection 与条数。

## Task 13：用户界面与可观测性

**CLI：**

```bash
ccf-paper-scout inspect-zotero
ccf-paper-scout list-venues
ccf-paper-scout preview
ccf-paper-scout run
ccf-paper-scout feedback
ccf-paper-scout runs
```

**可观测性：** JSON 日志、阶段耗时、API 请求/缓存命中、错误分类；不记录 API key、私人摘要或完整 LLM prompt。

## Task 14：实验和发布策略

每项算法变化必须记录：

```text
baseline metrics
new metrics
latency/memory
API calls/cost
failure rate
privacy impact
```

建议版本：

```text
v0.3：规范 lexical + Top-K 依据 + MMR
v0.4：反馈闭环 + 多投递渠道
v0.5：OpenReview early channel + 期刊增量
v0.6：可选 semantic backend + 评测报告
v1.0：稳定配置、迁移、隐私与兼容性承诺
```

## P2 验收门

```text
[ ] 离线评测集和指标公开
[ ] 标准 lexical baseline 优于或不弱于旧算法
[ ] Top-K Zotero 推荐依据
[ ] 多路召回和 MMR 多样性
[ ] 反馈能影响后续排序
[ ] early/verified 状态严格分离
[ ] 可选 semantic 后端不影响零依赖默认模式
[ ] 多渠道投递遵守事务与幂等
[ ] 性能、成本、隐私影响有量化记录
```
