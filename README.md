# CCF Paper Scout

CCF Paper Scout 是一个面向个人研究者的论文搜索与推荐工具，提供本地网页搜索和每日邮件推荐两个入口。它通过 DBLP、官方论文目录和 OpenAlex 获取候选及摘要，支持基于标题与摘要的关键词、向量和混合排序，以及阅读收藏、查询相关性反馈和离线评测。

| 使用方式 | 适合的场景 | 排序依据 |
|---|---|---|
| 本地网页搜索 | 围绕当前问题主动找论文、阅读与标注 | 本次查询与标题、摘要的相关性；默认语义＋关键词 |
| Daily 每日邮件 | 根据长期兴趣定期发现论文 | Zotero 兴趣文献、研究方向、主会资格及投递去重；可选语义排序 |

网页搜索不需要 Zotero、SMTP 或 LLM 凭据。Daily 通过 GitHub Actions 和 SMTP 投递，可选调用 OpenAI-compatible API 生成中文论文卡片。两者复用采集与向量编码能力，使用独立的搜索/投递数据库；网页反馈不会自动训练模型或改变 Daily 推荐。

Daily 示例配置的默认偏好：

- 主要方向：强化学习（RL）、多智能体强化学习（MARL）、LLM-assisted RL；
- 拓展方向：LLM Agent；
- 默认每次最多推荐 5 篇，其中纯 LLM Agent 拓展论文最多 1 篇。

## Daily 工作流程

```text
GitHub Actions
  → 恢复私有状态
  → 读取 Zotero 兴趣文献
  → 按动态年份、独立来源预算和持久游标采集 DBLP／PMLR／OpenAlex
  → 保存完整候选库并周期复查；发现进度不依赖是否已推送
  → CCF-A 白名单与具体 proceedings／官方轨道证据硬过滤（未知暂缓）
  → 排除已推送论文和 Zotero 已收藏论文
  → 在来源预算内完成官方页面／DOI 精确摘要补全
  → 归一化词项排序，可选 CPU embedding / 相关性复核
  → LLM／RL 作用方向原型与独立证据复核（默认 shadow，正式模式强制准入）
  → 质量门槛与新发现/历史队列，最多 5 篇，不用其他方向补位
  → LLM 生成中文论文卡片（可选）
  → SMTP 投递
  → 持久化 SQLite 和去重状态
```

LLM 不参与 CCF-A 出版资格判断和去重。关系复核与最终中文卡片分开，每轮最多审核 20 篇边界候选，要求摘要原句证据。Daily 的 CPU embedding 默认关闭，可先使用 shadow 模式评估；正式接管兴趣排序需要匹配模型与画像的人工审查校准，关系原型另有独立的校准版本。

主会与 LLM／RL 关系升级的范围、保守暂缓边界及 Actions 测试步骤见 [实施与审查说明](docs/main-conference-and-relations-implementation.md)。

`update-paper` 的实施内容、迁移方法、测试结果和 Actions 启用步骤见 [部署说明](docs/update-paper-implementation.md)。原始设计见 [优化方案](docs/recommendation-quality-and-discovery-plan.md)。

## 网页搜索：快速开始

以下命令使用包含网页升级的 `update-paper` 分支。在仓库根目录运行；Windows 推荐 Python 3.12。已有仓库和虚拟环境时，跳过克隆与环境创建步骤。升级安装前先在原服务窗口按 `Ctrl+C` 停止服务，避免 Windows 可执行文件被占用。

### Windows / PowerShell

```powershell
git clone --branch update-paper https://github.com/ademjest/ccf-paper-scout.git
cd ccf-paper-scout
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --no-cache-dir .
.\.venv\Scripts\ccf-paper-scout.exe serve --config config.example.json
```

### Linux / macOS

```bash
git clone --branch update-paper https://github.com/ademjest/ccf-paper-scout.git
cd ccf-paper-scout
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --no-cache-dir .
ccf-paper-scout serve --config config.example.json
```

浏览器打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)。服务仅监听本机，使用期间保持终端运行。基础安装即可使用关键词搜索；要启用默认选中的语义＋关键词匹配，还需完成下面的一次性模型准备。模型不可用时页面会提示并回退关键词排序。

### 启用本地语义匹配（可选）

停止服务后，在仓库根目录执行以下 PowerShell 命令。Linux/macOS 激活虚拟环境后，将 `.\.venv\Scripts\python.exe` 换成 `python`；macOS 安装 torch 时省略 `--index-url`，使用默认包源。以下固定依赖组合已在 Python 3.11/3.12 验证。

