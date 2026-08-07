# GitHub Actions 每日部署实施计划

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** 参考 `TideDra/zotero-arxiv-daily` 的 Actions 部署体验，为 CCF Paper Scout 建立可手动验收、每日北京时间 09:00 自动运行、私密持久化状态、失败可诊断且避免并发重复投递的 GitHub Actions 部署。

**Architecture:** 延续参考项目的 `workflow_dispatch + schedule + GitHub Secrets` 模式，但不复制其无状态假设。CCF Paper Scout 的跨日去重、运行历史和 LLM 缓存依赖 SQLite，因此使用独立私有仓库 `ademjest/ccf-paper-scout-state` 保存运行状态；公开代码仓库的 workflow 使用最小权限 fine-grained PAT 读写该私有状态仓库。每日 job 是短任务，不采用 5 小时 40 分钟常驻/自重启模式。

**Tech Stack:** GitHub Actions、Python 3.12、GitHub CLI/API、SQLite WAL、QQ SMTP、Zotero Web API、OpenAI-compatible LLM API、fine-grained PAT、unittest。

---

## 1. 参考项目中采用与不采用的设计

### 采用

- `workflow_dispatch`：网页手动触发测试。
- `schedule`：每天自动运行。
- GitHub repository secrets：保存 Zotero、SMTP、LLM 凭据。
- 独立 Test/Manual workflow：先验证，再启用每日正式任务。
- Keep Alive workflow：避免公开仓库 60 天无活动后 schedule 被 GitHub 禁用。
- workflow 日志作为主要远程诊断入口。

### 不直接采用

- 不使用长时间常驻 runner 或每 5 小时 40 分钟重启。
- 不把业务状态仅保留在 runner 本地；runner 每次都是临时环境。
- 不把 SQLite、Zotero 阅读画像、LLM 缓存提交到公开代码仓库。
- 不把 SMTP sender/receiver 或 LLM endpoint 直接硬编码进公开 workflow。
- 不把 Actions cache 当作唯一状态源：cache 可被回收，也不适合保存需要可靠恢复的隐私状态。
- 不允许正式 daily workflow 与本机 cron 同时发送邮件。

---

## 2. 目标运行流程

```text
北京时间 09:00 schedule / 手动 production dispatch
  → checkout 公开代码仓库
  → checkout 私有状态仓库到 .runtime-state/
  → Python 3.12 + pip install .
  → 从 Secrets/Variables 生成临时 config.action.json
  → doctor（不打印 secret 值）
  → 单实例 concurrency gate
  → ccf-paper-scout run
  → SMTP 成功后 SQLite 标记 delivered
  → 将 SQLite/seen/translation cache 提交回私有状态仓库
  → 上传脱敏报告和诊断 artifact
  → job 正常结束
```

失败语义：

```text
Zotero/DBLP/OpenAlex/LLM/SMTP 失败
  → job 失败
  → 上传脱敏日志
  → 不提交不完整的新状态（SMTP accepted 后的状态推送例外，见风险章节）
```

---

## 3. GitHub 配置清单

### Repository Secrets（公开代码仓库）

必须创建：

```text
ZOTERO_USER_ID
ZOTERO_API_KEY
SMTP_PASSWORD
SMTP_SENDER
SMTP_RECEIVER
LLM_API_KEY
STATE_REPO_TOKEN
```

可选但建议也作为 Secret：

```text
LLM_BASE_URL
LLM_MODEL
```

说明：

- `SMTP_PASSWORD` 填 QQ SMTP 授权码，不是 QQ 登录密码。
- `SMTP_SENDER` 是授权码所属 QQ 邮箱。
- `STATE_REPO_TOKEN` 是 fine-grained PAT，仅授权私有状态仓库 `Contents: Read and write`；不给公开代码仓库写权限，不给 organization/admin 权限。
- 不把本机 `.env.local` 上传到 GitHub。

### Repository Variables

```text
STATE_REPO=ademjest/ccf-paper-scout-state
TIMEZONE=Asia/Shanghai
```

