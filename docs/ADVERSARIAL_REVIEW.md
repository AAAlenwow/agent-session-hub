# 对抗式全量审查报告（三路智能体交叉验证）

- 日期：2026-11-12
- 方式：三个独立智能体对抗——`claim-auditor`（声明↔实现逐条对账，只读）、`attacker`（起 18421 真栈实弹攻击）、`fresh-deploy`（起 8421 独立栈，只看文档从零部署）
- 总判定：**Happy path 全部实测通过；对抗/并发场景有 3 个 P0。单人顺序使用可投产，多人并发使用需先修 P0。**

> **✅ 修复状态（2026-11-12 当日完成，v0.3.2）：**
> P0-1/P0-2（并发丢版与 revert 500）、P0-3（NUL 500）、P1-4（claim 锁三漏洞）、
> P1-5（脱敏旁路）、P1-6（原件丢项目归属）、P1-A（打包幂等被时间戳架空）、
> P1-B（重复指派/并发指派失真）、P2-7（hook 非 dict 崩溃）、P2-C（README 两处阻塞）
> 全部已修；两个 xfail 已翻成正常断言并通过；另修复回归中新发现的 claim 查-写竞态。
> 迁移 `006_claims_and_project.sql` 已在"老库 + 既有锁 + 孤儿锁"场景实测回填正确。
> 全量回归：**160 passed / 0 failed / 0 skipped**（31 单测 + 129 场景，含 MCP stdio），
> 删卷从零重跑同样通过。未修项：P2-8、P3-9~12、P3-E 的次要部分（见各节）。

---

## 一、声明场景能不能正确工作？——主链路结论

| 声明场景 | 判定 | 验证方式 |
|---|---|---|
| 从零部署（compose + schema + 发 token） | ✅ 走得通 | fresh-deploy 独立栈实测，迁移 8 次重复执行幂等 |
| hook 事件上报 + 检索 + 脱敏 | ✅ | fresh-deploy 实测 sk-xxx 入库变 [REDACTED-KEY] |
| MCP stdio（7 个工具） | ✅ | 真握手 + 真查询返回数据；提示词工具名契约一致 |
| 跨机 resume（路径重写 + slug + 占用锁） | ✅ 主链路 | attacker 确认 cwd/file_path/summary 全改写，sessionId/parentUuid 链不破坏，同前缀目录不误伤 |
| wb-sync / diff / revert / keep_revisions | ✅ 主链路 | r1 永不裁、裁剪后优雅 404、二次 revert 成功 |
| 负责人交接（assign 释放 + 报告六要素） | ⚠️ 有边界 | 见 P1-B |
| GLM/MiniMax 同 hook | ⚠️ 有前提 | 缺字段时静默塌缩，见 P2-D |
| 提示词声明 vs 实际行为 | ✅ 整体诚实 | 23 条声明：15 ✅ / 8 ⚠️ / 0 ❌ 空头支票 |

## 二、P0（必须修，均有实弹证据）

### P0-1 并发上传同 ext_id：静默丢版 + 版本号撒谎
- 实测：4 路并发不同内容 → 返回 revs=[2,3,3,4]；拿到 r3 的两个请求都收到 200，但 `on conflict do nothing`（transcripts_api.py:170）静默丢弃一份；revert 到 r3 取到的是另一分支内容。
- 根因：transcripts_api.py:105-122 读-算-写无事务无锁。
- 修法：上传事务内 `select rev from transcripts where id=$1 for update`，或对 (user_id, ext_id) 取 advisory lock。

### P0-2 并发 revert/upload → HTTP 500
- 实测：混合并发 6 请求 3 个 500，堆栈在 transcripts_api.py:299（revert 的 INSERT 无 on conflict）→ UniqueViolationError。
- 修法：同 P0-1 的串行化即可连带解决；INSERT 补 on conflict 兜底。

### P0-3 事件层 NUL 字节 → 500，事件直接丢失
- 实测：summary/files/payload 含 `\u0000` → asyncpg CharacterNotInRepertoireError 500。
- 位置：app.py:176-185 无 NUL 过滤（ts 反而有 422 防护，标准不一）。
- 修法：入库前对 str 字段 `replace("\x00", "")`，或 pydantic validator 统一做。

