#!/usr/bin/env python3
"""把 dsh-team 的编排 / 审批 / 审计事件转发到 agent-session-hub（上行通道）。

为什么需要它：dsh-team 的 sidecar 把任务流转、成员动作、审批留档、文件改动哈希
都落在本机 `~/.dsh/team/team.db`，但这些记录只在**这一台机器的面板里**能看。
推到 agent-session-hub 之后，同一份记录就变成团队可检索的事件——手机上（`/m`）能看，
别的机器上的 agent 也能通过 MCP 搜到「谁在哪台机器改了哪个文件、谁批的」。

设计取舍：
- **只读打开 sqlite**（`mode=ro`），绝不写 team.db，不和 sidecar 抢锁。
- **水位去重**：event/audit 用自增 id，approval/member_change 用 id 集合，
  状态存 `~/.dsh/team/.hub-bridge.json`。重复跑不会重复推。
- **失败静默**：hub 不通、token 错、表结构变了，一律 exit 0（可加 --verbose 看原因）。
  这个脚本可能被 cron 每 30 秒拉起，绝不能因为 hub 挂了就往系统日志里刷错误。
- 依赖只有标准库，单文件分发，和 cc_hook / generic_report 保持一致。

用法：
    # 推一次（cron 最合适）
    python3 team_bridge.py --once

    # 常驻轮询（前台跑，Ctrl-C 停）
    python3 team_bridge.py --watch --interval 30

    # 只看会推什么，不发送
    python3 team_bridge.py --once --dry-run --verbose

环境变量：
    AGENT_HUB_URL        默认 http://127.0.0.1:8421
    AGENT_HUB_TOKEN      必填，个人 token
    AGENT_HUB_MACHINE    机器别名，默认 hostname（如 "Alice 的 MacBook"）
    AGENT_HUB_TEAM_DB    默认 ~/.dsh/team/team.db
    AGENT_HUB_TEAM_WATERMARK  默认 ~/.dsh/team/.hub-bridge.json
"""

import argparse
import json
import os
import socket
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HUB_URL = os.environ.get("AGENT_HUB_URL", "http://127.0.0.1:8421").rstrip("/")
HUB_TOKEN = os.environ.get("AGENT_HUB_TOKEN", "")
TEAM_DB = Path(os.environ.get("AGENT_HUB_TEAM_DB", "~/.dsh/team/team.db")).expanduser()
WATERMARK = Path(
    os.environ.get("AGENT_HUB_TEAM_WATERMARK", "~/.dsh/team/.hub-bridge.json")
).expanduser()
TIMEOUT = 8
MAX_SUMMARY = 700
SEEN_LIMIT = 2000

VERBOSE = False


def log(msg):
    if VERBOSE:
        print(msg, file=sys.stderr)


def machine_name():
    """上传机器标识：AGENT_HUB_MACHINE 优先，否则 hostname。"""
    return (os.environ.get("AGENT_HUB_MACHINE") or socket.gethostname() or "unknown")[:120]


def clip(s, n=MAX_SUMMARY):
    return (s or "").replace("\n", " ").replace("\r", " ").strip()[:n]


def iso_now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ── 水位状态 ────────────────────────────────────────────────────────────────


def load_state():
    try:
        return json.loads(WATERMARK.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state):
    try:
        WATERMARK.parent.mkdir(parents=True, exist_ok=True)
        tmp = WATERMARK.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(WATERMARK)
    except OSError as exc:
        log(f"水位写入失败（下次会重复推送，不影响正确性）: {exc}")


def merge_seen(old, new):
    """approval / member_change 是 text 主键，用集合去重并限制长度。"""
    merged = list(dict.fromkeys(list(old) + list(new)))
    return merged[-SEEN_LIMIT:]


# ── 数据库读取（只读） ────────────────────────────────────────────────────────


def connect(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)


def fetch_teams(db):
    try:
        return db.execute("select id, name from team").fetchall()
    except sqlite3.Error:
        return []


def fetch_members(db, team_id):
    try:
        return {row[0]: (row[1] or "") for row in db.execute(
            "select id, name from member where team_id = ?", (team_id,))}
    except sqlite3.Error:
        return {}


