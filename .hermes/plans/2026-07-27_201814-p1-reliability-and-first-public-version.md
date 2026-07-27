# P1 首个公开版本可靠性实施方案

> **For Hermes:** P0 通过后实施；每个代码任务遵循 RED→GREEN→REFACTOR，并在提交前执行独立代码审查。

**目标：** 将 P0 合规 MVP 升级为可长期每日运行、不会漏掉大量候选、不会因并发/状态损坏重复推送、可安装且有真实投递语义的 `v0.2.0`。

**架构：** 拆分单文件为可安装包；使用确定性的候选增量策略；SQLite 统一管理论文、外部 ID、运行、投递、缓存和反馈；状态写入具备事务与单实例锁；投递成功后才标记 delivered。

**技术栈：** Python 3.11+、stdlib SQLite、可选 HTTP/邮件适配器、GitHub Actions。

---

## 完成定义

- 候选获取具备分页/游标，不会永远只看每个 venue 的前 N 条。
- 已存在于 Zotero 的论文不会再次推荐。
- 多 collection 不受配置顺序截断偏置影响。
- 状态持久化原子、可迁移、可恢复；并发实例不会互相覆盖。
- 至少一种真正投递通道完成，并只在投递成功后标记 delivered。
- 项目可通过 `pipx install` 或 `python -m` 安装运行。
- 配置统一校验，错误不再以意外 traceback 呈现。

## Task 1：拆分为可安装 Python 包

**文件结构：**

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
  delivery/base.py
```

**步骤：**

1. 先写 CLI smoke test：`ccf-paper-scout --help` 返回 0。
2. 添加 `pyproject.toml` 和 entry point。
3. 逐模块迁移，每迁移一个函数就迁移对应测试。
4. 保留 `paper_scout.py` 作为一版兼容 shim，输出弃用提示。
5. 更新 README 安装说明。

**验证：**

```bash
python3 -m venv /tmp/ccf-scout-venv
/tmp/ccf-scout-venv/bin/pip install .
/tmp/ccf-scout-venv/bin/ccf-paper-scout --help
```

## Task 2：集中配置校验和 doctor 命令

**目标：** 在网络请求前发现错误配置。

**文件：**
- `src/ccf_paper_scout/config.py`
- `src/ccf_paper_scout/cli.py`
- `tests/test_config.py`

**校验：**

- `years` 是合理年份列表；
- `venue_ids` 存在且唯一；
- `per_venue/max_results/enrich_limit` 非负；
- `explicit_interests` 为字符串列表；
- 输出和 state 路径可写；
- LLM enabled 时 endpoint、model、key env 齐全；
- 配置版本可迁移。

**CLI：**

```bash
ccf-paper-scout doctor --config config.json
```

只打印密钥是否存在和长度，不打印值。

## Task 3：设计确定性 DBLP 分页和增量窗口

**目标：** 避免每次只获取同一批前 N 条。

**文件：**
- `src/ccf_paper_scout/sources/dblp.py`
- `src/ccf_paper_scout/state.py`
- `tests/test_dblp.py`

**实现：**

1. 支持 `start` + `h` 分页。
2. 保存每个 venue/year 的抓取游标与最后检查时间。
3. 每次运行从最近窗口获取，直到：
   - API 无更多结果；
   - 达到配置总上限；
   - 连续若干页全部已见；
   - 超出年份/时间边界。
4. 保存原始 `fetched_at`、DBLP key、year、venue。
5. 明确 API 排序不能保证“最新”时，不依赖隐含顺序；本地按可用日期/年份和首次发现时间排序。

**测试：** 多页、空页、重复页、镜像切换、断点恢复、API 返回顺序变化。

## Task 4：增加 Zotero 库内论文去重

**目标：** 不推荐用户已经收藏的论文。

**文件：**
- `src/ccf_paper_scout/zotero.py`
- `src/ccf_paper_scout/models.py`
- `tests/test_zotero.py`

**匹配优先级：**

```text
DOI exact
DBLP/arXiv external ID exact
normalized title exact
high-confidence fuzzy title（只标记建议，默认不硬排除）
```

Zotero 读取增加 DOI、URL、extra、publicationTitle、tags、collections。

**报告：** 输出因 Zotero 已存在而排除的数量。

## Task 5：修复多个 collection 的全局截断偏置

**目标：** 多个目标 collection 都能参与兴趣建模。

**算法：**

```text
逐 collection 分页读取完整候选
→ 以 Zotero key 去重
→ 全局按 dateAdded 排序
→ 再应用 recent_interest_items
```

可选增加每 collection 配额。

**回归测试：** 第一个 collection 超过 cap，第二个 collection 仍有更新论文时，第二个 collection 的新论文必须进入最终兴趣集。

## Task 6：SQLite 状态模型与迁移

**目标：** 替换易损坏的 `seen.json` 和 translations JSON。

**表：**

```text
papers
external_ids
source_records
recommendation_runs
recommendation_items
translation_cache
delivery_attempts
source_cursors
schema_migrations
```

**要求：**

- 事务提交；
- WAL 模式；
- 唯一约束防重复；
- 从旧 JSON 一次性迁移；
- 自动备份旧文件；
- schema version。

**测试：** 中断回滚、重复 insert、迁移幂等、损坏旧 JSON 的可操作错误。

## Task 7：单实例锁与原子报告写入

**目标：** 避免 cron 重叠造成重复推送和 lost update。

**实现：**

- 进程启动时获取文件锁；
- 已有实例时退出码明确且日志说明；
- 报告写入临时文件，`flush/fsync/os.replace()`；
- 锁在异常时可靠释放。

**测试：** 启动两个实例，第二个必须快速失败且不修改 state。

## Task 8：实现事务性投递适配器

**目标：** 至少支持一种真实“推送”，并只在成功后标记 delivered。

**建议首选：** SMTP 邮件；后续再加 Telegram/飞书。

**接口：**

```python
class DeliveryAdapter(Protocol):
    def send(self, report: Report) -> DeliveryReceipt: ...
```

**状态机：**

```text
selected → rendered → delivery_pending → delivered
                                  ↘ delivery_failed
```

只有 `DeliveryReceipt.success` 后，论文才进入 delivered 去重集合。失败任务下次可重试，且使用 idempotency key 防重复发送。

## Task 9：推荐运行历史与可审计输出

**CLI：**

```bash
ccf-paper-scout runs list
ccf-paper-scout runs show <run-id>
ccf-paper-scout retry <run-id>
ccf-paper-scout state export
```

每次运行记录：配置摘要、抓取数、过滤数、候选数、选中数、投递状态、错误摘要和耗时；绝不记录密钥。

## Task 10：集成测试与发布候选

**测试层：**

- unit：纯函数和错误分支；
- contract：录制/构造 Zotero、DBLP、OpenAlex fixtures；
- integration：低频公共 API smoke test；
- end-to-end：本地兴趣 JSON → DBLP fixture → 排序 → Markdown → fake delivery → delivered 状态。

**验收：**

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
ccf-paper-scout doctor --config config.example.json
ccf-paper-scout run --config tests/fixtures/e2e-config.json
```

## P1 发布门

```text
[ ] 可安装 CLI
[ ] DBLP 分页/游标和确定性窗口
[ ] Zotero 库内 DOI/ID 去重
[ ] 多 collection 无顺序偏置
[ ] SQLite 事务状态与迁移
[ ] 单实例锁和原子写入
[ ] 至少一种事务性投递
[ ] 配置 doctor 与统一错误
[ ] unit/contract/e2e 全部通过
```
