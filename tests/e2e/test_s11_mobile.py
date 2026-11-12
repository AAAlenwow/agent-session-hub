"""场景 11：手机端只读视图 /m。

验证「不带电脑、只用手机浏览器也能看团队会话」这条链路：
登录（token → cookie）→ 机器分组列表 → 会话时间线 → 搜索 → 退出。
页面是服务端渲染的 HTML，测试用真实 HTTP + 真 cookie，不 mock。
"""
import urllib.error
import urllib.parse
import urllib.request

import helpers as H
from conftest import uid


def _post_no_redirect(path: str, form: dict):
    """登录接口是 303 跳转；测试要看到 Set-Cookie 本身，所以禁用重定向。"""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    req = urllib.request.Request(
        H.BASE_URL + path,
        data=urllib.parse.urlencode(form).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=15) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:      # 3xx 在禁用重定向时以异常形式抛出
        return e.code, dict(e.headers or {}), e.read()


def _ingest(api, sid, project, machine, summary="改了圣诞活动积分逻辑"):
    return api.post("/v1/events", {
        "session": {"ext_id": sid, "agent": "claude-code", "project": project,
                    "machine": machine, "cwd": "/home/u/" + project},
        "events": [{"kind": "turn_end", "summary": summary}],
    })


def test_mobile_requires_token(people):
    """没带 token：只给登录页，绝不泄露任何会话内容。"""
    r = H.Api().get("/m")
    assert r.status == 200
    assert "个人 token" in r.text
    assert "团队会话" in r.text


def test_mobile_login_sets_cookie_and_lists_machine(people):
    """登录后能看到「机器 → 会话」的列表，且机器名展示出来。"""
    p = people["carol"]
    proj, sid = uid("mob"), uid("sess")
    machine = uid("hechao-mac")
    assert _ingest(p["api"], sid, proj, machine).status == 200

    api = H.Api()
    status, headers, _body = _post_no_redirect("/m/login", {"token": p["token"]})
    assert status in (200, 303), status
    cookie = headers.get("Set-Cookie", "") or headers.get("set-cookie", "")
    assert "ah_token=" in cookie and "HttpOnly" in cookie

    listing = api.get("/m", headers={"Cookie": cookie.split(";")[0]})
    assert listing.status == 200
    assert machine in listing.text and proj in listing.text

    # 按机器过滤
    only = api.get("/m?machine=" + urllib.parse.quote(machine),
                   headers={"Cookie": cookie.split(";")[0]})
    assert only.status == 200 and proj in only.text


def test_mobile_token_in_query_works(people):
    """没登录时也允许 ?token= 直链（手机扫码/分享场景），内容一致。"""
    p = people["carol"]
    proj, sid = uid("mobq"), uid("sess")
    machine = uid("hk-server")
    assert _ingest(p["api"], sid, proj, machine, summary="香港服务器上的限流改动").status == 200

    r = H.Api().get(f"/m?token={p['token']}")
    assert r.status == 200
    assert machine in r.text


def test_mobile_session_timeline(people):
    """点进某条会话，能看到 turn_end 的思维链正文（手机上的核心价值）。"""
    p = people["carol"]
    proj, sid = uid("mobt"), uid("sess")
    machine = uid("mob-timeline-mac")
    marker = uid("圣诞逻辑")
    r = _ingest(p["api"], sid, proj, machine, summary=f"{marker}：满100送50")
    sid_db = r.json()["session_id"]

    page = H.Api().get(f"/m/s/{sid_db}?token={p['token']}")
    assert page.status == 200
    assert marker in page.text and "turn_end" in page.text
    assert machine in page.text
    # 不存在的会话给友好页面，不是 500
    assert H.Api().get(f"/m/s/99999999?token={p['token']}").status == 200


def test_mobile_search(people):
    p = people["carol"]
    marker = uid("德国活动")
    assert _ingest(p["api"], uid("sess"), uid("mobs"), uid("mob-search-mac"),
                   summary=f"{marker} 迁移到新配置").status == 200
    r = H.Api().get(f"/m?q={urllib.parse.quote(marker)}&token={p['token']}")
    assert r.status == 200 and marker in r.text


def test_mobile_bad_token_is_rejected(people):
    r = H.Api().get("/m?token=deadbeef")
    assert r.status == 200 and "个人 token" in r.text
    r = H.Api().post("/m/login", body=b"token=deadbeef",
                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status == 200 and "无效" in r.text
