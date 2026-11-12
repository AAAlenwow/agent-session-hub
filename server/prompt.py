"""按部署配置渲染「给 agent 看的使用说明」（/v1/agent-prompt）。

关键设计：提示词不是通用文案，而是每个部署实例根据 hub-config.yaml
（隐私模式、脱敏规则、保留期、实例信息）动态生成——
私有化程度不同的服务器，产出不同的提示词，互不通用。
"""

_MODES = {
    "auto": {
        "label": "自动上报（auto）",
        "share_when": "本部署为自动上报模式，正常情况下无需调用",
        "rule": (
            "你所在机器的 hooks 已被授权自动上报会话事件（提问原文、改动的文件、回合结束）。\n"
            "因此你不需要、也不应该主动调用分享工具；本 MCP 对你的主要价值是**查询**团队记忆。"
        ),
    },
    "curated": {
        "label": "人工精选（curated）",
        "share_when": "仅当用户明确要求分享会话 / 结论时",
        "rule": (
            "本部署默认**不上报任何内容**，你的一切会话都留在用户本机。\n"
            "仅当用户明确表达「分享这次会话」「把这个结论记到团队」「让团队记住这个坑」等意图时，\n"
            "才调用 `share_team_memory`；其余时候绝不调用。"
        ),
    },
    "strict": {
        "label": "严格确认（strict）",
        "share_when": "用户明确要求、且已确认你展示的摘要之后",
        "rule": (
            "本部署默认不上报任何内容，且每次分享都要用户逐次确认。流程：\n"
            "① 用户表达分享意图 → ② 你把将要分享的标题、摘要、文件列表**原样展示给用户** →\n"
            "③ 用户明确同意后再调用 `share_team_memory`。用户未确认时不要调用。"
        ),
    },
}


