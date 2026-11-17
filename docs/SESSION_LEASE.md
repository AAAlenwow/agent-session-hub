# 会话执行权：把「正在干活」从机器上摘下来

> 这份文档回答一个问题：**一个人从头到尾干完一件事之后，为什么整条活就被焊死在那台机器上了？**
> 以及 agent-session-hub 怎么把这个焊点松开。

## 1. 问题的形状

一个团队里最常见的场景不是「没人会做」，而是**只有那个人那台机器上的那个会话知道做到哪了**。

- 他在家里的 MacBook 上跟 agent 磨了两小时，方案改了四版，第三版的取舍理由只存在于那轮对话里。
- 第二天到公司换台式机，`claude --resume` 找不到那个会话——它在另一台机器的 `~/.claude/projects/<slug>/` 下。
- 他去休假，同事接手，只能从头问一遍，或者索要一份「你跟我讲讲当时是怎么想的」的口头交接。
- 两个人同时想接着干，各自拉一份原件，各改各的，最后两份上下文都成了孤本。

这不是「记忆缺失」，是**执行权缺失**：会话是活的，但活在一个不可迁移的位置上。

## 2. 与调度中台的分工（为什么不是竞争）

拿 [agent-autoloop](https://github.com/KKKKhazix/agent-autoloop) 做对照——它把「活」抽象成事件流：
GitHub Issue、定时器、告警进来 → 按规则派给某个 agent → 干完退出 → 拿不准就推卡片问人 → 人答完接着干。
这套抽象解决的是**「活怎么被派出去、怎么被跟踪」**，而且解决得很好。

但它和所有同类工具共享一个前提：**「一台电脑、一条命令」**。规则里写死 `cwd: ~/code/app`，
agent 是本机 CLI，会话续接是本机会话，卡片发给你本人。于是同一个焊点还在：

| | agent-autoloop（调度层） | agent-session-hub（记忆 + 执行权层） |
|---|---|---|
| 关注对象 | **事**：一条事件、一条规则、一轮执行 | **会话**：一份上下文、它的归属、谁能接着写 |
| 时间性 | 事中（正在跑的这一轮） | 跨时间（三周前的结论、昨天的半成品） |
| 部署假设 | 单机，无公网端口 | 一台团队服务器，多机接入 |
| 换机器/换人 | 不在抽象范围内 | 核心场景 |
| 数据 | 本机事件溯源日志 | 服务端 Postgres，跨人可检索 |

一句话：**autoloop 让活跑起来，session-hub 让活换手的时候不丢。**
两者不是替代关系，中间缺的那一层是「这条活的**执行权**现在归谁，怎么交出去」。

## 3. 租约模型

v0.6 把执行权做成服务端的显式状态，而不是「谁先连上算谁的」。

```
      ┌─────────────── hub（一台服务器）───────────────┐
      │  session_leases                                │
      │  claude-code:abc123                            │
      │    holder  alice-mac:51234                     │
      │    mode    exec                                │
      │    generation  4        ← fencing token        │
      │    expires_at  11:20                           │
      └───────▲───────────────────────┬────────────────┘
              │ acquire/renew         │ transfer
      ┌───────┴───────┐       ┌───────▼────────┐
      │  Alice 的 Mac │       │ 云 runner 容器  │
      │  (正在跑)      │       │ (接手继续跑)    │
      └───────────────┘       └────────────────┘
```

四条规则，每条都对应一个真实事故：

1. **同一会话同一时刻只有一个 `exec` 持有者。** 别人来申请拿到 409，附带「谁在跑、在哪台机器、还剩几分钟、generation 多少」——不是一句干巴巴的「Conflict」。
2. **续租而不是硬过期。** 长任务每几分钟续一次，不会在第 24 小时被静默夺走（原来的回放占用锁就是这个毛病）。
3. **generation 是栅栏。** 所有权每变一次 +1。旧进程即使还在跑，带旧 generation 的续租和写入会被拒——这解决的是「双方同时写、上下文互相覆盖」的脑裂，而不是简单地抢一把锁。
4. **transfer 是原子交接。** 持有者或归属人可以一条命令把执行权交给另一台机器/另一个人，不用「你先释放，我再申请」这种有窗口期的两步操作。

与原件层原有的 `resume_claims` 的关系：

- `claim` = 「我要在这台机器装一份来 resume」（回放占用，24h，管的是**文件落地**）
- `lease` = 「我正在跑这条会话」（执行权 + 栅栏，管的是**谁在写**）

两者并存。不开租约的部署行为与 v0.5 完全一致。

## 4. 怎么用

### 接手别人的会话

```bash
# 1) 看现在有谁在跑
agenthub lease status
# SESSION                        HOLDER                     MACHINE              GEN  LEFT   BY
# claude-code:9f2c...            alice-mac:51234            Alice 的 MacBook       4  38m    alice

# 2) 直接申请（对方还持有 → 409，会告诉你等到几点）
agenthub lease acquire 42
# 拿不到执行权：alice（alice-mac:51234）正在 Alice 的 MacBook 上跑这条会话，
# 还剩 38m（generation 4）。
#   · 等它释放后重试，或让对方 `agenthub lease transfer ... --to <你>`
#   · 归属人/管理员可加 --force 强制夺回（旧进程的写入会被 fence）

# 3) 对方主动交接（推荐）
agenthub lease transfer claude-code:9f2c... --to bob-linux:7788 --to-machine build-01

# 4) 拿到之后：拉原件 → 接着跑 → 释放
agenthub pull 42 --into ~/code/api-gateway
cd ~/code/api-gateway && claude --resume 9f2c...
agenthub lease release claude-code:9f2c... --generation 5
```

### agent 自己判断该不该动手

MCP 里加了三个工具，agent 在改别人的东西之前可以自己问：

```
list_active_leases()          → 现在谁在跑什么
acquire_session_lease(42)     → 我要跑它，给我 generation
release_session_lease(key, 5) → 干完了，还回去
```

这也是在 `AGENTS.md` / `CLAUDE.md` 里最值得加的一句：

> 接手一条你没开始的会话之前，先 `list_active_leases()`；要动手就先
> `acquire_session_lease()`，拿不到就停下来问人，别硬写。

### 手机上看

`/m` 的会话卡片和详情页会显示 `▶ 正在跑 · alice · 剩 38m`。
出差时打开手机就知道「这条活现在有人接着，别乱动」。

### 栅栏强度

`hub-config.yaml`：

```yaml
lease:
  ttl_seconds: 3600
  max_ttl_seconds: 86400
  fencing: warn      # off | warn | strict
```

- `off`：只做记录，不拦写入
- `warn`（默认）：上传时若该会话正被别人执行，响应头带 `X-Lease-Warning`（百分号编码），
  并在 body 里提示——**不拒写入，老客户端不受影响**
- `strict`：该会话有活跃 exec 租约时，上传不带 / 带错 `lease_generation` 一律 409

## 5. 三种部署形态

用户问的「让每个人有自己的服务器和云环境」其实是同一套东西的三种规模：

| 形态 | hub 在哪 | 谁在跑 agent | 租约的作用 |
|---|---|---|---|
| **单机** | 自己笔记本，`docker compose up` | 本机的 agent | 防止自己开两个终端改同一份会话 |
| **团队** | 公司内网一台小机器 | 每人自己的机器 | 交接：谁现在有权接着跑；换电脑不丢上下文 |
| **云 runner** | 云上一台小 VPS | 按需拉起的容器 / CI runner | 把「干活的位置」从人的机器上摘下来 |

第三种是真正让对方「不再依赖那台机器」的形态，流程只有五步：

```bash
# runner 启动时（可以是一个 20 行的 systemd 服务 / CI job）
agenthub lease acquire "$SESSION_ID" --note "runner:${RUN_ID}"   # 拿执行权
agenthub pull "$SESSION_ID" --into /work/repo                    # 拉原件 + 重写路径
cd /work/repo && claude -p "$TASK"                               # 跑
agenthub push ...                                                # 回传新版本
agenthub lease release "$SESSION_ID" --generation "$GEN"         # 交还
```

配套只要两样东西：一个带 `AGENT_HUB_TOKEN` 的机器账号，和一个能跑 agent CLI 的容器镜像。
不需要开公网端口，runner 主动连 hub 即可（同一套 Bearer token 鉴权）。

为什么值得这么做：人的机器可以关机、可以换、可以被人借走；执行权在服务端，谁拿着 token
谁就能在任意一台被授权的机器上接着干——**上下文不再和硬件绑定**。

## 6. 边界（写清楚，免得踩）

- **租约不保证顺序。** 它保证「同一时刻只有一个写者」，不保证「先写的一定赢」。
  真正的合并仍然靠 `wb-diff` / `wb-revert` 看内容。
- **fencing 默认是 warn。** 想让旧进程彻底写不进来，必须开 `strict`，并且客户端上传时带
  `lease_generation`。默认不严格是为了不打断已有部署。
- **租约存在服务端，不在 session 文件里。** 所以换机器时不需要搬运租约状态，
  但这也意味着：hub 挂了，谁也申请不到执行权（这是有意的——宁可停下，不要两边同时写）。
- **不解决「agent 进程本身」的编排。** 容器怎么起、镜像怎么建、任务怎么排队，
  这属于调度层（agent-autoloop、CI、K8s 都行）。本层只回答「能不能写」。
- **跨组织不适用。** 模型是「一个团队一个 hub」，租约键是全局的 `agent:ext_id`，
  没有多租户隔离。

## 7. 下一步

- 把 `resume_claims` 收敛成 `lease mode=read`，只留一个概念（v0.7）
- 租约过期前的主动提醒（推到 `/m` 或卡片：「你 10 分钟没续租了，还跑吗」）
- `agenthub run`：把上面五步 runner 流程做成一个子命令，减少上手成本
- runner 侧的最小镜像示例（Dockerfile + systemd unit）
