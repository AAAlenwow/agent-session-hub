"""场景 2：事件上报、项目自动关联、脱敏、检索。

hooks 每回合都打这个接口，所以幂等、脏数据、脱敏三件事必须在这里锁死。
"""
import time
import urllib.parse

import pytest

import helpers as H
from conftest import uid


def post_events(api, sid, project, cwd="", repo="", events=None, title="", agent="claude-code"):
    body = {
        "session": {"ext_id": sid, "agent": agent, "project": project,
                    "cwd": cwd, "repo": repo, "title": title},
        "events": events or [{"kind": "user_prompt", "summary": "初始提问"}],
    }
    return api.post("/v1/events", body)


def test_ingest_creates_session_and_events(people):
    api, proj = people["alice"]["api"], uid("svc")
    sid = uid("sess")
    r = post_events(api, sid, proj, cwd="/repo/" + proj,
                    events=[{"kind": "user_prompt", "summary": "怎么改限流"},
                            {"kind": "tool_call", "summary": "Edit: a.py", "files": ["a.py"]},
                            {"kind": "turn_end", "summary": "done"}])
    assert r.status == 200
    d = r.json()
    assert d["ok"] and d["ingested"] == 3

    listed = api.get(f"/v1/sessions?limit=200").json()
    assert any(s["ext_id"] == sid for s in listed)

    detail = api.get(f"/v1/sessions/{d['session_id']}").json()
    kinds = [e["kind"] for e in detail["events"]]
    assert kinds == ["user_prompt", "tool_call", "turn_end"]
    assert detail["session"]["owner"] == people["alice"]["name"]


def test_project_is_auto_created_from_session(people):
    api, proj = people["alice"]["api"], uid("auto")
    post_events(api, uid("sess"), proj, repo="git@corp:" + proj)
    rows = api.get("/v1/projects").json()
    hit = [p for p in rows if p["name"] == proj]
    assert hit, "上报时应自动建项目"
    assert hit[0]["repo"] == "git@corp:" + proj
    assert hit[0]["session_count"] >= 1


def test_same_ext_id_upserts_instead_of_duplicating(people):
    api, proj, sid = people["alice"]["api"], uid("svc"), uid("sess")
    first = post_events(api, sid, proj).json()
    second = post_events(api, sid, proj, events=[{"kind": "turn_end", "summary": "第二轮"}]).json()
    assert first["session_id"] == second["session_id"]
    assert H.sql(f"select count(*) from sessions where ext_id = '{sid}'") == "1"
    assert H.sql(f"select count(*) from events where session_id = {first['session_id']}") == "2"


def test_empty_title_does_not_erase_existing(people):
    api, proj, sid = people["alice"]["api"], uid("svc"), uid("sess")
    post_events(api, sid, proj, title="限流改造")
    post_events(api, sid, proj, title="")
    assert H.sql(f"select title from sessions where ext_id = '{sid}'") == "限流改造"


def test_empty_repo_does_not_wipe_project_repo(people):
    """hooks 常常拿不到 git remote，会把 repo 传空；不能因此把已有 remote 抹掉。"""
    api, proj = people["alice"]["api"], uid("svc")
    post_events(api, uid("sess"), proj, repo="git@corp:team/" + proj)
    post_events(api, uid("sess"), proj, repo="")
    assert H.sql(f"select repo from projects where name = '{proj}'") == "git@corp:team/" + proj


def test_summary_is_redacted_before_storage(people):
    api, proj, sid = people["alice"]["api"], uid("svc"), uid("sess")
    post_events(api, sid, proj, events=[
        {"kind": "user_prompt", "summary": "我的 key 是 sk-abcdefghijklmnop1234 帮我配一下"},
        {"kind": "tool_call", "summary": "password: supersecret123 写进 env"},
    ])
    stored = H.sql(f"select string_agg(summary, ' | ') from events e join sessions s "
                   f"on s.id = e.session_id where s.ext_id = '{sid}'")
    assert "sk-abcdefghijklmnop1234" not in stored
    assert "[REDACTED-KEY]" in stored
    assert "supersecret123" not in stored
    assert "[REDACTED-CREDENTIAL]" in stored


def test_title_is_redacted(people):
    api, proj, sid = people["alice"]["api"], uid("svc"), uid("sess")
    post_events(api, sid, proj, title="调试 token=abcdefgh12345678 的问题")
    assert "abcdefgh12345678" not in H.sql(f"select title from sessions where ext_id = '{sid}'")


def test_search_by_summary_title_project_and_file(people):
    api, proj, sid = people["alice"]["api"], uid("ratelimit"), uid("sess")
    post_events(api, sid, proj, title="网关限流改造",
                events=[{"kind": "tool_call", "summary": "Edit: gateway/middleware.py",
                         "files": ["gateway/middleware.py"]}])
    assert api.get(f"/v1/search?q={proj}").json(), "按项目名应能搜到"
    assert api.get("/v1/search?q=" + urllib.parse.quote("限流改造")).json(), "按标题应能搜到"
    assert api.get("/v1/search?q=middleware.py").json(), "按文件名应能搜到"


