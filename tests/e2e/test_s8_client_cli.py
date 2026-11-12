"""场景 8：客户端 CLI 全命令（list / usage / pull / wb-sync / wb-pull / wb-hist / wb-diff / wb-revert）。

成员真正敲的是这些命令，接口对了但 CLI 用不了等于没做。
"""
import os
import pathlib
import subprocess
import sys
import urllib.parse

import pytest

import helpers as H
from conftest import uid

CLI = H.ROOT / "client" / "agenthub.py"
CWD = "/Users/old/WorkBuddy/cli-proj"


def cli(token, *args, wb_home=None, state=None, timeout=120):
    env = {
        **os.environ,
        "AGENT_HUB_URL": H.BASE_URL,
        "AGENT_HUB_TOKEN": token,
        "AGENT_HUB_WB_DIR": str(wb_home or "/tmp/ahe2e-cli-wb"),
        "AGENT_HUB_STATE": str(state or "/tmp/ahe2e-cli-state.json"),
        "AGENT_HUB_CLAUDE_DIR": str((wb_home.parent if wb_home else pathlib.Path("/tmp")) / "claude"),
        "AGENT_HUB_CODEX_DIR": str((wb_home.parent if wb_home else pathlib.Path("/tmp")) / "codex"),
    }
    return subprocess.run([sys.executable, str(CLI), *args], capture_output=True,
                          text=True, env=env, timeout=timeout)


@pytest.fixture()
def cc_tid(people, sandbox):
    p = people["alice"]
    sid = uid("cli")
    raw = H.cc_simple(CWD, sid, 2)
    qs = urllib.parse.urlencode({"agent": "claude-code", "ext_id": sid, "cwd": CWD})
    r = p["api"].upload("/v1/transcripts?" + qs, raw)
    assert r.status == 200
    return p, sid, raw, r.json()["transcript_id"]


def test_list_shows_uploaded_transcript(cc_tid, sandbox):
    p, sid, raw, tid = cc_tid
    r = cli(p["token"], "list", wb_home=sandbox / "wb")
    assert r.returncode == 0, r.stderr
    assert sid[:24] in r.stdout and p["name"][:10] in r.stdout


def test_usage_shows_tokens(cc_tid, sandbox):
    p, sid, raw, tid = cc_tid
    r = cli(p["token"], "usage", "--days", "1", wb_home=sandbox / "wb")
    assert r.returncode == 0 and p["name"][:10] in r.stdout


def test_pull_installs_claude_session_with_rewritten_cwd(cc_tid, sandbox):
    p, sid, raw, tid = cc_tid
    repo = sandbox / "repo"
    repo.mkdir()
    r = cli(p["token"], "pull", str(tid), "--into", str(repo), "--no-db",
            wb_home=sandbox / "wb")
    assert r.returncode == 0, r.stderr + r.stdout
    files = list((sandbox / "claude").rglob(f"{sid}.jsonl"))
    assert files, "应落到 claude 项目目录"
    text = files[0].read_text()
    assert str(repo) in text and CWD not in text
    assert "claude --resume" in r.stdout


def test_pull_twice_backs_up_previous(cc_tid, sandbox):
    p, sid, raw, tid = cc_tid
    repo = sandbox / "repo2"
    repo.mkdir()
    args = ("pull", str(tid), "--into", str(repo), "--no-db")
    assert cli(p["token"], *args, wb_home=sandbox / "wb").returncode == 0
    r = cli(p["token"], *args, wb_home=sandbox / "wb")
    assert "已备份" in r.stdout


def test_pull_missing_target_dir_fails(cc_tid, sandbox):
    p, sid, raw, tid = cc_tid
    r = cli(p["token"], "pull", str(tid), "--into", str(sandbox / "nope"), "--no-db",
            wb_home=sandbox / "wb")
    assert r.returncode != 0 and "不存在" in (r.stdout + r.stderr)


def test_pull_unknown_id_fails(people, sandbox):
    r = cli(people["alice"]["token"], "pull", "99999999", "--into", "/tmp", "--no-db",
            wb_home=sandbox / "wb")
    assert r.returncode != 0


