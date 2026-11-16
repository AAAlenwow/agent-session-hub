"""场景 7：hook 与 MCP 两个"真实入口"。

前面测的是 HTTP 接口；这里测的是团队成员机器上实际被调用的东西：
cc_hook.py（Claude Code / GLM / MiniMax 共用）、codex_notify.py、mcp_query.py。
"""
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.parse

import pytest

import helpers as H
from conftest import RUN, uid

ROOT = H.ROOT
CC_HOOK = ROOT / "hooks" / "cc_hook.py"
CODEX_HOOK = ROOT / "hooks" / "codex_notify.py"
MCP = ROOT / "mcp" / "mcp_query.py"


def run_hook(script, payload, token, agent="claude-code", extra_env=None, timeout=20,
             argv=False):
    env = {
        **os.environ,
        "AGENT_HUB_URL": H.BASE_URL,
        "AGENT_HUB_TOKEN": token,
        "AGENT_HUB_AGENT": agent,
        "AGENT_HUB_WB_DIR": "/tmp/ahe2e-unused",
        **(extra_env or {}),
    }
    stdin = None
    if isinstance(payload, (dict, list)):
        stdin = json.dumps(payload)
    elif isinstance(payload, str):
        stdin = payload
    args = [sys.executable, str(script)]
    if argv:                      # Codex 的 notify 走 argv[1]，不是 stdin
        args.append(stdin or "{}")
        stdin = None
    p = subprocess.run(args, input=stdin, capture_output=True, text=True, env=env,
                       timeout=timeout)
    return p


def test_claude_code_hook_reports_all_event_kinds(people):
    p = people["bob"]
    sid, proj = uid("cc"), uid("proj")
    cwd = "/repos/" + proj
    cases = [
        ({"hook_event_name": "SessionStart", "source": "startup"}, "session_start"),
        ({"hook_event_name": "UserPromptSubmit", "prompt": "把限流改成令牌桶"}, "user_prompt"),
        ({"hook_event_name": "PostToolUse", "tool_name": "Skill",
          "tool_input": {"command": "code-review"}}, "skill_call"),
        ({"hook_event_name": "PostToolUse", "tool_name": "mcp__postgres__query",
          "tool_input": {}}, "mcp_call"),
        ({"hook_event_name": "PostToolUse", "tool_name": "Edit",
          "tool_input": {"file_path": "src/limit.py"}}, "tool_call"),
        ({"hook_event_name": "Stop"}, "turn_end"),
    ]
    for msg, _kind in cases:
        r = run_hook(CC_HOOK, {"session_id": sid, "cwd": cwd, **msg}, p["token"])
        assert r.returncode == 0, r.stderr

    kinds = H.sql(f"select string_agg(e.kind, ',' order by e.id) from events e "
                  f"join sessions s on s.id = e.session_id where s.ext_id = '{sid}'")
    assert kinds == "session_start,user_prompt,skill_call,mcp_call,tool_call,turn_end"
    # 技能名与 MCP 工具名要能直接被聚合出来（交接报告靠它）
    assert H.sql(f"select summary from events e join sessions s on s.id = e.session_id "
                 f"where s.ext_id = '{sid}' and e.kind = 'skill_call'") == "skill code-review"
    assert H.sql(f"select summary from events e join sessions s on s.id = e.session_id "
                 f"where s.ext_id = '{sid}' and e.kind = 'mcp_call'") == "mcp postgres: query"


def test_claude_code_hook_survives_bad_input(people):
    """hook 绝不能因为输入异常或网络异常阻塞用户敲代码。"""
    p = people["bob"]
    assert run_hook(CC_HOOK, "{not json", p["token"]).returncode == 0
    assert run_hook(CC_HOOK, "{}", p["token"]).returncode == 0
    assert run_hook(CC_HOOK, {"hook_event_name": "Unknown"}, p["token"]).returncode == 0
    assert run_hook(CC_HOOK, {"hook_event_name": "Stop"}, "").returncode == 0
    # 服务不可达
    env = {"AGENT_HUB_URL": "http://127.0.0.1:1"}
    r = run_hook(CC_HOOK, {"hook_event_name": "Stop", "session_id": "x"}, p["token"],
                 extra_env=env)
    assert r.returncode == 0


