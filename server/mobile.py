"""手机端只读视图 /m：在手机浏览器里看团队会话（按机器分组 + 搜索 + 时间线）。

为什么放在 hub 里而不是单独做 App：
  · 手机没法跑 stdio MCP，但浏览器人人都有 → 服务端直接渲染 HTML，零构建、零 CDN；
  · 覆盖面最广：iOS / Android 任一台设备打开链接就能看，不用装东西；
  · 鉴权沿用个人 token（存 HttpOnly cookie），与 API 同一套权限，不新增信任面。

只读：这里刻意不提供写操作（改项目负责人、回滚版本等仍走 CLI/API），
避免手机上误触破坏性操作。
"""
from __future__ import annotations

import html
import urllib.parse

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from auth import sha256

router = APIRouter()

COOKIE = "ah_token"
MAX_EVENTS = 500

CSS = """
:root{color-scheme:light dark;--bg:#faf9f7;--card:#fff;--tx:#1f1f1f;--tx2:#6b6b6b;
--bd:#e6e3dd;--acc:#534ab7;--chip:#eeedfe;--chiptx:#3c3489}
@media (prefers-color-scheme:dark){:root{--bg:#161616;--card:#211f1e;--tx:#ededed;
--tx2:#a0a0a0;--bd:#3a3735;--acc:#a9a2f0;--chip:#2b2748;--chiptx:#cec9f7}}
*{box-sizing:border-box}
body{margin:0;padding:16px 14px 40px;background:var(--bg);color:var(--tx);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
h1{font-size:17px;font-weight:500;margin:0 0 4px}
h2{font-size:14px;font-weight:500;margin:22px 0 8px;color:var(--tx2)}
a{color:var(--acc);text-decoration:none}
.card{background:var(--card);border:1px solid var(--bd);border-radius:12px;
padding:12px 14px;margin-bottom:10px;display:block;color:inherit}
.row{display:flex;justify-content:space-between;gap:10px;align-items:baseline}
.t{color:var(--tx2);font-size:12px;white-space:nowrap}
.sub{color:var(--tx2);font-size:12.5px;margin-top:4px;word-break:break-all}
.chip{display:inline-block;background:var(--chip);color:var(--chiptx);border-radius:6px;
padding:1px 7px;font-size:11.5px;margin-right:5px}
input,button{font:inherit;border-radius:10px;border:1px solid var(--bd);padding:10px 12px;
background:var(--card);color:var(--tx);width:100%}
button{background:var(--acc);color:#fff;border:none;margin-top:10px}
form{margin:0}
.bar{display:flex;gap:8px;margin:12px 0 4px}
.bar input{flex:1}
.bar button{width:auto;margin:0;padding:10px 16px}
.ev{border-left:2px solid var(--bd);padding:2px 0 12px 12px;margin-left:4px}
.ev .k{font-size:11.5px;color:var(--chiptx);background:var(--chip);border-radius:6px;
padding:1px 6px;margin-right:6px}
.empty{color:var(--tx2);font-size:13.5px;padding:16px 0}
"""


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


def page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html lang=zh-CN><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1,"
        "viewport-fit=cover'><title>" + esc(title) + "</title>"
        "<style>" + CSS + "</style></head><body>" + body + "</body></html>"
    )


def _fmt_ts(ts) -> str:
    if not ts:
        return ""
    s = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
    return s.replace("T", " ")[:16]


async def _user_from_token(request: Request, token: str):
    if not token:
        return None
    return await request.app.state.pool.fetchrow(
        "select id, name, is_admin from users where token_hash = $1", sha256(token)
    )


def _login_page(err: str = "") -> HTMLResponse:
    msg = f"<div class=empty>{esc(err)}</div>" if err else (
        "<div class=sub>输入管理员给你的个人 token（AGENT_HUB_TOKEN）。</div>")
    return page("团队会话", f"""
<h1>团队会话</h1>{msg}
<form method=post action="/m/login">
<input name=token type=password placeholder="个人 token" autocomplete=current-password required>
<button type=submit>进入</button></form>
<div class=sub style="margin-top:14px">只读视图：看各机器上的会话与进展，不做任何写操作。</div>
""")


