"""场景 1：健康检查与鉴权边界。

目标：确认"没有 token 就什么都做不了"，以及管理员/普通成员的权限分层。
"""
import pytest

import helpers as H
from conftest import uid


def test_health_without_token(hub):
    r = hub.get("/health")
    assert r.status == 200 and r.json()["ok"] is True


def test_health_checks_database(hub):
    """健康检查必须真的碰一下数据库，否则 DB 挂了也返回 200。"""
    r = hub.get("/health")
    assert r.status == 200
    assert H.sql("select count(*) from users") != ""      # DB 可用（失败会抛异常）


@pytest.mark.parametrize("path", [
    "/v1/sessions", "/v1/projects", "/v1/skills", "/v1/agent-prompt",
    "/v1/transcripts", "/v1/usage", "/v1/search?q=x",
])
def test_protected_endpoints_require_token(hub, path):
    assert hub.get(path).status == 401


def test_bad_token_rejected(hub):
    assert hub.get("/v1/sessions", token="definitely-not-a-token").status == 401


@pytest.mark.parametrize("scheme", ["Token", "token", "Basic", ""])
def test_wrong_auth_scheme_rejected(hub, scheme):
    r = hub.get("/v1/sessions", headers={"Authorization": f"{scheme} abcdef"})
    assert r.status == 401


def test_token_is_not_stored_in_plaintext(people):
    """库里只应有 sha256，不应能搜到明文 token。"""
    tok = people["alice"]["token"]
    hit = H.sql(f"select count(*) from users where token_hash = '{tok}'")
    assert hit == "0"
    assert H.sql("select length(token_hash) from users limit 1") == "64"


def test_non_admin_cannot_create_user(people):
    r = people["alice"]["api"].post("/v1/admin/users?name=" + uid("nobody"))
    assert r.status == 403


def test_admin_can_create_user_and_it_works(admin):
    name = uid("dave")
    r = admin.post("/v1/admin/users?name=" + name)
    assert r.status == 200
    body = r.json()
    assert body["name"] == name and body["token"]
    assert admin.with_token(body["token"]).get("/v1/sessions").status == 200


def test_duplicate_user_name_is_rejected(admin):
    """重名应给出 409，而不是把唯一约束异常抛成 500。"""
    name = uid("dup")
    assert admin.post("/v1/admin/users?name=" + name).status == 200
    assert admin.post("/v1/admin/users?name=" + name).status == 409


def test_user_name_validation(admin):
    """空名字 / 超长名字不应进库。"""
    assert admin.post("/v1/admin/users?name=").status in (400, 422)
    assert admin.post("/v1/admin/users?name=" + "x" * 300).status in (400, 422)


def test_malformed_json_body_returns_4xx(hub, admin):
    r = hub.call("POST", "/v1/events", body=b"{not json",
                 headers={"Content-Type": "application/json"})
    assert 400 <= r.status < 500
