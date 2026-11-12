"""跨机路径重写：同前缀仓库不能被误伤（P1-1 的回归锁）。

真实场景：同事的仓库叫 api，你的叫 api-gateway。把 /code/api 重写成 /new/repo 时，
/code/api-gateway 必须保持原样，否则 --resume 找不到会话。
"""
import io
import tarfile

import wb
from transcripts_parse import rewrite_in_bundle, rewrite_paths

OLD = "/code/api"
NEW = "/new/repo"


def test_prefix_sibling_is_not_rewritten():
    raw = b'{"cwd":"/code/api-gateway","p":"/code/api-gateway/main.py"}\n'
    out = rewrite_paths(raw, [(OLD, NEW)]).decode()
    assert "/code/api-gateway" in out
    assert "/new/repo-gateway" not in out


def test_real_path_is_rewritten():
    raw = b'{"cwd":"/code/api","p":"/code/api/src/main.py"}\n'
    out = rewrite_paths(raw, [(OLD, NEW)]).decode()
    assert '"cwd":"/new/repo"' in out
    assert "/new/repo/src/main.py" in out


def test_bundle_member_rewrite_respects_boundary():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        body = b'{"cwd":"/code/api","other":"/code/api-gateway/x"}\n'
        ti = tarfile.TarInfo("s.jsonl")
        ti.size = len(body)
        tf.addfile(ti, io.BytesIO(body))

    out = rewrite_in_bundle(buf.getvalue(), [(OLD, NEW)])
    with tarfile.open(fileobj=io.BytesIO(out), mode="r:gz") as tf:
        text = tf.extractfile("s.jsonl").read().decode()

    assert '"/code/api-gateway/x"' in text
    assert '{"cwd":"/new/repo"' in text


def test_client_replace_path_matches_server_behavior():
    """客户端与服务端必须用同一套边界规则，否则 pull 与上传后的内容会不一致。"""
    assert wb.replace_path("/code/api-gateway/x", OLD, NEW) == "/code/api-gateway/x"
    assert wb.replace_path("/code/api/x", OLD, NEW) == "/new/repo/x"
