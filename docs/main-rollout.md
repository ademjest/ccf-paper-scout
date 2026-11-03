# 将 update-paper 升级为每日运行版本

本次提供两个接入点：手动运行可选 `research_profile`，每日运行读取 Repository Variable `RESEARCH_PROFILE`；正式摘要补全优先尝试支持的 PMLR、NeurIPS、ACL 官方页面，并回退到精确 DOI 的 OpenAlex。两条路径共享请求预算，既有书目占位摘要会失效并尝试重新获取。AAAI 等其他官方来源尚未实现直接解析。

下面步骤保留现有私有仓库、Secrets、已推送记录和 Git 历史。不要删除 SQLite、`seen.json` 或 `.delivery-pending` 来完成升级。

## 1. 保存升级起点

在代码仓库和私有状态仓库分别打开最新提交，记下两个完整 commit SHA。私有状态仓库名可在代码仓库 **Settings → Secrets and variables → Actions → Variables → STATE_REPO** 查看。

状态备份应留在私有仓库或本地私有目录，不能上传到公开代码仓库。正式切换前确认没有正在运行的 Daily 或 production，避免记录的状态落后于刚完成的投递。

## 2. 在 update-paper 上做真实预览

进入代码仓库 **Actions → Paper Scout Manual → Run workflow**，选择：

| 输入 | 值 |
|---|---|
| Branch | `update-paper` |
| mode | `preview` |
| research_profile | `rl-action-world-models` |
| semantic_mode | `shadow` |
| relation_mode | `shadow` |
| max_results | `10` |

如果网页仍显示 main 的旧输入，使用已登录的 GitHub CLI：

```bash
gh workflow run paper-scout-manual.yml --repo ademjest/ccf-paper-scout --ref update-paper --field mode=preview --field research_profile=rl-action-world-models --field semantic_mode=shadow --field relation_mode=shadow --field max_results=10
```

此预览读取实际 Zotero 与私有状态，可能调用现有 LLM API 进行角色复核及中文卡片生成，产生正常 API 费用。不会发邮件；生产 SQLite、扫描游标与已投递记录使用隔离副本，不写回；结果报告会作为新提交保存到私有状态仓库的 `reports/<run-id>/`。因此“生产状态不变”不等于“私有仓库没有新 commit”。这与不读私有数据的 `research` 模式不同。

检查该目录的 `recommendations.md` 和 `recommendations.diagnostics.json`：是否符合新方向、是否有重复投递、`metadata.enriched/invalidated/failed`、主会核验与摘要缺失原因、实际 `selected_ids`。数量少于 10 不自动代表失败；没有合格论文时不会凑数。

shadow 下实际邮件仍由词项排序及质量门槛决定，语义只提供诊断。不要为了让语义立即接管而设置 production 或伪造校准批准。

## 3. 设置每日使用的变量

先在 **Actions → Paper Scout Daily → ⋯ → Disable workflow** 暂停该工作流，并确认已有运行已结束；暂停不会取消正在运行的任务。然后在 **Settings → Secrets and variables → Actions → Variables** 设置 Repository Variables：

| Name | Value |
|---|---|
| `RESEARCH_PROFILE` | `rl-action-world-models` |
| `SEMANTIC_MODE` | `shadow` |
| `RELATION_MODE` | `shadow` |

保留现有 `STATE_REPO` 和所有正常工作的 Secrets。不设置 `RESEARCH_PROFILE` 时，Daily 使用 `legacy` 画像；手动工作流使用本次选择的输入。

## 4. 正常合并代码

打开 `https://github.com/ademjest/ccf-paper-scout/compare/main...update-paper`，确认 **base: main / compare: update-paper**，创建 Pull Request。等待当前最新提交的 CI 全绿，再正常合并；保留 main 的 keep-alive 等已有提交，不强制 push 覆盖 main，也不改私有状态仓库的分支历史。

合并完成后，确认默认分支仍为 main。既有 schedule 会使用 main 的代码，不必另建定时工作流。

## 5. 验证一次实际投递，再恢复定时任务

在 **Paper Scout Manual** 选择 `main` 分支，参数与预览相同，但改为 `mode=production`、`max_results=3`。这一步会真正发送邮件，并记录已投递状态；手动生产任务不受每日定时等待限制。只启动一次，等待运行结束。

检查邮箱、工作流的投递步骤与私有状态提交：状态 push 成功，事务正常结束，没有残留的未处理 `.delivery-pending`。若出现 pending，先调查日志和实际收件情况，不能直接删除标记或重跑来“试一下”。

验证通过后，在 **Paper Scout Daily** 页面选择 **Enable workflow**，由下一次 schedule 继续。现有调度约北京时间 04:00 启动准备，程序不早于 09:00 投递；GitHub 调度可能延迟，晚启动任务按现有逻辑尽快投递。

## 历史保留与回退

升级会增加候选、摘要、向量等表，并扩展扫描状态，不会清空历史投递。首次可能因扫描签名变化而重新索引；这是补齐候选，不是重发历史论文。

新画像可能使旧语义校准签名失效；当前先使用 shadow，不修改 approved 来绕过校验。关系规则尚未适配所有 VLM 行动策略，应继续保持 shadow。

回退也先停 Daily。不要只切回旧代码并直接使用已升级数据库：旧代码对扩展表结构可能不兼容。更不要把升级期间新增的已投递记录丢掉；若需恢复旧状态备份，必须同时合并升级后已投递 ID，避免重复邮件。
