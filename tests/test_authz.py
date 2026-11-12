"""写接口鉴权：负责人时间线与会话回滚不能被普通成员随意改（P0-3 的回归锁）。"""
import asyncio

import pytest

fastapi = pytest.importorskip("fastapi")

import app as app_module            # noqa: E402  server/app.py
import transcripts_api              # noqa: E402


class FakePool:
    """按 SQL 片段返回预置结果的最小 fake，够用来测鉴权分支。"""

    def __init__(self, routes=None):
        self.routes = routes or []
        self.executed = []

    async def fetchrow(self, sql, *args):
        for key, val in self.routes:
            if key in sql:
                return val
        return None

    async def fetchval(self, sql, *args):
        row = await self.fetchrow(sql, *args)
        return list(row.values())[0] if isinstance(row, dict) else row

    async def fetch(self, sql, *args):
        return []

    async def execute(self, sql, *args):
        self.executed.append(sql)
        return "OK"


ADMIN = {"id": 1, "name": "root", "is_admin": True}
MEMBER = {"id": 2, "name": "zhangsan", "is_admin": False}
OTHER = {"id": 3, "name": "lisi", "is_admin": False}


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------- POST /v1/projects

def test_member_cannot_overwrite_others_project():
    pool = FakePool([
        ("select id from projects where name", {"id": 7}),
        ("from project_owners", {"user_id": OTHER["id"]}),
    ])
    app_module.app.state.pool = pool
    body = app_module.ProjectIn(name="api", repo="git@x/api.git", description="hijack")
    with pytest.raises(fastapi.HTTPException) as e:
        run(app_module.create_project(body, user=MEMBER))
    assert e.value.status_code == 403
    assert pool.executed == []


def test_owner_can_update_own_project():
    pool = FakePool([
        ("select id from projects where name", {"id": 7}),
        ("from project_owners", {"user_id": MEMBER["id"]}),
        ("update projects set", {"id": 7}),
    ])
    app_module.app.state.pool = pool
    body = app_module.ProjectIn(name="api", repo="", description="新描述")
    out = run(app_module.create_project(body, user=MEMBER))
    assert out["id"] == 7 and out["updated"] is True


def test_admin_can_update_any_project():
    pool = FakePool([
        ("select id from projects where name", {"id": 7}),
        ("update projects set", {"id": 7}),
    ])
    app_module.app.state.pool = pool
    out = run(app_module.create_project(app_module.ProjectIn(name="api"), user=ADMIN))
    assert out["updated"] is True


def test_unowned_project_can_be_claimed_by_anyone():
    """项目还没有负责人时要放行，否则第一个 owner 永远指派不进来。"""
    pool = FakePool([
        ("select id from projects where name", {"id": 7}),
        ("from project_owners", None),
        ("update projects set", {"id": 7}),
    ])
    app_module.app.state.pool = pool
    out = run(app_module.create_project(app_module.ProjectIn(name="api"), user=MEMBER))
    assert out["updated"] is True


# --------------------------------------------------- POST /{pid}/assign

def test_member_cannot_reassign_owner():
    pool = FakePool([
        ("select 1 from projects", {"1": 1}),
        ("from project_owners", {"user_id": OTHER["id"]}),
    ])
    app_module.app.state.pool = pool
    with pytest.raises(fastapi.HTTPException) as e:
        run(app_module.assign_role(7, app_module.AssignIn(user="zhangsan"), user=MEMBER))
    assert e.value.status_code == 403
    assert pool.executed == []


def test_invalid_role_is_rejected():
    app_module.app.state.pool = FakePool()
    with pytest.raises(fastapi.HTTPException) as e:
        run(app_module.assign_role(7, app_module.AssignIn(user="zhangsan", role="superuser"),
                                   user=ADMIN))
    assert e.value.status_code == 400


# ------------------------------------------------- POST /transcripts/{id}/revert

def _request():
    from starlette.requests import Request
    return Request({"type": "http", "method": "POST", "path": "/", "headers": [],
                    "app": app_module.app})


def test_member_cannot_revert_others_transcript():
    app_module.app.state.pool = FakePool([
        ("from transcripts where id", {"id": 1, "rev": 3, "user_id": OTHER["id"],
                                       "agent": "workbuddy"}),
    ])
    with pytest.raises(fastapi.HTTPException) as e:
        run(transcripts_api.revert_revision(1, _request(), revision=1, message="",
                                            user=MEMBER))
    assert e.value.status_code == 403


def test_admin_can_revert_any_transcript():
    """管理员被放行后应继续往下走（这里只验证没有在鉴权处被拦）。"""
    pool = FakePool([
        ("from transcripts where id", {"id": 1, "rev": 3, "user_id": OTHER["id"],
                                       "agent": "workbuddy"}),
        ("from transcript_revisions where transcript_id", None),
    ])
    app_module.app.state.pool = pool
    with pytest.raises(fastapi.HTTPException) as e:
        run(transcripts_api.revert_revision(1, _request(), revision=99, message="",
                                            user=ADMIN))
    assert e.value.status_code == 404        # 走到"版本不存在"，说明鉴权已放行
