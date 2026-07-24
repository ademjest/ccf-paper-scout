# 调研与设计说明：Zotero → CCF-A 兴趣论文推送

## 1. 结论先行

最合理的系统不是“把 arXiv 换成另一个大而全的数据源”，而是把流程拆成三层：

1. **候选发现层**：DBLP、OpenAlex、Semantic Scholar、OpenReview/官方 proceedings 各司其职。
2. **质量资格层**：CCF-A venue 白名单 + 稳定标识（DBLP key/ISSN/DOI/官方 accepted list）硬过滤。
3. **兴趣排序层**：Zotero 正反馈 + 明确负反馈进行轻量内容排序；只对少量候选调用 LLM 生成摘要。

本 MVP 选择 **DBLP 正式收录 → DBLP key 与 CCF-A 表双检 → OpenAlex 补摘要 → 本地稀疏排序**。这是在精度、资源、可解释性和维护成本之间较稳妥的第一版。

## 2. 现有项目与可借鉴技术

### TideDra/zotero-arxiv-daily

- URL：https://github.com/TideDra/zotero-arxiv-daily
- 已实查：Zotero Web API 读取条目；arXiv/bioRxiv/medRxiv retriever；sentence-transformers 本地 rerank 或 embedding API；LLM 生成 TL;DR；GitHub Actions + SMTP。
- 优点：部署简单、Zotero 接入成熟、流水线清楚、支持 collection 路径筛选。
- 局限：候选质量由预印本源决定；推荐是较简单的摘要相似度；默认本地方案依赖 torch/sentence-transformers，并会提取候选全文，资源明显高于本 MVP。
- 最值得复用：Zotero 接入、GitHub Actions、邮件渲染和 source/reranker adapter 结构。

### ccfddl/ccf-deadlines

- URL：https://github.com/ccfddl/ccf-deadlines
- 提供会议 YAML：简称、领域、CCF/CORE/TH-CPL 等级、DBLP key、历年会议链接与日期。
- 适合作为“会议白名单及 DBLP key”的辅助维护源；不是论文数据源。

### WenyanLiu/CCFrank4dblp

- URL：https://github.com/WenyanLiu/CCFrank4dblp
- 浏览器扩展会在 DBLP、Semantic Scholar、Google Scholar 等页面标注 CCF 等级。
- 数据中包含 DBLP path、完整 venue 名、简称与等级的对应关系；本 MVP 派生出 91 个 CCF-A 条目（60 会议、31 期刊）。
- 适合作为机器可读映射种子，但应定期对照 CCF 官方目录，而不能冒充官方 API。

### 其他相关工具

- DelinQu/zotero-arxiv-feishu-llm：https://github.com/DelinQu/zotero-arxiv-feishu-llm
  - Zotero Web API + 当日 arXiv + OpenAI-compatible embedding/LLM，支持飞书和企业微信通知；适合复用中文消息卡片与反馈按钮，但仍缺 venue 质量门。
- Marverlises/Paper-Agent-Zotero：https://github.com/Marverlises/Paper-Agent-Zotero
  - 支持 Zotero 或本地 PDF 语料，包含 PDF 下载、版面/图片分析和 LLM 总结；能力丰富但依赖和资源显著更重。
- tianbaiting/bib-arxiv-daily：https://github.com/tianbaiting/bib-arxiv-daily
  - 通过 Zotero 导出的 BibTeX 建模，使用本地 BGE-small 和 GitHub Actions；不需付费 LLM，但兴趣库不能自动同步，且要防止公开仓库泄露个人书目。
- yuandong-tian/arXiv_recbot：https://github.com/yuandong-tian/arXiv_recbot
  - 没有 Zotero 接入，但 Telegram 点赞/点踩/评论和累积反馈后训练个人偏好模型的闭环非常值得借鉴。
- karpathy/arxiv-sanity-lite：https://github.com/karpathy/arxiv-sanity-lite
  - 经典 TF-IDF/个人库推荐参考，但维护已停滞，部署比每日脚本重。
- yilewang/llm-for-zotero：https://github.com/yilewang/llm-for-zotero
  - 更偏“基于个人 Zotero 的研究 Agent”，适合借鉴库内检索与问答，而不是高质量新论文资格审核。
- MuiseDestiny/zotero-gpt：https://github.com/MuiseDestiny/zotero-gpt
  - Zotero 内 LLM 交互插件，可借鉴 UI/插件集成；不是候选数据源。
- Zotero RSS / Saved Search / Better BibTeX：可作为本地工作流入口，但不解决 venue 质量问题。

综合 GitHub 调研，尚未发现一个成熟项目可以开箱即用地同时满足：Zotero 自动兴趣建模、多源最新召回、CCF-A/venue 可靠识别、质量排序和显式反馈学习。

## 3. 数据源比较

| 数据源 | 最适合的角色 | Venue/正式性 | 摘要/引用 | 时效 | 主要问题 |
|---|---|---|---|---|---|
| DBLP | 计算机正式论文骨架、venue 核验 | 强；record key 稳定 | 摘要弱；引用弱 | 正式收录后 | 新论文可能有索引延迟 |
| OpenAlex | 摘要、DOI、引用、OA 链接补全 | 中强；source ID/ISSN 可用 | 强 | 较快 | 会议 source 拆分、名称规范化需维护 |
| Semantic Scholar | related/recommendation、引用图谱 | 中 | 强 | 较快 | 推荐 API/限流可能需 key；venue 不能单独当 CCF 证据 |
| OpenReview | ICLR/NeurIPS 等审稿与录用状态 | 对所覆盖会议强 | 摘要强 | 非常快 | 覆盖范围不完整；venue schema 各异 |
| 官方 proceedings | 最终 accepted-paper 权威源 | 最强 | 视会议而定 | 常最快/最准 | 每个会议适配器不同，维护成本高 |
| Crossref | DOI/期刊元数据补全 | ISSN 较强 | 摘要不稳定 | 快 | 会议名称与录用类型不够统一 |
| arXiv | 早期发现、全文 | 不能证明录用 | 摘要/全文强 | 最快 | 质量层次不齐；comment 不能作为权威录用证据 |
| Unpaywall | DOI → 合法 OA 全文 | 不负责质量 | OA URL 强 | 快 | 仅补全文，不做推荐/venue 认证 |
| CORE | OA 全文聚合 | 中 | 全文强 | 中 | API/许可/覆盖与个人用量需单独评估 |

