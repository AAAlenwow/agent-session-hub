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


def test_agent_name_is_recorded(people):
    """GLM / MiniMax 走同一套 hook，靠 agent 字段区分。"""
    api, proj, sid = people["alice"]["api"], uid("svc"), uid("sess")
    post_events(api, sid, proj, agent="glm-4.6")
    assert H.sql(f"select agent from sessions where ext_id = '{sid}'") == "glm-4.6"
