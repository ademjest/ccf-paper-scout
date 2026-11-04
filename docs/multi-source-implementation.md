# 多来源采集与本地搜索实施说明

实施与实测日期：2026-11-04。对应设计：[multi-source-upgrade-plan.md](multi-source-upgrade-plan.md)。本文区分已交付功能、已验证访问和后续观察；设计稿中的接口草案不等同于当前配置字段。

## 1. 本次解决的问题

DBLP 超时不再阻断整个采集阶段。各来源在独立进程中运行，有请求次数、单请求超时和真实耗时上限；父进程收到完整页后提交 SQLite，失败的来源不拖住正常来源。全部实时来源失败时，仍可从已核验缓存筛选，并在报告中注明来源状态。数据库或投递事务错误仍然中止运行。

正式论文可以凭已审查的官方目录成员证据通过主会核验，不再要求先在线取得 DBLP ID。OpenAlex、arXiv 等搜索接口返回的会议名称本身不能证明主会资格。Daily 继续执行兴趣、摘要、主会、Zotero 和历史投递去重检查，默认最多 5 篇，不凑数；语义和关系策略的启用方式保持原有设置。

本地增加手动搜索 CLI 和网页，支持缓存先返回、限时外部补充、逐来源状态、取消、相关性标注、收藏、已读。手动搜索不读取 Zotero、不调用 LLM、不发送邮件。当前手动搜索使用已有词项排序，未声称语义推荐效果已经提升。

## 2. 代码与状态结构

| 文件 | 职责 |
|---|---|
| `source_http.py` | HTTPS、响应大小、可信域名、重试与 Retry-After、配额头、凭据脱敏 |
| `source_worker.py` | 单来源进程，原子输出完整页和结果，不连接数据库 |
| `sources.py` | 并发上限、父进程硬截止时间、取消与回收、健康冷却、事务提交 |
| `source_adapters.py` | 11 个来源的归一化记录、目录扫描与精确查找 |
| `identity.py` / `state_store.py` | DOI／URL／来源 ID 别名、旧 ID 保留、冲突暂缓、迁移 |
| `proceedings.py` / `data/official_sources.json` | 版本化官方目录证据及主会准入 |
| `search.py` | 手动检索、范围过滤、独立搜索库、来源 doctor |
| `web.py` / `data/search.html` | 仅本机可访问的 HTTP 服务与无前端框架网页 |

沿用现有 `papers` 和 `candidate_papers`，新增别名、题名索引、来源记录、任务断点、来源健康、运行诊断和反馈表。正式证据保存在候选及来源记录的 JSON 中，没有再建重复的独立证据表。每页的记录、别名和断点由同一事务提交。

旧 DBLP 主键不改名。强别名命中时合并到旧主键；多个身份命中、冲突 DOI、只有相同题名但缺强别名时暂缓，保留 `source_records.identity_review_pending`，不进入新增投递。暂缓不是自动合并，也不是“未发现重复”。历史库中完全没有 DOI／主页／题名的记录仍无法凭空恢复关联，需单独核验。

正常取消记作 `cancelled`，不会触发来源冷却。超时等故障冷却至少 30 分钟；认证、挑战或协议错误至少 24 小时。当前按来源隔离，连接器的请求域名不重叠。不要新增共享同一 host 的连接器而不补共享限流。

## 3. 每个来源的实际接口与用途

