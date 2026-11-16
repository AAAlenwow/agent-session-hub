"""会话原件（JSONL / tar.gz 打包体）解析与跨机适配。

支持三种源文件：
  - Claude Code: ~/.claude/projects/<slug>/<session-id>.jsonl
      每行 JSON，assistant 行带 message.usage，含 cwd / timestamp
  - Codex:      ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
      event_msg / token_count 里有累计用量
  - WorkBuddy:  ~/.workbuddy/projects/<slug>/<session-id>.jsonl
                        + <session-id>/tool-results/*.txt（旁挂文件，必须一起搬）
      行类型 session-meta / message / reasoning / function_call /
      function_call_result / file-history-snapshot / ai-title
      时间戳是毫秒整数；用量在 message.usage（每次请求一份，需按 messageId 去重）
      → 打包成 tar.gz 上传，包内 _agenthub/meta.json 记录 DB 行快照

只做三件事：统计用量、识别 cwd、把旧路径改写成新路径。
不做任何结构改写，保证原客户端能直接接着读。
"""
import gzip
import io
import json
import re
import tarfile
import zlib

BUNDLE_META = "_agenthub/meta.json"
TEXT_EXT = (".jsonl", ".json", ".ndjson", ".txt", ".md", ".log", ".yaml", ".yml")
MAX_BUNDLE_MEMBERS = 5000


def decompress(body: bytes) -> bytes:
    """兼容 gzip 与明文上传（注意：tar.gz 由调用方先行识别，不走这里）。

    无体积上限，只适合处理**自己生成**的数据。外部上传请用 decompress_limited()。"""
    return gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body


def decompress_limited(body: bytes, limit: int) -> bytes:
    """有上限的解压：展开后的字节数一旦超过 limit 立刻中止。

    必须先限量再解压，不能"解完再看多大"——gzip 压缩比可达 1000:1，
    一个 30MB 的请求体能解出 30GB，直接 OOM 掉整个服务。"""
    if not body:
        return b""
    if body[:2] != b"\x1f\x8b":
        if len(body) > limit:
            raise ValueError(f"内容超过 {limit} 字节上限")
        return body

    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = bytearray()
    buf = body
    while True:
        remaining = limit + 1 - len(out)          # 多解 1 字节，用来判断是否越界
        out.extend(d.decompress(buf, remaining))
        if len(out) > limit:
            raise ValueError(f"解压后超过 {limit} 字节上限（疑似压缩炸弹）")
        buf = d.unconsumed_tail
        if not buf:
            break
    out.extend(d.flush())
    if len(out) > limit:
        raise ValueError(f"解压后超过 {limit} 字节上限（疑似压缩炸弹）")
    return bytes(out)


def is_bundle(raw: bytes) -> bool:
    """是不是 tar.gz 打包体（WorkBuddy 那种 jsonl + 旁挂文件）。"""
    if not raw or raw[:2] != b"\x1f\x8b":
        return False
    try:
        return tarfile.is_tarfile(io.BytesIO(raw))
    except Exception:
        return False


def bundle_guard(raw: bytes, limit: int) -> None:
    """打包体的体积守卫：成员数、单成员大小、总展开大小都设上限。

    getmembers() 只读 tar 头（大小信息在头里），所以在真正 extractfile 之前
    就能拦住"声明 3GB 的成员"，不需要先把数据读进内存。

    但只读头是不够的：一个**空 tar**（全零块）里没有任何成员，头部合计 0 字节，
    而 gzip 能把它解成上百 MB —— 这类"没有成员但展开巨大"的包必须按展开流拦住。
    """
    total = 0
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as gz:
            expanded = 0
            while True:
                chunk = gz.read(1 << 20)
                if not chunk:
                    break
                expanded += len(chunk)
                if expanded > limit:
                    raise ValueError(f"打包体展开后超过 {limit} 字节上限（疑似压缩炸弹）")
    except (OSError, EOFError) as e:
        raise ValueError(f"打包体无法解压：{e}") from e

    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
            for i, m in enumerate(tf.getmembers(), 1):
                if i > MAX_BUNDLE_MEMBERS:
                    raise ValueError(f"打包体成员过多（> {MAX_BUNDLE_MEMBERS}）")
                if not m.isfile():
                    continue
                if m.size > limit:
                    raise ValueError(f"打包体成员 {m.name} 超过 {limit} 字节上限")
                total += m.size
                if total > limit:
                    raise ValueError(f"打包体展开后超过 {limit} 字节上限")
    except (tarfile.TarError, OSError, EOFError) as e:
        raise ValueError(f"打包体无法解析：{e}") from e


# ---------------------------------------------------------------- 打包体操作

