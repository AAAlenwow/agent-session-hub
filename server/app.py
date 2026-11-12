"""agent-hub: 团队级 agent 会话事件中枢（MVP）

职责三件事：
  1. 接收各人机器上 hooks 上报的会话事件（带 Bearer token，逐人归因）
  2. 存 Postgres（sessions / events）
  3. 提供检索与查询 API，供 MCP 查询端和未来的 Web UI 使用
"""
import hashlib
import json
import os
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

import asyncpg
import yaml
from fastapi import Depends, FastAPI, HTTPException, Header
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, field_validator

from auth import current_user as auth, sha256
from prompt import build_prompt
from mobile import router as mobile_router
from transcripts_api import router as transcripts_router

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://agenthub:agenthub@127.0.0.1:5432/agenthub"
)
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

# 部署配置：决定脱敏规则，以及 /v1/agent-prompt 生成的部署专属提示词。
# 提示词随部署而异：不同服务器改各自的 hub-config.yaml，产出不同的规则文案。
CONFIG_PATH = os.environ.get(
    "HUB_CONFIG", os.path.join(os.path.dirname(__file__), "hub-config.yaml")
)
with open(CONFIG_PATH, encoding="utf-8") as _f:
    HUB_CONFIG = yaml.safe_load(_f) or {}

_REDACTIONS = [
    (re.compile(r["pattern"]), r.get("replace", "[REDACTED]"))
    for r in HUB_CONFIG.get("privacy", {}).get("redaction", [])
]


def redact(text: str) -> str:
    """入库前脱敏：按部署配置的正则替换敏感内容。"""
    for rx, repl in _REDACTIONS:
        text = rx.sub(repl, text)
    return text


def strip_nul(v):
    """Postgres 的 text 与 jsonb 都拒绝 NUL（\\u0000）：
    hooks 上来的脏数据不带过滤直接入库，一条就会把接口打成 500。"""
    if isinstance(v, str):
        return v.replace("\x00", "")
    if isinstance(v, list):
        return [strip_nul(x) for x in v]
    if isinstance(v, dict):
        return {str(k).replace("\x00", ""): strip_nul(x) for k, x in v.items()}
    return v


def redact_obj(v):
    """递归脱敏：payload / files 里的字符串同样要走 redact，
    否则密钥只要不放 summary 就原样入库还能被搜到。"""
    if isinstance(v, str):
        return redact(v)
    if isinstance(v, list):
        return [redact_obj(x) for x in v]
    if isinstance(v, dict):
        return {k: redact_obj(x) for k, x in v.items()}
    return v


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    # 首次启动：用 ADMIN_TOKEN 初始化管理员，方便发第一个用户 token
    if ADMIN_TOKEN:
        count = await app.state.pool.fetchval("select count(*) from users")
        if count == 0:
            await app.state.pool.execute(
                "insert into users (name, token_hash, is_admin) values ('admin', $1, true)",
                sha256(ADMIN_TOKEN),
            )
    yield
    await app.state.pool.close()


app = FastAPI(title="agent-hub", version="0.4.0", lifespan=lifespan)
app.state.config = HUB_CONFIG          # 供原件层路由读取部署策略
app.include_router(transcripts_router)
app.include_router(mobile_router)


def sha256_legacy(s: str) -> str:      # 兼容旧引用（实际使用 auth.sha256）
    return hashlib.sha256(s.encode()).hexdigest()


# ---------- 模型 ----------

class EventIn(BaseModel):
    ts: Optional[str] = None          # ISO 时间，缺省用服务器时间
    kind: str = Field(..., description="user_prompt / tool_call / turn_end / ...")
    summary: str = ""
    files: list[str] = []
    payload: dict = {}

    @field_validator("kind", "summary", "files", "payload", mode="before")
    @classmethod
    def _no_nul(cls, v):
        return strip_nul(v)

    @field_validator("ts")
    @classmethod
    def _valid_ts(cls, v):
        """非法时间戳在入库前就挡掉：否则 asyncpg 抛 DataError，接口变成 500。"""
        if v in (None, ""):
            return None
        try:
            datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("ts 必须是 ISO 8601 时间（如 2026-11-12T10:00:00Z）")
        return v


class SessionIn(BaseModel):
    ext_id: str = Field(..., description="CLI 侧 session_id")
    agent: str = "claude-code"
    project: str = ""
    machine: str = Field("", description="上传机器名（hostname / 自定义标签）")
    cwd: str = ""
    repo: str = ""
    title: str = ""

    @field_validator("ext_id", "agent", "project", "machine", "cwd", "repo", "title",
                     mode="before")
    @classmethod
    def _no_nul(cls, v):
        return strip_nul(v)


class IngestIn(BaseModel):
    session: SessionIn
    events: list[EventIn]


