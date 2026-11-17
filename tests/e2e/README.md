# 全量场景测试（E2E）

真栈、真 HTTP、真子进程。不 mock：起一套独立的 Docker Compose（Postgres + hub），
把成员机器上会发生的事都跑一遍——从 hooks 上报到 CLI 回放，从项目交接到压缩炸弹。

## 跑法

```bash
tests/e2e/run_e2e.sh down                       # 清掉上次的数据卷（想要干净结果时）
PYTHON=/path/to/python-with-pytest tests/e2e/run_e2e.sh run
```

`run` 会做四件事：构建镜像 → 起栈 → 等 `/health` → 跑 `tests/e2e`。
只想跑测试（栈已在跑）：`tests/e2e/run_e2e.sh test`。看日志：`logs`。

依赖：Docker + 一个装了 pytest 的 Python。MCP 协议相关的 3 个用例需要 `mcp[cli]`
（`pip install "mcp[cli]"`），没装则自动 skip，不算失败。

## 隔离说明

| 项 | 生产 | 测试 |
|---|---|---|
| 端口 | 8421 | **18421**（不会撞上正在跑的正式实例） |
| compose project | 默认 | `agenthub-e2e`（独立容器名与数据卷） |
| 部署配置 | `server/hub-config.yaml` | `tests/e2e/hub-config.e2e.yaml`（原件层开启、max_mb=2、keep_revisions=3） |
| 密钥 | `.env` | `tests/e2e/.env.e2e`（仅测试用） |
| 数据 | 生产库 | 测完 `down -v` 即销毁 |

**不会碰本机真实数据**：所有落盘路径都指到 `mkdtemp()` 出来的临时目录
（`AGENT_HUB_WB_DIR` / `AGENT_HUB_CLAUDE_DIR` / `AGENT_HUB_STATE` 全部重定向）。

## 场景清单

| 文件 | 覆盖 |
|---|---|
| `test_s1_auth.py` | 健康检查真连库、无 token/坏 token/错方案一律 401、管理员与成员的分层、token 只存哈希、畸形 JSON |
| `test_s2_ingest_search.py` | 事件上报、项目自动创建、同 ext_id 幂等、空 title/repo 不覆盖已有值、**入库前脱敏**、四种检索维度、`limit` 上下界、非法时间戳 |
| `test_s3_projects_handover.py` | 项目档案只允许现任负责人改、**负责人时间线**（指派即释放）、交接报告六块数据、成员角色不误释放 owner、权限与错误码 |
| `test_s4_prompt.py` | 部署专属提示词：实例名/隐私模式/工具清单/保留期、**不许宣称原件已脱敏**、"同步永远由用户发起"、三种模式文案互不相同 |
| `test_s5_transcripts.py` | 上传解析（行数/token/活跃时长）、幂等提交、纯追加 diff、**下载路径重写不误伤同前缀仓库**、历史版本、回滚=新提交、占用锁与过期、413/400 边界、`keep_revisions` 裁剪、用量统计 |
| `test_s6_workbuddy_bundle.py` | 打包体往返（jsonl + 旁挂 tool-results + 索引行）、整包重写、本机还原、覆盖前备份、**恶意成员整包拒绝**、超大成员 |
| `test_s7_hooks_mcp.py` | `cc_hook.py` 六类事件（含 Skill / `mcp__` 前缀）、GLM/MiniMax 共用同一 hook、坏输入静默退出、Stop 自动上传、`codex_notify.py`、MCP 工具直调 + **stdio JSON-RPC 协议**（initialize / tools/list / tools/call） |
| `test_s8_client_cli.py` | `list`/`usage`/`pull`/`wb-sync`/`wb-pull`/`wb-hist`/`wb-diff`/`wb-revert` 全命令；**未确认时绝不静默上传** |
| `test_s9_concurrency.py` | 24 并发上报、4 人抢同一会话占用锁、并发下载、并发同内容上传只产生一个版本、上报延迟 p95 |
| `test_s10_migrations.py` | v1 → 002 → 003 → 004 → 005 升级路径与全新安装**逐列对比**、005 为存量数据补 r1 基线、迁移可重跑 |
| `test_s11_mobile.py` | `/m` 登录（token→HttpOnly cookie）、机器分组、时间线取最新 N 条、跨机搜索、退出 |
| `test_s12_lease.py` | 会话执行权：申请 / 409 带持有者信息 / 重复申请＝续租 / **generation 栅栏** / 释放后换代 / 原子交接 / 过期接管 / 归属人 force 夺回 / 非归属人不能抢 / 上传时栅栏告警 |

## 已知标记

历史上曾有 2 个 `xfail`（wb-sync 原件丢项目归属、并发 rev 同号覆盖），均已在
对抗式审查后修复（迁移 006 + advisory lock），相应测试已翻成正常断言。当前没有 xfail。
