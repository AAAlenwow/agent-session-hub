# agent-hub

团队级 agent 会话事件中枢。**不搬 session 文件**——代码和 `~/.claude`、`~/.codex` 都留在每个人本机；只把"谁、在哪个仓库、问了什么、改了哪些文件"以事件形式上报到内网服务器，存 Postgres，再通过 MCP 让任何 agent（Claude Code / Codex / GLM / MiniMax）都能查询团队记忆。

自建、零订阅费、Apache 类内部使用，部署在自己公司服务器上。

## 服务器部署

```bash
# 1) 准备
cp .env.example .env && chmod 600 .env    # 填 POSTGRES_PASSWORD / ADMIN_TOKEN

# 2) 起服务（schema.sql 会自动初始化，含全部表；migrations/ 只给老部署升级用，新部署无需执行）
docker compose up -d --build
curl -s http://127.0.0.1:8421/health       # {"ok":true}

# 3) 用管理员 token 给每个成员发个人 token
curl -s -X POST "http://127.0.0.1:8421/v1/admin/users?name=zhangsan" \
     -H "Authorization: Bearer $ADMIN_TOKEN"
# → {"id":2,"name":"zhangsan","token":"xxx"}  把 token 私发给本人，库里只存哈希
# 注意：ADMIN_TOKEN 本身就是 admin 账号的全权限 token（能管用户、能改任何项目、
# 能回滚任何会话），按最高机密保管，不要发给普通成员。

# 4) 反代（如 Caddy，最小配置；mem.corp.local → 127.0.0.1:8421）
#    mem.corp.local {
#        reverse_proxy 127.0.0.1:8421
#        tls internal        # 内网 CA；有公司证书就换 tls <cert> <key>
#    }
```

## 两层架构：事件层 + 原件层（为什么必须分两层）

| 层 | 存什么 | 能做什么 | 不能做什么 |
|---|---|---|---|
| **事件层**（默认） | 一行摘要、工具名、文件路径、技能调用、token 汇总 | 全文检索、交接报告、技能统计、低成本 | **不能**跨机 `--resume`（不是源文件） |
| **原件层**（可选，v3） | 原始 JSONL 完整对话（gzip 存 PG） | 换机器后 `claude --resume` **接着聊**、精确用量 | 体积大、含完整对话原文，需显式开启 |

原件层回答你的问题：**拿到的是源文件格式**——Claude Code 的 `~/.claude/projects/<slug>/<id>.jsonl`
或 Codex 的 `rollout-*.jsonl`，原样归档、原样下发，只在下载时按目标机器重写路径。

### 开启与使用

```yaml
# server/hub-config.yaml
transcripts:
  enabled: true          # 默认 false；开启后 hooks 在回合结束上传原始 JSONL
  allow_cross_user_pull: true
  claim_ttl_hours: 24
```

成员机器（hooks 配置里加两个环境变量）：

```bash
AGENT_HUB_UPLOAD_TRANSCRIPT=1    # 允许 hooks 归档原件
# 可选：turn_end 事件摘要里 assistant 最终回答的截断长度（默认 800）
# AGENT_HUB_TURN_SUMMARY_CHARS=800
```

**思维链共享（事件层，不需要开原件层）**：每个回合结束时，hook 会从本地
transcript 尾部提取 assistant 的最终回答（进展 / 修复说明 / 结论 / 待办），
截断后作为 `turn_end` 事件的摘要上报（服务端照常脱敏）。这样同事换了机器、
连原件都不用拉，直接 `search_team_memory("圣诞活动")` 就能搜到当时的结论原文。

交接/接手时，新负责人在自己机器上：

```bash
agenthub list                                   # 看有哪些会话：owner / in-out token / 活跃时长 / cwd
agenthub usage --days 30                        # 按人按 agent 的 token 与活跃时长
agenthub pull 7 --into ~/code/api-gateway       # 拉取 + 按本机路径重写 + 安装到 ~/.claude
# → 已安装：~/.claude/projects/-Users-you-code-api-gateway/sess-xxx.jsonl
# → 恢复： cd ~/code/api-gateway && claude --resume sess-xxx
```

