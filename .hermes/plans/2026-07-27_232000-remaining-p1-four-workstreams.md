# 剩余 P1 四项工作实施计划

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** 完成多 collection 公平兴趣建模、SQLite 事务状态、可安装 CLI/doctor，以及 QQ 邮箱真实 SMTP 与北京时间 09:00 定时验收，使个人每日推送具备可长期维护的 P1 基础。

**Architecture:** 先修复纯数据读取逻辑，再引入可迁移 SQLite 状态层，之后模块化为 `src/ccf_paper_scout` 包并建立 doctor，最后在不提交任何凭据的前提下完成 QQ SMTP 真实验收和 cron 安装。迁移期间保留 `paper_scout.py` 兼容入口，避免打断现有个人使用。

**Tech Stack:** Python 3.11+ 标准库、unittest、sqlite3/WAL、fcntl、smtplib、pyproject.toml、GitHub Actions、cron。

---

## 工作 1：修复多 collection 兴趣语料的全局截断偏置

### 目标

保证多个 Zotero collection 都完整参与候选合并，再全局按 `dateAdded` 排序并应用 `recent_interest_items`，不让第一个 collection 提前耗尽总上限。

### 设计

当前风险是每个 collection 顺序读取时共享全局 cap。改为：

```text
逐 collection 分页获取（每个 collection 不受全局 recent cap 影响）
→ 以 Zotero item key 合并去重
→ 全局按 dateAdded 降序
→ 最后应用 recent_interest_items
```

全库去重索引继续走独立扫描，不与兴趣语料混用。

### 文件

- 迁移前修改：`paper_scout.py`
- 迁移后目标：`src/ccf_paper_scout/zotero.py`
- 测试：`tests/test_scout.py`，之后迁移为 `tests/test_zotero.py`
- 文档：`README.md`

### TDD 步骤

1. 写失败测试：collection A 返回 200 篇旧论文，collection B 返回 1 篇更新论文，`recent_interest_items=200`；B 的新论文必须保留。
2. 写失败测试：同一 Zotero key 出现在两个 collection，只保留一条。
3. 写失败测试：collection 顺序互换后最终 key 集合不变。
4. 抽取 `fetch_zotero_collection_pages()` 和 `merge_interest_items()`。
5. 让 cap 只作用于合并排序后的列表。
6. 保持 `zotero_dedup_items=0` 的全库索引逻辑独立。

### 验收

```bash
python3 -m unittest tests.test_scout -v
```

预期：collection 顺序不影响最终兴趣语料；全库去重仍正常。

---

## 工作 2：SQLite 状态、运行历史与迁移

### 目标

用 SQLite 替代 `seen.json` 和 `translations.json`，提供事务、WAL、运行历史、投递状态、抓取游标和可恢复迁移。

### MVP 表结构

```sql
schema_migrations(version, applied_at)
papers(paper_id PRIMARY KEY, title, doi, first_seen_at)
external_ids(kind, value, paper_id, UNIQUE(kind, value))
recommendation_runs(run_id PRIMARY KEY, started_at, finished_at, status, config_hash,
                    raw_hits, delivered_skipped, zotero_skipped, eligible, selected, error_summary)
recommendation_items(run_id, paper_id, rank, score, state,
                     PRIMARY KEY(run_id, paper_id))
delivery_attempts(attempt_id PRIMARY KEY, run_id, channel, status, started_at,
                  finished_at, provider_message, idempotency_key UNIQUE)
translation_cache(cache_key PRIMARY KEY, fingerprint, payload_json, updated_at)
source_cursors(source, venue_key, year, next_offset, last_checked_at,
               PRIMARY KEY(source, venue_key, year))
```

### 事务语义

```text
开始 run → selected/rendered → SMTP pending
SMTP 接受 → 在事务中写 delivered + 去重状态
SMTP 失败 → delivery_failed，不标记论文 delivered
进程中断 → run 保留 failed/interrupted，可审计重试
```

### 文件

