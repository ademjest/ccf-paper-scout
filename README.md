# CCF-A Paper Scout

CCF-A Paper Scout 是一个面向个人研究者的论文发现与每日推荐工具。它从 Zotero 阅读库提取近期兴趣，在 DBLP 中检索指定年份和 venue 的正式论文记录，通过维护的 CCF-A venue 白名单进行资格约束，再用本地可解释算法完成排序。系统还可以从 OpenAlex 补充摘要，调用 OpenAI-compatible API 生成中文论文卡片，并通过 GitHub Actions 每天发送邮件。

> CCF 等级是 venue 级参考信号，不代表对单篇论文科学质量的评价。本项目不隶属于 CCF、Zotero、DBLP、OpenAlex、GitHub 或任何会议及出版社。

## 主要功能

- 从 Zotero 标题、摘要和显式研究方向构建个人兴趣画像；
- 支持多个 Zotero collection，全局合并、按加入时间排序后再截断；
- 从 DBLP 分页检索指定 venue 和年份的正式论文记录；
- 用固定版本的 CCF-A venue 白名单和 DBLP record key 前缀约束候选；
- 排除历史已推送论文以及 Zotero 中已经收藏的论文；
- 使用带时间权重的稀疏词项算法排序，并展示主要贡献词；
- 对初排候选从 OpenAlex 补充摘要，失败时自动回退到标题排序；
- 可选调用 OpenAI-compatible API 生成中文标题、摘要、方法、创新、证据、局限和推荐理由；
- 通过 QQ SMTP 或其他兼容 SMTP 服务发送每日推荐；
- 使用 SQLite 保存运行、投递和缓存状态，并通过私有状态仓库跨 GitHub Actions 运行持久化；
- 提供 `doctor`、SMTP 测试、不会发送邮件或持久化去重状态的预览，以及正式运行工作流。

## 工作流程

```text
Zotero 阅读库与显式兴趣
        ↓
构建带时间权重的个人兴趣画像
        ↓
按配置的 CCF-A venue 和年份分页检索 DBLP
        ↓
DBLP record key 前缀复核
        ↓
排除历史已推送论文和 Zotero 已收藏论文
        ↓
标题初排 → OpenAlex 摘要补全 → 标题与摘要重排
        ↓
选出达到 min_score 的前 N 篇论文
        ↓
可选 LLM 中文分析与论文卡片生成
        ↓
SMTP 邮件投递
        ↓
投递成功后更新 SQLite 和去重状态
```

生产环境默认由 GitHub Actions 每天 UTC 01:00（北京时间 09:00）运行。历史投递状态保存在私有状态仓库中，同一论文不会因 runner 是临时环境而被反复发送。

## 推荐逻辑

当前默认排序器是带时间权重和显式兴趣增强的稀疏词项匹配，不依赖 GPU、Torch、向量数据库或本地大模型。

1. 标题重复三次后与摘要合并，使标题词拥有更高权重。
2. 显式研究方向被作为强正样本加入兴趣画像。
3. 使用对数 TF 和近似 IDF 降低长摘要与通用词的影响。
4. Zotero 中最近加入的论文权重更高，但旧论文仍保留至少一半时间权重。
5. 候选论文的每个词根据兴趣画像计算贡献值；显式兴趣词额外放大。
6. 所有词项贡献相加得到相关性分数，并记录贡献最大的六个词作为解释。
7. 先按标题初排，只对前若干篇有 DOI 的候选请求 OpenAlex，补摘要后再次排序。
8. 最后应用 `min_score` 和 `max_results`，不会为了凑数加入完全无关论文。

该算法借用了 TF-IDF 的思想，但不是严格的 TF-IDF cosine 或 BM25。它更适合描述为“可解释的稀疏词项相关性排序”。后续计划在离线评测集上比较标准 TF-IDF、BM25、Top-K Zotero 相似依据和 MMR 多样性后，再决定是否替换默认算法。

LLM 不参与 CCF-A 资格判断和核心排序。它只处理最终选中的标题和摘要，用于生成更易读的中文内容。