### 原件层的关键机制与边界

- **路径重写**：下载时把源机器的绝对路径（含每条记录的 `cwd` 和工具输出里的路径）整体替换为目标路径，
  否则 `claude --resume` 找不到会话目录（slug 由 cwd 决定）。
- **占用锁**：同一会话同一时间只允许一台机器 resume（默认 24h），避免双方上下文互相覆盖。
  成员可占用他人会话（交接场景），但**锁挡得住成员、挡不住归属人**——归属人/管理员随时可夺回，
  也可用 `DELETE /v1/transcripts/{id}/claim` 解铃，不必干等 24h。
- **必须是同一个 CLI 大版本**：跨版本 JSONL 结构可能变，`--resume` 可能拒绝；升级前先备份。
- **Codex 是尽力而为**：文件名/索引格式比 Claude 更不稳定，CLI 会装到 `~/.codex/sessions/<日期>/`，
  若 `codex resume` 不认，用 `codex resume <id>` 或等索引重建。
- **不要用云盘同步 `~/.claude`**：多机同时写会锁冲突 —— 这就是为什么用 pull 而不是实时同步。

## WorkBuddy 会话的同步与还原（`wb-sync` / `wb-pull`）

WorkBuddy 没有 CLI 的 hook 机制，会话是完全本地化的，而且**散在三个地方**，只拷一个文件必然残废：

| 位置 | 内容 | 漏了会怎样 |
|---|---|---|
| `~/.workbuddy/projects/<slug>/<session-id>.jsonl` | 对话正文（毫秒时间戳、usage 在 `message.usage`） | 没有对话 |
| `~/.workbuddy/projects/<slug>/<session-id>/tool-results/*.txt` | 大体积工具输出的旁挂文件 | 正文里引用的大块输出全断 |
| `~/.workbuddy/workbuddy.db`（`sessions` / `session_usage` / `workspaces`） | 侧边栏索引 | **界面里根本不显示这条会话** |

所以这里传的不是裸 jsonl，而是**打包体**：tar.gz 内含 jsonl + rollback + tool-results +
`_agenthub/meta.json`（原机的 DB 行快照）。服务端识别为 `bundle`，体积/行数/用量照常解析。

### 手动同步：先看计划 → 你挑内容 → 确认 → 看 diff

**没有定时任务，也没有后台偷偷上传**。`wb-sync` 不带参数就是交互模式：

```bash
$ agenthub wb-sync
本机会话 42 条（~/.workbuddy），按最近改动排序：

  #  会话       状态     最后改动            行数     变化     体积  标题
  1  397a830c 变更     11-12 09:41      1334      +3     4.3M  清理电脑内存垃圾
  2  7138e131 新增     11-12 08:12        37      +37    138K  每日自动领美团优惠券
  3  b61d9ac6 未变化   11-11 22:03       921        —     2.1M  修复 DeepSeek Harness

选择要同步的编号（如 1,3-5；a=全部变更；回车=全部变更；q=退出）1
将要同步 1 个会话（原始合计 4.3MB，压缩后会更小）：
  · 397a830c  变更  +3 行  清理电脑内存垃圾
提交说明（可选，回车跳过）：清完内存顺手记了结论
确认同步这 1 个会话？[y/N] y
[1/1] 397a830c → r4  +3 行（1 条消息 / 2 次工具 / 1 个文件）
```

选择范围可以先用参数收窄，再进交互挑选：

```bash
agenthub wb-sync --since-days 3          # 只看最近 3 天改过的
agenthub wb-sync --since 2026-11-01      # 只看这个日期之后改过的
agenthub wb-sync --project coze-studio   # 只看某个工作区
agenthub wb-sync --only 397a,7138        # 只要这两个会话
agenthub wb-sync --no-sidecar            # 不打包 tool-results（更小，但大块输出会断）
agenthub wb-sync --all --yes             # 非交互：同步全部变更（CI / 批量用）
```