- 创建：`src/ccf_paper_scout/state.py`
- 创建：`src/ccf_paper_scout/migrations.py`
- 创建：`tests/test_state.py`
- 创建：`tests/test_migrations.py`
- 修改：`paper_scout.py` 兼容入口
- 修改：`.gitignore`、`README.md`、`docs/privacy.md`

### 迁移策略

1. 首次运行创建 `state/paper_scout.sqlite3`，启用 WAL 和 foreign keys。
2. 检测旧 `state/seen.json` 与 `state/translations.json`。
3. 先复制为带时间戳的 `.bak`。
4. 在单一事务中导入；成功后记录 migration version。
5. 重复启动不得重复插入。
6. 旧 JSON 损坏时给出可操作错误，不删除原文件。

### TDD 步骤

1. 写 schema 初始化失败测试。
2. 写 delivered 唯一约束和事务回滚测试。
3. 写 SMTP 失败不标记 delivered 测试。
4. 写 JSON 迁移幂等测试。
5. 写损坏 JSON 不破坏数据库测试。
6. 写 source cursor 保存/恢复测试。
7. 接入现有 `seen`、LLM cache、诊断计数和 SMTP 流程。

### 验收

```bash
python3 -m unittest tests.test_state tests.test_migrations -v
sqlite3 state/paper_scout.sqlite3 'PRAGMA integrity_check;'
```

预期：`ok`；迁移重复执行无副作用。

---

## 工作 3：可安装 CLI 与 doctor

### 目标

将单文件项目变成可安装包，同时保留当前命令兼容性，并在发起网络请求前检查配置、凭据存在性、路径和网络端点。

### 目标结构

```text
pyproject.toml
src/ccf_paper_scout/
  __init__.py
  cli.py
  config.py
  models.py
  http.py
  zotero.py
  venues.py
  sources/dblp.py
  enrichers/openalex.py
  ranking/lexical.py
  translation.py
  state.py
  render.py
  delivery/smtp.py
paper_scout.py  # 兼容 shim
```

### CLI

```bash
ccf-paper-scout run --config config.json
ccf-paper-scout doctor --config config.json
ccf-paper-scout test-delivery --config config.json
ccf-paper-scout inspect-zotero --config config.json
ccf-paper-scout list-venues
ccf-paper-scout runs list
ccf-paper-scout runs show <run-id>
```

### doctor 检查

- JSON/schema 和配置版本；
- years、venue、分页和输出参数范围；
- 输出/state 路径可写；
- Zotero ID/key 环境变量是否存在及长度（不显示值）；
- LLM enabled 时 endpoint/model/key 是否齐全；
- SMTP enabled 时 host、port、sender、receiver、授权码环境变量是否齐全；
- DNS/TLS 连接；
- SQLite integrity；
- cron 时区和单实例锁路径。

默认 doctor 不发送邮件；`test-delivery` 才产生 SMTP 副作用。

### 文件

- 创建：`pyproject.toml`
- 创建：`src/ccf_paper_scout/**`
- 创建：`tests/test_cli.py`、`tests/test_config.py`
- 修改：`paper_scout.py`、README、CI

### 迁移步骤

1. 添加 `pyproject.toml` 和最小 entry point。
2. 写 `ccf-paper-scout --help` smoke test。
3. 每次只迁移一个模块及其测试。
4. `paper_scout.py` 调用新 CLI，不立即删除。
5. 配置采用 version 字段并提供旧配置适配。
6. CI 创建临时 venv，执行安装和 CLI smoke test。

### 验收

```bash
python3 -m venv /tmp/ccf-scout-venv
/tmp/ccf-scout-venv/bin/pip install .
/tmp/ccf-scout-venv/bin/ccf-paper-scout --help
/tmp/ccf-scout-venv/bin/ccf-paper-scout doctor --config config.example.json
```

---

## 工作 4：QQ SMTP 真实验收与北京时间 09:00 cron

### 目标

使用 QQ 邮箱 SMTP 授权码完成一次真实测试邮件，再用同一环境启动真实推荐，最后安装北京时间 09:00 cron。

