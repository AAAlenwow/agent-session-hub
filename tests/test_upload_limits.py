"""上传体积上限：不能等整个请求读进内存再判断（P0-2 的回归锁）。"""
import asyncio

import pytest

fastapi = pytest.importorskip("fastapi")

import transcripts_api  # noqa: E402  server/transcripts_api.py


def _request(chunks, content_length=None):
    from starlette.requests import Request

    remaining = list(chunks)

    async def receive():
        if remaining:
            body = remaining.pop(0)
            return {"type": "http.request", "body": body, "more_body": bool(remaining)}
        return {"type": "http.disconnect"}

    headers = []
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode()))
    return Request({"type": "http", "method": "POST", "path": "/", "headers": headers},
                   receive)


def test_declared_content_length_is_rejected_early():
    req = _request([b"x"], content_length=10 * 1024 * 1024)
    with pytest.raises(fastapi.HTTPException) as e:
        asyncio.run(transcripts_api._read_body(req, 1024))
    assert e.value.status_code == 413


def test_streaming_body_is_cut_off_at_limit():
    req = _request([b"a" * 600, b"a" * 600], content_length=None)
    with pytest.raises(fastapi.HTTPException) as e:
        asyncio.run(transcripts_api._read_body(req, 1024))
    assert e.value.status_code == 413


def test_normal_body_passes_through():
    req = _request([b"abc", b"def"], content_length=6)
    assert asyncio.run(transcripts_api._read_body(req, 1024)) == b"abcdef"


def test_ext_id_charset_is_restricted():
    """ext_id 会进响应头，必须限制字符集。"""
    assert transcripts_api._EXT_ID_RE.match("0f3a-11ee_8b2c.v1")
    for bad in ("abc\r\nX-Injected: 1", 'a"b', "../../etc/passwd", "", "a" * 65):
        assert not transcripts_api._EXT_ID_RE.match(bad)


def test_header_values_are_sanitized():
    assert transcripts_api._hdr('a\r\nX-Evil: 1') == "aX-Evil: 1"
    assert transcripts_api._hdr('a"b') == "ab"
    assert transcripts_api._hdr(None) == ""