@router.get("/m", response_class=HTMLResponse)
async def mobile_home(request: Request, q: str = "", machine: str = ""):
    token = request.cookies.get(COOKIE) or request.query_params.get("token", "")
    user = await _user_from_token(request, token)
    if not user:
        return _login_page()
    pool = request.app.state.pool

    machines = await pool.fetch(
        """select coalesce(nullif(machine,''),'(未上报)') as m, count(*) as n,
                  max(updated_at) as last_seen
           from sessions group by 1 order by max(updated_at) desc limit 30"""
    )
    if q:
        rows = await pool.fetch(
            """select e.id, e.ts, e.kind, e.summary, s.id as sid, s.project,
                      s.machine, u.name as owner
               from events e join sessions s on s.id = e.session_id
               join users u on u.id = s.user_id
               where e.summary ilike $1 or s.project ilike $1 or s.title ilike $1
               order by e.ts desc limit 50""", f"%{q}%")
        items = "".join(
            f"<a class=card href='/m/s/{r['sid']}'>"
            f"<div class=row><span><span class=chip>{esc(r['kind'])}</span>"
            f"{esc(r['project'] or '(无项目)')}</span>"
            f"<span class=t>{esc(_fmt_ts(r['ts']))}</span></div>"
            f"<div class=sub>{esc(r['summary'])[:220]}</div>"
            f"<div class=sub>{esc(r['owner'])} · {esc(r['machine'] or '未知机器')}</div></a>"
            for r in rows) or "<div class=empty>没有命中。</div>"
        body = f"""
<h1>搜索：{esc(q)}</h1>
<form class=bar method=get action="/m"><input name=q value="{esc(q)}" placeholder="关键词">
<button>搜</button></form><h2>命中 {len(rows)} 条</h2>{items}
<div class=sub><a href="/m">← 返回首页</a></div>"""
        return page("搜索 " + q, body)

    sess_rows = await pool.fetch(
        """select s.id, s.project, s.machine, s.agent, s.updated_at, s.title,
                  u.name as owner,
                  (select count(*) from events e where e.session_id = s.id) as n,
                  (select e.summary from events e where e.session_id = s.id
                     and e.kind = 'turn_end' order by e.ts desc limit 1) as last_turn
           from sessions s join users u on u.id = s.user_id
           where ($1 = '' or coalesce(nullif(s.machine,''),'(未上报)') = $1)
           order by s.updated_at desc limit 60""", machine)
    mchips = "".join(
        f"<a class=chip href='/m?machine={urllib.parse.quote(r['m'])}'"
        f" style='padding:5px 10px;font-size:12.5px'>{esc(r['m'])} · {r['n']}</a> "
        for r in machines)
    cards = "".join(
        f"<a class=card href='/m/s/{r['id']}'>"
        f"<div class=row><span>{esc(r['project'] or '(无项目)')}</span>"
        f"<span class=t>{esc(_fmt_ts(r['updated_at']))}</span></div>"
        f"<div class=sub><span class=chip>{esc(r['machine'] or '未知机器')}</span>"
        f"<span class=chip>{esc(r['agent'])}</span>{esc(r['owner'])} · {r['n']} 条事件</div>"
        + (f"<div class=sub>最近：{esc(r['last_turn'])[:160]}</div>" if r["last_turn"] else "")
        + "</a>"
        for r in sess_rows) or "<div class=empty>这台机器还没有会话。</div>"
    body = f"""
<div class=row><h1>团队会话</h1><a class=t href="/m/logout">退出</a></div>
<div class=sub>{esc(user['name'])}{' · 管理员' if user['is_admin'] else ''}</div>
<form class=bar method=get action="/m"><input name=q placeholder="搜关键词，如 圣诞活动">
<button>搜</button></form>
<h2>机器（{len(machines)}）</h2><div>{mchips}<a class=chip href="/m" style="padding:5px 10px">全部</a></div>
<h2>{('机器：' + esc(machine)) if machine else '最近会话'}</h2>{cards}"""
    return page("团队会话", body)


@router.get("/m/s/{sid}", response_class=HTMLResponse)
async def mobile_session(request: Request, sid: int):
    token = request.cookies.get(COOKIE) or request.query_params.get("token", "")
    user = await _user_from_token(request, token)
    if not user:
        return _login_page()
    pool = request.app.state.pool
    s = await pool.fetchrow(
        "select s.*, u.name as owner from sessions s join users u on u.id = s.user_id "
        "where s.id = $1", sid)
    if not s:
        return page("未找到", "<h1>会话不存在</h1><div class=sub><a href='/m'>← 返回</a></div>")
    evs = await pool.fetch(
        "select ts, kind, summary, files from events where session_id = $1 "
        "order by ts limit $2", sid, MAX_EVENTS)
    items = "".join(
        f"<div class=ev><div><span class=k>{esc(e['kind'])}</span>"
        f"<span class=t>{esc(_fmt_ts(e['ts']))}</span></div>"
        f"<div>{esc(e['summary'])}</div></div>"
        for e in evs) or "<div class=empty>这个会话还没有事件。</div>"
    body = f"""
<div class=sub><a href="/m">← 返回</a></div>
<h1>{esc(s['project'] or '(无项目)')}</h1>
<div class=sub><span class=chip>{esc(s['machine'] or '未知机器')}</span>
<span class=chip>{esc(s['agent'])}</span>{esc(s['owner'])}</div>
<div class=sub>cwd: {esc(s['cwd'])}</div>
<div class=sub>更新: {esc(_fmt_ts(s['updated_at']))} · {len(evs)} 条事件</div>
<h2>时间线</h2>{items}"""
    return page(s["project"] or "会话", body)


@router.post("/m/login")
async def mobile_login(request: Request):
    raw = (await request.body()).decode("utf-8", "replace")
    token = (urllib.parse.parse_qs(raw).get("token") or [""])[0].strip()
    user = await _user_from_token(request, token)
    if not user:
        return _login_page("token 无效，请找管理员确认。")
    resp = RedirectResponse("/m", status_code=303)
    resp.set_cookie(COOKIE, token, max_age=30 * 86400, httponly=True,
                    samesite="lax", path="/")
    return resp


@router.get("/m/logout")
async def mobile_logout():
    resp = RedirectResponse("/m", status_code=303)
    resp.delete_cookie(COOKIE, path="/")
    return resp


@router.get("/m/health")
async def mobile_health() -> Response:
    return Response("ok", media_type="text/plain")
