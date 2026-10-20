# 主会准入与 LLM／RL 关系改进：实施与测试说明

日期：2026-10-20。实施分支：`update-paper`，不直接合并 `main`。

对应设计：`docs/main-conference-and-rl-llm-relation-plan.md`。本文区分“程序功能已实现”和“需要人工验收”，不会用单元测试代替推荐准确率评估。

## 1. 已实现的变化

### 主会资格硬过滤

- SPARQL 发现直接读取实际 `dblp:publishedAsPartOf`，保存真实 proceedings URI。
- 新增 `data/main_proceedings.json`：31 条实际论文集记录、八个会议两个年份的覆盖状态、68 个 AAAI 官方技术轨道卷期映射、来源和核验日期。
- `verified` 才能进入摘要补全、embedding、关系复核、翻译；`excluded` 和 `unresolved` 均不进入推荐。
- 每轮重新核验未投递候选。旧库缺论文集信息时，每次最多回填 1,000 篇、每批 100 篇，不清空候选、不重置发现游标或投递去重。
- 回填暂时失败时保留未处理候选；成功查询但无关联者七天后再查，未查询候选优先，避免长期饥饿。
- 摘要补全后再核验一次，拦截摘要明确自称 tutorial 的非研究内容。
- 候选保存 `proceedings_id`、`track`、`main_conference_status`、`verification_source`、`exclusion_reason`、`qualification_version`。不删除排除项，不修改历史 delivered 状态。

### 可审查的准入覆盖

| 会议 | 当前准入情况 |
| --- | --- |
| ACL 2025/2026 | 已核实 Long / Short；Findings、Tutorial、Demo、Student Research Workshop 排除；Industry 未核实则暂缓 |
| AAAI 2025/2026 | 混合总卷不能直接放行；根据官方目录的卷期映射，仅技术轨道放行；特殊轨道等未核实者暂缓 |
| CVPR 2025/2026、ICCV 2025 | 已识别主会论文集；对应已知 Workshop 论文集排除 |
| ICML 2025 | 已识别主会论文集；ICML Position 轨道暂缓，不按名称自动放行 |
| NeurIPS 2025 | 年度卷是混合入口；同时要求实际 proceedings 关联及官方 Main Conference 目录可验证的 `-Abstract-Conference.html` 文章轨道标识，Creative AI／其他未核实轨道不放行 |
| SIGIR 2025/2026、KDD 2025/2026 | 已记录真实论文集，但混合内容尚无足够细的研究轨道证据，默认暂缓 |
| ICML 2026、NeurIPS 2026、ICCV 2026 | 本次公开数据核验未建立可准入论文集，不伪造标识，保留未核实状态 |

证据来源是实际 SPARQL 关联、论文集标题和出版入口。AAAI 进一步核对官方目录：

- `https://aaai.org/proceeding/aaai-39-2025/`：第 1–25 期是 Technical Tracks。
- `https://aaai.org/proceeding/aaai-40-2026/`：第 1–43 期是 Technical Tracks。

这里的匹配是出版商明确的卷期与轨道关联，不是用文章编号后缀或 DOI 年份推断会议资格。例如公开 DBLP 记录 `conf/aaai/000100025` 的 DOI 对应卷 39 第 20 期；准入同时要求实际 AAAI 2025 proceedings 关联。

NeurIPS 官方年度入口本次实际展示 Creative AI，不能整卷批准。已核对 `https://papers.nips.cc/paper_files/paper/2025/vol38-main-conference` 的 Main Conference 标题、`data-track="conference"` 与文章路径标识。仅放行这类明确 Conference 记录；目录也出现 Datasets and Benchmarks，但本版仍保守暂缓其他轨道，后续另行审查。

DBLP 网页在本次检查中仍返回人机验证页面，因此程序继续使用公开 SPARQL，不依赖抓取 DBLP HTML，也不绕过人机验证。AAAI OJS 单页在本地存在 TLS 连接失败，轨道映射的证据来自可访问的官方 AAAI 总目录，不宣称抓取过每篇 OJS 文章。

**限制：这不是八个会议全部论文均已完成轨道核实。** 特别是 SIGIR/KDD，本版宁可减少推荐，不整卷放行。新年份发现仍会自动执行，但新增论文集必须补充经过审查的白名单证据，不能自动继承旧年份资格。注册表支持 `papers` 中的精确记录级轨道证据，供后续逐轨道补齐。

### LLM／RL 作用方向

