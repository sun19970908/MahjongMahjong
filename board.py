"""把 state.json 画成一张网页桌面（board.html），打牌时另开个浏览器看着。

    python board.py                    # 读 state.json，写 桌面.html
    python board.py --watch 2          # 每 2 秒重画一次（挂着当直播看）
    python board.py --out 桌面.html --seat 2

贴图：想换成真牌面，把 4 套牌图按 `tiles/<牌名>.png` 丢进 tiles/ 目录即可
（牌名形如 m1 / p5 / s9 / z5，红中是 z5），脚本会自动改用图片，没有图就用文字画。
"""

from __future__ import annotations

import argparse
import html
import json
import time
from pathlib import Path

import seats as seats_mod
from referee import tile_id

HERE = Path(__file__).resolve().parent
STATE_PATH = HERE / "state.json"
TILES_DIR = HERE / "tiles"

SUIT_COLOR = {"m": "#b3261e", "p": "#1d4ed8", "s": "#0f7b3a", "z": "#c026d3"}
SUIT_NAME = {"m": "萬", "p": "餅", "s": "條", "z": ""}

RANK = ["一", "二", "三", "四", "五", "六", "七", "八", "九"]


def _parts(tid: str) -> tuple[str, str]:
    """m3 → ('三', '萬')；z5 → ('中', '')。"""
    if tid.startswith("z"):
        return ("中" if tid == "z5" else "?"), ""
    s, n = tid[0], int(tid[1])
    return RANK[n - 1], SUIT_NAME[s]


def tile_html(tid: str, *, small: bool = False) -> str:
    """一张牌：有贴图用贴图，没有就用字画。"""
    png = TILES_DIR / f"{tid}.png"
    cls = " tile small" if small else " tile"
    if png.exists():
        return f'<span class="{cls} img" style="background-image:url(tiles/{tid}.png)"></span>'
    num, suit = _parts(tid)
    color = SUIT_COLOR.get(tid[0], "#333")
    if tid.startswith("z"):
        body = f'<b style="color:{color}">中</b>'
    else:
        body = f'<b>{num}</b><i style="color:{color}">{suit}</i>'
    return f'<span class="{cls}">{body}</span>'


def hand_html(counts: dict, seat: int, hands_format: str) -> str:
    """手牌：存档里是计数数组，展开成一张张牌。"""
    if hands_format != "counts":
        return ""
    arr = counts.get(str(seat)) or []
    out = []
    for t, n in enumerate(arr):
        out.extend(tile_html(tile_id(t)) for _ in range(n))
    return "".join(out)


def melds_html(melds: list) -> str:
    if not melds:
        return '<span class="empty">—</span>'
    groups = []
    for m in melds:
        groups.append("".join(tile_html(t, small=True) for t in m["tiles"]))
    return " | ".join(groups)


def river_html(tiles: list) -> str:
    return "".join(tile_html(t, small=True) for t in tiles) or '<span class="empty">—</span>'


CSS = """
body{background:#123524;color:#eee;font-family:"Microsoft YaHei",sans-serif;margin:0}
.wrap{max-width:1080px;margin:0 auto;padding:18px}
h1{font-size:20px;margin:0 0 6px;font-weight:600}
.meta{color:#9cc4a8;font-size:13px;margin-bottom:14px}
.table{background:#1b4d33;border:1px solid #2f6f4c;border-radius:14px;padding:16px}
.seat{border-top:1px dashed #2f6f4c;padding:10px 0}
.seat:first-child{border-top:none}
.who{font-size:14px;color:#cfe8d8;margin-bottom:6px}
.badge{display:inline-block;font-size:11px;padding:1px 6px;border-radius:8px;margin-left:6px;
       background:#2f6f4c;color:#dff}
.badge.dealer{background:#8a5a00}
.tile{display:inline-flex;flex-direction:column;align-items:center;justify-content:center;
      width:34px;height:46px;margin:2px;border-radius:6px;background:#fdfaf0;color:#222;
      box-shadow:0 2px 0 #cfc6ad;font-size:15px;vertical-align:top}
.tile.small{width:26px;height:35px;font-size:12px;box-shadow:0 1px 0 #cfc6ad}
.tile.img{background-size:cover;background-position:center}
.tile b{line-height:1;font-weight:700}
.tile i{font-style:normal;font-size:11px;line-height:1}
.empty{color:#6f9b82}
.river{line-height:1}
.settle{background:#0d2c1d;border:1px solid #2f6f4c;border-radius:10px;padding:10px 12px;margin-top:12px}
.settle h2{font-size:15px;margin:0 0 6px;color:#ffd479}
"""


