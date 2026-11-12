# 代码审查标准与流程（团队版 v1.0）

> 适用仓库：agent-hub（Python 3.12 / FastAPI / asyncpg / Postgres 16 / Docker Compose）
> 生效日期：2026-11-12　　负责人：项目负责人（现任）　　复审周期：每季度
>
> 一句话原则：**审查是为了让后面接手的人少踩坑，不是为了证明谁更强。**

---

## 1. 目标与边界

### 1.1 审查要解决的问题
| 问题 | 审查能做到 | 审查做不到 |
|---|---|---|
| 逻辑错误、边界条件漏判 | ✅ 人工读逻辑 | — |
| 安全漏洞（鉴权、注入、路径穿越） | ✅ 有清单可依 | — |
| 数据迁移写坏存量数据 | ✅ 看 SQL 能发现 | — |
| 命名差、风格不一致 | 部分 | 应交给 linter，不要人肉争 |
| 需求本身做错了 | 部分 | 应在需求评审解决 |

### 1.2 明确不做的事
- 不在 review 里讨论**纯风格**问题（缩进、引号、行宽）——全部交给 `ruff format`，谁提谁错。
- 不用 review 做**绩效评估**。审查意见数量与个人能力无因果关系，统计只用于发现系统性缺陷。
- 不做"橡皮图章"。**看不懂就说看不懂**，要求作者补说明，这是合法且被鼓励的审查结论。

---

## 2. 角色与职责

| 角色 | 谁 | 职责 | 不可推卸的事 |
|---|---|---|---|
| 作者 | 提交 PR 的人 | 自检、写清意图、小步提交、响应意见 | 合入前跑通本地检查；对合入后的故障负责 |
| 审查者 | 至少 1 名同模块成员 | 按第 4 节清单逐项过、给出可执行建议 | 24 小时内首次响应（哪怕只是"我今天看"） |
| 负责人 | 项目 owner | 终审、判定分级、决定是否放行 | 对"带已知 P0 上线"签字负责 |
| 安全审查人 | 负责人 + 1 名指定成员 | 触发第 6 节条件时必须到场 | 对鉴权/凭据/文件写入路径签字 |

**规则：作者不能审查自己的 PR，也不能是唯一审查者。** 唯一例外见 8.2 紧急修复。

---

## 3. 分级定义与处理时限

审查意见统一使用 4 级标记，出现在每条评论开头。**没有标记的评论视为提问，不是要求。**

| 标记 | 名称 | 定义 | 处理时限 |
|---|---|---|---|
| 🔴 P0 | Blocker | 安全漏洞、数据丢失/损坏、越权、破坏兼容、必然导致线上故障 | 合入前必须修，无例外 |
| 🟠 P1 | Major | 功能在特定条件下不工作、性能退化明显、缺关键错误处理、缺关键测试 | 合入前修，或开 issue 并写明排期 |
| 🟡 P2 | Minor | 可维护性问题、重复代码、命名误导、日志缺失 | 可以下个 PR 修 |
| 💭 P3 | Nit | 个人偏好、可选优化 | 作者可拒绝，无需说明 |

**P0 的判定不看改动大小。** 改一行引入越权也是 P0。反过来，重构 2000 行但没有 P0 也可以一次合入。

### 3.1 时限（SLA）
- 审查者首次响应：**1 个工作日**
- 作者响应意见：**1 个工作日**（超时视为放弃，PR 关闭重开）
- 同一 PR 往返轮次：**不超过 3 轮**；超 3 轮说明设计没对齐，改为线下对齐后重写

---

## 4. 审查清单

按顺序过。**前 4 项任何一项不过，直接打回，不必看后面。**

### 4.1 正确性（必看）
- [ ] 边界条件：空输入、`None`、0、负数、超长字符串、并发同时写
- [ ] 分页/限制参数有没有**下界**（`limit=-1` 在 Postgres 会直接报错 → 500）
- [ ] 事务边界：多条写操作要么一起成功，要么一起回滚（`ingest` 的多条 insert 属此类）
- [ ] 时间/时区：混用 `now()`、`datetime.now()`、不带时区的 ISO 字符串会静默错
- [ ] 幂等：重放同一个请求会不会产生重复数据
- [ ] 外部输入的日期/数字解析失败时，是**报错**还是**静默降级为默认值**？后者一律视为 bug

