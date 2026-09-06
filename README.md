# CCF Paper Scout

CCF Paper Scout 是一个面向个人研究者的自动论文推荐程序。它从 Zotero 构建兴趣画像，可按私人 Profile 自主选择 DBLP、arXiv 和 IEEE Xplore 数据源，使用本地算法完成出版状态区分、跨源去重和排序，可选调用 OpenAI-compatible API 生成中文论文卡片，并通过 GitHub Actions 和 SMTP 每日发送邮件。

公开配置不包含个人研究方向。GitHub Actions 从加密 Secret `PAPER_SCOUT_PROFILE_JSON` 注入私人画像；本地运行可在未提交的 `config.json` 中配置。

## 工作流程

```text
GitHub Actions
  → 恢复私有状态
  → 读取 Zotero 兴趣文献
  → 按私人 Profile 检索 DBLP / arXiv / IEEE Xplore
  → CCF-A、预印本与控制工程资格策略
  → 排除已推送论文和 Zotero 已收藏论文
  → 本地相关性排序与主题优先级
  → DOI 跨源合并，OpenAlex 补充英文摘要
  → 选择最多 10 篇论文
  → LLM 生成中文论文卡片（可选）
  → SMTP 投递
  → 持久化 SQLite 和去重状态
```

LLM 不参与来源选择、出版状态、CCF-A/控制工程资格判断、跨源去重、核心排序和最终选文。它只处理最终入选论文的标题和摘要，用于生成中文标题、中文摘要、问题、方法、创新、证据、局限、推荐理由和主题标签。

## 环境要求

- Python 3.11+
- Zotero 数字 User ID 和只读 API Key
- 使用邮件投递时需要 SMTP 账号
- 使用中文论文卡片时需要 OpenAI-compatible API
- 云端运行时需要一个私有 GitHub 状态仓库

## 本地安装

