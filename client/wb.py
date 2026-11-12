#!/usr/bin/env python3
"""WorkBuddy 会话同步与跨机还原。

WorkBuddy 没有 CLI 的 hook 机制，会话完全落在本地：
    ~/.workbuddy/projects/<slug>/<session-id>.jsonl     ← 对话正文（毫秒时间戳）
    ~/.workbuddy/projects/<slug>/<session-id>/tool-results/*.txt   ← 大体积工具输出（旁挂）
    ~/.workbuddy/workbuddy.db  (sessions / session_usage / workspaces)  ← 界面索引

所以换机器只拷 jsonl 是不够的：
  1) 旁挂的 tool-results 不拷 → 会话里引用的大块工具输出全丢
  2) 索引表没有对应行 → WorkBuddy 侧边栏根本不显示这个会话

本模块把这三样作为一个打包体上传，回放时再一起还原：
  · 上传：tar.gz（jsonl + rollback + tool-results + _agenthub/meta.json 里的 DB 行快照）
  · 回放：解包 → 按本机新路径重写全部文本 → 落到 ~/.workbuddy/projects/<新slug>/
          → 把 sessions / session_usage / workspaces 行补进本机 workbuddy.db

只依赖标准库。DB 写入前一定备份，且可 --dry-run 只看 SQL。
"""
import gzip
import hashlib
import io
import json
import os
import pathlib
import re
import shutil
import sqlite3
import tarfile
import time

WB_HOME = pathlib.Path(os.environ.get("AGENT_HUB_WB_DIR", "~/.workbuddy")).expanduser()
META_NAME = "_agenthub/meta.json"
STATE_PATH = pathlib.Path(os.environ.get("AGENT_HUB_STATE", "~/.agenthub/wb-sync.json")).expanduser()


# ------------------------------------------------------------------ 基础工具

def slug_for(path: str) -> str:
    """WorkBuddy 项目目录名：去掉开头 '/'，再把 '/' 换成 '-'（点号保留）。"""
    return path.strip("/").replace("/", "-")


def db_path() -> pathlib.Path:
    return WB_HOME / "workbuddy.db"


def replace_path(text: str, old: str, new: str) -> str:
    """按路径段边界替换，避免 /code/api 把 /code/api-gateway 改成 repo-gateway。"""
    o, n = (old or "").rstrip("/"), (new or "").rstrip("/")
    if not o or not n or o == n:
        return text
    return re.sub(re.escape(o) + r"(?![\w.\-])", lambda _m: n, text)


def safe_join(base: pathlib.Path, member: str) -> pathlib.Path:
    """把打包体成员名安全地拼到 base 之下。

    成员名来自服务器上的打包体，而打包体是**别的成员上传的**，因此完全不可信：
      · "../../../../.zshrc"  → 会解析到用户 HOME（下一次开 shell 即执行）
      · "/etc/cron.d/x"       → Path 的 / 运算在右侧为绝对路径时直接丢弃左侧
    两者都必须拒绝，而不是"尽力而为"地修正。
    """
    raw = str(member).replace("\\", "/")
    rel = pathlib.PurePosixPath(raw)
    if rel.is_absolute() or any(part == ".." for part in rel.parts):
        raise RuntimeError(f"打包体成员越界，已拒绝：{member}")
    root = base.resolve()
    target = (root / raw).resolve()
    if target != root and root not in target.parents:
        raise RuntimeError(f"打包体成员越界，已拒绝：{member}")
    return target


