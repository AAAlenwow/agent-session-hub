"""场景 5：原件层（上传 / 幂等 / 版本 / 下载重写 / 占用锁 / 回滚 / 用量）。

原件层是"换台机器接着聊"的地基，任何一处静默失效都会让 resume 白做。
"""
import gzip
import json
import urllib.parse
import uuid

import pytest

import helpers as H
from conftest import uid

CWD = "/srv/old-repo"


def up(api, sid, raw, agent="claude-code", cwd=CWD, message="", gz=False, token=None,
       project="", machine=""):
    qs = urllib.parse.urlencode({"agent": agent, "ext_id": sid, "cwd": cwd,
                                 "message": message, "project": project,
                                 "machine": machine})
    payload = gzip.compress(raw) if gz else raw
    ctype = "application/gzip" if gz else "application/x-ndjson"
    return api.upload("/v1/transcripts?" + qs, payload, token=token or api.token, ctype=ctype)


def fresh(people, turns=3, person="alice"):
    """新建一份会话原件，返回 (api, sid, raw, tid)。"""
    p = people[person]
    sid = uid("cc")
    raw = H.cc_simple(CWD, sid, turns)
    r = up(p["api"], sid, raw)
    assert r.status == 200, r
    return p["api"], sid, raw, r.json()["transcript_id"]


def test_upload_parses_stats(people):
    p = people["alice"]
    sid = uid("cc")
    raw = H.cc_simple(CWD, sid, 4)
    d = up(p["api"], sid, raw).json()
    assert d["rev"] == 1 and not d.get("unchanged")
    assert d["lines"] == 8                       # 4 轮问答
    assert d["input_tokens"] == 4000 and d["output_tokens"] == 800
    row = H.sql(f"select agent || '|' || rev || '|' || coalesce(cwd,'') "
                f"from transcripts where id = {d['transcript_id']}")
    assert row == f"claude-code|1|{CWD}"
    assert H.sql(f"select bundle from transcripts where id = {d['transcript_id']}") == "f"


def test_machine_recorded_and_redacted(people):
    """machine 元数据与事件层同一套脱敏口径：密钥串不能借 machine 原样入库。"""
    p = people["alice"]
    sid = uid("cc")
    raw = H.cc_simple(CWD, sid, 2)
    d = up(p["api"], sid, raw, machine="box01-sk-abcdefghijklmnop1234").json()
    stored = H.sql(f"select machine from transcripts where id = {d['transcript_id']}")
    assert stored == "box01-[REDACTED-KEY]", stored
    # 正常机器名不受影响
    d2 = up(p["api"], uid("cc"), raw, machine="hechao 的 mac").json()
    assert H.sql(
        f"select machine from transcripts where id = {d2['transcript_id']}") == "hechao 的 mac"


def test_resubmit_identical_is_noop(people):
    api, sid, raw, tid = fresh(people)
    d = up(api, sid, raw).json()
    assert d["unchanged"] is True and d["rev"] == 1
    assert H.sql(f"select count(*) from transcript_revisions where transcript_id = {tid}") == "1"


def test_gzip_and_plain_are_the_same_content(people):
    api, sid, raw, tid = fresh(people)
    d = up(api, sid, raw, gz=True).json()
    assert d["unchanged"] is True, "压缩与否应视为同一内容（按解压后比对）"


def test_append_creates_new_revision_with_diff(people):
    api, sid, raw, tid = fresh(people, turns=2)
    more = raw + H.cc_transcript(CWD, sid, [
        {"role": "user", "text": "继续：把重试次数改成 5", "ts": H.time.time()},
        {"role": "assistant", "text": "改好了", "file": "/srv/old-repo/src/retry.py",
         "ts": H.time.time() + 10},
    ])
    d = up(api, sid, more, message="加一次重试").json()
    assert d["rev"] == 2 and d["previous_rev"] == 1
    assert d["diff"]["added"] == 2 and d["diff"]["removed"] == 0
    assert d["diff"]["user_messages"] == 1 and d["diff"]["tool_calls"] == 1
    assert "/srv/old-repo/src/retry.py" in d["diff"]["files"]
    assert H.sql(f"select message from transcript_revisions where transcript_id = {tid} "
                 f"and rev = 2") == "加一次重试"


