"""EventIn.ts 的解析契约（2026-11-16 修 500 的回归测试）。

原缺陷：ts 声明为 str 原样下传，asyncpg 在客户端编码阶段拒绝 str，
任何带时间戳的上报都 500。hooks 从不传 ts 所以历史测试没覆盖到。
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))


def _ev(ts):
    from app import EventIn
    return EventIn(kind="turn_end", summary="x", ts=ts)


def test_z_suffix_becomes_aware_datetime():
    """前端/第三方最常见的写法：UTC + Z。"""
    got = _ev("2026-11-16T06:07:44.969Z").ts
    assert isinstance(got, datetime)
    assert got.tzinfo is not None
    assert got.utcoffset().total_seconds() == 0


def test_offset_is_preserved():
    got = _ev("2026-11-16T14:07:44+08:00").ts
    assert got.utcoffset().total_seconds() == 8 * 3600
    assert got.astimezone(timezone.utc).hour == 6


def test_naive_is_treated_as_utc():
    """跨机场景本机时区不可知，显式约定为 UTC 而不是猜。"""
    got = _ev("2026-11-16T06:07:44").ts
    assert got.tzinfo == timezone.utc


def test_none_and_empty_use_server_time():
    assert _ev(None).ts is None
    assert _ev("").ts is None


def test_garbage_is_rejected_not_500():
    with pytest.raises(ValueError):
        _ev("昨天下午")


def test_datetime_passthrough():
    d = datetime(2026, 11, 16, 6, 0, tzinfo=timezone.utc)
    assert _ev(d).ts == d