## 快速开始

要求 Python 3.11+。项目默认仅使用 Python 标准库。

```bash
git clone https://github.com/ademjest/ccf-paper-scout.git
cd ccf-paper-scout
python -m pip install .
cp config.example.json config.json
```

先使用示例兴趣文件生成本地报告：

```bash
ccf-paper-scout run \
  --config config.json \
  --interests interests.example.json \
  --output recommendations.md \
  --no-update-seen
```

正式读取 Zotero 前，设置只读凭据：

```bash
export ZOTERO_USER_ID='你的数字 User ID'
export ZOTERO_API_KEY='你的只读 API Key'
ccf-paper-scout doctor --config config.json
ccf-paper-scout run --config config.json --output recommendations.md
```

程序读取 `journalArticle`、`conferencePaper` 和 `preprint`。建议用 `zotero_collection_keys` 限制兴趣来源，例如只选择“已读”“重点”和当前课题 collection，避免多年文库稀释近期兴趣。

## 配置说明

主要配置位于 `config.json`：

- `venue_keys`：DBLP venue key。可从 `data/ccf_a_venues.json` 选择支持的 CCF-A venue；
- `years`：候选年份。正式出版存在索引延迟，建议保留本年和上一年；
- `dblp.page_size`：单页 DBLP 结果数；
- `dblp.max_pages_per_venue`：每个 venue/年份最多扫描页数；
- `dblp.target_unseen_per_venue`：找到足够多未推送论文后停止翻页；
- `dblp.stop_after_seen_pages`：连续若干页没有新论文时提前停止；
- `max_results`：每次最多输出论文数；
- `min_score`：最低相关性阈值；
- `explicit_interests`：显式研究方向，建议使用完整英文短语；
- `openalex_enrich_limit`：最多为多少篇初排候选补摘要；
- `recent_interest_items`：用于兴趣建模的近期 Zotero 条目数；
- `zotero_dedup_items`：用于库内去重的 Zotero 扫描上限，`0` 表示全部；
- `zotero_collection_keys`：限定参与兴趣建模的 collection；
- `seen_db`：旧版兼容去重文件；
- `state_db`：SQLite 状态数据库；
- `run_lock`：本地运行锁路径。

`--no-update-seen` 用于预览，不会把论文记录为已投递。正式运行不要添加此参数。

## 本地命令

```bash
ccf-paper-scout --help
ccf-paper-scout doctor --config config.json
ccf-paper-scout test-delivery --config config.json
ccf-paper-scout run --config config.json --output recommendations.md
```

`doctor` 在运行前检查配置、Zotero 凭据、SMTP 配置、TLS 和状态目录。它只显示密钥是否存在及长度，不输出密钥值。

状态数据库默认位于 `state/paper_scout.sqlite3`，使用 SQLite WAL。首次运行会备份并迁移旧 `seen.json` 和翻译缓存。`python3 paper_scout.py` 目前仍作为兼容入口保留。

## 完整部署流程

下面的流程将项目部署为每日自动运行的个人论文推荐服务。推荐使用一个公开代码仓库和一个私有状态仓库。

### 1. Fork 或复制代码仓库

将本项目 Fork 到自己的 GitHub 账号，或新建仓库后推送代码。后续 Secrets、Variables 和 Workflows 都配置在自己的代码仓库中。

### 2. 准备 Zotero 只读凭据

1. 登录 Zotero Web；
2. 打开 <https://www.zotero.org/settings/security>；
3. 记录页面显示的数字 User ID；
4. 创建新的 API Key；
5. 只勾选个人文库读取权限，不需要写权限；
6. 妥善保存 API Key，GitHub 创建 Secret 后将无法再次读取原值。

需要的 GitHub Secrets：

```text
ZOTERO_USER_ID
ZOTERO_API_KEY
```

### 3. 获取 QQ 邮箱 SMTP 授权码

QQ 邮箱不能直接使用登录密码连接 SMTP，需要单独生成授权码。

