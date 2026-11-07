# 固定快照的离线排序评测

目标：先确认跨会议摘要覆盖与排序差异，再由人工标签决定是否更换推荐算法。此流程仅面向单个固定兴趣画像，不等同于多查询搜索评测或大规模个性化推荐评测。

## 实验范围

- AAAI、ACL、ICML、NeurIPS，固定 2025 年；每分区最多 3 页、每页 100 条，总预算 12 页。
- 继续使用现有主会核验清单；ICLR 尚未纳入。前几页按 DBLP ID 排列，不代表完整会议或随机样本。
- 独立目录保存数据、向量和输出，不读生产数据库或私人文库，不发邮件，不调用生成式模型。
- 研究画像沿用示例配置的 RL、MARL、LLM-assisted RL；语义和关系策略均为 shadow。
- 实验的固定候选池和轮询预算独立于每日流程，不会自动改变现有推荐策略。

## 1. 采集候选

在项目根目录的 PowerShell 中执行。每一步的输出目录必须是新的，防止覆盖旧实验。

```powershell
$env:PYTHONUTF8 = '1'
.\.venv\Scripts\python.exe .\scripts\benchmark_papers.py collect --output .\local-notes\my-benchmark\collection
```

需要访问 DBLP。保存 `candidates.json`（含所有采集记录和资格状态）与独立 `acquisition.sqlite3`。查询失败会停止，不将不完整来源伪装成完整结果。

## 2. 补摘要并冻结快照

```powershell
.\.venv\Scripts\python.exe .\scripts\benchmark_papers.py prepare --candidates .\local-notes\my-benchmark\collection\candidates.json --output .\local-notes\my-benchmark\evidence --budget 100
```

前提：已安装项目的 semantic 可选依赖，固定版本的 BGE 模型位于项目 `.model-cache` 中。脚本开启 Hugging Face 离线模式，模型缺失时会报错，不会悄悄下载或更换模型。

处理过程：

1. 给所有已核验候选计算标题语义分数，作为摘要获取的辅助依据。
2. 在四个会议间轮流取候选；每个会议交替使用词项前列、语义前列，并保留约 20% 的随机探索位置。
3. PMLR、NeurIPS、ACL Anthology 使用 DBLP 给出的官方论文页，核对规范化标题及作者重合；ACL 支持明确的当年主会长／短论文页面。有 DOI 时可走 OpenAlex 精确标识路径，并核对返回 DOI。OpenAlex 的非空内容仍可能只是书目引用，不能直接等同于有效摘要，需在冻结实验前核查。
4. 所有摘要路径共享总请求预算；一次请求不自动重试或跟随跳转。来源返回 429 后本轮停止请求该来源，其余来源继续。
5. 保存每篇摘要的来源、文本、内容哈希和尝试记录；缺摘要区分预算未覆盖、无支持标识、失败、来源限流等。
6. 写入 `snapshot.json`：配置、完整文本、获取统计、代码文件哈希、原始候选文件哈希、数据集哈希；保存模型向量缓存。

100 次请求不是“必有 100 篇摘要”。失败、无摘要、回退尝试也消耗预算。每次请求后保存 `metadata-checkpoint.json`，供中断排查；当前工具没有自动恢复中断的 prepare 任务，重试使用新目录并重新计量预算。

该阶段的快照保存的是已经实现的新获取策略，不是对旧获取策略的随机对照试验。不能把不同候选池或不同分配策略下的摘要数直接解释为算法精度提升。

## 3. 离线比较与生成标注页

```powershell
.\.venv\Scripts\python.exe .\scripts\benchmark_papers.py compare --snapshot .\local-notes\my-benchmark\evidence\snapshot.json --output .\local-notes\my-benchmark\review
```

比较阶段不访问论文来源、不补摘要、不调用 LLM。校验快照哈希，复制向量缓存，重新计算词项及语义分数。所有候选必须完成语义计算，否则停止。

- `lexical`：词项分数的原始排序。
- `semantic`：语义分数的原始排序。
- `rrf`：两种名次的倒数融合，常数固定为 60；不直接相加不同尺度的分数。

三组原始排序共享“主会核验通过且具有摘要”的候选池，不加未经校准的语义门槛。现有词项流程的主题、最低分数及探索配额输出另列为基线，不能与原始排序混为一谈。

脚本运行两遍，检查排序和基线入选是否一致，并记录缓存命中、新编码数与最大浮点分差。生成：

- `comparison.json` / `comparison.md`：完整分数、逐篇现有过滤原因、排序与回放结果。
- `annotate.html`：自包含中文盲标页面，无外部资源、无模型分数。
- `annotation-manifest.json`：固定抽样与校准／测试划分。
- `labels-template.json`：空标签模板，可用于程序化标注；通常使用网页即可。

## 4. 人工需要做什么

需要人的环节是研究兴趣判断，不是安装权限或配置密钥。