若 LLM endpoint/model 不视为敏感，可放变量：

```text
LLM_BASE_URL
LLM_MODEL
```

### 私有状态仓库

创建：

```text
ademjest/ccf-paper-scout-state   visibility=PRIVATE
```

只保存：

```text
state/paper_scout.sqlite3
state/seen.json                  # 兼容镜像
state/translations.json          # 兼容镜像，可后续移除
README.md
.gitignore
```

排除：

```text
*.lock
*.sqlite3-wal
*.sqlite3-shm
*.bak
*.log
recommendations*.md
```

本地现有 `state/` 作为首次 bootstrap 数据提交到该私有仓库，避免 Actions 第一次运行重新推送历史论文。

---

### Task 1: 创建 Actions 配置生成器

**Objective:** 从公开的模板和 GitHub Secrets 生成仅存在于 runner 的 `config.action.json`，避免公开邮箱地址和 API 配置。

**Files:**
- Create: `scripts/build_actions_config.py`
- Create: `tests/test_actions_config.py`
- Modify: `.gitignore`

**Step 1: 写失败测试**

测试：

- 从 `config.example.json` 生成配置；
- 强制 `delivery.smtp.enabled=true`；
- 写入 `smtp.qq.com:465/use_ssl=true`；
- sender、receiver、LLM endpoint/model 从环境变量读取；
- 输出路径全部指向 `.runtime-state/state/`；
- 缺任一必需值时失败；
- stdout 和异常不包含 secret 值；
- 生成文件权限为 `600`。

Run:

```bash
python3 -m unittest tests.test_actions_config -v
```

Expected: FAIL，因为脚本尚不存在。

**Step 2: 最小实现**

CLI：

```bash
python3 scripts/build_actions_config.py \
  --base config.example.json \
  --output config.action.json \
  --state-dir .runtime-state/state
```

环境变量：

```text
SMTP_SENDER
SMTP_RECEIVER
LLM_BASE_URL
LLM_MODEL
```

API key 与 SMTP password 仍只通过现有运行时环境变量读取，不写入 JSON。

**Step 3: 验证**

```bash
python3 -m unittest tests.test_actions_config -v
python3 -m json.tool config.action.json >/dev/null
stat -c '%a' config.action.json
```

Expected: PASS，权限 `600`。

**Step 4: Commit**

```bash
git add scripts/build_actions_config.py tests/test_actions_config.py .gitignore
git commit -m "feat(actions): generate ephemeral deployment config"
```

---

### Task 2: 增加状态仓库恢复与提交脚本

**Objective:** 安全地从私有状态仓库恢复状态，并在成功运行后 checkpoint SQLite、提交和推送。

**Files:**
- Create: `scripts/actions_state.py`
- Create: `tests/test_actions_state.py`

**Step 1: 写失败测试**

覆盖：

- bootstrap 时创建预期目录；
- 清理 `*.lock`、`*.sqlite3-wal`、`*.sqlite3-shm`；
- SQLite 执行 `PRAGMA wal_checkpoint(TRUNCATE)` 后再提交；
- 只允许 state allowlist 文件；
- 无变化时返回成功且不创建空提交；
- remote push 冲突时 fail closed，不做 force push；
- 输出不含 PAT。

Run:

```bash
python3 -m unittest tests.test_actions_state -v
```

Expected: FAIL。

**Step 2: 最小实现**

子命令：

```bash
python3 scripts/actions_state.py prepare --dir .runtime-state/state
python3 scripts/actions_state.py checkpoint --dir .runtime-state/state
python3 scripts/actions_state.py verify --dir .runtime-state/state
```

Git clone/push 仍由 workflow 的标准 `git` 命令执行；脚本只处理 SQLite 和 allowlist，避免在 Python 中持有 PAT。

**Step 3: 验证**

```bash
python3 -m unittest tests.test_actions_state -v
```

Expected: PASS。

**Step 4: Commit**

