# P0 GitHub 发布阻塞项实施方案

> **For Hermes:** 使用 TDD 与独立代码审查逐项实施；P0 全部完成前，不创建公开 release。

**目标：** 清除许可证、数据归属、资格表述、OpenAlex 容错、venue 数据一致性、测试发现和隐私文档等公开发布阻塞项，使项目达到 `v0.1.0-alpha` 的最低可信发布门槛。

**架构原则：** 保留“CCF venue 资格门 → 本地兴趣排序”的核心，但将所有承诺收敛到真实能力；第三方派生数据拥有独立来源记录；所有非关键元数据补全必须 best-effort；配置和 venue 数据在启动时 fail-fast 校验。

**技术栈：** Python 3.11+ 标准库、unittest、JSON、GitHub Actions。

---

## 完成定义

- 根目录存在明确的项目许可证和第三方声明。
- `data/ccf_a_venues.json` 可追溯到精确上游 commit、文件和许可证。
- README 不再使用“论文质量认证”“双重校验”“最新论文完整源”等过强表述。
- OpenAlex 400/404 不会中断整次运行；429/5xx 仍按策略重试。
- venue schema 无静默重复覆盖，canonical venue 与 alias 分离。
- 标准测试 discovery 能运行非零测试，CI 覆盖 Python 3.11/3.12/3.13。
- README 不含开发者本机绝对路径，包含数据流、隐私、限制和免责声明。
- `RESEARCH.md` 与当前功能、测试数和定位一致。

## Task 1：确定许可证与代码来源边界

**目标：** 明确本项目代码许可证，并确认未复制 AGPL 源码。

**文件：**
- 创建：`LICENSE`
- 创建：`THIRD_PARTY_NOTICES.md`
- 创建：`docs/provenance.md`
- 修改：`README.md`

**步骤：**

1. 由维护者在 MIT 与 Apache-2.0 中选择本项目许可证；默认建议 Apache-2.0，若追求最简生态则选择 MIT。
2. 逐文件检查 Git 历史，记录哪些代码是独立实现、哪些内容仅受概念启发、哪些数据实际派生。
3. 明确写入：本项目没有直接复用 `TideDra/zotero-arxiv-daily` 的 AGPL 源码；若人工检查发现直接复制，立即停止并重新评估许可证。
4. 添加项目许可证全文。
5. 添加第三方 notices：
   - `WenyanLiu/CCFrank4dblp`：派生数据来源，MIT；
   - `ccfddl/ccf-deadlines`：只有实际使用其数据时才列为派生来源，否则列入 acknowledgements；
   - `TideDra/zotero-arxiv-daily`：概念启发，不声明代码派生。
6. 在 README 加入 License、Acknowledgements、Disclaimer。

**验证：**

```bash
test -f LICENSE
test -f THIRD_PARTY_NOTICES.md
git grep -n "CCFrank4dblp"
git grep -n "zotero-arxiv-daily"
```

**提交：**

```bash
git add LICENSE THIRD_PARTY_NOTICES.md docs/provenance.md README.md
git commit -m "docs: add license provenance and third-party notices"
```

## Task 2：固定 CCF 派生数据来源并提供可复现生成器

**目标：** 使 venue 数据来源、转换和更新可审计。

**文件：**
- 创建：`scripts/build_venue_data.py`
- 创建：`data/README.md`
- 创建：`tests/test_venue_data.py`
- 修改：`data/ccf_a_venues.json`

**步骤：**

1. 记录上游精确 commit SHA、源文件路径、抓取日期、许可证。
2. 在 `data/README.md` 解释字段来源：`id/type/dblp_key/abbr/name/rank`。
3. 生成器只接受固定 commit 的原始文件，不读取浮动 `main/master`。
4. 生成器输出排序稳定、格式稳定的 JSON，并写入 `source_commit` 和 `generated_at`。
5. 添加回归测试：
   - schema 完整；
   - `id` 唯一；
   - canonical ID 唯一；
   - rank 全为 A；
   - 生成两次结果一致。
6. 对 `nips`、`kbse`、`usenix`、VLDB/PVLDB、FSE 等争议/重复项人工复核并记录决策。

**测试先行：** 先写“重复 canonical key 导致失败”的测试，并确认当前数据测试失败，再修数据。

**验证：**

```bash
python3 -m unittest tests.test_venue_data -v
python3 scripts/build_venue_data.py --check
```

**提交：**

```bash
git add scripts/build_venue_data.py data tests/test_venue_data.py
git commit -m "data: make CCF venue mapping reproducible and validated"
```

## Task 3：引入稳定 venue ID 与 alias schema

**目标：** 消除只按 `dblp_key` 建字典导致的静默覆盖和会议/期刊歧义。

**文件：**
- 创建：`src/ccf_paper_scout/venues.py`（若尚未模块化，可先创建 `venue_data.py`）
- 修改：`paper_scout.py`
- 修改：`config.example.json`
- 测试：`tests/test_venue_data.py`、`tests/test_scout.py`