```powershell
.\.venv\Scripts\python.exe -m pip install torch==2.6.0 --index-url https://download.pytorch.org/whl/cpu
.\.venv\Scripts\python.exe -m pip install ".[semantic]" -c constraints-semantic.txt
.\.venv\Scripts\python.exe -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-small-en-v1.5', revision='5c38ec7c405ec4b44b94cc5a9bb96e735b38267a', device='cpu', trust_remote_code=False, cache_folder='.model-cache/hub')"
.\.venv\Scripts\ccf-paper-scout.exe serve --config config.example.json
```

模型准备需要联网下载；搜索时只使用本地缓存模型和 CPU，无需 GPU 或付费 embedding API。当前模型主要面向英文，建议输入英文研究问题。

### 网页怎么用

1. 输入研究问题，选择年份和发表范围。默认只展示正式期刊/会议论文且必须有有效摘要，排除预印本、学位论文和发表状态未核实的记录。
2. 选择“语义＋关键词”“语义匹配”或“关键词匹配”，点击“查找论文”。先展示关键词结果，联网补充结束后再完成语义排序。
3. 展开摘要、打开论文，使用收藏与已读按钮整理阅读进度。相关性反馈为“很有帮助”“有些帮助”“没有帮助”，对应原来的 2/1/0 分。
4. 从“最近搜索”恢复结果。历史保留当时的论文和排序；使用新算法需重新发起搜索。

“来源与搜索选项”可调整来源或只查本地库。默认来源是 OpenAlex、PMLR、NeurIPS、ACL、CVF 和 AAAI；Crossref、arXiv、DBLP 可按需开启。启用 arXiv 不会自动放宽发表范围，查看预印本需同时选择包含预印本的范围。

语义匹配使用标题与摘要共同生成 384 维向量，长摘要分段处理；混合排序融合词项和向量两路排名。论文不必先命中关键词才可进入向量候选。向量缓存存入 `state/search.sqlite3`，摘要变化后自动重算；模型缓存为 `.model-cache/hub`。每次默认最多新计算 500 篇，覆盖不完整或模型不可用时页面会明确提示。首次加载和编码需要额外时间，后续可复用缓存。

向量搜索覆盖已经抓取的本地候选，不是全网向量检索；联网来源仍使用各自查询接口和官方目录。详见 [语义搜索原理、配置与评测](docs/search-semantic-ranking.md)。

命令行也可指定模式（CLI 默认仍为关键词）：

```powershell
.\.venv\Scripts\ccf-paper-scout.exe search --config config.example.json --query "world model planning" --ranking hybrid --require-abstract
```

来源状态、Windows 命令、预算配置、真实测试结果及上线限制见 [多来源实施说明](docs/multi-source-implementation.md)。完整设计见 [升级方案](docs/multi-source-upgrade-plan.md)。

“反馈数据与评估（可选）”支持导出查询反馈和 AI 协作任务。人工与 AI 标签分开保存，独立测试样本不进入 AI 待标注包；可对固定结果快照重跑评测。操作步骤与指标边界见 [搜索反馈与评测说明](docs/search-feedback-and-evaluation.md)。

来源抓取支持逐篇恢复、摘要身份核对和响应缓存，区分访问失败与预算耗尽。单篇异常不会直接丢弃整个目录进度，来源失败时仍可展示已取得的本地结果；见 [抓取可靠性实施记录](docs/source-fetch-reliability-implementation.md)。

## 环境要求

- Python 3.11+；本地语义匹配推荐 Python 3.12
- 仅运行网页搜索无需 Zotero、SMTP 或 LLM 凭据
- 使用 Daily 兴趣推荐时需要 Zotero 数字 User ID 和只读 API Key
- 使用邮件投递时需要 SMTP 账号
- 使用中文论文卡片时需要 OpenAI-compatible API
- 使用 GitHub Actions 正式投递时需要一个私有 GitHub 状态仓库

## Daily 本地运行

如果已完成网页快速安装，可直接复制配置并设置 Zotero 凭据：

```bash
git clone --branch update-paper https://github.com/ademjest/ccf-paper-scout.git
cd ccf-paper-scout
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
cp config.example.json config.json
```

设置 Zotero 凭据：

```bash
export ZOTERO_USER_ID='你的数字 User ID'
export ZOTERO_API_KEY='你的只读 API Key'
```

检查配置：

```bash
ccf-paper-scout doctor --config config.json
```

生成本地报告：

```bash
ccf-paper-scout run \
  --config config.json \
  --output recommendations.md \
  --no-update-seen
```

`--no-update-seen` 表示预览，不把论文记录为已投递。正式运行时不要添加该参数。

## 主要配置

编辑复制得到的 `config.json`：

