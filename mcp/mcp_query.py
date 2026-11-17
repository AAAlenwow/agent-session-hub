#!/usr/bin/env python3
"""agent-session-hub MCP 查询端：让任何支持 MCP 的 agent 查询团队会话记忆。

安装（每个成员的机器上执行一次）:
    claude mcp add team-memory --env AGENT_HUB_URL=https://mem.corp.local \
        --env AGENT_HUB_TOKEN=<个人token> -- python3 /opt/agent-hub/mcp/mcp_query.py

依赖: pip install "mcp[cli]"
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid

try:                                    # mcp 1.x：FastMCP
    from mcp.server.fastmcp import FastMCP as _Server
except ImportError:                     # mcp 2.x：FastMCP 改名为 MCPServer（本文件用到的
    from mcp.server.mcpserver import MCPServer as _Server   # tool()/run() 两者一致）

HUB_URL = os.environ.get("AGENT_HUB_URL", "http://127.0.0.1:8421")
HUB_TOKEN = os.environ.get("AGENT_HUB_TOKEN", "")
TIMEOUT = 10

mcp = _Server("team-memory")


def _get(path: str):
    req = urllib.request.Request(
        HUB_URL.rstrip("/") + path,
        headers={"Authorization": "Bearer " + HUB_TOKEN},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def _get_text(path: str) -> str:
    req = urllib.request.Request(
        HUB_URL.rstrip("/") + path,
        headers={"Authorization": "Bearer " + HUB_TOKEN},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8")


def _post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        HUB_URL.rstrip("/") + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + HUB_TOKEN,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


@mcp.tool()
def search_team_memory(q: str, limit: int = 20) -> str:
    """在团队所有成员的 agent 会话记录中检索关键词。
    返回匹配的事件摘要、所属 session、作者、时间，用于回答
    「谁改过 X、为什么这么改、当时的结论是什么」。"""
    rows = _get(f"/v1/search?q={urllib.parse.quote(q)}&limit={limit}")
    if not rows:
        return "没有找到相关记录。"
    lines = []
    for r in rows:
        lines.append(
            f"[{r['ts']}] {r['owner']} @ {r['project']} ({r['agent']}) "
            f"[{r.get('machine') or '未知机器'}] "
            f"#{r['session_id']} {r['kind']}: {r['summary']}"
        )
    return "\n".join(lines)


@mcp.tool()
def recent_team_sessions(limit: int = 20) -> str:
    """列出团队最近的 agent 会话（谁、哪个仓库、哪个 agent、事件数、更新时间）。"""
    rows = _get(f"/v1/sessions?limit={limit}")
    if not rows:
        return "还没有会话记录。"
    lines = []
    for r in rows:
        lines.append(
            f"[{r['updated_at']}] {r['owner']} @ {r['project']} "
            f"({r['agent']}) [{r.get('machine') or '未知机器'}] "
            f"#{r['id']} {r['event_count']} events"
        )
    return "\n".join(lines)


@mcp.tool()
def get_team_session(session_id: int) -> str:
    """查看某个团队会话的完整事件时间线（prompt、tool call、回合结束）。"""
    data = _get(f"/v1/sessions/{session_id}")
    s = data["session"]
    head = (
        f"#{s['id']} {s['owner']} @ {s['project']} ({s['agent']}) "
        f"machine={s.get('machine') or '未知'} cwd={s['cwd']}"
    )
    lines = [head]
    for e in data["events"]:
        lines.append(f"[{e['ts']}] {e['kind']}: {e['summary']}")
    return "\n".join(lines)


@mcp.tool()
def list_team_machines() -> str:
    """列出团队会话来自哪些机器 / 服务器（机器名、会话数、涉及项目、最后活跃）。
    用户问「这些会话是哪台机器上的」「某台服务器上在做什么」时先看这个。"""
    rows = _get("/v1/machines")
    if not rows:
        return "还没有上报过机器信息。"
    return "\n".join(
        f"{r['machine']} — 会话 {r['session_count']} | 涉及 {r['owners']} 人 | "
        f"最后活跃 {r['last_seen']} | 项目: {', '.join(r['projects'] or []) or '-'}"
        for r in rows
    )


@mcp.tool()
def team_memory_instructions() -> str:
    """获取本部署（私有服务器）专属的 team-memory 使用说明：隐私模式、
    分享规则、脱敏与保留策略。该说明由部署管理员在服务器端配置并动态生成，
    不同部署的说明不同。使用 team-memory 的分享工具前，应先调用本工具
    确认本部署的规则（有些部署要求分享前必须先经用户确认）。"""
    return _get_text("/v1/agent-prompt")


@mcp.tool()
def share_team_memory(title: str, summary: str, files: list[str] = [],
                      tags: list[str] = [], project: str = "",
                      session_ref: str = "") -> str:
    """把当前会话中的一个结论 / 经验 / 踩坑记录分享到团队记忆库。
    summary 请写「结论 + 原因 + 影响」的一段话，不要流水账；
    files 填涉及的文件路径；project 填所属项目（仓库名），便于项目交接时检索。
    分享规则以 team_memory_instructions() 返回的本部署说明为准——
    部分部署要求先把摘要展示给用户确认后才能调用。"""
    ext = session_ref or f"shared-{uuid.uuid4().hex[:12]}"
    body = {
        "session": {
            "ext_id": ext,
            "agent": os.environ.get("AGENT_HUB_AGENT", "manual"),
            "project": project or os.environ.get("AGENT_HUB_PROJECT", "shared"),
            "title": title,
        },
        "events": [{
            "kind": "shared_note",
            "summary": summary,
            "files": files,
            "payload": {"tags": tags},
        }],
    }
    try:
        r = _post("/v1/events", body)
        return f"已分享到团队记忆库：session #{r['session_id']}"
    except Exception as e:
        return f"分享失败：{e}（不自动重试，请如实告知用户）"


@mcp.tool()
def list_team_projects() -> str:
    """列出团队所有项目：现任负责人、会话数、参与人数、描述。"""
    rows = _get("/v1/projects")
    if not rows:
        return "还没有登记的项目。hooks 上报会按仓库名自动建项目，也可让管理员 POST /v1/projects 手动登记。"
    lines = []
    for r in rows:
        lines.append(
            f"#{r['id']} {r['name']} — 负责人: {r['current_owner'] or '(未指派)'} | "
            f"会话 {r['session_count']} | 参与者 {r['contributors']} | {r['description']}"
        )
    return "\n".join(lines)


@mcp.tool()
def get_project_handover(project: str) -> str:
    """获取项目交接报告：项目档案、历任负责人时间线、上一任负责人的历史会话、
    改动热点文件、使用过的技能包与 MCP、分享过的结论、近期提问记录。
    project 传项目名或数字 id。负责人变更后，新负责人用它直接继承上一任的上下文。"""
    data = _get(f"/v1/projects/{urllib.parse.quote(project)}/handover")
    p = data["project"]
    lines = [
        f"项目 #{p['id']} {p['name']} — {p['description']}",
        f"repo: {p['repo'] or '(未登记)'}",
        "",
        "== 负责人时间线 ==",
    ]
    for o in data["ownership"]:
        end = o["released_at"] or "现任"
        lines.append(f"  {o['owner']}（{o['role']}）{o['assigned_at']} → {end}")

    prev = data.get("previous_owner")
    if not prev:
        lines.append("")
        lines.append("没有已离任的负责人记录（可能首任仍在任，或未通过 assign 指派过）。")
        return "\n".join(lines)

    lines.append("")
    lines.append(f"== 上一任负责人 {prev['owner']} 的工作档案 ==")
    lines.append(f"[历史会话 {len(data['previous_owner_sessions'])} 个]")
    for s in data["previous_owner_sessions"][:20]:
        lines.append(
            f"  #{s['id']} {s['agent']} {s['updated_at']} "
            f"{s['event_count']} events — {s['title'] or '(无标题)'}"
        )
    lines.append("[改动热点文件]")
    for f in data["top_files"]:
        lines.append(f"  {f['file']} ({f['touches']} 次)")
    lines.append("[使用过的技能包 / MCP]")
    for s in data["skills_used"]:
        lines.append(f"  {s['skill']} — {s['uses']} 次，最后使用 {s['last_used']}")
    lines.append("[分享过的结论]")
    for n in data["shared_notes"]:
        lines.append(f"  [{n['ts']}] {n['summary']}")
    lines.append("[近期提问记录（最多 30 条）]")
    for q in data["recent_prompts"][:30]:
        lines.append(f"  [{q['ts']}] {q['summary']}")
    return "\n".join(lines)

# ---------------- 会话执行权租约（v0.6） ----------------
# 这三个工具让 agent 在「动手改别人的会话/接手一条没跑完的活」之前先问一句
# 「这台机器现在有权接着跑吗」，而不是两边同时写同一份上下文。


def _holder() -> str:
    machine = os.environ.get("AGENT_HUB_MACHINE") or __import__("socket").gethostname()
    return f"{machine}:{os.getpid()}"


def _lease_call(method: str, path: str, payload: dict = None):
    """返回 (data, error_text)。409 是人话不是异常：agent 要能读懂并据此决定等还是走。"""
    data = None
    headers = {"Authorization": "Bearer " + HUB_TOKEN}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(HUB_URL.rstrip("/") + path, data=data,
                                 headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            detail = json.loads(raw).get("detail", raw)
        except Exception:
            detail = raw
        if e.code == 409 and isinstance(detail, dict):
            return None, (
                f"这条会话正被 {detail.get('holder')}（{detail.get('holder_id')}）"
                f"在 {detail.get('machine') or '某台机器'} 上执行，"
                f"generation={detail.get('generation')}，"
                f"还剩 {int(detail.get('ttl_left_seconds') or 0) // 60} 分钟。"
                f"不要同时写。等对方释放，或请归属人用 force 夺回。"
            )
        return None, f"{e.code}: {detail}"


@mcp.tool()
def list_active_leases() -> str:
    """列出团队里现在正在执行（持有租约）的会话：谁、在哪台机器、还剩多久。

    动手接手一条会话、或准备改别人负责的代码之前，先调它确认没人在跑。"""
    data, err = _lease_call("GET", "/v1/leases")
    if err:
        return err
    rows = data.get("leases", [])
    if not rows:
        return "当前没有活跃租约：没有人正在执行任何会话。"
    out = []
    for r in rows:
        out.append(
            f"{r['session_key']} · {r['holder']} @ {r.get('machine') or '未知机器'}"
            f" · mode={r['mode']} · generation={r['generation']}"
            f" · 剩 {int(r.get('ttl_left_seconds') or 0) // 60} 分钟"
            + (f" · {r['note']}" if r.get("note") else "")
        )
    return "\n".join(out)


@mcp.tool()
def acquire_session_lease(session_id: int, ttl_minutes: int = 60, note: str = "") -> str:
    """申请一条会话的执行权（等价于「这台机器现在由我来跑它」）。

    成功返回 generation，之后续租/释放/写入都要带它；被占用则明确告诉你谁在跑。
    session_id 是 transcript id（见 recent_team_sessions / list 输出里的 #编号）。
    note 写清你要干什么（例如「接手 alice 的限流重构」），别人能看到。"""
    # 只给 transcript_id，服务端自己推导 session_key（客户端不需要猜 agent/ext_id）
    payload = {
        "transcript_id": session_id,
        "holder_id": _holder(),
        "machine": os.environ.get("AGENT_HUB_MACHINE") or "",
        "mode": "exec",
        "note": note,
        "ttl_seconds": max(60, int(ttl_minutes) * 60),
    }
    res, err = _lease_call("POST", "/v1/leases", payload)
    if err:
        return err
    return (
        f"已获得执行权：{res['session_key']}\n"
        f"  generation={res['generation']}，到期 {res['expires_at']}\n"
        f"  续租用 renew（带上这个 generation），干完用 release_session_lease 释放。\n"
        f"  上传新内容时带上 lease_generation={res['generation']}，否则可能覆盖别人的进度。"
    )


@mcp.tool()
def release_session_lease(session_key: str, generation: int = 0) -> str:
    """释放执行权。generation 建议带上：不带的话可能把别人刚接手的租约删掉。"""
    path = f"/v1/leases/{urllib.parse.quote(session_key, safe='')}"
    if generation:
        path += f"?generation={int(generation)}"
    q = "&" if "?" in path else "?"
    path += f"{q}holder_id={urllib.parse.quote(_holder(), safe='')}"
    res, err = _lease_call("DELETE", path)
    if err:
        return err
    return f"已释放：{res['session_key']}。别人现在可以接手了。"


if __name__ == "__main__":
    mcp.run()