## 三、P1（强烈建议修）

### P1-4 claim 锁三个漏洞（attacker 新发现）
1. 任何人可 claim 任何人的 tid →  owner 自己 409，24h 无解；
2. admin 也无法解铃（无 release/override 端点）；
3. claim 键是全局 (agent, ext_id) 不含 user → 可预注锁批量锁死同事同名会话。
- 修法：claim 校验归属（owner/admin 可锁可解）、锁键加 user_id、加 DELETE /claim。

### P1-5 脱敏旁路：files/payload 不脱敏（attacker 新发现）
- 实测：密钥放 files/payload 原样入库且可被 /v1/search 命中明文；redact 只作用于 summary/title（app.py:173,182）。
- 修法：对 files 逐条、payload 的 json.dumps 同样过 redact()；或文档明确"只脱 summary/title"。

### P1-6 纯上传原件丢失项目归属（确认已知 xfail + 新发现）
- upload 的 `project` query 参数声明了但从未使用（transcripts_api.py:69）；session_id NULL → 项目视图/用量/交接报告全部漏掉，但全局统计照收。WorkBuddy 的 wb-sync 正走这条路。
- 修法：迁移 006 给 transcripts 加 project 字段 + `coalesce(t.project, s.project)`；或上传时自动补 session 行。

### P1-A wb-sync 幂等被架空（claim-auditor 新发现）
- client/wb.py:191/206 把 `packed_at`/`mtime=time.time()` 打进 tar → 每包字节必不同 → 服务端 sha256 恒不同 → `unchanged` 只在字节级重试时命中。state 文件丢失后全量重同步会长出 +0 -0 空版本。
- 修法：meta 时间戳移出包内，或服务端对主 jsonl+旁挂清单算稳定指纹。

### P1-B 交接时间线失真（claim-auditor）
- 重复指派同一人 → 零时长任期且 previous_owner 变成现任本人（报告误导）；并发 assign 无锁 → 可出两个现任。

## 四、P2 / P3（排期即可）

- **P2-7** hook 收到非对象 JSON（`[1,2,3]`）exit 1 崩溃，违反"hook 永远静默成功"：cc_hook.py:127 加 isinstance 检查。
- **P2-8** bundle 内无扩展名旁挂文件不参与路径重写（rewrite_in_bundle 只认 TEXT_EXT）。
- **P2-C** README 阻塞级文档缺陷 ×2（fresh-deploy）：成员接入漏 CLI 安装步骤（agenthub 命令从哪来全文没说）；README:22 引用不存在的 `deploy/Caddyfile`。
- **P2-D** GLM/MiniMax 缺 session_id → 全部会话塌缩成 ext_id="unknown" 一条；缺 transcript_path → 原件层悄悄不工作。README 无提示。
- **P3-9~12** symlink 成员静默丢弃、相对路径 cwd 词级误替换、revert 后统计字段漂移（cache_creation/active_seconds 不回滚）、search 的 q 未转义 `%_` 且无长度上限。
- **P3-E** README:205 升级指引只给 002（应 002~005）；未说明 ADMIN_TOKEN=admin 超级用户；README:335 "Phase 2 加正则脱敏"已过时（脱敏已上线）。

## 五、防住的（值得保留的设计）

压缩炸弹三层守卫、tar 逃逸整包拒绝（先校验全成员再落盘）、ext_id 白名单 + 响应头净化、上传流式限长、token 只存哈希且时序无差异、r1 基线永不裁、路径段边界重写、1MB/万条/控制字符脏数据不崩、hook 对截断/二进制/15 万层嵌套静默 exit 0、挂起的 hub 被 5s 超时硬顶。

## 六、修复优先级建议

1. P0-1+P0-2 一把修（串行化 rev 分配，一处改动覆盖两个）
2. P0-3（NUL 过滤，几行）
3. P1-4（claim 归属 + admin 解铃）+ P1-6（project 归属）—— 直接影响交接核心场景
4. P1-5（脱敏作用域）—— 合规面
5. P2-C README 两处阻塞 —— 十分钟的事，新成员必踩