def test_rewrite_does_not_touch_sibling_repo_with_same_prefix(people):
    """线上事故复现：/srv/old-repo 不能把 /srv/old-repo-gateway 改坏。"""
    p = people["alice"]
    sid = uid("cc")
    raw = H.cc_transcript(CWD, sid, [
        {"role": "user", "text": "看下 /srv/old-repo-gateway 那边的配置",
         "ts": H.time.time()},
        {"role": "assistant", "text": "好", "file": "/srv/old-repo/src/a.py",
         "ts": H.time.time() + 5},
    ])
    tid = up(p["api"], sid, raw).json()["transcript_id"]
    body = p["api"].get(f"/v1/transcripts/{tid}?cwd=/home/me/new-repo").text
    assert "/home/me/new-repo/src/a.py" in body
    assert "/srv/old-repo-gateway" in body, "同前缀仓库不能被改写"
    assert "old-repo/src" not in body


def test_download_headers_and_content(people):
    api, sid, raw, tid = fresh(people, turns=2)
    r = api.get(f"/v1/transcripts/{tid}")
    assert r.status == 200
    assert r.body == raw, "不带 cwd 时应原样返回"
    assert r.headers["x-ext-id"] == sid
    assert r.headers["x-agent"] == "claude-code"
    assert r.headers["x-origin-cwd"] == CWD
    assert r.headers["x-rev"] == "1"
    assert r.headers["x-lines"] == "4"
    assert f'filename="{sid}.jsonl"' in r.headers["content-disposition"]


def test_download_historical_revision(people):
    api, sid, raw, tid = fresh(people, turns=1)
    raw2 = raw + H.cc_transcript(CWD, sid, [
        {"role": "user", "text": "第二轮", "ts": H.time.time()}])
    up(api, sid, raw2)
    r = api.get(f"/v1/transcripts/{tid}?revision=1")
    assert r.body == raw and r.headers["x-rev"] == "1"


def test_revisions_list_is_git_log(people):
    api, sid, raw, tid = fresh(people, turns=1)
    up(api, sid, raw + H.cc_transcript(CWD, sid, [
        {"role": "user", "text": "x", "ts": H.time.time()}]), message="第二次")
    d = api.get(f"/v1/transcripts/{tid}/revisions").json()
    assert d["head"] == 2
    assert [r["rev"] for r in d["revisions"]] == [2, 1]
    assert d["revisions"][0]["author"] == people["alice"]["name"]
    assert d["revisions"][0]["message"] == "第二次"
    assert "added_lines" not in d["revisions"][0]["summary"]


def test_diff_between_revisions(people):
    api, sid, raw, tid = fresh(people, turns=1)
    up(api, sid, raw + H.cc_transcript(CWD, sid, [
        {"role": "user", "text": "差异", "ts": H.time.time()}]))
    d = api.get(f"/v1/transcripts/{tid}/diff?from_rev=1&to_rev=2").json()
    assert d["added"] == 1 and d["removed"] == 0
    assert d["summary"]["user_messages"] == 1
    assert "差异" in json.dumps(d["added_lines"], ensure_ascii=False)
    same = api.get(f"/v1/transcripts/{tid}/diff?from_rev=2&to_rev=2").json()
    assert same["added"] == 0 and same["removed"] == 0


def test_revert_moves_head_but_keeps_history(people):
    api, sid, raw, tid = fresh(people, turns=1)
    raw2 = raw + H.cc_transcript(CWD, sid, [{"role": "user", "text": "追加", "ts": H.time.time()}])
    up(api, sid, raw2)
    r = api.post(f"/v1/transcripts/{tid}/revert?" +
                 urllib.parse.urlencode({"revision": 1, "message": "回退坏改动"}))
    assert r.status == 200 and r.json()["rev"] == 3
    assert api.get(f"/v1/transcripts/{tid}").body == raw
    revs = api.get(f"/v1/transcripts/{tid}/revisions").json()
    assert [x["rev"] for x in revs["revisions"]] == [3, 2, 1]
    assert revs["revisions"][0]["summary"]["reverted_from"] == 1
    # 再回滚回 r2 也应该可以（历史没被删）
    assert api.post(f"/v1/transcripts/{tid}/revert?revision=2").json()["rev"] == 4
    assert api.get(f"/v1/transcripts/{tid}").body == raw2


def test_revert_requires_ownership(people):
    api, sid, raw, tid = fresh(people, person="alice")
    r = people["bob"]["api"].post(f"/v1/transcripts/{tid}/revert?revision=1")
    assert r.status == 403
    assert people["alice"]["api"].post(f"/v1/transcripts/{tid}/revert?revision=99").status == 404


