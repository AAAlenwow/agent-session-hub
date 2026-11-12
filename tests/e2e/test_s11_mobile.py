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


def _no_redirect():
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    return urllib.request.build_opener(NoRedirect)


def _post_no_redirect(path: str, form: dict):
    """登录接口是 303 跳转；测试要看到 Set-Cookie 本身，所以禁用重定向。"""
    req = urllib.request.Request(
        H.BASE_URL + path,
        data=urllib.parse.urlencode(form).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with _no_redirect().open(req, timeout=15) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:      # 3xx 在禁用重定向时以异常形式抛出
        return e.code, dict(e.headers or {}), e.read()


def _get_no_redirect(path: str):
    """?token= 直链现在是 303 换 cookie；同样禁用重定向看 Set-Cookie。"""
    try:
        with _no_redirect().open(H.BASE_URL + path, timeout=15) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
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
    # 会话数据不允许被浏览器 / 代理缓存
    assert "no-store" in listing.headers.get("cache-control", "")

    # 按机器过滤
    only = api.get("/m?machine=" + urllib.parse.quote(machine),
                   headers={"Cookie": cookie.split(";")[0]})
    assert only.status == 200 and proj in only.text


def test_mobile_token_in_query_works(people):
    """?token= 直链（手机扫码/分享场景）：303 换发 cookie 并去掉 URL 里的 token，
    之后带着 cookie 能看到机器分组内容——后续点击不再依赖 URL。"""
    p = people["carol"]
    proj, sid = uid("mobq"), uid("sess")
    machine = uid("hk-server")
    assert _ingest(p["api"], sid, proj, machine, summary="香港服务器上的限流改动").status == 200

    status, headers, _ = _get_no_redirect(f"/m?token={p['token']}")
    assert status == 303, status
    cookie = headers.get("Set-Cookie", "") or headers.get("set-cookie", "")
    assert "ah_token=" in cookie
    assert "token=" not in headers.get("Location", "")   # 重定向目标不带 token

    r = H.Api().get("/m", headers={"Cookie": cookie.split(";")[0]})
    assert r.status == 200
    assert machine in r.text and proj in r.text
    # 换到 cookie 后，点卡片（不带 token）不再掉回登录页
    sid_db = p["api"].get("/v1/sessions").json()[0]["id"]
    card = H.Api().get(f"/m/s/{sid_db}", headers={"Cookie": cookie.split(";")[0]})
    assert card.status == 200 and "个人 token" not in card.text[:300]


def test_mobile_session_timeline(people):
    """点进某条会话，能看到 turn_end 的思维链正文（手机上的核心价值）。

    ?token= 直链进详情页也是 303 换 cookie，再带 cookie 看内容。"""
    p = people["carol"]
    proj, sid = uid("mobt"), uid("sess")
    machine = uid("mob-timeline-mac")
    marker = uid("圣诞逻辑")
    r = _ingest(p["api"], sid, proj, machine, summary=f"{marker}：满100送50")
    sid_db = r.json()["session_id"]

    status, headers, _ = _get_no_redirect(f"/m/s/{sid_db}?token={p['token']}")
    assert status == 303
    cookie = (headers.get("Set-Cookie", "") or headers.get("set-cookie", "")).split(";")[0]
    page = H.Api().get(f"/m/s/{sid_db}", headers={"Cookie": cookie})
    assert page.status == 200
    assert marker in page.text and "turn_end" in page.text
    assert machine in page.text
    # 不存在的会话给友好页面，不是 500
    assert H.Api().get("/m/s/99999999", headers={"Cookie": cookie}).status == 200


def test_mobile_timeline_shows_latest_events(people):
    """长会话（>500 事件）：手机时间线必须展示**最新**的事件，
    而不是最早的 500 条——「同步最新进展」是这个页面的核心诉求。"""
    p = people["carol"]
    proj, sid = uid("mobbig"), uid("sess")
    early, late = uid("早期事件"), uid("最新事件")
    events = [{"kind": "user_prompt", "summary": f"第 {i} 步"} for i in range(600)]
    events[0]["summary"] = f"{early} 应被截掉"
    events[-1]["summary"] = f"{late} 必须可见"
    r = p["api"].post("/v1/events", {
        "session": {"ext_id": sid, "agent": "claude-code", "project": proj,
                    "machine": uid("big-mac"), "cwd": "/tmp"},
        "events": events,
    })
    assert r.status == 200
    sid_db = r.json()["session_id"]

    status, headers, _ = _get_no_redirect(f"/m/s/{sid_db}?token={p['token']}")
    assert status == 303
    cookie = (headers.get("Set-Cookie", "") or headers.get("set-cookie", "")).split(";")[0]
    page = H.Api().get(f"/m/s/{sid_db}", headers={"Cookie": cookie})
    assert page.status == 200
    assert late in page.text                     # 最新事件在
    assert early not in page.text                # 最早事件被截掉（500 条窗口）
    assert "500/600" in page.text                # 截断情况如实展示


def test_mobile_search(people):
    p = people["carol"]
    marker = uid("德国活动")
    assert _ingest(p["api"], uid("sess"), uid("mobs"), uid("mob-search-mac"),
                   summary=f"{marker} 迁移到新配置").status == 200
    # 直链搜索同样先 303 换 cookie（保留 q 参数、丢掉 token）
    status, headers, _ = _get_no_redirect(
        f"/m?q={urllib.parse.quote(marker)}&token={p['token']}")
    assert status == 303
    loc = headers.get("Location", "") or headers.get("location", "")
    assert marker in urllib.parse.unquote(loc) and "token=" not in loc
    cookie = (headers.get("Set-Cookie", "") or headers.get("set-cookie", "")).split(";")[0]
    r = H.Api().get(f"/m?q={urllib.parse.quote(marker)}", headers={"Cookie": cookie})
    assert r.status == 200 and marker in r.text


def test_mobile_bad_token_is_rejected(people):
    r = H.Api().get("/m?token=deadbeef")
    assert r.status == 200 and "个人 token" in r.text
    r = H.Api().post("/m/login", body=b"token=deadbeef",
                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status == 200 and "无效" in r.text
