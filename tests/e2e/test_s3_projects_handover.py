"""场景 3：项目、负责人时间线、交接报告。

这是"负责人换人后能接手"的核心链路：指派 → 释放 → 交接报告里能看到上一任的一切。
"""
import uuid

import helpers as H
from conftest import uid


def ingest(api, sid, project, events, cwd="", title=""):
    return api.post("/v1/events", {
        "session": {"ext_id": sid, "agent": "claude-code", "project": project,
                    "cwd": cwd or "/repo/" + project, "title": title},
        "events": events,
    })


def make_project(admin_api, name, repo="", desc=""):
    r = admin_api.post("/v1/projects", {"name": name, "repo": repo, "description": desc})
    assert r.status == 200, r
    return r.json()["id"]


def test_create_then_update_metadata(admin, people):
    name = uid("proj")
    pid = make_project(admin, name, repo="git@corp:team/" + name, desc="订单服务")
    r = admin.post("/v1/projects", {"name": name, "repo": "", "description": ""})
    assert r.status == 200 and r.json()["updated"] is True
    row = H.sql(f"select repo || '|' || description from projects where id = {pid}")
    assert row == f"git@corp:team/{name}|订单服务", "空字段不应覆盖已有档案"


def test_member_cannot_edit_someone_elses_project(people, admin):
    name = uid("proj")
    pid = make_project(admin, name)
    admin.post(f"/v1/projects/{pid}/assign", {"user": people["alice"]["name"], "role": "owner"})
    r = people["bob"]["api"].post("/v1/projects", {"name": name, "repo": "git@evil/x"})
    assert r.status == 403, "非负责人不应能改项目档案"


def test_owner_timeline_and_handover(people, admin):
    name = uid("handover")
    pid = make_project(admin, name, repo="git@corp:order-svc", desc="订单服务")
    alice, bob, carol = people["alice"], people["bob"], people["carol"]

    # 无 owner 时可自荐（bootstrap），否则第一个负责人永远指派不进来
    assert alice["api"].post(f"/v1/projects/{pid}/assign",
                             {"user": bob["name"], "role": "owner"}).status == 200

    # 上一任（bob）干活：提问、技能、MCP、共享结论、改文件
    sid = uid("sess")
    ingest(bob["api"], sid, name, [
        {"kind": "user_prompt", "summary": "订单超时怎么排查"},
        {"kind": "skill_call", "summary": "skill debugging"},
        {"kind": "mcp_call", "summary": "mcp postgres: query"},
        {"kind": "tool_call", "summary": "Edit: order/service.py", "files": ["order/service.py"]},
        {"kind": "tool_call", "summary": "Edit: order/service.py", "files": ["order/service.py"]},
        {"kind": "shared_note", "summary": "结论：超时是连接池 20 太小，调到 50"},
    ], title="订单超时排查")

    # 非负责人不能改任命
    assert carol["api"].post(f"/v1/projects/{pid}/assign",
                             {"user": carol["name"], "role": "owner"}).status == 403

    # 现任 owner 交接给 carol
    assert bob["api"].post(f"/v1/projects/{pid}/assign",
                           {"user": carol["name"], "role": "owner"}).status == 200
    # 已卸任的人不再有权限
    assert bob["api"].post(f"/v1/projects/{pid}/assign",
                           {"user": bob["name"], "role": "owner"}).status == 403

    rpt = carol["api"].get(f"/v1/projects/{pid}/handover").json()
    assert rpt["project"]["name"] == name
    assert rpt["previous_owner"]["owner"] == bob["name"]
    assert rpt["previous_owner"]["released_at"] is not None
    assert [s["id"] for s in rpt["previous_owner_sessions"]], "应能看到上一任的会话"
    assert rpt["top_files"][0]["file"] == "order/service.py"
    assert rpt["top_files"][0]["touches"] == 2
    skills = {(s["skill"], s["kind"]) for s in rpt["skills_used"]}
    assert ("skill debugging", "skill_call") in skills
    assert ("mcp postgres: query", "mcp_call") in skills
    assert "连接池 20 太小" in rpt["shared_notes"][0]["summary"]
    assert rpt["recent_prompts"][0]["summary"] == "订单超时怎么排查"

    # 时间线：两个 owner 记录，前一条已释放
    owners = [o for o in rpt["ownership"] if o["role"] == "owner"]
    assert [(o["owner"], o["released_at"] is not None) for o in owners] == \
           [(bob["name"], True), (carol["name"], False)]