def test_glm_and_minimax_share_the_same_hook(people):
    p = people["bob"]
    for agent in ("glm-4.6", "minimax-m2"):
        sid = uid("cc")
        run_hook(CC_HOOK, {"session_id": sid, "cwd": "/repos/x",
                           "hook_event_name": "UserPromptSubmit", "prompt": "hi"},
                 p["token"], agent=agent)
        assert H.sql(f"select agent from sessions where ext_id = '{sid}'") == agent


def test_stop_hook_uploads_transcript_when_enabled(people, sandbox):
    p = people["bob"]
    sid, proj = uid("ccu"), uid("proj")
    cwd = "/repos/" + proj
    path = sandbox / f"{sid}.jsonl"
    path.write_bytes(H.cc_simple(cwd, sid, 2))

    r = run_hook(CC_HOOK, {"hook_event_name": "Stop", "session_id": sid,
                           "cwd": cwd, "transcript_path": str(path)},
                 p["token"], extra_env={"AGENT_HUB_UPLOAD_TRANSCRIPT": "1"})
    assert r.returncode == 0

    deadline = time.time() + 20
    while time.time() < deadline:
        n = H.sql(f"select count(*) from transcripts where ext_id = '{sid}'")
        if n == "1":
            break
        time.sleep(0.5)
    assert n == "1", "Stop hook 应后台把原件传上去"
    assert H.sql(f"select agent from transcripts where ext_id = '{sid}'") == "claude-code"


def test_codex_notify_reports_turn(people):
    p = people["bob"]
    proj = uid("proj")
    rollout = f"/home/u/.codex/sessions/2026/rollout-2026-11-12-{RUN}.jsonl"
    payload = json.dumps({"type": "agent-turn-complete", "cwd": "/repos/" + proj,
                          "rollout_path": rollout})
    r = run_hook(CODEX_HOOK, payload, p["token"], agent="codex", argv=True)
    assert r.returncode == 0
    ext = f"2026-11-12-{RUN}"
    assert H.sql(f"select agent || '|' || count(*) from sessions s join events e "
                 f"on e.session_id = s.id where s.ext_id = '{ext}' group by agent") == "codex|1"
    # 非回合结束的事件不该上报
    before = H.sql("select count(*) from events")
    run_hook(CODEX_HOOK, json.dumps({"type": "session-start"}), p["token"],
             agent="codex", argv=True)
    assert H.sql("select count(*) from events") == before


def test_stop_hook_carries_assistant_answer(people, sandbox):
    """turn_end 事件带 assistant 最终回答：换机后同事不拉原件、纯靠事件层
    就能搜到「上次修到哪/结论是什么」——思维链共享的验收路径。"""
    p = people["bob"]
    sid, proj = uid("ccs"), uid("proj")
    cwd = "/repos/" + proj
    marker = uid("圣诞")
    path = sandbox / f"{sid}.jsonl"
    turns = [
        {"role": "user", "text": f"{marker}活动的积分翻倍逻辑改一下"},
        {"role": "assistant", "text": f"已实现：{marker}活动满 100 送 50 积分，"
                                      f"入口在 gift.py 的 double_points 分支，"
                                      f"回归测试跑通",
         "file": "/srv/old-repo/src/gift.py"},
    ]
    path.write_bytes(H.cc_transcript(cwd, sid, turns))

    r = run_hook(CC_HOOK, {"hook_event_name": "Stop", "session_id": sid,
                           "cwd": cwd, "transcript_path": str(path)},
                 p["token"])
    assert r.returncode == 0, r.stderr

    summary = H.sql(f"select e.summary from events e join sessions s "
                    f"on s.id = e.session_id where s.ext_id = '{sid}' "
                    f"and e.kind = 'turn_end'")
    assert marker in summary, f"turn_end 摘要没带上 assistant 回答: {summary!r}"
    assert "gift.py" in summary
    # 关键验收：MCP 语义检索能靠回答本身命中（不用拉原件）
    row = H.sql(f"select e.summary from events e join sessions s on s.id = e.session_id "
                f"where e.summary ilike '%{marker}%' limit 1")
    assert row and marker in row


