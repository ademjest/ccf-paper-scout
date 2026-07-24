# CCF-A Paper Scout

一个低资源、可解释的 Zotero 兴趣论文推荐 MVP。它不把 arXiv 当作“质量标签”，而是先从 DBLP 获取已被目标 venue 收录的论文，再用 CCF-A 白名单做硬过滤，最后按你的 Zotero 论文标题/摘要做本地排序。

## 核心原则

1. **质量过滤先于推荐**：候选论文必须命中维护过的 CCF-A venue 白名单。
2. **DBLP key 优先**：用 DBLP record key（如 `conf/nips/...`）识别会议，避免只靠易混淆的 venue 文本。
3. **低资源**：仅 Python 标准库；稀疏 TF-IDF + BM25 风格词项评分，不需要 GPU、Torch、向量数据库或 LLM。
4. **隐私友好**：Zotero 数据只在本机读取；只向 DBLP 请求公开候选元数据。
5. **可解释**：每条推荐显示匹配关键词、venue、年份和 DBLP 链接。

## 快速开始

要求 Python 3.11+，无需安装依赖。

```bash
cd /home/zlw/ccf-paper-scout
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
- `per_venue`：每个 venue/年份最多拉取多少条。
- `max_results`：最终输出数量。
- `min_score`：最低相关度；默认建议 `0.01`，避免为了凑数推送完全无关论文。
- `openalex_enrich_limit`：按 DOI 从 OpenAlex 补充摘要的候选上限；设为 `0` 可获得最少请求、仅标题排序。
- `recent_interest_items`：只取最近加入 Zotero 的多少篇论文作为兴趣。
- `zotero_collection_keys`：可选；只读取这些 Zotero collection key。
- `seen_db`：已推荐记录，防止重复推送。加 `--no-update-seen` 可试跑而不更新。

## 网络错误排查

若出现 `Temporary failure in name resolution`，这是 WSL 当时未能解析 Zotero/DBLP/OpenAlex 域名，而不是 API Key 错误。程序会自动重试 3 次。先运行：

```bash
getent hosts api.zotero.org
curl -I --connect-timeout 10 https://api.zotero.org/
```

两条命令均成功后重新执行程序即可。如果在 WSL 中反复出现且 `getent` 失败，可在 Windows PowerShell 执行 `wsl --shutdown` 后重新打开 WSL；长期故障再检查 VPN/代理、防火墙和 WSL DNS 配置。不要把 API Key 粘贴到诊断输出中。

若出现 `HTTP 403: Forbidden`，网络已经正常，但 Zotero 拒绝了凭据或库权限。请在 https://www.zotero.org/settings/security 核对：

1. `ZOTERO_USER_ID` 是页面显示的数字 User ID，不是用户名或 Library ID；
2. API key 与该 User ID 属于同一账号；
3. key 至少具有个人库的只读权限；
4. 在当前终端重新 `export ZOTERO_USER_ID=...` 和 `export ZOTERO_API_KEY=...`，注意不要带多余引号、空格或换行。

可以安全地只检查变量是否存在及长度（不会打印密钥）：

```bash
python3 -c 'import os; print("ID:", os.getenv("ZOTERO_USER_ID")); print("KEY length:", len(os.getenv("ZOTERO_API_KEY", "")))'
```

## 定时运行

Linux/WSL cron 示例（每天 08:00）：

```cron
0 8 * * * cd /home/zlw/ccf-paper-scout && /usr/bin/python3 paper_scout.py --config config.json --output recommendations.md >> scout.log 2>&1
```

MVP 默认生成 Markdown，不主动发送邮件。邮件、Telegram、企业微信等推送应作为独立 adapter 接在报告之后，避免把检索、推荐和投递耦合。

## 为什么不是“直接搜 arXiv + CCF-A 名称匹配”

arXiv 的 `comment` 中出现 “accepted at ...” 不是权威录用元数据，且存在 workshop、findings、short paper、同名 venue 和错误声明。稳妥的双通道方案是：

- **严格通道（本 MVP）**：DBLP / 官方 proceedings / OpenReview 已接收列表 → CCF-A 白名单；精度高，可能稍晚。
- **早期通道（后续可选）**：arXiv/OpenReview 新稿 → 声称 venue → 再与官方 accepted-paper 列表核验；更快，但不能在核验前标作 CCF-A 论文。

## 数据与许可说明

`data/ccf_a_venues.json` 是从 `WenyanLiu/CCFrank4dblp` 的公开 MIT 数据文件派生的 venue 映射，源文件标注更新于 2026-04-06。CCF 等级应定期与 CCF 官方最新版目录复核；本项目不宣称该派生表是 CCF 官方 API。

## 当前 MVP 的边界

- DBLP 搜索结果通常没有摘要；程序会对有 DOI 的前若干候选通过 OpenAlex 补摘要，失败时自动回退到标题排序。
- “最新”指数据源已经索引的正式论文，不等于刚投稿的预印本。
- 排序是内容相关性，不等同于论文影响力。正式版可再加入 citation percentile、最佳论文标记、作者/实验室偏好，但不应让引用数压倒新论文。
