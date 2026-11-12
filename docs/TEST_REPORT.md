# agent-hub 全量场景测试报告

- **被测版本**：0.3.1（`server/app.py`）
- **测试日期**：2026-11-12
- **测试方式**：`tests/e2e/run_e2e.sh` 起独立 Docker 栈（Postgres 16 + hub），真 HTTP、真子进程、真 CLI，无 mock
- **结果**：**149 passed / 0 failed / 2 xfailed**（27 个单测 + 122 个场景用例）
  - 拆掉数据卷从零重跑同样通过：`122 passed, 2 xfailed in 11.32s`
- **结论**：**主线链路可用，可以从"能跑"进入"能发给团队"的阶段**；仍有 2 个已定位的行为缺陷（见 §4），其中一个会让交接时"按项目看原件"漏数据。

---

## 1. 测试范围

| 层 | 入口 | 覆盖 |
|---|---|---|
| 服务器 | FastAPI 20 个端点 | 鉴权、上报、检索、项目/交接、提示词、原件层（上传/下载/版本/回滚/占用锁/用量） |
| hook | `hooks/cc_hook.py`、`hooks/codex_notify.py` | 六类事件、GLM/MiniMax 共用、坏输入静默、Stop 自动上传 |
| MCP | `mcp/mcp_query.py` | 7 个工具直调 + stdio JSON-RPC 协议握手 |
| 客户端 | `client/agenthub.py`、`client/wb.py` | 8 个子命令、打包/还原/补索引、路径重写、越界防护 |
| 数据库 | `schema.sql` + `migrations/002…005` | 全新安装 与 逐版升级 **逐列对比** |
| 并发 | 多线程 | 上报、占用锁争抢、并发下载、并发提交 |

**刻意不做 mock**：路径重写的坑（`/code/api` vs `/code/api-gateway`）、占用锁的时序、
打包体的成员名、CLI 的落盘位置，这些只有在真栈上才会暴露。

## 2. 环境与隔离

| 项 | 值 |
|---|---|
| Docker Engine / Compose | 29.4.0 / v5.1.2 |
| 数据库 | postgres:16-alpine（独立卷 `agenthub-e2e_pgdata`） |
| Python | 3.13.12 + pytest 9.2.0 + mcp 2.x |
| 端口 | `127.0.0.1:18421`（不与生产的 8421 冲突） |
| 部署配置 | `tests/e2e/hub-config.e2e.yaml`：原件层开启、`max_mb=2`、`keep_revisions=3`、`mode=auto` |

阈值调小是为了让"超限 / 裁剪"这类场景几秒内跑完；所有落盘路径都重定向到临时目录，
**没有读写本机真实的 `~/.workbuddy` 与 `~/.claude`**。

## 3. 场景结果

| 场景 | 用例 | 结果 | 关键断言 |
|---|---|---|---|
| S1 鉴权边界 | 20 | ✅ | 7 个受保护端点无 token 一律 401；错方案（`Token`/`Basic`）也 401；库里只有 sha256，搜不到明文 token |
| S2 上报与检索 | 15 | ✅ | 同 ext_id 24 次上报只 1 行 session；空 title/repo 不覆盖已有值；`sk-…` 与 `password: …` 入库前被替换；`%`/`_`/引号不炸 SQL |
| S3 项目与交接 | 9 | ✅ | 指派新 owner 时旧 owner 自动释放；交接报告含历任时间线、上一任会话、热点文件、技能包、共享结论、提问记录；非 owner 改任命 403 |
| S4 部署提示词 | 13 | ✅ | 实例名/模式/工具名/保留期随配置变；**不再宣称"服务器端已自动脱敏"**；严格模式含"用户未确认时不要调用" |
| S5 原件层 | 25 | ✅（1 xfail） | 上传解析 token 与活跃时长；相同内容再传 `unchanged`；纯追加 diff 只算新增 2 行；回滚生成 r3 且历史保留；占用锁对他人 409、过期后可接管；keep_revisions=3 时只剩 `[6,5,4,1]` |
| S6 WorkBuddy 打包体 | 8 | ✅ | 下载字节一致（旁挂文件不丢）；整包重写新旧路径都改对、旁挂内容不变；恶意 `../../` 成员**整包拒绝且不建目录** |
| S7 hook 与 MCP | 10 | ✅ | 六类事件按序入库；GLM 与 MiniMax 落在同一条 hook 上；MCP stdio 握手返回本部署提示词；分享→检索闭环 |
| S8 客户端 CLI | 11 | ✅ | 非交互且无 `--yes` 时**明确拒绝上传**（"已取消，没有上传任何内容"）；`pull` 的 slug 与 `cwd` 对齐；重复 pull 自动备份 |
| S9 并发 | 6 | ✅（1 xfail） | 24 并发上报无丢失；4 人抢锁只 1 人成功；同内容 4 并发只产生 1 个版本；上报 p95 < 1s |
| S10 升级路径 | 7 | ✅ | 升级后 8 张表与全新安装**列定义完全一致**；005 给存量原件补 `r1` 基线；迁移与 `schema.sql` 均可重跑 |

