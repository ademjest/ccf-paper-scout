# update-paper 实施与部署说明

日期：2026-10-20。基线：GitHub `main` 的 `99498fb7ddc7d0214a92f8db9dea73f9a76d0c7e`。

## 1. 同步与分支

已核对 GitHub 最新主分支，并从该提交创建 `update-paper`。原本的本地 `main` 与远程主分支没有共同历史，包含 12 个独有提交，已保留为本地 `archive/local-main-20261020`。当前本地 `main` 与 GitHub 主分支一致。

原先未跟踪的优化方案文档已保留并随此分支提交。此分支不自动合并 main；GitHub 定时任务仍然只使用默认分支。

## 2. 本次落地的能力

| 问题 | 新行为 |
| --- | --- |
| 每天重复第一页 | 每个会议年份保存游标；预算耗尽后跨日续扫 |
| 30 篇未投递论文阻止继续采集 | 发现流程不再使用该停止条件，与投递历史分离 |
| 深页新增记录漏检 | 页边界重叠、预算允许时头页复查、完成后开启下一轮扫描 |
| 不会自动加入新年份 | 默认按北京时间滚动当前年与上一年，低频探测下一年 |
| 不相关论文补足每日数量 | 禁止 other 补位；最多 10 篇，允许少发或零发送 |
| 原分数受候选长度影响 | 对候选词项向量归一化；词项分数和语义分数分别保存 |
| 重复补摘要、请求量不可控 | 持久摘要、优先 DOI 字段、请求预算、缺失/错误退避 |
| 语义相似度缺失 | 可选固定 BGE 小模型，CPU 分块编码，SQLite float32 BLOB 缓存 |
| 多研究方向混为一个画像 | 分方向描述向量；近期 Zotero 参考论文作为较弱辅助信号 |
| 缺少最终质量复核 | 可选最多 30 篇 LLM 复核，相关判断必须附摘要原文证据 |
| 预览可能修改运行数据库 | preview/evaluate 使用 SQLite backup 生成的隔离副本 |
| 无法解释未入选原因 | 生成私有 diagnostics JSON，保存分数、方向和排除原因 |

原有 CCF-A 白名单、DBLP ID 前缀检查、Zotero 排重、成功投递历史和邮件事务保护继续使用。新库中的论文在推荐前会重新核对当前白名单。

## 3. 默认行为与需要校准的部分

默认生产使用经过本次逻辑修正的词项匹配与方向硬过滤，`semantic.enabled=false`，`relevance_review.enabled=false`。默认缺摘要论文延期，不凭标题或 LLM 编写内容凑数。初始 `selection.min_lexical_score=0.05` 是可调整的保守起点，不是已验证的个人相关性阈值。

Embedding 有三个部署选择：

- `off`：不下载模型，使用质量门槛与持久发现。
- `shadow`：计算真实 embedding、缓存及诊断分数，实际投递继续使用词项策略。
- `production`：使用匹配当前模型和画像的已批准校准门槛；缺少校准时拒绝正式语义选文。预览仍可生成待校准分数。

用户没有提供个人正负标注，因此本次不声称已达到 80% 相关率，也不自动制造或批准语义阈值。推荐先部署 off，再使用 shadow 收集评估数据。

与原始方案的细节取舍：第一版只支持 BGE 小模型，不同时维护 E5；画像向量复用 `paper_embeddings` 的独立 cache key，不新增单独 profile 表；discover 模式仅持久化来源发现，摘要补全在推荐运行中进行；不引入额外向量检索服务。新发现时延受请求预算与上游变化影响，目前报告 overdue，不承诺固定 7 天内覆盖所有会议。

## 4. Actions 上的测试与启用

### 4.1 先测试 update-paper 分支

进入 `Paper Scout Manual`，在 `Run workflow` 的分支下拉框选择 `update-paper`，不要重跑旧 SHA 对应的任务。

建议顺序：