def test_claim_ownership_and_release(people, admin):
    """锁挡得住成员、挡不住归属人：
    普通成员可占用（交接场景），但归属人随时可夺回、可解铃——恶意占用锁不死任何人。"""
    api, sid, raw, tid = fresh(people, person="alice")
    bob = people["bob"]["api"]
    # 交接场景：bob（新负责人）接手 alice 的会话
    assert bob.post(f"/v1/transcripts/{tid}/claim").status == 200
    assert bob.get(f"/v1/transcripts/{tid}").status == 200
    assert api.get(f"/v1/transcripts/{tid}").status == 409, "bob 占用期间 alice 不能拉取"
    # 历史版本下载也受同一把锁约束
    assert api.get(f"/v1/transcripts/{tid}?revision=1").status == 409
    # 归属人可直接夺回（锁挡不住归属人）
    assert api.post(f"/v1/transcripts/{tid}/claim").status == 200
    assert bob.get(f"/v1/transcripts/{tid}").status == 409
    assert api.get(f"/v1/transcripts/{tid}").status == 200
    # bob 不能再从 alice 手里抢回（非归属人不能抢占现有持有者）
    assert bob.post(f"/v1/transcripts/{tid}/claim").status == 409
    # 归属人可解铃；解开后 bob 的拉取恢复
    assert api.delete(f"/v1/transcripts/{tid}/claim").status == 200
    assert bob.get(f"/v1/transcripts/{tid}").status == 200
    assert bob.delete(f"/v1/transcripts/{tid}/claim").status == 403, "成员不能解别人的铃"
    # 管理员同样可夺回（此前没有任何解铃手段是缺陷）
    assert admin.post(f"/v1/transcripts/{tid}/claim").status == 200
    assert api.delete(f"/v1/transcripts/{tid}/claim").status == 200
    # 再解一次（无锁状态）也安全
    assert api.delete(f"/v1/transcripts/{tid}/claim").status == 200


def test_claim_same_ext_id_does_not_block_other_users(people):
    """锁键必须是会话而不是全局 (agent, ext_id)：
    bob 与 alice 各自有一份同名 ext_id 的原件，bob 占用自己的不能挡住 alice 的。"""
    alice, bob = people["alice"]["api"], people["bob"]["api"]
    sid = uid("clash")
    ta = up(alice, sid, H.cc_simple(CWD, sid, 2)).json()["transcript_id"]
    tb = up(bob, sid, H.cc_simple("/srv/bob-repo", sid, 2), cwd="/srv/bob-repo").json()[
        "transcript_id"]
    assert bob.post(f"/v1/transcripts/{tb}/claim").status == 200
    assert alice.get(f"/v1/transcripts/{ta}").status == 200, "同名 ext_id 不能互相挡锁"
    assert alice.post(f"/v1/transcripts/{ta}/claim").status == 200


def test_claim_expires(people):
    api, sid, raw, tid = fresh(people, person="alice")
    bob = people["bob"]["api"]
    assert api.post(f"/v1/transcripts/{tid}/claim").status == 200
    H.sql(f"update resume_claims set expires_at = now() - interval '1 minute' "
          f"where transcript_id = {tid}")
    assert bob.get(f"/v1/transcripts/{tid}").status == 200, "锁过期后应能接管"
    assert api.post(f"/v1/transcripts/{tid}/claim").status == 200, "过期后归属人可重新占用"


def test_claim_unknown_transcript(people):
    assert people["alice"]["api"].post("/v1/transcripts/99999999/claim").status == 404


def test_validation_errors(people):
    api = people["alice"]["api"]
    assert up(api, "", b"{}").status == 400
    assert up(api, "bad id/../x", b"{}").status == 400
    assert up(api, "a" * 65, b"{}").status == 400
    assert up(api, "ok.look_id-1", b"{}\n").status == 200


def test_upload_size_limits(people):
    api = people["alice"]["api"]
    big = b'{"x":"' + b"A" * (3 * 1024 * 1024) + b'"}\n'
    assert up(api, uid("big"), big).status == 413


def test_gzip_bomb_is_rejected(people):
    """100MB 的零字节压成 100KB：按解压后体积判定，不能解完再看。"""
    api = people["alice"]["api"]
    bomb = gzip.compress(b"\0" * (100 * 1024 * 1024))
    assert len(bomb) < 200 * 1024
    assert up(api, uid("bomb"), bomb).status == 413


