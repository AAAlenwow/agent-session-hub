# agent-hub 全量代码审查报告

- **审查对象**：`agent-hub/`（server / client / hooks / mcp / migrations / 部署配置）+ `deploy/`
- **审查范围**：10 个 Python 文件（1 858 行）、5 个 SQL、2 套 compose、1 份 Caddyfile、README
- **审查方法**：逐文件通读 → 按《代码审查标准 v1.0》第 4/5/6 节清单核对 → 可疑点**实测复现**（不靠推测）
- **审查日期**：2026-11-12
- **总体结论**：**架构设计好于实现质量。当前状态不建议发到团队内网。**

---

## 0. 修复状态（2026-11-12 更新）

| 项 | 状态 | 改动位置 | 回归锁 |
|---|---|---|---|
| P0-1 打包体路径穿越 → 任意文件写入 | ✅ 已修 | `client/wb.py` 新增 `safe_join()`，落盘前先校验全部成员名 | `tests/test_bundle_safety.py`（3 例） |
| P0-2 gzip bomb / 请求体无上限 | ✅ 已修 | `server/transcripts_parse.py` 新增 `decompress_limited()`、`bundle_guard()`；`transcripts_api._read_body()` 边读边限 | `tests/test_bundle_safety.py`、`tests/test_upload_limits.py`（7 例） |
| P0-3 写接口越权 | ✅ 已修 | `server/app.py` 新增 `_require_project_control()`，覆盖 `create_project` / `assign_role`；`revert` 加归属校验 | `tests/test_authz.py`（8 例） |
| P0-4 原件层未脱敏但宣称已脱敏 | ✅ 已修（改承诺，不改行为） | `server/prompt.py` 明确「原件按原文保存、不做脱敏」；`hub-config.yaml` 加同款说明 | 提示词断言（见下） |
| P1-1 路径重写误伤同前缀仓库 | ✅ 已修 | `transcripts_parse._replace_path()` + `wb.replace_path()`，按路径段边界匹配 | `tests/test_path_rewrite.py`（4 例） |
| P1-2 上报时清空 repo | ✅ 已修 | `_resolve_project` / `create_project` 改 `coalesce(nullif(...))` | `tests/test_authz.py` 间接覆盖 |
| P1-11 strict 模式工具名写错 | ✅ 已修 | `prompt.py`：`share_memory` → `share_team_memory` | 提示词断言 |
| P1-8 输入校验（`limit` 下界、`ext_id` 字符集、响应头净化） | ✅ 已修 | `ext_id` 白名单 + 响应头净化 + `limit` 下界（`max(1, min(limit, N))`） | `tests/test_upload_limits.py`（2 例） |
| 其余 P1 / P2 | ⬜ 未修 | 见第 3、4 节 | — |

测试：`pytest -q` → **27 passed**（`agent-hub/tests/`）。原漏洞的复现脚本现在会抛
`RuntimeError: 打包体成员越界，已拒绝`，目标目录外不再产生文件。

### 0.1 第二轮：全量场景测试发现并修复（详见 `docs/TEST_REPORT.md`）

起真 Docker 栈跑完 122 个场景用例后，又暴露 5 处问题，均已修 + 补了回归用例
（合计 `149 passed / 2 xfailed`）：

| # | 问题 | 严重度 | 修法 |
|---|---|---|---|
| 1 | "空 tar" 压缩炸弹：gzip 全零字节被当成合法空包，成员体积合计 0 → 越过 `bundle_guard`；再套一层 gzip 也能绕（判的是原始请求体而非最终入库内容） | **P0 残余** | 守卫先按解压流限额；上传接口改为对最终入库内容判定 |
| 2 | 重复用户名注册抛 500 | P2 | 捕获 `UniqueViolationError` → 409，名字限 1~64 字符 |
| 3 | 非法 `ts` 抛 500（`timestamptz` 转换失败） | P2 | `EventIn.ts` 加 ISO 8601 校验 → 422 |
| 4 | 提示词只列 5 个工具，MCP 实际 7 个，缺的正是交接用的 `list_team_projects` / `get_project_handover` | P1 | 补齐工具表 + 加"提示词工具名 ⊆ MCP 工具"的契约测试 |
| 5 | `mcp_query.py` 只兼容 `mcp<2`；2.x 已把 `FastMCP` 改名 `MCPServer`，新环境 `pip install "mcp[cli]"` 后**直接 import 失败** | P1 | 双版本兼容导入 + `mcp/requirements.txt`，并用 2.x 实测 stdio 通过 |

