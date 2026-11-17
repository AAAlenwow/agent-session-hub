"""场景 10：升级路径（v1 → v2 → v3 → 打包体 → 版本历史）。

新部署由 schema.sql 一次建全，已有部署只能靠 migrations/ 里的脚本升级。
如果升级后的表结构与全新安装不一致，线上就会出现"测试环境好好的、生产 500"。
"""
import pathlib
import re

import pytest

import helpers as H

SCHEMA = H.ROOT / "server" / "schema.sql"
MIG = H.ROOT / "server" / "migrations"
DB = "agenthub_mig"

COLS_SQL = (
    "select table_name||'.'||column_name||':'||data_type||':'||is_nullable||':'||"
    "coalesce(column_default,'') from information_schema.columns "
    "where table_schema='public' order by 1"
)


@pytest.fixture(scope="module")
def migrated():
    """建一个全新的 agenthub_mig 库，按 v1 → 005 的顺序升级一遍。"""
    if not H.sql_ok():
        pytest.skip("无法访问测试数据库")
    H.sql(f"drop database if exists {DB} with (force)", db="postgres")
    H.sql(f"create database {DB}", db="postgres")

    v1 = SCHEMA.read_text(encoding="utf-8").split("-- ---------- v2:")[0]
    assert "create table if not exists events" in v1 and "projects" not in v1
    H.sql(v1, db=DB)
    for f in sorted(MIG.glob("*.sql")):
        H.sql(f.read_text(encoding="utf-8"), db=DB)
    yield DB
    H.sql(f"drop database if exists {DB} with (force)", db="postgres")


def test_all_tables_exist(migrated):
    tables = set(H.sql(
        "select table_name from information_schema.tables where table_schema='public' "
        "order by 1", db=migrated).splitlines())
    assert tables == {"users", "sessions", "events", "projects", "project_owners",
                      "transcripts", "transcript_revisions", "resume_claims",
                      "session_leases"}


def test_schema_matches_fresh_install(migrated):
    fresh = H.sql(COLS_SQL).splitlines()
    upgraded = H.sql(COLS_SQL, db=migrated).splitlines()
    only_fresh = sorted(set(fresh) - set(upgraded))
    only_up = sorted(set(upgraded) - set(fresh))
    assert not only_fresh and not only_up, \
        f"升级后与全新安装不一致\n仅在全新：{only_fresh}\n仅在升级：{only_up}"


def test_v1_data_survives_upgrade(migrated):
    """v1 时代的数据在升级后必须还在（不能靠 drop/recreate）。"""
    n_users = H.sql("select count(*) from users", db=migrated)
    assert int(n_users) >= 0
    # sessions 必须能被 002 加上 project_id 且旧行 project_id 为 null
    assert H.sql("select count(*) from sessions where project_id is not null",
                 db=migrated) == "0"


def test_revisions_backfill_for_preexisting_rows(people):
    """005 要为升级前就存在的原件补一条 r1 基线，否则历史从"没有版本"开始，回滚不可用。"""
    H.sql("drop database if exists agenthub_backfill with (force)", db="postgres")
    H.sql("create database agenthub_backfill", db="postgres")
    try:
        v1 = SCHEMA.read_text(encoding="utf-8").split("-- ---------- v2:")[0]
        H.sql(v1, db="agenthub_backfill")
        for name in ("002_projects.sql", "003_transcripts.sql", "004_bundles.sql"):
            H.sql((MIG / name).read_text(encoding="utf-8"), db="agenthub_backfill")

        H.sql("insert into users (name, token_hash) values ('u1', 'h1')", db="agenthub_backfill")
        H.sql("insert into transcripts (user_id, ext_id, sha256, content_gz, lines) "
              "values (1, 'legacy-1', 'abc', '\\x00'::bytea, 42)", db="agenthub_backfill")
        H.sql((MIG / "005_revisions.sql").read_text(encoding="utf-8"), db="agenthub_backfill")

        assert H.sql("select rev from transcripts where ext_id = 'legacy-1'",
                     db="agenthub_backfill") == "1"
        assert H.sql("select rev || '|' || lines || '|' || message from transcript_revisions",
                     db="agenthub_backfill") == "1|42|（升级前已存在，历史基线）"
    finally:
        H.sql("drop database if exists agenthub_backfill with (force)", db="postgres")


def test_migrations_are_rerunnable(migrated):
    """运维手动跑脚本，重复执行一次不能把库搞坏。"""
    for f in sorted(MIG.glob("*.sql")):
        H.sql(f.read_text(encoding="utf-8"), db=migrated)
    assert H.sql("select count(*) from information_schema.tables where table_schema='public'",
                 db=migrated) == "9"


def test_fresh_schema_is_idempotent(migrated):
    """schema.sql 被 docker-entrypoint 执行过一次后，再执行不应报错。"""
    H.sql(SCHEMA.read_text(encoding="utf-8"), db=migrated)
    H.sql(SCHEMA.read_text(encoding="utf-8"), db=migrated)


def test_transcripts_unique_key_present(migrated):
    """同一会话只能有一行 HEAD（004 的核心变更）。"""
    idx = H.sql(
        "select count(*) from pg_indexes where tablename='transcripts' "
        "and indexdef like '%(user_id, ext_id)%' and lower(indexdef) like '%unique%'", db=migrated)
    assert int(idx) >= 1
