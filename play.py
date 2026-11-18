#!/usr/bin/env python
"""终端麻将桌（TUI）。

    python play.py              你坐 0 号，牌友坐 1 号
    python play.py --seat 1     你坐 1 号
    python play.py --demo       牌友自己打，你在旁边看
    python play.py --offline    三家全随机，不调模型（只验画面）

画面是 ANSI 画的，Windows 10+ 的终端都能上色；上不了色会自动退回黑白。
"""

from __future__ import annotations

import argparse
import ctypes
import os
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from game import Action, Game, GameConfig
from llm_player import LLMPlayer, StrataClient
from players import ExternalPlayer, HumanPlayer, LLMSeat, RandomPlayer, Tracer
from referee import tile_id
import state

NUM = "一二三四五六七八九"
SEAT = {0: "0号", 1: "1号", 2: "2号", 3: "3号"}
TRACE_PATH = Path(__file__).resolve().parent / "trace.jsonl"
STATE_PATH = Path(__file__).resolve().parent / "state.json"

ESC = "\x1b["
BORDER, WAN, PIN, SUO, ZHONG, DIM, MINE = 90, 31, 34, 32, 91, 37, 36
COLOR = True
VT_OK = True


def enable_vt() -> bool:
    """Windows 下让终端认 ANSI。失败就退回黑白。"""
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
    """牌面：4 个显示列 + 颜色。"""
    if tid == "z5":
        return " 中 ", ZHONG
    suit, n = tid[0], int(tid[1])
    ch = NUM[n - 1]
    return ch + {"m": "萬", "p": "餅", "s": "條"}[suit], \
        {"m": WAN, "p": PIN, "s": SUO}[suit]


def tile3(tid: str) -> tuple[str, str, str]:
    """一张竖着的牌：三行、六列宽。"""
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


def screen(game: Game, me: int, buddy: int, status: str, log: list[str],
           pick: list[Action] | None = None, say: str = "",
           names: dict[int, str] | None = None) -> str:
    v = game.observe(me)
    out = [RULE]
    wall = f" 牌墙 {v['wall_left']:>3} 张"
    if v.get("wall_last"):
        wall += f" · 明牌 {v['wall_last']}"
    out.append(wall.ljust(20) + paint(status, MINE))
    if say:
        out.append(" " + paint(say, DIM))
    out.append(RULE)

    order = [i for i in range(game.cfg.seats) if i != me] + [me]
    for s in order:
        who = (names or {}).get(s) or (
            f"{SEAT[s]}" + ("（牌友）" if s == buddy else "（你）" if s == me else "（随机）"))
        fixed = (v.get("melds") or {}).get(s, [])
        hand_n = v["hand_sizes"].get(s, "?")
        head = f" {who}  {hand_n} 张"
        if v.get("ting", {}).get(s):
            head += paint(" ●报听", MINE)
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
        out.append(f" 上一张：{SEAT[last['seat']]} 打 {paint(face(last['tile'])[0].strip(), face(last['tile'])[1])}")

    out.append("")
    out.append(f" 你的手牌（{len(v['hand'])} 张）")
    out += [" " + row for row in hand_block(v["hand"])]
    if pick:
        opts = []
        for i, a in enumerate(pick, 1):
            opts.append(f"{i}.{describe(a)}")
        line = " " + "  ".join(opts)
        while len(line) > 100:
            cut = line.rfind("  ", 0, 100)
            out.append(line[:cut])
            line = "   " + line[cut:].strip()
        out.append(line)
    out.append("")
    return "\n".join(out)


def describe(a: Action) -> str:
    if a.kind == "discard":
        return f"打{face(tile_id(a.tile))[0].strip()}"
    if a.kind == "pon":
        return f"碰{face(tile_id(a.tile))[0].strip()}"
    if a.kind == "kan":
        return f"杠{face(tile_id(a.tile))[0].strip()}"
    if a.kind == "chi":
        return "吃" + "+".join(face(tile_id(t))[0].strip() for t in a.combo)
    return {"win": "胡！", "pass": "过", "draw": "摸牌", "ting": "报听"}.get(a.kind, a.kind)


def draw_screen(text: str) -> None:
    if sys.stdout.isatty():
        if VT_OK:
            print(f"{ESC}H{ESC}2J", end="")
        else:
            os.system("cls" if os.name == "nt" else "clear")
    print(text, flush=True)