仍有两个已定位但未修的行为缺陷，写成 `xfail` 留在 `tests/e2e/` 里（修好后会自己 XPASS）：
WorkBuddy 原件因缺 project 字段在"按项目"视图整条丢失；并发提交未串行化 `rev`，可能同号覆盖。

---

## 1. 结论速览

| 分级 | 数量 | 说明 |
|---|---|---|
| 🔴 P0 Blocker | 4 | 其一已实测可远程写任意文件（→ RCE），必须全部修完才能上线 |
| 🟠 P1 Major | 12 | 功能缺陷 / 性能 / 部署不一致，上线前应修或明确排期 |
| 🟡 P2 Minor | 13 | 可维护性、死代码、命名误导 |
| ✅ 亮点 | 6 | 有几处设计确实做了正确判断，别在重构时改坏 |

**能直接上生产吗**：不能。P0-1 是同组成员之间就能触发的问题（任何成员上传一个构造过的 tar 包，其他人执行 `wb-pull` 就会被写文件），而"跨用户回放"恰好是默认开启的——这正是该产品的核心卖点，也正好是攻击面。

**代码写得差吗**：不差。SQL 全部参数化（我逐条核对过 f-string 拼接，只有整数占位、无注入面），token 只存哈希，回放占用锁、语义 diff 快路径、按 `pragma` 求交的动态列名——这几处是有判断力的设计。问题集中在**边界校验**和**鉴权**上，属于"功能写完但没做威胁建模"的典型状态。

---

## 2. 🔴 P0 —— 上线前必须修复

### P0-1　打包体成员名未校验 → 任意文件写入 → 远程代码执行

**位置**：`client/wb.py:340-360`（`install_bundle` 落盘循环）

```python
for m in members:
    rel = pathlib.Path(m.name)          # ← 来自远端 tar，完全不可信
    plan.append((rel, text))
...
dst = target_dir / rel                  # ← 绝对路径会丢弃左侧；".." 不做任何限制
dst.write_bytes(data)
```

**为什么是 P0**：
`pathlib` 的 `/` 运算符在右侧为绝对路径时**直接丢弃左侧**；`..` 也不做归一化限制。而 `rel` 完全来自上传者的 tar 成员名。攻击链完整成立：

1. 成员 A（任意普通成员，不需要 admin）用 `POST /v1/transcripts` 上传一个含 `../../../../.zshrc` 成员的 tar.gz；
2. 成员 B 或项目接手人执行 `agenthub wb-pull <id> --into ~/work/proj`；
3. `target_dir` = `~/.workbuddy/projects/Users-x-work-proj`，向上四级即 `~`，文件被写入 `~/.zshrc`；
4. B 下次开终端，攻击者代码执行。

`docker-compose.yml` 默认 `allow_cross_user_pull: true`，所以这不是理论路径，是产品的正常使用路径。

**已实测复现**（本机执行，未写入真实 HOME）：

```
installed_to: /tmp/seclab/wb/projects/tmp-seclab-workspace
files: ['.../sess-evil.jsonl', '.../../../../../tmp/seclab/PWNED.txt']
→ 实际落在 /tmp/tmp/seclab/PWNED.txt，内容 "pwned"
```

目标目录**之外**成功写入，确认漏洞成立。

**建议补丁**（`client/wb.py`，加一个入口校验函数并在落盘处调用）：

```python
def _safe_join(base: pathlib.Path, member: str) -> pathlib.Path:
    base = base.resolve()
    target = (base / member).resolve()
    if target != base and base not in target.parents:
        raise RuntimeError(f"打包体成员越界，已拒绝：{member}")
    return target

# 落盘处
dst = _safe_join(target_dir, rel)
```

同时建议：成员名拒绝 `..` 段、拒绝绝对路径、拒绝符号链接成员（当前 `m.isfile()` 已过滤非普通文件，但名称未校验）。

---