- 新增 `llm_for_rl`、`rl_for_llm`、`llm_multiagent_rl`，允许多标签。
- 独立保存是否存在实质 MARL 贡献；“多个 agent”不直接等于 MARL。
- 三组各三个英文原型，以组内最大余弦相似度聚合；记录三组分数及有符号方向分差。
- 复用固定 BGE 模型、同一编码器与已有论文向量，不新增模型训练、GPU 或向量数据库。
- 九条原型单独缓存。原型、聚合方式、模型或分块签名变化会使关系校准失效；论文文本改变会重编码。
- 未经人工批准的阈值不参与确定关系分类；高双向得分、小分差和多智能体边界进入独立复核。
- 有多智能体线索的候选不会仅因单组 RL→LLM 得分突出而直接排除，需要关系复核检查实质 MARL。

关系复核与翻译分开，复用现有 API 的端点与模型，但独立开关、独立缓存，每轮最多 20 次请求。要求回答被优化模型、LLM 角色、RL 角色、MARL 贡献，且附摘要准确原句。无摘要、引用不匹配、API 失败或超出预算均暂缓，不能根据标题补造方法。

LLM 只是背景时，可在提供摘要证据后判为 `not_applicable`；不会因为出现 LLM 一词就永久排除传统 RL。模型明确回答不确定的结果缓存七天，避免每天重复占满前 20 个名额；网络或格式失败不会缓存为确定结论。

### 真正作用于准入的偏好策略

`relation_policy.mode=production` 时：

| 判断 | 行为 |
| --- | --- |
| 通用 RL / 传统 MARL，未发现 LLM 作用关系 | 保留原有兴趣与质量门槛 |
| LLM→RL | 保留 |
| 仅 RL→LLM，且无实质 MARL 证据 | 排除，不能通过宽泛 RL 匹配绕过 |
| 有实质 MARL 原句证据 | 保留，不一刀切排除 LLM 多智能体研究 |
| 混合关系 | 经过独立原句复核后可以保留；仅 embedding 多标签不直接放行 |
| 证据不足 | 暂缓，不用弱相关内容补足数量 |

`mode=shadow` 只计算这些判断和反事实选文，不改变当前关系准入。**默认仍是 shadow，符合设计第三阶段“先审查再切换”。主会硬过滤则默认立即生效。**

翻译结果及翻译缓存只允许写入卡片字段，不能覆盖资格、关系或策略决定。报告的推荐解释由已判定方向和关系生成，不要求“与全部方向高度相关”。

## 2. 你在 GitHub Actions 上如何测试

选择 `Paper Scout Manual`，确认运行分支为 **update-paper**。不要先选会实际发邮件的业务 `mode=production`。

### 第一次：影子预览

```text
mode: preview
semantic_mode: shadow
relation_mode: shadow
max_results: 10
```

读取私有状态仓库 `reports/<run-id>/` 下的 Markdown 与 diagnostics。此次不会发邮件，不会更新生产 SQLite、发现游标和 delivered 状态；私有仓库只保存报告。

重点查看：

- `qualification.statuses`：verified / excluded / unresolved 数量。
- `qualification.backfill`：本轮旧库回填与剩余数量。
- `qualification_deferred`：被排除或未核实的记录及具体原因。
- `semantic.relations`：原型签名与缓存命中；`semantic.seconds` 是该阶段实测时间。
- `papers[].relation_scores / relation_margin / relation_review / relation_decision`：分数、角色原句和拟执行策略。
- `selected_ids`：当前实际选文；`relation_policy_selected_ids`：若正式启用关系策略会选择的论文。
- `relations.review_requests`：本轮独立关系复核的请求数，不是最终翻译调用数。

### 第二次：正式关系规则，但仍不发邮件

```text
mode: preview
semantic_mode: shadow
relation_mode: production
max_results: 10
```

这里的 `relation_mode=production` **不等于**业务 `mode=production`：关系规则会拦截纯 RL→LLM 和不确定项，但 preview 仍不会发邮件或修改生产状态。没有关系校准文件时，BGE 只给出诊断分数；LLM 相关候选靠独立原句复核，传统 RL 继续使用现有兴趣筛选。

若只想先验证规则与 API、节省模型安装时间，可以将 `semantic_mode=off`；这时不计算关系原型，只执行出版资格、词项兴趣和角色原句复核。

