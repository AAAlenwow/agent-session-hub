#!/usr/bin/env python3
"""agenthub CLI：把同事的会话原件拉到自己机器上，接着原来的上下文继续。

用法：
    export AGENT_HUB_URL=https://mem.corp.local
    export AGENT_HUB_TOKEN=<个人 token>

    agenthub.py list                     # 看有哪些会话原件（含 token / 时长）
    agenthub.py usage --days 30          # 按人 / agent 的 token 与时长统计
    agenthub.py pull 7 --into ~/code/api-gateway     # Claude Code / Codex 回放
    agenthub.py wb-sync                  # 把本机 WorkBuddy 会话同步上去（增量）
    agenthub.py wb-pull 7 --into ~/work/proj         # 在另一台机器还原 WorkBuddy 会话
    # 然后按提示执行： cd ~/code/api-gateway && claude --resume <session-id>

只依赖标准库。
"""
import argparse
import datetime
import json
import os
import pathlib
import shutil
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wb  # WorkBuddy 适配（同目录）

HUB_URL = os.environ.get("AGENT_HUB_URL", "http://127.0.0.1:8421")
HUB_TOKEN = os.environ.get("AGENT_HUB_TOKEN", "")


def machine_name() -> str:
    """上传机器标识：hostname 或 AGENT_HUB_MACHINE 自定义（如 "Alice 的 MacBook"）。"""
    return (os.environ.get("AGENT_HUB_MACHINE") or socket.gethostname() or "unknown")[:120]


