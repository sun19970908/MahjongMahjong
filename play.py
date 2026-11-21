#!/usr/bin/env python
"""终端麻将桌（TUI）：只负责画画面和读键盘。

牌局怎么走、谁该出手、怎么存档、怎么回退、怎么轮庄，全在 core.TableCore；
这个文件只把 `TableCore` 的 Decision 画成终端画面，把你的编号翻译成一个 Action。
"""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import CoreConfig, TableCore, action_label
from game import Action, Game
import seats as seats_mod

NUM = "一二三四五六七八九"
ESC = "\x1b["
BORDER, WAN, PIN, SUO, ZHONG, DIM, MINE = 90, 31, 34, 32, 91, 37, 36
COLOR = True
VT_OK = True


def enable_vt() -> bool:
    if os.name != "nt":
        return True
    try:
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not k.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        return bool(k.SetConsoleMode(h, mode.value | 0x0004))
    except Exception:
        return False


def paint(s: str, code: int) -> str:
    return f"{ESC}{code}m{s}{ESC}0m" if COLOR and code else s


def face(tid: str) -> tuple[str, int]:
    if tid == "z5":
        return " 中 ", ZHONG
    suit, n = tid[0], int(tid[1])
    ch = NUM[n - 1]
    return ch + {"m": "萬", "p": "餅", "s": "條"}[suit], \
        {"m": WAN, "p": PIN, "s": SUO}[suit]


def tile3(tid: str) -> tuple[str, str, str]:
    f, c = face(tid)
    return (paint("┌────┐", BORDER),
            paint("│", BORDER) + paint(f, c) + paint("│", BORDER),
            paint("└────┘", BORDER))


def hand_block(hand: list[str]) -> list[str]:
    rows = ["", "", ""]
    for t in hand:
        for i in range(3):
            rows[i] += tile3(t)[i]
    rows.append("".join(f"{i + 1:^6}" for i in range(len(hand))))
    return rows


def mini(hand: list[str]) -> str:
    return " ".join(paint(face(t)[0], face(t)[1]) for t in hand)


RULE = paint("─" * 78, BORDER)


def describe(a: Action) -> str:
    return action_label(a.to_dict()).replace("打 ", "打").replace("碰 ", "碰") \
        .replace("杠 ", "杠").replace("加杠 ", "加杠").replace("吃 ", "吃")


def settle_block(result: dict | None) -> list[str]:
    s = (result or {}).get("settle") or {}
    if not s:
        return []
    out = [RULE, " " + paint("结算　", DIM) + paint(s.get("text", ""), MINE)]
    fans = s.get("fans") or []
    if fans:
        line = "  " + "  ".join(
            f"{f['label']}" + (f"×{f['fan']}" if f.get("fan", 1) > 1 else "") for f in fans)
        out.append(paint(line, DIM))
    payers = s.get("payers") or {}
    payer_fans = s.get("payer_fans") or {}
    if payers:
        items = []
        for k, v in payers.items():
            own = payer_fans.get(k) or []
            tag = f"（{'、'.join(f['label'] for f in own)}）" if own else ""
            items.append(f"{seats_mod.label(int(k))}号{tag} 付 {v} 分")
        out.append("  " + "  ".join(items))
    return out