def test_wb_sync_uploads_only_when_confirmed(people, sandbox):
    """非交互且没 --yes：必须拒绝上传（用户要求"同步前确认"）。"""
    p = people["alice"]
    sid = uid("wbsync")
    home = H.make_wb_home(sandbox / "wb1", sid, CWD)
    r = cli(p["token"], "wb-sync", "--all", wb_home=home, state=sandbox / "st1.json")
    assert "已取消" in r.stdout
    assert H.sql(f"select count(*) from transcripts where ext_id = '{sid}'") == "0"

    r = cli(p["token"], "wb-sync", "--all", "--yes", wb_home=home, state=sandbox / "st1.json")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "→ r1" in r.stdout
    assert H.sql(f"select bundle from transcripts where ext_id = '{sid}'") in ("t", "true")

    # 没改动再同步：不应产生新版本
    r = cli(p["token"], "wb-sync", "--all", "--yes", wb_home=home, state=sandbox / "st1.json")
    assert "没有需要同步的变更" in r.stdout
    assert H.sql(f"select rev from transcripts where ext_id = '{sid}'") == "1"


def test_wb_hist_diff_revert_chain(people, sandbox):
    p = people["alice"]
    sid = uid("wbchain")
    home = H.make_wb_home(sandbox / "wb2", sid, CWD)
    state = sandbox / "st2.json"
    assert cli(p["token"], "wb-sync", "--all", "--yes", "--message", "第一次",
               wb_home=home, state=state).returncode == 0

    # 追加一轮，模拟用户又聊了两句
    jsonl = next((home / "projects").rglob(f"{sid}.jsonl"))
    with open(jsonl, "ab") as fh:
        fh.write(H.wd_transcript(CWD, sid, 1).splitlines(keepends=True)[1])
    r = cli(p["token"], "wb-sync", "--all", "--yes", "--message", "第二次",
            wb_home=home, state=state)
    assert "→ r2" in r.stdout, r.stdout

    tid = H.sql(f"select id from transcripts where ext_id = '{sid}'")
    hist = cli(p["token"], "wb-hist", tid, wb_home=home, state=state)
    assert hist.returncode == 0 and "r2" in hist.stdout and "r1" in hist.stdout
    assert "第二次" in hist.stdout

    diff = cli(p["token"], "wb-diff", tid, "--from", "1", "--to", "2", "--quiet",
               wb_home=home, state=state)
    assert diff.returncode == 0 and "r1 → r2" in diff.stdout

    rev = cli(p["token"], "wb-revert", tid, "1", "--yes", "--message", "回退",
              wb_home=home, state=state)
    assert rev.returncode == 0 and "新版本 r3" in rev.stdout


def test_wb_pull_restores_bundle_locally(people, sandbox):
    p = people["alice"]
    sid = uid("wbpull")
    qs = urllib.parse.urlencode({"agent": "workbuddy", "ext_id": sid, "cwd": CWD})
    tid = p["api"].upload("/v1/transcripts?" + qs, H.wd_bundle(CWD, sid, 2),
                          ctype="application/gzip").json()["transcript_id"]

    ws = sandbox / "new-workspace"
    ws.mkdir()
    home = sandbox / "wb3"
    r = cli(p["token"], "wb-pull", str(tid), "--into", str(ws), "--no-db",
            wb_home=home, state=sandbox / "st3.json")
    assert r.returncode == 0, r.stdout + r.stderr
    slug = str(ws).strip("/").replace("/", "-")
    got = home / "projects" / slug / f"{sid}.jsonl"
    assert got.is_file(), r.stdout
    assert str(ws) in got.read_text()
    assert (home / "projects" / slug / sid / "tool-results" / "r1.txt").is_file()


def test_wb_pull_revision_specific(people, sandbox):
    p = people["alice"]
    sid = uid("wbver")
    qs = urllib.parse.urlencode({"agent": "workbuddy", "ext_id": sid, "cwd": CWD})
    v1 = H.wd_bundle(CWD, sid, 1)
    tid = p["api"].upload("/v1/transcripts?" + qs, v1, ctype="application/gzip").json()[
        "transcript_id"]
    p["api"].upload("/v1/transcripts?" + qs, H.wd_bundle(CWD, sid, 3),
                    ctype="application/gzip")

    ws, home = sandbox / "ws-rev", sandbox / "wb4"
    ws.mkdir()
    r = cli(p["token"], "wb-pull", str(tid), "--into", str(ws), "--no-db",
            "--revision", "1", wb_home=home, state=sandbox / "st4.json")
    assert r.returncode == 0
    assert "版本：r1" in r.stdout
    body = next((home / "projects").rglob(f"{sid}.jsonl")).read_text()
    assert body.count('"type": "message"') == 2, "应还原 r1（1 轮对话）"


def test_cli_requires_token(people, sandbox):
    r = cli("", "list", wb_home=sandbox / "wb5")
    assert r.returncode != 0