| 来源 | 已实现的请求 | 使用方式与准入限制 | 本轮验证 / 默认状态 |
|---|---|---|---|
| DBLP | `GET https://sparql.dblp.org/sparql`，复用现有 SPARQL 查询和解析 | 按 venue/year 增量扫描，承接旧 offset；正式 proceedings 规则沿用 | 离线契约及超时隔离；Daily 默认开 |
| PMLR | `GET https://proceedings.mlr.press/v267/`；随后读取成员 `.html` 页面 | ICML 2025 官方目录；抽取 citation 元数据和摘要，保存目录 hash、规则版本和成员 URL | 真实闭环通过；Daily 默认开 |
| OpenAlex | `GET https://api.openalex.org/works?search=...&filter=publication_year:...`；精确 `GET /works/{URL编码的 DOI URL}` | 广泛发现、DOI 摘要补全；重建倒排摘要；官方主页/DOI 用于关联，不凭 venue claim 授予主会资格 | 匿名请求通过；Daily 默认开；推荐申请激活 key |
| NeurIPS | `GET https://proceedings.neurips.cc/paper_files/paper/2025/vol38-main-conference`；成员页面 | 仅 `data-track=conference` 且符合主会 URL 的条目，排除 position/datasets 等混轨道 | 解析契约通过，本轮真实请求超时；默认关 |
| ACL Anthology | `GET /volumes/2025.acl-long/`、`/volumes/2025.acl-short/`；成员页面 | 仅 ACL 2025 主会 long/short，不含 Findings；保留尾部斜杠，减少跳转开销 | 真实目录、摘要与准入通过；按需开 |
| CVF | `GET https://openaccess.thecvf.com/CVPR2025?day=all`；`/content/CVPR2025/html/..._paper.html` | CVPR 2025 主会目录，排除 workshop 路径 | 真实目录与准入通过；按需开 |
| AAAI | 读取现有审查表中 verified 的 `issue_source`，再读 `/index.php/AAAI/article/view/{id}` | 按已审查卷期/轨道采集，不靠 article number 或 DOI 年份猜资格 | 真实目录与准入通过；按需开 |
| arXiv | `GET https://export.arxiv.org/api/query`，Atom；关键词转成 AND 词项，提交日期范围 | 手动搜索；稳定 ID 去版本号、单独保存版本；标记预印本，不能自动进入 Daily | 真实搜索通过；手动默认开，Daily 默认关 |
| Crossref | `GET https://api.crossref.org/works?query.bibliographic=...`；精确 `GET /works/{DOI}` | DOI 身份和出版元数据，清理 JATS 摘要；主会资格仍需独立核验 | 真实搜索通过、精确查找契约测试通过；按需开 |
| Semantic Scholar | `GET https://api.semanticscholar.org/graph/v1/paper/search`，选取 title/abstract/externalIds 等字段 | 补充搜索；兼容两种历史 key 名；不推断主会资格 | 先前匿名探测 429；本轮未重复消耗请求，默认关 |
| OpenReview | `GET https://api2.openreview.net/notes?content.venueid=...` | 必须配置 `providers.openreview.venues`；结果一律视为投稿，未实现录用决定自动准入 | 先前匿名探测 403 challenge；不绕过，默认关 |

精确摘要查找支持 OpenAlex/Crossref DOI 和现有官方页面定位器支持的 PMLR/NeurIPS/ACL；CVF/AAAI 当前在目录采集时抽摘要，没有扩充旧页面定位器。查找与发现共享该来源的请求、重试、间隔和总时限。精确 DOI 响应必须匹配请求 DOI；官方查找核对题名与作者。

每来源先给旧候选补全保留少量额度：最多 `min(8, max_requests // 4, 剩余 metadata 预算)` 个候选，余下用于发现。精确查找的缺失和 404 记录 7 天后再尝试。整个多来源模式不再接着运行旧的 200 次串行摘要请求，避免后半段继续卡住。需要增加摘要覆盖时应提高相应来源总预算，并检查目录产出与精确补全的比例。

对摘要采用统一最低检查：至少 80 字符且不是已识别的书目信息占位文本；不合格文本视为缺失，不生成补写内容。这只是最低数据质量检查，不是对摘要真实性的完全验证。

### 当前覆盖边界

- 官方目录是明确审查的卷册清单，不自动按年份猜 URL。PMLR/NeurIPS/ACL/CVF 的新增目录以 2025 年为主；AAAI 年份来自既有卷期审查表。未登记年份返回 `not_configured`，DBLP 仍负责更广覆盖。
- DBLP 和官方目录持久保存断点；官方目录按上次检查时间轮换，已处理成员 URL 下轮跳过。
- OpenAlex 目前使用普通页码，小规模搜索每次重新取前几页。arXiv/Crossref/S2/OpenReview 当前只取有上限的第一页；不是全量索引或通用游标框架。
- 未实现 S2 batch、OpenReview 录用证据、自动跨源补齐所有 DOI 或预印本与正式版的模糊合并。
- 反馈已经持久保存，但尚未用于训练或自动调参，也未自动同步回 Daily 画像。

## 4. Windows 本地使用

在 PowerShell 中进入项目目录，使用项目虚拟环境：

```powershell
Set-Location E:\Agent\ccf-paper-scout
$env:PYTHONUTF8 = '1'
.\.venv\Scripts\python.exe -m pip install --no-cache-dir .
.\.venv\Scripts\ccf-paper-scout.exe serve --config config.example.json
```

浏览器打开 `http://127.0.0.1:8765`。默认选择 OpenAlex/arXiv，可以加入 PMLR 搜索已核验 ICML 论文。搜索最多补充 60 秒，单来源最多 30 秒/4 次请求；预算包括重试。点击“取消补充”会回收子进程，已经提交的结果保留。关闭服务用终端 Ctrl+C。