def screen(game: Game, me: int, buddy: int, status: str, log: list[str],
           pick: list[Action] | None = None, say: str = "",
           names: dict[int, str] | None = None,
           pick_kind: str | None = None) -> str:
    v = game.observe(me)
    out = [RULE]
    wall = f" 牌墙 {v['wall_left']:>3} 张"
    who_dealer = paint(f"庄 {seats_mod.label(game.dealer)}号·{seats_mod.wind(game.dealer, game.dealer)}", MINE)
    if game.dice is not None:
        who_dealer += paint(f" 骰 {game.dice.a}+{game.dice.b}={game.dice.total}", DIM)
    out.append(wall.ljust(20) + who_dealer)
    out.append(" " + paint(status, MINE))
    if v.get("wall_last"):
        block = tile3(v["wall_last"])
        out.append(" " + paint("明牌 ", DIM) + block[0])
        out.append(" " * 6 + block[1])
        out.append(" " * 6 + block[2])
    if say:
        for line in say.splitlines():
            out.append(" " + paint(line, DIM))
    out.append(RULE)

    order = [i for i in range(game.cfg.seats) if i != me] + [me]
    for s in order:
        label = seats_mod.label(s)
        who = (names or {}).get(s) or f"{label}号"
        who += f"·{seats_mod.wind(s, game.dealer)}"
        fixed = (v.get("melds") or {}).get(s, [])
        hand_n = v["hand_sizes"].get(s, "?")
        head = f" {who}  {hand_n} 张"
        if s == game.dealer:
            head += paint(" ◆庄", MINE)
        if v.get("ting", {}).get(s):
            head += paint(" ●报听", MINE)
        if v.get("piao", {}).get(s):
            head += paint(" ●飘", MINE)
        if v.get("ting_closed", {}).get(s):
            head += paint("·闭门", DIM)
        if (game.kans or {}).get(s):
            head += paint(f" ●杠×{game.kans[s]}", DIM)
        if v.get("dead", {}).get(s):
            head += paint(" ✕已弃胡", DIM)
        if fixed:
            head += "   面子 " + "  ".join(mini(m) for m in fixed)
        elif s != me:
            head += "   面子 " + paint("（无）", DIM)
        out.append(head)
        river = v["discards"].get(s, [])
        out.append("    河 " + (mini(river) if river else paint("（空）", DIM)))

    if log:
        out.append(RULE)
        for line in log[-3:]:
            out.append(" " + line)

    out.append(RULE)
    if v.get("last"):
        last = v["last"]
        out.append(f" 上一张：{seats_mod.label(last['seat'])}号 打 "
                   f"{paint(face(last['tile'])[0].strip(), face(last['tile'])[1])}")

    out += settle_block(game.result)

    out.append("")
    out.append(f" 你的手牌（{len(v['hand'])} 张）")
    out += [" " + row for row in hand_block(v["hand"])]
    if pick:
        if pick_kind == "discard" and any(a.kind == "discard" for a in pick):
            hand = list(v.get("hand") or [])
            by_tile = {a.tile: a for a in pick if a.kind == "discard"}
            opts = [f"{i}.{describe(by_tile[t])}"
                    for i, t in enumerate(hand, 1) if t in by_tile]
            start = len(hand) + 1
            for i, a in enumerate((a for a in pick if a.kind != "discard"), start):
                opts.append(f"{i}.{describe(a)}")
        else:
            opts = [f"{i}.{describe(a)}" for i, a in enumerate(pick, 1)]
        line = " " + "  ".join(opts)
        while len(line) > 100:
            cut = line.rfind("  ", 0, 100)
            out.append(line[:cut])
            line = "   " + line[cut:].strip()
        out.append(line)
    out.append("")
    return "\n".join(out)


def draw_screen(text: str) -> None:
    if sys.stdout.isatty():
        if VT_OK:
            print(f"{ESC}H{ESC}2J", end="")
        else:
            os.system("cls" if os.name == "nt" else "clear")
    print(text, flush=True)


