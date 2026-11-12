"""场景 4：部署专属提示词。

关键约定：提示词由**这一套部署**的 hub-config.yaml 渲染，不同部署的文案必须不同；
且提示词里关于脱敏的说法必须与服务器实际行为一致（说过头就是骗 agent）。
"""
import pytest

import helpers as H
from prompt import build_prompt


BASE_CFG = {
    "instance": {"name": "某事业部记忆库", "url": "https://mem.example.local"},
    "privacy": {
        "mode": "curated",
        "share_prompt_content": True,
        "redaction": [{"pattern": "sk-[A-Za-z0-9]{8,}", "replace": "[REDACTED-KEY]"}],
    },
    "retention_days": 180,
    "transcripts": {"enabled": False},
}


def cfg(**over):
    c = {**BASE_CFG, **over}
    if "privacy" in over:
        c["privacy"] = {**BASE_CFG["privacy"], **over["privacy"]}
    return c


def test_prompt_requires_auth(hub):
    assert hub.get("/v1/agent-prompt").status == 401


def test_live_prompt_reflects_this_deployment(people):
    r = people["alice"]["api"].get("/v1/agent-prompt")
    assert r.status == 200
    text = r.text
    assert "E2E 测试部署" in text and "https://e2e.agenthub.local" in text
    assert "自动上报（auto）" in text
    assert "只对这一套部署有效" in text


def test_live_prompt_lists_real_tool_names(people):
    text = people["alice"]["api"].get("/v1/agent-prompt").text
    for name in ("team_memory_instructions", "search_team_memory", "recent_team_sessions",
                 "get_team_session", "share_team_memory"):
        assert name in text, f"提示词里少了工具 {name}"


def test_live_prompt_does_not_overclaim_redaction(people):
    """原件层开着，就必须说清"原件原文入库、不脱敏"。"""
    text = people["alice"]["api"].get("/v1/agent-prompt").text
    assert "服务器端已配置自动脱敏" not in text
    assert "会话原件按原文保存，不做脱敏" in text


def test_live_prompt_mentions_manual_sync_only(people):
    """用户明确要求：同步必须由用户发起，不允许后台定时。"""
    text = people["alice"]["api"].get("/v1/agent-prompt").text
    assert "同步永远由用户发起" in text
    assert "wb-sync" in text and "wb-revert" in text
    assert "30 天" in text


def test_three_modes_produce_different_rules():
    auto = build_prompt(cfg(privacy={"mode": "auto"}))
    curated = build_prompt(cfg(privacy={"mode": "curated"}))
    strict = build_prompt(cfg(privacy={"mode": "strict"}))
    assert len({auto, curated, strict}) == 3
    assert "不需要、也不应该主动调用分享工具" in auto
    assert "默认**不上报任何内容**" in curated
    assert "逐次确认" in strict and "用户未确认时不要调用" in strict


def test_mode_label_is_rendered():
    assert "人工精选（curated）" in build_prompt(cfg())
    assert "严格确认（strict）" in build_prompt(cfg(privacy={"mode": "strict"}))


def test_unknown_mode_falls_back_to_curated():
    text = build_prompt(cfg(privacy={"mode": "yolo"}))
    assert "人工精选（curated）" in text


def test_share_prompt_content_false_changes_wording():
    on = build_prompt(cfg(privacy={"share_prompt_content": True}))
    off = build_prompt(cfg(privacy={"share_prompt_content": False}))
    assert "可以引用当时的指令" in on
    assert "不要复述指令" in off and "可以引用当时的指令" not in off


def test_transcripts_section_toggles():
    assert "会话原件回放" not in build_prompt(cfg())
    assert "会话原件回放" in build_prompt(cfg(transcripts={"enabled": True}))
    assert "会话原件按原文保存" not in build_prompt(cfg())


def test_retention_line_is_conditional():
    assert "保留 180 天" in build_prompt(cfg())
    assert "保留" not in build_prompt(cfg(retention_days=None))


def test_redaction_hint_only_when_rules_exist():
    assert "自动脱敏" in build_prompt(cfg())
    assert "自动脱敏" not in build_prompt(cfg(privacy={"redaction": []}))


def test_prompt_has_no_unrendered_placeholders(people):
    text = people["alice"]["api"].get("/v1/agent-prompt").text
    assert "{" not in text and "}" not in text
    assert "None" not in text