def bundle_members(raw: bytes) -> list[str]:
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
            return tf.getnames()
    except Exception:
        return []


def extract_primary_jsonl(raw: bytes) -> tuple[bytes, str]:
    """从打包体里挑主会话文件：最大的、非 _agenthub 前缀、非 rollback 的 .jsonl。"""
    best = (b"", "")
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
        for m in tf.getmembers():
            if not m.isfile():
                continue
            name = m.name
            base = name.rsplit("/", 1)[-1]
            if name.startswith("_agenthub/") or "file-rollback" in base:
                continue
            if not base.endswith(".jsonl"):
                continue
            if m.size > len(best[0]):
                f = tf.extractfile(m)
                best = (f.read() if f else b"", name)
    return best


def bundle_meta(raw: bytes) -> dict:
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
            m = tf.getmember(BUNDLE_META)
            f = tf.extractfile(m)
            return json.loads(f.read().decode("utf-8")) if f else {}
    except Exception:
        return {}


def _replace_path(text: str, old: str, new: str) -> str:
    """按**路径段边界**替换，避免前缀相同的仓库被误伤：
    old=/code/api 时，/code/api-gateway 必须保持原样，不能被改成 repo-gateway。"""
    o, n = str(old or "").rstrip("/"), str(new or "").rstrip("/")
    if not o or not n or o == n:
        return text
    return re.sub(re.escape(o) + r"(?![\w.\-])", lambda _m: n, text)


def rewrite_in_bundle(raw: bytes, replacements: list[tuple[str, str]]) -> bytes:
    """在打包体内做路径重写：只改文本类成员，二进制成员原样搬运。
    返回新的 tar.gz（gzip 后）。"""
    reps = [(o, n) for o, n in sorted(replacements, key=lambda x: len(x[0]), reverse=True)
            if o and n and o != n]
    if not reps:
        return raw

    src = io.BytesIO(raw)
    out = io.BytesIO()
    with tarfile.open(fileobj=src, mode="r:gz") as tin, \
            tarfile.open(fileobj=out, mode="w:gz") as tout:
        for m in tin.getmembers():
            if not m.isfile():
                continue
            f = tin.extractfile(m)
            data = f.read() if f else b""
            if m.name.endswith(TEXT_EXT):
                text = data.decode("utf-8", "surrogateescape")
                for old, new in reps:
                    text = _replace_path(text, old, new)
                data = text.encode("utf-8", "surrogateescape")
            ti = tarfile.TarInfo(m.name)
            ti.size = len(data)
            ti.mtime = m.mtime
            ti.mode = m.mode
            tout.addfile(ti, io.BytesIO(data))
    # tarfile 的 "w:gz" 已经产出了 gzip 流，直接返回，别再压一层
    return out.getvalue()


# ------------------------------------------------------------------ 解析各源

def _ms_to_iso(v):
    """WorkBuddy 用毫秒整数；也容忍已经是 ISO 字符串的情况。"""
    if isinstance(v, (int, float)):
        try:
            from datetime import datetime, timezone
            return datetime.fromtimestamp(v / 1000, tz=timezone.utc).isoformat()
        except Exception:
            return None
    if isinstance(v, str) and v.isdigit():
        return _ms_to_iso(int(v))
    return v or None


def _active_seconds(stamps: list) -> int:
    """活跃时长：把相邻消息的时间差累加，但只算 ≤30 分钟的间隔。
    跨天/跨周续接的会话，首尾差可能几百小时，那是「挂着的时长」不是「干活的时长」。"""
    vals = sorted(s for s in stamps if isinstance(s, (int, float)))
    return int(sum(b - a for a, b in zip(vals, vals[1:]) if 0 < b - a <= 1800))


def _to_epoch(v):
    """毫秒整数 / 秒整数 / ISO 字符串 → epoch 秒。"""
    if isinstance(v, (int, float)):
        return v / 1000 if v > 1e11 else float(v)
    if isinstance(v, str):
        if v.isdigit():
            return _to_epoch(int(v))
        try:
            from datetime import datetime
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except Exception:
            return None
    return None


