"""会话执行权租约（session lease）。

解决的问题：agent-session-hub 原本只能回答「谁干过什么」（事后记忆），
但组织里真正的痛点是「这条会话现在归谁执行」——一个人在本机跑到一半，
换机器/换人接手时，两边都可能同时在写同一份会话，上下文互相覆盖。

租约把「执行权」变成服务端的显式状态：

  · 同一会话同一时刻只有一个 exec 持有者（其他人拿到 409，能看到是谁、到几点）
  · generation 是 fencing token：所有权每变一次就 +1，旧进程即使还在跑，
    带着过期 generation 的续租/写入会被拒（防脑裂）
  · 续租而不是硬过期：长任务不会在 24h 被静默夺走
  · transfer 是原子交接：holder 或归属人/管理员可以主动交给下一台机器/下一个人

与 resume_claims 的分工：
  · claim  = 「我要在这台机器装一份来 resume」（回放占用，24h）
  · lease  = 「我正在跑这个会话」（执行权 + fencing）
两者并存；claim 是 lease mode='read' 的历史实现，后续版本会收敛。
"""
from datetime import datetime, timedelta, timezone
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from auth import current_user as auth
from privacy import redact

router = APIRouter()

DEFAULT_TTL = 3600
MAX_TTL = 86400
_KEY_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}:[A-Za-z0-9._-]{1,64}$")


def _cfg(request: Request) -> dict:
    cfg = getattr(request.app.state, "config", None) or {}
    return cfg.get("lease", {}) or {}


def _ttl(request: Request, asked: Optional[int]) -> int:
    cfg = _cfg(request)
    default = int(cfg.get("ttl_seconds", DEFAULT_TTL))
    ceiling = int(cfg.get("max_ttl_seconds", MAX_TTL))
    ttl = int(asked or default)
    if ttl <= 0:
        raise HTTPException(400, "ttl_seconds 必须为正")
    return min(ttl, ceiling)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _norm_key(agent: str, ext_id: str) -> str:
    key = f"{agent}:{ext_id}"
    if not _KEY_RE.match(key):
        raise HTTPException(400, "session_key 需形如 agent:ext_id，只允许字母数字与 . _ -")
    return key


def _lease_row(r) -> dict:
    return {
        "session_key": r["session_key"],
        "holder_id": r["holder_id"],
        "holder": r["user_name"],
        "machine": r["machine"],
        "agent": r["agent"],
        "mode": r["mode"],
        "generation": int(r["generation"]),
        "note": r["note"],
        "acquired_at": r["acquired_at"].isoformat(),
        "renewed_at": r["renewed_at"].isoformat(),
        "expires_at": r["expires_at"].isoformat(),
        "ttl_left_seconds": max(0, int((r["expires_at"] - _now()).total_seconds())),
    }


class AcquireIn(BaseModel):
    session_key: str = ""
    agent: str = ""
    ext_id: str = ""
    transcript_id: Optional[int] = None
    holder_id: str
    machine: str = ""
    mode: str = "exec"
    ttl_seconds: Optional[int] = None
    note: str = ""
    force: bool = False


class RenewIn(BaseModel):
    holder_id: str
    generation: int
    ttl_seconds: Optional[int] = None


class TransferIn(BaseModel):
    to_holder_id: str
    to_machine: str = ""
    ttl_seconds: Optional[int] = None
    note: str = ""