def connect_db(readonly: bool = True) -> sqlite3.Connection:
    p = db_path()
    if readonly:
        uri = f"file:{p}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
    else:
        conn = sqlite3.connect(str(p), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def local_user_id() -> str:
    """本机 WorkBuddy 账号 id：优先取 DB 里出现最多的 user_id，退化为目录名。"""
    try:
        conn = connect_db()
        row = conn.execute(
            "select user_id, count(*) c from sessions where user_id is not null "
            "group by user_id order by c desc limit 1"
        ).fetchone()
        conn.close()
        if row and row["user_id"]:
            return row["user_id"]
    except Exception:
        pass
    for d in WB_HOME.glob("user-*-personal"):
        name = d.name  # user-<uuid>-personal
        if name.startswith("user-") and name.endswith("-personal"):
            return name[5:-9]
    return ""


# ------------------------------------------------------------- 扫描与打包

def iter_sessions(since_days: int = 0, only_ids: list[str] = None):
    """遍历本机会话，yield 每个会话的元信息。"""
    projects = WB_HOME / "projects"
    if not projects.is_dir():
        return
    cutoff = time.time() - since_days * 86400 if since_days else 0
    for proj in sorted(projects.iterdir()):
        if not proj.is_dir():
            continue
        for f in sorted(proj.glob("*.jsonl")):
            if f.name.endswith(".file-rollback.ndjson"):
                continue
            st = f.stat()
            if cutoff and st.st_mtime < cutoff:
                continue
            sid = f.stem
            if only_ids and sid not in only_ids:
                continue
            yield {
                "id": sid,
                "jsonl": f,
                "project_dir": proj,
                "sidecar": proj / sid / "tool-results",
                "rollback": proj / f"{sid}.file-rollback.ndjson",
                "size": st.st_size,
                "mtime": int(st.st_mtime),
            }


def read_db_rows(sid: str) -> dict:
    """取该会话在索引库里的行快照（回放时用它重建界面条目）。"""
    out = {}
    try:
        conn = connect_db()
        r = conn.execute("select * from sessions where id = ?", (sid,)).fetchone()
        if r:
            out["session"] = dict(r)
        u = conn.execute("select * from session_usage where session_id = ?", (sid,)).fetchone()
        if u:
            out["usage"] = dict(u)
        conn.close()
    except Exception as e:
        out["error"] = f"{e}"
    return out


def session_cwd(jsonl: pathlib.Path) -> str:
    """从首几行拿 cwd（WorkBuddy 每行都带 cwd）。"""
    try:
        with open(jsonl, "r", encoding="utf-8", errors="replace") as fh:
            for _ in range(50):
                line = fh.readline()
                if not line:
                    break
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("cwd"):
                    return d["cwd"]
    except Exception:
        pass
    return ""


def build_bundle(sess: dict, include_sidecar: bool = True) -> tuple[bytes, dict]:
    """打成一个 tar.gz：jsonl + rollback + tool-results + meta。

    字节级确定性：同样的内容打出的包必须一字节不差（meta 不带打包时间、
    tar 成员 mtime 用源文件自己的、gzip 头 mtime 固定为 0）。
    否则服务端按整包 sha256 判幂等永远判不中，state 丢失后全量重同步
    会给每个未变化的会话长出一个 +0 -0 的空版本。"""
    sid = sess["id"]
    cwd = session_cwd(sess["jsonl"])
    rows = read_db_rows(sid)
    sidecar_files = []
    if include_sidecar and sess["sidecar"].is_dir():
        sidecar_files = [f for f in sorted(sess["sidecar"].rglob("*")) if f.is_file()]

    meta = {
        "agent": "workbuddy",
        "ext_id": sid,
        "cwd": cwd,
        "slug": sess["project_dir"].name,
        "title": (rows.get("session") or {}).get("title") or "",
        "size": sess["size"],
        "mtime": sess["mtime"],
        "sidecar": [str(f.relative_to(sess["sidecar"])) for f in sidecar_files],
        "db_rows": rows,
    }

    buf = io.BytesIO()
    # 先打裸 tar 再手动 gzip（mtime=0）：mode="w:gz" 的 gzip 头会带当前时间
    with tarfile.open(fileobj=buf, mode="w") as tf:
        tf.add(str(sess["jsonl"]), arcname=f"{sid}.jsonl")
        if sess["rollback"].exists():
            tf.add(str(sess["rollback"]), arcname=sess["rollback"].name)
        for f in sidecar_files:
            arc = f"{sid}/tool-results/{f.relative_to(sess['sidecar'])}"
            tf.add(str(f), arcname=arc)

        data = json.dumps(meta, ensure_ascii=False).encode("utf-8")
        ti = tarfile.TarInfo(META_NAME)
        ti.size = len(data)
        ti.mtime = int(sess["mtime"])      # 源文件 mtime：内容不变则字节稳定
        tf.addfile(ti, io.BytesIO(data))

    return gzip.compress(buf.getvalue(), mtime=0), meta


# --------------------------------------------------- 同步计划（选什么、变了多少）

def content_sha(sess: dict, include_sidecar: bool = True) -> str:
    """内容指纹：jsonl 正文 + 旁挂文件名与大小。用来判断「新增 / 变更 / 未变化」。"""
    h = hashlib.sha256()
    h.update(sess["jsonl"].read_bytes())
    if include_sidecar and sess["sidecar"].is_dir():
        for f in sorted(sess["sidecar"].rglob("*")):
            if f.is_file():
                h.update(f"{f.relative_to(sess['sidecar'])}:{f.stat().st_size}".encode())
    return h.hexdigest()


def count_lines(sess: dict) -> int:
    n = 0
    with open(sess["jsonl"], "rb") as fh:
        for _ in fh:
            n += 1
    return n


def titles_by_id() -> dict:
    """一次读完所有标题，避免 plan 里每个会话查一次库。"""
    try:
        conn = connect_db()
        return {r["id"]: r["title"] or "" for r in
                conn.execute("select id, title from sessions")}
    except Exception:
        return {}


def plan(since_days: int = 0, since_date: str = "", project: str = "",
         only: list[str] = None, exclude: list[str] = None,
         include_sidecar: bool = True, state: dict = None) -> list[dict]:
    """生成本次要同步的清单：带状态、行数变化、体积。不联网、不写盘。"""
    state = state if state is not None else load_state()
    titles = titles_by_id()
    cutoff = 0.0
    if since_days:
        cutoff = time.time() - since_days * 86400
    elif since_date:
        try:
            import datetime as _dt
            cutoff = _dt.datetime.fromisoformat(since_date).timestamp()
        except Exception:
            cutoff = 0.0

    ids = None
    if only:
        ids = [o.strip() for o in only if o.strip()]

    rows = []
    for s in wb_iter_all():
        st = s["jsonl"].stat()
        if cutoff and st.st_mtime < cutoff:
            continue
        if ids and not any(s["id"].startswith(i) or s["id"] == i for i in ids):
            continue
        if exclude and any(s["id"].startswith(e) for e in exclude):
            continue
        if project:
            hay = f"{s['project_dir'].name} {session_cwd(s['jsonl'])}".lower()
            if project.lower() not in hay:
                continue

        sha = content_sha(s, include_sidecar)
        lines = count_lines(s)
        rec = state.get(s["id"]) or {}
        if not rec:
            status = "新增"
        elif rec.get("sha") != sha:
            status = "变更"
        else:
            status = "未变化"
        prev_lines = int(rec.get("lines") or 0)
        rows.append({
            "sess": s,
            "id": s["id"],
            "short": s["id"][:8],
            "title": titles.get(s["id"], ""),
            "status": status,
            "lines": lines,
            "delta": lines - prev_lines if status == "变更" else (lines if status == "新增" else 0),
            "size": s["size"],
            "sha": sha,
            "rev": rec.get("rev", 0),
            "mtime": int(st.st_mtime),
        })
    rows.sort(key=lambda r: ({"变更": 0, "新增": 1, "未变化": 2}[r["status"]], -r["mtime"]))
    return rows


def wb_iter_all():
    """无条件遍历（不受 --since-days 影响），供 plan 自己过滤。"""
    return iter_sessions()



# --------------------------------------------------------------- 增量状态

def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def save_state(state: dict):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=1))


