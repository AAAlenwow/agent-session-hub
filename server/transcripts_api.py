"""会话原件层 API：上传归档、跨机下载（含路径重写）、占用锁、用量统计、版本历史。"""
import gzip
import hashlib
import json
import re
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from auth import current_user as auth
from privacy import redact
from leases import fencing_check
from transcripts_parse import (
    bundle_guard,
    decompress_limited,
    diff_transcripts,
    is_bundle,
    parse_transcript,
    rewrite_in_bundle,
    rewrite_paths,
)

router = APIRouter()

# ext_id 会进响应头与 Content-Disposition，必须限制字符集（防头注入与引号破坏）
_EXT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _cfg(request: Request) -> dict:
    return (getattr(request.app.state, "config", {}) or {}).get("transcripts", {})


def _lease_cfg(request: Request) -> dict:
    return (getattr(request.app.state, "config", {}) or {}).get("lease", {}) or {}


def _fence_header(response: Optional[Response], warning: Optional[str]) -> None:
    """warn 模式把栅栏告警放进响应头：不拒写入，但让调用方知道自己可能已经失去执行权。

    HTTP 头只能是 latin-1，而告警里会带机器名（可能是中文），所以整体做百分号编码；
    客户端用 urllib.parse.unquote 还原。
    """
    if response is not None and warning:
        response.headers["X-Lease-Warning"] = quote(_hdr(warning[:300]), safe="")


