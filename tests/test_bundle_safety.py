"""打包体安全测试：路径穿越 / 绝对路径 / 体积守卫。

这些用例是 P0 的回归锁 —— 它们失败，就说明又可以把任意文件写到用户机器上了。
"""
import gzip
import io
import json
import pathlib
import tarfile

import pytest

from transcripts_parse import bundle_guard, decompress_limited


# --------------------------------------------------------------- 构造工具

def _meta(ext_id: str, cwd: str = "/old/path") -> bytes:
    return json.dumps({"ext_id": ext_id, "cwd": cwd}).encode()


def make_bundle(members: list[tuple[str, bytes]], ext_id: str = "sess-1",
                cwd: str = "/old/path") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        ti = tarfile.TarInfo("_agenthub/meta.json")
        data = _meta(ext_id, cwd)
        ti.size = len(data)
        tf.addfile(ti, io.BytesIO(data))
        for name, body in members:
            ti = tarfile.TarInfo(name)
            ti.size = len(body)
            tf.addfile(ti, io.BytesIO(body))
    return buf.getvalue()


BODY = b'{"type":"message","role":"user","content":[{"type":"input_text","text":"hi"}]}\n'


# --------------------------------------------------------------- P0-1 穿越

def test_install_bundle_rejects_parent_traversal(tmp_path, monkeypatch):
    import wb

    monkeypatch.setattr(wb, "WB_HOME", tmp_path / "wb")
    bundle = make_bundle([("sess-1.jsonl", BODY), ("../../../../PWNED.txt", b"pwned\n")])

    with pytest.raises(RuntimeError, match="越界"):
        wb.install_bundle(bundle, str(tmp_path / "workspace"), patch_db=False)

    assert not (tmp_path / "PWNED.txt").exists()
    assert not (tmp_path / "wb" / "PWNED.txt").exists()


def test_install_bundle_rejects_absolute_member(tmp_path, monkeypatch):
    import wb

    monkeypatch.setattr(wb, "WB_HOME", tmp_path / "wb")
    target = tmp_path / "ESCAPED.txt"
    bundle = make_bundle([("sess-1.jsonl", BODY), (str(target), b"pwned\n")])

    with pytest.raises(RuntimeError, match="越界"):
        wb.install_bundle(bundle, str(tmp_path / "workspace"), patch_db=False)

    assert not target.exists()


def test_install_bundle_rejects_nested_traversal(tmp_path, monkeypatch):
    """sid/tool-results/../../../x 这种藏在子目录里的逃逸也要拦住。"""
    import wb

    monkeypatch.setattr(wb, "WB_HOME", tmp_path / "wb")
    bundle = make_bundle([("sess-1.jsonl", BODY), ("sess-1/tool-results/../../../../x.txt", b"x")])

    with pytest.raises(RuntimeError, match="越界"):
        wb.install_bundle(bundle, str(tmp_path / "workspace"), patch_db=False)


def test_install_bundle_normal_member_is_written(tmp_path, monkeypatch):
    """正常打包体要照常落盘（别把安全修改变成功能倒车）。"""
    import wb

    monkeypatch.setattr(wb, "WB_HOME", tmp_path / "wb")
    bundle = make_bundle([
        ("sess-1.jsonl", BODY),
        ("sess-1/tool-results/t1.txt", b"sidecar"),
    ])
    res = wb.install_bundle(bundle, str(tmp_path / "workspace"),
                            rewrite_from="/old/path", patch_db=False)

    target_dir = pathlib.Path(res["installed_to"])
    assert (target_dir / "sess-1.jsonl").read_bytes() == BODY
    assert (target_dir / "sess-1/tool-results/t1.txt").read_bytes() == b"sidecar"
    assert res["session_id"] == "sess-1"


# --------------------------------------------------------------- P0-2 体积

def test_decompress_limited_accepts_normal_payload():
    raw = BODY * 100
    assert decompress_limited(gzip.compress(raw), len(raw) + 10) == raw


def test_decompress_limited_rejects_gzip_bomb():
    bomb = gzip.compress(b"\0" * (50 * 1024 * 1024))     # 50MB 压成几十 KB
    with pytest.raises(ValueError, match="上限"):
        decompress_limited(bomb, 1024 * 1024)
    assert len(bomb) < 200 * 1024                        # 确认它确实是"小体积大展开"


def test_decompress_limited_rejects_plain_oversize():
    with pytest.raises(ValueError, match="上限"):
        decompress_limited(b"x" * 2048, 1024)


def test_bundle_guard_rejects_oversize_member():
    bundle = make_bundle([("sess-1.jsonl", b"x" * (2 * 1024 * 1024))])
    with pytest.raises(ValueError, match="上限"):
        bundle_guard(bundle, 1024 * 1024)


def test_bundle_guard_rejects_member_count(monkeypatch):
    import transcripts_parse

    monkeypatch.setattr(transcripts_parse, "MAX_BUNDLE_MEMBERS", 5)   # 免得造 5001 个成员
    members = [(f"sess-1/tool-results/{i}.txt", b"x") for i in range(30)]
    bundle = make_bundle(members)
    with pytest.raises(ValueError, match="成员过多"):
        bundle_guard(bundle, 64 * 1024 * 1024)


def test_bundle_guard_accepts_normal_bundle():
    bundle_guard(make_bundle([("sess-1.jsonl", BODY)]), 1024 * 1024)