1. `mode=doctor`、`semantic_mode=off`：检查已有配置和凭据。
2. `mode=discover`：只更新私有状态的发现库，不获取 Zotero、不发送邮件；大会议需多次运行完成初始化。
3. `mode=preview`、`semantic_mode=off`：验证实际选文和摘要补全，生产状态不更新。
4. `mode=preview` 或 `evaluate`、`semantic_mode=shadow`：验证实际 CPU 模型和诊断。evaluate 使用已保存候选快照，不调用 DBLP 或 OpenAlex；兴趣仍来自当前 Zotero，也可在本地指定兴趣快照。
5. 审查私有报告；确认后才运行 production 或合并默认分支。

preview 的发现和元数据更新发生在临时副本，不会持久化，因此首次 preview 可能再次做同样的补全工作。需要持久缓存时使用正式推荐运行；discover 只负责持久发现。

私人预览/评估报告写入私有状态仓库的 `reports/<GITHUB_RUN_ID>/`，不上传公开仓库 artifact。报告包括 Markdown 和 `.diagnostics.json`。预览不会推进生产扫描位置或 delivered；保存报告本身会产生私有仓库提交。

新增可选 Secret：`OPENALEX_API_KEY`，按提供方实际认证要求设置。原有 STATE_REPO、STATE_REPO_TOKEN、Zotero、SMTP 和 LLM 配置继续使用。现有配置构建脚本仍要求 SMTP 与 LLM 基本字段，discover 虽不调用这些服务，也沿用同一配置构建过程。

### 4.2 默认分支每日任务

合并分支之后，仓库变量 `SEMANTIC_MODE` 决定 daily 的模型模式：不设置或 `off` 为基础模式，`shadow` 为影子评估，`production` 为已校准语义模式。

语义模式仅安装 CPU 版 torch 和约束文件中的依赖。模型权重使用公共 Actions cache；个人画像、向量与候选正文只存在私有 SQLite，不进入公共缓存。

工作流会输出 checkout SHA 与安装模块路径，用来确认运行的确实是目标版本。原定投递时间等待逻辑继续保留；不要把等待时间算成 embedding 推理耗时。

## 5. 本地使用

基础安装与回归测试：

```bash
python -m pip install .
python -m unittest discover -s tests -v
python -m compileall -q paper_scout.py state_store.py src scripts
```

CPU 语义安装，建议 Python 3.12：

```bash
python -m pip install torch==2.6.0 --index-url https://download.pytorch.org/whl/cpu
python -m pip install '.[semantic]' -c constraints-semantic.txt
python scripts/semantic_smoke.py
```

独立发现与隔离评估：

```bash
ccf-paper-scout discover --config config.json --output recommendations-discovery.json
ccf-paper-scout run --config config.json --output recommendations-preview.md --no-update-seen
ccf-paper-scout evaluate --config config.json --output recommendations-evaluation.md --labels /private/evaluation-labels.json
```

本地命令同样保留 pending 保护。Windows 支持本地文件锁，并通过条件依赖安装时区数据库；Linux 使用原有 flock。

## 6. 校准与评估

标签文件为私有 JSON 对象，键为 DBLP ID，值必须是布尔值。校准集与独立测试集的 ID 不可重叠；不要把未知标签视为负例。

```json
{
  "conf/aaai/EXAMPLE_POSITIVE": true,
  "conf/aaai/EXAMPLE_NEGATIVE": false
}
```

以上 ID 仅示范格式，不能用于实际质量评价。使用 shadow 生成的 diagnostics 进行校准提案：

```bash
python scripts/evaluate_recommendations.py \
  --diagnostics /private/recommendations-evaluation.diagnostics.json \
  --labels /private/calibration-labels.json \
  --test-labels /private/heldout-labels.json \
  --calibrate \
  --target-precision 0.8 \
  --output /private/calibration-proposal.json
```