`未变化` 的会话不会被选中；内容一模一样时服务端返回 `unchanged`，**不会长出空提交**。
判定依据是内容指纹（jsonl 全文 + 旁挂文件名与大小），不是 mtime，所以“打开过但没聊”不会误报。

### 每次同步都是一次提交：可 diff、可回滚

```bash
agenthub wb-hist 1                  # git log
# r4   11-12 09:41  zhangsan  +3  -0   1334 行  1 消息 / 2 工具 / 1 文件 ← HEAD
#      清完内存顺手记了结论
# r3   11-12 09:12  zhangsan  +0  -0   1331 行  回滚自 r1
# r1   11-11 21:40  zhangsan  +1331 -0  1331 行  —

agenthub wb-diff 1 --from 3 --to 4   # 语义差异 + 新增行原文
# r3 → r4   +3 -0 行
#   1 条用户消息 / 2 次工具调用
#   工具：Edit×2
#   文件：/repo/ratelimit.go, /repo/config/prod.yaml
#   · 新增提问：顺手把默认值改成 500 并更新 prod 配置

agenthub wb-revert 1 3 --yes         # 回滚到 r3（生成 r5，历史不丢，可再回滚回来）
agenthub wb-revert 1 3 --yes --apply # 回滚并立即覆盖本机原工作区
```

**版本不会无限增长**：`hub-config.yaml` 里 `transcripts.keep_revisions`（默认 20）控制每个会话保留几个版本，
更早的自动裁掉，但**永远保留 r1 基线**（实测 keep=2 时剩下 r1 / r4 / r5）。

「回滚」是服务端历史操作：如果本机文件还是新内容，下次同步会把它再推上去（这就是 git 的行为）。
要让回滚真正生效到本机，用 `--apply`，或 `wb-pull <id> --revision 3 --into <路径>`。

### 换机器还原

```bash
agenthub wb-pull 7 --into ~/work/2026-10-16-10-42-31              # 最新版
agenthub wb-pull 7 --revision 3 --into ~/work/2026-10-16-10-42-31 # 指定历史版本
# → 落盘 ~/.workbuddy/projects/<新slug>/<session-id>.jsonl + <session-id>/tool-results/
# → 把 sessions / session_usage / workspaces 行写进本机 workbuddy.db（写前自动备份 .db.bak-<ts>）
# → 重启 WorkBuddy，侧边栏出现该会话，点开即原上下文继续
```

**实测结果**（真实会话 1331 行 / 构造会话全链路）：15 个文件全部落位；`cwd` 全量重写为
`/tmp/newplace/2026-10-16-10-42-31`；`sessions` 行 40 列按快照还原；`session_usage` 与 `workspaces` 补齐；
旁挂 `tool-results` 随版本一起回到位；占用冲突 409；重复同步幂等。

### 三个必须知道的点

- **`--dry-run` 先看**：`wb-pull ... --dry-run` 只打印将执行的 SQL 与落盘路径，不写库不写文件。
  想彻底不动索引就加 `--no-db`。
- **写库前先关 WorkBuddy 更稳**：SQLite 有 `busy_timeout` 兜底，但 WorkBuddy 侧边栏有内存缓存，
  写完要重启（或切换工作区）才看得到。
- **时长看「活跃」不看「墙上」**：跨天续接的会话首尾差可能是几百小时（实测 647h→真实 2h14m）。
  活跃时长只累计相邻消息间隔 ≤30 分钟的部分，才代表真实投入。

**没有定时同步**：WorkBuddy 没有 hooks，我们也**故意不做后台轮询**——同步动作完全由人触发，
上传前一定先给计划、再要确认。想批量就跑 `wb-sync --all --yes`，不存在「悄悄上报」的路径。

顺带一句：WorkBuddy 也能挂同一套 MCP（`~/.workbuddy/mcp.json` 加一个 stdio 项，和 Claude Code 同形），
挂上后 WorkBuddy 里的 agent 一样能 `search_team_memory` / `get_project_handover`。

## 项目管理与负责人交接

项目由 hooks 上报时按仓库名**自动创建并关联**（也可手动登记 git remote）。负责人通过 `assign` 指派，
换人时现任自动释放、形成可追溯的时间线；交接报告一键拉出上一任的全部工作档案：