class TuiUi:
    """键盘这一侧：画面与输入都在这里，不碰牌局规则。"""

    def __init__(self, *, seat: int = 1, no_chat: bool = False) -> None:
        self.seat = seat
        self.no_chat = no_chat
        self.core: TableCore | None = None

    def bind(self, core: TableCore) -> None:
        self.core = core

    def said_text(self) -> str:
        c = self.core
        if not c:
            return ""
        names = c.names
        return "\n".join(f"{names.get(s, '')}：{t}" for s, t in sorted(c.said.items()) if t.strip())

    def status(self, core: TableCore, d) -> str:
        p = core.table.at(d.seat) if core.table else None
        if getattr(core.game, "hai", False):
            return "最后四张：抓了不打，能胡就胡（海底捞月），不能胡就过（/p 暂停）"
        if d.kind == "ting_declare" and p and p.supports == "human":
            return "你听牌了，报不报？（/p 暂停）"
        if d.kind == "draw" and any(a.kind == "bloom" for a in d.legal) and p and p.supports == "human":
            return "杠后补张：能后开花就输 2，正常摸牌输 1（/p 暂停）"
        if p and p.supports == "human":
            return "轮到你了（/p 暂停）"
        if p and p.supports == "external":
            return f"{p.name} 接入中……"
        if p and p.supports == "llm":
            return f"{p.name} 在算……"
        return p.name if p else ""

    def draw(self, core: TableCore, d=None, status: str | None = None,
             pick: list[Action] | None = None) -> None:
        if core.game is None:
            return
        st = status or (self.status(core, d) if d else "牌桌")
        st += f" · 第{core.round}局 · 第{core.lap}圈 · LLM上下文 {core.context_round}/{core.rounds}"
        draw_screen(screen(core.game, core.me, core.buddy, st, core.log,
                           pick, self.said_text(), core.names,
                           d.kind if d else None))

    def decide(self, core: TableCore, d):
        self.draw(core, d, pick=d.legal)
        while True:
            try:
                raw = input(" > ").strip()
            except EOFError:
                raise SystemExit("\n退了。")
            if raw in ("q", "quit"):
                raise SystemExit("退了。")
            if raw in ("/p", "p", "暂停"):
                return None, ""
            if not raw.isdigit():
                print(" 输入编号，或 /p")
                continue
            n = int(raw)
            opts = d.legal
            has_discard = any(a.kind == "discard" for a in opts)
            if d.kind == "discard" and has_discard:
                hand = list((d.view or {}).get("hand") or [])
                if 1 <= n <= len(hand):
                    tid = hand[n - 1]
                    for a in opts:
                        if a.kind == "discard" and a.tile == tid:
                            return a, ""
                    print(f" 第 {n} 张现在不能打（报听后只能打摸进来的那张）")
                    continue
                others = [a for a in opts if a.kind != "discard"]
                j = n - len(hand) - 1
                if 0 <= j < len(others):
                    return others[j], ""
                print(f" 没有第 {n} 号选项")
                continue
            if 1 <= n <= len(opts):
                return opts[n - 1], ""
            print(f" 没有第 {n} 号选项")

    def pause(self, core: TableCore, actor: str, opener: str) -> None:
        print()
        if opener:
            print(f" {actor}：{opener}")
        if self.no_chat:
            print(paint(" （跳过中场聊天）", DIM))
            return
        if not core.talker:
            print(paint(" （这桌没有能说话的座位，直接接着打）", DIM))
            return
        print(paint(" （中场暂停：想聊就打字，空行回车接着打）", DIM))
        while True:
            try:
                line = input(" 你：").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                break
            r = core.chat(line, phase="pause")
            print(f" {core.names.get(core.buddy, '')}：{r.get('reply') or ''}", flush=True)

    def round_end(self, core: TableCore, info: dict) -> bool:
        msg = ("提前停止：这一场到这儿（当前这局没结算）" if info.get("ended_early")
               else "荒庄：牌墙摸空" if (info.get("result") or {}).get("type") == "draw_game"
               else (info.get("result") or {}).get("settle", {}).get("text")
               or f"这一局打到第 {info.get('steps')} 步")
        self.draw(core, status=str(msg))
        if info.get("broadcast"):
            print("\n 播报：" + info["broadcast"], flush=True)
        if info.get("auto_reply"):
            print(f" {core.names.get(core.buddy, '')}：{info['auto_reply']}", flush=True)
        if self.no_chat:
            return not info.get("last") and not info.get("ended_early")
        last = bool(info.get("last") or info.get("ended_early"))
        if not core.talker:
            return not last
        notes_end = bool(info.get("context_cut_next"))
        if last:
            print("\n 打完了。想跟牌友聊两句就直接打字，空行回车收场。", flush=True)
        else:
            print("\n 这一局打完了，下一局还没开。想聊两句就直接打字，空行回车下一局；"
                  "想改长期笔记就先打 /e 提前结束上下文（下一局清空），然后再聊。", flush=True)
        while True:
            try:
                line = input(" 你：").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                break
            if line in ("q", "quit", "退出"):
                raise SystemExit("退了。")
            if not last and line in ("/e", "提前结束上下文"):
                r = core.context_cut_request()
                print(" " + (r.get("message") or "提前结束上下文。"), flush=True)
                notes_end = True
                continue
            r = core.chat(
                line,
                phase=("after" if (notes_end or last) else "between"),
            )
            print(f" {core.names.get(core.buddy, '')}：{r.get('reply') or ''}", flush=True)
        return not last

    def tick(self, core: TableCore, seconds: float) -> None:
        self.draw(core, status=f"接入中…… 已等 {seconds:.0f}s（不接就继续等）")

    def cancel(self, core: TableCore) -> None:
        return