def who(members, member_id):
    if not member_id:
        return "team"
    return members.get(member_id) or member_id[-6:]


# ── 四类事件的 summary 构造（密度优先：谁 / 做了什么 / 结果） ──────────────────


def ev_from_events(db, team_id, members, after_id, limit):
    """sidecar 的统一事件流：任务生命周期、成员动作都在这里。"""
    try:
        rows = db.execute(
            "select id, member_id, task_id, phase, action, target, result, ts "
            "from event where team_id = ? and id > ? order by id limit ?",
            (team_id, after_id, limit),
        ).fetchall()
    except sqlite3.Error:
        return [], after_id
    out, last = [], after_id
    for eid, mid, tid, phase, action, target, result, ts in rows:
        last = max(last, eid or 0)
        bits = [f"[{who(members, mid)}]", action or phase or "event"]
        if target:
            bits.append(str(target))
        if result:
            bits.append(f"→ {result}")
        out.append({
            "ts": ts or None,
            "kind": "team_event",
            "summary": clip(" ".join(bits)),
            "files": [],
            "payload": {"phase": phase, "task_id": tid, "member": who(members, mid),
                        "row_id": eid},
        })
    return out, last


def ev_from_changes(db, team_id, members, seen, limit):
    """文件级改动：哪个人动了哪个文件、增删多少行。"""
    try:
        rows = db.execute(
            "select id, task_id, member_id, path, kind, added, deleted, created_at "
            "from member_change where team_id = ? order by created_at desc limit 200",
            (team_id,),
        ).fetchall()
    except sqlite3.Error:
        return [], []
    out, ids = [], []
    for cid, tid, mid, path, kind, added, deleted, created in rows:
        if cid in seen:
            continue
        ids.append(cid)
        stat = ""
        if added is not None or deleted is not None:
            stat = f" (+{added or 0}/-{deleted or 0})"
        out.append({
            "ts": created or None,
            "kind": "team_change",
            "summary": clip(f"[{who(members, mid)}] 改动 {path}{stat} · {kind}"),
            "files": [path] if path else [],
            "payload": {"task_id": tid, "member": who(members, mid), "change_kind": kind,
                        "added": added, "deleted": deleted},
        })
    out.reverse()
    return out[:limit], ids


def ev_from_approvals(db, team_id, members, seen, limit):
    """审批留档：谁在什么工具上被卡、谁批的。这是 never 策略会掐掉的那条链。"""
    try:
        rows = db.execute(
            "select id, member_id, tool_name, reason, status, decided_by, created_at "
            "from approval where team_id = ? order by created_at desc limit 200",
            (team_id,),
        ).fetchall()
    except sqlite3.Error:
        return [], []
    out, ids = [], []
    for aid, mid, tool, reason, status, decided_by, created in rows:
        if aid in seen:
            continue
        ids.append(aid)
        who_decided = f" by {decided_by}" if decided_by else ""
        out.append({
            "ts": created or None,
            "kind": "team_approval",
            "summary": clip(f"[{who(members, mid)}] 审批 {status}{who_decided} · "
                            f"{tool} {reason or ''}".strip()),
            "files": [],
            "payload": {"tool": tool, "status": status, "decided_by": decided_by},
        })
    out.reverse()
    return out[:limit], ids


def ev_from_audit(db, team_id, after_id, limit):
    """哈希审计：before/after SHA-256，客户验收时是硬证据。"""
    try:
        rows = db.execute(
            "select id, ts, actor, domain, action, target, before_hash, after_hash, result "
            "from audit where team_id = ? and id > ? order by id limit ?",
            (team_id, after_id, limit),
        ).fetchall()
    except sqlite3.Error:
        return [], after_id
    out, last = [], after_id
    for aid, ts, actor, domain, action, target, before, after, result in rows:
        last = max(last, aid or 0)
        parts = [f"[{actor or 'unknown'}]", f"{domain}.{action}" if domain else action,
                 str(target or "")]
        if before or after:
            parts.append(f"{str(before or '-')[:8]}→{str(after or '-')[:8]}")
        if result:
            parts.append(result)
        out.append({
            "ts": ts or None,
            "kind": "team_audit",
            "summary": clip(" ".join(p for p in parts if p)),
            "files": [target] if target and len(str(target)) < 400 else [],
            "payload": {"before": before, "after": after, "result": result},
        })
    return out, last