```bash
git clone https://github.com/ademjest/ccf-paper-scout.git
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

### 推荐方式：只写一次自然语言个人画像

升级版支持由 LLM 将自然语言画像编译为稳定的结构化兴趣设置。普通用户只需在本地 `config.json` 中填写：

```json
{
  "profile": {
    "description": "我的研究领域是强化学习，对 RL、MARL、LLM+RL 等方向比较感兴趣，主要聚焦 RL，也希望阅读少量其他 AI 领域论文拓展视野。"
  },
  "max_results": 10
}
```

程序只在首次运行或画像/数据源覆盖发生变化时调用 LLM；编译结果保存到私人状态中的
`compiled-profile.json`，后续运行直接复用。如果画像和覆盖配置没有变化，即使 LLM 暂时不可用也会继续使用匹配缓存；如果用户已修改画像但新画像编译失败，系统会安全停止，不会悄悄沿用与新描述不匹配的旧画像。

GitHub Actions 使用 Secret `PAPER_SCOUT_PROFILE_TEXT` 保存同一段自然语言。高级用户仍可继续使用
`PAPER_SCOUT_PROFILE_JSON`；若两者同时存在，结构化 JSON 优先。

### 可选数据源覆盖

系统会为未设置的数据源字段自动选择安全默认值。用户显式设置永远优先：

```text
用户显式 discovery 设置 > 画像自动推断 > 系统安全默认值
```

本地配置示例：

```json
{
  "discovery": {
    "years": [2026, 2025],
    "dblp": {
      "enabled": true,
      "venue_keys": ["nips", "icml", "aaai"]
    },
    "arxiv": {
      "enabled": true,
      "categories": ["cs.LG", "stat.ML", "cs.AI"],
      "max_age_days": 30
    },
    "ieee_xplore": {"enabled": false}
  }
}
```

Actions 中可将同一对象（只放 `discovery` 的内容）设置为可选 Secret `PAPER_SCOUT_DISCOVERY_JSON`。
省略字段表示自动推断；`enabled: false` 表示明确关闭；`venue_keys` 和 `categories` 如需自定义必须提供非空列表。

#### `years` 选择

省略时自动使用当前年份和上一年份。DBLP 在年初可能尚未完整索引当年记录，保留上一年可减少漏检。自定义时允许 1–5 个年份，例如：

```json
"years": [2026, 2025, 2024]
```

#### DBLP `venue_keys` 参考

`venue_keys` 是本项目 CCF-A/DBLP 映射中的内部 key，不是任意会议全称。常见组合：

| 方向 | 建议 key |
|---|---|
| RL / 机器学习 | `nips`, `icml`, `aaai` |
| 计算机视觉 | `cvpr`, `iccv` |
| NLP | `acl`, `aaai` |
| 数据挖掘 / 推荐 | `kdd`, `sigir` |
| 综合 AI 拓展 | `nips`, `icml`, `aaai`, `cvpr`, `acl`, `kdd`, `sigir` |

不建议无差别开启所有 Venue；这会增加运行时间和主题噪声。Venue 映射代表发现范围，不是对单篇论文质量的官方认证。

#### arXiv Category 参考

arXiv Category 决定预印本发现范围，仍会与画像解析出的主题做联合查询。常用设置：

| 方向 | 建议 Category |
|---|---|
| 机器学习、RL、MARL | `cs.LG`, `stat.ML`, `cs.AI` |
| NLP、LLM | `cs.CL`, `cs.AI`, `cs.LG` |
| 计算机视觉 | `cs.CV`, `cs.LG` |
| 机器人 | `cs.RO`, `eess.SY`, `cs.LG` |
| 系统与控制 | `eess.SY`, `math.OC` |
| 机器人与学习控制 | `cs.RO`, `eess.SY`, `math.OC`, `cs.LG` |

**特别注意：arXiv 没有 `cs.ML`，机器学习应使用 `cs.LG`。** 配置 `cs.ML` 会被校验器拒绝并提示改用 `cs.LG`。完整分类以
<https://arxiv.org/category_taxonomy> 为准。Category 不是质量评级；只使用宽泛的 `cs.AI` 可能产生较多噪声。

#### 修改画像

无需手动清理缓存。修改本地 `profile.description`、`discovery`，或 Actions 中相应 Secret 后，指纹会变化并自动重新编译。修改生产画像时建议先暂停 Daily workflow，更新 Secret 后再重新启用，以避免恰好运行中的任务继续使用旧值。

编辑复制得到的 `config.json`：

- `years`：检索年份；
- `venue_keys`：允许检索的 DBLP venue key；
- `dblp`：分页、重试和部分数据源失败策略；
- `max_results`：每次最多推荐数量，当前默认 10；
- `min_score`：最低相关性分数；
- `explicit_interests`：显式研究方向（公开示例保持为空）；
- `topic_priority.primary_topics`：主要方向；
- `topic_priority.exploration_topics`：拓展方向；
- `topic_priority.max_exploration_results`：拓展方向最大篇数；
- `openalex_enrich_limit`：OpenAlex 摘要补全上限；
- `recent_interest_items`：用于兴趣建模的近期 Zotero 文献数量；
- `zotero_collection_keys`：限定参与兴趣建模的 Zotero collections；
- `state_db`、`seen_db`：SQLite 和兼容去重状态路径。

私人凭据只能通过环境变量或 GitHub Actions Secrets 提供，不要写入 `config.json` 或提交到 Git。

## 本地命令

```bash
ccf-paper-scout --help
ccf-paper-scout doctor --config config.json
ccf-paper-scout test-delivery --config config.json
ccf-paper-scout run --config config.json --output recommendations.md
```

## GitHub Actions 部署

仓库保留两个业务工作流：

- `Paper Scout Manual`：`doctor`、`smtp-test`、`preview`、`production`；
- `Paper Scout Daily`：每日自动推荐。

另有 `Keep Scheduled Workflows Active`，用于降低公开仓库长时间无提交后 schedule 被暂停的概率。

### GitHub Actions 怎样读取配置

GitHub Actions 无法读取用户电脑上的 `config.json`。云端流程为：

```text
公开的 config.example.json（中性默认值）
+ 加密 Secret PAPER_SCOUT_PROFILE_JSON（私人来源和研究方向）
+ 其他服务 Secrets（凭据）
→ scripts/build_actions_config.py 严格校验和合并
→ runner 临时文件 config.action.json（权限 0600）
→ 推荐程序读取
→ workflow 结束后 runner 被销毁
```

`config.action.json` 不会提交到代码仓库、状态仓库或 Artifact。自然语言原文也不会写入状态仓库；私人状态只缓存画像指纹和已编译的结构化策略。生产日志默认不输出完整配置、具体兴趣词、论文标题或报告正文。注意：能修改默认分支 workflow 的维护者理论上可以让 workflow 读取 Secrets，因此公开 Fork 的使用者应保护仓库写权限、审核 workflow 改动，并且不要让不受信任的 pull request 在可访问生产 Secrets 的事件中运行。

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
IEEE_XPLORE_API_KEY
PAPER_SCOUT_PROFILE_TEXT
PAPER_SCOUT_DISCOVERY_JSON
PAPER_SCOUT_PROFILE_JSON
STATE_REPO_TOKEN
```