def _req(path: str, method: str = "GET", data: bytes = None, headers: dict = None,
         timeout: int = 300):
    hdrs = {"Authorization": "Bearer " + HUB_TOKEN}
    hdrs.update(headers or {})
    req = urllib.request.Request(HUB_URL.rstrip("/") + path, data=data, headers=hdrs, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        # HTTP 头大小写不敏感，统一转小写后取值
        return r.read(), {k.lower(): v for k, v in r.headers.items()}


def _json(path: str, method: str = "GET", data: bytes = None, ctype: str = None):
    headers = {"Content-Type": ctype} if ctype else None
    body, _ = _req(path, method=method, data=data, headers=headers)
    return json.loads(body.decode("utf-8"))


def _fmt_tokens(n: int) -> str:
    return f"{n/1000:.1f}k" if n >= 1000 else str(n)


def _fmt_seconds(s) -> str:
    s = int(s or 0)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s//60}m"
    return f"{s//3600}h{(s%3600)//60:02d}m"


def cmd_list(args):
    rows = _json(f"/v1/transcripts?limit={args.limit}")
    if not rows:
        print("没有原件。确认服务器 hub-config.yaml 里 transcripts.enabled: true，"
              "客户端 Claude Code 设 AGENT_HUB_UPLOAD_TRANSCRIPT=1，"
              "WorkBuddy 用 `agenthub wb-sync`。")
        return
    print(f"{'id':>4}  {'owner':10} {'agent':11} {'machine':14} {'in/out':>13} "
          f"{'活跃':>6} {'形':2} {'ext_id':24} cwd")
    for r in rows:
        tok = f"{_fmt_tokens(r['input_tokens'])}/{_fmt_tokens(r['output_tokens'])}"
        shape = "包" if r.get("bundle") else "行"
        print(f"{r['id']:>4}  {r['owner']:10} {r['agent']:11} "
              f"{(r.get('machine') or '-')[:14]:14} {tok:>13} "
              f"{_fmt_seconds(r.get('active_seconds')):>6} {shape:2} "
              f"{r['ext_id'][:24]:24} {r['cwd']}")


def cmd_usage(args):
    rows = _json(f"/v1/usage?days={args.days}")
    if not rows:
        print(f"最近 {args.days} 天没有原件记录。")
        return
    print(f"{'owner':12} {'agent':12} {'会话':>4} {'输入':>10} {'输出':>10} "
          f"{'缓存读':>10} {'活跃时长':>8}")
    for r in rows:
        print(f"{r['owner']:12} {r['agent']:12} {r['transcripts']:>4} "
              f"{_fmt_tokens(r['input_tokens']):>10} {_fmt_tokens(r['output_tokens']):>10} "
              f"{_fmt_tokens(r['cache_read_tokens']):>10} "
              f"{_fmt_seconds(r.get('active_seconds')):>8}")


def _claude_target(cwd: str, ext_id: str) -> pathlib.Path:
    base = pathlib.Path(os.environ.get("AGENT_HUB_CLAUDE_DIR", "~/.claude")).expanduser()
    slug = cwd.rstrip("/").replace("/", "-")
    return base / "projects" / slug / f"{ext_id}.jsonl"


def _codex_target(ext_id: str) -> pathlib.Path:
    base = pathlib.Path(os.environ.get("AGENT_HUB_CODEX_DIR", "~/.codex")).expanduser()
    now = datetime.datetime.now()
    stamp = now.strftime("%Y-%m-%dT%H-%M-%S")
    return base / "sessions" / now.strftime("%Y") / now.strftime("%m") / now.strftime("%d") / \
        f"rollout-{stamp}-{ext_id}.jsonl"


def cmd_pull(args):
    # 不用 resolve()：要保留 shell 里的原样路径（macOS 上 /tmp 是 /private/tmp 的软链，
    # 解析后会和 Claude Code 记录的 cwd 对不上，导致 --resume 找不到会话）
    into = pathlib.Path(os.path.abspath(os.path.expanduser(args.into)))
    if not into.is_dir():
        sys.exit(f"目标目录不存在：{into}（先 git clone 仓库再来）")

    try:
        _json(f"/v1/transcripts/{args.id}/claim", method="POST")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        sys.exit(f"占用失败：{detail}\n（同一会话不能两台机器同时 resume）")

    qs = urllib.parse.urlencode({"rewrite_from": args.from_cwd or "", "cwd": str(into)})
    raw, headers = _req(f"/v1/transcripts/{args.id}?{qs}")
    ext_id = headers.get("x-ext-id") or args.ext_id
    agent = headers.get("x-agent") or args.agent
    origin_cwd = headers.get("x-origin-cwd") or ""
    if not ext_id:
        sys.exit("响应缺少 X-Ext-Id，服务端版本过旧？")

    if headers.get("x-bundle") == "tar.gz":
        res = wb.install_bundle(raw, str(into), rewrite_from=args.from_cwd or origin_cwd,
                                patch_db=not args.no_db, dry_run=args.dry_run)
        _print_wb_result(res)
        return

    target = _claude_target(str(into), ext_id) if agent != "codex" else _codex_target(ext_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        backup = target.with_suffix(f".jsonl.bak-{int(datetime.datetime.now().timestamp())}")
        shutil.move(str(target), str(backup))
        print(f"已备份本机同名会话 → {backup.name}")

    target.write_bytes(raw)
    print(f"已安装：{target}")
    print(f"来源路径：{origin_cwd or '(未知)'} → 已重写为 {into}")
    if agent == "codex":
        print(f"恢复： cd {into} && codex resume {ext_id}")
    else:
        print(f"恢复： cd {into} && claude --resume {ext_id}")
    print("提示：恢复后该会话归你独占，别让原负责人在同一时间 resume 同一会话。")


def _print_wb_result(res: dict):
    print(f"会话：{res['session_id']}  {res['title'] or '(无标题)'}")
    print(f"落盘：{res['installed_to']}（{len(res['files'])} 个文件）")
    print(f"路径：{res['origin_cwd'] or '(未知)'} → {res['new_cwd']}")
    db = res.get("db") or {}
    if db.get("patched"):
        print(f"索引：已写入 workbuddy.db（{db['session_cols']} 列，备份 {db['backup']}）")
    elif db.get("dry_run"):
        print("索引：dry-run 未写入，将要执行：")
        for s in db.get("sql", []):
            print("  " + s[:160])
    elif db.get("skipped"):
        print(f"索引：{db['skipped']}")
    print("下一步：重启 WorkBuddy（或退出重进该工作区），侧边栏即可看到这条会话，点开即原上下文继续。")


def _ask(prompt: str, default: str = "") -> str:
    try:
        val = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""
    return val or default


def _parse_selection(sel: str, n: int) -> list[int]:
    """"1,3-5" → [1,3,4,5]；"a" → 全部；空 → 空。"""
    sel = (sel or "").strip().lower()
    if sel in ("a", "all", "*"):
        return list(range(1, n + 1))
    out: list[int] = []
    for part in sel.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            if a.isdigit() and b.isdigit():
                out.extend(range(int(a), int(b) + 1))
        elif part.isdigit():
            out.append(int(part))
    return [i for i in dict.fromkeys(out) if 1 <= i <= n]


def _fmt_delta(d: int) -> str:
    return f"+{d}" if d > 0 else ("—" if d == 0 else str(d))


def cmd_wb_sync(args):
    """挑着同步：先列计划 → 你选内容 → 确认 → 上传 → 回显 diff。"""
    state = wb.load_state()
    rows = wb.plan(
        since_days=args.since_days, since_date=args.since, project=args.project,
        only=(args.only.split(",") if args.only else None),
        exclude=(args.exclude.split(",") if args.exclude else None),
        include_sidecar=not args.no_sidecar, state=state,
    )
    if not rows:
        print(f"{wb.WB_HOME}/projects 下没有符合条件的会话（试试 --since-days 30）。")
        return

    changed = [r for r in rows if r["status"] != "未变化"]

    if not args.all and not args.only:
        print(f"本机会话 {len(rows)} 条（{wb.WB_HOME}），按最近改动排序：\n")
        print(f"{'#':>3}  {'会话':8} {'状态':6} {'最后改动':11} {'行数':>6} {'变化':>6} {'体积':>6}  标题")
        for i, r in enumerate(rows[:args.show], 1):
            when = datetime.datetime.fromtimestamp(r["mtime"]).strftime("%m-%d %H:%M")
            dtxt = _fmt_delta(r["delta"])
            if r["status"] == "变更" and r["delta"] == 0:
                dtxt = "旁挂"
            print(f"{i:>3}  {r['short']} {r['status']:6} {when:11} {r['lines']:>6} "
                  f"{dtxt:>6} {r['size']/1024:>5.0f}K  "
                  f"{(r['title'] or '(无标题)')[:26]}")
        if len(rows) > args.show:
            print(f"  … 另有 {len(rows)-args.show} 条未显示（--show 调整，或用 --since-days 收窄）")

        if not sys.stdin.isatty():
            print("\n非交互环境：请加 --all（同步全部变更）或 --only 1,3 指定编号，"
                  "或 --yes 跳过确认。")
            return
        sel = _ask(f"\n选择要同步的编号（如 1,3-5；a=全部变更；回车=全部变更；q=退出）", "a")
        if sel.lower() == "q":
            print("已取消。")
            return
        picked = [rows[i - 1] for i in _parse_selection(sel, len(rows))]
        picked = [r for r in picked if r["status"] != "未变化"]
    else:
        picked = changed

    if not picked:
        print("没有需要同步的变更（全部已是最新）。")
        return

    over = [r for r in picked if r["size"] > args.max_mb * 1024 * 1024]
    for r in over:
        print(f"跳过 {r['short']}：{r['size']/1048576:.1f}MB 超过 --max-mb {args.max_mb}")
    picked = [r for r in picked if r not in over]

    # ---- 确认 ----
    total = sum(r["size"] for r in picked) / 1048576
    print(f"\n将要同步 {len(picked)} 个会话（原始合计 {total:.1f}MB，压缩后会更小）：")
    for r in picked[:20]:
        print(f"  · {r['short']}  {r['status']}  {_fmt_delta(r['delta'])} 行  {(r['title'] or '')[:30]}")
    if len(picked) > 20:
        print(f"  … 另 {len(picked)-20} 个")

    message = args.message
    if not args.yes and sys.stdin.isatty():
        message = _ask("提交说明（可选，回车跳过）：", message)
        ok = _ask(f"确认同步这 {len(picked)} 个会话？[y/N] ", "n")
        if ok.lower() not in ("y", "yes"):
            print("已取消，没有上传任何内容。")
            return
    elif not args.yes:
        print("\n非交互环境且未加 --yes：已取消。确认无误后加 --yes 执行。")
        return

    # ---- 上传 ----
    done = skipped = 0
    up_bytes = 0
    for idx, r in enumerate(picked, 1):
        sess = r["sess"]
        try:
            bundle, meta = wb.build_bundle(sess, include_sidecar=not args.no_sidecar)
        except Exception as e:
            print(f"[{idx}/{len(picked)}] {r['short']} 打包失败：{e}")
            continue

        # 项目归属：取工作区目录名（与 hooks 的「项目=仓库名」同口径）。
        # wb-sync 不走 /v1/events，没有 session 行可反查，必须显式带上，
        # 否则这些原件在按项目 / 用量 / 交接视图里整条消失。
        proj = (meta.get("cwd") or "").rstrip("/").rsplit("/", 1)[-1]
        qs = urllib.parse.urlencode({
            "agent": "workbuddy", "ext_id": r["id"],
            "cwd": meta.get("cwd", ""), "project": proj, "machine": machine_name(),
            "message": message or "",
        })
        try:
            res = _json(f"/v1/transcripts?{qs}", method="POST", data=bundle,
                        ctype="application/gzip")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            sys.exit(f"上传失败 HTTP {e.code}：{detail}")
        except Exception as e:
            sys.exit(f"连不上 {HUB_URL}：{e}")

        if res.get("unchanged"):
            state[r["id"]] = {**state.get(r["id"], {}), "sha": r["sha"],
                              "lines": r["lines"], "rev": res.get("rev", r["rev"])}
            skipped += 1
            print(f"[{idx}/{len(picked)}] {r['short']} 内容未变，未产生新版本")
            continue

        state[r["id"]] = {
            "sha": r["sha"], "lines": r["lines"], "rev": res.get("rev", 0),
            "synced_at": int(datetime.datetime.now().timestamp()), "message": message or "",
        }
        done += 1
        up_bytes += len(bundle)
        d = res.get("diff") or {}
        extra = []
        if d.get("user_messages"):
            extra.append(f"{d['user_messages']} 条消息")
        if d.get("tool_calls"):
            extra.append(f"{d['tool_calls']} 次工具")
        if d.get("files_count"):
            extra.append(f"{d['files_count']} 个文件")
        print(f"[{idx}/{len(picked)}] {r['short']} → r{res.get('rev')}  "
              f"{_fmt_delta(d.get('added', 0))} 行"
              + (f"（{' / '.join(extra)}）" if extra else ""))

    wb.save_state(state)
    print(f"\n同步完成：提交 {done} 个，内容未变 {skipped} 个，"
          f"上传 {up_bytes/1048576:.1f}MB。")
    print("查历史： agenthub wb-hist <id>   看差异： agenthub wb-diff <id>   回滚： agenthub wb-revert <id> <rev>")


def _resolve_tid(args) -> int:
    """允许用 transcript id 或会话 id 前缀定位。"""
    if getattr(args, "id", 0):
        return args.id
    ext = getattr(args, "ext", "")
    if not ext:
        sys.exit("需要 transcript id 或用 --ext <会话id前缀> 定位")
    rows = _json("/v1/transcripts?limit=200")
    hit = [r for r in rows if r["ext_id"].startswith(ext)]
    if not hit:
        sys.exit(f"没有匹配 {ext} 的会话（先 agenthub list 看）")
    if len(hit) > 1:
        sys.exit(f"{ext} 匹配到 {len(hit)} 个，请写得更长一些")
    return hit[0]["id"]


def _fmt_time(v) -> str:
    """服务端给的是带时区 ISO，转成本机时间显示。"""
    try:
        return datetime.datetime.fromisoformat(str(v).replace("Z", "+00:00")) \
            .astimezone().strftime("%m-%d %H:%M")
    except Exception:
        return str(v)[:16].replace("T", " ")


def cmd_wb_hist(args):
    """提交历史（git log）。"""
    tid = _resolve_tid(args)
    data = _json(f"/v1/transcripts/{tid}/revisions")
    revs = data.get("revisions") or []
    if not revs:
        print("没有历史记录（可能是旧版上传的，下一次同步开始记录）。")
        return
    print(f"会话 #{tid}  当前 HEAD = r{data.get('head')}  共 {len(revs)} 个版本\n")
    for r in revs:
        s = r.get("summary") or {}
        bits = []
        if s.get("user_messages"):
            bits.append(f"{s['user_messages']} 消息")
        if s.get("tool_calls"):
            bits.append(f"{s['tool_calls']} 工具")
        if s.get("files_count"):
            bits.append(f"{s['files_count']} 文件")
        if s.get("reverted_from"):
            bits.append(f"回滚自 r{s['reverted_from']}")
        at = _fmt_time(r["created_at"])
        head = " ← HEAD" if r["rev"] == data.get("head") else ""
        print(f"r{r['rev']:<3} {at}  {r['author']:<10} "
              f"+{r['diff_added']:<4} -{r['diff_removed']:<4} "
              f"{r['lines']:>6} 行  {' / '.join(bits) or '—'}{head}")
        print(f"      {r['message'] or '(无说明)'}")


def cmd_wb_diff(args):
    """两个版本之间的差异。"""
    tid = _resolve_tid(args)
    qs = urllib.parse.urlencode({"from_rev": args.from_rev or 0, "to_rev": args.to_rev or 0})
    d = _json(f"/v1/transcripts/{tid}/diff?{qs}")
    s = d.get("summary") or {}
    print(f"r{d['from']} → r{d['to']}   +{d['added']} -{d['removed']} 行")
    bits = []
    if s.get("user_messages"):
        bits.append(f"{s['user_messages']} 条用户消息")
    if s.get("tool_calls"):
        bits.append(f"{s['tool_calls']} 次工具调用")
    if s.get("tools"):
        bits.append("工具：" + ", ".join(f"{k}×{v}" for k, v in s["tools"].items()))
    if s.get("files"):
        bits.append("文件：" + ", ".join(s["files"][:8]))
    for b in bits:
        print("  " + b)
    for p in (s.get("preview") or []):
        print(f"  · 新增提问：{p[:100]}")
    lines = d.get("added_lines") or []
    if lines and not args.quiet:
        print(f"\n新增的 {len(lines)} 行（最多显示 {args.head} 行）：")
        for ln in lines[:args.head]:
            print("  + " + ln[:200])


def cmd_wb_revert(args):
    """回滚到某个历史版本（作为新提交，历史不丢）。"""
    tid = _resolve_tid(args)
    if not args.rev:
        sys.exit("需要版本号，例如：agenthub wb-revert 1 2")
    if not args.yes and sys.stdin.isatty():
        ok = _ask(f"把会话 #{tid} 回滚到 r{args.rev}？会生成一个新版本（r+1），历史保留。[y/N] ", "n")
        if ok.lower() not in ("y", "yes"):
            print("已取消。")
            return
    qs = urllib.parse.urlencode({"revision": args.rev, "message": args.message or ""})
    r = _json(f"/v1/transcripts/{tid}/revert?{qs}", method="POST")
    print(f"已回滚：r{r['reverted_from']} → 新版本 r{r['rev']}（{r['lines']} 行）")

    if args.apply:
        # 把回滚后的内容直接覆盖到本机原工作区（否则下次同步又会把新内容推回去）
        rows = _json("/v1/transcripts?limit=200")
        me = next((x for x in rows if x["id"] == tid), None)
        cwd = (me or {}).get("cwd") or ""
        if not cwd:
            print("取不到原工作区路径，请手动执行下面的取回命令。")
        else:
            print(f"\n把回滚结果写回本机：{cwd}")
            _pull_into(tid, cwd, revision=r["rev"], no_db=args.no_db)
            return
    print(f"以该版本取回本机： agenthub wb-pull {tid} --revision {r['rev']} --into <路径>")


def _pull_into(tid: int, into: str, revision: int = 0, no_db: bool = False,
               dry_run: bool = False, from_cwd: str = ""):
    """统一入口：占用 → 下载（可指定版本）→ 路径重写 → 落盘 → 补索引。"""
    try:
        _json(f"/v1/transcripts/{tid}/claim", method="POST")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        sys.exit(f"占用失败 HTTP {e.code}：{detail}\n（同一会话不能两台机器同时继续）")

    qs = urllib.parse.urlencode({"cwd": into, "revision": revision or 0})
    raw, headers = _req(f"/v1/transcripts/{tid}?{qs}")
    origin = from_cwd or headers.get("x-origin-cwd") or ""
    rev = headers.get("x-rev")

    res = wb.install_bundle(raw, into, rewrite_from=origin, patch_db=not no_db,
                            dry_run=dry_run)
    if rev:
        print(f"版本：r{rev}")
    _print_wb_result(res)
    return res


def cmd_wb_pull(args):
    """在另一台机器还原 WorkBuddy 会话。"""
    into = os.path.abspath(os.path.expanduser(args.into))
    _pull_into(args.id, into, revision=args.revision, no_db=args.no_db,
               dry_run=args.dry_run, from_cwd=args.from_cwd)


def _holder_id() -> str:
    """租约持有者标识：默认「机器名:pid」，同一台机器上多个 agent 进程也能区分。"""
    return f"{machine_name()}:{os.getpid()}"


def _lease_call(path: str, method: str = "GET", payload: dict = None):
    """调租约 API。409/403 的 detail 是结构化的，这里翻成人话，不抛栈。"""
    data = None
    headers = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode()
        headers = {"Content-Type": "application/json"}
    try:
        body, _ = _req(path, method=method, data=data, headers=headers, timeout=30)
        return json.loads(body.decode()), None
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            detail = json.loads(raw).get("detail", raw)
        except Exception:
            detail = raw
        return None, (e.code, detail)


def _lease_left(sec: int) -> str:
    hours, mins = divmod(max(0, int(sec)) // 60, 60)
    return f"{hours}h{mins:02d}m" if hours else f"{mins}m"


def _print_lease_table(rows: list, show_expired: bool = False):
    if not rows:
        print("当前没有活跃租约：没有人正在执行任何会话。")
        return
    print(f"{'SESSION':<30} {'HOLDER':<26} {'MACHINE':<20} {'GEN':>4}  LEFT   BY")
    for r in rows:
        print(f"{r['session_key']:<30} {r['holder_id']:<26} "
              f"{(r.get('machine') or '-'):<20} {r['generation']:>4}  "
              f"{_lease_left(r.get('ttl_left_seconds', 0)):<6} {r.get('holder','')}")


def _print_lease_one(r: dict):
    print(f"  会话        {r['session_key']}")
    print(f"  持有者      {r.get('holder_id')}（{r.get('holder','')}）")
    print(f"  机器        {r.get('machine') or '-'}")
    print(f"  模式        {r.get('mode')}")
    print(f"  generation  {r['generation']}")
    print(f"  到期        {r.get('expires_at','')}（剩 {_lease_left(r.get('ttl_left_seconds',0))}）")
    if r.get("note"):
        print(f"  备注        {r['note']}")


def cmd_lease(args):
    """会话执行权租约。status/acquire/renew/release/transfer，见 docs/SESSION_LEASE.md。"""
    action, sess = args.action, (args.session or "")
    key_path = urllib.parse.quote(sess, safe="")

    if action == "status":
        if sess:
            res, err = _lease_call(f"/v1/leases/{key_path}")
            if err:
                code, detail = err
                if code == 404:
                    print("该会话没有租约记录（= 没人在跑）。")
                    return
                sys.exit(f"查询失败（{code}）：{detail}")
            print("租约：")
            _print_lease_one(res)
            print(f"  状态        {'活跃' if res.get('active') else '已过期'}")
            return
        res, err = _lease_call("/v1/leases?active_only=" + ("false" if args.all else "true"))
        if err:
            sys.exit(f"查询失败（{err[0]}）：{err[1]}")
        _print_lease_table(res.get("leases", []))

    elif action == "acquire":
        if not sess:
            sys.exit("用法：agenthub lease acquire <transcript-id 或 agent:ext_id>")
        payload = {
            "holder_id": args.holder or _holder_id(),
            "machine": machine_name(),
            "mode": args.mode,
            "note": args.note,
            "force": args.force,
        }
        if args.ttl:
            payload["ttl_seconds"] = args.ttl
        if sess.isdigit():
            payload["transcript_id"] = int(sess)
        else:
            payload["session_key"] = sess
        res, err = _lease_call("/v1/leases", method="POST", payload=payload)
        if err:
            code, detail = err
            if code == 409 and isinstance(detail, dict):
                print(f"拿不到执行权：{detail.get('holder')}（{detail.get('holder_id')}）"
                      f"正在 {detail.get('machine') or '某台机器'} 上跑这条会话，"
                      f"还剩 {_lease_left(detail.get('ttl_left_seconds', 0))}（generation "
                      f"{detail.get('generation')}）。")
                print("  · 等它释放后重试，或让对方 `agenthub lease transfer ... --to <你>`")
                print("  · 归属人/管理员可加 --force 强制夺回（旧进程的写入会被 fence）")
                return
            sys.exit(f"申请失败（{code}）：{detail}")
        verb = "已夺取执行权" if res.get("stolen") else "已获得执行权"
        print(f"{verb}：{res['session_key']}  generation={res['generation']}")
        _print_lease_one(res)
        print(f"\n  续租：agenthub lease renew {res['session_key']} --generation {res['generation']}")
        print(f"  释放：agenthub lease release {res['session_key']} --generation {res['generation']}")

    elif action == "renew":
        if not sess or not args.generation:
            sys.exit("用法：agenthub lease renew <agent:ext_id> --generation <N>")
        res, err = _lease_call(f"/v1/leases/{key_path}/renew", method="POST", payload={
            "holder_id": args.holder or _holder_id(),
            "generation": args.generation,
            **({"ttl_seconds": args.ttl} if args.ttl else {}),
        })
        if err:
            code, detail = err
            if code == 409 and isinstance(detail, dict):
                sys.exit(f"续租被拒：{detail.get('reason','执行权已不属于你')}。"
                         f"当前 holder={detail.get('holder_id')} generation={detail.get('generation')}。"
                         f"请停止写入，重新 acquire。")
            sys.exit(f"续租失败（{code}）：{detail}")
        print(f"已续租：{res['session_key']}  剩 {_lease_left(res.get('ttl_left_seconds', 0))}")

    elif action == "release":
        if not sess:
            sys.exit("用法：agenthub lease release <agent:ext_id> [--generation N]")
        q = {"holder_id": args.holder or _holder_id()}
        if args.generation:
            q["generation"] = args.generation
        res, err = _lease_call(
            f"/v1/leases/{key_path}?" + urllib.parse.urlencode(q), method="DELETE")
        if err:
            sys.exit(f"释放失败（{err[0]}）：{err[1]}")
        print(f"已释放：{res['session_key']}")

    elif action == "transfer":
        if not sess or not args.to:
            sys.exit("用法：agenthub lease transfer <agent:ext_id> --to <对方 holder_id>")
        payload = {"to_holder_id": args.to, "to_machine": args.to_machine, "note": args.note}
        if args.ttl:
            payload["ttl_seconds"] = args.ttl
        res, err = _lease_call(f"/v1/leases/{key_path}/transfer", method="POST", payload=payload)
        if err:
            sys.exit(f"交接失败（{err[0]}）：{err[1]}")
        print(f"已交接：{res['session_key']}  {res.get('transferred_from')} → {res['holder_id']}")
        print(f"  新 generation={res['generation']}（旧进程的写入从此被 fence）")
        _print_lease_one(res)


def main():
    p = argparse.ArgumentParser(prog="agenthub", description="团队会话原件回放客户端")
    sub = p.add_subparsers(dest="cmd", required=True)

    l = sub.add_parser("list", help="列出可回放的会话原件")
    l.add_argument("--limit", type=int, default=30)
    l.set_defaults(fn=cmd_list)

    u = sub.add_parser("usage", help="token 与时长用量统计")
    u.add_argument("--days", type=int, default=30)
    u.set_defaults(fn=cmd_usage)

    pl = sub.add_parser("pull", help="拉取会话并安装到本机 CLI 目录")
    pl.add_argument("id", type=int, help="transcript id（见 list）")
    pl.add_argument("--into", required=True, help="本机仓库绝对路径")
    pl.add_argument("--from-cwd", default="", help="覆盖原始 cwd（默认用服务器记录的）")
    pl.add_argument("--agent", default="", help="覆盖 agent 类型（默认用服务器记录的）")
    pl.add_argument("--ext-id", default="", help="覆盖 session id（默认用服务器记录的）")
    pl.add_argument("--no-db", action="store_true", help="只落盘，不写本机索引库")
    pl.add_argument("--dry-run", action="store_true", help="只看计划，不写入")
    pl.set_defaults(fn=cmd_pull)

    ws = sub.add_parser("wb-sync", help="挑着同步本机 WorkBuddy 会话（先看计划再确认）")
    ws.add_argument("--since-days", type=int, default=0, help="只考虑最近 N 天改过的")
    ws.add_argument("--since", default="", help="只考虑该日期之后改过的（YYYY-MM-DD）")
    ws.add_argument("--project", default="", help="按工作区路径/目录名过滤")
    ws.add_argument("--only", default="", help="指定会话 id 前缀，逗号分隔")
    ws.add_argument("--exclude", default="", help="排除会话 id 前缀，逗号分隔")
    ws.add_argument("--show", type=int, default=40, help="列表最多显示几条")
    ws.add_argument("--max-mb", type=int, default=48, help="单个会话体积上限（MB）")
    ws.add_argument("--no-sidecar", action="store_true", help="不打包 tool-results 旁挂文件（更小）")
    ws.add_argument("--message", default="", help="提交说明")
    ws.add_argument("--all", action="store_true", help="非交互：同步全部变更")
    ws.add_argument("--yes", action="store_true", help="跳过确认（仍会显示计划）")
    ws.set_defaults(fn=cmd_wb_sync)

    wp = sub.add_parser("wb-pull", help="在本机还原别人的 WorkBuddy 会话")
    wp.add_argument("id", type=int, help="transcript id（见 list）")
    wp.add_argument("--into", required=True, help="本机工作区绝对路径（对应会话的 cwd）")
    wp.add_argument("--revision", type=int, default=0, help="取某个历史版本（0=最新）")
    wp.add_argument("--from-cwd", default="", help="覆盖原始 cwd")
    wp.add_argument("--no-db", action="store_true", help="只落盘，不写本机索引库")
    wp.add_argument("--dry-run", action="store_true", help="只看计划，不写入")
    wp.set_defaults(fn=cmd_wb_pull)

    wh = sub.add_parser("wb-hist", help="查看某会话的提交历史")
    wh.add_argument("id", type=int, nargs="?", default=0)
    wh.add_argument("--ext", default="", help="用会话 id 前缀定位")
    wh.set_defaults(fn=cmd_wb_hist)

    wd = sub.add_parser("wb-diff", help="查看两个版本之间的差异")
    wd.add_argument("id", type=int, nargs="?", default=0)
    wd.add_argument("--ext", default="", help="用会话 id 前缀定位")
    wd.add_argument("--from", dest="from_rev", type=int, default=0, help="起始版本（默认上一版）")
    wd.add_argument("--to", dest="to_rev", type=int, default=0, help="目标版本（默认 HEAD）")
    wd.add_argument("--head", type=int, default=20, help="显示多少行新增内容")
    wd.add_argument("--quiet", action="store_true", help="只看摘要")
    wd.set_defaults(fn=cmd_wb_diff)

    wr = sub.add_parser("wb-revert", help="回滚到某个历史版本")
    wr.add_argument("id", type=int, nargs="?", default=0)
    wr.add_argument("rev", type=int, nargs="?", default=0, help="目标版本号")
    wr.add_argument("--ext", default="", help="用会话 id 前缀定位")
    wr.add_argument("--message", default="", help="回滚说明")
    wr.add_argument("--apply", action="store_true", help="回滚后立即写回本机原工作区")
    wr.add_argument("--no-db", action="store_true", help="配合 --apply：不写索引库")
    wr.add_argument("--yes", action="store_true", help="跳过确认")
    wr.set_defaults(fn=cmd_wb_revert)

    le = sub.add_parser("lease", help="会话执行权租约：谁现在正在跑这条会话")
    le.add_argument("action", choices=["status", "acquire", "renew", "release", "transfer"])
    le.add_argument("session", nargs="?", default="", help="transcript id 或 agent:ext_id")
    le.add_argument("--generation", type=int, default=0, help="renew/release 时带上你拿到的 generation")
    le.add_argument("--holder", default="", help="holder id（默认 本机名:pid）")
    le.add_argument("--to", default="", help="transfer 的目标 holder id")
    le.add_argument("--to-machine", default="", help="transfer 的目标机器名")
    le.add_argument("--ttl", type=int, default=0, help="租约秒数（默认取服务器配置）")
    le.add_argument("--mode", choices=["exec", "read"], default="exec")
    le.add_argument("--note", default="", help="备注：这条会话在干什么")
    le.add_argument("--force", action="store_true", help="强制夺回（仅归属人/管理员）")
    le.add_argument("--all", action="store_true", help="status 时包含已过期租约")
    le.set_defaults(fn=cmd_lease)

    args = p.parse_args()
    if not HUB_TOKEN:
        sys.exit("请先 export AGENT_HUB_TOKEN=<个人 token>")
    args.fn(args)


if __name__ == "__main__":
    main()