def test_search_is_case_insensitive(people):
    api, proj, sid = people["alice"]["api"], uid("svc"), uid("sess")
    post_events(api, sid, proj, events=[{"kind": "user_prompt", "summary": "Fix The N+1 Query"}])
    assert api.get("/v1/search?q=the+n%2B1+query").json()


def test_search_no_result_returns_empty_list(people):
    r = people["alice"]["api"].get("/v1/search?q=" + uid("nothing-matches"))
    assert r.status == 200 and r.json() == []


def test_search_special_chars_do_not_break(people):
    """% 和 _ 是 LIKE 通配符；不能让它们变成全表扫描或语法错误。"""
    api = people["alice"]["api"]
    for q in ("%", "_", "'", "\\", "100%覆盖"):
        assert api.get("/v1/search?q=" + urllib.parse.quote(q)).status == 200


def test_limit_bounds_are_clamped(people):
    api = people["alice"]["api"]
    assert api.get("/v1/sessions?limit=0").status == 200
    assert len(api.get("/v1/sessions?limit=-5").json()) <= 1
    assert len(api.get("/v1/sessions?limit=100000").json()) <= 200


def test_invalid_timestamp_does_not_500(people):
    api, proj, sid = people["alice"]["api"], uid("svc"), uid("sess")
    r = post_events(api, sid, proj,
                    events=[{"kind": "user_prompt", "summary": "x", "ts": "not-a-date"}])
    assert 400 <= r.status < 500, "非法时间戳应被拒绝，而不是把数据库异常抛成 500"


def test_unknown_session_id_is_404(people):
    assert people["alice"]["api"].get("/v1/sessions/99999999").status == 404


def test_event_ts_is_accepted_and_stored(people):
    """2026-11-16 修的 500 回归：ts 声明为 str 直传 asyncpg 会崩，
    第三方上报器（team_bridge / dsh-go / cron 脚本）都带时间戳。"""
    api, proj, sid = people["alice"]["api"], uid("tsproj"), uid("tssess")
    r = post_events(api, sid, proj, events=[
        {"kind": "team_event", "summary": "第一件事", "ts": "2026-11-16T06:07:44.969Z"},
        {"kind": "team_event", "summary": "第二件事", "ts": "2026-11-16T14:07:45+08:00"},
    ])
    assert r.status == 200, r.text
    detail = api.get(f"/v1/sessions/{r.json()['session_id']}").json()
    evs = [e for e in detail["events"] if e["kind"] == "team_event"]
    assert [e["summary"] for e in evs] == ["第一件事", "第二件事"]
    assert evs[0]["ts"].startswith("2026-11-16T06:07:44")
    assert evs[1]["ts"].startswith("2026-11-16T06:07:45")


def test_bad_ts_is_422_not_500(people):
    api, proj = people["alice"]["api"], uid("tsbad")
    r = post_events(api, uid("badsess"), proj,
                    events=[{"kind": "x", "summary": "y", "ts": "昨天下午"}])
    assert r.status == 422


def test_agent_name_is_recorded(people):
    """GLM / MiniMax 走同一套 hook，靠 agent 字段区分。"""
    api, proj, sid = people["alice"]["api"], uid("svc"), uid("sess")
    post_events(api, sid, proj, agent="glm-4.6")
    assert H.sql(f"select agent from sessions where ext_id = '{sid}'") == "glm-4.6"


def test_machine_is_recorded_and_queryable(people):
    """机器溯源：会话从哪台机器传上来的，必须能查、能按机器过滤、能汇总。"""
    api = people["alice"]["api"]
    proj, sid = uid("mch"), uid("sess")
    m = uid("alice-mac")
    body = {"session": {"ext_id": sid, "agent": "claude-code", "project": proj,
                        "machine": m, "cwd": "/home/u/" + proj},
            "events": [{"kind": "turn_end", "summary": "改了圣诞活动"}]}
    assert api.post("/v1/events", body).status == 200

    rows = api.get(f"/v1/sessions?machine={urllib.parse.quote(m)}").json()
    assert [s["ext_id"] for s in rows] == [sid]
    assert rows[0]["machine"] == m

    # 搜索结果也要带机器名（手机端 / agent 靠它判断是哪台机子上的活）
    hits = api.get(f"/v1/search?q={urllib.parse.quote('圣诞')}").json()
    assert any(h["session_id"] and h.get("machine") == m for h in hits)

    machines = api.get("/v1/machines").json()
    hit = [x for x in machines if x["machine"] == m]
    assert hit and hit[0]["session_count"] >= 1

    # 同一会话再次上报但不带 machine：不能把已有机器名冲掉
    body["session"].pop("machine")
    body["events"] = [{"kind": "turn_end", "summary": "继续改"}]
    assert api.post("/v1/events", body).status == 200
    assert H.sql(f"select machine from sessions where ext_id = '{sid}'") == m
