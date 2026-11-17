"""场景 12：会话执行权租约（谁现在正在跑这条会话）。

覆盖：申请 / 重复申请＝续租 / 409 带持有者信息 / generation 栅栏 /
释放与换代 / 主动交接 / 过期接管 / force 夺回 / 上传时的栅栏告警。

这条链路是「换机器接着干」的正确性基础：任何一处静默失效，
都会变成两个人同时写同一份上下文、事后谁也说不清谁覆盖了谁。
"""
import gzip
import urllib.parse
import uuid

import helpers as H
from conftest import uid

CWD = "/srv/lease-repo"


def up(api, sid, raw, agent="claude-code", cwd=CWD, machine="", lease_generation=None):
    params = {"agent": agent, "ext_id": sid, "cwd": cwd, "machine": machine}
    if lease_generation is not None:
        params["lease_generation"] = lease_generation
    qs = urllib.parse.urlencode(params)
    return api.upload("/v1/transcripts?" + qs, raw, token=api.token,
                      ctype="application/x-ndjson")


def fresh(people, person="alice", turns=3):
    """新建一份原件，返回 (api, sid, key, tid)。"""
    p = people[person]
    sid = uid("cc")
    r = up(p["api"], sid, H.cc_simple(CWD, sid, turns))
    assert r.status == 200, r
    return p["api"], sid, f"claude-code:{sid}", r.json()["transcript_id"]


def acquire(api, **kw):
    return api.post("/v1/leases", kw)


def test_acquire_derives_key_and_generation(people):
    """只给 transcript_id，服务端自己推出 session_key，首租 generation=1。"""
    api, sid, key, tid = fresh(people)
    r = acquire(api, transcript_id=tid, holder_id="mac-a:111", machine="Alice 的 MacBook")
    assert r.status == 200, r
    d = r.json()
    assert d["session_key"] == key
    assert d["generation"] == 1
    assert d["mode"] == "exec"
    assert d["holder_id"] == "mac-a:111"
    assert d["ttl_left_seconds"] > 0


def test_second_holder_gets_409_with_owner_info(people):
    """被别人持有时拿到的是「谁在跑、还剩多久、generation」，不是干巴巴的 Conflict。"""
    api, sid, key, tid = fresh(people)
    assert acquire(api, transcript_id=tid, holder_id="mac-a:111").status == 200
    r = acquire(api, transcript_id=tid, holder_id="mac-b:222", machine="build-01")
    assert r.status == 409, r
    detail = r.json()["detail"]
    assert detail["error"] == "session is leased"
    assert detail["holder_id"] == "mac-a:111"
    assert detail["generation"] == 1
    assert detail["ttl_left_seconds"] > 0


def test_same_holder_acquire_is_renewal(people):
    """同一 holder 再申请＝续租，不能把 generation 抬高（否则自己的写入会被自己 fence）。"""
    api, sid, key, tid = fresh(people)
    a = acquire(api, transcript_id=tid, holder_id="mac-a:111").json()
    b = acquire(api, transcript_id=tid, holder_id="mac-a:111").json()
    assert b["generation"] == a["generation"] == 1
    assert b["acquired_at"] == a["acquired_at"]


def test_renew_requires_matching_generation(people):
    api, sid, key, tid = fresh(people)
    gen = acquire(api, transcript_id=tid, holder_id="mac-a:111").json()["generation"]
    ok = api.post(f"/v1/leases/{urllib.parse.quote(key, safe='')}/renew",
                  {"holder_id": "mac-a:111", "generation": gen, "ttl_seconds": 120})
    assert ok.status == 200, ok
    assert ok.json()["ttl_left_seconds"] <= 120

    bad = api.post(f"/v1/leases/{urllib.parse.quote(key, safe='')}/renew",
                   {"holder_id": "mac-a:111", "generation": gen + 9})
    assert bad.status == 409, bad
    assert bad.json()["detail"]["error"] == "lease lost"


def test_release_then_other_machine_takes_over(people):
    """释放后别人能接，但 generation 前进——旧进程的写入从此不合规。"""
    api, sid, key, tid = fresh(people)
    p = urllib.parse.quote(key, safe="")
    g1 = acquire(api, transcript_id=tid, holder_id="mac-a:111").json()["generation"]

    wrong = api.delete(f"/v1/leases/{p}?holder_id=mac-a:111&generation={g1 + 3}")
    assert wrong.status == 409, "带错 generation 不应能删掉别人的租约"

    assert api.delete(f"/v1/leases/{p}?holder_id=mac-a:111&generation={g1}").status == 200

    r = acquire(api, transcript_id=tid, holder_id="mac-b:222", machine="build-01")
    assert r.status == 200, r
    assert r.json()["generation"] == g1 + 1


