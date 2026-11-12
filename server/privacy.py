"""部署配置加载 + 入库前净化（脱敏 / NUL 剥除）。

从 app.py 抽出来的共享模块：transcripts_api.py（原件层）原先只剥 NUL 不过
脱敏，和事件层口径不一致；现在两条入库通道统一走这里。

注意：**原件的会话正文不脱敏**（脱敏会改坏工具输出，resume 后上下文与原始
会话不一致，见 hub-config.yaml 注释）——本模块只作用于元数据字段
（summary / title / machine / payload 等）。
"""
import os
import re

import yaml

# 部署配置：决定脱敏规则，以及 /v1/agent-prompt 生成的部署专属提示词。
# 提示词随部署而异：不同服务器改各自的 hub-config.yaml，产出不同的规则文案。
CONFIG_PATH = os.environ.get(
    "HUB_CONFIG", os.path.join(os.path.dirname(__file__), "hub-config.yaml")
)
with open(CONFIG_PATH, encoding="utf-8") as _f:
    HUB_CONFIG = yaml.safe_load(_f) or {}

_REDACTIONS = [
    (re.compile(r["pattern"]), r.get("replace", "[REDACTED]"))
    for r in HUB_CONFIG.get("privacy", {}).get("redaction", [])
]


def redact(text: str) -> str:
    """入库前脱敏：按部署配置的正则替换敏感内容。"""
    for rx, repl in _REDACTIONS:
        text = rx.sub(repl, text)
    return text


def strip_nul(v):
    """Postgres 的 text 与 jsonb 都拒绝 NUL（\\u0000）：
    hooks 上来的脏数据不带过滤直接入库，一条就会把接口打成 500。"""
    if isinstance(v, str):
        return v.replace("\x00", "")
    if isinstance(v, list):
        return [strip_nul(x) for x in v]
    if isinstance(v, dict):
        return {str(k).replace("\x00", ""): strip_nul(x) for k, x in v.items()}
    return v


def redact_obj(v):
    """递归脱敏：payload / files 里的字符串同样要走 redact，
    否则密钥只要不放 summary 就原样入库还能被搜到。"""
    if isinstance(v, str):
        return redact(v)
    if isinstance(v, list):
        return [redact_obj(x) for x in v]
    if isinstance(v, dict):
        return {k: redact_obj(x) for k, x in v.items()}
    return v