### P0-2　先解压后校验体积 → gzip bomb / 单请求打满内存

**位置**：`server/transcripts_api.py:54-60`

```python
body = await request.body()                       # ① 全量进内存，无上限
raw = body if is_bundle(body) else decompress(body)   # ② 先在内存里解开
max_bytes = int(cfg.get("max_mb", 32)) * 1024 * 1024
if len(raw) > max_bytes:                          # ③ 解开之后才判断大小
    raise HTTPException(413, ...)
```

**为什么是 P0**：gzip 压缩比可达 1000:1。一个 30 MB 的请求体可以解出 30 GB，在 ② 处直接 OOM，容器被 OOM killer 干掉——**整个团队的记忆库对一个人来说就是一行 curl 能打挂的**。即使不 OOM，`request.body()` 本身没有任何 max size 保护，Starlette 会把整个流读完。

**建议**：
1. 先按 `Content-Length` 拒绝超限请求，并设置 `max_mb` 上限读取（`await request.stream()` 累计，超限即断开）；
2. 用 `gzip.GzipFile` 分块解压，**累计输出超过 `max_bytes` 立即中止**，不要一次性 `decompress`；
3. tar 分支同样限制成员数量与单成员 `m.size` 之和。

---

### P0-3　写接口缺鉴权：任何成员可改任意项目负责人、回滚任意人的会话

**位置**：
- `server/app.py:241-249` `POST /v1/projects` —— 只要求登录，无角色校验；且 `on conflict (name) do update set repo=..., description=...`，**任何成员可以覆盖任何项目的档案**
- `server/app.py:269-289` `POST /v1/projects/{pid}/assign` —— 只要求登录，**任何成员可以把任意项目指派给自己或任何人**
- `server/transcripts_api.py:230-271` `POST /v1/transcripts/{tid}/revert` —— 只要求登录，**任何成员可以把别人的会话 HEAD 回滚到任意历史版本**（且会改写 `transcripts.sha256/content_gz`）

**为什么是 P0**：
`project_owners`（`assigned_at` / `released_at`）是**审计数据**，整个"负责人交接"功能的可信度就建立在这张表上。任何人可以改它，等于交接报告可以被任意伪造——这比"功能有 bug"更严重，是产品前提被破坏。

`revert` 更直接：它把受害者的会话内容整体替换掉，且**不删历史**，所以受害者很难察觉（下一次同步还会把本机内容推回去，看起来像"我自己又改回来了"）。

**建议**：抽出统一的授权函数，所有写路径走它。

```python
def _require_project_admin(row, user):
    if not user["is_admin"] and (not row or row["owner_id"] != user["id"]):
        raise HTTPException(403, "需要管理员或现任负责人")

# assign / create_project / revert 三处调用
```

`revert` 至少要满足 `row["user_id"] == user["id"] or user["is_admin"]`；跨人回滚应当走显式的高权限接口并留审计记录。

---

### P0-4　原件层完全未脱敏，但提示词对 agent 宣称"服务器端已配置自动脱敏"

**位置**：
- `server/transcripts_api.py:119`、`136`：`gzip.compress(raw)` 直接存原文，**不经过 `redact()`**
- `server/prompt.py:117-118`：

```python
if privacy.get("redaction"):
    a("- 服务器端已配置自动脱敏（匹配模式入库前会被替换为 [REDACTED]）…")
```

**为什么是 P0**：
这是对使用者的**错误承诺**。管理员按 README 开启 `transcripts.enabled: true`，以为配了 `redaction` 就安全了，实际上事件层脱敏、**原件层裸存**——而原件层恰恰是完整对话原文，包含 hook 里所有工具输出、`.env` 被读出的内容、内部地址。提示词还把这个错误承诺喂给了 agent，让 agent 更放心地把敏感内容写进会话。

在"私有化部署"这个产品语境下，这类不一致比一个崩溃更难接受——它让人做出错误的保密判断。

**建议**（二选一，不要停在中间态）：
- **A（推荐）**：原件层保持不脱敏（脱敏会破坏 resume 的可读性），但把承诺改准确：
  ```
  - 事件层（摘要/标题）入库前已按本部署规则脱敏；会话原件（如本部署开启）保存原文、不做脱敏。
  ```
  并在 `hub-config.yaml` 的 `transcripts` 段写清 `store_raw: true` 的后果。