```bash
# 登记项目（可选；hooks 上报会按仓库名自动建）
curl -X POST "https://mem.corp.local/v1/projects" -H "Authorization: Bearer <token>" \
     -H "Content-Type: application/json" \
     -d '{"name":"api-gateway","repo":"git@corp:team/api-gateway.git","description":"网关服务"}'

# 指派负责人（每次 assign 自动释放现任同角色 → 历任记录永久保留）
curl -X POST "https://mem.corp.local/v1/projects/1/assign" -H "Authorization: Bearer <token>" \
     -H "Content-Type: application/json" -d '{"user":"zhangsan"}'

# —— 人员变更，交接时 ——
curl -X POST "https://mem.corp.local/v1/projects/1/assign" -H "Authorization: Bearer <token>" \
     -H "Content-Type: application/json" -d '{"user":"lisi"}'

# 一键取交接报告（支持项目名或数字 id）
curl -H "Authorization: Bearer <token>" https://mem.corp.local/v1/projects/api-gateway/handover
```

交接报告包含：**历任负责人时间线、上一任的历史会话列表、改动热点文件（频次排序）、
使用过的技能包与 MCP（`skill_call` / `mcp_call` 事件聚合）、分享过的结论、近期提问记录**。

新负责人的 agent 侧无需 curl——MCP 工具 `get_project_handover(project)` 直接返回可读的交接报告，
`list_team_projects()` 列出项目与现任负责人。`share_team_memory` 新增 `project` 参数，
交接期间产生的结论可直接归档到项目下。

已有 v1 部署升级（迁移必须按序跑完 002~007，幂等可重复）：

```bash
for f in server/migrations/0*.sql; do
  docker compose exec -T postgres psql -U agenthub -d agenthub -v ON_ERROR_STOP=1 < "$f" || break
done
docker compose up -d --build
```

## 部署专属提示词（关键设计：每个服务器生成的都不一样）

提示词**不是**项目自带的一份通用文案。它由服务器根据 `server/hub-config.yaml`
动态生成（`server/prompt.py` 渲染）——隐私模式、脱敏规则、保留期不同，产出的提示词就不同：

```bash
# 部署后取回「本部署专属」的提示词（需个人 token）
curl -s -H "Authorization: Bearer <token>" https://mem.corp.local/v1/agent-prompt
```

把输出放进团队的 `AGENTS.md` / `CLAUDE.md`（或直接发给任何 agent 阅读），agent 读后即理解：
何时该分享、摘要怎么写、什么内容不能碰。三种隐私模式对应三种提示词：

| 模式 | 提示词告诉 agent 的话 |
|---|---|
| `auto` | hooks 已全量自动上报，你无需主动分享，本 MCP 主要用于查询 |
| `curated`（默认） | 默认零上报；仅当用户明确说"分享"时才调 `share_team_memory` |
| `strict` | 分享前必须先把标题/摘要/文件列表展示给用户，确认后才能调用 |

改 `hub-config.yaml` 后 `docker compose restart hub`，重新 curl 即得新提示词。
agent 也可随时调用 MCP 工具 `team_memory_instructions()` 自己拉取最新说明——
所以团队规范里只需写一句：「使用 team-memory 前先执行 team_memory_instructions 并遵循其规则」。

入库脱敏同样由 `hub-config.yaml` 的 `redaction` 正则控制（对 summary / title / files / payload 生效；
会话原件按原文保存、不做脱敏——脱敏会改坏工具输出导致 resume 后上下文不一致），每个部署配自己的模式。

## 成员机器接入（一次性，3 分钟）

```bash
# 1) 安装 hook / MCP / CLI（CLI 只依赖 Python 标准库，两个文件要放同一目录）
mkdir -p ~/.agent-hub
cp hooks/cc_hook.py hooks/codex_notify.py mcp/mcp_query.py client/agenthub.py client/wb.py ~/.agent-hub/
alias agenthub='python3 ~/.agent-hub/agenthub.py'   # 写进 ~/.zshrc / ~/.bashrc 持久化
agenthub list --help                                 # 验证 CLI 可用（list/usage/pull/wb-*）

# 2) 环境变量
export AGENT_HUB_URL=https://mem.corp.local
export AGENT_HUB_TOKEN=<管理员发给你的个人token>
```