说明：

- `SMTP_PASSWORD` 应为 SMTP 授权码，不是邮箱登录密码；
- QQ 邮箱默认使用 `smtp.qq.com:465` 和 SSL；
- `STATE_REPO_TOKEN` 建议使用只授权私有状态仓库、Contents read/write 的 fine-grained token；
- 所有 Secret 都不得出现在 Issue、PR、日志或配置文件中。
- 推荐普通用户只设置 `PAPER_SCOUT_PROFILE_TEXT`；`PAPER_SCOUT_DISCOVERY_JSON` 是可选数据源覆盖；`PAPER_SCOUT_PROFILE_JSON` 是高级结构化覆盖。

`PAPER_SCOUT_PROFILE_JSON` 必须是单个 JSON 对象，当前 schema version 为 `1`。顶层字段固定为
`version`、`sources`、`domains`、`primary`、`exploration`、`digest`，以及可选的 `eligibility`；未知字段和任何类似凭据的 key
（例如 `password`、`token`、`secret`、`api_key`）都会导致配置构建失败。例如：

```json
{
  "version": 1,
  "sources": {
    "years": [2026, 2025],
    "venue_keys": ["nips", "icml"],
    "zotero_collection_keys": []
  },
  "domains": ["your broad research domain"],
  "primary": ["your primary topic"],
  "exploration": ["an adjacent topic"],
  "digest": {
    "min_score": 0.01,
    "primary_topic_boost": 2.0,
    "exploration_topic_boost": 0.2,
    "max_exploration_results": 1
  }
}
```

上面是 DBLP-only 的最小示例。用户应只启用自己需要的数据源和方向，不要同时开启所有领域。常用组合如下。

RL / MARL / LLM-assisted RL：

```json
{
  "version": 1,
  "sources": {
    "years": [2026, 2025],
    "venue_keys": ["nips", "icml", "aaai"],
    "dblp": {"enabled": true},
    "arxiv": {
      "enabled": true,
      "categories": ["cs.LG", "stat.ML"],
      "max_age_days": 14,
      "max_pages": 2,
      "failure_policy": "continue"
    },
    "ieee_xplore": {"enabled": false}
  },
  "domains": ["reinforcement learning"],
  "primary": [
    "reinforcement learning",
    "multi-agent reinforcement learning",
    "LLM-assisted reinforcement learning"
  ],
  "exploration": ["LLM agents"],
  "digest": {
    "primary_topic_boost": 2.0,
    "exploration_topic_boost": 0.2,
    "max_exploration_results": 1,
    "quotas": {"formal": 7, "preprint": 2, "exploration": 1}
  }
}
```

