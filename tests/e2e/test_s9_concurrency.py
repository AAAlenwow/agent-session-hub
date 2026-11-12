"""场景 9：并发与幂等。

团队十几个人同时用，happens-before 不成立的时候才是真出问题的时候。
"""
import concurrent.futures as cf
import json
import urllib.parse

import pytest

import helpers as H
from conftest import uid

CWD = "/srv/conc-repo"


def test_parallel_event_ingest_same_session(people):
    api, proj, sid = people["alice"]["api"], uid("conc"), uid("sess")

    def one(i):
        return api.post("/v1/events", {
            "session": {"ext_id": sid, "agent": "claude-code", "project": proj, "cwd": CWD},
            "events": [{"kind": "tool_call", "summary": f"Edit: f{i}.py", "files": [f"f{i}.py"]}],
        }).status

    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        codes = list(ex.map(one, range(24)))
    assert codes == [200] * 24, codes
    assert H.sql(f"select count(*) from events e join sessions s on s.id = e.session_id "
                 f"where s.ext_id = '{sid}'") == "24"
    assert H.sql(f"select count(*) from sessions where ext_id = '{sid}'") == "1"


def test_parallel_claim_only_one_wins(people, admin):
    p = people["alice"]
    sid = uid("claim")
    tid = p["api"].upload(
        "/v1/transcripts?" + urllib.parse.urlencode(
            {"agent": "claude-code", "ext_id": sid, "cwd": CWD}),
        H.cc_simple(CWD, sid, 1)).json()["transcript_id"]

    # 普通成员之间抢锁：只有一人能拿到（归属人/管理员则随时可夺回，不参与此断言）
    contenders = [people["bob"]["api"], people["carol"]["api"]]

    def claim(api):
        return api.post(f"/v1/transcripts/{tid}/claim").status

    with cf.ThreadPoolExecutor(max_workers=2) as ex:
        codes = list(ex.map(claim, contenders))
    assert codes.count(200) == 1, codes
    assert set(codes) <= {200, 409}, codes
    holders = H.sql(f"select count(*) from resume_claims where transcript_id = {tid}")
    assert holders == "1", "锁表里只能有一条记录"
    # 归属人并发夺回也安全（最多一条锁记录）
    with cf.ThreadPoolExecutor(max_workers=2) as ex:
        codes2 = list(ex.map(claim, [p["api"], admin]))
    assert all(c in (200, 409) for c in codes2), codes2
    holders2 = H.sql(f"select count(*) from resume_claims where transcript_id = {tid}")
    assert holders2 == "1", "夺回后锁表里仍然只能有一条记录"


def test_parallel_downloads_by_owner_all_succeed(people):
    p = people["alice"]
    sid = uid("dl")
    raw = H.cc_simple(CWD, sid, 2)
    tid = p["api"].upload(
        "/v1/transcripts?" + urllib.parse.urlencode(
            {"agent": "claude-code", "ext_id": sid, "cwd": CWD}), raw).json()["transcript_id"]

    def get(_i):
        r = p["api"].get(f"/v1/transcripts/{tid}")
        return r.status, r.body == raw

    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(get, range(12)))
    assert all(st == 200 and same for st, same in results)


def test_parallel_identical_upload_is_single_revision(people):
    p = people["alice"]
    sid = uid("same")
    raw = H.cc_simple(CWD, sid, 1)
    qs = urllib.parse.urlencode({"agent": "claude-code", "ext_id": sid, "cwd": CWD})

    def up(_i):
        return p["api"].upload("/v1/transcripts?" + qs, raw).status

    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        codes = list(ex.map(up, range(4)))
    assert codes == [200] * 4, codes
    assert H.sql(f"select count(*) from transcript_revisions r join transcripts t "
                 f"on t.id = r.transcript_id where t.ext_id = '{sid}'") == "1"
    assert H.sql(f"select rev from transcripts where ext_id = '{sid}'") == "1"


def test_parallel_different_uploads_do_not_lose_a_version(people):
    """并发上传不同内容：rev 分配串行化（advisory lock + 事务）后，
    每次提交一个版本号，不允许同号静默覆盖。"""
    p = people["alice"]
    sid = uid("race")
    base = H.cc_simple(CWD, sid, 1)
    qs = urllib.parse.urlencode({"agent": "claude-code", "ext_id": sid, "cwd": CWD})

    def up(i):
        raw = base + H.cc_transcript(CWD, sid, [
            {"role": "user", "text": f"并行分支 {i}", "ts": H.time.time()}])
        return p["api"].upload("/v1/transcripts?" + qs, raw).json()

    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(up, range(4)))

    revs = [r["rev"] for r in results if "rev" in r]
    assert len(revs) == 4, results
    assert len(set(revs)) == 4, \
        f"不同内容的提交拿到了重复版本号 {revs}：后写者静默覆盖了前一版"
    head = int(H.sql(f"select rev from transcripts where ext_id = '{sid}'"))
    assert head == max(revs)
    accepted = int(H.sql(f"select count(*) from transcript_revisions r join transcripts t "
                         f"on t.id = r.transcript_id where t.ext_id = '{sid}'"))
    assert accepted == head, f"提交数 {accepted} 应等于 HEAD 版本号 {head}，否则有版本被静默丢弃"


def test_endpoint_latency_is_sane(people):
    """hooks 在每回合同步调用，单次上报必须足够快（否则拖慢用户敲代码）。"""
    api, proj = people["alice"]["api"], uid("lat")
    timings = []
    for i in range(10):
        t0 = H.time.time()
        r = api.post("/v1/events", {
            "session": {"ext_id": f"{proj}-{i}", "project": proj, "cwd": CWD},
            "events": [{"kind": "turn_end", "summary": "x"}] * 5,
        })
        assert r.status == 200
        timings.append(H.time.time() - t0)
    timings.sort()
    p95 = timings[int(len(timings) * 0.95) - 1]
    assert p95 < 1.0, f"p95 {p95*1000:.0f}ms 太慢（本地栈应在 100ms 量级）"
