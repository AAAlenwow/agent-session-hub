"""场景 6：WorkBuddy 打包体（jsonl + 旁挂 tool-results + 索引行）的往返。

这是"换台电脑接着用 WorkBuddy"的完整链路：打包 → 上传 → 下载 → 在本机还原。
顺带验证上一轮修掉的打包体越界漏洞在真实往返里确实拦得住。
"""
import json
import pathlib
import urllib.parse

import pytest

import helpers as H
from conftest import uid

CWD = "/Users/old/WorkBuddy/proj-a"


def up_bundle(api, sid, data, cwd=CWD, message=""):
    qs = urllib.parse.urlencode({"agent": "workbuddy", "ext_id": sid,
                                 "cwd": cwd, "message": message})
    return api.upload("/v1/transcripts?" + qs, data, ctype="application/gzip")


def test_upload_bundle_keeps_all_members(people):
    p = people["alice"]
    sid = uid("wb")
    bundle = H.wd_bundle(CWD, sid, n=3)
    d = up_bundle(p["api"], sid, bundle).json()
    assert d["bundle"] is True
    assert d["members"] >= 4, "jsonl + rollback + 2 个旁挂 + meta"
    assert d["lines"] == 7, "1 行 meta + 3 轮 × 2 条消息"
    # 下载回来必须字节一致（旁挂文件一个都不能丢）
    assert p["api"].get(f"/v1/transcripts/{d['transcript_id']}").body == bundle


def test_bundle_download_rewrites_every_member(people):
    p = people["alice"]
    sid = uid("wb")
    tid = up_bundle(p["api"], sid, H.wd_bundle(CWD, sid, n=2)).json()["transcript_id"]
    r = p["api"].get(f"/v1/transcripts/{tid}?cwd=/Users/new/WorkBuddy/proj-b")
    assert r.status == 200 and r.headers["x-bundle"] == "tar.gz"
    members = H.untar(r.body)
    assert f"{sid}.jsonl" in members and f"{sid}/tool-results/r1.txt" in members
    body = members[f"{sid}.jsonl"].decode()
    assert "/Users/new/WorkBuddy/proj-b" in body and CWD not in body
    meta = json.loads(members["_agenthub/meta.json"])
    assert meta["cwd"] == "/Users/new/WorkBuddy/proj-b"
    assert members[f"{sid}/tool-results/r1.txt"] == b"tool output 1\n", "二进制/旁挂内容不能被改写"


def test_bundle_revision_and_diff(people):
    p = people["alice"]
    sid = uid("wb")
    base = int(H.time.time() * 1000) - 3_600_000      # 固定基准时间 → 第二次是纯追加
    first = H.wd_bundle(CWD, sid, n=1, base=base)
    tid = up_bundle(p["api"], sid, first).json()["transcript_id"]
    assert up_bundle(p["api"], sid, first).json()["unchanged"] is True

    second = H.wd_bundle(CWD, sid, n=2, base=base)
    d = up_bundle(p["api"], sid, second, message="多问了一轮").json()
    assert d["rev"] == 2 and d["diff"]["added"] == 2
    assert d["diff"]["user_messages"] == 1
    revs = p["api"].get(f"/v1/transcripts/{tid}/revisions").json()
    assert revs["head"] == 2
    assert [r["rev"] for r in revs["revisions"]] == [2, 1]