脚本为含摘要和仅标题两类分别搜索门槛，每类至少需要 3 个正例和 3 个负例；该数量只是最小运行条件，正式建议仍为至少 60 篇标注。提案默认为 `approved=false`。审查独立测试结果、数量与覆盖率后，再显式改为 true，并存入私有状态仓库 `state/calibration.json`。

Actions 自动从该文件加载校准。模型 revision、分块策略、方向描述、Zotero 参考集合或相关权重发生变化时，校准匹配检查会失效，须重新评估。首次上线建议限制参与画像的 Zotero collection，减少研究方向以外文献的干扰。

语义分数是方向与较弱参考信号的组合，不是相关性概率。阈值脚本报告的是标注候选范围内的表现，不代表全库真实召回率；不使用未标注论文虚构指标。自动提案不会自动启用生产。

## 7. 数据迁移与恢复

SQLite schema v3 新增候选、元数据、向量和判断缓存表，兼容扩展 source_cursors；保留旧 papers、推荐记录和投递状态。

迁移不会清空 seen.json。运行时继续合并 JSON 与 SQLite delivered IDs。首次完整扫描的论文标记为历史导入；初始化后的新发现才进入新发现队列。使用新状态表不等于认为论文已阅读或已发送。

正式运行前保留私有状态仓库的当前 SHA；迁移/部署验证使用副本。回滚不要直接恢复投递前数据库，否则可能丢失升级期间已投递 IDs；先合并新增投递记录。代码回退与 schema 兼容性必须一起检查。

pending 或 SMTP accepted 问题仍使用原有恢复流程，不能通过清空向量或重建候选库绕过。状态 push 失败需要审查投递事务，避免邮件重发。

数据库达到约 100 MiB 或 clone/push 持续变慢时评估迁移存储。私有 `reports/` 也会累积，需要用户制定保留策略；本次不自动删除历史或重写 Git 仓库。

## 8. 已执行验证与边界

本地执行环境：Windows、Python 3.11.15、独立项目 `.venv`。基础安装、可选 CPU 依赖安装、`pip check`、Python 编译和新增模块静态检查已通过。

- 单元回归：37 项通过，覆盖原有反机器人与降级，以及续扫、重叠推进、transport 重置、动态年份、旧 schema、事务原子性、完整 CLI 隔离预览、零发送、缓存失效、负缓存、LLM 原文证据等。
- 真实 DBLP：AAAI 2026 连续两次各取 2 条记录，游标从 0 到 2 再到 4，持久候选为 4 条，通过。仅使用临时数据库，不接触私有状态和邮件。
- 真实 CPU embedding：固定 revision `5c38ec7c405ec4b44b94cc5a9bb96e735b38267a` 加载并编码 2 条合成健康检查文本，排序与缓存断言通过；首次编码链路约 24.7 秒，包含该次模型加载/下载等开销。该数字不是 GitHub runner 的性能基准。
- CI 新增 Python 3.11/3.12/3.13 基础测试、安装后离开源码目录的导入检查，以及独立 Python 3.12 Linux CPU 模型 smoke job。

尚未执行个人标注准确率评价、真实私人邮件投递、完整会议索引回填或 7～14 天灰度观察。这些需要实际用户数据与上线观察；不能由小规模 smoke test 代替。

## 9. 主要审查点

1. 默认无摘要延期可能降低每日数量，属于质量优先的显式取舍。需要标题级推荐时修改 selection 并单独评估。
2. 探索方向仍使用词项主题规则；语义主方向由 research_profile 定义，不把纯 LLM Agent 自动升级为主方向。
3. 首版画像辅助使用近期最多 20 篇 Zotero 文献，默认权重 0.15；尚未提供人工分方向 reference IDs 的管理界面。
4. 整轮 OFFSET 扫描不是数据库快照；页重叠和周期复查能降低遗漏风险，不能保证任意动态插入都在当天捕获。
5. 多模态对比、跨编码器重排、自建向量服务和多会议来源扩展仍未引入，避免将本次优化扩大为完整平台重写。
