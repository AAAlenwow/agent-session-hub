"""对抗式审查（ADVERSARIAL_REVIEW）修复的单元回归：不依赖 Docker 的部分。

对应修复：
  P1-A  wb.build_bundle 字节级确定性（服务端 unchanged 幂等的前提）
  P2-7  cc_hook 收到非对象 JSON 必须静默 exit 0
  P0-3  strip_nul 递归剥 NUL（Postgres text/jsonb 都拒绝 \\u0000）
  P1-5  redact_obj 递归脱敏（files/payload 不再是脱敏旁路）
"""
import json
import os
import pathlib
import subprocess
import sys

import pytest

import wb


def _make_sess(tmp_path: pathlib.Path, n_lines: int = 3) -> dict:
    """构造一个最小的 WorkBuddy 会话（jsonl + tool-results 旁挂）。"""
    proj = tmp_path / "projects" / "wb-slug"
    proj.mkdir(parents=True)
    sid = "sess-unit-1"
    jsonl = proj / f"{sid}.jsonl"
    lines = [
        json.dumps({"type": "message", "role": "user", "cwd": "/old/ws",
                    "ts": 1700000000000 + i}) + "\n"
        for i in range(n_lines)
    ]
    jsonl.write_text("".join(lines))
    sidecar = proj / sid / "tool-results"
    sidecar.mkdir(parents=True)
    (sidecar / "out-1.txt").write_text("tool output 1\n")
    return {
        "id": sid, "jsonl": jsonl, "project_dir": proj,
        "sidecar": sidecar, "rollback": proj / f"{sid}.file-rollback.ndjson",
        "size": jsonl.stat().st_size, "mtime": int(jsonl.stat().st_mtime),
    }


def test_build_bundle_is_byte_deterministic(tmp_path, monkeypatch):
    """同样内容打两次包必须一字节不差——否则服务端按整包 sha256 判幂等
    永远判不中，state 丢失后重同步会狂长空版本。"""
    monkeypatch.setattr(wb, "WB_HOME", tmp_path)
    monkeypatch.setattr(wb, "STATE_PATH", tmp_path / "state.json")
    def _no_db(readonly=True):
        raise FileNotFoundError("no workbuddy.db on packing machine")
    monkeypatch.setattr(wb, "connect_db", _no_db)
    sess = _make_sess(tmp_path)
    b1, _ = wb.build_bundle(sess)
    b2, _ = wb.build_bundle(sess)
    assert b1 == b2
    assert b1[:2] == b"\x1f\x8b"

    # 内容变了字节必须变（不能为了确定性牺牲正确性）
    with open(sess["jsonl"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "message", "role": "user", "cwd": "/old/ws"}) + "\n")
    b3, _ = wb.build_bundle(sess)
    assert b3 != b1


def test_cc_hook_silently_ignores_non_object_stdin():
    """hook 铁律：永远静默成功。stdin 是数组/字符串/数字时 exit 0 且无 stderr。"""
    hook = pathlib.Path(__file__).resolve().parents[1] / "hooks" / "cc_hook.py"
    for payload in ('[1,2,3]', '"str"', '123', 'null', 'true'):
        p = subprocess.run(
            [sys.executable, str(hook)], input=payload, capture_output=True,
            text=True, timeout=20,
            env={**os.environ, "AGENT_HUB_URL": "http://127.0.0.1:1",
                 "AGENT_HUB_TOKEN": "x"},
        )
        assert p.returncode == 0, f"{payload!r} 导致 exit {p.returncode}: {p.stderr}"
        assert not p.stderr, f"{payload!r} 往 stderr 吐了东西: {p.stderr[:200]}"


def test_strip_nul_is_recursive():
    from app import strip_nul
    assert strip_nul("a\x00b") == "ab"
    assert strip_nul(["x\x00", ["y\x00"]]) == ["x", ["y"]]
    assert strip_nul({"k\x00": "v\x00", "n": 1}) == {"k": "v", "n": 1}
    assert strip_nul(3) == 3


def test_redact_obj_covers_nested_payload():
    from app import redact_obj
    src = {
        "key": "sk-abcdefghijklmnop1234",
        "note": "password: hunter2secret",
        "nested": [{"path": "/tmp/sk-abcdefghijklmnop1234.env"}],
        "num": 42,
    }
    out = redact_obj(src)
    s = json.dumps(out)
    assert "sk-abcdefghijklmnop1234" not in s
    assert "hunter2secret" not in s
    assert out["num"] == 42


# ------------------------------------------------ turn_end 承载 assistant 思维链