def test_install_bundle_roundtrip(people, sandbox, monkeypatch):
    """服务端下载 → 本机解包还原，路径与旁挂都要对。"""
    import wb
    p = people["alice"]
    sid = uid("wb")
    tid = up_bundle(p["api"], sid, H.wd_bundle(CWD, sid, n=2)).json()["transcript_id"]

    into = "/Users/new/WorkBuddy/proj-b"
    body = p["api"].get(f"/v1/transcripts/{tid}?cwd={urllib.parse.quote(into)}").body

    monkeypatch.setattr(wb, "WB_HOME", sandbox)
    res = wb.install_bundle(body, into, rewrite_from=CWD, patch_db=False)
    proj = sandbox / "projects" / wb.slug_for(into)
    assert (proj / f"{sid}.jsonl").is_file()
    assert (proj / f"{sid}.file-rollback.ndjson").is_file()
    assert (proj / sid / "tool-results" / "r1.txt").read_bytes() == b"tool output 1\n"
    assert into in (proj / f"{sid}.jsonl").read_text()
    assert CWD not in (proj / f"{sid}.jsonl").read_text()
    assert res["installed_to"].endswith(wb.slug_for(into))


def test_install_bundle_backs_up_existing_file(sandbox, monkeypatch):
    import wb
    monkeypatch.setattr(wb, "WB_HOME", sandbox)
    sid = "sess-1"
    into = "/Users/new/proj"
    slug = wb.slug_for(into)
    (sandbox / "projects" / slug).mkdir(parents=True)
    (sandbox / "projects" / slug / f"{sid}.jsonl").write_text("旧内容")

    wb.install_bundle(H.wd_bundle("/Users/old/proj", sid, n=1), into,
                      rewrite_from="/Users/old/proj", patch_db=False)
    files = sorted(x.name for x in (sandbox / "projects" / slug).iterdir())
    assert any(n.startswith(f"{sid}.jsonl.bak-") for n in files), "覆盖前必须留备份"


def test_malicious_bundle_is_rejected_end_to_end(people, sandbox, monkeypatch):
    """上传 → 下载 → 还原 全链路：带 ../../ 的成员必须整包拒绝，且不产生越界文件。"""
    import wb
    p = people["alice"]
    sid = uid("evil")
    into = f"/Users/new/proj-{uid('t')}"
    members = [(f"{sid}.jsonl", H.wd_transcript(CWD, sid, 1)),
               ("../../../../../../tmp/PWNED-e2e.txt", b"pwned\n"),
               (f"{sid}/tool-results/ok.txt", b"fine\n")]
    tid = up_bundle(p["api"], sid, H.make_tar(members, {"ext_id": sid, "cwd": CWD})).json()[
        "transcript_id"]
    body = p["api"].get(f"/v1/transcripts/{tid}").body

    monkeypatch.setattr(wb, "WB_HOME", sandbox)
    with pytest.raises(RuntimeError, match="越界"):
        wb.install_bundle(body, into, rewrite_from=CWD, patch_db=False)
    assert not pathlib.Path("/tmp/PWNED-e2e.txt").exists()
    target = sandbox / "projects" / wb.slug_for(into)
    assert not target.exists(), "整包拒绝时不应创建目标目录"


def test_absolute_path_member_is_rejected(sandbox, monkeypatch):
    import wb
    monkeypatch.setattr(wb, "WB_HOME", sandbox)
    sid = "sess-abs"
    bundle = H.make_tar([(f"{sid}.jsonl", b'{"cwd":"/x"}\n'),
                         ("/etc/cron.d/evil", b"* * * * * root id\n")],
                        {"ext_id": sid, "cwd": "/Users/old/proj"})
    with pytest.raises(RuntimeError, match="越界"):
        wb.install_bundle(bundle, "/Users/new/proj", patch_db=False)


def test_bundle_with_oversized_member_is_rejected(people):
    """打包体里塞一个大成员（3MB 零字节，压缩后只有几 KB）：仍须按展开体积拒绝。"""
    api = people["alice"]["api"]
    sid = uid("huge")
    bundle = H.make_tar([
        (f"{sid}.jsonl", b'{"a":1}\n'),
        (f"{sid}/tool-results/big.bin", b"\0" * (3 * 1024 * 1024)),
    ], {"ext_id": sid, "cwd": CWD})
    assert len(bundle) < 1024 * 1024, "压缩后必须足够小，才能绕过请求体上限"
    assert up_bundle(api, sid, bundle).status == 413
