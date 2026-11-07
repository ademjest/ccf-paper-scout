# Daily 运维与恢复指南

首次部署请先阅读 [无 Zotero 部署图文指南](deploy-without-zotero.md)。本文用于已有部署的更新、来源配置与故障排查。更新代码不需要重置私有状态仓库；保留 SQLite、`seen.json` 和未处理的 `.delivery-pending`。

## 更新代码与验证

1. 在 **Actions → Paper Scout Daily → ⋯ → Disable workflow** 暂停定时任务，并等待正在运行的任务结束；暂停不会取消已有运行。
2. 记下代码仓库与私有状态仓库当前的提交 SHA。状态备份保存在私有仓库或本地私有目录。
3. 在自己的 Fork 同步上游更新，处理自定义兴趣文件的冲突；自己开发的修改通过 Pull Request 合并，等待 CI 通过。定时任务使用默认分支，不会自动使用功能分支代码。
4. 从 **Actions → Paper Scout Manual → Run workflow** 发起新运行，选择更新后的分支、`mode=preview`、`max_results=5`，其余选项保持 `configured`，沿用自己的 Variables。旧运行的 **Re-run jobs** 使用旧提交。
5. 检查私有状态仓库 `reports/<run-id>/recommendations.md` 和 `recommendations.diagnostics.json`：研究方向是否正确、来源和摘要覆盖是否正常、最终论文是否合格。Preview 不发邮件、不写回生产候选及投递状态，但会提交私有报告；启用 LLM 时可能产生 API 费用。
6. 需要验证实际投递时，在默认分支运行一次 `mode=production`、`max_results=3`。确认邮件、状态保存和投递事务均成功，再启用 Daily。

`max_results` 只限制最终推荐数量，不限制抓取请求数。首次扫描或向量编码可能耗时较长，结合来源进度日志判断。预览产生的候选缓存与扫描游标不会写回生产状态；需要积累生产发现库时使用 `discover`，它保存抓取进度但不发邮件。