- `year_policy`：默认按北京时间自动查询当前年与上一年，并低频探测下一年；固定年份使用 `year_policy.mode=fixed` 和 `years`；
- `venue_keys`：允许检索的 DBLP venue key；
- `dblp`：分页、全局限速、瞬时错误重试、SPARQL 降级和部分分区失败策略；
- `max_results`：Daily 每次最多推荐数量，示例配置当前为 5；
- `selection`：质量门槛、是否允许仅标题、探索上限和新发现队列；
- `semantic`：Daily 可选 CPU embedding，固定模型 revision、增量预算与 shadow/production 模式；
- `search.semantic`：网页/手动搜索使用的本地模型缓存位置和增量编码预算；相对模型路径以启动目录为准；
- `search.state_db`：独立搜索库，默认 `state/search.sqlite3`，不能与 Daily 数据库相同；
- `main_conference`：主会资格硬过滤、历史候选论文集回填预算；
- `relation_policy`：LLM→RL、RL→LLM、LLM 多智能体 RL 独立分类与证据复核，默认 shadow；
- `explicit_interests`：显式主要研究方向；
- `topic_priority.primary_topics`：主要方向；
- `topic_priority.exploration_topics`：拓展方向；
- `topic_priority.max_exploration_results`：拓展方向最大篇数；
- `metadata.max_requests_per_run`：OpenAlex 每次运行的请求上限，默认 200，缺摘要与失败结果会缓存；
- `recent_interest_items`：用于兴趣建模的近期 Zotero 文献数量；
- `zotero_collection_keys`：限定参与兴趣建模的 Zotero collections；
- `state_db`、`seen_db`：SQLite 和兼容去重状态路径。

私人凭据只能通过环境变量或 GitHub Actions Secrets 提供，不要写入 `config.json` 或提交到 Git。

### DBLP 可用性与降级

DBLP Search API 可能对云端出口返回 HTTP 200 的反机器人 HTML 页面。程序会先检查
`Content-Type` 和正文特征，不会把验证页面当作 JSON 重试；两个 Search API 域名均不可用时，
独立搜索接口调用会熔断并降级到官方 SPARQL 服务；持久发现默认直接使用 SPARQL，并固定
单轮 transport，避免跨端点混用分页偏移。`dblp.request_delay_seconds` 应保持至少
2–3 秒，使所有 DBLP 请求共享同一个进程级间隔。`dblp.max_attempts` 只作用于 429、5xx、
超时和连接中断，不作用于反机器人页面或结构错误。

当前多来源流程保留其他来源和已有缓存的合格候选，并在报告中注明来源异常。仅使用旧 DBLP
发现流程时，`minimum_success_ratio` 仍检测异常；示例配置允许临时故障时使用缓存，严格模式
则停止运行。缓存候选仍需通过原有资格、相关性和去重检查。详细设计和恢复步骤见
[`docs/dblp-resilience-improvement.md`](docs/dblp-resilience-improvement.md)。

## 本地命令

需要比较词项与语义排序、保存完整候选快照或制作人工标注集时，见
[离线排序评测指南](docs/offline-benchmark.md)。该流程使用独立实验目录，不发送邮件。

```bash
ccf-paper-scout --help
ccf-paper-scout doctor --config config.json
ccf-paper-scout test-delivery --config config.json
ccf-paper-scout run --config config.json --output recommendations.md
```

## GitHub Actions 部署

仓库保留两个业务工作流：

- `Paper Scout Manual`：`doctor`、`smtp-test`、`discover`、`evaluate`、`preview`、`production`、`research`；
- `Paper Scout Daily`：每日自动推荐。

另有 `Keep Scheduled Workflows Active`，用于降低公开仓库长时间无提交后 schedule 被暂停的概率。

Actions 负责批次检索与投递，不托管常驻网页。网页通过本机 `serve` 启动；推送 `update-paper` 不会自动替换默认分支上的 Daily 程序，正式部署仍需完成预览并合并到默认分支。

`research` 可在 `update-paper` 分支运行新的 RL 行动策略／世界模型画像实验：定向检索公开论文、核对官方摘要、比较新旧画像与三种排序，并提供 HTML/JSON 下载。此模式无需 Zotero、SMTP、LLM 或私有状态仓库 Secrets，不发送邮件。操作步骤见 [Actions 研究实验](docs/research-actions.md)。

将当前分支接入正式每日推送的配置、预览、合并与历史保留步骤见 [main 升级操作指南](docs/main-rollout.md)。

### 1. 创建私有状态仓库

创建一个 private 仓库，例如：

```text
你的账号/ccf-paper-scout-state
```

私有状态仓库用于保存：

```text
state/paper_scout.sqlite3
state/seen.json
state/translations.json
```

不要将状态仓库设为公开，因为推荐历史和缓存可能反映个人研究兴趣。

### 2. 配置 GitHub Variable

在代码仓库的 `Settings → Secrets and variables → Actions → Variables` 中设置：

