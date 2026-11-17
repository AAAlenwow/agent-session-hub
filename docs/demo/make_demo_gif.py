#!/usr/bin/env python3
"""生成 README 用的 demo.gif。

逐帧渲染一个「终端 → agent 检索 → 手机端」的演示，再用 ffmpeg 合成 GIF。
只依赖 Pillow + ffmpeg，不联网、不读真实数据（内容全部是写死的示例）。

用法：
    python3 docs/demo/make_demo_gif.py            # 输出 docs/demo/demo.gif
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 600
FPS = 15
LEFT, TOP, LH = 48, 104, 29
BG = "#0d1117"
PANEL = "#161b22"
BORDER = "#30363d"
TEXT = "#c9d1d9"
DIM = "#7d8590"
GREEN = "#3fb950"
BLUE = "#58a6ff"
YELLOW = "#d29922"
PURPLE = "#bc8cff"
CYAN = "#39c5cf"
WHITE = "#f0f6fc"

FONT_MONO = "/System/Library/Fonts/Menlo.ttc"
FONT_CJK = "/System/Library/Fonts/Hiragino Sans GB.ttc"
FONT_UI = "/System/Library/Fonts/SFNS.ttf"

mono = ImageFont.truetype(FONT_MONO, 18)
mono_b = ImageFont.truetype(FONT_MONO, 18, index=1) if FONT_MONO else mono
cjk = ImageFont.truetype(FONT_CJK, 18)
HALF = mono.getlength("M")


def ui(size: int):
    try:
        return ImageFont.truetype(FONT_UI, size)
    except OSError:
        return ImageFont.truetype(FONT_CJK, size)


def ui_cjk(size: int):
    return ImageFont.truetype(FONT_CJK, size)


def draw_any(d, x, y, text, fill, f_ascii, f_cjk):
    for ch in text:
        if ord(ch) < 128:
            d.text((x, y), ch, font=f_ascii, fill=fill)
            x += f_ascii.getlength(ch)
        else:
            w = f_cjk.getlength(ch)
            d.text((x, y), ch, font=f_cjk, fill=fill)
            x += w


def draw_ui(d, x, y, text, size, fill, center_x=None):
    """SFNS 渲染 ASCII，中文回退 Hiragino；center_x 给定时按整串宽度水平居中。"""
    fa, fc = ui(size), ui_cjk(size)
    if center_x is not None:
        w = sum((fa if ord(c) < 128 else fc).getlength(c) for c in text)
        x = center_x - w / 2
    draw_any(d, x, y, text, fill, fa, fc)


def advance(text: str) -> float:
    """等宽网格推进量：ASCII 半角，其他按 2 个半角算。"""
    n = 0
    for ch in text:
        n += 1 if ord(ch) < 128 else 2
    return n * HALF


def draw_mixed(d: ImageDraw.ImageDraw, x: float, y: float, text: str, fill: str, bold=False):
    fm = mono_b if bold else mono
    for ch in text:
        if ord(ch) < 128:
            d.text((x, y), ch, font=fm, fill=fill)
            x += fm.getlength(ch)
        else:
            w = HALF * 2
            tw = cjk.getlength(ch)
            d.text((x + (w - tw) / 2, y), ch, font=cjk, fill=fill)
            x += w


def window(d: ImageDraw.ImageDraw, title: str, accent: str = DIM):
    d.rounded_rectangle([24, 24, W - 24, H - 24], radius=14, fill=BG, outline=BORDER, width=2)
    d.rounded_rectangle([24, 24, W - 24, 76], radius=14, fill=PANEL, outline=BORDER, width=2)
    d.rectangle([24, 60, W - 24, 78], fill=PANEL)
    for i, c in enumerate(("#f85149", "#d29922", "#3fb950")):
        cx = 52 + i * 22
        d.ellipse([cx - 6, 44, cx + 6, 56], fill=c)
    tw = ui(15).getlength(title)
    d.text(((W - tw) / 2, 41), title, font=ui(15), fill=accent)


# 每屏是 [(文本, 颜色), ...] 的列表；空列表表示空行
S1 = [
    [("$ ", GREEN), ("agenthub list", WHITE)],
    [],
    [("#   machine            project        owner   age   title", DIM)],
    [("40  iPhone             api-gateway    carol   1d    ", TEXT), ("排查 5xx 突增", TEXT)],
    [("42  Alice 的 MacBook   api-gateway    alice   3d    ", TEXT), ("限流从 100 调到 500 的原因", TEXT)],
    [("31  build-01           billing        bob     2w    ", TEXT), ("重试改指数退避", TEXT)],
    [],
    [("3 sessions · 3 machines · 2 projects · 1 server", DIM)],
]

S2 = [
    [("$ ", GREEN), ("claude", WHITE)],
    [("> ", CYAN), ("团队里谁改过限流？当时为什么从 100 调到 500？", WHITE)],
    [],
    [("● ", GREEN), ("search_team_memory", TEXT), ('("限流 500")', YELLOW)],
    [("  └─  ", DIM), ("3 hits · newest first", DIM)],
    [],
    [("  #42  alice · api-gateway · Alice 的 MacBook · 3d", BLUE)],
    [("  压测发现 100 QPS 在促销档口会被打穿，502 主要是排队超时。", TEXT)],
    [("  和运维确认后放到 500，同时加了 200ms 排队上限。", TEXT)],
    [("  回滚：改 config/prod.yaml 的 gateway.rate_limit。", TEXT)],
    [],
    [("  #31  bob · api-gateway · build-01 · 2w", BLUE)],
    [("  初版限流按 1/3 峰值留余量，定 100，偏保守。", TEXT)],
]

CARDS = [
    ("Alice 的 MacBook", "12 sessions · 3 projects", "限流从 100 调到 500 的原因", "3d · api-gateway"),
    ("build-01", "31 sessions · 6 projects", "重试改指数退避", "2w · billing"),
    ("iPhone", "4 sessions · 2 projects", "排查 5xx 突增", "1d · api-gateway"),
]


def frame_scene1(budget):
    img = Image.new("RGB", (W, H), "#010409")
    d = ImageDraw.Draw(img)
    window(d, "agenthub — team memory")
    y = TOP
    for segs in S1:
        x = LEFT
        for text, color in segs:
            if budget <= 0:
                draw_mixed(d, x, y, "▋", GREEN)
                return img
            t = text[:budget]
            draw_mixed(d, x, y, t, color)
            x += advance(t)
            budget -= len(t)
        y += LH
    if budget >= 0:
        draw_mixed(d, x, y - LH, "", TEXT)
    return img


def frame_scene2(budget):
    img = Image.new("RGB", (W, H), "#010409")
    d = ImageDraw.Draw(img)
    window(d, "claude — team-memory (MCP)", BLUE)
    y = TOP
    for segs in S2:
        x = LEFT
        for text, color in segs:
            if budget <= 0:
                draw_mixed(d, x, y, "▋", BLUE)
                return img
            t = text[:budget]
            draw_mixed(d, x, y, t, color)
            x += advance(t)
            budget -= len(t)
        y += LH
    return img


def frame_phone(shown_cards: int):
    img = Image.new("RGB", (W, H), "#010409")
    d = ImageDraw.Draw(img)
    window(d, "agent-session-hub · /m  (mobile, read-only)", PURPLE)

    pw, ph = 360, 452
    px, py = (W - pw) / 2, 96
    d.rounded_rectangle([px, py, px + pw, py + ph], radius=26, fill=BG, outline=BORDER, width=2)
    d.rounded_rectangle([px + 130, py + 12, px + 230, py + 20], radius=4, fill=BORDER)

    draw_ui(d, px + 28, py + 38, "agent-session-hub", 19, WHITE)
    draw_ui(d, px + 28, py + 64, "机器 · 会话 · 最近结论", 13, DIM)

    y = py + 92
    for i, (machine, stat, title, meta) in enumerate(CARDS):
        if i >= shown_cards:
            break
        d.rounded_rectangle([px + 18, y, px + pw - 18, y + 96], radius=10, fill=PANEL, outline=BORDER)
        draw_ui(d, px + 34, y + 12, machine, 15, CYAN)
        draw_ui(d, px + 34, y + 34, stat, 11, DIM)
        draw_ui(d, px + 34, y + 56, title, 13, TEXT)
        draw_ui(d, px + 34, y + 74, meta, 11, DIM)
        y += 108

    draw_ui(d, px + 28, py + ph - 34, "只读 · HttpOnly cookie · 30d", 12, DIM)
    return img


def frame_outro():
    img = Image.new("RGB", (W, H), "#010409")
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([24, 24, W - 24, H - 24], radius=14, fill=BG, outline=BORDER, width=2)

    title = "agent-session-hub"
    tw = ui(52).getlength(title)
    d.text(((W - tw) / 2, 210), title, font=ui(52), fill=WHITE)

    sub = "team memory for AI coding agents"
    sw = ui(22).getlength(sub)
    d.text(((W - sw) / 2, 288), sub, font=ui(22), fill=BLUE)

    line = "self-hosted · event-first · MCP · Apache-2.0"
    lw = ui(15).getlength(line)
    d.text(((W - lw) / 2, 340), line, font=ui(15), fill=DIM)

    chips = ["Claude Code", "Codex", "WorkBuddy", "DSH"]
    widths = [ui(13).getlength(t) + 22 for t in chips]
    gap = 10
    total_w = sum(widths) + gap * (len(chips) - 1)
    bx = (W - total_w) / 2
    for t, bw in zip(chips, widths):
        d.rounded_rectangle([bx, 396, bx + bw, 430], radius=8, fill=PANEL, outline=BORDER)
        d.text((bx + 11, 404), t, font=ui(13), fill=DIM)
        bx += bw + gap
    return img


def build_frames() -> list[Image.Image]:
    frames: list[Image.Image] = []

    def hold(img, n):
        frames.extend([img.copy() for _ in range(n)])

    total1 = sum(len(t) for segs in S1 for t, _ in segs)
    total2 = sum(len(t) for segs in S2 for t, _ in segs)

    hold(frame_scene1(0), 6)
    for b in range(0, total1 + 1, 9):
        frames.append(frame_scene1(min(b, total1)))
    hold(frame_scene1(total1), 34)

    hold(frame_scene2(0), 6)
    for b in range(0, total2 + 1, 9):
        frames.append(frame_scene2(min(b, total2)))
    hold(frame_scene2(total2), 50)

    for n in (1, 2, 3):
        hold(frame_phone(n), 16)

    hold(frame_outro(), 55)
    return frames


def main() -> int:
    out_dir = pathlib.Path(__file__).resolve().parent
    tmp = pathlib.Path("/tmp/ah_demo_frames")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)

    frames = build_frames()
    print(f"frames={len(frames)}  size={W}x{H}  fps={FPS}")
    for i, im in enumerate(frames):
        im.save(tmp / f"f{i:04d}.png")

    gif = out_dir / "demo.gif"
    vf = (
        "split[s0][s1];[s0]palettegen=max_colors=128:stats_mode=diff[p];"
        "[s1][p]paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle"
    )
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-framerate", str(FPS), "-i", str(tmp / "f%04d.png"),
        "-vf", vf, "-loop", "0", str(gif),
    ]
    subprocess.run(cmd, check=True)
    kb = gif.stat().st_size / 1024
    print(f"wrote {gif}  {kb:.0f} KB  duration≈{len(frames)/FPS:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