def test_stop_hook_without_text_falls_back(people):
    """transcript 缺失/无 assistant 正文时退占位符，绝不报错。"""
    p = people["bob"]
    sid = uid("ccn")
    r = run_hook(CC_HOOK, {"hook_event_name": "Stop", "session_id": sid,
                           "cwd": "/repos/x", "transcript_path": "/nonexistent.jsonl"},
                 p["token"])
    assert r.returncode == 0, r.stderr
    assert H.sql(f"select e.summary from events e join sessions s on s.id = e.session_id "
                 f"where s.ext_id = '{sid}' and e.kind = 'turn_end'") == \
        "assistant turn finished"


def test_codex_notify_carries_rollout_answer(people, sandbox):
    p = people["bob"]
    proj = uid("proj")
    marker = uid("德国")
    rollout = sandbox / f"rollout-2026-11-12-{RUN}-b.jsonl"
    rollout.write_text(json.dumps({
        "timestamp": "2026-11-12T00:00:00Z", "type": "response_item",
        "payload": {"type": "message", "role": "assistant",
                    "content": [{"type": "output_text",
                                 "text": f"{marker}活动迁移完成，配置见 act-de.json"}]},
    }) + "\n")
    payload = json.dumps({"type": "agent-turn-complete", "cwd": "/repos/" + proj,
                          "rollout_path": str(rollout)})
    r = run_hook(CODEX_HOOK, payload, p["token"], agent="codex", argv=True)
    assert r.returncode == 0, r.stderr
    ext = f"2026-11-12-{RUN}-b"
    assert H.sql(f"select e.summary from events e join sessions s on s.id = e.session_id "
                 f"where s.ext_id = '{ext}' and e.kind = 'turn_end'") == \
        f"{marker}活动迁移完成，配置见 act-de.json"


GENERIC = ROOT / "hooks" / "generic_report.py"


def test_generic_report_reports_machine_and_summary(people):
    """通用上报契约：dsh-go 这类插件不写 hook，直接调这个脚本即可接入。"""
    p = people["bob"]
    sid, proj = uid("gen"), uid("demo-shop")
    machine = uid("hk-server")
    payload = json.dumps({"session": {"ext_id": sid, "agent": "dsh",
                                      "project": proj, "machine": machine,
                                      "cwd": "/root/" + proj},
                          "events": [{"kind": "turn_end",
                                      "summary": "圣诞活动逻辑：满100送50"}]})
    r = run_hook(GENERIC, payload, p["token"])
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout

    assert H.sql(f"select agent || '|' || machine from sessions where ext_id = '{sid}'") \
        == f"dsh|{machine}"
    assert H.sql(f"select e.summary from events e join sessions s on s.id = e.session_id "
                 f"where s.ext_id = '{sid}'") == "圣诞活动逻辑：满100送50"


def test_codex_notify_ignores_bad_argv(people):
    p = people["bob"]
    assert run_hook(CODEX_HOOK, "not json", p["token"], argv=True).returncode == 0
    assert run_hook(CODEX_HOOK, "", p["token"], argv=True).returncode == 0


# ------------------------------------------------------------------ MCP

def _mcp_available() -> bool:
    try:
        import mcp.server.fastmcp  # noqa: F401  (mcp 1.x)
        return True
    except Exception:
        pass
    try:
        import mcp.server.mcpserver  # noqa: F401  (mcp 2.x)
        return True
    except Exception:
        return False


needs_mcp = pytest.mark.skipif(not _mcp_available(),
                               reason="未安装 mcp 包（pip install 'mcp[cli]'）")


def test_prompt_tool_names_match_mcp_source(people):
    """提示词里写的工具名必须真的在 MCP 服务里存在，否则 agent 会瞎调。"""
    src = MCP.read_text(encoding="utf-8")
    declared = set(re.findall(r"@mcp\.tool\(\)\s*\ndef (\w+)\(", src))
    assert declared, "没能从 mcp_query.py 解析出工具"
    prompt = people["alice"]["api"].get("/v1/agent-prompt").text
    missing = [n for n in declared if n not in prompt]
    assert not missing, f"提示词缺少工具说明：{missing}"


