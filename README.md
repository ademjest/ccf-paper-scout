# CCF-A Paper Scout

一个非官方、低资源、可解释的 Zotero 个性化正式论文候选筛选器。它先用配置的 CCF-A venue 白名单约束 DBLP 检索，再以 DBLP record-key 前缀复核降低文本误命中，最后在本地结合 Zotero 标题/摘要和显式兴趣排序。OpenAlex 摘要补全、LLM 双语特色提炼和 SMTP 邮件投递均为可选功能。

> CCF venue 等级是 venue 级参考信号，不代表对单篇论文科学质量的评价。本项目不隶属于或受 CCF、Zotero、DBLP、OpenAlex、任何会议或出版社背书。

## 核心原则

1. **venue 资格约束先于推荐**：候选论文必须命中维护过的 CCF-A venue 白名单；这不是对单篇质量的认证。
2. **DBLP key 优先**：用 DBLP record key（如 `conf/nips/...`）做前缀复核，避免只靠易混淆的 venue 文本；它与白名单不是两个独立认证源。
3. **低资源**：仅 Python 标准库；默认使用带时间权重的稀疏词项匹配，不需要 GPU、Torch 或向量数据库。
4. **有边界的隐私设计**：Zotero 兴趣语料仅在本地参与排序，不发送给 DBLP、OpenAlex 或 LLM；可选 LLM 只接收最终候选的标题和摘要。
5. **可解释与可读**：每条推荐显示贡献词、venue、年份、DBLP 链接，并可用 LLM生成论文聚焦、问题、方法、创新、证据、局限与相关性提示。

## 快速开始

要求 Python 3.11+，无需安装依赖。

```bash
git clone https://github.com/ademjest/ccf-paper-scout.git
cd ccf-paper-scout
cp config.example.json config.json
cp interests.example.json interests.json
python3 paper_scout.py --config config.json --interests interests.json --output recommendations.md
```

`interests.json` 可用来先体验。正式使用 Zotero Web API：

```bash
export ZOTERO_USER_ID=你的数字用户ID
export ZOTERO_API_KEY=只读API密钥
python3 paper_scout.py --config config.json --output recommendations.md
```

程序会读取 `journalArticle`、`conferencePaper` 和 `preprint`，优先使用标题和摘要。建议在配置中设置 `zotero_collection_keys`，只让“已读/重点/当前课题”集合参与兴趣建模，避免整个多年文库稀释兴趣。

## 配置说明

- `venue_keys`：DBLP venue key 列表。默认示例偏 AI；可从 `data/ccf_a_venues.json` 选择其他 CCF-A venue。
- `years`：候选年份。会议论文正式出版存在延迟，建议同时保留本年和上一年。
- `dblp.page_size`：每次 DBLP 搜索页大小。
- `dblp.max_pages_per_venue`：每个 venue/年份单次最多翻页数，限制请求成本。
- `dblp.target_unseen_per_venue`：找到多少篇历史未推送论文后停止翻页。
- `dblp.stop_after_seen_pages`：连续多少页没有新论文后停止，避免无效扫描。
- `max_results`：最终输出数量。
- `min_score`：最低相关度；默认建议 `0.01`，避免为了凑数推送完全无关论文。
- `explicit_interests`：显式研究方向，作为强正反馈加入排序；建议使用完整英文短语，如 `reinforcement learning`、`large language models`、`LLM agents`。
- `openalex_enrich_limit`：先按标题相关性排序，再对前 N 个有 DOI 的候选从 OpenAlex 补摘要；设为 `0` 可获得最少请求、仅标题排序。
- `recent_interest_items`：只取最近加入 Zotero 的多少篇论文作为兴趣。
- `zotero_dedup_items`：用于库内去重的全库扫描上限；`0` 表示扫描全部符合类型的 Zotero 条目，不受兴趣 collection 限制。
- `zotero_collection_keys`：可选；只读取这些 Zotero collection key。
- `seen_db`：已推荐记录，防止重复推送。加 `--no-update-seen` 可试跑而不更新。

## 可安装 CLI 与状态库

项目提供标准 Python 包配置。安装后可使用：

```bash
python -m pip install .
ccf-paper-scout --help
ccf-paper-scout doctor --config config.json
ccf-paper-scout test-delivery --config config.json
```

`doctor` 检查配置、凭据是否存在（只显示长度，不显示值）、SMTP TLS 和状态目录。状态数据库默认位于 `state/paper_scout.sqlite3`，使用 SQLite WAL；首次运行会备份并迁移旧 `seen.json` 与翻译缓存。为兼容已有脚本，`python3 paper_scout.py` 暂时继续可用。

## Zotero 读取调试