### 4.2 安全（必看）
- [ ] **鉴权**：每个写接口都要回答"谁能调"。有 `Depends(auth)` 只代表"登录了"，不代表"有权限"
- [ ] **越权（IDOR）**：拿到别人资源的 id 能不能改/删/回滚？必须校验 `owner` 或 `admin`
- [ ] **SQL**：只能参数化。`f"...{len(args)}"` 这种拼整数位置是安全的，拼字符串不是
- [ ] **路径**：任何来自外部的文件名/成员名，落盘前必须 `os.path.realpath` 后校验仍在目标目录内（见 6.2）
- [ ] **解压**：先校验解压后体积上限，再解压。不能先 `decompress` 再判断大小
- [ ] **请求体上限**：`await request.body()` 之前有没有 max size？没有就是一发 OOM
- [ ] **凭据**：不落日志、不进 URL query、只存哈希、不进 agent 可读的环境变量
- [ ] **响应头**：把外部字符串写进 `headers` / `Content-Disposition` 前要过滤 `\r\n` 和引号

### 4.3 数据（有 schema 变更时必看）
- [ ] 迁移脚本可**重复执行**（`if not exists` / `on conflict`）
- [ ] 对存量数据的影响：`delete`/`update` 前先 `select count(*)` 验证范围
- [ ] 有没有破坏性 DDL 没有回滚方案
- [ ] 大表加索引是否用 `concurrently`（本库量级不大可豁免，但要在 PR 里说明）

### 4.4 性能（必看）
- [ ] 循环里发查询（N+1）——批量用 `executemany` 或 `unnest`
- [ ] **在 `async def` 里做 CPU 密集/阻塞调用**（difflib、大文件 gzip、`tarfile`）。会卡住整个事件循环，不是只卡这一个请求
- [ ] 同一次请求里重复解压/重复压缩同一份数据
- [ ] 存储增长模型：每条记录存全量快照 × 保留 20 版 = 20 倍体积，算过总盘吗

### 4.5 可维护性
- [ ] 同一个规则是否在多处各写一份（例：目录 slug 规则）
- [ ] 死代码、未使用导入（`sha256_legacy`、`fingerprint()`）
- [ ] 函数名与实际行为是否一致（`created_at` 实际存的是"最后上传时间"）
- [ ] 错误是被吞掉还是被处理？`except Exception: pass` 只允许出现在"绝不能阻塞用户"的客户端路径，且必须注释说明

### 4.6 测试
- [ ] 新增/修改的逻辑有没有对应测试
- [ ] **安全相关代码必须有负向测试**（路径穿越、越权、超大输入）
- [ ] 涉及文件的用 `tmp_path`，不碰真实 `~/.workbuddy` 和 `workbuddy.db`

### 4.7 文档与配置
- [ ] 配置项被写进 `hub-config.yaml` 但**没有人读**（如 `retention_days`）= 假承诺，必须实现或删除
- [ ] 提示词/README 里对用户和 agent 的承诺，与实际代码行为一致（例：说"已自动脱敏"，但原件层裸存）
- [ ] 端口、进程模型、服务名在 compose / Caddy / README 三处一致

---

## 5. 本仓库特有的必查项（每次改动都过一遍）

这几条是 agent-hub 的"高危区"，历史 bug 全部出在这里。

### 5.1 路径重写
会话文件里的绝对路径被替换后，必须仍能被 `claude --resume` 找到。
- [ ] 替换必须**按路径段边界**匹配，不能用裸 `str.replace`
      （反例：`/code/api` → `/new/repo` 会把 `/code/api-gateway` 改成 `/new/repo-gateway`）
- [ ] 替换前**不能解析符号链接**（macOS `/tmp` → `/private/tmp`，解析后 slug 对不上）
- [ ] 替换顺序：长路径优先

### 5.2 打包体（tar.gz）
- [ ] 解包成员名必须过 `safe_join`：绝对路径、`..`、符号链接一律拒绝
- [ ] 成员数量与单成员体积都要有上限（防 zip bomb）
- [ ] `_agenthub/meta.json` 里的字段来自远端，写进本地 sqlite 前要过滤列名（只留 `pragma table_info` 求交，已有实现是对的，别退化）

### 5.3 鉴权模型
- [ ] 团队记忆的"读"默认全员可读（设计如此），但"改"必须区分 owner / admin
- [ ] 负责人时间线（`project_owners`）是审计数据，**只有 admin 或现任 owner 能改**
- [ ] 会话回滚、占用锁释放属于写操作，要校验归属