1. 双击 `annotate.html`，使用 Edge 或 Chrome 打开；不需启动服务器。
2. 先统一标准：0 不相关、1 部分相关、2 高度相关。证据不足选“待确认”，不当成负例。
3. 阅读原文摘要，选择研究角色，例如通用 RL、MARL、LLM→外部 RL、RL→LLM、其他 Agent。
4. 复制支持判断的一段摘要原句，写一句个人理由。全文入口可辅助阅读；本轮证据字段只接受摘要中存在的原句。
5. 每标完一批点击“导出标注 JSON”。浏览器缓存只是便利机制，下载文件才是备份。
6. 再次打开可自动恢复同浏览器的进度，或点击“导入继续”。相同论文以导入文件为准，因此请导入最新版本。
7. 把最终导出文件放到实验 review 目录，命名 `labels.completed.json`，把路径告诉助手即可。

初始抽样包含三种排序前 20 的并集、会议分层及随机补充，通常共 80 篇；如果前列并集较大则保留，不强行截掉某个方法的候选。同 DOI 或规范化同标题记录合并分组后，再按会议预分配约 75% 校准、25% 测试。人工发现其他版本关联时，必须在评测前补充分组，不能假定自动识别了所有版本。

为降低判断偏差，建议标注结束前不阅读 `comparison.md` 的排序。页面不显示校准／测试归属；标注完成后先只评估校准部分。

## 5. 标签到位后的评估

```powershell
.\.venv\Scripts\python.exe .\scripts\benchmark_papers.py evaluate --comparison .\local-notes\my-benchmark\review\comparison.json --manifest .\local-notes\my-benchmark\review\annotation-manifest.json --labels .\local-notes\my-benchmark\review\labels.completed.json --fold calibration --output .\local-notes\my-benchmark\review\metrics.calibration.json
```

脚本检查数据集、论文 ID、重复标签、相关度范围、摘要证据和理由。未标注及“待确认”保持未知。

输出 P@5/P@10（等级至少 1）、严格 P@5/P@10（等级 2）、nDCG@5/nDCG@10，以及逐会议指标。Top-K 标签不完整或样本不足时返回 null；nDCG 还要求该比较池全部有标签。数值是在预分配标注池内部重新排序后的指标，不是全候选池指标，更不是全网召回率。样本量很小，仅支持探索性分析。

方案在校准集上选定并冻结后，才把 `--fold calibration` 改为 `--fold test`，并使用新的输出路径 `metrics.test.json`。不能根据测试结果持续调参后仍将其称为独立测试。

没有人工标签时，不会自动生成“正例”、调低门槛或批准 production。后续先决定排序方法，再单独评价关系过滤；本工具不拟合或批准现有生产阈值。

## 验证与边界

已有单元测试覆盖摘要身份核对、错误页拒绝、共享预算、限流隔离、会议分配、向量失效、无网络回放、标签隔离和缺失标签处理。运行：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
```

本轮在 Windows 验证。Linux 仍需由仓库 CI 执行，不能把本地测试当成已通过远端 CI。标注页面本地保存与导入导出已用独立无界面 Edge 做操作检查；自动化检查使用的合成标签不属于人工标签，不得用于效果评估。

## Daily 语义排序的正式校准

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


## LLM/RL 关系标签与校准

从影子报告抽取独立样本，分别标注出版资格、关系、实质 MARL 和是否希望收到。校准集与留出测试集使用两个文件，不能用同一批反复选阈值再报告最终准确率。

人工标签文件格式（以下 ID 是格式占位符，不是真实论文判定）：

```json
[
  {
    "id": "conf/venue/replace-with-real-record",
    "eligibility": "verified",
    "relations": ["llm_for_rl"],
    "substantial_marl": false,
    "wanted": true
  }
]
```

```bash
python scripts/evaluate_roles.py --diagnostics recommendations.diagnostics.json --labels role-test-labels.json
```

工具报告有标签选文相关性、关系混淆、主会误放行、MARL 误排除／暂缓和未覆盖标签数量；未标注记录不自动当负例，也不会自动生成“approved”。请保存在私有目录，不提交个性化标签到公开仓库。现有 `evaluation-labels.json` 的 true/false 格式不变，仍服务原有相关性评估。

关系阈值文件为私有 `state/relation-calibration.json`，已加入状态文件白名单：

```json
{
  "approved": false,
  "relation_signature": "复制本次 diagnostics 的 semantic.relations.signature",
  "thresholds": {"positive": 0.0, "margin": 0.1}
}
```

上述数值**只是结构示例，不是可用默认阈值**。在人工校准前不要创建这个文件；只有根据校准样本选定阈值并通过独立留出检查后，才填写实际数值并将 approved 设为布尔 true。错误批准值、旧模型／原型签名和非法数值会报错，而不是静默降级。