- **B**：上传时对原文做脱敏（会改坏 resume 的工具输出，需评估），并在 UI/文档上标注"原件已脱敏，可能与原会话不完全一致"。

---

## 3. 🟠 P1 —— 上线前应修，或开 issue 明确排期

### P1-1　路径重写用裸 `str.replace`，前缀相同的仓库被改坏，resume 直接失效

**位置**：`server/transcripts_parse.py:321-328`（`rewrite_paths`）、`:84-109`（`rewrite_in_bundle`）、`client/wb.py:332-338`

**实测**：

```
输入: {"cwd":"/code/api-gateway","p":"/code/api/main.py"}
重写: [("/code/api", "/new/repo")]
输出: {"cwd":"/new/repo-gateway","p":"/new/repo/main.py"}
```

`api-gateway` 被改成 `repo-gateway`——`cwd` 与真实目录不再一致，`claude --resume` 会报找不到会话，而用户看到的现象是"会话拉下来了但打不开"，极难定位。

**建议**：按路径段边界替换，长路径优先：

```python
import re
def _sub_path(text: str, old: str, new: str) -> str:
    pattern = re.escape(old.rstrip("/")) + r"(?![\w.\-])"   # 后面不能再接路径段字符
    return re.sub(pattern, new.rstrip("/"), text)
```

---

### P1-2　每次事件上报都会把项目的 `repo` 清空

**位置**：`server/app.py:111-117`

```python
"insert into projects (name, repo) values ($1, $2) "
"on conflict (name) do update set repo = excluded.repo returning id"
```

`cc_hook` 只在 `AGENT_HUB_REPO` 环境变量存在时才带 `repo`（`hooks/cc_hook.py:143`），绝大多数人不会设。于是每次上报都用空串覆盖掉之前登记的 git remote，`projects.repo` 长期是空——而 `_resolve_project` 又靠 `repo` 做二次匹配，形成"写了就丢"的循环。

**建议**：`set repo = coalesce(nullif(excluded.repo, ''), projects.repo)`。`create_project` 同理（那里可能是有意覆盖，需在 PR 里说清）。

---

### P1-3　WorkBuddy 会话永远挂不上 session 行 → `?project=` 过滤静默漏掉全部 WorkBuddy 数据

**位置**：`server/transcripts_api.py:66-72`（只 `select` 已有 session，**不创建**）、`:291-293`、`:443-445`（按 `s.project` 过滤）

WorkBuddy 没有 hooks，`wb-sync` 只调 `POST /v1/transcripts`，从不发 `/v1/events`，所以 `sessions` 表里没有对应行 → `transcripts.session_id` 为 NULL → 所有 `left join sessions` 后按 `s.project` 过滤的查询（`list_transcripts?project=`、`usage?project=`）**一行都不会返回**。

现象：管理员按项目看用量，WorkBuddy 的 token 消耗全部不可见；而且不报错，只是"数字偏小"。

**建议**：上传时若 `sessions` 无行，按 `ext_id` 创建一条并挂上 `project_id`（从 `cwd` 的仓库名或请求参数推导）。或统一改成 `transcripts.project_id` 直接落库，别依赖 session 关联。

---

### P1-4　每回合整份重新上传 → O(n²) 流量与写放大

**位置**：`hooks/cc_hook.py:152-156`

```python
if UPLOAD_TRANSCRIPT and tp and event["kind"] in ("turn_end", "session_end"):
    spawn_upload(tp, ...)
```

`Stop` hook 每回合触发一次，每次都把**整个** JSONL 重新传一遍。一个 100 回合、最终 8 MB 的会话，累计上传约 400 MB。不仅浪费带宽，服务端每回合都要 `parse_transcript` + `diff_transcripts`（difflib）+ 存一份全量 revision。

**建议**：只在 `session_end` 上传；或改为"仅当文件增长超过阈值（如 2 MB）或距上次上传 > 30 分钟"才上传；最好支持 `Content-Range` 式的尾段追加。

---

### P1-5　同一份内容在一次上传里被 gzip 两次、并且每个版本都存全量快照

