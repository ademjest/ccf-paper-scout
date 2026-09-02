# CCF Paper Scout

CCF Paper Scout 是一个面向个人研究者的自动论文推荐程序。它从 Zotero 构建兴趣画像，从 DBLP 获取指定 CCF-A venue 的正式论文记录，使用本地算法完成过滤、去重和排序，可选调用 OpenAI-compatible API 生成中文论文卡片，并通过 GitHub Actions 和 SMTP 每日发送邮件。

当前默认偏好：

- 主要方向：强化学习（RL）、多智能体强化学习（MARL）、LLM-assisted RL；
- 拓展方向：LLM Agent；
- 每次最多推荐 10 篇，其中纯 LLM Agent 拓展论文最多 1 篇。

## 工作流程

```text
GitHub Actions
  → 恢复私有状态
  → 读取 Zotero 兴趣文献
  → 分页检索 DBLP
  → CCF-A 白名单与 DBLP record-key 校验
  → 排除已推送论文和 Zotero 已收藏论文
  → 本地相关性排序与主题优先级
  → OpenAlex 补充英文摘要
  → 选择最多 10 篇论文
  → LLM 生成中文论文卡片（可选）
  → SMTP 投递
  → 持久化 SQLite 和去重状态
```

LLM 不参与 CCF-A 资格判断、去重、核心排序和最终选文。它只处理最终入选论文的标题和摘要，用于生成中文标题、中文摘要、问题、方法、创新、证据、局限、推荐理由和主题标签。

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

编辑复制得到的 `config.json`：

- `years`：检索年份；
- `venue_keys`：允许检索的 DBLP venue key；
- `dblp`：分页、重试和部分数据源失败策略；
- `max_results`：每次最多推荐数量，当前默认 10；
- `min_score`：最低相关性分数；
- `explicit_interests`：显式主要研究方向；
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

Daily workflow 在 UTC 00:55 请求启动。应用会在 SMTP 调用前执行北京时间 09:00 门控：

- 提前完成准备时等待至 09:00；
- GitHub Actions 严重延迟并错过允许窗口时，本次停止，不在下午或晚上补发；
- 手动 workflow 不受该门控影响。

GitHub-hosted schedule 是 best-effort，不能提供严格的准点 SLA。如果必须保证每天准时，应改用具有调度时效保证的外部平台。

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