@router.post("/v1/leases")
async def acquire_lease(body: AcquireIn, request: Request, user=Depends(auth)):
    """申请执行权。同一 holder 重复申请＝续租（generation 不变）。

    被别人持有时返回 409，并告诉你是谁、到几点、generation 多少，
    调用方可以据此「等」或者（归属人/管理员）用 force=true 夺回。
    """
    if body.mode not in ("exec", "read"):
        raise HTTPException(400, "mode 只能是 exec 或 read")
    if not body.holder_id:
        raise HTTPException(400, "holder_id 必填（建议 hostname:pid 或 runner id）")

    # 服务端自己从 transcript_id 推导 session_key：客户端不该为了拿一个键
    # 去猜 agent/ext_id（/v1/transcripts/{id} 返回的是文件流，不是元数据）
    pool = request.app.state.pool
    owner_id = None

    if body.transcript_id is not None:
        ref = await pool.fetchrow(
            "select agent, ext_id, user_id from transcripts where id = $1",
            body.transcript_id,
        )
        if not ref:
            raise HTTPException(404, "transcript not found")
        owner_id = ref["user_id"]
        key = body.session_key or _norm_key(ref["agent"], ref["ext_id"])
    else:
        key = body.session_key or _norm_key(body.agent or "claude-code", body.ext_id)
        agent, ext_id = key.split(":", 1)
        ref = await pool.fetchrow(
            "select user_id from sessions where agent = $1 and ext_id = $2 "
            "order by updated_at desc limit 1", agent, ext_id,
        )
        if ref:
            owner_id = ref["user_id"]

    if not _KEY_RE.match(key):
        raise HTTPException(400, "session_key 需形如 agent:ext_id")

    ttl = _ttl(request, body.ttl_seconds)
    expires = _now() + timedelta(seconds=ttl)

    privileged = bool(user["is_admin"]) or (owner_id is not None and owner_id == user["id"])

    async with pool.acquire() as conn:
        async with conn.transaction():
            # 租约表的读写必须串行：否则两个成员同时 acquire 都能通过检查、
            # 都拿到 200，后到的静默覆盖前者（和 claim 同一个坑）
            await conn.execute("select pg_advisory_xact_lock(hashtext($1))", f"lease:{key}")
            cur = await conn.fetchrow(
                """
                select l.*, u.name as user_name
                from session_leases l join users u on u.id = l.user_id
                where l.session_key = $1
                """, key,
            )
            active = cur is not None and cur["expires_at"] > _now()

            if active and cur["holder_id"] != body.holder_id:
                if not (body.force and privileged):
                    raise HTTPException(409, detail={
                        "error": "session is leased",
                        "holder_id": cur["holder_id"],
                        "holder": cur["user_name"],
                        "machine": cur["machine"],
                        "mode": cur["mode"],
                        "generation": int(cur["generation"]),
                        "expires_at": cur["expires_at"].isoformat(),
                        "ttl_left_seconds": max(0, int(
                            (cur["expires_at"] - _now()).total_seconds())),
                        "hint": "等它释放，或由归属人/管理员带 force=true 夺回",
                    })
                gen = int(cur["generation"]) + 1          # 夺取：bump，旧进程的写入被 fence
            elif active:                                   # 自己持有 → 续租
                gen = int(cur["generation"])
            else:                                          # 无锁或已过期 → 接过来
                gen = int(cur["generation"]) + 1 if cur else 1

            row = await conn.fetchrow(
                """
                insert into session_leases
                    (session_key, transcript_id, user_id, holder_id, machine, agent,
                     mode, generation, note, acquired_at, renewed_at, expires_at)
                values ($1, $2, $3, $4, $5, $6, $7, $8, $9, now(), now(), $10)
                on conflict (session_key) do update set
                    transcript_id = coalesce(excluded.transcript_id, session_leases.transcript_id),
                    user_id       = excluded.user_id,
                    holder_id     = excluded.holder_id,
                    machine       = excluded.machine,
                    agent         = excluded.agent,
                    mode          = excluded.mode,
                    generation    = excluded.generation,
                    note          = excluded.note,
                    acquired_at   = case when session_leases.holder_id = excluded.holder_id
                                         then session_leases.acquired_at else now() end,
                    renewed_at    = now(),
                    expires_at    = excluded.expires_at
                returning session_leases.*, (select name from users where id = $3) as user_name
                """,
                key, body.transcript_id, user["id"], body.holder_id,
                redact(body.machine.strip()), key.split(":", 1)[0],
                body.mode, gen, redact(body.note.strip()), expires,
            )
    out = _lease_row(row)
    out["acquired"] = True
    out["stolen"] = bool(active and cur and cur["holder_id"] != body.holder_id)
    return out


@router.get("/v1/leases")
async def list_leases(request: Request, active_only: bool = True, user=Depends(auth)):
    """全局视图：现在谁在跑哪条会话。agent 建队/接手前应先看这里。"""
    pool = request.app.state.pool
    cond = "where l.expires_at > now()" if active_only else ""
    rows = await pool.fetch(
        f"""
        select l.*, u.name as user_name
        from session_leases l join users u on u.id = l.user_id
        {cond}
        order by l.renewed_at desc
        """,
    )
    return {"leases": [_lease_row(r) for r in rows]}


@router.get("/v1/leases/{session_key:path}")
async def get_lease(session_key: str, request: Request, user=Depends(auth)):
    pool = request.app.state.pool
    row = await pool.fetchrow(
        """
        select l.*, u.name as user_name
        from session_leases l join users u on u.id = l.user_id
        where l.session_key = $1
        """, session_key,
    )
    if not row:
        raise HTTPException(404, "该会话没有租约记录")
    out = _lease_row(row)
    out["active"] = row["expires_at"] > _now()
    return out