在 `config.json` 中启用：

```json
"debug": {
  "list_zotero_items": true,
  "zotero_output": "zotero_library_debug.md"
}
```

每次运行都会把实际用于兴趣建模的 Zotero 文献逐条写入 `zotero_library_debug.md`，包括 item type、标题、Zotero key、加入时间和摘要。注意：它列出的是受 `recent_interest_items` 与 `zotero_collection_keys` 限制后的语料；若要检查更多条目，提高 `recent_interest_items`。

## LLM 标题和摘要翻译

支持 OpenAI-compatible `/chat/completions` API。不要将密钥写入 `config.json`，而应使用环境变量：

```bash
export LLM_API_KEY='你的密钥'
```

然后配置：

```json
"llm_translation": {
  "enabled": true,
  "base_url": "https://你的服务地址/v1",
  "model": "你的模型名",
  "api_key_env": "LLM_API_KEY",
  "language": "简体中文",
  "timeout_seconds": 90,
  "cache": "state/translations.json"
}
```

不要只填写 `api_key` 和 `base_url`：还必须将 `enabled` 改为 `true`。启动日志第 7 步会明确显示 LLM 是 disabled、misconfigured，还是 enabled，并在运行后报告 API 成功数与缓存命中数。

程序只分析最终选中的论文，结果缓存到 `state/translations.json`，同一论文再次出现时不会重复调用 API。输出除了双语标题和摘要，还可包含：论文聚焦、解决问题、核心方法、主要创新、摘要披露的证据/实验、局限提示、为何与你的显式兴趣相关以及主题标签。所有内容都被要求严格基于标题与摘要；摘要没有披露的事实会标记为“摘要未披露”，而不是猜测。单篇分析失败只产生 warning，不会中断整次推荐。

## SMTP 邮件投递

在 `config.json` 中启用 `delivery.smtp`，密码只放在环境变量：

```bash
export SMTP_PASSWORD='邮箱服务商提供的 SMTP 密码或授权码'
```

```json
"delivery": {
  "smtp": {
    "enabled": true,
    "host": "smtp.example.com",
    "port": 465,
    "use_ssl": true,
    "sender": "sender@example.com",
    "receiver": "receiver@example.com",
    "password_env": "SMTP_PASSWORD",
    "subject": "CCF Paper Scout 每日论文推荐"
  }
}
```

启用 SMTP 后，只有服务端接受邮件后，论文才会写入 `state/seen.json`。发送失败会使运行失败且不标记 delivered，下一次定时任务可重试。未启用任何投递通道时，生成 Markdown 即视为本地交付成功。

在启用 cron 前，先执行真实 SMTP 测试：

```bash
python3 paper_scout.py --config config.json --test-delivery
```

该模式只发送一封测试邮件，不访问 Zotero/DBLP/OpenAlex/LLM，也不会生成推荐或修改 `state/seen.json`。确认收件箱、垃圾箱、中文主题和发件人均正常后，再启用每日任务。

## 网络错误排查

若出现 `Temporary failure in name resolution`，这是 WSL 当时未能解析 Zotero/DBLP/OpenAlex 域名，而不是 API Key 错误。程序会自动重试 3 次。先运行：

```bash
getent hosts api.zotero.org
curl -I --connect-timeout 10 https://api.zotero.org/
```

两条命令均成功后重新执行程序即可。如果在 WSL 中反复出现且 `getent` 失败，可在 Windows PowerShell 执行 `wsl --shutdown` 后重新打开 WSL；长期故障再检查 VPN/代理、防火墙和 WSL DNS 配置。不要把 API Key 粘贴到诊断输出中。

若 DBLP 出现 `RemoteDisconnected` 或连接被重置，程序会先重试主站，然后自动切换到 DBLP Trier 镜像 `dblp.uni-trier.de`。这通常是 DBLP 主站或中间网络临时断开，不是 Zotero 配置问题。

若出现 `HTTP 403: Forbidden`，网络已经正常，但 Zotero 拒绝了凭据或库权限。请在 https://www.zotero.org/settings/security 核对：

1. `ZOTERO_USER_ID` 是页面显示的数字 User ID，不是用户名或 Library ID；
2. API key 与该 User ID 属于同一账号；
3. key 至少具有个人库的只读权限；
4. 在当前终端重新 `export ZOTERO_USER_ID=...` 和 `export ZOTERO_API_KEY=...`，注意不要带多余引号、空格或换行。

可以安全地只检查变量是否存在及长度（不会打印密钥）：

```bash
python3 -c 'import os; print("ID:", os.getenv("ZOTERO_USER_ID")); print("KEY length:", len(os.getenv("ZOTERO_API_KEY", "")))'
```