```powershell
# 无需 Zotero、SMTP 或 LLM
.\.venv\Scripts\ccf-paper-scout.exe search --config config.example.json --query "world model planning" --years 2025 2026 --sources openalex arxiv --output local-notes/search-result.json

# 只读本地候选结果，不做外部检索
.\.venv\Scripts\ccf-paper-scout.exe search --config config.example.json --query "reinforcement learning" --cached-only --scope verified_main

# 只检查来源配置与凭据是否存在，不打印 key
.\.venv\Scripts\ccf-paper-scout.exe doctor --config config.example.json --sources

# 明确要求联网探测；每来源最多 2 次请求，独立临时库
.\.venv\Scripts\ccf-paper-scout.exe doctor --config config.example.json --sources --network
```

推荐英文查询。当前词项算法对同义词、中文问题与长自然语言问题的处理有限。`verified_main` 只展示已核验主会；`published` 允许来源标为正式发表但主会未核验的记录；`all_with_preprints` 还展示预印本、投稿和状态未知记录，并明确标签。不同范围不改变 Daily 准入。

默认搜索库是配置文件同目录下的 `state/search.sqlite3`。首次创建时若存在 Daily 库，通过 SQLite backup 复制快照，包含 WAL 已提交内容；后续独立更新，不持续镜像 Daily。可设 `search.state_db` 改路径，但禁止与 `state_db` 相同。搜索快照含私有候选/反馈，应继续保留在忽略目录中。

### 本机 HTTP 接口

| 接口 | 输入／行为 |
|---|---|
| `POST /searches` | JSON：`query`、可选 `years: [2025, 2026]`、`sources: ["openalex", "arxiv"]`、`publication_scope`；返回 202 和 `id` |
| `GET /searches/{id}` | `state`、`results`、`ranking: lexical`、`discovery.providers`；约 700ms 轮询一次 |
| `DELETE /searches/{id}` | 取消外部补充，保留已经完成的结果 |
| `POST /papers/{URL编码ID}/feedback` | `relevance: relevant/irrelevant/unsure`、`read: bool`、`saved: bool`，按字段合并保存 |

仅监听 127.0.0.1；写请求需要页面提供的 `X-Scout-Token`，校验 Host/Origin，限制 JSON 大小；页面把题名与摘要按文本渲染。一次只运行一个检索任务，再提交返回 409。任务状态仅存在内存中，最近任务替代前一次，候选和反馈存 SQLite。此服务不面向公网部署或多用户认证场景。

## 5. Daily / Actions 配置

默认配置启用 `dblp,pmlr,openalex`，`sources.mode=production`。这是代码分支的待审查配置；不会自行改动 GitHub main 或创建/触发定时任务。合并之后 Daily 会继承该配置。

| 来源 | 请求上限 | 来源时限 | 备注 |
|---|---:|---:|---|
| DBLP | 12 | 120 秒 | 每分区默认最多 2 页 |
| PMLR | 26 | 90 秒 | 每卷最多 24 篇；有旧缺摘要候选时先用部分请求精确补全 |
| OpenAlex | 4 | 60 秒 | 每页 25，普通搜索默认每查询 1 页 |

并发上限 2，采集总时限 420 秒；它不是整条 Daily 流水线总时限，Zotero、embedding、关系评审和邮件卡片仍遵守各自现有设置。进程回收另有短暂宽限。更多来源可修改 `sources.enabled`，预算放在 `sources.providers.<name>`。不要把设计稿中的未实现字段直接复制进配置。

启用多来源时，`sources.providers.dblp` 控制新 DBLP worker 的预算；旧 `dblp.max_pages_per_run` 等仅用于 legacy 路径，不与新预算叠加。将 `sources.enabled` 设为 `[]` 可回到旧发现链路及其缓存降级。回退不删除新版产生的投递记录；旧发现模式不会新增选择只有 UUID 的官方源论文。

`sources.mode=shadow` 会把 SQLite 复制到临时库，执行多源采集并输出统计，随后丢弃采集写入。它不改变生产候选、别名、游标、来源健康；Daily 本轮用原缓存进行筛选。它不是同时重跑 legacy 网络采集的对照实验。严格排名实验仍需固定候选和摘要快照。

Manual 和 Daily 均已注入以下环境变量，密钥始终留在环境中：

- `OPENALEX_API_KEY`：GitHub Secret；可选但推荐申请并激活。没有 key 时只用当前允许的匿名普通查询，不使用游标。
- `SEMANTIC_SCHOLAR_API_KEY`：GitHub Secret；启用 S2 前申请并验证。运行时兼容历史名称 `SEMANTICSCHOLAR_API_KEY`。
- `CROSSREF_MAILTO`：GitHub Variable；可选，填写同意作为 API 联系方式的真实邮箱。
- OpenReview 不注入账户密码或浏览器 cookie；遇挑战保持禁用。

