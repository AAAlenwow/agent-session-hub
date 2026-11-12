"""机器溯源 + 通用上报器（dsh-go 等第三方插件接入契约）的单元回归。"""
import argparse
import importlib.util
import json
import pathlib
import subprocess
import sys

HOOKS = pathlib.Path(__file__).resolve().parents[1] / "hooks"
SERVER = pathlib.Path(__file__).resolve().parents[1] / "server"
sys.path.insert(0, str(SERVER))


def test_privacy_redact_applies_config_patterns():
    """privacy.py 是事件层 / 原件层共享的脱敏入口：配置正则必须生效。"""
    from privacy import redact
    assert redact("key sk-abcdefghijklmnop1234 here") == "key [REDACTED-KEY] here"
    assert redact("password: hunter2secret") == "[REDACTED-CREDENTIAL]"
    assert redact("香港服务器") == "香港服务器"          # 正常机器名不受影响


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name.replace(".py", "_mod"), HOOKS / name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_machine_name_env_override(monkeypatch):
    """默认 hostname；AGENT_HUB_MACHINE 可读性更好的别名优先。"""
    cc = _load("cc_hook.py")
    monkeypatch.setenv("AGENT_HUB_MACHINE", "hechao 的 mac")
    assert cc.machine_name() == "hechao 的 mac"
    monkeypatch.delenv("AGENT_HUB_MACHINE")
    assert cc.machine_name()  # hostname 非空
    assert len(cc.machine_name()) <= 120

    cx = _load("codex_notify.py")
    monkeypatch.setenv("AGENT_HUB_MACHINE", "香港服务器")
    assert cx.machine_name() == "香港服务器"


def test_generic_report_from_args():
    """命令行模式：缺省字段自动补齐（machine / project），事件结构正确。"""
    gr = _load("generic_report.py")
    ns = argparse.Namespace(
        session_id="dsh-abc", agent="dsh", project="", machine="",
        cwd="/home/u/code/geeadmin", title="", kind="turn_end",
        summary="修好了限流", file=["src/limit.py"], ts="",
    )
    body = gr.from_args(ns)
    assert body["session"]["ext_id"] == "dsh-abc"
    assert body["session"]["project"] == "geeadmin"      # 从 cwd 目录名推
    assert body["session"]["machine"]                         # hostname 兜底
    assert body["events"] == [{"kind": "turn_end", "summary": "修好了限流",
                               "files": ["src/limit.py"]}]


def test_generic_report_stdin_defaults():
    """stdin 模式：缺 machine 自动补；缺 agent 默认 dsh；events 为空要报错。"""
    gr = _load("generic_report.py")
    body = gr.from_stdin(json.dumps({
        "session": {"ext_id": "dsh-1", "cwd": "/srv/app/geeadmin"},
        "events": [{"kind": "turn_end", "summary": "ok"}],
    }))
    assert body["session"]["machine"]
    assert body["session"]["agent"] == "dsh"
    assert body["session"]["project"] == "geeadmin"

    try:
        gr.from_stdin('{"session": {"ext_id": "x"}}')
    except ValueError as e:
        assert "events" in str(e)
    else:
        raise AssertionError("空 events 必须报错")


def test_generic_report_cli_silent_without_token(tmp_path):
    """没配 token 时静默 exit 0，绝不把宿主插件搞崩。"""
    p = subprocess.run(
        [sys.executable, str(HOOKS / "generic_report.py"),
         "--session-id", "x", "--summary", "y"],
        capture_output=True, text=True, timeout=20,
        env={"PATH": "/usr/bin:/bin", "AGENT_HUB_TOKEN": ""},
    )
    assert p.returncode == 0, p.stderr
    assert not p.stderr


def test_generic_report_cli_reports_failure_clearly():
    """服务不可达时给清晰错误（cron 里可据此告警），而不是静默吞掉。"""
    p = subprocess.run(
        [sys.executable, str(HOOKS / "generic_report.py"),
         "--session-id", "x", "--summary", "y"],
        capture_output=True, text=True, timeout=30,
        env={"PATH": "/usr/bin:/bin", "AGENT_HUB_TOKEN": "t",
             "AGENT_HUB_URL": "http://127.0.0.1:1"},
    )
    assert p.returncode == 1
    assert "上报失败" in p.stderr