async def _resolve_project(name: str, repo: str):
    """按项目名 / git remote 找到或自动创建 projects 行。交接追踪的基础：
    hooks 上报时 sessions 自动挂到项目上，无需人工登记。"""
    if name:
        row = await app.state.pool.fetchrow("select id from projects where name = $1", name)
        if row:
            return row["id"]
    if repo:
        row = await app.state.pool.fetchrow("select id from projects where repo = $1", repo)
        if row:
            return row["id"]
    if name:
        row = await app.state.pool.fetchrow(
            "insert into projects (name, repo) values ($1, $2) "
            "on conflict (name) do update set "
            "repo = coalesce(nullif(excluded.repo, ''), projects.repo) returning id",
            name, repo,
        )
        return row["id"]
    return None


async def _require_project_control(pid: int, user: dict):
    """项目级写权限：管理员，或该项目的现任负责人。

    项目尚无负责人时放行 —— 否则第一个负责人永远指派不进来（先有鸡还是先有蛋）。
    负责人时间线是审计数据，绝不能任何人都能改。"""
    if user["is_admin"]:
        return
    owner = await app.state.pool.fetchrow(
        "select user_id from project_owners "
        "where project_id = $1 and role = 'owner' and released_at is null limit 1",
        pid,
    )
    if owner is None:
        return
    if owner["user_id"] != user["id"]:
        raise HTTPException(403, "需要管理员或该项目现任负责人")


# ---------- 上报 ----------

@app.post("/v1/events")
async def ingest(body: IngestIn, user=Depends(auth)):
    s = body.session
    project_id = await _resolve_project(s.project, s.repo)
    sess = await app.state.pool.fetchrow(
        """
        insert into sessions (ext_id, user_id, agent, project, machine, cwd, repo,
                              title, project_id)
        values ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        on conflict (user_id, ext_id) do update set
            agent   = excluded.agent,
            project = excluded.project,
            machine = coalesce(nullif(excluded.machine, ''), sessions.machine),
            cwd     = excluded.cwd,
            repo    = excluded.repo,
            title   = coalesce(nullif(excluded.title, ''), sessions.title),
            project_id = coalesce(excluded.project_id, sessions.project_id),
            updated_at = now()
        returning id
        """,
        s.ext_id, user["id"], s.agent, s.project, redact(s.machine), s.cwd, s.repo,
        redact(s.title), project_id,
    )
    for e in body.events:
        await app.state.pool.execute(
            """
            insert into events (session_id, ts, kind, summary, files, payload)
            values ($1, coalesce($2::timestamptz, now()), $3, $4, $5::jsonb, $6::jsonb)
            """,
            sess["id"], e.ts, e.kind, redact(e.summary),
            json.dumps([redact(f) for f in e.files], ensure_ascii=False),
            json.dumps(redact_obj(e.payload), ensure_ascii=False),
        )
    return {"ok": True, "session_id": sess["id"], "ingested": len(body.events)}


# ---------- 查询 ----------

@app.get("/v1/sessions")
async def list_sessions(limit: int = 50, machine: str = "", user=Depends(auth)):
    rows = await app.state.pool.fetch(
        """
        select s.id, s.ext_id, s.agent, s.project, s.machine, s.title, s.updated_at,
               u.name as owner,
               (select count(*) from events e where e.session_id = s.id) as event_count
        from sessions s join users u on u.id = s.user_id
        where ($2 = '' or s.machine = $2)
        order by s.updated_at desc limit $1
        """,
        max(1, min(limit, 200)), machine,
    )
    return [dict(r) for r in rows]


@app.get("/v1/machines")
async def list_machines(user=Depends(auth)):
    """机器 / 服务器清单：会话从哪些机器传上来、各自多少会话、最后活跃时间。
    手机端与多人多机协作的入口——先看机器，再看机器上的会话。"""
    rows = await app.state.pool.fetch(
        """
        select coalesce(nullif(s.machine, ''), '(未上报)') as machine,
               count(*) as session_count,
               count(distinct s.user_id) as owners,
               max(s.updated_at) as last_seen,
               array_agg(distinct s.project) filter (where s.project <> '') as projects
        from sessions s
        group by 1
        order by max(s.updated_at) desc
        """
    )
    return [dict(r) for r in rows]


@app.get("/v1/sessions/{sid}")
async def get_session(sid: int, user=Depends(auth)):
    s = await app.state.pool.fetchrow(
        "select s.*, u.name as owner from sessions s "
        "join users u on u.id = s.user_id where s.id = $1",
        sid,
    )
    if not s:
        raise HTTPException(404, "session not found")
    evs = await app.state.pool.fetch(
        "select ts, kind, summary, files from events "
        "where session_id = $1 order by ts limit 500",
        sid,
    )
    return {"session": dict(s), "events": [dict(e) for e in evs]}