def _load_hook(name):
    import importlib.util
    path = pathlib.Path(__file__).resolve().parents[1] / "hooks" / name
    spec = importlib.util.spec_from_file_location(name.replace(".py", "_hook_mod"), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cc_line(kind, content):
    return json.dumps({"type": kind,
                       "message": {"role": kind, "content": content}}) + "\n"


def test_last_assistant_text_extracts_final_answer(tmp_path):
    """Stop 时应从 transcript 尾部倒序提取最后一条 assistant 正文。"""
    cc = _load_hook("cc_hook.py")
    f = tmp_path / "t.jsonl"
    f.write_text(
        _cc_line("user", [{"type": "text", "text": "圣诞活动的逻辑是什么"}]) +
        _cc_line("assistant", [{"type": "text", "text": "老逻辑是满减+积分翻倍"}]) +
        _cc_line("assistant", [{"type": "tool_use", "id": "tu", "name": "Edit"}]) +
        # 最后一条 assistant 只有 tool_use 没正文 → 应回溯到上一条带正文的
        _cc_line("assistant", [{"type": "text", "text": "修复完成：见 gift.py 的双倍积分分支"}])
    )
    assert cc.last_assistant_text(str(f)) == "修复完成：见 gift.py 的双倍积分分支"
    # 只有 tool_use 的结尾：回溯到更早的正文
    f.write_text(
        _cc_line("assistant", [{"type": "text", "text": "answer-1"}]) +
        _cc_line("assistant", [{"type": "tool_use", "id": "tu", "name": "Bash"}])
    )
    assert cc.last_assistant_text(str(f)) == "answer-1"


def test_last_assistant_text_survives_garbage(tmp_path):
    """transcript 不存在 / 二进制 / 空路径 → 一律空串，不抛异常。"""
    cc = _load_hook("cc_hook.py")
    assert cc.last_assistant_text("") == ""
    assert cc.last_assistant_text(str(tmp_path / "nope.jsonl")) == ""
    bad = tmp_path / "bad.jsonl"
    bad.write_bytes(b"\xff\xfe\x00garbage\n" + b"nonsense" * 100)
    assert cc.last_assistant_text(str(bad)) == ""
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    assert cc.last_assistant_text(str(empty)) == ""


def test_stop_event_carries_answer_and_falls_back(tmp_path):
    """build_event(Stop) 带 transcript 时摘要=回答；不带时退占位符。"""
    cc = _load_hook("cc_hook.py")
    f = tmp_path / "t.jsonl"
    f.write_text(_cc_line("assistant", [{"type": "text", "text": "改完了\n待办：回归测试"}]))
    ev = cc.build_event({"hook_event_name": "Stop", "transcript_path": str(f)})
    assert ev["kind"] == "turn_end"
    assert ev["summary"] == "改完了 待办：回归测试"   # 换行折叠成空格
    # 无 transcript_path / 文件不存在 → 占位符，不能抛异常
    ev = cc.build_event({"hook_event_name": "Stop"})
    assert ev["summary"] == "assistant turn finished"
    ev = cc.build_event({"hook_event_name": "Stop",
                         "transcript_path": str(tmp_path / "nope.jsonl")})
    assert ev["summary"] == "assistant turn finished"


def test_turn_summary_is_capped(tmp_path, monkeypatch):
    """800 字上限可被环境变量覆盖，超长回答被截断。"""
    cc = _load_hook("cc_hook.py")
    monkeypatch.setattr(cc, "MAX_TURN_SUMMARY", 10)
    f = tmp_path / "t.jsonl"
    f.write_text(_cc_line("assistant", [{"type": "text", "text": "x" * 500}]))
    ev = cc.build_event({"hook_event_name": "Stop", "transcript_path": str(f)})
    assert ev["summary"] == "x" * 10


def test_codex_rollout_extraction_both_shapes(tmp_path):
    """Codex rollout 两种行形态都能提取；提不出来退占位符。"""
    cx = _load_hook("codex_notify.py")
    f = tmp_path / "rollout-x.jsonl"
    f.write_text(json.dumps({
        "timestamp": "2026-11-12T00:00:00Z", "type": "response_item",
        "payload": {"type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": "go"}]},
    }) + "\n" + json.dumps({
        "timestamp": "2026-11-12T00:00:01Z", "type": "response_item",
        "payload": {"type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "第一版实现"}]},
    }) + "\n")
    assert cx.last_assistant_text(str(f)) == "第一版实现"
    # 新形态：event_msg agent_message
    f.write_text(json.dumps({
        "timestamp": "2026-11-12T00:00:02Z", "type": "event_msg",
        "payload": {"type": "agent_message", "message": "最终回答：德国活动已迁移"},
    }) + "\n")
    assert cx.last_assistant_text(str(f)) == "最终回答：德国活动已迁移"
    assert cx.last_assistant_text("") == ""
    assert cx.last_assistant_text(str(tmp_path / "nope.jsonl")) == ""