def fingerprint(sess: dict) -> str:
    """jsonl 大小 + mtime + 旁挂文件数 → 变了才重新上传。"""
    n = 0
    if sess["sidecar"].is_dir():
        n = sum(1 for f in sess["sidecar"].iterdir() if f.is_file())
    raw = f"{sess['size']}|{sess['mtime']}|{n}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# --------------------------------------------------------------- 跨机还原

def install_bundle(bundle_bytes: bytes, into: str, rewrite_from: str = "",
                   user_id: str = "", dry_run: bool = False, patch_db: bool = True) -> dict:
    """把打包体装到本机：解包 → 路径重写 → 落盘 → 补 DB 索引。"""
    into = os.path.abspath(os.path.expanduser(into))
    target_dir = WB_HOME / "projects" / slug_for(into)

    with tarfile.open(fileobj=io.BytesIO(bundle_bytes), mode="r:gz") as tf:
        members = [m for m in tf.getmembers() if m.isfile()]
        meta = {}
        for m in members:
            if m.name == META_NAME:
                meta = json.loads(tf.extractfile(m).read().decode("utf-8"))
        sid = meta.get("ext_id") or ""
        if not sid:
            # 退化为找主 jsonl 名
            for m in members:
                base = m.name.rsplit("/", 1)[-1]
                if base.endswith(".jsonl") and "rollback" not in base:
                    sid = base[:-6]
                    break
        if not sid:
            raise RuntimeError("打包体里没有会话文件")

        old = rewrite_from or meta.get("cwd") or ""

        def rewrite(data: bytes) -> bytes:
            if not (old and old != into):
                return data
            try:
                return replace_path(data.decode("utf-8"), old, into).encode("utf-8")
            except UnicodeDecodeError:
                return data

        plan = []
        for m in members:
            name = m.name
            if name == META_NAME:
                continue
            # 先校验目标路径：任何一个成员越界，整包拒绝，一个字节都不落盘。
            # 包内结构：<sid>.jsonl / <sid>.file-rollback.ndjson / <sid>/tool-results/...
            dst = safe_join(target_dir, name)
            plan.append((dst, rewrite(tf.extractfile(m).read())))

    written = []
    if not dry_run:
        target_dir.mkdir(parents=True, exist_ok=True)
        for dst, data in plan:
            if dst.exists():
                bak = dst.with_name(dst.name + f".bak-{int(time.time())}")
                shutil.move(str(dst), str(bak))
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)
            written.append(str(dst))
    else:
        written = [str(dst) for dst, _ in plan]

    result = {
        "session_id": sid,
        "title": meta.get("title", ""),
        "installed_to": str(target_dir),
        "files": written,
        "origin_cwd": old,
        "new_cwd": into,
        "dry_run": dry_run,
    }

    if patch_db:
        result["db"] = patch_local_db(meta, sid, into, user_id or local_user_id(),
                                      dry_run=dry_run)
    return result