### 5.4 原件层与事件层的边界
- [ ] 事件层（`events`）入库前过 `redact()`
- [ ] 原件层（`transcripts.content_gz`）**没有脱敏**——任何新增的"我们已脱敏"表述都必须先改代码

---

## 6. 安全审查触发条件

改动命中以下任意一条，**必须在 PR 描述里勾选 "Security review required"**，并由安全审查人单独 approve：

1. 处理外部输入到文件系统（解包、落盘、路径拼接）
2. 新增/修改鉴权、token、角色判断
3. 反序列化（`json.loads` on 外部输入后直接当参数用）
4. 新增对外暴露端口、改变信任边界
5. 日志或返回体里出现凭据、内网地址、会话原文
6. 修改 `redact()`、`hub-config.yaml` 的 privacy 段

### 6.1 一个必须记住的反面案例（本仓库已实测复现）
`client/wb.py` 把 tar 成员名直接拼到目标目录：

```python
dst = target_dir / rel          # rel 来自远端打包体，可为 "../../../../.zshrc" 或绝对路径
dst.write_bytes(data)
```

`pathlib` 在右侧是绝对路径时会**直接丢弃左侧**；`..` 也不做任何限制。于是任何成员上传一个构造过的 tar，其他人在 `wb-pull` 时就会把任意内容写到任意位置（`~/.zshrc`、`~/.claude/settings.json`），下次开终端即执行。

正确写法（固定模板，照抄）：

```python
def safe_join(base: pathlib.Path, member: str) -> pathlib.Path:
    base = base.resolve()
    target = (base / member).resolve()
    if target != base and base not in target.parents:
        raise RuntimeError(f"打包体成员越界，已拒绝：{member}")
    return target
```

### 6.2 负面清单（出现即 P0，无需讨论）
- `tarfile.extractall()` 直接用于不可信输入
- `eval` / `exec` / `pickle.loads` 处理外部数据
- SQL 字符串拼接外部输入
- `verify=False`、`InsecureSkipVerify`
- 把 token 打进日志、URL、异常 message

---

## 7. 流程

```
① 本地自检   →  ② 提 PR  →  ③ 自动门禁  →  ④ 人工审查  →  ⑤ 修复往返  →  ⑥ 终审合入  →  ⑦ 观察
```

### ① 本地自检（作者，10 分钟）
```bash
ruff check . && ruff format --check .
python -m py_compile $(git ls-files '*.py')
pytest -q                     # 有测试后必跑
git diff --stat               # 超过 400 行就该拆
```

### ② 提 PR（作者）
- 一个 PR 只做一件事。**超过 400 行改动（不含生成文件/迁移）请拆分**，除非是纯重命名或格式化。
- 标题用 Conventional Commits：`fix(transcripts): 拒绝越界的打包体成员`
- 描述必须包含：**为什么改** / **怎么验证的** / **风险点** / **回滚方式**
- 贴关键日志或截图。不接受"已测试"三个字

### ③ 自动门禁（CI，必须全绿才能进人工审查）
见第 9 节。

### ④ 人工审查（审查者）
- 逐条过第 4、5 节清单，**每条意见带分级标记**
- 一次给完所有意见，不要挤牙膏
- 看不懂的实现直接问，不要假设它是对的
- 结论必须三选一：`Approve` / `Request changes` / `Comment`（后者不得作为合入依据）

### ⑤ 修复往返
- 作者逐条回复：已改（附 commit）/ 不改（说明理由）/ 另开 issue（附链接）
- 不同意可以争，争的判据是**证据**（复现步骤、性能数据、文档），不是职级

### ⑥ 终审合入（负责人）
- 确认 P0 全清、P1 有结论
- 涉及 schema 的确认迁移脚本已验证过（在空库和存量库上各跑一次）
- 用 squash merge，PR 标题即 commit message

### ⑦ 观察
- 上线后 24 小时内，作者负责盯 `docker compose logs -f hub` 与错误率
- 出现回滚的，48 小时内补一份事后说明（不是追责，是补测试）

---

## 8. 例外

### 8.1 文档/注释-only 改动
可 0 审查直接合入，但**必须**有一名成员在 24 小时内事后扫一眼。

### 8.2 紧急修复（线上挂了）
1. 允许作者单人合入，**必须**在 PR 描述第一行写 `HOTFIX`
2. 只改导致故障的那一处，禁止夹带
3. 合入后 24 小时内补审，补审发现的问题按正常流程走
4. 一周内补上回归测试，否则视为未完成

---

## 9. 自动化门禁