**位置**：`server/transcripts_api.py:119` 与 `:136` 各 `gzip.compress(raw)` 一次；`transcript_revisions.content_gz` 存全量

**双重成本**：
1. CPU：同一份 30 MB 数据压两遍；
2. 存储：`keep_revisions: 20` × 30 MB × N 个会话。100 个活跃会话就是 60 GB，而其中 95% 的内容是重复的。

**建议**：压一次，两个字段复用同一个 `bytes`；版本层改存**增量**（只存 `added_lines`，或存 zstd 差异），或把 `keep_revisions` 降到 5 并对单会话设总配额。至少要在 `hub-config.yaml` 里把"这里会占多少盘"写出来。

---

### P1-6　`async def` 里同步跑 difflib / tarfile 解包 → 单请求卡死整个服务

**位置**：`server/transcripts_api.py:81`（上传时 diff）、`:226`（diff 接口）、`:57-62`（tar 解析）

`difflib.SequenceMatcher` 在最坏情况下是 O(n²)；10 000 行的会话走非快路径时，实测可以跑到秒级。因为这段代码在 `async def` 里同步执行，它会**阻塞整个 uvicorn 事件循环**——所有其他成员的请求（包括 hook 上报）一起被卡住。

**建议**：`await asyncio.get_running_loop().run_in_executor(None, diff_transcripts, a, b)`，并对参与 diff 的行数设上限（超过就只返回统计、不返回 `added_lines`）。

---

### P1-7　项目名取 `basename(cwd)`，同名目录的项目被合并

**位置**：`hooks/cc_hook.py:141`、`hooks/codex_notify.py:52`

`project = os.path.basename(cwd)`。团队里只要有两个 `api`、两个 `web`（`~/work/api` 与 `~/side/api`），它们的会话就被并到同一个项目里，交接报告会把两个人的工作混在一起。这对"项目负责人交接"是致命的——报告内容不可信。

**建议**：优先用 `git remote get-url origin`（或 `git rev-parse --show-toplevel` 的目录名 + 仓库根路径哈希），退化时才用 basename。

---

### P1-8　输入校验缺失：负数 limit 直接 500，`ext_id` 未过滤就进响应头

**位置**：`server/app.py:169/209/395`、`server/transcripts_api.py:294`（`min(limit, 200)` 没有下界）、`:360-386`（`ext_id` 拼进 `X-Ext-Id` 与 `Content-Disposition`）

- `limit=-1` → Postgres `LIMIT -1` 报错 → 500，日志里是一堆无意义的栈。
- `ext_id` 来自查询参数，未做字符集限制，直接写进响应头。含 `\r\n` 时，是否被拒取决于 HTTP 后端（h11 会抛错、httptools 直接写出）——**行为不确定本身就是缺陷**；含引号会破坏 `Content-Disposition`。

**建议**：`limit` 用 `Query(50, ge=1, le=200)`；`ext_id` 用 `^[A-Za-z0-9._-]{1,64}$` 校验，不合法直接 400。

---

### P1-9　`retention_days` 只是一句声明，没有任何清理任务

**位置**：`server/hub-config.yaml:26`、`server/prompt.py:121-122`

提示词会对 agent 说"本部署记录保留 180 天"，但代码里**没有任何删除逻辑**（全仓 grep 只有这三处引用）。对使用者这是隐私承诺，对合规是虚假陈述。

**建议**：要么加一个每天跑的清理任务（`delete from events where ts < now() - interval`、`delete from transcript_revisions ...`），要么把提示词改成"当前不做自动清理，由管理员手动执行"，并在 README 给出清理 SQL。

---

### P1-10　数据库备份在打开连接之后才做，WAL 未落盘 → 备份可能缺最新数据

**位置**：`client/wb.py:396` 先 `connect_db(readonly=False)`，`:433-434` 才 `shutil.copy2(db, backup)`

sqlite 默认 WAL 模式下，已提交但未 checkpoint 的数据在 `-wal` 文件里。只拷主库文件会**丢掉最近的事务**——恰好是刚刚 pull 进来的那条会话。用户以为有备份，实际备份里没有。

**建议**：用 sqlite 官方的 online backup API（`conn.backup(dst_conn)`），或先 `conn.execute("pragma wal_checkpoint(FULL)")` 再拷（连同 `-wal`、`-shm` 一起）。