@router.post("/v1/leases/{session_key:path}/renew")
async def renew_lease(session_key: str, body: RenewIn, request: Request, user=Depends(auth)):
    """续租。带旧 generation 会被拒——这是防止「已失去执行权的进程继续写」的关键。"""
    ttl = _ttl(request, body.ttl_seconds)
    expires = _now() + timedelta(seconds=ttl)
    pool = request.app.state.pool
    row = await pool.fetchrow(
        """
        update session_leases
           set renewed_at = now(), expires_at = $4
         where session_key = $1 and holder_id = $2 and generation = $3
           and expires_at > now()
        returning session_leases.*,
                  (select name from users where id = session_leases.user_id) as user_name
        """,
        session_key, body.holder_id, int(body.generation), expires,
    )
    if not row:
        cur = await pool.fetchrow(
            "select holder_id, generation, expires_at from session_leases where session_key = $1",
            session_key,
        )
        if not cur:
            raise HTTPException(404, "该会话没有租约，请先 acquire")
        raise HTTPException(409, detail={
            "error": "lease lost",
            "reason": "generation 已过期或租约已被别人持有",
            "holder_id": cur["holder_id"],
            "generation": int(cur["generation"]),
            "expires_at": cur["expires_at"].isoformat(),
            "hint": "停止写入，重新 acquire 后再继续",
        })
    return _lease_row(row)


@router.post("/v1/leases/{session_key:path}/transfer")
async def transfer_lease(session_key: str, body: TransferIn, request: Request,
                         user=Depends(auth)):
    """原子交接执行权给另一台机器/另一个人。holder 本人、归属人或管理员可发起。"""
    if not body.to_holder_id:
        raise HTTPException(400, "to_holder_id 必填")
    ttl = _ttl(request, body.ttl_seconds)
    expires = _now() + timedelta(seconds=ttl)
    pool = request.app.state.pool

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("select pg_advisory_xact_lock(hashtext($1))", f"lease:{session_key}")
            cur = await conn.fetchrow(
                "select * from session_leases where session_key = $1", session_key)
            if not cur:
                raise HTTPException(404, "该会话没有租约，请先 acquire")
            if cur["expires_at"] <= _now():
                raise HTTPException(409, "租约已过期，直接 acquire 即可")
            # 持有者身份以「账号」为准：holder_id 是客户端自定义串，
            # 服务端只认 lease.user_id，避免任何人拿字符串冒充他人交接
            privileged = bool(user["is_admin"]) or cur["user_id"] == user["id"]
            if not privileged:
                raise HTTPException(403, "只有持有者、会话归属人或管理员能交接")

            row = await conn.fetchrow(
                """
                update session_leases
                   set holder_id = $2, machine = $3, generation = generation + 1,
                       renewed_at = now(), expires_at = $4,
                       note = coalesce(nullif($5, ''), note)
                 where session_key = $1
                returning session_leases.*,
                          (select name from users where id = session_leases.user_id) as user_name
                """,
                session_key, body.to_holder_id, redact(body.to_machine.strip()),
                expires, redact(body.note.strip()),
            )
    out = _lease_row(row)
    out["transferred_from"] = cur["holder_id"]
    return out


@router.delete("/v1/leases/{session_key:path}")
async def release_lease(session_key: str, request: Request, holder_id: str = "",
                        generation: Optional[int] = None, user=Depends(auth)):
    """主动释放。带 generation 可避免「旧进程把别人刚拿到的租约删掉」。"""
    if not holder_id:
        raise HTTPException(400, "holder_id 必填")
    pool = request.app.state.pool
    # 逻辑释放而不是物理删除：generation 必须单调递增。
    # 删行会让下一个人的 generation 从 1 重新开始，旧进程带着 gen=1 就又能写进来了。
    if generation is None:
        status = await pool.execute(
            "update session_leases set expires_at = now() "
            "where session_key = $1 and holder_id = $2 and expires_at > now()",
            session_key, holder_id,
        )
    else:
        status = await pool.execute(
            "update session_leases set expires_at = now() "
            "where session_key = $1 and holder_id = $2 and generation = $3 "
            "and expires_at > now()", session_key, holder_id, int(generation),
        )
    released = status != "UPDATE 0"
    if not released:
        raise HTTPException(409, detail={
            "error": "lease not held by caller",
            "hint": "holder_id/generation 不匹配，可能已被别人接手",
        })
    return {"ok": True, "released": True, "session_key": session_key}


async def fencing_check(pool, session_key: str, generation: Optional[int]) -> Optional[str]:
    """上传/回滚写入前的栅栏校验。

    返回警告文案（warn 模式）或抛 409（strict 模式由调用方处理）。
    没有 active exec 租约时不干预——不开租约的部署行为完全不变。
    """
    row = await pool.fetchrow(
        "select holder_id, generation, expires_at from session_leases "
        "where session_key = $1 and mode = 'exec' and expires_at > now()",
        session_key,
    )
    if not row:
        return None
    if generation is None:
        return (f"该会话正由 {row['holder_id']} 执行（generation={row['generation']}）；"
                f"本次写入未带 lease_generation")
    if int(generation) != int(row["generation"]):
        return (f"generation 不匹配：当前 {row['generation']}，你带的是 {generation}；"
                f"说明执行权已转给别人")
    return None