def apply_and_note(game: Game, act: Action, log: list[str], me: int) -> None:
    for e in game.apply(act):
        t = e.type
        name = paint(face(tile_id(e.tile))[0].strip(), face(tile_id(e.tile))[1]) if e.tile is not None else ""
        if t == "draw" and e.seat == me:
            log.append(f"你摸到 {name}")
        elif t == "kan_draw" and e.seat == me:
            log.append(f"你补张 {name}")
        elif t == "discard":
            log.append(f"{SEAT[e.seat]} 打 {name}")
        elif t == "pon":
            log.append(f"{SEAT[e.seat]} 碰 {name}")
        elif t == "chi":
            log.append(f"{SEAT[e.seat]} 吃 " + "+".join(face(x)[0].strip() for x in e.data["use"]))
        elif t == "kan":
            label = {"in_hand": "明杠", "added": "加杠"}.get((e.data or {}).get("kind"), "杠")
            log.append(f"{SEAT[e.seat]} {label} {name}")
        elif t == "ting":
            waits = "、".join(face(t)[0].strip() for t in (e.data or {}).get("waits", []))
            log.append(f"{SEAT[e.seat]} 报听" + (f"（等 {waits}）" if waits else ""))
        elif t == "win":
            tag = "抢杠" if (e.data or {}).get("rob") else ""
            log.append(f"*** {SEAT[e.seat]} {tag}胡了！")
        elif t == "draw_game":
            log.append("*** 荒庄：牌墙摸空")
    del log[:-6]


def pause_talk(talker, buddy: int, opener: str, actor: str = "") -> None:
    """中场暂停：先说一句，然后你一句句回，空行回车就接着打。"""
    print()
    if opener:
        print(f" {actor or SEAT[buddy]}：{opener}")
    if not talker:
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
        print(f" {SEAT[buddy]}：{talker.chat(line, phase='pause')}", flush=True)
    talker.impl.set_phase("play")