def parse_workbuddy(raw_jsonl: bytes) -> dict:
    """WorkBuddy 会话：用量按 (messageId, stepSeq) 去重后求和。
    注意 input_tokens 含 cache_read 部分（整段 prompt 每次请求都计费），
    cache_read_tokens 是其中的命中量，成本核算时不要重复相加。"""
    text = raw_jsonl.decode("utf-8", "replace")
    lines = [ln for ln in text.splitlines() if ln.strip()]

    inp = out = cache_read = 0
    seen: set = set()
    model = cwd = title = ""
    started = ended = None
    user_msgs = tool_calls = 0
    stamps: list = []

    for ln in lines:
        try:
            d = json.loads(ln)
        except Exception:
            continue

        cwd = cwd or d.get("cwd") or ""
        ts = d.get("timestamp")
        if ts:
            ep = _to_epoch(ts)
            if ep:
                stamps.append(ep)
            iso = _ms_to_iso(ts)
            if iso:
                started = started or iso
                ended = iso

        t = d.get("type")
        if t == "ai-title":
            title = d.get("aiTitle") or title
        elif t == "message":
            if d.get("role") == "user":
                user_msgs += 1
        elif t == "function_call":
            tool_calls += 1

        pd = d.get("providerData") or {}
        if isinstance(pd, dict) and not model:
            model = pd.get("model") or pd.get("requestModelName") or ""

        u = (d.get("message") or {}).get("usage")
        if isinstance(u, dict) and u:
            key = (pd.get("messageId"), pd.get("stepSeq")) if isinstance(pd, dict) else (None, None)
            if key == (None, None):
                key = (d.get("id"), None)
            if key not in seen:
                seen.add(key)
                inp += int(u.get("input_tokens") or 0)
                out += int(u.get("output_tokens") or 0)
                cache_read += int(u.get("cache_read_input_tokens") or 0)
        elif isinstance(pd, dict) and isinstance(pd.get("usage"), dict):
            key = (pd.get("messageId"), pd.get("stepSeq"))
            if key not in seen:
                seen.add(key)
                pu = pd["usage"]
                inp += int(pu.get("inputTokens") or 0)
                out += int(pu.get("outputTokens") or 0)

    return {
        "lines": len(lines),
        "bytes": len(raw_jsonl),
        "model": model,
        "cwd": cwd,
        "title": title,
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": 0,
        "started_at": started,
        "ended_at": ended,
        "active_seconds": _active_seconds(stamps),
        "user_messages": user_msgs,
        "tool_calls": tool_calls,
    }


def parse_transcript(raw: bytes, agent: str = "claude-code") -> dict:
    """统一入口：自动识别打包体 / WorkBuddy / Claude Code / Codex。"""
    if is_bundle(raw):
        inner, name = extract_primary_jsonl(raw)
        stats = parse_workbuddy(inner) if inner else {"lines": 0, "bytes": 0}
        stats.setdefault("model", "")
        stats.setdefault("cwd", "")
        stats.setdefault("input_tokens", 0)
        stats.setdefault("output_tokens", 0)
        stats.setdefault("cache_read_tokens", 0)
        stats.setdefault("cache_creation_tokens", 0)
        stats.setdefault("started_at", None)
        stats.setdefault("ended_at", None)
        stats.setdefault("active_seconds", 0)
        stats["bytes"] = len(raw)          # 体积按打包体算（含旁挂文件）
        stats["bundle"] = True
        stats["primary_member"] = name
        stats["members"] = len(bundle_members(raw))
        return stats

    if agent == "workbuddy" or _looks_like_workbuddy(raw):
        stats = parse_workbuddy(raw)
        stats["bundle"] = False
        return stats

    return _parse_claude_or_codex(raw)


def _looks_like_workbuddy(raw: bytes) -> bool:
    head = raw[:2000].decode("utf-8", "replace")
    return '"type": "session-meta"' in head or '"type":"session-meta"' in head


def _parse_claude_or_codex(raw: bytes) -> dict:
    text = raw.decode("utf-8", "replace")
    lines = [ln for ln in text.splitlines() if ln.strip()]

    inp = out = cache_read = cache_write = 0
    model = cwd = ""
    started = ended = None
    stamps: list = []

    for ln in lines:
        try:
            d = json.loads(ln)
        except Exception:
            continue

        ts = d.get("timestamp") or (d.get("payload") or {}).get("timestamp")
        if ts:
            started = started or ts
            ended = ts
            ep = _to_epoch(ts)
            if ep:
                stamps.append(ep)

        cwd = cwd or d.get("cwd") or (d.get("payload") or {}).get("cwd") or ""

        msg = d.get("message") or {}
        if isinstance(msg, dict):
            model = model or msg.get("model") or ""
            u = msg.get("usage") or {}
            if isinstance(u, dict) and u:
                inp += int(u.get("input_tokens") or 0)
                out += int(u.get("output_tokens") or 0)
                cache_read += int(u.get("cache_read_input_tokens") or 0)
                cache_write += int(u.get("cache_creation_input_tokens") or 0)

        # Codex：累计用量取最大值（每轮是累计值，不是增量）
        p = d.get("payload") or {}
        if isinstance(p, dict) and p.get("type") == "token_count":
            total = ((p.get("info") or {}).get("total_token_usage") or {})
            if total:
                inp = max(inp, int(total.get("input_tokens") or 0))
                out = max(out, int(total.get("output_tokens") or 0))

    return {
        "lines": len(lines),
        "bytes": len(raw),
        "model": model,
        "cwd": cwd,
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": cache_write,
        "started_at": started,
        "ended_at": ended,
        "active_seconds": _active_seconds(stamps),
        "bundle": False,
    }