---

### P1-11　`strict` 模式提示词引用了不存在的工具名

**位置**：`server/prompt.py:32`

```
③ 用户明确同意后再调用 `share_memory` 对应的分享工具。
```

实际工具名是 `share_team_memory`（`mcp/mcp_query.py:114`）。`strict` 恰恰是涉密团队会选的模式，agent 在关键路径上找不到工具，要么报错、要么自己编一个调用。

**建议**：改为 `share_team_memory`；更好的做法是在 `prompt.py` 里从工具清单常量渲染，避免手写字面量再次漂移。

---

### P1-12　`deploy/` 与 `agent-hub/` 是两套互相矛盾的部署模型

| 位置 | 声明 | 实际 |
|---|---|---|
| `deploy/Caddyfile:8` | `reverse_proxy 127.0.0.1:8420` | hub 监听 **8421**（`docker-compose.yml`） |
| `deploy/.env.example:9` | "团队记忆服务（ai-memory 宿主机 systemd 方式）AI_MEMORY_PORT=8420" | 仓库里没有 ai-memory，也没有 systemd unit |
| `deploy/docker-compose.yml` | 只有 litellm + postgres | **没有 hub 服务** |

按 `deploy/` 部署，`mem.corp.local` 会 502。新人第一次部署必然卡在这里。

**建议**：把 `deploy/` 明确降级为"模型网关脚手架（可选）"，或直接合并进 `agent-hub/docker-compose.yml` 成一个 compose（含 Caddy），端口统一。**当前别留两套。**

---

### P1-13　凭据通过环境变量注入每个 agent 进程

**位置**：`hooks/cc_hook.py:17`、`mcp/mcp_query.py:19`、`client/agenthub.py:32`

`AGENT_HUB_TOKEN` 在 agent 进程的环境里，意味着 agent 执行的**任何** Bash 命令都能 `echo $AGENT_HUB_TOKEN` 读走。这是团队内网，风险不高，但配合"原件层可跨用户拉取"，一个被污染的 prompt 就能拿到全员 token，进而拉走全部会话原文。

**建议**：token 从 `~/.agenthub/token`（`chmod 600`）读，用完即弃；服务端加 token 轮换接口和 `last_used_at`。

---

### P1-14　没有任何测试，也没有 CI

全仓 `test_*.py` 数量为 **0**。审查过程中发现的所有 P0/P1，全部只能靠人工读 + 手工构造攻击样本验证。

**建议**：先只补 3 个文件的关键测试，投入产出比最高：
1. `tests/test_bundle_safety.py`：路径穿越、绝对路径、超大成员、损坏 gzip（P0-1、P0-2 的回归锁）
2. `tests/test_authz.py`：非 owner 改 assign / revert 必须 403（P0-3）
3. `tests/test_rewrite.py`：前缀相同的仓库不被误伤（P1-1）

---

## 4. 🟡 P2 —— 可以下个 PR 处理

| # | 位置 | 问题 |
|---|---|---|
| 1 | `server/app.py:72-73` | `sha256_legacy()` 死代码；`hashlib`、`Header` 导入随之无用 |
| 2 | `client/wb.py:296-302` | `fingerprint()` 已被 `content_sha()` 取代，死代码 |
| 3 | `client/wb.py:340` | 局部变量 `plan` 遮蔽模块级 `plan()` 函数，读代码的人会误解 |
| 4 | `client/wb.py:112-122, 205-212` | `read_db_rows` / `titles_by_id` 在异常路径上不关连接 |
| 5 | `client/wb.py:291-293` | `save_state` 非原子写，进程被杀会留下半截 JSON → 下次全量重传 |
| 6 | `client/wb.py:225-229` | `--since` 日期非法时静默 `cutoff=0`（= 全量），危险默认值应报错 |
| 7 | `client/agenthub.py:184-193` | `_parse_selection("3-1")` 返回空列表，用户以为选了却没选，无提示 |
| 8 | `server/app.py:150` | `e.ts` 直接进 `$2::timestamptz`，非法值导致**整批**上报 500（同文件 `_ts()` 有正确做法，未复用） |
| 9 | `server/app.py:192-211` | `search` 未转义 `%`/`_`，传 `%` 即全表扫描；`e.files::text ilike` 使索引失效 |
| 10 | `server/transcripts_api.py:245` | `revert` 里 `parse_transcript(raw, "workbuddy")` 硬编码，非 WorkBuddy 会话统计错误 |
| 11 | `server/transcripts_api.py:114` | `created_at = now()` 让 `created_at` 实际含义变成"最后上传时间"，列名误导所有后续读者 |
| 12 | `server/transcripts_parse.py:429-438`、`client/wb.py:37`、`client/agenthub.py:97` | 三处各写一份 slug 规则，其中一处带前导 `-`、一处不带，极易漂移 |
| 13 | `server/app.py:82-83` | `files: list[str] = []` / `payload: dict = {}` 可变默认值（pydantic v2 会深拷贝，不致命，但应改 `default_factory`） |