def render(state: dict, *, me_no: int | None) -> str:
    cfg = state.get("cfg") or {}
    gs = state.get("game") or {}
    meta = state.get("meta") or {}
    seats = int(cfg.get("seats") or 4)
    dealer = cfg.get("dealer")
    dice = cfg.get("dice")
    hands_format = gs.get("hands_format") or ""
    counts = gs.get("hands") or {}
    fixed = gs.get("fixed") or {}
    discards = gs.get("discards") or {}
    ting = gs.get("ting") or {}
    dead = gs.get("dead") or {}
    kans = gs.get("kans") or {}
    wall = gs.get("wall") or []

    names = {}
    for key in ("you", "buddy", "agent"):
        if meta.get(key) is not None:
            names[int(meta[key])] = key

    parts = ['<div class="wrap">', "<h1>麻将桌</h1>",
             f'<div class="meta">第 {meta.get("steps", 0)} 步 · 阶段 {gs.get("phase", "?")} · '
             f'牌墙 {len(wall)} 张 · 明牌 {wall[-1] if wall else "—"}</div>',
             '<div class="table">']

    # 从自己开始绕一圈，看着顺眼
    start = (me_no or 1) - 1
    order = [(start + i) % seats for i in range(seats)]
    for s in order:
        no = seats_mod.label(s)
        badges = []
        if s == dealer:
            badges.append('<span class="badge dealer">庄</span>')
        if dice:
            badges.append(f'<span class="badge">骰 {dice[0]}+{dice[1]}={dice[2]}</span>')
        if ting.get(str(s)):
            badges.append('<span class="badge">报听</span>')
        if kans.get(str(s)):
            badges.append(f'<span class="badge">杠×{kans[str(s)]}</span>')
        if dead.get(str(s)):
            badges.append('<span class="badge">已弃胡</span>')
        tag = names.get(no)
        who = f"{no} 号" + (f"（{tag}）" if tag else "")
        parts.append(f'<div class="seat"><div class="who">{who}{"".join(badges)}</div>')
        parts.append(f'<div class="hand">{hand_html(counts, s, hands_format)}</div>')
        parts.append(f'<div class="river">面子 {melds_html(fixed.get(str(s)) or [])}</div>')
        parts.append(f'<div class="river">河 {river_html(discards.get(str(s)) or [])}</div>')
        parts.append("</div>")

    parts.append("</div>")

    settle = (gs.get("result") or {}).get("settle")
    if settle:
        fans = "、".join(f["label"] for f in settle.get("fans") or []) or "无番"
        parts.append('<div class="settle"><h2>结算</h2>'
                     f'<div>{html.escape(settle.get("text", ""))}</div>'
                     f'<div class="meta">番种：{html.escape(fans)}</div></div>')

    parts.append("</div>")
    return f"""<!doctype html><html lang="zh"><head><meta charset="utf-8"><title>麻将桌</title>
<style>{CSS}</style></head><body>{''.join(parts)}</body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(HERE / "桌面.html"))
    ap.add_argument("--state", default=str(STATE_PATH))
    ap.add_argument("--seat", type=int, default=None, help="从几号视角排（默认按存档里的你）")
    ap.add_argument("--watch", type=float, default=0.0, help="每隔几秒重画一次")
    args = ap.parse_args()

    while True:
        try:
            state = json.loads(Path(args.state).read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            print(f"读不到存档：{args.state}")
            return 1
        me = args.seat
        if me is None and state.get("meta", {}).get("you") is not None:
            me = int(state["meta"]["you"])
        Path(args.out).write_text(render(state, me_no=me), encoding="utf-8")
        print(f"写好 {args.out}")
        if args.watch <= 0:
            return 0
        time.sleep(args.watch)


if __name__ == "__main__":
    raise SystemExit(main())