控制工程（arXiv + IEEE Xplore）：

```json
{
  "version": 1,
  "sources": {
    "years": [2026, 2025],
    "venue_keys": [],
    "dblp": {"enabled": false},
    "arxiv": {
      "enabled": true,
      "categories": ["eess.SY", "math.OC", "cs.RO"],
      "max_age_days": 14,
      "max_pages": 2,
      "failure_policy": "continue"
    },
    "ieee_xplore": {
      "enabled": true,
      "page_size": 50,
      "max_pages": 2,
      "failure_policy": "continue"
    }
  },
  "domains": ["control systems"],
  "primary": [
    "model predictive control",
    "optimal control",
    "robust control",
    "adaptive control",
    "system identification"
  ],
  "exploration": ["reinforcement learning for control"],
  "eligibility": {"control_policy": true},
  "digest": {
    "primary_topic_boost": 2.0,
    "exploration_topic_boost": 0.2,
    "max_exploration_results": 1,
    "quotas": {"formal_control": 7, "preprint": 2, "exploration": 1}
  }
}
```

说明：

- `dblp` 适合 CCF-A 正式计算机论文；`arxiv` 是预印本来源；`ieee_xplore` 是 IEEE 出版商记录来源；
- arXiv 论文会明确标记为“预印本（未经同行评审）”，不会被描述为 CCF-A 正式论文；
- IEEE 出版商记录本身不会自动获得控制核心资格，只有本地审核列表中的 Venue 才进入 `formal_control`；
- 启用 IEEE 时还需单独创建 `IEEE_XPLORE_API_KEY` Secret；API Key 不得写入 Profile JSON；
- `quotas` 总和不能超过工作流的 `max_results`。Daily 固定为 10，Manual 可在 1–20 范围内选择，但不得低于 Profile 配额总和；
- arXiv API 无需 Key；默认建议约每 3 秒最多请求一次，并限制类别、主题、时间窗口与页数。

`sources` 可包含通用参数 `years`、`venue_keys`、`zotero_collection_keys`、`recent_interest_items`、
`zotero_dedup_items`、`openalex_enrich_limit`，以及来源对象 `dblp`、`arxiv`、`ieee_xplore`；
`digest` 可包含主题权重和 `quotas`。Actions 日志默认只显示
兴趣和入选论文计数，不显示具体方向或标题。仅本地调试时可在未提交的配置中显式设置
`debug.log_paper_titles: true`。Manual preview 报告只留在临时 runner，不上传 Actions artifact。

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
- OpenAlex 接收候选 DOI；
- LLM 只接收最终入选论文的标题、摘要和显式兴趣方向；
- 完整 Zotero 文库、推荐历史、SQLite、SMTP 授权码和各类 API Key 不应进入公开仓库；
- 报告、调试导出、测试、设计文档和个人分析文件由 `.gitignore` 保持在本地。

## 数据与许可证

项目代码采用 Apache-2.0，详见 `LICENSE`。

运行时打包的 CCF-A venue 映射位于：

```text
src/ccf_paper_scout/data/ccf_a_venues.json
```

该映射是从 `WenyanLiu/CCFrank4dblp` 固定版本转换得到的 CCF-A 子集，原始数据采用 MIT License。完整第三方声明见 `THIRD_PARTY_NOTICES.md`。该映射不是 CCF 官方 API，使用时应以最新 CCF 官方目录和正式 proceedings 为准。

## 能力边界

- DBLP 已索引的正式论文不等于最新投稿或预印本；
- OpenAlex 可能缺少摘要；
- 当前排序器是可解释的稀疏词项算法，不是论文质量评分；
- LLM 输出是基于标题和摘要的辅助解读，不等于阅读全文后的严格评审；
- GitHub Actions 和 SMTP 无法构成绝对原子事务；
- 当前项目面向单个研究者和单个私有状态仓库。
