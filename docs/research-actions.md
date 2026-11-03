# GitHub Actions 研究画像实验

`Paper Scout Manual` 新增 `research` 模式，运行 `scripts/research_experiment.py`。画像位于 `profiles/rl-action-world-models.json`，包括行动／规划策略、世界模型、LLM 辅助 RL、VLM 行动策略和自博弈。模仿学习可作为主要兴趣，普通 MARL 不再作为独立主要语义方向。

## 从 update-paper 分支运行

此分支尚未合并到默认分支时，Actions 网页可能仍展示默认分支的旧输入选项。可以用 GitHub CLI 明确指定已存在的工作流文件、目标分支和模式：

```bash
gh workflow run paper-scout-manual.yml --repo ademjest/ccf-paper-scout --ref update-paper --field mode=research
gh run list --repo ademjest/ccf-paper-scout --workflow paper-scout-manual.yml --branch update-paper --limit 5
```

GitHub CLI 需先通过 `gh auth login` 登录具备仓库 Actions 运行权限的账号。网页若已显示新选项，也可以进入 **Actions → Paper Scout Manual → Run workflow**，选择 `update-paper` 分支和 `research` 模式。

实验结束后，在该次运行页面的 **Artifacts** 下载 `research-experiment-<run-id>`，解压并打开 `results.html`。其中有原画像、新画像各自的 lexical、semantic、RRF Top 20，标题可展开摘要。JSON 同时保留候选、核验结果、请求失败原因、冻结快照、完整排序和回放验证。

此模式只访问公开论文接口和固定版本公共模型，不需要仓库中的 Zotero、SMTP、LLM API 或 STATE_REPO Secrets；不会执行邮件或私有状态步骤。Artifacts 保存 14 天，只包含本次公开检索产物和示例画像；没有上传本地人工标注、数据库、私有文库或模型缓存。

## 实验范围

- AAAI、ACL、ICML、NeurIPS 的 2025 年论文；保留现有主会核验规则，ICLR 暂未纳入。
- 世界模型、自博弈、行动策略三个检索组，每组四个会议，共 12 次 DBLP 标题查询，每次最多 50 条；候选去重。
- 最多 80 次官方摘要请求，核对标题和作者；不将 OpenAlex 书目信息当作摘要。缺少支持的官方链接、请求失败或预算不足会单独记录。
- 同一批通过核验、具有摘要的论文比较新旧画像；两种画像均为 shadow。模型固定为 BGE-small-en-v1.5，不调用生成式 LLM。
- 云端每次重新采集公开候选，不携带本地历史候选池。因此数量和名次可能与本地 176 篇实验不同，不能直接混比。来源不完整则保留诊断产物并使工作流失败。
- 无人工标签，不输出准确率或 nDCG。关键词和向量相似仍可能混淆“环境行动策略”与“语言模型回答优化”，尚未实现下一阶段的任务角色重排。

每日定时工作流仍按原配置执行。GitHub 的 schedule 使用默认分支，推送到 `update-paper` 不会自动让默认分支定时任务使用该分支的新代码；需要后续明确合并和配置生产部署。此次提供的研究画像也没有批准生产阈值。

## Windows 本地运行

先安装语义依赖并准备固定版本模型。以下命令在仓库根目录的 PowerShell 执行：

```powershell
$env:PYTHONUTF8 = '1'
$env:HF_HOME = Join-Path (Get-Location) '.model-cache'
.\.venv\Scripts\python.exe -c 'from ccf_paper_scout.semantic import Encoder; Encoder({})'
.\.venv\Scripts\python.exe scripts\research_experiment.py --output local-notes\research-new-run --abstract-budget 80
```

输出目录必须尚不存在，以免覆盖已完成实验。首次预热模型需要网络；之后的排序阶段开启模型离线模式。
