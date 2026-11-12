"""E2E 场景测试的公共积木：HTTP 客户端、原件构造器、容器内 SQL。

刻意只用标准库：测试要在"什么依赖都没装"的机器上也能跑（除了 pytest 本身）。
"""
from __future__ import annotations

import gzip
import io
import json
import os
import pathlib
import sqlite3
import subprocess
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]        # agent-hub/
E2E_DIR = pathlib.Path(__file__).resolve().parent
DEFAULT_URL = "http://127.0.0.1:18421"
COMPOSE_PROJECT = "agenthub-e2e"


# ------------------------------------------------------------------ 环境

def _parse_env_file(path: pathlib.Path) -> dict:
    out = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


ENV_FILE = _parse_env_file(E2E_DIR / ".env.e2e")
BASE_URL = os.environ.get("AGENT_HUB_E2E_URL", DEFAULT_URL).rstrip("/")
ADMIN_TOKEN = os.environ.get("AGENT_HUB_E2E_ADMIN_TOKEN") or ENV_FILE.get("ADMIN_TOKEN", "")


# ------------------------------------------------------------------ HTTP

class Resp:
    def __init__(self, status: int, headers: dict, body: bytes):
        self.status = status
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.body = body

    def json(self):
        return json.loads(self.body.decode("utf-8"))

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def __repr__(self):
        return f"<Resp {self.status} {self.body[:120]!r}>"


class Api:
    def __init__(self, base: str = BASE_URL, token: str = ""):
        self.base = base.rstrip("/")
        self.token = token

    def with_token(self, token: str) -> "Api":
        return Api(self.base, token)

    def call(self, method: str, path: str, body=None, token: str | None = None,
             headers: dict | None = None, timeout: int = 60) -> Resp:
        url = self.base + path
        hdrs = dict(headers or {})
        tok = self.token if token is None else token
        if tok:
            hdrs["Authorization"] = "Bearer " + tok
        data = body
        if body is not None and not isinstance(body, (bytes, bytearray)):
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return Resp(r.status, dict(r.headers), r.read())
        except urllib.error.HTTPError as e:
            return Resp(e.code, dict(e.headers or {}), e.read())

    def get(self, path: str, **kw) -> Resp:
        return self.call("GET", path, **kw)

    def post(self, path: str, body=None, **kw) -> Resp:
        return self.call("POST", path, body=body, **kw)

    def delete(self, path: str, **kw) -> Resp:
        return self.call("DELETE", path, **kw)

    def upload(self, path: str, payload: bytes, token: str | None = None,
               ctype: str = "application/x-ndjson", timeout: int = 120) -> Resp:
        return self.call("POST", path, body=payload,
                         headers={"Content-Type": ctype}, token=token, timeout=timeout)


def alive(base: str = BASE_URL, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(base + "/health", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


# ------------------------------------------------------------------ 容器内 SQL

def sql(query: str, db: str = "agenthub") -> str:
    """在测试栈的 postgres 容器里执行 SQL（-tA：无表头、无对齐）。"""
    cmd = [
        "docker", "compose", "-p", COMPOSE_PROJECT,
        "--env-file", str(E2E_DIR / ".env.e2e"),
        "-f", str(ROOT / "docker-compose.yml"),
        "-f", str(E2E_DIR / "docker-compose.e2e.yml"),
        "exec", "-T", "postgres", "psql", "-U", "agenthub", "-d", db, "-tAc", query,
    ]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise RuntimeError(f"psql 失败：{p.stderr.strip()[:400]}")
    return p.stdout.strip()


def sql_ok() -> bool:
    try:
        return sql("select 1") == "1"
    except Exception:
        return False


# ------------------------------------------------------------------ 时间

def iso(epoch: float) -> str:
    """epoch → 带毫秒的 ISO（Claude Code 的 timestamp 格式）。"""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch)) + \
        f".{int((epoch % 1) * 1000):03d}Z"


# ------------------------------------------------------------------ Claude Code 原件

def cc_transcript(cwd: str, sid: str, turns: list[dict]) -> bytes:
    """构造一份 Claude Code 风格的 JSONL。

    turns 每项：{"role": "user"|"assistant", "text": str, "file": str|None,
                "ts": epoch, "in": int, "out": int, "cache": int}
    """
    lines = []
    for i, t in enumerate(turns):
        ts = iso(t.get("ts") or (time.time() - 3600 + i * 60))
        if t["role"] == "user":
            lines.append(json.dumps({
                "type": "user",
                "sessionId": sid,
                "cwd": cwd,
                "timestamp": ts,
                "message": {"role": "user",
                            "content": [{"type": "text", "text": t.get("text", "")}]},
            }, ensure_ascii=False))
            continue

        content = [{"type": "text", "text": t.get("text", "")}]
        if t.get("file"):
            content.append({"type": "tool_use", "id": f"tu_{i}", "name": "Edit",
                            "input": {"file_path": t["file"], "old_string": "a",
                                      "new_string": "b"}})
        lines.append(json.dumps({
            "type": "assistant",
            "sessionId": sid,
            "cwd": cwd,
            "timestamp": ts,
            "message": {
                "role": "assistant",
                "model": t.get("model", "claude-sonnet-4-5"),
                "content": content,
                "usage": {
                    "input_tokens": int(t.get("in", 100)),
                    "output_tokens": int(t.get("out", 50)),
                    "cache_read_input_tokens": int(t.get("cache", 0)),
                    "cache_creation_input_tokens": int(t.get("cache_write", 0)),
                },
            },
        }, ensure_ascii=False))
    return ("\n".join(lines) + "\n").encode("utf-8")