```bash
git add scripts/actions_state.py tests/test_actions_state.py
git commit -m "feat(actions): validate and checkpoint durable state"
```

---

### Task 3: 新增手动部署验收 workflow

**Objective:** 提供类似参考项目 Test workflow 的手动入口，但区分 doctor、SMTP 测试、preview 和 production，避免误发和误更新状态。

**Files:**
- Create: `.github/workflows/paper-scout-manual.yml`
- Create: `tests/test_workflows.py`

**workflow_dispatch inputs:**

```text
mode: doctor | smtp-test | preview | production
max_results: 默认 5，仅用于 preview/production
```

**权限：**

```yaml
permissions:
  contents: read
```

**并发：**

```yaml
concurrency:
  group: paper-scout-production
  cancel-in-progress: false
```

`doctor` 和 `smtp-test` 不需要恢复/提交状态；`preview` 恢复状态但加 `--no-update-seen`，不提交状态；只有 `production` 可以提交状态。

**Step 1: 写失败测试**

静态验证 YAML：

- 仅有 `workflow_dispatch`；
- 默认 mode 不是 production；
- permissions 是 read-only；
- secrets 只进入 `env`，不拼入 shell command；
- production 才执行状态 push；
- preview 包含 `--no-update-seen`；
- workflow 不执行 `env`/`printenv`/`cat config.action.json`；
- timeout 不超过 60 分钟。

Run:

```bash
python3 -m unittest tests.test_workflows.WorkflowTests.test_manual_workflow -v
```

Expected: FAIL。

**Step 2: 实现 workflow**

主要步骤：

```text
checkout code
setup-python 3.12
pip install .
clone private state repo（preview/production）
build_actions_config.py
doctor
branch by mode
checkpoint + commit + push（production success only）
upload redacted recommendations/log artifact
```

状态仓库 checkout 使用：

```bash
git clone "https://x-access-token:${STATE_REPO_TOKEN}@github.com/${STATE_REPO}.git" .runtime-state
```

该命令必须启用 GitHub masking，且不能 `set -x`。

**Step 3: Commit**

```bash
git add .github/workflows/paper-scout-manual.yml tests/test_workflows.py
git commit -m "feat(actions): add manual deployment verification workflow"
```

---

### Task 4: 新增每日北京时间 09:00 workflow

**Objective:** 每日执行一次短任务，不使用 5h40m 常驻 runner。

**Files:**
- Create: `.github/workflows/paper-scout-daily.yml`
- Modify: `tests/test_workflows.py`

**触发：**

```yaml
on:
  workflow_dispatch:
  schedule:
    - cron: "0 9 * * *"
      timezone: "Asia/Shanghai"
```

如果 GitHub 对 timezone 语法校验不兼容，回退为 UTC：

```yaml
- cron: "0 1 * * *"
```

**安全与可靠性：**

```yaml
permissions:
  contents: read
concurrency:
  group: paper-scout-production
  cancel-in-progress: false
timeout-minutes: 60
```

正式运行：

```bash
ccf-paper-scout run \
  --config config.action.json \
  --output recommendations.md
```

只有程序退出码为 0 才 checkpoint/push 状态。无候选也应正常结束并提交必要的运行历史；SMTP 失败必须 job failed 且不伪造成功。

**Step 1: 写失败测试**

验证：

- schedule 是北京时间 09:00；
- 有手动触发；
- 与 manual production 使用同一 concurrency group；
- `cancel-in-progress=false`；
- timeout 60 分钟；
- 默认分支运行；
- job 成功才 push 状态；
- artifact 用 `if: always()`，但不上传 SQLite；
- 不存在 5h40m sleep/自触发链。

**Step 2: 实现并验证**

```bash
python3 -m unittest tests.test_workflows -v
python3 scripts/validate_repo.py
```

**Step 3: Commit**

```bash
git add .github/workflows/paper-scout-daily.yml tests/test_workflows.py
git commit -m "feat(actions): schedule daily Beijing paper delivery"
```