@needs_mcp
def test_mcp_tools_return_live_data(people, sandbox):
    """直接调用工具函数（与 agent 通过 MCP 调用走同一段逻辑）。"""
    sys.path.insert(0, str(ROOT / "mcp"))
    p = people["carol"]
    proj, sid = uid("mcpproj"), uid("cc")
    p["api"].post("/v1/events", {
        "session": {"ext_id": sid, "agent": "claude-code", "project": proj, "cwd": "/repos/" + proj},
        "events": [{"kind": "shared_note", "summary": "结论：改用令牌桶限流"}],
    })

    env_backup = {k: os.environ.get(k) for k in ("AGENT_HUB_URL", "AGENT_HUB_TOKEN")}
    os.environ["AGENT_HUB_URL"] = H.BASE_URL
    os.environ["AGENT_HUB_TOKEN"] = p["token"]
    try:
        import importlib
        import mcp_query
        importlib.reload(mcp_query)
        hit = mcp_query.search_team_memory("令牌桶")
        assert "令牌桶" in hit and p["name"] in hit
        assert "没有找到相关记录" in mcp_query.search_team_memory(uid("nohit"))
        sessions = mcp_query.recent_team_sessions(5)
        assert proj in sessions or sid[:8] in sessions or sessions
        handover = mcp_query.get_project_handover(proj)
        assert isinstance(handover, str) and handover
        assert "team-memory" in mcp_query.team_memory_instructions()
    finally:
        for k, v in env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@needs_mcp
def test_mcp_stdio_protocol(people):
    """真正走 MCP 协议（stdio JSON-RPC）：initialize → tools/list → tools/call。"""
    p = people["alice"]
    env = {**os.environ, "AGENT_HUB_URL": H.BASE_URL, "AGENT_HUB_TOKEN": p["token"]}
    proc = subprocess.Popen([sys.executable, str(MCP)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, env=env, bufsize=1)

    def rpc(obj, expect_id):
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()
        for _ in range(60):
            line = proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if msg.get("id") == expect_id:
                return msg
        return {}

    try:
        init = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05",
                               "capabilities": {},
                               "clientInfo": {"name": "e2e", "version": "1"}}}, 1)
        assert init.get("result", {}).get("serverInfo", {}).get("name") == "team-memory"
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        proc.stdin.flush()

        tools = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, 2)
        names = {t["name"] for t in tools.get("result", {}).get("tools", [])}
        assert {"search_team_memory", "share_team_memory", "team_memory_instructions"} <= names

        called = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                      "params": {"name": "team_memory_instructions", "arguments": {}}}, 3)
        text = json.dumps(called, ensure_ascii=False)
        assert "E2E 测试部署" in text, "MCP 应返回本部署的提示词"
    finally:
        proc.kill()


@needs_mcp
def test_mcp_share_then_search_roundtrip(people):
    """agent 分享 → 团队检索得到（自举闭环）。"""
    sys.path.insert(0, str(ROOT / "mcp"))
    p = people["carol"]
    proj = uid("share")
    env_backup = {k: os.environ.get(k) for k in ("AGENT_HUB_URL", "AGENT_HUB_TOKEN")}
    os.environ["AGENT_HUB_URL"] = H.BASE_URL
    os.environ["AGENT_HUB_TOKEN"] = p["token"]
    try:
        import importlib
        import mcp_query
        importlib.reload(mcp_query)
        marker = uid("结论标记")
        mcp_query.share_team_memory(title="限流方案定稿", summary=f"{marker} 用令牌桶",
                                    files=["src/limit.py"], tags=["限流"], project=proj)
        assert marker in mcp_query.search_team_memory(marker)
    finally:
        for k, v in env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ── team_bridge：dsh-team 的审计链上行到 hub ────────────────────────────────

TEAM_BRIDGE = ROOT / "hooks" / "team_bridge.py"