def build_prompt(cfg: dict) -> str:
    inst = cfg.get("instance", {})
    privacy = cfg.get("privacy", {})
    mode = str(privacy.get("mode", "curated")).lower()
    m = _MODES.get(mode, _MODES["curated"])
    name = inst.get("name", "团队")
    url = inst.get("url", "（地址见管理员）")
    retention = cfg.get("retention_days")
    tr = cfg.get("transcripts") or {}

    lines: list[str] = []
    a = lines.append
    a(f"# {name} · team-memory MCP 使用说明")
    a("")
    a(f"> 本说明由你们私有部署的 agent-hub 服务器（{url}）按其部署配置动态生成，")
    a("> **只对这一套部署有效**。不同服务器的私有化程度不同，生成的规则也不同，")
    a("> 不要把这里的规则套用到其他部署。配置变更后以 `team_memory_instructions()` 的最新返回为准。")
    a("")
    a("## 这是什么")
    a("")
    a(f"你的运行环境挂载了 `team-memory` MCP，连接到 **{name}**：团队成员用 Claude Code /")
    a("Codex / GLM / MiniMax 等 agent 工作时沉淀的会话记忆都在这里，每条记录带作者、时间、")
    a("所属仓库，可检索、可追溯。它让你能回答「谁改过 X、当时为什么这么改、结论是什么」。")
    a("")
    a(f"## 本部署的隐私模式：{m['label']}")
    a("")
    a(m["rule"])
    a("")
    a("## 可用工具与使用时机")
    a("")
    a("| 工具 | 何时使用 |")
    a("|---|---|")
    a("| `team_memory_instructions()` | 获取本部署最新使用说明（配置可能变更，以它为准） |")
    a("| `search_team_memory(q)` | 接到与团队历史 / 既有决策相关的疑问时，先搜再答，不要凭空编 |")
    a("| `recent_team_sessions()` | 需要了解团队最近在哪些仓库做了什么时 |")
    a("| `get_team_session(id)` | 搜索命中后需要完整上下文时，拉取该会话时间线 |")
    a("")
    a("会话时间线里每个回合的 `turn_end` 事件带当时 assistant 的最终回答摘要"
      "（任务进展、修复说明、结论、待办等），可以直接用来回答"
      "「圣诞活动的逻辑是什么」「上次修到哪一步了」这类问题——先搜 turn_end，"
      "不够再让用户拉原件回放。")
    a("")
    a(f"每条会话都记录了**上传机器**（hostname，或成员用 AGENT_HUB_MACHINE 起的"
      f"别名如「hechao 的 mac」「香港服务器」）。用户问「这是哪台机器上的活」时用"
      f" `list_team_machines()`。手机上可直接打开 {url}/m 查看（个人 token 登录，只读）。")
    a("| `list_team_projects()` | 想知道团队有哪些项目、各自负责人是谁时 |")
    a("| `list_team_machines()` | 想知道会话来自哪些机器 / 服务器、哪台机器在做什么时 |")
    a("| `get_project_handover(project)` | 接手别人的项目时：一次拿到历任负责人、上一任的历史会话、"
      "改动热点文件、用过的技能包与分享过的结论 |")
    a(f"| `share_team_memory(title, summary, files, tags)` | {m['share_when']} |")
    a("")
    a("## 分享内容怎么写")
    a("")
    a("- `summary` 先写结论，再写原因和影响，一段话讲清；不要流水账。")
    if privacy.get("share_prompt_content", True):
        a("- 可以引用当时的指令 / 需求原文，帮助后来者理解背景。")
    else:
        a("- 不要复述指令 / 需求原文，只写结论与影响（本部署要求）。")
    a("- `files` 填涉及的文件路径（相对仓库根）；`tags` 填主题词，如限流、部署、踩坑。")
    a("- 不确定内容是否敏感时，先问用户再分享。")
    a("")

    if tr.get("enabled"):
        a("## 会话原件回放")
        a("")
        a("本部署开启了原件层：完整会话文件（含上下文与用量）已归档。")
        a("当用户说「接着某个会话继续」时，提示用户在本机仓库目录执行下面的命令（由用户操作，不是你调用）：")
        a("")
        a("```bash")
        a("agenthub list                                # 找到要接的会话 id（带 token/时长）")
        a("agenthub pull <id> --into <本机仓库绝对路径>  # Claude Code / Codex：自动按本机路径重写")
        a("claude --resume <session-id>                 # 接着原来的上下文聊")
        a("```")
        a("")
        a("WorkBuddy 的会话（打包体，含 tool-results 旁挂文件与索引行）用另一对命令：")
        a("")
        a("```bash")
        a("agenthub wb-sync                             # 交互式：先列计划，用户挑选后确认上传")
        a("agenthub wb-pull <id> --into <本机工作区路径>  # 在另一台机器还原，写完重启 WorkBuddy")
        a("agenthub wb-hist <id>                        # 提交历史（每次同步=一次提交，可 diff）")
        a("agenthub wb-diff <id> --from 2 --to 3        # 看两个版本之间改了什么")
        a("agenthub wb-revert <id> <rev>                # 回滚到某个历史版本（历史保留）")
        a("```")
        a("")
        a("同步永远由用户发起：不要代替用户执行 wb-sync，也不要建议后台定时上传。")
        a("")
        a("同一会话不要在两台机器同时 resume（服务端有占用锁，会拒绝第二个）。")
        a("")

    a("## 禁止事项")
    a("")
    a("- 不要把 API key、密码、token、证书、内网拓扑写进任何分享内容。")
    if privacy.get("redaction"):
        a("- 事件层（摘要、标题）入库前会按本部署规则自动脱敏，但这不是你写敏感信息的理由。")
    if tr.get("enabled"):
        a("- **会话原件按原文保存，不做脱敏**：本部署开启了原件层，整份对话（含工具输出原文）"
          "都会原样归档，团队其他成员可拉取回放。不要把凭据写进会话，也不要指望事后脱敏。")
    a("- 不要出于好奇检索与当前任务无关的其他成员会话。")
    a("- 分享失败时如实告知用户，不要静默重试。")
    if retention:
        a(f"- 本部署记录保留 {retention} 天；分享意味着团队任何成员在此期限内都能检索到它。")
    a("")
    return "\n".join(lines)