```text
STATE_REPO=你的账号/ccf-paper-scout-state
```

### 3. 配置 GitHub Secrets

```text
ZOTERO_USER_ID
ZOTERO_API_KEY
SMTP_SENDER
SMTP_RECEIVER
SMTP_PASSWORD
LLM_API_KEY
LLM_BASE_URL
LLM_MODEL
STATE_REPO_TOKEN
```

说明：

- `SMTP_PASSWORD` 应为 SMTP 授权码，不是邮箱登录密码；
- QQ 邮箱默认使用 `smtp.qq.com:465` 和 SSL；
- `STATE_REPO_TOKEN` 建议使用只授权私有状态仓库、Contents read/write 的 fine-grained token；
- 所有 Secret 都不得出现在 Issue、PR、日志或配置文件中。

### 4. 按顺序验收

在 `Paper Scout Manual` 中依次运行：

1. `doctor`：检查配置、凭据存在性、SMTP TLS 和状态目录；
2. `smtp-test`：只发送测试邮件，不抓取论文、不修改推荐状态；
3. `preview`：恢复私有状态并生成报告，不发送邮件、不更新去重状态；
4. `production`：首次建议使用较小的 `max_results`，确认真实邮件和状态持久化都成功。

成功后再启用 `Paper Scout Daily`，并停用其他生产调度器，避免重复发送。

## 每日发送时间

Daily workflow 在前一天 UTC 20:00（北京时间当天 04:00）请求启动。该提前量用于补偿 GitHub Actions 定时任务常见的排队延迟。应用会在 SMTP 调用前执行北京时间 09:00 门控：

- 提前完成准备时等待至 09:00；工作流最长允许运行 6 小时，以覆盖提前启动后的等待和正常处理时间；
- 09:00 后才完成准备时不再取消当天发送，而是立即发送并在日志中记录迟到秒数；
- 手动 workflow 不受该门控影响。

GitHub-hosted schedule 是 best-effort，提前调度只是根据历史延迟进行经验补偿，不能提供严格的准点 SLA。如果必须保证每天准时，应改用具有调度时效保证的外部平台。

## 投递和去重安全

系统只在 SMTP 服务端接受邮件后把论文标记为 delivered。生产运行在 SMTP 前持久化 `.delivery-pending`：

```text
准备报告
→ 写入并推送 delivery_pending
→ 调用 SMTP
→ SMTP accepted
→ 更新 SQLite 和 seen.json
→ 清除 pending 并推送最终状态
```

如果系统无法确认邮件是否已发送，会保留 pending 并阻止下一次自动 production，以减少重复邮件风险。

## 隐私边界

- Zotero 文献在运行环境中用于本地兴趣建模；
- DBLP 接收 venue、年份和查询参数；
- OpenAlex 等检索接口接收查询词或候选 DOI，官方目录来源接收论文集和详情页请求；
- 网页 embedding 在本机计算，模型准备阶段会下载公开权重；
- 翻译 LLM 接收最终入选论文的标题、摘要和显式兴趣方向；独立关系复核还会接收每轮有预算上限的边界候选标题及摘要；
- 完整 Zotero 文库、推荐历史、SQLite、SMTP 授权码和各类 API Key 不应进入公开仓库；
- 个人论文库、模型缓存及 `local-notes/` 内的标注和调试导出由 `.gitignore` 保持在本地；公开代码、自动化测试和 `docs/` 使用文档纳入版本控制。

## 数据与许可证

项目代码采用 Apache-2.0，详见 `LICENSE`。

运行时打包的 CCF-A venue 映射位于：

```text
src/ccf_paper_scout/data/ccf_a_venues.json
```

该映射是从 `WenyanLiu/CCFrank4dblp` 固定版本转换得到的 CCF-A 子集，原始数据采用 MIT License。完整第三方声明见 `THIRD_PARTY_NOTICES.md`。该映射不是 CCF 官方 API，使用时应以最新 CCF 官方目录和正式 proceedings 为准。

## 能力边界

- DBLP 已索引的正式论文不等于最新投稿或预印本；
- DBLP Search API 和 SPARQL 都是外部服务；自动降级可以减少单点故障，但不能提供可用性 SLA；
- OpenAlex 可能缺少摘要；
- 网页支持词项、语义和混合排序，但相关性分数不是论文质量评分；当前英文 embedding 模型不保证中文跨语言检索效果；
- 向量匹配无法弥补尚未抓取的论文和缺失摘要；目前单个完整人工标注开发查询上的改善不能代表整体效果；
- LLM 输出是基于标题和摘要的辅助解读，不等于阅读全文后的严格评审；
- GitHub Actions 和 SMTP 无法构成绝对原子事务；
- 当前项目面向单个研究者和单个私有状态仓库。