---

### Task 5: 增加 Keep Alive workflow

**Objective:** 参考上游项目，避免公开仓库 60 天无活动后 scheduled workflow 被 GitHub 自动禁用。

**Files:**
- Create: `.github/workflows/keep-alive.yml`
- Modify: `tests/test_workflows.py`

**设计：**

```yaml
on:
  workflow_dispatch:
  schedule:
    - cron: "17 3 1 * *"
permissions:
  contents: write
```

每月 1 日 UTC 03:17 更新：

```text
.github/keep-alive.txt
```

选择非整点分钟，降低 GitHub schedule 高峰延迟。提交信息：

```text
chore: keep scheduled workflows active [skip ci]
```

风险：该 workflow 拥有 contents write。只运行固定 shell，不 checkout PR 代码、不读取任何业务 secret。

**测试：**

- workflow 不引用 Zotero/SMTP/LLM/STATE secrets；
- 只有该 workflow 拥有 contents write；
- `[skip ci]` 防止无意义 CI；
- 不能 force push。

**Commit:**

```bash
git add .github/workflows/keep-alive.yml tests/test_workflows.py
git commit -m "ci: keep scheduled workflows active"
```

---

### Task 6: Bootstrap 私有状态仓库

**Objective:** 将当前本地已投递状态安全迁移到私有仓库，避免首次云端运行重复推荐。

**External actions:**

```bash
gh repo create ademjest/ccf-paper-scout-state --private \
  --description "Private runtime state for CCF Paper Scout"
```

本地初始化临时工作树，复制 allowlist 状态，checkpoint SQLite，再提交。绝不复制：

```text
.env.local
config.json
recommendations.md
paper_scout.log
zotero_library_debug.md
```

验收：

```bash
gh repo view ademjest/ccf-paper-scout-state --json visibility --jq .visibility
```

Expected:

```text
PRIVATE
```

再检查私有仓库 tree 只包含允许文件。

注意：创建 private repo 和上传状态属于外部副作用，执行阶段需要明确确认目标仓库名后再做。

---

### Task 7: 配置 GitHub Secrets/Variables

**Objective:** 将本机已有凭据安全写入 GitHub Actions secrets，绝不输出值。

**Actions:**

从 `.env.local` 读取并通过 stdin 设置：

```bash
gh secret set ZOTERO_USER_ID
...其余 secret...
```

不得在 shell history 或命令参数中出现值，优先：

```bash
printf '%s' "$VALUE" | gh secret set NAME
```

需要用户补充/确认的值：

```text
SMTP_SENDER
SMTP_RECEIVER
LLM_BASE_URL
LLM_MODEL
STATE_REPO_TOKEN
```

配置完成只验证名称：

```bash
gh secret list
gh variable list
```

不读取 secret 值。

---

### Task 8: 手动四阶段验收

**Objective:** 在启用 daily 前证明每个模式的副作用边界正确。

按顺序触发：

1. `doctor`：不得发邮件、不得写状态。
2. `smtp-test`：只发测试邮件、不得抓论文、不得写状态。
3. `preview`：抓取和生成报告，不发正式邮件，不写 delivered。
4. `production`：发送最多 5 篇，成功后提交私有状态。

命令：

```bash
gh workflow run paper-scout-manual.yml -f mode=doctor
gh run watch <run-id>
```

每一步检查：

- 日志没有 secret；
- receiver 实际收到预期邮件；
- state repo 的 commit 只在 production 后变化；
- production 重跑不会重复发送同一批 delivered ID；
- artifact 不包含 SQLite、Zotero debug 或 `.env`。

---

### Task 9: 停用本机 cron，启用唯一生产调度器

**Objective:** 避免本机 cron 与 GitHub Actions 在 09:00 同时发送两封邮件。

在 GitHub production 手动验收通过后：

```bash
crontab -l
```

移除：

```cron
0 9 * * * ...ccf-paper-scout/scripts/run_daily.sh...
```