### Claude Code / GLM / MiniMax（都走 CC 协议，同一份配置）

`~/.claude/settings.json` 追加（GLM/MiniMax 换成对应的 AGENT_HUB_AGENT）：

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {"hooks": [{"type": "command",
        "command": "AGENT_HUB_URL=https://mem.corp.local AGENT_HUB_TOKEN=<token> AGENT_HUB_AGENT=claude-code python3 ~/.agent-hub/cc_hook.py"}]}
    ],
    "PostToolUse": [
      {"matcher": "*", "hooks": [{"type": "command",
        "command": "AGENT_HUB_URL=https://mem.corp.local AGENT_HUB_TOKEN=<token> AGENT_HUB_AGENT=claude-code python3 ~/.agent-hub/cc_hook.py"}]}
    ],
    "Stop": [
      {"hooks": [{"type": "command",
        "command": "AGENT_HUB_URL=https://mem.corp.local AGENT_HUB_TOKEN=<token> AGENT_HUB_AGENT=claude-code python3 ~/.agent-hub/cc_hook.py"}]}
    ]
  }
}
```

### Codex

`~/.codex/config.toml`：

```toml
notify = ["python3", "/Users/you/.agent-hub/codex_notify.py"]
```

（Codex 的 notify 环境变量需写在系统环境里：`AGENT_HUB_URL` / `AGENT_HUB_TOKEN`）

### WorkBuddy

没有 hooks，走定时同步（见上文 `wb-sync`）。MCP 同样可挂，编辑 `~/.workbuddy/mcp.json`：

```json
{
  "mcpServers": {
    "team-memory": {
      "type": "stdio",
      "command": "python3",
      "args": ["/Users/you/.agent-hub/mcp_query.py"],
      "env": {"AGENT_HUB_URL": "https://mem.corp.local", "AGENT_HUB_TOKEN": "<token>"}
    }
  }
}
```

### 挂载 MCP 查询（任何一个 agent 里都能问）

```bash
claude mcp add team-memory --env AGENT_HUB_URL=https://mem.corp.local \
    --env AGENT_HUB_TOKEN=<token> -- python3 ~/.agent-hub/mcp_query.py
pip install "mcp[cli]"    # 前置依赖
```

然后就可以在任何 agent 里问："团队里谁改过限流？当时为什么从 100 调到 500？"

### 机器名（会话从哪台机器 / 服务器传上来）

每条会话都记录上传机器。默认取 `hostname`，建议每台机器设一个人类可读的别名：

```bash
export AGENT_HUB_MACHINE="hechao 的 mac"     # 或 "香港服务器" / "测试机-03"
```

- `GET /v1/machines` 汇总：机器名、会话数、涉及人数、最后活跃、涉及项目
- `agenthub list` 输出多一列 `machine`
- MCP 工具 `list_team_machines()`：agent 可自己回答「这些活是哪台机器上的」

### 接入其他生态（dsh-go / DSH / 自研插件）

不写 hook 也能接：`hooks/generic_report.py` 是稳定的上报契约，只需两个环境变量。

```bash
# 方式 1：插件里 spawn 一个子进程（最省事）
echo '{"session":{"ext_id":"dsh-abc","agent":"dsh","project":"geeadmin"},
       "events":[{"kind":"turn_end","summary":"圣诞活动逻辑改成满100送50"}]}' \
  | python3 ~/.agent-hub/generic_report.py

# 方式 2：命令行参数（cron / 一次性上报）
python3 ~/.agent-hub/generic_report.py --session-id dsh-abc --agent dsh \
  --project geeadmin --kind turn_end --summary "修好了限流"
