#!/usr/bin/env python3
"""Claude Code 通用 hook：把会话事件上报到 agent-session-hub。

覆盖所有 Claude Code 协议兼容 CLI（Claude Code / GLM / MiniMax / Kimi 等），
它们共享同一套 hooks 机制，只是 agent 名用 AGENT_HUB_AGENT 环境变量区分。

设计原则：网络失败 / 输入异常一律静默退出，绝不阻塞用户敲代码。
"""
import json
import os
import socket
import subprocess
import sys
import urllib.parse
import urllib.request

HUB_URL = os.environ.get("AGENT_HUB_URL", "http://127.0.0.1:8421")
HUB_TOKEN = os.environ.get("AGENT_HUB_TOKEN", "")
TIMEOUT = 5
MAX_SUMMARY = 400
# turn_end 摘要带上 assistant 最终回答（思维链共享的刚需）：
# 太短丢语义、太长污染事件流，默认 800 字，可用环境变量调。
try:
    MAX_TURN_SUMMARY = max(0, int(os.environ.get("AGENT_HUB_TURN_SUMMARY_CHARS", "800")))
except ValueError:
    MAX_TURN_SUMMARY = 800
UPLOAD_TRANSCRIPT = os.environ.get("AGENT_HUB_UPLOAD_TRANSCRIPT", "0") == "1"


def machine_name() -> str:
    """上传机器标识：默认 hostname（如 alice-mac-mini），
    AGENT_HUB_MACHINE 可覆盖成更好认的名字（如 "Alice 的 MacBook"、"build-01"）。"""
    return (os.environ.get("AGENT_HUB_MACHINE")
            or socket.gethostname()
            or "unknown")[:120]
# 只读 transcript 尾部这么多字节：回合末尾的 assistant 回答必然在文件最尾，
# 没必要为一段摘要整读几 MB 的会话原件。
TAIL_BYTES = 262144


def clip(s, n=MAX_SUMMARY):
    return (s or "").replace("\n", " ").strip()[:n]


def _tail_lines(path, tail_bytes=TAIL_BYTES):
    """读文件尾部按行切。文件不存在/读不了返回 []。"""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            f.seek(max(0, end - tail_bytes))
            raw = f.read()
    except Exception:
        return []
    lines = raw.decode("utf-8", "replace").splitlines()
    # 起点落在行中间时第一行是半行，丢掉
    if end > tail_bytes and lines:
        lines = lines[1:]
    return lines