## 每日去重与北京时间 09:00 定时运行

> 当前实现面向 Linux/WSL 单机个人使用；程序使用 `fcntl.flock` 获取 `state/paper_scout.lock`，cron 与手动运行重叠时第二个进程会安全退出。报告、翻译缓存和 `state/seen.json` 使用原子替换。更完整的运行历史与投递事务仍计划迁移到 SQLite。

程序使用 DBLP record key 作为稳定论文 ID，并将每次**实际输出**的论文写入 `state/seen.json`。下一次运行会分页搜索并排除这些 ID；同时使用 Zotero 中的 DOI、DBLP/arXiv ID 和标准化标题排除已经收藏的论文。因此昨天推送过或 Zotero 已有的论文都不会再次进入推荐。运行日志会分别显示历史推送排除和 Zotero 库内排除数量。

注意：

- 正式定时任务不要添加 `--no-update-seen`，否则不会记录本次推送；
- 不要定期删除 `state/seen.json`；
- 调试时用了 `--no-update-seen` 不会污染历史；
- 若报告最终为空，程序不会伪造或重复旧论文来凑满 20 篇；
- 如果在容器或 GitHub Actions 中运行，必须持久化 `state/`，否则每次都是全新文件系统，去重会失效。

WSL 中的 cron 使用 Linux 当前时区。先确认：

```bash
date '+%F %T %Z %z'
```

若显示 `CST +0800` 或其他 UTC+8 时区，每天北京时间 09:00 可加入 `crontab -e`：

```cron
0 9 * * * PROJECT_DIR=$HOME/ccf-paper-scout $HOME/ccf-paper-scout/scripts/run_daily.sh >> $HOME/ccf-paper-scout/paper_scout.log 2>&1
```

如果 WSL 不是 UTC+8，可在 crontab 顶部显式声明：

```cron
CRON_TZ=Asia/Shanghai
0 9 * * * PROJECT_DIR=$HOME/ccf-paper-scout $HOME/ccf-paper-scout/scripts/run_daily.sh >> $HOME/ccf-paper-scout/paper_scout.log 2>&1
```

cron 通常不会加载交互式 shell 的 `export`。创建权限为 `600` 且被 `.gitignore` 排除的 `$HOME/ccf-paper-scout/.env.local`，写入 `ZOTERO_USER_ID`、`ZOTERO_API_KEY`、可选 `LLM_API_KEY` 与 `SMTP_PASSWORD`。包装脚本 `scripts/run_daily.sh` 会自动加载它；不要把任何密钥写进 crontab 或 Git 仓库。

## 为什么不是“直接搜 arXiv + CCF-A 名称匹配”

arXiv 的 `comment` 中出现 “accepted at ...” 不是权威录用元数据，且存在 workshop、findings、short paper、同名 venue 和错误声明。稳妥的双通道方案是：

- **严格通道（本 MVP）**：DBLP / 官方 proceedings / OpenReview 已接收列表 → CCF-A 白名单；精度高，可能稍晚。
- **早期通道（后续可选）**：arXiv/OpenReview 新稿 → 声称 venue → 再与官方 accepted-paper 列表核验；更快，但不能在核验前标作 CCF-A 论文。

## 数据、隐私与许可证

- Venue 数据来源和限制：[`data/README.md`](data/README.md)
- 隐私与数据流：[`docs/privacy.md`](docs/privacy.md)
- 当前能力边界和新增论文源策略：[`docs/limitations.md`](docs/limitations.md)
- 代码与数据溯源：[`docs/provenance.md`](docs/provenance.md)
- 本项目代码采用 Apache-2.0；第三方归属见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。

`data/ccf_a_venues.json` 是从 `WenyanLiu/CCFrank4dblp` 固定版本的公开 MIT 数据文件派生的 venue 映射。CCF 等级应定期与官方最新版目录复核；本项目不宣称该派生表是 CCF 官方 API。

## Acknowledgements

本项目为独立实现，概念上受到 `TideDra/zotero-arxiv-daily` 的 Zotero 个性化论文发现流程启发；venue 数据派生自 `WenyanLiu/CCFrank4dblp`。未来反馈设计参考了 `yuandong-tian/arXiv_recbot`。致谢不表示代码复用、隶属或背书。

## 当前 MVP 的边界

- DBLP 搜索结果通常没有摘要；程序会对有 DOI 的前若干候选通过 OpenAlex 补摘要，失败时自动回退到标题排序。
- “最新”指数据源已经索引的正式论文，不等于刚投稿的预印本。
- 排序是内容相关性，不等同于论文影响力。正式版可再加入 citation percentile、最佳论文标记、作者/实验室偏好，但不应让引用数压倒新论文。