def _ts(value):
    """ISO 字符串 → datetime（asyncpg 需要 datetime 对象）；非法值按 NULL 处理。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _hdr(v) -> str:
    """响应头取值净化：去掉换行与引号，防头注入、防破坏 Content-Disposition。"""
    return str(v or "").replace("\r", "").replace("\n", "").replace('"', "")


def _q(v: str) -> str:
    """query 参数净化：剥 NUL（入库 text 列会 500）。"""
    return (v or "").replace("\x00", "")


async def _read_body(request: Request, limit: int) -> bytes:
    """带上限地读取请求体。

    不能用 `await request.body()`：它会无上限地把整个流读进内存，
    一个异常大的请求就能把服务打挂。这里边读边计数，超限立即断开。"""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(413, f"上传体积超过上限 {limit // 1048576}MB")

    buf = bytearray()
    async for chunk in request.stream():
        buf.extend(chunk)
        if len(buf) > limit:
            raise HTTPException(413, f"上传体积超过上限 {limit // 1048576}MB")
    return bytes(buf)


@router.post("/v1/transcripts")
async def upload_transcript(
    request: Request,
    response: Response,
    agent: str = "claude-code",
    ext_id: str = "",
    cwd: str = "",
    project: str = "",
    machine: str = "",
    message: str = "",
    lease_generation: Optional[int] = None,
    user=Depends(auth),
):
    """上传一份会话原件（JSONL 或 tar.gz 打包体）= 一次 git 式提交。
    与上一版内容相同则不产生新版本（幂等），返回 unchanged=true。"""
    cfg = _cfg(request)
    if not cfg.get("enabled", False):
        raise HTTPException(403, "原件层未开启：请在 hub-config.yaml 设置 transcripts.enabled: true")
    if not ext_id:
        raise HTTPException(400, "ext_id required")
    if not _EXT_ID_RE.match(ext_id):
        raise HTTPException(400, "ext_id 只允许字母数字与 . _ -（1~64 位）")
    cwd, project, message = _q(cwd), _q(project), _q(message)
    # machine 是元数据（非会话正文），与事件层同一套脱敏口径
    machine = redact(_q(machine))

    max_bytes = int(cfg.get("max_mb", 32)) * 1024 * 1024
    # 压缩态的体积上限与展开态一致：gzip 只会更小，超过就说明请求本身不正常
    body = await _read_body(request, max_bytes)

    # WorkBuddy 那种「jsonl + 旁挂 tool-results」的 tar.gz 打包体必须整体保留，
    # 不能走解压（否则会被拆成裸 tar，回放时旁挂文件全丢）
    try:
        raw = body if is_bundle(body) else decompress_limited(body, max_bytes)
        # 判的是**最终要入库的内容**：外面再套一层 gzip 的打包体同样要限量，
        # 否则「gzip(gzip(100MB 零字节))」这种就能绕过守卫。
        if is_bundle(raw):
            bundle_guard(raw, max_bytes)      # 成员数 / 单成员 / 总展开量都要限量
    except ValueError as e:
        raise HTTPException(413, str(e))

    stats = parse_transcript(raw, agent)
    digest = hashlib.sha256(raw).hexdigest()
    pool = request.app.state.pool

    # 版本分配必须串行化：读 prev.rev → 算 new_rev → 写，全程在同一事务的
    # advisory lock 之内。否则两个并发上传会拿到同一个 rev，
    # on conflict 静默吞掉一份内容，而两个请求都收到 200（API 撒谎）。
    # 栅栏：这条会话若正被别人执行，写入可能是「两个进程同时改同一份上下文」。
    # 锁顺序固定为 lease → t，与 acquire/transfer 一致，避免与其他路径死锁。
    fencing = str(_lease_cfg(request).get("fencing", "warn")).lower()
    lease_key = f"{agent}:{ext_id}"
    lease_warning: Optional[str] = None

    async with pool.acquire() as conn:
        async with conn.transaction():
            if fencing != "off":
                await conn.execute(
                    "select pg_advisory_xact_lock(hashtext($1))", f"lease:{lease_key}")
                lease_warning = await fencing_check(conn, lease_key, lease_generation)
                if lease_warning and fencing == "strict":
                    raise HTTPException(409, detail={
                        "error": "session is being executed elsewhere",
                        "detail": lease_warning,
                        "hint": "先 acquire 该会话的租约（会拿到新的 generation），再带 lease_generation 上传",
                    })
                _fence_header(response, lease_warning)
            await conn.execute(
                "select pg_advisory_xact_lock(hashtext($1))",
                f"t:{user['id']}:{agent}:{ext_id}",
            )
            sess = await conn.fetchrow(
                "select id from sessions where user_id = $1 and ext_id = $2",
                user["id"], ext_id,
            )
            prev = await conn.fetchrow(
                "select id, rev, sha256, content_gz from transcripts "
                "where user_id = $1 and ext_id = $2",
                user["id"], ext_id,
            )

            # 内容没变就不提交（避免每次点同步都长出一堆空提交）
            if prev and prev["sha256"] == digest:
                return {"ok": True, "unchanged": True, "transcript_id": prev["id"],
                        "rev": prev["rev"], "lines": stats["lines"],
                        "bundle": bool(stats.get("bundle"))}

            old_raw = gzip.decompress(prev["content_gz"]) if prev else b""
            d = diff_transcripts(old_raw, raw) if prev else {
                "added": stats["lines"], "removed": 0,
                "summary": {"user_messages": 0, "tool_calls": 0, "files": [],
                            "tools": {}, "preview": []},
                "added_lines": [],
            }
            new_rev = int(prev["rev"]) + 1 if prev else 1

            row = await conn.fetchrow(
                """
                insert into transcripts
                    (session_id, user_id, agent, ext_id, cwd, model, bytes, lines,
                     sha256, content_gz, project, machine,
                     input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
                     started_at, ended_at, bundle, members, active_seconds, rev)
                values ($1, $2, $3, $4, $5, $6, $7, $8,
                        $9, $10, $11, $12, $13, $14, $15, $16,
                        $17::timestamptz, $18::timestamptz, $19, $20, $21, $22)
                on conflict (user_id, ext_id) do update set
                    session_id   = coalesce(excluded.session_id, transcripts.session_id),
                    cwd          = excluded.cwd,
                    model        = excluded.model,
                    bytes        = excluded.bytes,
                    lines        = excluded.lines,
                    sha256       = excluded.sha256,
                    content_gz   = excluded.content_gz,
                    project      = coalesce(nullif(excluded.project, ''), transcripts.project),
                    machine      = coalesce(nullif(excluded.machine, ''), transcripts.machine),
                    input_tokens = excluded.input_tokens,
                    output_tokens= excluded.output_tokens,
                    cache_read_tokens     = excluded.cache_read_tokens,
                    cache_creation_tokens = excluded.cache_creation_tokens,
                    started_at   = excluded.started_at,
                    ended_at     = excluded.ended_at,
                    bundle       = excluded.bundle,
                    members      = excluded.members,
                    active_seconds = excluded.active_seconds,
                    rev          = excluded.rev,
                    created_at   = now()
                returning id
                """,
                sess["id"] if sess else None, user["id"], agent, ext_id,
                stats.get("cwd") or cwd, stats.get("model") or "", stats["bytes"],
                stats["lines"], digest, gzip.compress(raw), project, machine,
                stats["input_tokens"], stats["output_tokens"],
                stats["cache_read_tokens"], stats["cache_creation_tokens"],
                _ts(stats["started_at"]), _ts(stats["ended_at"]),
                bool(stats.get("bundle")), int(stats.get("members") or 0),
                int(stats.get("active_seconds") or 0), new_rev,
            )

            # 在锁内不会撞 unique(transcript_id, rev)；万一撞了让事务回滚报 500，
            # 也绝不能 on conflict do nothing 静默吞版（那是"提交成功但内容不存在"的谎言）
            await conn.execute(
                """
                insert into transcript_revisions
                    (transcript_id, rev, user_id, message, sha256, content_gz, bytes, lines,
                     input_tokens, output_tokens, cache_read_tokens,
                     diff_added, diff_removed, diff)
                values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14::jsonb)
                """,
                row["id"], new_rev, user["id"], message[:500], digest, gzip.compress(raw),
                stats["bytes"], stats["lines"], stats["input_tokens"], stats["output_tokens"],
                stats["cache_read_tokens"], d["added"], d["removed"],
                json.dumps({**d["summary"], "added_lines": d["added_lines"]},
                           ensure_ascii=False),
            )

            # 历史裁剪：保留最近 keep_revisions 个 + 最早的基线，避免体积失控
            keep = int(cfg.get("keep_revisions", 20))
            await conn.execute(
                "delete from transcript_revisions where transcript_id = $1 and rev > 1 "
                "and rev <= $2",
                row["id"], new_rev - keep,
            )

    _fence_header(response, lease_warning)
    return {
        "ok": True,
        "transcript_id": row["id"],
        "rev": new_rev,
        "previous_rev": int(prev["rev"]) if prev else 0,
        "diff": {"added": d["added"], "removed": d["removed"], **d["summary"]},
        "lines": stats["lines"],
        "input_tokens": stats["input_tokens"],
        "output_tokens": stats["output_tokens"],
        "bundle": bool(stats.get("bundle")),
        "members": stats.get("members"),
        "resume_hint": f"agenthub pull {row['id']} --into <本机同一仓库路径>",
    }


def _jsonb(v):
    """asyncpg 把 jsonb 以字符串返回，这里统一转成对象。"""
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return {}
    return v or {}


@router.get("/v1/transcripts/{tid}/revisions")
async def list_revisions(tid: int, request: Request, user=Depends(auth)):
    """提交历史（git log）：谁、什么时候、改了多少、提交说明。"""
    pool = request.app.state.pool
    rows = await pool.fetch(
        """
        select r.rev, r.message, r.bytes, r.lines,
               r.input_tokens, r.output_tokens, r.cache_read_tokens,
               r.diff_added, r.diff_removed,
               r.diff - 'added_lines' as summary,
               r.created_at, u.name as author
        from transcript_revisions r
        join users u on u.id = r.user_id
        where r.transcript_id = $1
        order by r.rev desc
        """,
        tid,
    )
    head = await pool.fetchval("select rev from transcripts where id = $1", tid)
    out = []
    for r in rows:
        d = dict(r)
        d["summary"] = _jsonb(d.get("summary"))
        out.append(d)
    return {"head": head, "revisions": out}


@router.get("/v1/transcripts/{tid}/diff")
async def diff_revisions(tid: int, request: Request, from_rev: int = 0, to_rev: int = 0,
                         user=Depends(auth)):
    """两个版本之间的差异（默认：上一版 → 当前版）。"""
    pool = request.app.state.pool
    head = await pool.fetchval("select rev from transcripts where id = $1", tid)
    if not head:
        raise HTTPException(404, "transcript not found")
    to_rev = to_rev or head
    from_rev = from_rev or max(1, to_rev - 1)

    if from_rev == to_rev:
        return {"from": from_rev, "to": to_rev, "added": 0, "removed": 0,
                "summary": {}, "added_lines": []}

    rows = await pool.fetch(
        "select rev, content_gz from transcript_revisions "
        "where transcript_id = $1 and rev = any($2::int[])",
        tid, [from_rev, to_rev],
    )
    by_rev = {r["rev"]: gzip.decompress(r["content_gz"]) for r in rows}
    if to_rev not in by_rev:
        raise HTTPException(404, f"revision {to_rev} 不存在（可能已被历史裁剪）")

    d = diff_transcripts(by_rev.get(from_rev, b""), by_rev[to_rev])
    return {"from": from_rev, "to": to_rev, **d}


@router.post("/v1/transcripts/{tid}/revert")
async def revert_revision(tid: int, request: Request, revision: int,
                          message: str = "", user=Depends(auth)):
    """回滚 = 把某个历史版本的内容作为新 HEAD 再提交一次（不删历史，可再回滚回来）。

    这是**破坏性写操作**（会覆盖 HEAD 内容），只允许会话归属人本人或管理员执行。"""
    pool = request.app.state.pool
    tr = await pool.fetchrow("select id, rev, user_id, agent, ext_id from transcripts where id = $1", tid)
    if not tr:
        raise HTTPException(404, "transcript not found")
    if tr["user_id"] != user["id"] and not user["is_admin"]:
        raise HTTPException(403, "只能回滚自己的会话（或由管理员执行）")
    old = await pool.fetchrow(
        "select * from transcript_revisions where transcript_id = $1 and rev = $2", tid, revision
    )
    if not old:
        raise HTTPException(404, f"revision {revision} 不存在")

    raw = gzip.decompress(old["content_gz"])
    stats = parse_transcript(raw, tr["agent"] or "workbuddy")
    digest = hashlib.sha256(raw).hexdigest()
    msg = (message or f"回滚到 r{revision}")[:500]

    # 与上传共用同一把 advisory lock：并发 revert/upload 不能各算各的 rev
    #（否则 INSERT 撞 unique(transcript_id, rev) 直接 500，且状态不确定）
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "select pg_advisory_xact_lock(hashtext($1))",
                f"t:{tr['user_id']}:{tr['agent']}:{tr['ext_id']}",
            )
            head_rev = await conn.fetchval("select rev from transcripts where id = $1", tid)
            new_rev = int(head_rev) + 1
            await conn.execute(
                """
                update transcripts set rev = $2, sha256 = $3, content_gz = $4, bytes = $5,
                       lines = $6, input_tokens = $7, output_tokens = $8,
                       cache_read_tokens = $9, created_at = now()
                where id = $1
                """,
                tid, new_rev, digest, old["content_gz"], old["bytes"], old["lines"],
                old["input_tokens"], old["output_tokens"], old["cache_read_tokens"],
            )
            await conn.execute(
                """
                insert into transcript_revisions
                    (transcript_id, rev, user_id, message, sha256, content_gz, bytes, lines,
                     input_tokens, output_tokens, cache_read_tokens, diff_added, diff_removed, diff)
                values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, 0, 0, $12::jsonb)
                """,
                tid, new_rev, user["id"], msg, digest, old["content_gz"], old["bytes"],
                old["lines"], old["input_tokens"], old["output_tokens"],
                old["cache_read_tokens"],
                json.dumps({"reverted_from": revision}, ensure_ascii=False),
            )
    return {"ok": True, "rev": new_rev, "reverted_from": revision,
            "lines": stats.get("lines", old["lines"])}


@router.get("/v1/transcripts")
async def list_transcripts(request: Request, limit: int = 50, project: str = "",
                           user=Depends(auth)):
    """原件清单（不含内容）：谁、哪个 agent、多少 token、多长时间。
    项目归属优先取元件自己的 project（wb-sync 只传原件不报事件），
    没有再回退 session 的 project。"""
    q = """
        select t.id, t.agent, t.ext_id, t.cwd, t.model, t.lines, t.bytes,
               t.bundle, t.members, t.rev,
               t.input_tokens, t.output_tokens, t.cache_read_tokens, t.cache_creation_tokens,
               t.started_at, t.ended_at, t.active_seconds, t.created_at,
               coalesce(extract(epoch from (t.ended_at - t.started_at)), 0)::bigint as wall_seconds,
               u.name as owner,
               coalesce(nullif(t.project, ''), s.project) as project,
               coalesce(nullif(t.machine, ''), s.machine, '') as machine
        from transcripts t
        join users u on u.id = t.user_id
        left join sessions s on s.id = t.session_id
        where 1 = 1
    """
    args: list = []
    if project:
        args.append(project)
        q += f" and coalesce(nullif(t.project, ''), s.project) = ${len(args)}"
    args.append(max(1, min(limit, 200)))
    q += f" order by t.created_at desc limit ${len(args)}"
    rows = await request.app.state.pool.fetch(q, *args)
    return [dict(r) for r in rows]


@router.get("/v1/transcripts/{tid}")
async def download_transcript(
    tid: int,
    request: Request,
    rewrite_from: str = Query("", description="源机器上的绝对路径"),
    cwd: str = Query("", description="目标机器上的仓库绝对路径"),
    revision: int = Query(0, description="取某个历史版本（0 = 当前 HEAD）"),
    user=Depends(auth),
):
    """下载原件。带 rewrite_from + cwd 时，服务端把会话里的旧路径整体改写成新路径，
    直接可用于 `claude --resume`。带 revision 时取历史版本（回滚用）。"""
    cfg = _cfg(request)
    pool = request.app.state.pool
    row = await pool.fetchrow(
        "select * from transcripts where id = $1", tid
    )
    if not row:
        raise HTTPException(404, "transcript not found")
    if not cfg.get("allow_cross_user_pull", True) and row["user_id"] != user["id"]:
        raise HTTPException(403, "本部署不允许跨用户回放")

    claimed = await pool.fetchrow(
        "select u.name as owner, rc.expires_at from resume_claims rc "
        "join users u on u.id = rc.user_id "
        "where rc.transcript_id = $1 and rc.expires_at > now() "
        "and rc.user_id <> $2",
        tid, user["id"],
    )
    if claimed:
        raise HTTPException(
            409,
            f"该会话正由 {claimed['owner']} 占用回放（至 {claimed['expires_at']}）。"
            "同一会话同时两台机器 resume 会导致双方上下文错乱。",
        )

    served_rev = int(row["rev"] or 1)
    if revision:
        hist = await pool.fetchrow(
            "select content_gz, rev from transcript_revisions "
            "where transcript_id = $1 and rev = $2",
            tid, revision,
        )
        if not hist:
            raise HTTPException(404, f"revision {revision} 不存在（可能已被历史裁剪）")
        raw = gzip.decompress(hist["content_gz"])
        served_rev = int(hist["rev"])
    else:
        raw = gzip.decompress(row["content_gz"])

    old = rewrite_from or row["cwd"]
    bundle = is_bundle(raw)
    # 响应头里带的是历史数据（旧版本上传的 ext_id 可能没经过校验），统一净化
    ext_hdr = _hdr(row["ext_id"])
    cwd_hdr = _hdr(row["cwd"])

    if bundle:
        # WorkBuddy：整包重写（jsonl + tool-results + _agenthub/meta.json 里的 cwd 一起改）
        if cwd and old:
            raw = rewrite_in_bundle(raw, [(old, cwd)])
        return Response(
            content=raw,
            media_type="application/gzip",
            headers={
                "Content-Disposition": f'attachment; filename="{ext_hdr}.tar.gz"',
                "X-Ext-Id": ext_hdr,
                "X-Agent": _hdr(row["agent"]),
                "X-Origin-Cwd": cwd_hdr,
                "X-Lines": str(row["lines"]),
                "X-Rev": str(served_rev),
                "X-Bundle": "tar.gz",
                "X-Input-Tokens": str(row["input_tokens"]),
                "X-Output-Tokens": str(row["output_tokens"]),
            },
        )

    if cwd and old:
        raw = rewrite_paths(raw, [(old, cwd)])

    return Response(
        content=raw,
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": f'attachment; filename="{ext_hdr}.jsonl"',
            "X-Ext-Id": ext_hdr,
            "X-Agent": _hdr(row["agent"]),
            "X-Origin-Cwd": cwd_hdr,
            "X-Lines": str(row["lines"]),
            "X-Rev": str(served_rev),
            "X-Input-Tokens": str(row["input_tokens"]),
            "X-Output-Tokens": str(row["output_tokens"]),
        },
    )


@router.post("/v1/transcripts/{tid}/claim")
async def claim_transcript(tid: int, request: Request, user=Depends(auth)):
    """占用回放权（默认 24h），避免两人同时 resume 同一会话。

    权限模型（锁挡得住成员、挡不住归属人）：
      · 归属人 / 管理员：随时可占用，含从别人手里**夺回**——恶意占用锁不死归属人；
        也可用 DELETE /claim 解铃，不必干等 24h。
      · 其他成员：仅当部署允许跨用户回放（交接场景：新负责人接手前任会话）时可占用，
        且不能抢占现有持有者。"""
    cfg = _cfg(request)
    pool = request.app.state.pool
    row = await pool.fetchrow(
        "select agent, ext_id, user_id from transcripts where id = $1", tid
    )
    if not row:
        raise HTTPException(404, "transcript not found")
    privileged = user["is_admin"] or row["user_id"] == user["id"]
    if not privileged and not cfg.get("allow_cross_user_pull", True):
        raise HTTPException(403, "本部署不允许跨用户回放，不能占用他人的会话")

    ttl = int(cfg.get("claim_ttl_hours", 24))
    expires = datetime.now().astimezone() + timedelta(hours=ttl)
    # 查-判-写必须原子：否则两个成员同时 claim 都通过检查、都拿到 200，
    # 先到的实际已静默丢锁（后者 upsert 覆盖）
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("select pg_advisory_xact_lock(hashtext($1))", f"claim:{tid}")
            existing = await conn.fetchrow(
                """
                select rc.user_id, rc.expires_at, u.name
                from resume_claims rc join users u on u.id = rc.user_id
                where rc.transcript_id = $1 and rc.expires_at > now()
                """,
                tid,
            )
            if existing and existing["user_id"] != user["id"] and not privileged:
                raise HTTPException(
                    409, f"{existing['name']} 正在回放该会话，至 {existing['expires_at']}")
            await conn.execute(
                """
                insert into resume_claims (transcript_id, agent, ext_id, user_id, expires_at)
                values ($1, $2, $3, $4, $5)
                on conflict (transcript_id) do update
                    set agent = excluded.agent, ext_id = excluded.ext_id,
                        user_id = excluded.user_id, claimed_at = now(),
                        expires_at = excluded.expires_at
                """,
                tid, row["agent"], row["ext_id"], user["id"], expires,
            )
    return {"ok": True, "expires_at": expires.isoformat(),
            "install_hint": f"agenthub pull {tid} --into <本机同一仓库路径>"}


@router.delete("/v1/transcripts/{tid}/claim")
async def release_claim(tid: int, request: Request, user=Depends(auth)):
    """解除占用（解铃）。归属人或管理员可解除，过期接管前不用干等 24h。"""
    pool = request.app.state.pool
    row = await pool.fetchrow("select user_id from transcripts where id = $1", tid)
    if not row:
        raise HTTPException(404, "transcript not found")
    if row["user_id"] != user["id"] and not user["is_admin"]:
        raise HTTPException(403, "只有会话归属人（或管理员）能解除占用")
    status = await pool.execute("delete from resume_claims where transcript_id = $1", tid)
    return {"ok": True, "released": status != "DELETE 0"}


@router.get("/v1/usage")
async def usage(request: Request, days: int = 30, project: str = "", user=Depends(auth)):
    """Token 与时长用量：按人 / 按 agent 聚合。时长是会话首尾时间差之和（墙上时间）。"""
    pool = request.app.state.pool
    q = """
        select u.name as owner, t.agent,
               count(*) as transcripts,
               sum(t.input_tokens) as input_tokens,
               sum(t.output_tokens) as output_tokens,
               sum(t.cache_read_tokens) as cache_read_tokens,
               sum(t.cache_creation_tokens) as cache_creation_tokens,
               coalesce(sum(extract(epoch from (t.ended_at - t.started_at))), 0)::bigint as wall_seconds,
               coalesce(sum(t.active_seconds), 0)::bigint as active_seconds,
               sum(t.lines) as lines,
               max(t.created_at) as last_upload
        from transcripts t
        join users u on u.id = t.user_id
        left join sessions s on s.id = t.session_id
        where t.created_at > now() - make_interval(days => $1)
    """
    args: list = [days]
    if project:
        args.append(project)
        q += f" and coalesce(nullif(t.project, ''), s.project) = ${len(args)}"
    q += " group by u.name, t.agent order by input_tokens desc"
    rows = await pool.fetch(q, *args)
    return [dict(r) for r in rows]