**设计：**

```json
{
  "id": "conference:neurips",
  "type": "conference",
  "canonical_dblp_key": "nips",
  "aliases": ["neurips", "nips"],
  "rank": "A"
}
```

**步骤：**

1. 写失败测试：重复 canonical ID、同一 alias 指向多个 venue 时启动失败。
2. 实现 `load_venues()` 和 `validate_venues()`。
3. 配置增加 `venue_ids`；暂时兼容旧 `venue_keys` 并输出弃用警告。
4. `fetch_dblp()` 接收 canonical venue 对象，不再从可冲突字典静默覆盖。
5. 报告同时显示 canonical 名称与 DBLP key。

**验证：**

```bash
python3 -m unittest tests.test_venue_data tests.test_scout -v
```

## Task 4：修复 OpenAlex best-effort 容错

**目标：** 单篇 DOI 元数据缺失不影响整次推荐。

**文件：**
- 修改：`paper_scout.py` 中 `enrich_candidates()` / HTTP 层
- 测试：`tests/test_scout.py`

**测试矩阵：**

- 200 + 有摘要：`enriched += 1`
- 200 + 无摘要：missing，不报错
- 400/404：missing，继续下一篇
- 429：重试后成功或计 failed
- 500/连接断开：重试后成功或计 failed
- 401/403：打印服务级诊断；是否中断由明确策略决定
- 畸形 JSON/schema 缺失：failed，继续

**返回统计：**

```python
EnrichmentStats(attempted, enriched, missing, failed)
```

**验证：**

```bash
python3 -m unittest tests.test_scout.ScoutTests.test_openalex_404_is_skipped -v
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

## Task 5：修正文档承诺、品牌定位与隐私模型

**目标：** README 中每项核心宣称都可被代码和测试支撑。

**文件：**
- 修改：`README.md`
- 创建：`docs/privacy.md`
- 创建：`docs/limitations.md`
- 修改：`RESEARCH.md`

**必须替换的表述：**

- “质量过滤” → “venue 资格过滤”
- “双重校验” → “白名单约束 + DBLP key 前缀复核”
- “最新论文” → “DBLP 已索引且进入检索窗口的正式论文候选”
- “Zotero 数据只在本机读取” → “Zotero 兴趣语料仅在本地参与排序，不发送给 DBLP/OpenAlex/LLM”
- “TF-IDF + BM25” → “带时间权重的稀疏词项匹配”

**隐私表：** 明确 Zotero、DBLP、OpenAlex、LLM、本地磁盘各自收到/保存的数据。

**免责声明：** 本项目与 CCF、Zotero、DBLP、OpenAlex、会议和出版社无隶属或背书关系；venue 等级不等于单篇论文质量结论。

**验证：**

```bash
git grep -n "双重校验\|质量认证\|只在本机读取"
```

预期：不存在未加限定的过强承诺。

## Task 6：修复标准测试发现并建立 CI

**目标：** GitHub 上每次提交都真实运行测试，而非 0 tests 假绿。

**文件：**
- 创建：`tests/__init__.py`
- 创建：`.github/workflows/ci.yml`
- 创建：`scripts/validate_repo.py`
- 修改：`README.md`

**CI 矩阵：** Python 3.11、3.12、3.13。

**CI 命令：**

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
python -m py_compile paper_scout.py tests/test_scout.py
python scripts/validate_repo.py
```

`validate_repo.py` 检查：

- 测试数量 > 0；
- JSON 可解析；
- venue schema 与唯一性；
- README 不含 `/home/zlw`；
- 示例配置不含真实密钥。

**验证：**

```bash
python3 -m unittest discover -v
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

两条均应运行非零测试。

## Task 7：执行发布前安全与完整性检查

**目标：** 确认仓库及历史中无密钥和私有数据。

**检查：**

```bash
git status --short
git ls-files
git log --all --name-only --format=
git grep -nEi 'sk-[A-Za-z0-9_-]{10,}|Bearer [A-Za-z0-9._-]{10,}'
git check-ignore -v config.json state/seen.json zotero_library_debug.md .env.local
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

人工确认历史中从未跟踪：

- `config.json`
- `.env.local`
- `state/`
- `recommendations*.md`
- `zotero_library_debug.md`

若历史中曾出现真实密钥，必须撤销/轮换密钥，并在发布前清理 Git 历史。

## Task 8：定义 P0 发布门

只有以下条件全部满足才能标记 P0 完成：

```text
[ ] LICENSE 与第三方 notices 完整
[ ] venue 数据可复现且无静默重复
[ ] OpenAlex 常见失败不终止任务
[ ] README 承诺边界准确
[ ] 隐私与免责声明清楚
[ ] 标准测试 discovery 非零
[ ] Python 3.11/3.12/3.13 CI 通过
[ ] Git 历史无密钥和私有文库
```

**最终验证：**

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 scripts/validate_repo.py
git diff --check
git status --short
```