@app.get("/v1/search")
async def search(q: str, limit: int = 20, user=Depends(auth)):
    like = f"%{q}%"
    rows = await app.state.pool.fetch(
        """
        select e.id, e.ts, e.kind, e.summary, e.files,
               s.id as session_id, s.agent, s.project, s.machine, s.title,
               u.name as owner
        from events e
        join sessions s on s.id = e.session_id
        join users u on u.id = s.user_id
        where e.summary ilike $1
           or s.title ilike $1
           or s.project ilike $1
           or e.files::text ilike $1
        order by e.ts desc
        limit $2
        """,
        like, max(1, min(limit, 100)),
    )
    return [dict(r) for r in rows]


# ---------- 管理 ----------

@app.post("/v1/admin/users")
async def create_user(name: str, user=Depends(auth)):
    if not user["is_admin"]:
        raise HTTPException(403, "admin only")
    name = (name or "").strip()
    if not name or len(name) > 64:
        raise HTTPException(400, "用户名需 1~64 个字符")
    token = secrets.token_urlsafe(24)
    try:
        row = await app.state.pool.fetchrow(
            "insert into users (name, token_hash) values ($1, $2) returning id",
            name, sha256(token),
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(409, f"用户 '{name}' 已存在")
    return {"id": row["id"], "name": name, "token": token}


# ---------- 项目 / 负责人交接 ----------

class ProjectIn(BaseModel):
    name: str
    repo: str = ""
    description: str = ""


class AssignIn(BaseModel):
    user: str
    role: str = "owner"   # owner / member


@app.post("/v1/projects")
async def create_project(body: ProjectIn, user=Depends(auth)):
    """登记项目。已存在时只允许管理员或现任负责人修改档案；
    空字段不覆盖已有值（hooks 上报时常带空 repo，否则每次都把 git remote 抹掉）。"""
    existing = await app.state.pool.fetchrow(
        "select id from projects where name = $1", body.name
    )
    if existing:
        await _require_project_control(existing["id"], user)
        row = await app.state.pool.fetchrow(
            "update projects set repo = coalesce(nullif($2, ''), repo), "
            "description = coalesce(nullif($3, ''), description) "
            "where id = $1 returning id",
            existing["id"], body.repo, body.description,
        )
        return {"id": row["id"], "name": body.name, "updated": True}

    row = await app.state.pool.fetchrow(
        "insert into projects (name, repo, description) values ($1, $2, $3) returning id",
        body.name, body.repo, body.description,
    )
    return {"id": row["id"], "name": body.name, "updated": False}


@app.get("/v1/projects")
async def list_projects(user=Depends(auth)):
    rows = await app.state.pool.fetch(
        """
        select p.id, p.name, p.repo, p.description,
               (select u.name from project_owners po join users u on u.id = po.user_id
                 where po.project_id = p.id and po.role = 'owner'
                   and po.released_at is null limit 1) as current_owner,
               (select count(*) from sessions s where s.project_id = p.id) as session_count,
               (select count(distinct s.user_id) from sessions s where s.project_id = p.id) as contributors
        from projects p
        order by p.created_at desc
        """
    )
    return [dict(r) for r in rows]


@app.post("/v1/projects/{pid}/assign")
async def assign_role(pid: int, body: AssignIn, user=Depends(auth)):
    """指派负责人 / 成员。对同一角色：现任自动释放（released_at=now()），
    形成可追溯的负责人时间线 —— 这是交接报告的数据来源。

    只有管理员或现任负责人能改（否则任何人都能把自己塞成 owner，交接报告即失真）。"""
    if body.role not in ("owner", "member"):
        raise HTTPException(400, "role 只能是 owner 或 member")
    if not await app.state.pool.fetchrow("select 1 from projects where id = $1", pid):
        raise HTTPException(404, "project not found")
    await _require_project_control(pid, user)
    target = await app.state.pool.fetchrow("select id from users where name = $1", body.user)
    if not target:
        raise HTTPException(404, f"user '{body.user}' not found")
    async with app.state.pool.acquire() as conn:
        async with conn.transaction():
            # 时间线是审计数据：指派必须在锁内完成，否则并发 assign
            # 会把现任释放两次、插出两个"现任"（hashtext：锁参数必须是 bigint）
            await conn.execute("select pg_advisory_xact_lock(hashtext($1))", f"proj:{pid}")
            current = await conn.fetchrow(
                "select user_id from project_owners "
                "where project_id = $1 and role = $2 and released_at is null limit 1",
                pid, body.role,
            )
            if current and current["user_id"] == target["id"]:
                # 指派同一人 = no-op。若照常 release+insert 会产生零时长任期，
                # 且交接报告的"上一任"变成现任本人（误导）
                return {"ok": True, "project": pid, "role": body.role,
                        "user": body.user, "unchanged": True}
            await conn.execute(
                "update project_owners set released_at = now() "
                "where project_id = $1 and role = $2 and released_at is null",
                pid, body.role,
            )
            await conn.execute(
                "insert into project_owners (project_id, user_id, role) values ($1, $2, $3)",
                pid, target["id"], body.role,
            )
    return {"ok": True, "project": pid, "role": body.role, "user": body.user}


@app.get("/v1/projects/{pid}/handover")
async def project_handover(pid: str, user=Depends(auth)):
    """交接报告：项目档案 + 历任负责人时间线 + 上一任负责人的
    历史会话 / 改动热点文件 / 使用过的技能包与 MCP / 分享过的结论 / 提问记录。
    pid 支持数字 id 或项目名。"""
    if pid.isdigit():
        proj = await app.state.pool.fetchrow("select * from projects where id = $1", int(pid))
    else:
        proj = await app.state.pool.fetchrow("select * from projects where name = $1", pid)
    if not proj:
        raise HTTPException(404, "project not found")
    proj_id = proj["id"]

    owners = await app.state.pool.fetch(
        """
        select po.role, po.assigned_at, po.released_at, u.id as user_id, u.name as owner
        from project_owners po join users u on u.id = po.user_id
        where po.project_id = $1
        order by po.assigned_at
        """,
        proj_id,
    )
    prev = next(
        (o for o in reversed(owners) if o["released_at"] is not None and o["role"] == "owner"),
        None,
    )

    report = {
        "project": dict(proj),
        "ownership": [dict(o) for o in owners],
        "previous_owner": dict(prev) if prev else None,
        "previous_owner_sessions": [],
        "top_files": [],
        "skills_used": [],
        "shared_notes": [],
        "recent_prompts": [],
    }
    if prev:
        uid = prev["user_id"]
        report["previous_owner_sessions"] = [
            dict(r) for r in await app.state.pool.fetch(
                """
                select s.id, s.agent, s.title, s.updated_at,
                       (select count(*) from events e where e.session_id = s.id) as event_count
                from sessions s where s.project_id = $1 and s.user_id = $2
                order by s.updated_at desc limit 50
                """, proj_id, uid)
        ]
        report["top_files"] = [
            dict(r) for r in await app.state.pool.fetch(
                """
                select f as file, count(*) as touches
                from events e join sessions s on s.id = e.session_id,
                     jsonb_array_elements_text(e.files) as f
                where s.project_id = $1 and s.user_id = $2
                group by f order by touches desc limit 20
                """, proj_id, uid)
        ]
        report["skills_used"] = [
            dict(r) for r in await app.state.pool.fetch(
                """
                select e.summary as skill, e.kind, count(*) as uses, max(e.ts) as last_used
                from events e join sessions s on s.id = e.session_id
                where s.project_id = $1 and s.user_id = $2
                  and e.kind in ('skill_call', 'mcp_call')
                group by e.summary, e.kind order by uses desc limit 30
                """, proj_id, uid)
        ]
        report["shared_notes"] = [
            dict(r) for r in await app.state.pool.fetch(
                """
                select e.ts, e.summary, e.files
                from events e join sessions s on s.id = e.session_id
                where s.project_id = $1 and s.user_id = $2 and e.kind = 'shared_note'
                order by e.ts desc limit 50
                """, proj_id, uid)
        ]
        report["recent_prompts"] = [
            dict(r) for r in await app.state.pool.fetch(
                """
                select e.ts, e.summary
                from events e join sessions s on s.id = e.session_id
                where s.project_id = $1 and s.user_id = $2 and e.kind = 'user_prompt'
                order by e.ts desc limit 100
                """, proj_id, uid)
        ]
    return report


@app.get("/v1/skills")
async def skills_usage(project: str = "", limit: int = 50, user=Depends(auth)):
    """技能包 / MCP 工具使用聚合（全团队或按项目）。"""
    q = """
        select e.summary as skill, e.kind, count(*) as uses, max(e.ts) as last_used
        from events e join sessions s on s.id = e.session_id
        where e.kind in ('skill_call', 'mcp_call')
    """
    args: list = []
    if project:
        q += " and s.project = $1"
        args.append(project)
    q += " group by e.summary, e.kind order by uses desc"
    q += f" limit ${len(args) + 1}"
    args.append(max(1, min(limit, 200)))
    rows = await app.state.pool.fetch(q, *args)
    return [dict(r) for r in rows]


@app.get("/v1/agent-prompt")
async def agent_prompt(user=Depends(auth)):
    """部署专属的 agent 使用说明：按本部署 hub-config.yaml 动态生成。
    隐私模式 / 脱敏 / 保留期不同的服务器，产出不同的提示词，互不通用。"""
    return PlainTextResponse(build_prompt(HUB_CONFIG), media_type="text/markdown")


@app.get("/health")
async def health():
    await app.state.pool.fetchval("select 1")
    return {"ok": True}