# ── 推送 ────────────────────────────────────────────────────────────────────


def post(path, body):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        HUB_URL + path, data=data, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {HUB_TOKEN}"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.status, r.read(400).decode("utf-8", "replace")


def build_batch(db, team_row, state, limit=60):
    """一个 team 的一批事件 + 更新后的水位片段。"""
    team_id, name = team_row
    members = fetch_members(db, team_id)
    cursor = state.get(team_id, {})
    events = []

    ev, last = ev_from_events(db, team_id, members, int(cursor.get("event_id") or 0), limit)
    events += ev
    au, last_audit = ev_from_audit(db, team_id, int(cursor.get("audit_id") or 0), limit)
    events += au
    ch, ch_ids = ev_from_changes(db, team_id, members, set(cursor.get("change_ids") or []), limit)
    events += ch
    ap, ap_ids = ev_from_approvals(db, team_id, members, set(cursor.get("approval_ids") or []),
                                   limit)
    events += ap

    events.sort(key=lambda e: e.get("ts") or "")
    events = events[-limit:]

    new_cursor = {
        "event_id": last,
        "audit_id": last_audit,
        "change_ids": merge_seen(cursor.get("change_ids") or [], ch_ids),
        "approval_ids": merge_seen(cursor.get("approval_ids") or [], ap_ids),
    }
    body = {
        "session": {
            "ext_id": f"team:{team_id}",
            "agent": "dsh-team",
            "project": name or "",
            "title": f"{name} · {len(members)} 成员",
            "machine": machine_name(),
            "cwd": "",
        },
        "events": events,
    }
    return body, new_cursor


def run_once(dry=False, limit=60):
    if not TEAM_DB.exists():
        log(f"team.db 不存在：{TEAM_DB}")
        return 0
    if not dry and not HUB_TOKEN:
        log("AGENT_HUB_TOKEN 未设置，跳过（空跑请加 --dry-run）")
        return 0

    state = load_state()
    pushed = 0
    try:
        db = connect(TEAM_DB)
    except sqlite3.Error as exc:
        log(f"打开 team.db 失败：{exc}")
        return 0

    try:
        for team_row in fetch_teams(db):
            body, cursor = build_batch(db, team_row, state, limit)
            if not body["events"]:
                state[team_row[0]] = cursor
                continue
            if dry:
                print(json.dumps(body, ensure_ascii=False, indent=2))
            else:
                try:
                    status, text = post("/v1/events", body)
                    log(f"{team_row[1]}: {len(body['events'])} 条 → {status}")
                    if status >= 300:
                        log(f"  响应: {text[:200]}")
                        continue
                except (urllib.error.URLError, OSError) as exc:
                    log(f"推送失败（保留水位，下次重试）：{exc}")
                    save_state(state)
                    return pushed
            state[team_row[0]] = cursor
            pushed += len(body["events"])
    finally:
        db.close()

    if not dry:
        save_state(state)
    return pushed


def main():
    global VERBOSE
    ap = argparse.ArgumentParser(
        description="dsh-team → agent-session-hub 事件桥接（只读 team.db，水位去重）")
    ap.add_argument("--once", action="store_true", help="推一次就退出（cron 用这个）")
    ap.add_argument("--watch", action="store_true", help="常驻轮询")
    ap.add_argument("--interval", type=int, default=30, help="轮询间隔秒，默认 30")
    ap.add_argument("--limit", type=int, default=60, help="单批最多事件数，默认 60")
    ap.add_argument("--dry-run", action="store_true", help="只打印 payload，不发送")
    ap.add_argument("--verbose", action="store_true", help="打印过程与错误原因")
    args = ap.parse_args()

    VERBOSE = args.verbose
    if not args.watch:
        args.once = True

    if args.once:
        n = run_once(args.dry_run, args.limit)
        log(f"本次推送 {n} 条")
        return 0

    log(f"watch 模式：每 {args.interval}s 扫一次 {TEAM_DB}")
    try:
        while True:
            run_once(False, args.limit)
            time.sleep(max(5, args.interval))
    except KeyboardInterrupt:
        log("已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