Daily 的上限仍由 workflow 中 `--max-results 5` 确定。增加来源不会自动增加每日篇数。无需重置私有仓库或 `seen.json`。

## 6. 验证与真实测试结果

离线测试 135 项通过（原 106 项 + 新增 29 项），包括：请求预算与重试、大小/协议检查、可信跳转、真实子进程硬终止、取消不熔断、页事务回滚、目录续扫、混轨道排除、身份合并与冲突、历史投递保留、shadow 和 preview 隔离、手动搜索范围、HTTP origin 校验与反馈持久化。CI 增加 Windows Python 3.12，保留 Linux Python 3.11/3.12/3.13 和已有语义 smoke。

真实状态副本迁移审计：

| 项目 | 结果 |
|---|---|
| 已投递集合 | 558 → 558，集合相等 |
| 已投递记录缺 DOI/主页别名 | 0 |
| 已投递记录缺题名索引 | 0 |
| 别名数量 | 65,031 |
| 外键错误 | 0 |
| 重复打开迁移 | 集合保持一致 |
| 原始状态副本 | SHA-256 未改变 |
| 本机迁移耗时 | 约 1.0 秒，单次样本 |

真实公共网络测试，无私有 Zotero、SMTP、LLM 或 embedding 调用：

| 测试 | 结果 |
|---|---|
| 关闭 DBLP，仅 PMLR + OpenAlex | 4 次请求，5 条提交记录，2 篇独立通过主会核验并选入推荐；约 3.3 秒 |
| CVF / AAAI / arXiv / Crossref | 各成功取到记录；CVF、AAAI 各 2 篇通过官方目录核验 |
| ACL 首测/修复复测 | 发现尾斜杠跳转耗费预算；修复后 3 次请求取得 2 篇并选入推荐，约 3.9 秒 |
| NeurIPS | 两轮超时，其他源正常完成；未宣称真实覆盖通过 |
| 已安装 CLI：OpenAlex + arXiv | 各 1 次请求，原始结果各 25 条，提交 48 条，2 条身份冲突暂缓；采集约 1.7 秒 |
| Edge 本地网页 | 显示 20 条缓存结果，取消外部补充并保存收藏；无脚本异常；取消误触发超时的缺陷由后续回归测试修正 |
| 安装包 | wheel 可安装；checkout 外可导入新模块、官方目录 JSON 与网页资源 |

`stored` 表示成功提交的记录数，可能含已有记录更新，不等于新增唯一作品数。上述样本不能推导长期成功率或 p95 延迟。OpenAlex 返回的普通查询配额计价为每次 `$0.001`，本轮使用匿名免费配额；这是额度统计，未使用付费模型或声明有账单扣款。来源测试按每来源 2–3 次请求/30–60 秒等小额度分轮执行；前台手动搜索上限另受交互预算约束。

可复现实测命令（只用公开来源，生成报告但不投递）：

```powershell
.\.venv\Scripts\python.exe scripts/source_smoke.py --output local-notes/source-check --sources pmlr openalex --max-requests 3 --deadline 35
```

本机证据保存在忽略目录 `local-notes/multisource-live-1/`、`multisource-live-2/`、`multisource-live-3/`、`multisource-migration-audit.json`、`manual-search-live.json` 和 `ui-browser-test.json`。真实状态副本、兴趣内容与反馈不上传公开仓库。

## 7. 上线步骤与尚未完成的观察

1. 审查分支/PR，等待 Linux 和 Windows CI。当前实现的主闭环已经通过本地验收；本轮不自动合并 main、不发送真实邮件。
2. 如需 key，在 GitHub 仓库 Settings → Secrets and variables → Actions 中配置，勿把 key 写入 JSON 或聊天。
3. 从分支先运行 Manual preview；检查来源状态、摘要、主会证据、重复情况和最终数量。建议先以 shadow 配置连续观察 3 轮，再采用 production；本轮短样本不替代多轮真实 Daily 观察。
4. 确认后合并 main，由用户触发一次真实投递，核对状态持久化；之后恢复正常每日任务，观察至少 7 天。已有每日调度配置无需重新创建。
5. 某来源不稳定时移出 `sources.enabled`，保留数据库与投递历史。不要用恢复旧数据库或删除 seen 的方式回退。

后续优先级：正式环境 7 天稳定性与覆盖统计 → 新年份官方目录审查 → 针对固定快照的搜索/推荐效果评估 → 将人工反馈用于校准。NeurIPS 网络恢复、S2 key 和 OpenReview 无人值守访问均需独立验收；目前不承诺这些可选来源已经可用。前 100 篇相关主会摘要覆盖 80% 的设计目标尚未以完整实验验证。