### QQ SMTP 配置

`config.json` 中：

```json
"delivery": {
  "smtp": {
    "enabled": true,
    "host": "smtp.qq.com",
    "port": 465,
    "use_ssl": true,
    "sender": "你的QQ号@qq.com",
    "receiver": "你希望接收推荐的邮箱地址",
    "password_env": "SMTP_PASSWORD",
    "subject": "CCF Paper Scout 每日论文推荐",
    "timeout_seconds": 60
  }
}
```

QQ 邮箱通常使用 `smtp.qq.com:465` + SSL。授权码是 SMTP 登录密码，不是 QQ 密码；`sender` 必须与授权码所属邮箱一致。

### 凭据文件

创建已被 `.gitignore` 排除的：

```text
/home/zlw/ccf-paper-scout/.env.local
```

内容：

```bash
ZOTERO_USER_ID='你的数字 Zotero User ID'
ZOTERO_API_KEY='你的只读 Zotero API key'
LLM_API_KEY='你的 OpenAI-compatible API key'
SMTP_PASSWORD='你的 QQ 邮箱 SMTP 授权码'
```

不要加 `export` 也可以，因为 `scripts/run_daily.sh` 会执行 `set -a` 后 source。文件权限：

```bash
chmod 600 /home/zlw/ccf-paper-scout/.env.local
```

### 真实 SMTP 测试

项目当前 `--test-delivery` 会读取当前 shell 环境，不自动加载 `.env.local`。为保证与 cron 完全一致，应先手动加载：

```bash
cd /home/zlw/ccf-paper-scout
set -a
source .env.local
set +a
python3 paper_scout.py --config config.json --test-delivery
```

预期：

```text
SMTP test delivery accepted for 你的接收地址
```

随后检查收件箱、垃圾箱、发件人和中文主题。该模式不请求 Zotero/DBLP/OpenAlex/LLM，也不更新 `seen.json`。

### 失败检查

- `535 Authentication failed`：授权码错误、SMTP 服务未开启、sender 与授权码不匹配；
- 连接超时：检查 WSL DNS、防火墙和 `smtp.qq.com:465`；
- 收到 SMTP accepted 但收件箱无邮件：检查垃圾箱、QQ 邮箱发信记录和频率限制；
- 不要把授权码打印到终端日志或聊天。

### 真实日推验收

```bash
set -a; source .env.local; set +a
python3 paper_scout.py --config config.json --output recommendations.md --no-update-seen
```

先以 `--no-update-seen` 预览报告；确认内容后去掉该参数执行一次真实投递。只有 SMTP 接受后才更新去重状态。

### cron

确认系统时区：

```bash
date '+%F %T %Z %z'
```

crontab：

```cron
CRON_TZ=Asia/Shanghai
0 9 * * * PROJECT_DIR=$HOME/ccf-paper-scout $HOME/ccf-paper-scout/scripts/run_daily.sh >> $HOME/ccf-paper-scout/paper_scout.log 2>&1
```

### 验收

1. `--test-delivery` 邮件到达；
2. `--no-update-seen` 预览不污染状态；
3. 正式运行收到富内容推荐；
4. SMTP 失败时不更新去重；
5. 第二次运行不重复已投递论文；
6. cron 使用 `.env.local` 且北京时间 09:00 触发；
7. 日志不包含授权码或 API key。

---

## 推荐实施顺序

```text
工作 1：多 collection 公平性
→ 工作 2：SQLite 状态与迁移
→ 工作 3：可安装 CLI/doctor
→ 工作 4：QQ SMTP 真实验收和 cron
```

工作 4 的 SMTP 测试可以提前做，但正式 cron 最好在工作 2/3 完成后安装，以获得运行历史、doctor 和更可靠的故障恢复。

## 总体验收

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 scripts/validate_repo.py
python3 scripts/build_venue_data.py --check
git diff --check
```

以及真实用户侧验收：QQ SMTP 测试邮件、一次预览、一次正式富内容投递和次日 09:00 定时触发。
