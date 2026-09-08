# DBLP 访问韧性改进说明

## 背景

2026-09-08 的 `Paper Scout Daily` 在远程 `main` 提交 `8242e38` 上失败。DBLP 的两个
Search API 域名均返回 HTTP 200，但正文是 `text/html` 的反机器人验证页面，标题为
`Making sure you're not a bot!`。旧实现直接调用 `json.load(response)`，把该响应记录成
`JSONDecodeError`，并对 8 个 Venue、2 个年份、2 个域名分别重试，最终得到 0% 的成功率。

## 改进目标

1. 不把 HTML、空正文或损坏 JSON 当作可无限重试的瞬时错误。
2. 所有 DBLP 请求共享全局限速，避免 Venue/Year 切换形成突发流量。
3. Search API 被拦截时自动切换到 DBLP 官方 SPARQL 服务。
4. 两条机器访问路径都失败时快速熔断，不再重复遍历所有分区。
5. 保持原有 CCF-A venue 白名单和 DBLP record-key 前缀校验。

## 实现内容

### 响应分类

新增四类 DBLP 错误：反机器人阻断、协议错误、瞬时请求失败和所有访问路径不可用。
程序读取正文前检查 `Content-Type`，并识别验证页面的稳定特征。反机器人页面、非 JSON
正文和损坏 JSON立即失败；只有 429、5xx、超时、断连和 URL transport error 进入有限重试。

### 全局限速和熔断

`dblp.request_delay_seconds` 现在约束进程内每一次 DBLP 请求，包括不同 Venue、不同年份、
Search API 域名切换和 SPARQL 请求。两个 Search API 域名受阻后，本次进程不再访问搜索接口，
后续分页直接使用 SPARQL。如果 SPARQL 也失败，则停止剩余 Venue/Year 遍历并报告明确错误。

### SPARQL 降级

SPARQL 查询使用 `publishedInStream`、`yearOfPublication` 和出版物 RDF 类型约束数据范围：

- conference 对应 `dblp:Inproceedings`；
- journal 对应 `dblp:Article`；
- stream URI 来自受信任的本地 `dblp_key`；
- 返回标题、作者、年份、DOI、文档入口和 DBLP record URI；
- 解析后再次检查 `conf/<key>/` 或 `journals/<key>/` 前缀。

这避免把 proceedings 容器记录误当成论文，也保留了现有资格边界。

## 配置

建议默认值：

```json
{
  "dblp": {
    "request_delay_seconds": 3.0,
    "timeout_seconds": 30,
    "max_attempts": 3,
    "sparql_fallback": true,
    "failure_policy": "continue",
    "minimum_success_ratio": 0.75
  }
}
```

不建议通过把 `minimum_success_ratio` 设为 0 来掩盖故障。当前 `main` 只有 DBLP 发现来源，
这样做只会把“明确失败”变成“零候选的成功运行”。

## 测试覆盖

新增测试覆盖：

- HTTP 200 反机器人 HTML 不重试；
- 普通非 JSON HTML 不重试；
- 503 等瞬时错误仍有限重试；
- 进程级请求间隔；
- SPARQL 查询约束与结果映射；
- Search API 阻断后切换 SPARQL，并对后续请求保持熔断；
- Search API 与 SPARQL 同时失败时停止剩余分区。

CI 在 Python 3.11、3.12 和 3.13 上执行安装、语法编译和全部单元测试。

## 审查重点

1. SPARQL 作者使用 `GROUP_CONCAT`，顺序由数据服务决定，不应依赖作者顺序进行去重。
2. SPARQL `ORDER BY` 使用 publication URI，属于稳定分页顺序，不代表论文质量或发布时间顺序。
3. 该修复降低了 DBLP Search API 单点故障，但 DBLP 整体仍是外部依赖。
4. 后续多源版本应允许 DBLP 整体失败后继续使用 arXiv/IEEE，并在报告中标记 degraded。

## 验收方法

```bash
python -m pip install .
python -m compileall -q paper_scout.py state_store.py src scripts
python -m unittest discover -s tests -v
```

网络集成验收应在 Search API 返回反机器人页面时调用一个小页面，确认日志只出现一次
`search_api=blocked fallback=sparql`，随后从 SPARQL 得到符合 record-key 前缀的论文记录。