SESSION_SKIP_COLS = {"user_id", "cwd", "id"}


def patch_local_db(meta: dict, sid: str, into: str, user_id: str, dry_run: bool = False) -> dict:
    """把会话行补进本机 workbuddy.db，让侧边栏能看到它。
    列名从快照里动态取，跨版本升级不会因为多/少一列而失败。"""
    rows = (meta or {}).get("db_rows") or {}
    sess_row = dict(rows.get("session") or {})
    usage_row = dict(rows.get("usage") or {})
    if not sess_row:
        return {"skipped": "原机没有索引行快照（旧版客户端上传的），只还原了文件"}

    db = db_path()
    if not db.exists():
        return {"skipped": f"{db} 不存在，请先启动一次 WorkBuddy"}

    conn = connect_db(readonly=False)
    conn.execute("pragma busy_timeout = 8000")
    existing_cols = {r["name"] for r in conn.execute("pragma table_info(sessions)")}
    sess_row["id"] = sid
    sess_row["cwd"] = into
    sess_row["user_id"] = user_id
    use = {k: v for k, v in sess_row.items() if k in existing_cols}

    cols = list(use.keys())
    sql = (f"insert into sessions ({','.join(cols)}) values ({','.join('?' * len(cols))}) "
           f"on conflict(id) do update set "
           + ",".join(f"{c}=excluded.{c}" for c in cols if c != "id"))
    params = [use[c] for c in cols]

    stmts = [(sql, params)]

    if usage_row:
        ucols = [c for c in usage_row if c in
                 {r["name"] for r in conn.execute("pragma table_info(session_usage)")}]
        if ucols:
            stmts.append((
                f"insert or replace into session_usage ({','.join(ucols)}) "
                f"values ({','.join('?' * len(ucols))})",
                [usage_row[c] for c in ucols],
            ))

    stmts.append((
        "insert or ignore into workspaces (path, last_opened_at) values (?, ?)",
        [into, int(time.time() * 1000)],
    ))

    if dry_run:
        conn.close()
        return {"dry_run": True,
                "sql": [s for s, _ in stmts],
                "session_cols": cols}

    backup = db.with_suffix(f".db.bak-{int(time.time())}")
    shutil.copy2(db, backup)
    try:
        with conn:
            for s, p in stmts:
                conn.execute(s, p)
    finally:
        conn.close()
    return {"patched": True, "backup": str(backup),
            "session_cols": len(cols), "usage_rows": 1 if usage_row else 0}