## 4. 本轮发现

### 4.1 已修复（测试先红后绿）

| # | 问题 | 影响 | 修法 |
|---|---|---|---|
| 1 | **"空 tar" 压缩炸弹绕过体积守卫**：全零字节的 gzip 能被 `tarfile` 当成合法空包，成员清单为空 → 成员体积合计 0 → 守卫放行，实际展开 100MB 写进库。再套一层 gzip 也能绕过（判的是原始请求体，不是最终入库内容） | 一条 100KB 的请求就能让库涨 100MB，重复几次打爆磁盘 | `bundle_guard()` 先按**解压流**限额（分块计数，超限即断）；上传接口改为对**最终入库内容**判定是否打包体 |
| 2 | 重复用户名注册抛 500 | hooks/脚本拿不到可读错误，管理员以为服务挂了 | `create_user` 捕获 `UniqueViolationError` → 409；名字限 1~64 字符 |
| 3 | 非法 `ts` 抛 500 | 上游 CLI 字段一变，整条上报链路口吐 500 | `EventIn.ts` 加 ISO 8601 校验 → 422 |
| 4 | 提示词只列了 5 个工具，MCP 实际有 7 个，**缺的正是交接用的 `list_team_projects` / `get_project_handover`** | agent 不知道有交接查询能力，用户"换人接手"的诉求落不了地 | `prompt.py` 补两行工具说明；新增"提示词工具名 ⊆ MCP 实际工具"的契约测试 |
| 5 | `mcp_query.py` 只支持 `mcp<2`：`from mcp.server.fastmcp import FastMCP` 在 2.x 已被移除，今天新装环境**直接起不来** | 新成员按 README `pip install "mcp[cli]"` 会装到 2.x，MCP 服务 import 失败 | 兼容导入（1.x `FastMCP` / 2.x `MCPServer`），并用 2.x 实测 stdio 协议通过；补 `mcp/requirements.txt` |

### 4.2 未修复（已写成 xfail，修好后会自己变 XPASS）

| # | 问题 | 影响 | 建议 |
|---|---|---|---|
| 6 | `wb-sync` 只上传原件、不上报事件，而 `transcripts` 没有 project 字段，只能靠 join `sessions` 反查 —— WorkBuddy 的原件在"按项目看用量/清单"里**整条消失**，且不报错 | 交接时看不到对方 WorkBuddy 的会话；用量统计漏掉一大块 | `transcripts` 加 `project text`（迁移 006），上传时从 query 参数写入，`wb-sync` 传 `--project`，列表/用量改为 `coalesce(t.project, s.project)` |
| 7 | 上传时 `rev` 是"读-算-写"，没有串行化：两个不同内容并发提交会拿到**同一个版本号**，后写者静默覆盖前一版 | 一人两台机器同时同步（或 CI 与人工撞车）会丢一个版本 | 上传事务内 `select … from transcripts where user_id=$1 and ext_id=$2 for update`，或对 `(user_id, ext_id)` 取 advisory lock 后再算 `rev` |

### 4.3 复测确认（不是新问题，但值得记一笔）

- 早前修的 P0-1（打包体路径穿越）在**真实往返**里成立：构造含 `../../../../../../tmp/PWNED-e2e.txt` 的包上传→下载→还原，抛 `RuntimeError: 打包体成员越界，已拒绝`，目标目录未被创建，`/tmp/PWNED-e2e.txt` 不存在。
- `keep_revisions=3` 的裁剪逻辑与"永远保留 r1 基线"一致（`[6,5,4,1]`），r2 下载返回 404 且提示"可能已被历史裁剪"。

## 5. 建议的推进顺序

1. **#6 原件挂项目**（影响交接可用性，改动集中在 schema 006 + 上传接口 + `wb-sync`）——半天。
2. **#7 上传串行化**（一条 `for update`）——1 小时，但需要补一条并发回归。
3. 之前报告里未动的 P1：每回合整份重传（O(n²) 流量）、每版全量快照的磁盘增长、`deploy/Caddyfile` 端口写错（8420 vs 8421）。
4. 把 `tests/e2e` 接进 CI：`run_e2e.sh run` 一条命令即可，无外部依赖（只要 Docker）。

## 6. 复现命令

```bash
cd agent-hub
tests/e2e/run_e2e.sh down                     # 清数据卷
PYTHON=/path/to/python-with-pytest tests/e2e/run_e2e.sh run
```

单独跑某个场景：`tests/e2e/run_e2e.sh test -k s5`；
单测（不需要 Docker）：`pytest tests -q --ignore=tests/e2e`。