1. 登录 QQ 邮箱网页版；
2. 打开“设置”；
3. 进入“账号与安全”或“账户”；
4. 找到“POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV 服务”；
5. 开启 SMTP 相关服务；
6. 按页面提示完成身份验证；
7. 生成并保存授权码。

项目默认使用：

```text
SMTP 主机：smtp.qq.com
端口：465
连接方式：SSL
用户名：完整 QQ 邮箱地址
密码：SMTP 授权码，不是 QQ 登录密码
```

需要的 GitHub Secrets：

```text
SMTP_SENDER
SMTP_RECEIVER
SMTP_PASSWORD
```

其中：

- `SMTP_SENDER`：发件 QQ 邮箱；
- `SMTP_RECEIVER`：接收日报的邮箱；
- `SMTP_PASSWORD`：QQ 邮箱 SMTP 授权码。

### 4. 配置 OpenAI-compatible API

如果需要中文论文卡片，在所使用的服务商处获取 API Key，并确认其支持 `/chat/completions` 接口。

需要的 GitHub Secrets：

```text
LLM_API_KEY
LLM_BASE_URL
LLM_MODEL
```

示例：

```text
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
```

如果暂时不使用 LLM，可以在本地配置中将 `llm_translation.enabled` 设为 `false`。当前云端配置生成器默认启用 LLM，因此云端部署应提供以上三项。

### 5. 创建私有状态仓库

在同一 GitHub 账号下新建私有仓库，例如：

```text
your-name/ccf-paper-scout-state
```

该仓库用于保存：

```text
state/paper_scout.sqlite3
state/seen.json
state/translations.json
```

不要把状态仓库设为公开。SQLite、历史推荐和翻译缓存可能反映个人研究兴趣。

初始化建议结构：

```text
README.md
.gitignore
state/
```

### 6. 创建状态仓库访问令牌

打开 GitHub Fine-grained personal access tokens 页面：

<https://github.com/settings/personal-access-tokens/new>

创建 Token 时：

1. Repository access 选择 `Only select repositories`；
2. 只选择私有状态仓库；
3. Repository permissions 中将 `Contents` 设为 `Read and write`；
4. 其他权限保持 `No access`；
5. 设置合理的过期时间，并记录续期日期。

将 Token 保存为代码仓库的 Secret：

```text
STATE_REPO_TOKEN
```

将私有状态仓库全名保存为代码仓库 Variable：

```text
STATE_REPO=your-name/ccf-paper-scout-state
```

### 7. 写入 GitHub Secrets 和 Variables

打开代码仓库：

```text
Settings
→ Secrets and variables
→ Actions
```

在 `Secrets` 页签创建：

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

在 `Variables` 页签创建：

```text
STATE_REPO=your-name/ccf-paper-scout-state
```

每日运行时间当前由 workflow 中的 `cron: '0 1 * * *'` 固定为北京时间 09:00。如果需要调整时间，请修改 `.github/workflows/paper-scout-daily.yml`；项目不会读取 `TIMEZONE` Variable。

Secret 保存后 GitHub 不会显示原值。可以通过更新时间确认名称已创建，但必须运行 workflow 才能验证值是否有效。不要在 Issue、PR、Actions 日志或聊天中粘贴任何密钥。

### 8. 按顺序完成云端验收

进入：

```text
Actions
→ Paper Scout Manual
→ Run workflow
```

依次运行：

1. `doctor`：只检查当前生成的配置所需项。注意，云端配置生成器默认启用 LLM 和 SMTP，因此即使选择 `doctor`，也要先配置 `SMTP_SENDER`、`SMTP_RECEIVER`、`LLM_BASE_URL` 和 `LLM_MODEL`；`doctor` 还会检查 Zotero 凭据和 SMTP 授权码是否存在，并测试 SMTP TLS；
2. `smtp-test`：只发送一封测试邮件，不抓取论文、不修改状态；
3. `preview`：恢复私有状态、读取 Zotero、检索论文并生成推荐报告，不发送邮件、不更新去重状态；
4. `production`：建议先设 `max_results=1`，确认正式邮件、SQLite 和状态提交都正常。