def main() -> int:
    global COLOR, VT_OK
    ap = argparse.ArgumentParser()
    ap.add_argument("--seat", type=int, default=0, help="你坐几号")
    ap.add_argument("--buddy", type=int, default=None, help="牌友坐几号，默认你下家")
    ap.add_argument("--agent", type=int, default=None, help="外部接入的座位（我坐这），默认不开")
    ap.add_argument("--agent-wait", type=float, default=0.0, help="外部座位最多等几秒，0=死等")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--steps", type=int, default=0)
    ap.add_argument("--no-prefetch", action="store_true")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--cache", action="store_true", help="每次请求后打一行缓存命中率，结束给总账")
    ap.add_argument("--trace", action="store_true", help="每步一行 JSONL，事后复盘")
    ap.add_argument("--resume", action="store_true", help="接上次那一手接着打（读 state.json）")
    ap.add_argument("--no-save", action="store_true", help="不写 state.json")
    args = ap.parse_args()

    VT_OK = enable_vt()
    COLOR = not args.no_color and VT_OK and sys.stdout.isatty()

    saved = state.load(STATE_PATH) if args.resume else None
    if args.resume and saved is None:
        raise SystemExit(f"没有存档：{STATE_PATH}")
    if saved:
        game, meta = saved
        me, buddy = int(meta["you"]), int(meta["buddy"])
        agent = None if meta.get("agent") is None else int(meta["agent"])
        if (args.seat, args.buddy, args.agent) != (0, None, None):
            print(" 接管存档：座位按存档里的来，--seat/--buddy/--agent 这轮不算")
    else:
        me = 0 if args.demo else args.seat
        buddy = args.buddy if args.buddy is not None else (me + 1) % 4
        agent = args.agent
        game, meta = None, {}
    seats = [me, buddy] + ([agent] if agent is not None else [])
    if len(set(seats)) != len(seats):
        raise SystemExit("座位撞了：--seat / --buddy / --agent 不能是同一个")
    rng = random.Random(args.seed)

    # ---- 座位：谁坐哪儿，全看这里 ----
    players: dict[int, object] = {}
    for s in range(4):
        if s == agent:
            players[s] = ExternalPlayer(s, wait=args.agent_wait)
        elif args.offline:
            players[s] = RandomPlayer(s, random.Random(rng.randint(0, 9999)))
        elif s == me and not args.demo:
            players[s] = HumanPlayer(s)
        elif s == buddy:
            impl = LLMPlayer(StrataClient(), s, debug_cache=args.cache)
            players[s] = LLMSeat(s, impl)
        else:
            players[s] = RandomPlayer(s, random.Random(rng.randint(0, 9999)))
    talker = next((p for p in players.values() if p.supports_talk()), None)
    names = {s: p.name for s, p in players.items()}
    llm = players.get(buddy) if buddy in players and getattr(players[buddy], "supports", "") == "llm" else None
    if llm and not saved:
        llm.impl.reset_temp_notes()        # 临时笔记只管这一局

    tracer = Tracer(TRACE_PATH) if args.trace else None

    if saved:
        log: list[str] = list(meta.get("log") or [])
        say = str(meta.get("say") or "")
        steps = int(meta.get("steps") or 0)
        if llm and meta.get("llm"):
            llm.impl.messages = list(meta["llm"].get("messages") or llm.impl.messages)
            llm.impl._pause_calls = int(meta["llm"].get("pause_calls") or 0)
        print(f" 接上第 {steps} 步：{meta.get('phase', '')}，牌墙 {len(game.wall)} 张")
    else:
        game = Game(GameConfig(seats=4, seed=args.seed))
        game.start()
        log = []
        say = ""
        steps = 0

    def persist() -> None:
        if args.no_save:
            return
        state.save(STATE_PATH, game, {
            "you": me, "buddy": buddy, "agent": agent,
            "steps": steps, "say": say, "log": log[-30:], "phase": game.phase,
            "llm": None if not llm else {"messages": llm.impl.messages,
                                         "pause_calls": llm.impl._pause_calls},
        })
    while game.pending() is not None:
        d = game.pending()
        if d.kind == "draw":
            apply_and_note(game, Action("draw"), log, me)
            steps += 1
            persist()
            continue

        p = players[d.seat]
        # 轮到我出手时，让牌友先把自由思考跑起来（别人回合的空档不算浪费）
        if d.seat == me and llm and not args.no_prefetch:
            llm.impl.prefetch_async(game.observe(buddy))

        status = ("你听牌了，报不报？（/p 中场暂停）" if d.kind == "ting_declare" and p.supports == "human"
                  else "轮到你了（/p 中场暂停）" if p.supports == "human"
                  else f"{p.name} 等待接入，牌局停在这一手" if p.supports == "external"
                  else f"{p.name} 在算……" if p.supports == "llm" else p.name)
        draw_screen(screen(game, me, buddy, status, log, d.legal if p.supports == "human" else None,
                           say=f"{p.name}：{say}" if say else "", names=names))

        t0 = time.time()
        act, speech = p.decide(d)
        took = time.time() - t0

        if act is None:                       # 有人要求中场暂停：这一手先不打
            draw_screen(screen(game, me, buddy, "牌先停着", log, say=f"{p.name}：{speech}", names=names))
            opener = speech
            actor = p.name
            if p.supports == "human" and talker:   # 我按的暂停：让牌友先说一句
                opener = talker.chat("（我按了暂停，先停一下，想跟你说两句）", phase="pause") or ""
                actor = names.get(buddy, "")
            pause_talk(talker, buddy, opener, actor)
            continue

        say = speech
        log.append(f"{SEAT[d.seat]} 出手 {paint(f'[{took:.1f}s]', DIM)}")
        if tracer:
            row = {"seat": d.seat, "name": p.name, "decision": d.kind, "action": act.to_dict(),
                   "seconds": round(took, 2), "say": speech[:60]}
            if llm and d.seat == llm.seat and llm.impl.cache_stats:
                st = llm.impl.cache_stats[-1]
                row.update(prompt=st["prompt"], cached=st["cached"],
                           read=max(0, st["prompt"] - st["cached"]))
            tracer.log(**row)

        apply_and_note(game, act, log, me)
        steps += 1
        persist()
        if args.steps and steps >= args.steps:
            draw_screen(screen(game, me, buddy, f"走到第 {steps} 步打住", log, say=say, names=names))
            break

    else:
        res = game.result or {}
        msg = "荒庄：牌墙摸空" if res.get("type") == "draw_game" else str(res)
        draw_screen(screen(game, me, buddy, msg, log, say=say, names=names))

    if llm:
        print(f" 牌友出牌 {len(llm.impl.records)} 次，兜底 "
              f"{sum(1 for r in llm.impl.records if r.used_fallback)} 次")
        if args.cache:
            print(llm.impl.cache_report())

    # 牌局散了才开聊：接在同一段历史后面，它记得这局怎么打的
    if talker and game.result:
        print("\n 牌局散了。想跟牌友聊两句就直接打字，空行回车结束。")
        print(" （它也能自己改笔记，跟它说“把笔记加一条……”就行；改完下一局生效）")
        while True:
            try:
                line = input(" 你：").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                break
            print(f" {SEAT[buddy]}：{talker.chat(line, phase='after')}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