如果投递留下 `.delivery-pending`，先调查日志和实际收件情况，再按 [投递和去重安全](../README.md#投递和去重安全) 处理，不能直接删除标记或反复重跑。

## 兴趣、排序与每日数量

无 Zotero 用户使用 `INTEREST_SOURCE=manual`、`RESEARCH_PROFILE=custom` 并修改 `profiles/custom.json`。已有 Zotero 用户可保留自己的文库配置。手动模式的 `configured` 沿用仓库 Variables，显式选择只覆盖本次运行。

首次部署建议 `SEMANTIC_MODE=off`、`RELATION_MODE=off`；需要比较语义排序时将前者改为 `shadow`。shadow 不接管实际推荐。Daily 的正式语义排序须完成 [独立评测和校准](offline-benchmark.md#daily-语义排序的正式校准)，网页混合排序不代表 Daily 已经校准。关系策略面向 LLM/RL 方向，其他研究方向保持关闭。

每日上限由 [Daily 工作流](../.github/workflows/paper-scout-daily.yml) 的配置生成参数 `--max-results 5` 控制；修改时同时更新后面的数量校验。没有合格论文不会凑数。Manual 的 `max_results` 仅影响本次手动运行。

当前 Daily 在北京时间约 04:00 启动准备，不早于 09:00 投递；GitHub 调度可能延迟。手动运行 Daily 或 Manual production 会真正投递，不受定时等待限制。

## 来源配置与诊断

当前示例通过 `sources.enabled` 启用 DBLP、PMLR、OpenAlex，`sources.mode=production`。独立来源的预算位于 `sources.providers.<name>`；修改并提交 `config.example.json` 会影响后续 Actions 生成的配置。增加来源不会自动增加每日篇数。

- `max_requests`：该来源每轮请求上限，包括重试。
- `deadline_seconds`：该来源的时间预算。
- `sources.max_parallel_sources`、`sources.collection_deadline_seconds`：来源并发上限和采集总时限，不是整个推荐流程的时限。
- 新采集流程的 DBLP 预算使用 `sources.providers.dblp`；旧 `dblp.max_pages_per_run` 等用于 legacy 路径，不与新预算叠加。

在本地仓库根目录检查来源配置；加 `--network` 才会发出小预算探测请求：

```powershell
.\.venv\Scripts\ccf-paper-scout.exe doctor --config config.example.json --sources
.\.venv\Scripts\ccf-paper-scout.exe doctor --config config.example.json --sources --network
```

Actions 中按来源需要添加凭据：`OPENALEX_API_KEY`、`SEMANTIC_SCHOLAR_API_KEY` 使用 Secrets，`CROSSREF_MAILTO` 使用 Variable。未启用的来源不必配置；Crossref 主要用于精确元数据补全，不建议作为通用发现入口。遇到反机器人挑战，不通过添加账号密码或浏览器 cookie 绕过。

来源超时、限流或预算耗尽时，先查看报告中的来源诊断。来源之间独立执行，已获取的候选与缓存仍需经过资格、相关性和投递去重检查；故障时有结果不代表已完整检索，更不代表都是当天新论文。无合格候选时不发邮件。不要通过清空状态库解决网络问题。

网页搜索使用独立数据库；来源补充失败时仍可展示已有本地结果。旧历史搜索保持当时快照，修复网络或更新解析器后需要重新搜索，才会看到新结果与诊断。

### 旧 DBLP 发现流程的缓存降级

将 `sources.enabled` 设为 `[]` 会回到旧发现路径。此时 `dblp.allow_cached_on_transient_failure` 允许 DBLP 超时、连接故障、429 或临时服务错误导致扫描不足时，使用本次扫描开始前的候选快照；邮件及诊断会说明来源异常。新抓到的页保存供后续使用，但不混入当次缓存推荐。

资格不明的论文继续暂缓，摘要和相关性要求不放宽。`failure_policy=strict`、纯 `discover` 模式、反机器人页面、响应结构错误和程序异常不适用此降级；关闭 `allow_cached_on_transient_failure` 可恢复扫描不足即停止的行为。

## 私有状态仓库恢复失败

如果 `Restore private state` 的克隆步骤失败，并出现 `could not read Username for 'https://github.com'`，说明 Git 无法完成认证。先检查这一步，而不是最后的状态保存步骤。旧工作流在恢复失败后仍执行 `always()` 保存，会额外报 `.runtime-state: No such file or directory`；修复后只有恢复和核验全部成功才允许保存。恢复成功而后续推荐/投递失败时仍尝试保存已有状态。

按以下步骤处理认证问题：

1. 在代码仓库 **Settings → Secrets and variables → Actions → Variables** 确认 `STATE_REPO` 是现有私有状态仓库的 `owner/repository`，不带 URL 前缀或 `.git` 后缀。
2. 在个人 **Settings → Developer settings → Personal access tokens → Fine-grained tokens** 检查令牌到期时间、资源所有者和仓库范围。需要时创建新令牌：只选择原私有状态仓库，Repository permissions 的 **Contents** 设为 **Read and write**。组织仓库还需满足组织审批/授权要求。
3. 回到代码仓库的 **Actions → Secrets**，编辑 Repository secret `STATE_REPO_TOKEN`，替换为新令牌。不要把令牌贴到日志、Issue 或聊天中。Secret 名字存在不等于值仍然有效。
4. 更新令牌或合并修复后，从 **Paper Scout Manual → Run workflow** 选择 `main`、`mode=preview`，验证恢复、推荐和私有报告保存，再恢复正常 Daily。单独 `doctor` 模式不会克隆状态仓库，不能验证这个问题。旧运行的 **Re-run jobs** 不会使用工作流的新提交。

这个错误不需要创建空状态目录、删除 `.delivery-pending`、重建私有仓库或重置历史。恢复不了既有历史时应停止投递，避免把已投递论文当成新论文。

## 历史保留与回退

更新或变更研究方向不需要删除候选、摘要、向量或投递历史。扫描配置变化可能触发重新索引；这是补齐候选，不是允许重发已投递论文。

模型、画像或兴趣参考集合变化可能使既有语义校准失效；重新评估前使用 off 或 shadow，不修改批准字段绕过匹配校验。

回退前也先暂停 Daily。数据库结构可能已变化，需核对旧代码兼容性；恢复旧备份前必须保留之后新增的投递 ID、阅读反馈和标注，避免重复邮件或丢失判断。不要用旧数据库直接覆盖当前状态。