def main() -> int:
    global COLOR, VT_OK
    ap = argparse.ArgumentParser()
    ap.add_argument("--seat", type=int, default=1, help="你坐几号（1 起）")
    ap.add_argument("--buddy", type=int, default=None, help="牌友坐几号，默认你下家")
    ap.add_argument("--agent", type=int, default=None, help="外部接入的座位（我坐这）")
    ap.add_argument("--players", default="",
                    help='整桌人一次说清："1=human,2=llm,3=external,4=random"')
    ap.add_argument("--dealer", type=int, default=None)
    ap.add_argument("--base", type=int, default=1)
    ap.add_argument("--fan-cap", type=int, default=8)
    ap.add_argument("--agent-wait", type=float, default=0.0)
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--rounds", type=int, default=1,
                    help="LLM 上下文窗口：每几局清空一次（不是牌局总局数）")
    ap.add_argument("--steps", type=int, default=0)
    ap.add_argument("--no-prefetch", action="store_true")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--cache", action="store_true")
    ap.add_argument("--trace", action="store_true")
    ap.add_argument("--new", action="store_true", help="新开一局；默认接 state.json")
    ap.add_argument("--no-chat", action="store_true")
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args()

    VT_OK = enable_vt()
    COLOR = not args.no_color and VT_OK and sys.stdout.isatty()

    cfg = CoreConfig(
        seat=args.seat, buddy=args.buddy, agent=args.agent, players=args.players,
        dealer=args.dealer, base=args.base, fan_cap=args.fan_cap, seed=args.seed,
        rounds=args.rounds, steps=args.steps, demo=args.demo, offline=args.offline,
        no_chat=args.no_chat, no_save=args.no_save, no_prefetch=args.no_prefetch,
        trace=args.trace, cache=args.cache, agent_wait=args.agent_wait,
    )
    ui = TuiUi(seat=args.seat, no_chat=args.no_chat)
    core = TableCore(cfg, ui)
    ui.bind(core)
    resume = not args.new
    if resume:
        print(" 接上次存档……")
    try:
        core.run_sync(resume=resume)
    except RuntimeError as exc:
        if resume and "没有存档" in str(exc):
            print(" 没有存档，用 --new 新开一局")
            return 1
        raise
    print(f"\n 牌友出牌 {core.summary()['llm_records']} 次，兜底 "
          f"{core.summary()['llm_fallback']} 次")
    if args.cache:
        print(core.summary()["cache_report"])
    if core.summary()["error"]:
        print(" 内核：", core.summary()["error"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