### 9.1 工具选型（够用即可，不追新）
| 用途 | 工具 | 失败即阻断 |
|---|---|---|
| Lint + 格式 | `ruff check` / `ruff format --check` | ✅ |
| 类型 | `mypy --ignore-missing-imports`（先只覆盖 server/，逐步推） | ⚠️ 先警告，三个月后阻断 |
| 测试 | `pytest` + `pytest-asyncio` | ✅ |
| 覆盖率 | `coverage`，阈值从 30% 起步，只升不降 | ⚠️ 仅报告 |
| 密钥扫描 | `gitleaks` 或 `detect-secrets` | ✅ |
| 依赖漏洞 | `pip-audit` | ⚠️ 高优漏洞阻断 |
| 镜像扫描 | `trivy image` | ⚠️ CRITICAL 阻断 |
| 静态安全 | `semgrep --config=p/python` | ✅ 仅 `ERROR` 级 |

### 9.2 CI 骨架（可直接落地）
```yaml
name: review-gate
on: [pull_request]
jobs:
  gate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -r agent-hub/server/requirements.txt ruff pytest pytest-asyncio mypy pip-audit
      - run: ruff check agent-hub && ruff format --check agent-hub
      - run: python -m py_compile $(git ls-files '*.py')
      - run: pytest -q --maxfail=1
      - run: pip-audit -r agent-hub/server/requirements.txt || true
      - uses: gitleaks/gitleaks-action@v2
```

### 9.3 本地钩子（pre-commit）
```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.6.9
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format
  - repo: https://github.com/Yelp/detect-secrets
    rev: v1.5.0
    hooks:
      - id: detect-secrets
```

---

## 10. 度量（只用于改进流程，不用于考核人）

| 指标 | 定义 | 健康区间 | 说明 |
|---|---|---|---|
| 首次响应时长 | PR 创建 → 第一条审查意见 | < 1 工作日 | 最大的体验瓶颈 |
| PR 规模 | 改动行数中位数 | < 250 | 超 400 行缺陷发现率断崖下降 |
| 返工轮次 | 平均往返次数 | ≤ 2 | > 3 说明设计没对齐 |
| 缺陷逃逸率 | 合入后 7 天内被 P0/P1 修复的比例 | < 15% | 唯一真正重要的质量指标 |
| 审查覆盖 | 有非作者 approve 的 PR 占比 | 100% | 低于 100% 直接说明流程失效 |

**不要统计**"每人提了多少条意见"——会激励挑刺。

---

## 11. PR 模板

```markdown
## 为什么改
（一句话说清问题；贴 issue 链接）

## 改了什么
- 

## 怎么验证的
- [ ] 本地跑通：`pytest -q`
- [ ] 手工验证：<步骤>
- [ ] 迁移脚本在空库 + 存量库各验证一次（有 schema 变更时）

## 风险与回滚
- 风险：
- 回滚方式：

## 审查自查
- [ ] 已过第 4 节清单
- [ ] 命中第 6 节任一条件 → 已标记 Security review required
- [ ] 无死代码 / 无用导入
- [ ] 新增逻辑有测试（或说明为何不测）
```

## 12. 审查意见模板

```
🔴 P0 **路径穿越：打包体成员名未校验**
client/wb.py:354

**为什么**：`rel` 来自远端 tar 成员名，`target_dir / "../../../../.zshrc"`
会解析到用户 HOME。任何成员上传构造过的 tar，其他人 pull 时即被写入任意文件，
下次开 shell 触发执行。

**复现**：构造含 `../../../../tmp/x` 成员的 tar，调用 install_bundle，
文件落在目标目录之外（已实测）。

**建议**：
base = target_dir.resolve(); dst = (base / rel).resolve()
if base not in dst.parents: raise RuntimeError(...)
```

---

## 13. 落地节奏（两周内跑起来）

| 时间 | 动作 | 产出 |
|---|---|---|
| D0 | 本文档评审通过，全组过一遍 | 达成共识 |
| D1-2 | 补 `ruff` + `pre-commit` + CI 骨架 | 门禁能跑 |
| D3-5 | 修复现有 P0（见审查报告），每个 P0 配一个负向测试 | 可上线 |
| D6-10 | 给核心模块补测试：`transcripts_parse`、`wb.install_bundle`、鉴权 | 覆盖率 ≥ 30% |
| D11-14 | 跑一次全量复审，验证流程是否真的被使用 | 缺陷逃逸率基线 |

> 流程文件没人执行就是废纸。**唯一的成功标准是：下一个 PR 真的按它走了。**