### 推荐组合

- **MVP/低维护**：DBLP + CCF-A DBLP key + OpenAlex 摘要。
- **更早发现**：增加 OpenReview 和重点会议官方 accepted list。
- **扩大期刊覆盖**：OpenAlex/Crossref 用 ISSN 白名单增量拉取，再用 DOI/DBLP 交叉验证。
- **相关论文扩展**：仅对已经通过 CCF-A 资格门的 seed 使用 Semantic Scholar references/citations/recommendations，然后再次过质量门。

## 4. 为什么 venue 字符串匹配不够

应按下列优先级识别 venue：

1. 官方 accepted list 中的 paper ID；
2. DBLP record key，如 `conf/nips/...`、`journals/tpami/...`；
3. ISSN/ISSN-L（期刊）或固定 OpenAlex source ID；
4. DOI prefix + proceedings/issue 元数据；
5. 规范化名称/简称，只能作为召回，不应单独通过资格门。

还应明确排除或单独标记：workshop、demo、doctoral consortium、Findings、short paper、industry track、dataset track。它们是否算目标 CCF-A 正会论文，需要由用户策略决定，不能因为顶会品牌名出现就自动等价。

## 5. 推荐模型设计

### 兴趣信号

正反馈：

- Zotero 指定 collection 中的论文；
- 最近加入且有摘要；
- 用户标记“已读/重要”的 tag；
- 点击、收藏、加入 Zotero、打开全文。

负反馈：

- 明确“不感兴趣”；
- 连续跳过；
- 排除 tag/collection；
- 已推荐、已存在库中。

不建议把整个 Zotero 多年文库等权平均。较好的个人画像是：

- 当前课题 collection 的 centroid；
- 最近 30/90 天 centroid；
- 3–8 个自动主题簇，各自保留 top-N；
- 探索配额约 10%–20%，防止推荐越来越窄。

### 排序分数建议

```text
score = 0.55 * topic_similarity
      + 0.15 * recent_interest_similarity
      + 0.10 * citation_graph_proximity
      + 0.08 * author_affinity
      + 0.07 * novelty/diversity
      + 0.05 * freshness
```

CCF-A 不放入软分数，而应作为**硬门槛**。新论文引用数低，因此引用指标只应做小权重或按同年/同领域 percentile 归一化。

### 低资源实现路径

- 第一阶段：标准库 TF-IDF/BM25，标题+摘要，CPU/内存占用极低。
- 第二阶段：需要更好语义时，换成小型 ONNX embedding（如 MiniLM/BGE-small），只增量编码；SQLite 保存向量，不急着部署向量数据库。
- 第三阶段：只把 top 10–20 候选交给 LLM 做“为什么推荐”和 TL;DR，避免全文全量 LLM 成本。

## 6. 生产化架构

```text
Zotero API/local export
        ↓
Interest Builder ─── Feedback Store (SQLite)
        ↓
Candidate Adapters
  ├─ DBLP strict
  ├─ OpenAlex journal
  ├─ OpenReview early
  └─ Official proceedings
        ↓
Venue Resolver + CCF Policy Gate
        ↓
Metadata Enricher (OpenAlex/S2/Unpaywall)
        ↓
Dedup (DOI > DBLP key > arXiv ID > normalized title)
        ↓
Lightweight Ranker + diversity
        ↓
Markdown/Email/Telegram/Zotero collection
```

SQLite 表建议：`papers`、`external_ids`、`venues`、`interest_items`、`recommendation_runs`、`feedback`。缓存 API 响应并用 ETag/`If-Modified-Since` 或增量日期，能将每日调用与计算压到很低。

## 7. 本次实现

目录：`/home/zlw/ccf-paper-scout`

具备：

- Zotero Web API 或本地兴趣 JSON；
- CCF-A 派生白名单（91 条）；
- DBLP 正式候选与 record-key 硬校验；
- DOI → OpenAlex 摘要补全；
- 本地稀疏相关度与可解释关键词；
- 已推荐去重；
- Markdown 报告；
- 纯标准库，无 GPU/torch/LLM/向量库。

真实运行验证：从 AAAI 2025 和 NeurIPS 2025 获取 20 个通过资格门的候选，使用 3 篇示例兴趣论文生成 5 条推荐；4 个单元测试全部通过；非法 venue key 会以退出码 2 被硬拒绝。

## 8. 下一步优先级

1. 用你的 Zotero 指定 collection 实跑并采集 1–2 周反馈；
2. 先增加“已经在 Zotero 中”的 DOI/标题去重；
3. 接入 OpenReview/官方 accepted list，解决顶会论文更早发现；
4. 增加期刊 ISSN 白名单 + OpenAlex cursor 增量；
5. 加 MMR 多样性和主题配额；
6. 最后才考虑小型 embedding 和 LLM TL;DR。
