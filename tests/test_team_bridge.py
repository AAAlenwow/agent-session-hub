"""team_bridge 的抓取与水位逻辑（不联网，纯 sqlite 夹具）。

桥接器的风险全在"重复推 / 漏推 / 把 sidecar 库写坏"，所以这里只测这三件事。
"""

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[1] / "hooks"


def load_module(monkeypatch, tmp_path, db_path, token="t0ken"):
    """每次重新加载，避免模块级常量被上一次的夹具污染。"""
    monkeypatch.setenv("AGENT_HUB_TEAM_DB", str(db_path))
    monkeypatch.setenv("AGENT_HUB_TEAM_WATERMARK", str(tmp_path / "wm.json"))
    monkeypatch.setenv("AGENT_HUB_TOKEN", token)
    monkeypatch.setenv("AGENT_HUB_MACHINE", "unit-test-mac")
    spec = importlib.util.spec_from_file_location("team_bridge_uut", HOOKS / "team_bridge.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["team_bridge_uut"] = mod
    spec.loader.exec_module(mod)
    return mod


DDL = """
create table team (id text primary key, name text);
create table member (id text primary key, team_id text, name text, role text);
create table event (id integer primary key autoincrement, team_id text not null,
  member_id text not null, task_id text, seq integer not null, ts text not null,
  phase text not null, action text not null, target text, result text, next text,
  meta_json text not null default '{}', unique(member_id, seq));
create table member_change (id text primary key, team_id text not null, task_id text,
  member_id text, path text not null, kind text not null default 'edit',
  diff text not null default '', added integer, deleted integer, created_at text not null);
create table approval (id text primary key, team_id text not null, member_id text,
  session_id text, tool_name text not null default '', reason text,
  status text not null default 'pending', decided_by text, decided_at text,
  created_at text not null, updated_at text not null);
create table audit (id integer primary key autoincrement, ts text not null, team_id text not null,
  actor text not null, domain text not null, action text not null, target text not null,
  before_hash text, after_hash text, result text not null default 'OK', detail_json text);
"""


@pytest.fixture()
def teamdb(tmp_path):
    path = tmp_path / "team.db"
    con = sqlite3.connect(path)
    con.executescript(DDL)
    con.execute("insert into team values ('t1', 'geeadmin-圣诞活动')")
    con.execute("insert into member values ('m_dev', 't1', 'dev', '实现成员')")
    con.execute("insert into member values ('m_rev', 't1', 'reviewer', '审查成员')")
    con.executemany(
        "insert into event (team_id, member_id, task_id, seq, ts, phase, action, target, result) "
        "values (?,?,?,?,?,?,?,?,?)",
        [
            ("t1", "m_dev", "task1", 1, "2026-11-16T06:00:00.000Z", "init", "agent.spawn",
             "dev", "OK"),
            ("t1", "m_dev", "task1", 2, "2026-11-16T06:05:00.000Z", "done", "task.done",
             "圣诞活动满100送50", "OK"),
        ])
    con.execute(
        "insert into member_change values ('c1','t1','task1','m_dev','gift.py','edit','',18,0,"
        "'2026-11-16T06:04:00.000Z')")
    con.execute(
        "insert into approval values ('a1','t1','m_dev','sess','bash','rm -rf 越界','approved',"
        "'lead','2026-11-16T06:06:00.000Z','2026-11-16T06:03:00.000Z',"
        "'2026-11-16T06:06:00.000Z')")
    con.execute(
        "insert into audit (ts, team_id, actor, domain, action, target, before_hash, after_hash,"
        " result) values ('2026-11-16T06:06:30.000Z','t1','dev','file','write','gift.py',"
        "'aaaa1111','bbbb2222','OK')")
    con.commit()
    con.close()
    return path


def test_first_run_collects_all_four_sources(teamdb, tmp_path, monkeypatch):
    mod = load_module(monkeypatch, tmp_path, teamdb)
    db = mod.connect(teamdb)
    teams = mod.fetch_teams(db)
    assert teams == [("t1", "geeadmin-圣诞活动")]
    body, cursor = mod.build_batch(db, teams[0], {})
    db.close()

    kinds = {e["kind"] for e in body["events"]}
    assert kinds == {"team_event", "team_change", "team_approval", "team_audit"}

    joined = " | ".join(e["summary"] for e in body["events"])
    assert "[dev]" in joined and "task.done" in joined
    assert "gift.py" in joined and "+18/-0" in joined
    assert "rm -rf 越界" in joined and "approved" in joined
    assert "aaaa1111→bbbb2222" in joined


def test_session_identity_is_stable(teamdb, tmp_path, monkeypatch):
    """ext_id 必须由 team_id 派生：换机重跑要落到同一条会话上。"""
    mod = load_module(monkeypatch, tmp_path, teamdb)
    db = mod.connect(teamdb)
    body, _ = mod.build_batch(db, ("t1", "geeadmin-圣诞活动"), {})
    db.close()
    s = body["session"]
    assert s["ext_id"] == "team:t1"
    assert s["agent"] == "dsh-team"
    assert s["machine"] == "unit-test-mac"
    assert s["project"] == "geeadmin-圣诞活动"


def test_watermark_prevents_duplicate_push(teamdb, tmp_path, monkeypatch):
    mod = load_module(monkeypatch, tmp_path, teamdb)
    db = mod.connect(teamdb)
    _, cursor = mod.build_batch(db, ("t1", "geeadmin-圣诞活动"), {})
    body2, cursor2 = mod.build_batch(db, ("t1", "geeadmin-圣诞活动"), {"t1": cursor})
    db.close()
    assert body2["events"] == [], "第二次必须没有新事件"
    assert cursor2["event_id"] == cursor["event_id"]
    assert set(cursor2["approval_ids"]) == {"a1"}


def test_only_new_rows_are_pushed(teamdb, tmp_path, monkeypatch):
    mod = load_module(monkeypatch, tmp_path, teamdb)
    db = mod.connect(teamdb)
    _, cursor = mod.build_batch(db, ("t1", "geeadmin-圣诞活动"), {})
    db.close()

    writer = sqlite3.connect(teamdb)          # sidecar 侧写入，桥接器只读
    writer.execute("insert into event (team_id, member_id, task_id, seq, ts, phase, action,"
                   " target, result) values ('t1','m_rev','task1',3,"
                   "'2026-11-16T07:00:00.000Z','done','task.done','独立审查通过','OK')")
    writer.commit()
    writer.close()

    db = mod.connect(teamdb)
    body, _ = mod.build_batch(db, ("t1", "geeadmin-圣诞活动"), {"t1": cursor})
    db.close()
    assert len(body["events"]) == 1
    assert "独立审查通过" in body["events"][0]["summary"]


def test_missing_db_is_silent(tmp_path, monkeypatch):
    mod = load_module(monkeypatch, tmp_path, tmp_path / "nope.db")
    assert mod.run_once(dry=False, limit=10) == 0


def test_no_token_does_not_touch_network(teamdb, tmp_path, monkeypatch):
    mod = load_module(monkeypatch, tmp_path, teamdb, token="")

    def boom(*a, **k):
        raise AssertionError("无 token 时不应该发请求")

    monkeypatch.setattr(mod, "post", boom)
    assert mod.run_once(dry=False, limit=10) == 0


def test_db_opened_readonly(teamdb, tmp_path, monkeypatch):
    """写 sidecar 的库会抢锁，只读是硬约束。"""
    mod = load_module(monkeypatch, tmp_path, teamdb)
    db = mod.connect(teamdb)
    with pytest.raises(sqlite3.OperationalError):
        db.execute("insert into team values ('t2','x')")
    db.close()