def last_assistant_text(path):
    """从 transcript（Claude Code 协议 JSONL）倒序找最后一条带正文的
    assistant 消息，返回其文本。任何异常/找不到都返回 ""。

    这是事件层承载思维链的关键：没有它，团队成员只能搜到"你问过什么"，
    搜不到"agent 当时给出的结论、修复说明、待办清单"。
    """
    if not path:
        return ""
    try:
        for line in reversed(_tail_lines(path)):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if not isinstance(obj, dict) or obj.get("type") != "assistant":
                continue
            content = (obj.get("message") or {}).get("content")
            texts = []
            if isinstance(content, str):
                texts = [content]
            elif isinstance(content, list):
                texts = [b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text"]
            t = "\n".join(x for x in texts if isinstance(x, str) and x.strip()).strip()
            if t:
                return t
    except Exception:
        pass
    return ""


def post(data):
    req = urllib.request.Request(
        HUB_URL.rstrip("/") + "/v1/events",
        data=json.dumps(data, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + HUB_TOKEN,
        },
        method="POST",
    )
    urllib.request.urlopen(req, timeout=TIMEOUT)


def build_event(msg):
    """把 Claude Code hook 消息转成 agent-session-hub 事件，不认识的事件返回 None。"""
    ev = msg.get("hook_event_name", "")

    if ev == "UserPromptSubmit":
        return {"kind": "user_prompt", "summary": clip(msg.get("prompt", ""))}

    if ev == "PostToolUse":
        ti = msg.get("tool_input") or {}
        tool = msg.get("tool_name", "") or ""

        # 技能包调用（Claude Code 的 Skill 工具，含 /slash command）
        if tool in ("Skill", "skill"):
            cmd = str(ti.get("command", "") or ti.get("name", ""))
            extra = str(ti.get("args", "") or "")[:120]
            return {
                "kind": "skill_call",
                "summary": clip(f"skill {cmd} {extra}".strip()),
                "payload": {"skill": cmd},
            }

        # MCP 工具调用（tool_name 形如 mcp__<server>__<tool>）
        if tool.startswith("mcp__"):
            parts = tool.split("__", 2)
            summary = (f"mcp {parts[1]}: {parts[2]}" if len(parts) == 3
                       else f"mcp {tool}")
            return {"kind": "mcp_call", "summary": clip(summary), "payload": {"tool": tool}}

        f = ti.get("file_path") or ti.get("path") or ti.get("notebook_path") or ""
        summary = f"{tool}: {f}" if f else \
            f"{tool}: {json.dumps(ti, ensure_ascii=False)[:200]}"
        return {
            "kind": "tool_call",
            "summary": clip(summary),
            "files": [f] if f else [],
            "payload": {"tool": tool},
        }

    if ev == "Stop":
        # turn_end 摘要 = 本回合 assistant 的最终回答（截断）。
        # 提不出来（无 transcript_path / 原件不可读 / 该回合无正文）时退回占位符，
        # 绝不能让提取失败阻塞 hook。
        text = last_assistant_text(msg.get("transcript_path") or "")
        if text:
            return {"kind": "turn_end", "summary": clip(text, MAX_TURN_SUMMARY)}
        return {"kind": "turn_end", "summary": "assistant turn finished"}

    if ev == "SessionStart":
        return {"kind": "session_start", "summary": clip(msg.get("source", "session start"))}

    if ev == "SessionEnd":
        return {"kind": "session_end", "summary": clip(msg.get("reason", "session end"))}

    return None


def upload_transcript_file(path: str, session_id: str, cwd: str, agent: str):
    """上传会话原件（JSONL）。原件层未开启时服务端返回 403，静默忽略。"""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except Exception:
        return
    qs = urllib.parse.urlencode({"agent": agent, "ext_id": session_id, "cwd": cwd})
    req = urllib.request.Request(
        HUB_URL.rstrip("/") + "/v1/transcripts?" + qs,
        data=raw,
        headers={
            "Content-Type": "application/x-ndjson",
            "Authorization": "Bearer " + HUB_TOKEN,
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=30)
    except Exception:
        pass


def spawn_upload(path: str, session_id: str, cwd: str, agent: str):
    """后台子进程上传，避免拖慢 Stop hook（Token 大时文件可能几 MB）。"""
    try:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--upload", path, session_id, cwd, agent],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass


def main():
    try:
        msg = json.load(sys.stdin)
    except Exception:
        return
    # hook 的铁律是静默成功：stdin 是数组/字符串/数字时直接忽略（不带 .get），
    # 不能 AttributeError 非零退出——Claude Code 会把非零退出当 hook 失败报给用户
    if not isinstance(msg, dict):
        return

    event = build_event(msg)
    if event is None or not HUB_TOKEN:
        return

    cwd = msg.get("cwd", "") or ""
    agent = os.environ.get("AGENT_HUB_AGENT", "claude-code")
    body = {
        "session": {
            "ext_id": msg.get("session_id", "unknown"),
            "agent": agent,
            "project": os.path.basename(cwd) or "",
            "machine": machine_name(),
            "cwd": cwd,
            "repo": os.environ.get("AGENT_HUB_REPO", ""),
        },
        "events": [event],
    }
    try:
        post(body)
    except Exception:
        pass

    # 回合/会话结束时归档原件（需显式开启，原件含完整对话）
    tp = msg.get("transcript_path")
    if UPLOAD_TRANSCRIPT and tp and os.path.isfile(tp) and \
            event["kind"] in ("turn_end", "session_end"):
        spawn_upload(tp, msg.get("session_id", "unknown"), cwd, agent)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--upload":
        upload_transcript_file(
            sys.argv[2],
            sys.argv[3] if len(sys.argv) > 3 else "unknown",
            sys.argv[4] if len(sys.argv) > 4 else "",
            sys.argv[5] if len(sys.argv) > 5 else "claude-code",
        )
    else:
        main()
