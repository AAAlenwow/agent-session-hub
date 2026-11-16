#!/usr/bin/env python3
"""通用上报器：给非 Claude-Code 协议的生态用（dsh-go / DSH / 自研插件 / cron 脚本）。

设计目标：**接入方不需要懂本项目的任何内部结构**，只要拼一个 JSON 或几条命令行参数。
Claude Code 系（含 GLM / MiniMax / Kimi）用 cc_hook.py，Codex 用 codex_notify.py，
其余一律走这里。

两种用法（都只需两个环境变量 AGENT_HUB_URL / AGENT_HUB_TOKEN）：

1) stdin JSON（插件里最省事）：
   echo '{"session":{"ext_id":"dsh-abc","agent":"dsh"},
          "events":[{"kind":"turn_end","summary":"修好了限流"}]}' | generic_report.py

2) 命令行参数（服务器 cron / 一次性上报）：
   generic_report.py --session-id dsh-abc --agent dsh --project demo-shop \\
       --kind turn_end --summary "圣诞活动逻辑已改为满100送50"

可选字段缺省：agent=dsh、project=当前目录名、machine=hostname
（AGENT_HUB_MACHINE 可覆盖，例如 "build-01"）。
未配置 token 时静默 exit 0；网络失败返回 1 并打印原因（可直接用于 cron 告警）。
"""
import argparse
import json
import os
import socket
import sys
import urllib.request

HUB_URL = os.environ.get("AGENT_HUB_URL", "http://127.0.0.1:8421")
HUB_TOKEN = os.environ.get("AGENT_HUB_TOKEN", "")
TIMEOUT = 10


def machine_name() -> str:
    return (os.environ.get("AGENT_HUB_MACHINE")
            or socket.gethostname()
            or "unknown")[:120]


def post(body: dict) -> dict:
    req = urllib.request.Request(
        HUB_URL.rstrip("/") + "/v1/events",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + HUB_TOKEN,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def from_args(a) -> dict:
    cwd = os.path.abspath(a.cwd or os.getcwd())
    sess = {
        "ext_id": a.session_id,
        "agent": a.agent,
        "project": a.project or os.path.basename(cwd),
        "machine": a.machine or machine_name(),
        "cwd": cwd,
        "title": a.title,
    }
    events = [{
        "kind": a.kind,
        "summary": a.summary,
        "files": a.file or [],
    }]
    if a.ts:
        events[0]["ts"] = a.ts
    return {"session": sess, "events": events}


def from_stdin(raw: str) -> dict:
    body = json.loads(raw)
    sess = body.setdefault("session", {})
    sess.setdefault("machine", machine_name())
    sess.setdefault("agent", "dsh")
    if not sess.get("project") and sess.get("cwd"):
        sess["project"] = os.path.basename(sess["cwd"])
    if not body.get("events"):
        raise ValueError("events 不能为空")
    return body


def main() -> int:
    ap = argparse.ArgumentParser(description="agent-hub 通用上报器")
    ap.add_argument("--session-id", help="会话 id；不给则读 stdin JSON")
    ap.add_argument("--agent", default=os.environ.get("AGENT_HUB_AGENT", "dsh"))
    ap.add_argument("--project", default=os.environ.get("AGENT_HUB_PROJECT", ""))
    ap.add_argument("--machine", default="", help="机器名（默认 hostname）")
    ap.add_argument("--cwd", default="")
    ap.add_argument("--title", default="")
    ap.add_argument("--kind", default="turn_end",
                    help="user_prompt / tool_call / turn_end / shared_note ...")
    ap.add_argument("--summary", default="")
    ap.add_argument("--file", action="append", help="涉及的文件路径，可重复")
    ap.add_argument("--ts", default="", help="ISO 时间，缺省用服务器时间")
    a = ap.parse_args()

    if not HUB_TOKEN:
        return 0
    try:
        body = from_args(a) if a.session_id else from_stdin(sys.stdin.read())
        r = post(body)
        print(f"ok session #{r['session_id']} ingested {r['ingested']}")
    except Exception as e:
        print(f"上报失败：{e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