按上述顺序执行可以更容易定位配置、邮件、数据读取和状态持久化问题。`production` 会发送真实邮件并修改私有状态。

验收完成后检查：

- 收件箱和垃圾箱中能收到测试邮件；
- 正式推荐邮件内容完整；
- 私有状态仓库出现 `state/paper_scout.sqlite3`；
- `.delivery-pending` 已在成功运行后删除；
- Actions 日志中没有密钥明文；
- 第二次运行不会再次推荐已投递论文。

### 9. 启用每日运行

`Paper Scout Daily` 使用：

```yaml
cron: '0 1 * * *'
```

对应北京时间 09:00。确认 manual production 成功后再启用 daily workflow。如果之前使用本机 cron、Windows Task Scheduler 或其他云端调度器，应将它们停用，避免重复邮件。

### 10. 令牌维护

Fine-grained Token 到期后，状态恢复会在 `Restore private state` 阶段失败。续期或重新生成 Token 后，覆盖 `STATE_REPO_TOKEN` Secret 即可，不需要修改 workflow。

## GitHub Actions 云端部署

仓库提供三套工作流：

- `Paper Scout Manual`：支持 `doctor`、`smtp-test`、`preview` 和 `production`；
- `Paper Scout Daily`：每天北京时间 09:00 自动运行；
- `Keep Scheduled Workflows Active`：降低公开仓库长期无提交后定时任务被停用的风险。

正式运行使用共享 concurrency group，避免手动 production 与每日任务同时发送。系统在 SMTP 前向私有状态仓库写入 `.delivery-pending`；如果上一次运行处于无法确认是否已发送的状态，后续 production 会停止并要求人工检查。SMTP 成功后，系统执行 SQLite checkpoint、移除 pending 标记并提交最终状态。

云端生产流程：

```text
Checkout 代码
→ 安装项目
→ 恢复并验证私有状态
→ 生成临时配置
→ 检查未完成投递
→ 写入 pending 标记
→ 生成论文推荐并发送邮件
→ SQLite checkpoint
→ 提交最终状态
```

## LLM 中文论文卡片

项目支持 OpenAI-compatible `/chat/completions` API。密钥只通过环境变量或 GitHub Secret 提供，不要写入 `config.json`。

本地配置示例：

```json
"llm_translation": {
  "enabled": true,
  "base_url": "https://api.openai.com/v1",
  "model": "gpt-4o-mini",
  "api_key_env": "LLM_API_KEY",
  "language": "简体中文",
  "timeout_seconds": 90,
  "cache": "state/translations.json"
}
```

程序只分析最终选中的论文。结果包括中文标题与摘要、研究问题、方法、创新、摘要披露的证据、局限、兴趣相关性和主题标签。内容要求以输入标题和摘要为依据，摘要没有披露的事实应标为“摘要未披露”。单篇分析失败只产生 warning，不中断整次推荐。

## SMTP 邮件投递

本地配置示例：

```json
"delivery": {
  "smtp": {
    "enabled": true,
    "host": "smtp.qq.com",
    "port": 465,
    "use_ssl": true,
    "sender": "sender@qq.com",
    "receiver": "receiver@example.com",
    "password_env": "SMTP_PASSWORD",
    "subject": "CCF Paper Scout 每日论文推荐"
  }
}
```

只有 SMTP 服务端接受邮件后，系统才会把论文标记为 delivered。发送失败会使运行失败，论文不会进入已投递集合。

本地测试：

```bash
export SMTP_PASSWORD='SMTP 授权码'
ccf-paper-scout test-delivery --config config.json
```

测试模式不读取 Zotero、DBLP、OpenAlex 或 LLM，也不会修改推荐状态。

## Zotero 读取检查

需要确认参与兴趣建模的论文时，可在 `config.json` 中启用：

```json
"debug": {
  "list_zotero_items": true,
  "zotero_output": "zotero_library_debug.md"
}
```

运行后会生成 `zotero_library_debug.md`，其中列出实际参与兴趣建模的 item type、标题、Zotero key、加入时间和摘要。该文件可能包含私人文库信息，已经被 `.gitignore` 排除，不要上传到公开仓库。