def rewrite_paths(raw: bytes, replacements: list[tuple[str, str]]) -> bytes:
    """把会话内的绝对路径整体替换（含 cwd 字段与工具输出里的路径）。
    先长后短替换，避免前缀互相吃掉；按路径段边界匹配，避免误伤同前缀仓库。"""
    text = raw.decode("utf-8", "surrogateescape")
    for old, new in sorted(replacements, key=lambda x: len(x[0]), reverse=True):
        text = _replace_path(text, old, new)
    return text.encode("utf-8", "surrogateescape")


# --------------------------------------------------------------------- 差异
#
# 会话文件基本是「只追加」的，所以先走快路径：新版前 N 行和旧版完全一致 → 纯追加。
# 不一致再退到序列比对，避免每行都跑 difflib（30MB 文件会卡）。

FILE_KEYS = ("file_path", "path", "notebook_path", "target_file")


def _lines_of(raw: bytes, agent: str = "") -> list[str]:
    if is_bundle(raw):
        raw, _ = extract_primary_jsonl(raw)
    text = raw.decode("utf-8", "replace")
    return [ln for ln in text.splitlines() if ln.strip()]


def _summarize_added(added: list[str]) -> dict:
    """从新增行里读出「语义变化」：几条用户消息、几次工具调用、碰了哪些文件。"""
    user_msgs = tool_calls = 0
    files: list[str] = []
    names: dict[str, int] = {}
    preview: list[str] = []

    for ln in added:
        try:
            d = json.loads(ln)
        except Exception:
            continue
        t = d.get("type")
        if t == "message" and d.get("role") == "user":
            user_msgs += 1
            if len(preview) < 3:
                for c in d.get("content") or []:
                    if isinstance(c, dict) and c.get("type") == "input_text":
                        preview.append(c.get("text", "")[:120].replace("\n", " "))
                        break
        elif t == "user":
            user_msgs += 1
        elif t == "function_call":
            tool_calls += 1
            names[d.get("name") or "?"] = names.get(d.get("name") or "?", 0) + 1
            try:
                args = json.loads(d.get("arguments") or "{}")
            except Exception:
                args = {}
            for k in FILE_KEYS:
                if isinstance(args, dict) and args.get(k):
                    files.append(str(args[k]))
                    break
        elif t == "assistant":
            for c in ((d.get("message") or {}).get("content") or []):
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    tool_calls += 1
                    names[c.get("name") or "?"] = names.get(c.get("name") or "?", 0) + 1
                    inp = c.get("input") or {}
                    for k in FILE_KEYS:
                        if inp.get(k):
                            files.append(str(inp[k]))
                            break

    uniq = list(dict.fromkeys(files))
    return {
        "user_messages": user_msgs,
        "tool_calls": tool_calls,
        "files": uniq[:20],
        "files_count": len(uniq),
        "tools": dict(sorted(names.items(), key=lambda kv: -kv[1])[:8]),
        "preview": preview,
    }


def diff_transcripts(old_raw: bytes, new_raw: bytes) -> dict:
    """返回 {added, removed, added_lines, summary}。
    added_lines 只留最多 200 行原文，供服务端做 diff 展示。"""
    old_l = _lines_of(old_raw) if old_raw else []
    new_l = _lines_of(new_raw) if new_raw else []

    added: list[str] = []
    removed = 0

    if new_l[:len(old_l)] == old_l:
        added = new_l[len(old_l):]          # 纯追加：最常见
    else:
        import difflib
        sm = difflib.SequenceMatcher(None, old_l, new_l, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag in ("replace", "insert"):
                added.extend(new_l[j1:j2])
            if tag in ("replace", "delete"):
                removed += i2 - i1

    return {
        "added": len(added),
        "removed": removed,
        "summary": _summarize_added(added),
        "added_lines": added[:200],
    }


def session_slug(path: str) -> str:
    """Claude Code 的项目目录名：绝对路径的 '/' 全部换成 '-'。"""
    return path.rstrip("/").replace("/", "-")


def workbuddy_slug(path: str) -> str:
    """WorkBuddy 的项目目录名：去掉开头的 '/'，再把 '/' 换成 '-'。
    实测 '/Users/alice/WorkBuddy/2026-01-01-00-00-00'
       → 'Users-alice-WorkBuddy-2026-01-01-00-00-00'（点号保留）"""
    return path.strip("/").replace("/", "-")
