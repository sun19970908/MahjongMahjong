#!/usr/bin/env python
"""终端麻将桌（TUI）。

    python play.py                         你坐 1 号，牌友坐 2 号
    python play.py --seat 2 --buddy 3      你坐 2 号，牌友坐 3 号
    python play.py --players "1=human,2=llm,3=external,4=random"
    python play.py --demo                  牌友自己打，你在旁边看
    python play.py --offline               四家全随机，不调模型（只验画面）

座位号从 1 起；画面、存档、seats 目录、命令行全用座位号，引擎内部依旧是 0 基下标，
换算只在 seats.label() / seats.index() 这两处发生。
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
from players import ExternalPlayer, HumanPlayer, LLMSeat, RandomPlayer, SeatPlan, Tracer, EndEarly
from players import build_players, parse_players, plan_from_flags
from referee import parse_tile, tile_id
import seats as seats_mod
import state

NUM = "一二三四五六七八九"
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
           names: dict[int, str] | None = None,
           pick_kind: str | None = None) -> str:
    v = game.observe(me)
    out = [RULE]
    wall = f" 牌墙 {v['wall_left']:>3} 张"
    who_dealer = paint(f"庄 {seats_mod.label(game.dealer)}号", MINE)
    if game.dice is not None:
        who_dealer += paint(f" 骰 {game.dice.a}+{game.dice.b}={game.dice.total}", DIM)
    out.append(wall.ljust(20) + who_dealer)
    out.append(" " + paint(status, MINE))
    if v.get("wall_last"):
        # 牌墙最后一张是明牌：画成一张麻将摆在这儿，谁都看得见
        block = tile3(v["wall_last"])
        out.append(" " + paint("明牌 ", DIM) + block[0])
        out.append(" " * 6 + block[1])
        out.append(" " * 6 + block[2])
    if say:
        for line in say.splitlines():          # 每家的最后一句话都摆出来，空的多行也照走
            out.append(" " + paint(line, DIM))
    out.append(RULE)

    order = [i for i in range(game.cfg.seats) if i != me] + [me]
    for s in order:
        label = seats_mod.label(s)
        who = (names or {}).get(s) or f"{label}号"
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
            # 出牌阶段：序号永远是手牌位置。即使同时有杠/胡，也别退回动作顺序，
            # 不然 p4、s6 这种重复牌会被压成一个，九条就从第 11 位变成第 9 位。
            hand = list(v.get("hand") or [])
            by_tile = {parse_tile(a.tile): a for a in pick if a.kind == "discard"}
            opts = [f"{i}.{describe(by_tile[parse_tile(t)])}"
                    for i, t in enumerate(hand, 1) if parse_tile(t) in by_tile]
            start = len(hand) + 1
            for i, a in enumerate((a for a in pick if a.kind != "discard"), start):
                opts.append(f"{i}.{describe(a)}")
        else:
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
        label = "加杠" if (a.meta or {}).get("kind") == "added" else "杠"
        return f"{label}{face(tile_id(a.tile))[0].strip()}"
    if a.kind == "chi":
        return "吃" + "+".join(face(tile_id(t))[0].strip() for t in a.combo)
    return {"win": "胡！", "bloom": "后开花！", "pass": "过", "draw": "摸牌", "ting": "报听"}.get(a.kind, a.kind)


def settle_block(result: dict | None) -> list[str]:
    """收局后把番种摆出来：几番、多少分、谁付。"""
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


def round_broadcast(rnd: int, rounds: int, res: dict, board: dict[int, int],
                    buddy: int | None, ended_early: bool = False) -> str:
    """每局收场给牌友的播报：谁胡的、怎么胡的、怎么结算、累计多少。

    播报是发给牌友本人看的，座位一律用「几号」，别用桌上那套「你/牌友」的
    显示名，不然 1 号胡了会写成「1号（你）胡」，牌友真以为自己胡了。
    """
    head = f"【第{rnd}/{rounds}局播报】"
    if ended_early:
        head += "提前结束，这一局没结算。"
    elif not res:
        head += "这一局没有结果。"
    elif res.get("type") == "draw_game":
        head += "荒庄，牌墙摸空。"
    else:
        seat = res.get("seat")
        who = f"{seats_mod.label(seat)}号"
        tile = face(res.get("tile"))[0].strip() if res.get("tile") else "?"
        if res.get("zimo"):
            how = "自摸"
        elif res.get("rob"):
            how = f"抢杠，{seats_mod.label(res.get('from'))}号"
        else:
            how = f"{seats_mod.label(res.get('from'))}号点炮"
        head += f"{who} 胡 {tile}（{how}）。"
        hand = res.get("hand") or []
        if hand:
            head += "赢家牌：" + " ".join(face(t)[0].strip() for t in hand) + "。"
        melds = res.get("melds") or []
        if melds:
            head += "副露：" + "；".join(
                " ".join(face(t)[0].strip() for t in m.get("tiles") or ())
                for m in melds) + "。"
    settle = res.get("settle") or {}
    if settle.get("text"):
        head += f"结算：{settle['text']}。"
    if board:
        head += "累计：" + "，".join(
            f"{seats_mod.label(s)}号 {board[s]:+d}"
            for s in range(len(board)))
        if buddy is not None:
            head += f"；你 {board.get(buddy, 0):+d}。"
    return head


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
            log.append(f"{seats_mod.label(e.seat)}号 打 {name}")
        elif t == "pon":
            log.append(f"{seats_mod.label(e.seat)}号 碰 {name}")
        elif t == "chi":
            log.append(f"{seats_mod.label(e.seat)}号 吃 " + "+".join(face(x)[0].strip() for x in e.data["use"]))
        elif t == "kan":
            label = {"in_hand": "明杠", "added": "加杠"}.get((e.data or {}).get("kind"), "杠")
            log.append(f"{seats_mod.label(e.seat)}号 {label} {name}")
        elif t == "ting":
            if e.seat == me:
                waits = "、".join(face(t)[0].strip() for t in (e.data or {}).get("waits", []))
                log.append("你 报听" + (f"（等 {waits}）" if waits else ""))
            else:
                log.append(f"{seats_mod.label(e.seat)}号 报听")
        elif t == "bloom":
            log.append(f"{seats_mod.label(e.seat)}号 后开花 {name}")
        elif t == "dead":
            log.append(f"{seats_mod.label(e.seat)}号 放过胡，本局不准再胡")
        elif t == "win":
            tag = "抢杠" if (e.data or {}).get("rob") else ("后开花" if (e.data or {}).get("bloom") else "")
            log.append(f"*** {seats_mod.label(e.seat)}号 {tag}胡了！")
        elif t == "draw_game":
            log.append("*** 荒庄：牌墙摸空")
    del log[:-6]


def pause_talk(talker, buddy: str, opener: str, actor: str = "") -> None:
    """中场暂停：先说一句，然后你一句句回，空行回车就接着打。"""
    print()
    if opener:
        print(f" {actor or buddy}：{opener}")
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
        print(f" {buddy}：{talker.chat(line, phase='pause')}", flush=True)
    talker.impl.set_phase("play")


def main() -> int:
    global COLOR, VT_OK
    ap = argparse.ArgumentParser()
    ap.add_argument("--seat", type=int, default=1, help="你坐几号（1 起）")
    ap.add_argument("--buddy", type=int, default=None, help="牌友坐几号，默认你下家")
    ap.add_argument("--agent", type=int, default=None, help="外部接入的座位（我坐这），默认不开")
    ap.add_argument("--players", default=None,
                    help='整桌人一次说清："1=human,2=llm,3=external,4=random" 或 "human,llm,external,random"')
    ap.add_argument("--dealer", type=int, default=None, help="庄家坐几号；不给就掷骰子定")
    ap.add_argument("--base", type=int, default=1, help="结算底分（默认 1）")
    ap.add_argument("--fan-cap", type=int, default=8, help="封顶番数（默认 8）")
    ap.add_argument("--agent-wait", type=float, default=0.0, help="外部座位最多等几秒，0=死等")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--rounds", type=int, default=1, help="连打几局（打完结分、庄家轮转）")
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

        def old_seat(v):
            """老存档是 0 基座位：0 号=你、1 号=牌友、2 号=接入位，读进来先抬成 1 基。"""
            return None if v is None else (int(v) + 1 if int(v) < seats_mod.FIRST_SEAT else int(v))

        you_no = old_seat(meta.get("you"))
        buddy_no = old_seat(meta.get("buddy"))
        agent_no = old_seat(meta.get("agent"))
        plans = plan_from_flags(seats=game.cfg.seats, human=you_no,
                                buddy=buddy_no, agent=agent_no)
        if any((args.seat != 1, args.buddy, args.agent, args.players)):
            print(" 接管存档：座位按存档里的来，--seat/--buddy/--agent 这轮不算")
    else:
        game = None
        if args.players:
            plans = parse_players(args.players, 4)
        else:
            human = None if args.demo else args.seat
            buddy_no = args.buddy if args.buddy is not None else (human % 4 + 1)
            plans = plan_from_flags(seats=4, human=human, buddy=buddy_no, agent=args.agent)

    if len({p.seat for p in plans}) != len(plans):
        raise SystemExit("座位号撞了，一个人只能坐一张椅子")

    n_seats = len(plans)

    # ---- 座位：谁坐哪儿全由计划说，这里只负责把牌友那只椅子造出来 ----
    if args.offline:
        plans = [SeatPlan(p.seat, "random") for p in plans]
    rng = random.Random(args.seed)

    def make_llm(plan: SeatPlan):
        impl = LLMPlayer(StrataClient(), plan.seat, debug_cache=args.cache)
        return LLMSeat(plan.seat, impl)

    table = build_players(plans, seats=n_seats, llm_factory=make_llm, rng=rng)
    talker = table.talker()
    names = table.names()                              # 下标 → 显示名
    llm = next(iter(table.of_kind("llm")), None)
    me = next((i for i, p in table.players.items() if p.kind == "human"),
              seats_mod.index(args.seat, n_seats))
    buddy = seats_mod.index(llm.seat, n_seats) if llm else None
    agent = next((i for i, p in table.players.items() if p.kind == "external"), None)
    if llm and not saved:
        llm.impl.reset_temp_notes()        # 临时笔记只管这一局

    tracer = Tracer(TRACE_PATH) if args.trace else None

    if saved:
        log: list[str] = list(meta.get("log") or [])
        said = {int(k): str(v) for k, v in (meta.get("said") or {}).items() if str(v).strip()}
        legacy = str(meta.get("say") or "")
        if legacy and meta.get("say_by") is not None:       # 老存档只有一句，接进来
            said.setdefault(int(meta["say_by"]), legacy)
        steps = int(meta.get("steps") or 0)
        if llm and meta.get("llm"):
            llm.impl.messages = list(meta["llm"].get("messages") or llm.impl.messages)
            llm.impl._pause_calls = int(meta["llm"].get("pause_calls") or 0)
        print(f" 接上第 {steps} 步：{meta.get('phase', '')}，牌墙 {len(game.wall)} 张")
    else:
        dealer = None if args.dealer is None else seats_mod.index(args.dealer, n_seats)
        game = Game(GameConfig(seats=n_seats, seed=args.seed, dealer=dealer,
                               base_points=args.base, fan_cap=args.fan_cap))
        game.start()
        log = []
        said: dict[int, str] = {}
        steps = 0

    def said_text() -> str:
        """每家的最后一句话都留着，标的是说这话的那个人，不是此刻轮到的这个人。"""
        return "\n".join(f"{names.get(s, '')}：{t}" for s, t in sorted(said.items()) if t.strip())

    def persist() -> None:
        if args.no_save:
            return
        state.save(STATE_PATH, game, {
            "you": seats_mod.label(me), "buddy": None if buddy is None else seats_mod.label(buddy),
            "agent": None if agent is None else seats_mod.label(agent),
            "steps": steps, "said": said, "log": log[-30:], "phase": game.phase,
            "llm": None if not llm else {"messages": llm.impl.messages,
                                         "pause_calls": llm.impl._pause_calls},
        })
    board = {s: 0 for s in range(n_seats)}        # 累计分，按座位下标记账
    rounds = max(1, args.rounds)
    winner = None
    stopped = False
    ended_early = False
    finished_all = False

    for rnd in range(rounds):
        if rnd and llm:
            llm.impl.reset_temp_notes()      # 新一局只清临时笔记；长期草稿整场留着
        if rnd:                              # 下一局：庄家胡了连庄，否则下家坐庄
            nxt = seats_mod.next_dealer(game.dealer, winner, n_seats)
            seed = None if args.seed is None else args.seed + rnd
            game = Game(GameConfig(seats=n_seats, seed=seed, dealer=nxt,
                                   base_points=args.base, fan_cap=args.fan_cap))
            game.start()
            said.clear()
            log.clear()
            log.append(f"—— 第 {rnd + 1} 局：{seats_mod.label(nxt)} 号坐庄 ——")

        while game.pending() is not None:
            d = game.pending()
            # 普通摸牌只有一个动作，直接摸；杠后补张可能同时有「摸牌 / 后开花」，
            # 必须把选择权留给玩家，不能先替他摸掉。
            if d.kind == "draw" and len(d.legal) == 1:
                apply_and_note(game, Action("draw"), log, me)
                steps += 1
                persist()
                continue

            p = table.at(d.seat)
            # 轮到我出手时，让牌友先把自由思考跑起来（别人回合的空档不算浪费）
            if d.seat == me and llm and not args.no_prefetch:
                llm.impl.prefetch_async(game.observe(buddy))

            status = ("最后四张：抓了不打，能胡就胡（海底捞月），不能胡就过（/p 中场暂停 /e 提前结束）"
                      if getattr(game, "hai", False)
                      else "你听牌了，报不报？（/p 中场暂停 /e 提前结束）" if d.kind == "ting_declare" and p.supports == "human"
                      else "轮到你了（/p 中场暂停 /e 提前结束）" if p.supports == "human"
                      else f"{p.name} 接入中……" if p.supports == "external"
                      else f"{p.name} 在算……" if p.supports == "llm" else p.name)

            def show_wait(sec: float, name: str = p.name) -> None:
                """等接入位的时候每秒重画一次：那个秒数在走，就说明牌子没死在这儿。"""
                draw_screen(screen(game, me, buddy, f"{name} 接入中…… 已等 {sec:.0f}s（不接就超时自动走一手）",
                                   log, say=said_text(), names=names))
            if p.supports == "external":
                p.on_wait = show_wait
            draw_screen(screen(game, me, buddy, status, log, d.legal if p.supports == "human" else None,
                               say=said_text(), names=names,
                               pick_kind=d.kind if p.supports == "human" else None))

            t0 = time.time()
            try:
                act, speech = p.decide(d)
            except EndEarly:                  # 你按了 /e：当前这局不结算，后面局也不打
                ended_early = True
                log.append("*** 提前结束：后面不打了")
                break
            took = time.time() - t0
            if speech.strip():
                said[d.seat] = speech.strip()      # 每家留着自己最后那句话，别人再打牌也不冲掉

            if act is None:                       # 有人要求中场暂停：这一手先不打
                draw_screen(screen(game, me, buddy, "牌先停着", log, say=said_text(), names=names))
                opener = speech
                actor = p.name
                if p.supports == "human" and talker:   # 我按的暂停：让牌友先说一句（chat 自己会把空闲思考掐掉）
                    opener = talker.chat("（我按了暂停，先停一下，想跟你说两句）", phase="pause") or ""
                    actor = names.get(buddy, "")
                pause_talk(talker, names.get(buddy, ""), opener, actor)
                continue

            log.append(f"{seats_mod.label(d.seat)}号 出手 {paint(f'[{took:.1f}s]', DIM)}")
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
                draw_screen(screen(game, me, buddy, f"走到第 {steps} 步打住", log, say=said_text(), names=names))
                stopped = True
                break

        res = game.result or {}
        if ended_early:
            msg = "提前结束：这一场到这儿（当前这局没结算）"
        elif res.get("type") == "draw_game":
            msg = "荒庄：牌墙摸空"
        elif res.get("settle"):
            msg = str(res["settle"].get("text") or "")
        else:
            msg = str(res) if res else f"这一局打到第 {steps} 步"
        draw_screen(screen(game, me, buddy, msg, log, say=said_text(), names=names))

        settle = res.get("settle") or {}
        for k, v in (settle.get("payers") or {}).items():
            board[int(k)] -= int(v)
            board[int(res["seat"])] += int(v)
        winner = res.get("seat")
        if any(board.values()):
            print(" 累计：" + "　".join(
                f"{names.get(s) or str(seats_mod.label(s)) + '号'} {board[s]:+d}" for s in range(n_seats)))

        # 打完一局就开聊；连打时这只是局间，只能往草稿里追加长期笔记
        last = ended_early or (rnd == rounds - 1) or stopped
        if last and (game.result or ended_early):
            finished_all = True
        if talker and (game.result or ended_early):
            phase = "after" if last else "between"
            if ended_early:
                print("\n 提前结束，进入终场聊天。想跟牌友聊两句就直接打字，空行回车结束。")
                print(" （现在可以改长期笔记了，改完下次开桌生效）")
            elif last:
                print("\n 所有局都打完了，进入终场聊天。想跟牌友聊两句就直接打字，空行回车结束。")
                print(" （现在可以改长期笔记了，改完下次开桌生效）")
            else:
                print("\n 这一局打完了，下一局还没开。想跟牌友聊两句就直接打字，空行回车结束。")
                print(" （现在只能追加长期笔记；想直接收场就打 /e，进入终场聊天）")
            bcast = round_broadcast(rnd + 1, rounds, res, board, buddy, ended_early)
            print(f"\n 播报：{bcast}", flush=True)
            print(f" {names.get(buddy) or ''}：{talker.chat(bcast, phase=phase)}", flush=True)
            while True:
                try:
                    line = input(" 你：").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                if not line:
                    break
                if phase == "between" and line in ("/e", "提前结束"):
                    ended_early = True
                    finished_all = True
                    phase = "after"
                    print("\n 提前结束，进入终场聊天。现在可以改长期笔记了。")
                    continue
                print(f" {names.get(buddy) or ''}：{talker.chat(line, phase=phase)}", flush=True)

        if stopped or ended_early:
            break

    if llm:
        if finished_all:
            llm.impl.finish_session()      # 打完最后一场、甚至没聊，也把草稿并档
        print(f" 牌友出牌 {len(llm.impl.records)} 次，兜底 "
              f"{sum(1 for r in llm.impl.records if r.used_fallback)} 次")
        if args.cache:
            print(llm.impl.cache_report())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