def test_transfer_is_atomic_and_bumps_generation(people):
    api, sid, key, tid = fresh(people)
    p = urllib.parse.quote(key, safe="")
    g1 = acquire(api, transcript_id=tid, holder_id="mac-a:111").json()["generation"]

    r = api.post(f"/v1/leases/{p}/transfer",
                 {"to_holder_id": "build-01:999", "to_machine": "build-01",
                  "note": "接力到云上跑"})
    assert r.status == 200, r
    d = r.json()
    assert d["holder_id"] == "build-01:999"
    assert d["generation"] == g1 + 1
    assert d["transferred_from"] == "mac-a:111"

    # 旧 holder 续租必须失败：它已经不再拥有执行权
    old = api.post(f"/v1/leases/{p}/renew",
                   {"holder_id": "mac-a:111", "generation": g1})
    assert old.status == 409, old


def test_force_steal_by_owner(people):
    """归属人可以强夺（恶意占用锁不死归属人），generation 也必须前进。"""
    api, sid, key, tid = fresh(people)
    assert acquire(api, transcript_id=tid, holder_id="intruder:1").status == 200
    r = api.post("/v1/leases", {"transcript_id": tid, "holder_id": "owner:7", "force": True})
    assert r.status == 200, r
    d = r.json()
    assert d["stolen"] is True
    assert d["generation"] == 2


def test_non_owner_cannot_force(people):
    """不是归属人、不是管理员，带 force 也抢不到别人的租约。"""
    api_a, sid, key, tid = fresh(people, "alice")
    assert acquire(api_a, transcript_id=tid, holder_id="a:1").status == 200
    api_b = people["bob"]["api"]
    r = api_b.post("/v1/leases", {"transcript_id": tid, "holder_id": "b:2", "force": True})
    assert r.status == 409, r


def test_expired_lease_can_be_taken(people):
    """过期＝没人在跑，别人可以直接接过来（并换 generation）。"""
    api, sid, key, tid = fresh(people)
    assert acquire(api, transcript_id=tid, holder_id="mac-a:111", ttl_seconds=60).status == 200
    H.sql("update session_leases set expires_at = now() - interval '1 minute' "
          f"where session_key = '{key}'")
    r = acquire(api, transcript_id=tid, holder_id="mac-b:222")
    assert r.status == 200, r
    assert r.json()["generation"] == 2


def test_list_and_get(people):
    api, sid, key, tid = fresh(people)
    assert acquire(api, transcript_id=tid, holder_id="mac-a:111", note="接手限流重构").status == 200
    rows = api.get("/v1/leases").json()["leases"]
    mine = [x for x in rows if x["session_key"] == key]
    assert mine and mine[0]["note"] == "接手限流重构"

    one = api.get("/v1/leases/" + urllib.parse.quote(key, safe=""))
    assert one.status == 200 and one.json()["active"] is True


def test_upload_gets_fence_warning(people):
    """warn 模式：别人正在跑时上传不拒，但响应头告诉调用方「你可能已经不该写了」。"""
    api_a, sid, key, tid = fresh(people, "alice")
    assert acquire(api_a, transcript_id=tid, holder_id="mac-a:111").status == 200

    api_b = people["bob"]["api"]
    raw = H.cc_simple(CWD, sid, 4)
    r = up(api_b, sid, raw)
    assert r.status == 200, r
    warn = r.headers.get("x-lease-warning")
    assert warn, "warn 模式下应带 X-Lease-Warning"
    assert "mac-a:111" in urllib.parse.unquote(warn)

    # 带上正确 generation 时不再告警
    gen = api_a.get("/v1/leases/" + urllib.parse.quote(key, safe="")).json()["generation"]
    r2 = up(api_a, sid, H.cc_simple(CWD, sid, 5), lease_generation=gen)
    assert not r2.headers.get("x-lease-warning")
    assert r2.status == 200


def test_lease_requires_valid_token(people):
    api, sid, key, tid = fresh(people)
    r = H.Api().post("/v1/leases", {"transcript_id": tid, "holder_id": "x:1"})
    assert r.status == 401, r


def test_unknown_transcript_404(people):
    api = people["alice"]["api"]
    r = acquire(api, transcript_id=99999999, holder_id="x:1")
    assert r.status == 404, r