另外：`sessions.project`（文本）与 `sessions.project_id`（外键）是**两个真相**，`/v1/skills` 用前者、交接报告用后者。建议逐步废弃文本字段，只留外键。

---

## 5. ✅ 值得保留的做法（重构时别改坏）

1. **SQL 全参数化**。我逐条检查了所有 f-string 拼接，只有 `f"limit ${len(args)}"` 这类整数占位，没有一处把用户输入拼进 SQL。这在手写 SQL 的项目里很少见，做得对。
2. **token 只存 `sha256`**，明文只在创建时返回一次（`app.py:220-225`）。方向正确。
3. **原件层按 `(user_id, ext_id)` 覆盖 + 内容 sha 判等实现幂等**（`transcripts_api.py:75-78`）。避免了"每次点同步都长出一个空提交"，这是想过实际使用场景的。
4. **回放占用锁**（`resume_claims` + 409）。跨机 resume 时两人同时打开会互相踩上下文，这是真实痛点，锁的设计位置是对的。同类产品普遍没做。
5. **语义 diff 的"前缀快路径"**（`transcripts_parse.py:410-411`）。识别出"会话文件基本只追加"这个领域特征，用 O(n) 判断绕开 difflib，是有洞察的优化。
6. **客户端失败一律静默 + `dry_run` + 写前备份 + 按 `pragma` 求交动态列名**。hook 不阻塞开发者、跨 WorkBuddy 版本升级不炸，这几条都守住了。

---

## 6. 修复路线图

| 阶段 | 动作 | 完成标准 |
|---|---|---|
| **今天** | 修 P0-1（`safe_join`）、P0-2（限制请求体 + 分块解压） | 手工构造恶意 tar 与 gzip bomb 均被拒绝，写两个回归测试 |
| **本周** | 修 P0-3（授权函数覆盖 assign / create_project / revert）、P0-4（改准提示词或实现脱敏） | 非 owner 调三个接口均 403；提示词与代码行为一致 |
| **上线前** | P1-1（路径边界）、P1-2（repo 覆盖）、P1-3（session 挂载）、P1-12（统一 compose 端口） | 跨机 resume 在两条不同前缀的仓库上都成功；`?project=` 能查到 WorkBuddy 数据 |
| **两周内** | P1-5/6/9（存储与阻塞、清理任务）、P1-4（上传频率）、补 3 个测试文件 + CI 门禁 | CI 能拦住下一次回归 |
| **可选** | 全部 P2 | 顺手清理 |

---

## 7. 本次审查未覆盖

- **运行时行为**：未在 Docker 真栈上跑端到端（本次为静态审查 + 定点复现），建议修复后跑一轮 `docker compose up` 的冒烟：发 token → 上报事件 → 上传 → 跨机 pull → resume。
- **前端/UI**：仓库中暂无 Web UI，未涉及。
- **`deploy/litellm-config.yaml`**：只做了配置语法与密钥引用方式检查，未验证模型 endpoint 可用性。
- **性能压测**：P1-5/6 的结论基于代码路径分析，未做真实压测。建议上线前用 50 个 10 MB 会话灌一次，看 `pg_database_size` 的增速。

---

> 审查意见只针对代码，不针对人。上面每一条都能给出复现步骤或代码依据；如果哪一条判断错了，请拿着反例来找我，我改。