保留一份本地 cron 备份到用户私有目录，不提交仓库。再次检查：

```bash
crontab -l
```

确保生产调度器只有 GitHub Actions。

---

### Task 10: CI、独立审查、PR 与合并

**Objective:** 完成安全审查并将部署能力合并到 main。

完整验证：

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 -m py_compile paper_scout.py state_store.py src/ccf_paper_scout/*.py scripts/*.py tests/*.py
python3 scripts/validate_repo.py
python3 scripts/build_venue_data.py --check
git diff --check
```

独立 fail-closed 审查重点：

- workflow 无 secret 泄漏；
- fork PR 不会执行带 secrets 的 production job；
- 私有状态仓库 token 最小权限；
- concurrency 防止重复；
- preview 不改状态；
- SMTP accepted 后状态 push 失败的 at-least-once 风险已明确记录；
- keep-alive workflow 不接触业务 secrets；
- state bootstrap 不含 `.env.local` 或公开隐私数据。

PR CI 通过后 squash merge。合并到 `main` 后才会使 scheduled workflow 生效。

---

## 4. 状态一致性与邮件语义

QQ SMTP 不支持业务幂等键，因此远程状态存在不可消除的小窗口：

```text
SMTP 已接受邮件
→ runner 在状态 push 前崩溃
→ 下一次可能重复发送
```

当前选择 `at-least-once`：宁可极少重复，不永久漏发。缓解方式：

- SMTP accepted 后立即 checkpoint/commit/push；
- 不在其间执行 artifact、日志整理等非关键步骤；
- concurrency 单实例；
- push 失败明确使 workflow 红灯；
- 私有状态仓库保留完整 Git 历史，便于恢复；
- 邮件主题未来可包含 run date/run ID，方便识别重复。

不选择“先保存 delivered 再发邮件”，因为 SMTP 失败会导致论文被永久标记已投递。

---

## 5. 部署后的运维

### 日常观察

```bash
gh run list --workflow paper-scout-daily.yml --limit 10
gh run view <run-id> --log-failed
```

### 手动重跑

只在确认上次没有 SMTP accepted 时使用：

```bash
gh run rerun <run-id> --failed
```

若日志显示 SMTP accepted 但 state push 失败，应先检查/修复私有状态仓库，再决定是否重跑，避免重复邮件。

### 状态恢复

```text
私有状态仓库 git history
→ checkout 最近一次健康 commit
→ sqlite PRAGMA integrity_check
→ 更新 main
```

### 60 天 inactivity

Keep Alive 每月产生一个 `[skip ci]` bot commit。若不希望有 bot commit，则删除 keep-alive workflow，并接受需要每 60 天人工重新启用 schedule。

---

## 6. 验收标准

部署完成必须同时满足：

- [ ] 代码仓库保持 PUBLIC，状态仓库为 PRIVATE。
- [ ] GitHub secret 名称齐全，日志无 secret 值。
- [ ] 手动 doctor、smtp-test、preview、production 均通过。
- [ ] production 后私有状态仓库出现新 commit。
- [ ] 第二次 production 不重复发送已 delivered 论文。
- [ ] 每日 workflow 为北京时间 09:00。
- [ ] timeout 不超过 60 分钟，不存在 5h40m 自重启。
- [ ] concurrency 阻止两个 production 同时运行。
- [ ] Python 3.11/3.12/3.13 CI 通过。
- [ ] 独立 fail-closed 审查通过。
- [ ] 本机 09:00 cron 已停用，生产调度器唯一。

---

## 7. 推荐执行顺序

```text
代码侧 Tasks 1-5
→ PR（先不合并 daily）
→ 创建 private state repo
→ bootstrap 本地状态
→ 配置 Secrets/Variables
→ 合并 workflow 到 main
→ manual doctor
→ manual smtp-test
→ manual preview
→ manual production(max_results=5)
→ 检查状态与收件
→ 停用本机 cron
→ daily schedule 正式接管
```