## 可能遇到的网络问题

### 1. 域名解析失败

典型错误：

```text
Temporary failure in name resolution
Name or service not known
```

先检查目标域名：

```bash
getent hosts api.zotero.org
getent hosts dblp.org
getent hosts api.openalex.org
```

再测试 HTTPS：

```bash
curl -I --connect-timeout 10 https://api.zotero.org/
curl -I --connect-timeout 10 https://dblp.org/
```

如果 WSL 反复解析失败，可在 Windows PowerShell 执行 `wsl --shutdown` 后重新打开 WSL。仍未恢复时检查 VPN、代理、防火墙和 WSL DNS 设置。

### 2. Zotero 返回 401 或 403

这表示网络已经连通，但 Zotero 拒绝了身份或权限。检查：

1. `ZOTERO_USER_ID` 是否为 Zotero 安全设置页显示的数字 User ID；
2. API Key 是否属于同一账号；
3. API Key 是否拥有个人文库只读权限；
4. Secret 或环境变量是否包含额外空格、引号或换行；
5. 是否误把用户名、Library ID 或 Group ID 当成 User ID。

可以安全检查变量是否存在和长度：

```bash
python3 -c 'import os; print("ID present:", bool(os.getenv("ZOTERO_USER_ID"))); print("KEY length:", len(os.getenv("ZOTERO_API_KEY", "")))'
```

不要把 API Key 打印到日志或聊天中。

### 3. DBLP 连接中断或服务端错误

典型错误：

```text
RemoteDisconnected
Connection reset by peer
HTTP 500
all DBLP endpoints failed
```

程序会重试 DBLP 主站，并自动切换到 Trier 镜像。主站和镜像同时异常时，本次运行会失败，等待几分钟后手动重跑通常可以恢复。持续失败时分别检查：

```bash
curl -I --connect-timeout 10 https://dblp.org/
curl -I --connect-timeout 10 https://dblp.uni-trier.de/
```

不要在 DBLP 不可用时绕过 venue 资格校验发送未核验候选。

### 4. OpenAlex 无摘要、404 或限流

OpenAlex 只负责补充摘要，不是候选资格来源。常见情况包括：

- DOI 尚未收录；
- work 存在但没有摘要；
- HTTP 404；
- HTTP 429；
- 连接超时或 5xx。

程序会把单篇补全记为 missing 或 failed，并继续使用标题排序。频繁遇到限流时，降低 `openalex_enrich_limit`，不要通过无限重试放大请求。

### 5. LLM API 返回 401、429 或超时

- 401/403：检查 `LLM_API_KEY`、服务地址和账号权限；
- 404：检查 `LLM_BASE_URL` 是否已经包含正确 API 根路径，以及模型名称是否存在；
- 429：账户余额、速率限制或并发限制不足；
- 超时/5xx：服务商暂时不可用，可稍后重跑。

LLM 失败不会改变 CCF-A 资格和本地排序，但对应论文可能缺少中文分析。

### 6. QQ SMTP 认证失败

典型错误：

```text
SMTPAuthenticationError
535 Authentication failed
```

检查：

1. `SMTP_SENDER` 是否为完整 QQ 邮箱地址；
2. `SMTP_PASSWORD` 是否为 SMTP 授权码，而不是 QQ 登录密码；
3. QQ 邮箱是否已经开启 SMTP 服务；
4. 授权码是否被撤销或过期；
5. 主机是否为 `smtp.qq.com`，SSL 端口是否为 `465`。

先运行 `Paper Scout Manual` 的 `smtp-test`，确认后再运行 production。

### 7. 私有状态仓库恢复失败

典型错误：

```text
fatal: could not read Username for 'https://github.com'
Repository not found
HTTP 403
```

检查：

1. 代码仓库中是否存在非空的 `STATE_REPO_TOKEN` Secret；
2. Token 是否只授权了正确的私有状态仓库；
3. Token 的 `Contents` 权限是否为 `Read and write`；
4. `STATE_REPO` Variable 是否为完整的 `owner/repository`；
5. Fine-grained Token 是否已经过期。

