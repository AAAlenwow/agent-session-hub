#!/usr/bin/env python3
"""Codex CLI notify 上报器。

配置 ~/.codex/config.toml:
    notify = ["python3", "/opt/agent-hub/hooks/codex_notify.py"]

Codex 在回合结束等时机以 JSON 参数（argv[1]）调用本脚本。
MVP 只上报回合级事件；更细粒度（tool call）需要 tail 监听
~/.codex/sessions/ 下的 rollout-*.jsonl，见 README 的 Phase 2。
"""
import json
import os
import sys
import urllib.request

HUB_URL = os.environ.get("AGENT_HUB_URL", "http://127.0.0.1:8421")
HUB_TOKEN = os.environ.get("AGENT_HUB_TOKEN", "")
TIMEOUT = 5
try:
    MAX_TURN_SUMMARY = max(0, int(os.environ.get("AGENT_HUB_TURN_SUMMARY_CHARS", "800")))
except ValueError:
    MAX_TURN_SUMMARY = 800
TAIL_BYTES = 262144


def _tail_lines(path, tail_bytes=TAIL_BYTES):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            f.seek(max(0, end - tail_bytes))
            raw = f.read()
    except Exception:
        return []
    lines = raw.decode("utf-8", "replace").splitlines()
    if end > tail_bytes and lines:
        lines = lines[1:]
    return lines


def last_assistant_text(path):
    """从 Codex rollout JSONL 倒序提取最后一条 assistant 正文。

    兼容两种行形态（不同 Codex 版本）：
      {"type":"response_item","payload":{"role":"assistant",
       "content":[{"type":"output_text","text":"..."}]}}
      {"type":"event_msg","payload":{"type":"agent_message","message":"..."}}
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
            if not isinstance(obj, dict):
                continue
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                continue
            if payload.get("role") == "assistant":
                content = payload.get("content")
                texts = []
                if isinstance(content, str):
                    texts = [content]
                elif isinstance(content, list):
                    texts = [b.get("text", "") for b in content
                             if isinstance(b, dict)]
                t = "\n".join(x for x in texts
                              if isinstance(x, str) and x.strip()).strip()
                if t:
                    return t
            if payload.get("type") == "agent_message" and payload.get("message"):
                return str(payload["message"]).strip()
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


def main():
    if not HUB_TOKEN:
        return
    raw = sys.argv[1] if len(sys.argv) > 1 else "{}"
    try:
        msg = json.loads(raw)
    except Exception:
        return

    t = msg.get("type", "")
    if t != "agent-turn-complete":
        return

    rollout = msg.get("rollout_path", "") or ""
    ext_id = (
        os.path.basename(rollout).replace("rollout-", "").removesuffix(".jsonl")
        if rollout else "unknown"
    )
    cwd = msg.get("cwd", "") or ""
    text = last_assistant_text(rollout)
    summary = " ".join(text.split())[:MAX_TURN_SUMMARY] if text else "codex turn finished"
    body = {
        "session": {
            "ext_id": ext_id,
            "agent": "codex",
            "project": os.path.basename(cwd) or "",
            "cwd": cwd,
        },
        "events": [{"kind": "turn_end", "summary": summary}],
    }
    try:
        post(body)
    except Exception:
        pass


if __name__ == "__main__":
    main()