def test_nested_gzip_bomb_is_rejected(people):
    """外面再套一层 gzip：解一层后仍是要入库的打包体，守卫必须按最终内容判。"""
    api = people["alice"]["api"]
    bomb = gzip.compress(gzip.compress(b"\0" * (100 * 1024 * 1024)))
    assert up(api, uid("bomb2"), bomb).status == 413


def test_bundle_member_limit(people):
    """打包体成员数上限（5000）：只读 tar 头就拦，不解压、不展开。"""
    api = people["alice"]["api"]
    members = [(f"{uid('s')}/tool-results/{i}.txt", b"x") for i in range(5001)]
    bundle = H.make_tar(members, {"ext_id": uid("many"), "cwd": CWD})
    assert up(api, uid("many"), bundle, agent="workbuddy").status == 413


def test_download_unknown_and_bad_params(people):
    api = people["alice"]["api"]
    assert api.get("/v1/transcripts/99999999").status == 404
    assert api.get("/v1/transcripts/99999999/revisions").json()["revisions"] == []
    assert api.get("/v1/transcripts/99999999/diff").status == 404
    assert api.get("/v1/transcripts/99999999").status == 404


def test_history_trimmed_to_keep_revisions(people):
    """keep_revisions=3（测试配置）：只留最近 3 版 + 最早基线。"""
    p = people["alice"]
    sid = uid("trim")
    raw = H.cc_simple(CWD, sid, 1)
    tid = up(p["api"], sid, raw).json()["transcript_id"]
    for i in range(5):
        raw = raw + H.cc_transcript(CWD, sid, [{"role": "user", "text": f"追加{i}",
                                               "ts": H.time.time()}])
        up(p["api"], sid, raw)
    revs = p["api"].get(f"/v1/transcripts/{tid}/revisions").json()
    assert revs["head"] == 6
    assert [r["rev"] for r in revs["revisions"]] == [6, 5, 4, 1], "应为 最近3版 + 基线"
    assert p["api"].get(f"/v1/transcripts/{tid}?revision=2").status == 404


def test_list_and_usage(people):
    api, sid, raw, tid = fresh(people, turns=2, person="alice")
    rows = api.get("/v1/transcripts?limit=200").json()
    mine = [r for r in rows if r["id"] == tid]
    assert mine and mine[0]["rev"] == 1 and mine[0]["lines"] == 4
    assert mine[0]["owner"] == people["alice"]["name"]
    assert mine[0]["active_seconds"] >= 0

    usage = api.get("/v1/usage?days=1").json()
    row = [u for u in usage if u["owner"] == people["alice"]["name"]]
    assert row and row[0]["input_tokens"] >= 2000


def test_download_respects_cross_user_switch(people):
    """本部署 allow_cross_user_pull=true：bob 能拉 alice 的会话（交接需要）。"""
    api, sid, raw, tid = fresh(people, person="alice")
    assert people["bob"]["api"].get(f"/v1/transcripts/{tid}").status == 200


def test_transcript_is_linked_to_session_project(people):
    """hooks 先报事件再传原件；此时清单里的 project 应能按项目过滤。"""
    p = people["alice"]
    proj, sid = uid("linkproj"), uid("cc")
    p["api"].post("/v1/events", {
        "session": {"ext_id": sid, "agent": "claude-code", "project": proj, "cwd": CWD},
        "events": [{"kind": "turn_end", "summary": "done"}],
    })
    tid = up(p["api"], sid, H.cc_simple(CWD, sid, 1)).json()["transcript_id"]
    rows = p["api"].get(f"/v1/transcripts?project={urllib.parse.quote(proj)}").json()
    assert [r["id"] for r in rows] == [tid]