def cc_simple(cwd: str, sid: str, n: int = 4) -> bytes:
    """n 轮问答的最小会话，用于版本/差异类场景。"""
    turns = []
    base = time.time() - 7200
    for i in range(n):
        turns.append({"role": "user", "text": f"第 {i+1} 个问题：改一下限流配置",
                      "ts": base + i * 120})
        turns.append({"role": "assistant", "text": f"第 {i+1} 个回答，改了 gateway.py",
                      "file": "/srv/old-repo/src/gateway.py",
                      "ts": base + i * 120 + 30, "in": 1000, "out": 200})
    return cc_transcript(cwd, sid, turns)


# ------------------------------------------------------------------ WorkBuddy 原件

def wd_line(kind: str, **kw) -> str:
    return json.dumps(kw, ensure_ascii=False)


def wd_transcript(cwd: str, sid: str, n: int = 3, model: str = "glm-4.6",
                  base: int | None = None) -> bytes:
    """构造 WorkBuddy 风格的 JSONL（每行带 cwd，用量在 providerData/message 里）。

    base 给固定值可让两次构造的前缀完全一致 —— 版本 diff 测试需要"纯追加"语义。
    """
    base = int((time.time() - 3600) * 1000) if base is None else int(base)
    lines = [wd_line(
        "session-meta", sessionId=sid, cwd=cwd, createdAt=base,
        providerData={"model": model},
    )]
    for i in range(n):
        lines.append(wd_line(
            "message", id=f"u{i}", sessionId=sid, cwd=cwd, type="message", role="user",
            timestamp=base + i * 60000,
            message={"role": "user", "content": [{"type": "input_text",
                                                  "text": f"用户提问 {i}：把同步改成手动的"}]},
        ))
        lines.append(wd_line(
            "message", id=f"a{i}", sessionId=sid, cwd=cwd, type="message", role="assistant",
            timestamp=base + i * 60000 + 20000,
            providerData={"messageId": f"msg{i}", "stepSeq": 1, "model": model},
            message={"role": "assistant", "model": model,
                     "content": [{"type": "output_text", "text": f"回答 {i}"}],
                     "usage": {"input_tokens": 800, "output_tokens": 120,
                               "cache_read_input_tokens": 300}},
        ))
    return ("\n".join(lines) + "\n").encode("utf-8")


def make_tar(members: list[tuple[str, bytes]], meta: dict | None = None) -> bytes:
    """任意打包体（含 meta.json）。members 可以是恶意成员名，用于负向测试。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        if meta is not None:
            data = json.dumps(meta, ensure_ascii=False).encode("utf-8")
            ti = tarfile.TarInfo("_agenthub/meta.json")
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
        for name, data in members:
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def wd_bundle(cwd: str, sid: str, n: int = 3, sidecars: dict | None = None,
              rollback: bool = True, base: int | None = None) -> bytes:
    """WorkBuddy 完整打包体：jsonl + rollback + tool-results 旁挂 + meta。"""
    jsonl = wd_transcript(cwd, sid, n, base=base)
    sidecars = sidecars or {"r1.txt": b"tool output 1\n", "r2.txt": b"tool output 2\n"}
    members = [(f"{sid}.jsonl", jsonl)]
    if rollback:
        members.append((f"{sid}.file-rollback.ndjson", b'{"file":"a.py"}\n'))
    for name, data in sidecars.items():
        members.append((f"{sid}/tool-results/{name}", data))
    meta = {"agent": "workbuddy", "ext_id": sid, "cwd": cwd,
            "slug": cwd.strip("/").replace("/", "-"), "title": "把同步改成手动",
            "db_rows": {"session": {"id": sid, "title": "把同步改成手动", "cwd": cwd}}}
    return make_tar(members, meta)


def untar(data: bytes) -> dict[str, bytes]:
    out = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        for m in tf.getmembers():
            if m.isfile():
                out[m.name] = tf.extractfile(m).read()
    return out


# ------------------------------------------------------------------ 本机 WorkBuddy 目录

def make_wb_home(root: pathlib.Path, sid: str, cwd: str, n: int = 3,
                 sidecars: dict | None = None) -> pathlib.Path:
    """造一个假的 ~/.workbuddy：projects/<slug>/<sid>.jsonl + tool-results + workbuddy.db。"""
    slug = cwd.strip("/").replace("/", "-")
    proj = root / "projects" / slug
    proj.mkdir(parents=True, exist_ok=True)
    (proj / f"{sid}.jsonl").write_bytes(wd_transcript(cwd, sid, n))
    (proj / f"{sid}.file-rollback.ndjson").write_text('{"file":"a.py"}\n', encoding="utf-8")
    sc = proj / sid / "tool-results"
    sc.mkdir(parents=True, exist_ok=True)
    for name, data in (sidecars or {"r1.txt": b"tool output 1\n"}).items():
        (sc / name).write_bytes(data)

    db = root / "workbuddy.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""create table if not exists sessions (
        id text primary key, user_id text, workspace_id text, title text,
        cwd text, created_at integer, updated_at integer)""")
    conn.execute("""create table if not exists session_usage (
        session_id text primary key, input_tokens integer, output_tokens integer)""")
    conn.execute("insert or replace into sessions (id, user_id, workspace_id, title, cwd, "
                 "created_at, updated_at) values (?,?,?,?,?,?,?)",
                 (sid, "u-e2e", "ws-1", "把同步改成手动", cwd,
                  int(time.time()) - 3600, int(time.time())))
    conn.execute("insert or replace into session_usage values (?,?,?)", (sid, 2400, 360))
    conn.commit()
    conn.close()
    return root


def gzip_bytes(raw: bytes) -> bytes:
    return gzip.compress(raw)