def test_handover_by_project_name(people, admin):
    name = uid("byname")
    pid = make_project(admin, name)
    admin.post(f"/v1/projects/{pid}/assign", {"user": people["alice"]["name"], "role": "owner"})
    r = people["alice"]["api"].get(f"/v1/projects/{name}/handover")
    assert r.status == 200 and r.json()["project"]["id"] == pid


def test_handover_without_previous_owner(admin, people):
    name = uid("fresh")
    pid = make_project(admin, name)
    d = people["alice"]["api"].get(f"/v1/projects/{pid}/handover").json()
    assert d["previous_owner"] is None
    assert d["previous_owner_sessions"] == [] and d["skills_used"] == []


def test_assign_member_does_not_release_owner(people, admin):
    name = uid("member")
    pid = make_project(admin, name)
    alice, bob = people["alice"], people["bob"]
    admin.post(f"/v1/projects/{pid}/assign", {"user": alice["name"], "role": "owner"})
    alice["api"].post(f"/v1/projects/{pid}/assign", {"user": bob["name"], "role": "member"})
    rpt = alice["api"].get(f"/v1/projects/{pid}/handover").json()
    actives = [o for o in rpt["ownership"] if o["released_at"] is None]
    assert {(o["owner"], o["role"]) for o in actives} == \
           {(alice["name"], "owner"), (bob["name"], "member")}


def test_reassign_same_owner_is_noop(people, admin):
    """重复指派同一人不能产生零时长任期（否则交接报告的
    previous_owner 会变成现任本人，上一任档案失真）。"""
    name = uid("same-owner")
    pid = make_project(admin, name)
    alice, bob = people["alice"], people["bob"]
    admin.post(f"/v1/projects/{pid}/assign", {"user": alice["name"], "role": "owner"})
    d = admin.post(f"/v1/projects/{pid}/assign",
                   {"user": alice["name"], "role": "owner"}).json()
    assert d.get("unchanged") is True
    # 真正换人后，previous_owner 是上一任（alice），不是现任（bob）本人
    admin.post(f"/v1/projects/{pid}/assign", {"user": bob["name"], "role": "owner"})
    rpt = bob["api"].get(f"/v1/projects/{pid}/handover").json()
    assert rpt["previous_owner"]["owner"] == alice["name"]
    tenures = [o for o in rpt["ownership"] if o["role"] == "owner"]
    assert len(tenures) == 2, tenures  # alice + bob，没有零时长任期


def test_assign_error_cases(people, admin):
    name = uid("err")
    pid = make_project(admin, name)
    api = people["alice"]["api"]
    assert api.post(f"/v1/projects/{pid}/assign", {"user": "ghost-user", "role": "owner"}).status == 404
    assert api.post(f"/v1/projects/{pid}/assign",
                    {"user": people["alice"]["name"], "role": "superuser"}).status == 400
    assert admin.post("/v1/projects/99999999/assign",
                      {"user": people["alice"]["name"], "role": "owner"}).status == 404
    assert api.get("/v1/projects/99999999/handover").status == 404


def test_projects_list_has_owner_and_stats(people, admin):
    name = uid("stats")
    pid = make_project(admin, name)
    admin.post(f"/v1/projects/{pid}/assign", {"user": people["bob"]["name"], "role": "owner"})
    ingest(people["bob"]["api"], uid("sess"), name, [{"kind": "user_prompt", "summary": "x"}])
    ingest(people["carol"]["api"], uid("sess"), name, [{"kind": "user_prompt", "summary": "y"}])
    row = [p for p in people["alice"]["api"].get("/v1/projects").json() if p["id"] == pid][0]
    assert row["current_owner"] == people["bob"]["name"]
    assert row["session_count"] >= 2 and row["contributors"] >= 2


def test_skills_aggregation_filtered_by_project(people):
    name = uid("skills")
    sid = uid("sess")
    ingest(people["alice"]["api"], sid, name, [
        {"kind": "skill_call", "summary": "skill test-driven-development"},
        {"kind": "skill_call", "summary": "skill test-driven-development"},
        {"kind": "mcp_call", "summary": "mcp sheetagent: read_table"},
        {"kind": "user_prompt", "summary": "不该出现在技能表里"},
    ])
    rows = people["alice"]["api"].get(f"/v1/skills?project={name}").json()
    got = {r["skill"]: r["uses"] for r in rows}
    assert got.get("skill test-driven-development") == 2
    assert "mcp sheetagent: read_table" in got
    assert all(r["skill"] != "不该出现在技能表里" for r in rows)