每次 preview 的新增摘要和缓存都随隔离副本丢弃，因此重复预览可能重复产生 API 费用；两次预览不能被视为完全同缓存条件的性能对比。

### 第三次：积累状态和人工验收

旧库回填、关系与摘要缓存的生产持久化发生在业务生产运行；preview 不会悄悄写回它们。`discover` 只推进发现并保存新抓取的 proceedings 字段，不运行关系复核。

审查后若接受关系策略，再由你选择业务 `mode=production` 做真实投递验证。自动 Daily 的变量：

```text
SEMANTIC_MODE=shadow
RELATION_MODE=production
```

在关系规则尚未审查前，保留 `RELATION_MODE=shadow`。这两项互相独立，不需要打开兴趣排序的 `semantic_mode=production`。分支测试不会自动更改 main 的定时业务，定时任务依然运行默认分支。

## 3. 独立校准与人工样本

没有虚构 60–100 篇人工标签，也没有自动批准生产阈值。现阶段程序验收已覆盖，但设计第三阶段的推荐准确率验收仍需你审查。

从影子报告抽取独立样本，分别标注出版资格、关系、实质 MARL 和是否希望收到。校准集与留出测试集使用两个文件，不能用同一批反复选阈值再报告最终准确率。

人工标签文件格式（以下 ID 是格式占位符，不是真实论文判定）：

```json
[
  {
    "id": "conf/venue/replace-with-real-record",
    "eligibility": "verified",
    "relations": ["llm_for_rl"],
    "substantial_marl": false,
    "wanted": true
  }
]
```

```bash
python scripts/evaluate_roles.py --diagnostics recommendations.diagnostics.json --labels role-test-labels.json
```

工具报告有标签选文相关性、关系混淆、主会误放行、MARL 误排除／暂缓和未覆盖标签数量；未标注记录不自动当负例，也不会自动生成“approved”。请保存在私有目录，不提交个性化标签到公开仓库。现有 `evaluation-labels.json` 的 true/false 格式不变，仍服务原有相关性评估。

关系阈值文件为私有 `state/relation-calibration.json`，已加入状态文件白名单：

```json
{
  "approved": false,
  "relation_signature": "复制本次 diagnostics 的 semantic.relations.signature",
  "thresholds": {"positive": 0.0, "margin": 0.1}
}
```

上述数值**只是结构示例，不是可用默认阈值**。在人工校准前不要创建这个文件；只有根据校准样本选定阈值并通过独立留出检查后，才填写实际数值并将 approved 设为布尔 true。错误批准值、旧模型／原型签名和非法数值会报错，而不是静默降级。

## 4. 验证结果与未做事项

本地验证：

- 69 个自动测试通过，包含原有 39 个测试，以及新增出版资格、关系策略、缓存、Actions 配置和正向流水线测试。
- Python 编译检查、Ruff F 类检查、依赖一致性检查、工作流 YAML 解析通过。
- 实际公开 DBLP 联网测试：AAAI 2026 两轮共保存四篇，分页 0→2→4；四篇通过官方技术卷期准入；单独旧记录关联查询一致。
- 实际 CPU BGE 测试：两篇合成文本及九个关系原型；第二轮两篇论文和九条原型全部命中缓存，无论文重编码。本地模型文件已预下载条件下，该次首轮约 4.2 秒，**不代表全量生产耗时**。
- CI 会继续验证 Python 3.11/3.12/3.13 和真实 CPU 模型，并检查安装包在仓库目录外能读取论文集数据文件。

未替你执行：真实 Zotero 推荐质量评估、私有候选全库迁移、付费 LLM 实际效果评估、SMTP 发送、main 合并、GitHub Secrets / Variables 修改。它们由你的分支实际测试和审查决定。

## 5. 代码入口

- `src/ccf_paper_scout/proceedings.py`：资格核验、旧候选回填、保守准入。
- `src/ccf_paper_scout/data/main_proceedings.json`：真实论文集和轨道证据。
- `src/ccf_paper_scout/relations.py`：关系原型、校准约束、独立角色审核和策略。
- `semantic.py`：同一编码器与论文向量复用；`selection.py`：最终硬门槛。
- `paper_scout.py`：流程接入、私有诊断、报告和翻译字段保护。
- `scripts/evaluate_roles.py`：独立人工标签评估，不自动批准校准。

后续补 SIGIR/KDD 或新年份时，应取得实际论文集和轨道证据、更新注册表版本及回归样本，不能通过关闭主会准入来“恢复推荐数量”。