def test_workbuddy_transcript_visible_under_project(people):
    """wb-sync 只传原件不报事件（没有 session 行可反查），
    项目归属必须由上传参数落进 transcripts.project，否则按项目视图整条丢失。"""
    p = people["alice"]
    proj, sid = uid("wbproj"), uid("wb")
    # 项目由另一个 agent 的会话建立（wb-sync 自己不会建 sessions 行）
    p["api"].post("/v1/events", {
        "session": {"ext_id": uid("cc"), "agent": "claude-code", "project": proj, "cwd": CWD},
        "events": [{"kind": "turn_end", "summary": "done"}],
    })
    up(p["api"], sid, H.wd_transcript(CWD, sid, 2), agent="workbuddy", project=proj)
    rows = p["api"].get(f"/v1/transcripts?project={urllib.parse.quote(proj)}").json()
    assert sid in [r["ext_id"] for r in rows]
    # 用量统计同样要能看到（事件会话没有原件，这里 1 = WorkBuddy 原件本身）
    usage = p["api"].get(f"/v1/usage?project={urllib.parse.quote(proj)}").json()
    assert any(u["agent"] == "workbuddy" and u["transcripts"] >= 1 for u in usage), usage


def test_upload_project_survives_subsequent_empty_param(people):
    """第二次上传不带 project 时不能把已落的项目归属抹掉。"""
    api, sid, raw, tid = fresh(people)
    proj = uid("keep")
    up(api, sid, H.cc_simple(CWD, sid, 5))
    H.sql(f"update transcripts set project = '{proj}' where id = {tid}")
    up(api, sid, H.cc_simple(CWD, sid, 6))
    assert H.sql(f"select project from transcripts where id = {tid}") == proj


def test_nul_bytes_in_events_do_not_500(people):
    """Postgres 的 text/jsonb 拒绝 \\u0000：一条脏数据不能打挂上报接口。"""
    api, proj, sid = people["alice"]["api"], uid("nul"), uid("ns")
    r = api.post("/v1/events", {
        "session": {"ext_id": sid, "agent": "claude-code", "project": f"{proj}\x00",
                    "cwd": CWD, "title": "ok"},
        "events": [{
            "kind": "turn_end",
            "summary": f"带\x00NUL的摘要 sk-abcdefghijklmnop1234",
            "files": [f"/tmp/sk-abcdefghijklmnop1234\x00.env", "a.py"],
            "payload": {"key": "sk-abcdefghijklmnop1234", "note": "token=abc123456789",
                        "nested": ["\x00", "deep-secret"]},
        }],
    })
    assert r.status == 200, r
    row = H.sql(f"select summary from events e join sessions s on s.id = e.session_id "
                f"where s.ext_id = '{sid}'")
    assert "\x00" not in row and "sk-abcdefghijklmnop1234" not in row  # NUL 剥掉 + 密钥脱敏
    raw = H.sql(f"select files::text || '|' || payload::text from events e "
                f"join sessions s on s.id = e.session_id where s.ext_id = '{sid}'")
    assert "\x00" not in raw and "sk-abcdefghijklmnop1234" not in raw and "abc123456789" not in raw
    proj_row = H.sql(f"select project from sessions where ext_id = '{sid}'")
    assert "\x00" not in proj_row


def test_concurrent_revert_and_upload_do_not_500(people):
    """revert 与 upload 混合并发：rev 分配串行化后不允许 500/静默丢版。
    上传内容各不相同 → 6 个操作必须恰好产生 6 个新版本（revert 总是产生新 rev）。"""
    import concurrent.futures as cf
    api, sid, raw, tid = fresh(people)
    for i in range(2):
        up(api, sid, H.cc_simple(CWD, sid, i + 4))
    head0 = int(H.sql(f"select rev from transcripts where id = {tid}"))
    base = H.cc_simple(CWD, sid, 1)
    counter = {"n": 0}

    def run(op):
        kind, rev = op
        if kind == "revert":
            return api.post(f"/v1/transcripts/{tid}/revert?revision={rev}&message=x").status
        counter["n"] += 1
        n = counter["n"]
        content = base + H.cc_transcript(CWD, sid, [
            {"role": "user", "text": f"并发上传第 {n} 次", "ts": H.time.time()}])
        return up(api, sid, content).status

    ops = ([("revert", 1)] * 3) + ([("upload", None)] * 3)
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        codes = list(ex.map(run, ops))

    assert codes == [200] * 6, codes  # 500 = 回归（此前并发 revert 撞 unique 直接 500）
    final_head = int(H.sql(f"select rev from transcripts where id = {tid}"))
    assert final_head == head0 + 6, \
        f"HEAD {final_head} 应为 {head0 + 6}（每个操作恰好一个版本，不允许静默丢版）"
    revs = H.sql(f"select string_agg(distinct rev::text, ',') from transcript_revisions "
                 f"where transcript_id = {tid}")
    assert "1" in revs.split(","), "r1 基线必须保留"