TEAM_DDL = """
create table team (id text primary key, name text);
create table member (id text primary key, team_id text, name text, role text);
create table event (id integer primary key autoincrement, team_id text not null,
  member_id text not null, task_id text, seq integer not null, ts text not null,
  phase text not null, action text not null, target text, result text, next text,
  meta_json text not null default '{}', unique(member_id, seq));
create table member_change (id text primary key, team_id text not null, task_id text,
  member_id text, path text not null, kind text not null default 'edit',
  diff text not null default '', added integer, deleted integer, created_at text not null);
create table approval (id text primary key, team_id text not null, member_id text,
  session_id text, tool_name text not null default '', reason text,
  status text not null default 'pending', decided_by text, decided_at text,
  created_at text not null, updated_at text not null);
create table audit (id integer primary key autoincrement, ts text not null, team_id text not null,
  actor text not null, domain text not null, action text not null, target text not null,
  before_hash text, after_hash text, result text not null default 'OK', detail_json text);
"""


def _make_team_db(path, team_id, proj, marker):
    import sqlite3
    con = sqlite3.connect(path)
    con.executescript(TEAM_DDL)
    con.execute("insert into team values (?,?)", (team_id, proj))
    con.execute("insert into member values ('m1', ?, 'dev', '实现成员')", (team_id,))
    con.execute(
        "insert into event (team_id, member_id, task_id, seq, ts, phase, action, target, result)"
        " values (?,?,?,?,?,?,?,?,?)",
        (team_id, "m1", "t1", 1, "2026-11-16T08:00:00.000Z", "done", "task.done", marker, "OK"))
    con.execute("insert into member_change values ('c1',?,'t1','m1','gift.py','edit','',18,0,"
                "'2026-11-16T08:00:01.000Z')", (team_id,))
    con.commit()
    con.close()


def test_team_bridge_lands_audit_chain_on_hub(people, tmp_path):
    """dsh-team 的任务/改动/审批记录推到 hub，换机后能搜到（这是 P1 上行）。"""
    p = people["alice"]
    team_id, proj, marker = uid("team"), uid("teamproj"), uid("圣诞活动满100送50")
    db = tmp_path / "team.db"
    _make_team_db(db, team_id, proj, marker)

    env = dict(os.environ)
    env.update({
        "AGENT_HUB_URL": H.BASE_URL, "AGENT_HUB_TOKEN": p["token"],
        "AGENT_HUB_TEAM_DB": str(db),
        "AGENT_HUB_TEAM_WATERMARK": str(tmp_path / "wm.json"),
        "AGENT_HUB_MACHINE": "e2e-mac",
    })
    r = subprocess.run([sys.executable, str(TEAM_BRIDGE), "--once", "--verbose"],
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr

    row = H.sql(f"select ext_id, agent, machine, project from sessions "
                f"where ext_id = 'team:{team_id}'")
    assert f"team:{team_id}" in row and "dsh-team" in row
    assert "e2e-mac" in row and proj in row

    hits = p["api"].get("/v1/search?q=" + urllib.parse.quote(marker)).json()
    assert any(h["agent"] == "dsh-team" for h in hits), hits

    changes = H.sql(f"select summary from events e join sessions s on s.id = e.session_id "
                    f"where s.ext_id = 'team:{team_id}' and e.kind = 'team_change'")
    assert "gift.py" in changes and "+18/-0" in changes


def test_team_bridge_is_idempotent(people, tmp_path):
    """水位生效：重复跑不会把同一批事件灌两遍。"""
    p = people["alice"]
    team_id, proj, marker = uid("team2"), uid("teamproj2"), uid("幂等标记")
    db = tmp_path / "team.db"
    _make_team_db(db, team_id, proj, marker)
    env = dict(os.environ)
    env.update({
        "AGENT_HUB_URL": H.BASE_URL, "AGENT_HUB_TOKEN": p["token"],
        "AGENT_HUB_TEAM_DB": str(db),
        "AGENT_HUB_TEAM_WATERMARK": str(tmp_path / "wm.json"),
        "AGENT_HUB_MACHINE": "e2e-mac",
    })
    cmd = [sys.executable, str(TEAM_BRIDGE), "--once"]
    assert subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60).returncode == 0
    first = H.sql(f"select count(*) from events e join sessions s on s.id = e.session_id "
                  f"where s.ext_id = 'team:{team_id}'")
    assert subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60).returncode == 0
    second = H.sql(f"select count(*) from events e join sessions s on s.id = e.session_id "
                   f"where s.ext_id = 'team:{team_id}'")
    assert first == second, f"{first} -> {second}：重复推送了"