```

DSH / dsh-go 的接法：在 DSH 插件里监听回合结束事件，把 `{ext_id, agent, project, machine}`
和事件数组喂给上面的脚本即可；`AGENT_HUB_MACHINE` 设成手机或服务器的别名，
这样手机端看到的就是「这条会话来自 iPhone / 香港服务器」。

### 手机访问（不装 App）

手机浏览器直接打开（个人 token 登录，30 天有效，只读）：

```
http://<hub 地址>:8421/m
```

页面结构：机器分组 → 该机器的会话列表（带最近一次 turn_end 摘要）→ 点进看完整时间线；
顶部搜索框按关键词跨机检索。也可以 `http://<hub 地址>:8421/m?token=<个人 token>` 直接进
（验证通过后自动换成 HttpOnly cookie 并 303 跳转到去掉 token 的地址，token 不留浏览器历史）。

为什么做成服务端渲染的网页而不是 App：手机跑不了 stdio MCP，网页零安装、iOS/Android
通吃；且只读，避免手机上误触回滚 / 改负责人这类破坏性操作。


## 测试

```bash
# 单测（不需要 Docker）：打包体守卫、路径重写、鉴权、上传限流
python -m venv .venv && . .venv/bin/activate
pip install -r server/requirements.txt pytest
pytest tests -q --ignore=tests/e2e

# 全量场景（真栈）：起独立栈（端口 18421）+ 跑 129 个场景用例
tests/e2e/run_e2e.sh down
PYTHON=.venv/bin/python tests/e2e/run_e2e.sh run
```

场景测试覆盖：鉴权边界、上报与脱敏、项目交接时间线、部署专属提示词、原件层版本/回滚/占用锁、
WorkBuddy 打包体往返、hook 与 MCP（含 stdio 协议）、CLI 全部子命令、并发与升级路径。
细节见 `tests/e2e/README.md`，最近一次结果见 `docs/TEST_REPORT.md`。

- 代码审查标准与流程：`docs/CODE_REVIEW_STANDARD.md`
- 全量代码审查（含未修项）：`docs/CODE_REVIEW_REPORT.md`
- 全量场景测试报告：`docs/TEST_REPORT.md`

## 路线图

| 阶段 | 内容 | 工作量 |
|---|---|---|
| Phase 1（本骨架） | 事件上报 + Postgres 存储 + 检索 API + MCP 查询 | 3–5 天打磨到可用 |
| Phase 2 | Codex 细粒度事件（tail 监听 rollout 文件）；服务端用 MiniMax 生成回合摘要（`turn_end` → 一段结论）；正则脱敏（token/key 模式入库前清洗） | 1 周 |
| Phase 3（可选） | 轻量 Web UI（时间线浏览）；"工作实况"直播（谁在改哪个文件，Tutti 式）；zhparser 中文分词或向量检索 | 1–2 周 |

## 已知边界

- **hook 兼容性是长期成本**：Claude Code hooks 字段、Codex notify 格式随上游版本变化，升级 CLI 后要回归测试。
- **检索起点是 trigram 子串匹配**：中文可用但召回一般，量大后再换 zhparser / 向量，别一开始就上。
- **summary 只存一行**：完整的 assistant 回复没有入库（避免存储爆炸和泄密面扩大）。Phase 2 的服务端摘要解决"结论沉淀"。
- **密钥风险**：UserPromptSubmit 会记录 prompt 原文。`redaction` 正则会在入库前替换
  summary/title/files/payload 里匹配的密钥形态（如 `sk-…`），但这挡不住所有写法——
  上线前仍要提醒全员不要往 prompt 里粘密钥。
- **WorkBuddy 的还原是「补索引」，不是官方 API**：`workbuddy.db` 是 WorkBuddy 内部库，写入前一定自动备份，
  且写死列名会因版本升级而失败 —— 所以实现里按原机快照动态取列，多/少列都不会炸；但**升级 WorkBuddy 大版本后建议先 `--dry-run` 跑一遍**。
- **WorkBuddy 的 token 统计口径**：`input_tokens` 含缓存命中部分（每次请求都把整段 prompt 重发），
  `cache_read_tokens` 是其中的命中量，算钱时别把两者相加。这是与 Claude Code（二者互斥）不同的地方。