Secret 的原值无法从 GitHub 读取。更新后应运行 manual preview，以“Restore private state”成功作为有效性证明。

### 8. GitHub Actions 定时任务没有触发

检查：

1. `Paper Scout Daily` workflow 是否为 active；
2. workflow 文件是否位于默认分支；
3. cron 是否为 `0 1 * * *`；
4. 公开仓库是否因长期无活动被 GitHub 暂停 schedule；
5. 同一 concurrency group 中是否仍有运行中的 production；
6. Actions 页面是否显示仓库级 Actions 被禁用。

GitHub schedule 可能比设定时间晚几分钟，这不代表任务失效。`Keep Scheduled Workflows Active` 会定期更新活动标记。

### 9. 未解决的投递事务

如果日志提示：

```text
Unresolved prior delivery exists
```

说明私有状态仓库中存在 `.delivery-pending`。这通常表示 SMTP 前后的 runner 或状态提交发生中断，系统无法自动判断邮件是否已经被服务端接受。

处理时先查看对应 Actions run 和收件箱，再决定：

- 邮件已收到：修复或补写 delivered 状态后移除 pending；
- 邮件未收到：确认失败原因后移除 pending，再重新运行 production。

不要直接删除 pending 后立刻重跑，否则可能重复发送。

## 数据、隐私与许可证

- Venue 数据来源和限制：[`data/README.md`](data/README.md)
- 隐私与数据流：[`docs/privacy.md`](docs/privacy.md)
- 当前能力边界：[`docs/limitations.md`](docs/limitations.md)
- 代码与数据溯源：[`docs/provenance.md`](docs/provenance.md)
- 第三方归属：[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)

项目代码采用 Apache-2.0。`data/ccf_a_venues.json` 从 `WenyanLiu/CCFrank4dblp` 固定版本的 MIT 数据派生。该映射不是 CCF 官方 API，应定期与最新官方目录复核。

Zotero 兴趣语料只在排序阶段使用，不会发送给 DBLP、OpenAlex 或 LLM。LLM 只接收最终候选的标题、摘要和显式兴趣方向。私有状态仓库可能反映研究兴趣，必须保持 private。

## 当前能力边界

- “最新”指数据源已索引的正式论文，不等于刚投稿的预印本；
- DBLP 通常不提供摘要，OpenAlex 补全失败时只能依赖标题排序；
- 当前排序器是内容相关性算法，不表示论文影响力或科学质量；
- 当前算法依赖英文词面重合，不是标准 TF-IDF、BM25 或语义 embedding；
- GitHub-hosted runner 与 SMTP 无法组成绝对原子事务，项目使用 pending 标记阻止不确定状态下自动重发；
- GitHub schedule 可能有数分钟延迟；
- 项目当前面向单个研究者和单个状态仓库，不提供多用户权限系统。

## 项目状态与路线

当前版本已经完成个人每日使用所需的完整链路：Zotero 兴趣读取、CCF-A venue 约束、DBLP 分页检索、相关性排序、中文论文卡片、SMTP 投递、SQLite 状态和 GitHub Actions 每日运行。

后续优先方向：

1. 建立离线相关性评测集与 Precision@K、nDCG、MRR 指标；
2. 比较标准 TF-IDF cosine 和 BM25；
3. 显示 Top-K 相似 Zotero 论文作为推荐依据；
4. 使用 MMR 和 venue/topic 配额提高多样性；
5. 增加显式反馈闭环；
6. 在明确区分正式核验与早期候选的前提下扩展 OpenReview 等来源。

## 致谢

项目为独立实现，论文发现流程受到 [`TideDra/zotero-arxiv-daily`](https://github.com/TideDra/zotero-arxiv-daily) 启发；venue 数据派生自 [`WenyanLiu/CCFrank4dblp`](https://github.com/WenyanLiu/CCFrank4dblp)。致谢不表示代码复用、隶属或背书。
