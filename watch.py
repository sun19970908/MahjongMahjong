"""2 号的手：牌局一停就自己出，不用叫。

    python watch.py --seat 3              # 默认模型脑（本地 strata）
    python watch.py --seat 3 --brain fast  # 脚本脑，一手不到 0.1 秒
    python watch.py --seat 3 --brain mine  # 等我（对话里的人）亲自落子，等不到就快脑保底
    python watch.py --seat 3 --dry        # 只打印打算怎么打，不落子

盯 seat2/pending.json，看到新的一手就自己想、自己写 answer.json。
砍掉它：Ctrl+C 或直接杀进程。
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from pathlib import Path

import seat_io
from game import Action, Decision, parse_tile
from referee import KINDS, Meld, Referee

HERE = Path(__file__).resolve().parent

REF = Referee()


def as_action(payload: dict) -> Action | None:
    """pending 里的动作 -> Action，meta 原样带回去。"""
    kind = str(payload.get("kind") or "")
    meta = dict(payload.get("meta") or {})
    tile, combo = payload.get("tile"), payload.get("combo")
    try:
        if kind in ("win", "pass", "draw", "bloom", "ting", "pause"):
            return Action(kind, meta=meta)
        if kind in ("discard", "pon", "kan"):
            return Action(kind, tile=parse_tile(tile), meta=meta)
        if kind == "chi":
            return Action("chi", combo=tuple(parse_tile(t) for t in combo), meta=meta)
    except (ValueError, TypeError):
        return None
    return None


def counts_of(hand: list[str]) -> list[int]:
    c = [0] * KINDS
    for t in hand:
        c[parse_tile(t)] += 1
    return c


def melds_of(fixed: list) -> tuple[Meld, ...]:
    out = []
    for m in fixed or []:
        ts = tuple(parse_tile(t) for t in m)
        if len(ts) == 4:
            out.append(Meld("kan", ts))
        elif ts[1] - ts[0] == 1 and ts[2] - ts[1] == 1:
            out.append(Meld("seq", ts))
        else:
            out.append(Meld("triplet", ts))
    return tuple(out)


def lonely(hand: list[int], t: int) -> tuple[int, int]:
    """越小越该打：邻近有没有搭子，再看手里有几张。"""
    base, rank = t / 9, t % 9          # 每门 9 张，红中在最后
    near = sum(1 for u in range(KINDS) if hand[u] and u // 9 == base and 0 < abs(u % 9 - rank) <= 2)
    return near, hand[t]


def fast(req: dict, legal: list[Action]) -> tuple[Action | None, str]:
    """快脑：能胡就胡、能开就开、听牌就报，出牌挑「打完就听」的那张。"""
    for kind in ("win", "bloom", "ting"):
        for a in legal:
            if a.kind == kind:
                return a, {"ting": "听牌了，报听。"}.get(kind, "")

    discards = [a for a in legal if a.kind == "discard"]
    if discards:
        v = req.get("view") or {}
        hand = counts_of(v.get("hand") or [])
        fixed = melds_of(v.get("fixed") or [])
        best, best_key = None, None
        for a in discards:
            hand[a.tile] -= 1
            waits = REF.waits(hand, fixed)
            hand[a.tile] += 1
            if len(waits) == 1:
                key = (0, 0, a.tile)
            elif waits:
                key = (1, len(waits), a.tile)
            else:
                key = (2, *lonely(hand, a.tile), a.tile)
            if best_key is None or key < best_key:
                best, best_key = a, key
        return best, ""

    for kind in ("pass", "draw"):
        for a in legal:
            if a.kind == kind:
                return a, ""
    return (legal[0], "") if legal else (None, "")


def ask(impl, req: dict) -> tuple[Action | None, str]:
    legal = [a for a in (as_action(x) for x in req.get("legal") or []) if a]
    if impl is None:
        return fast(req, legal)
    d = Decision(seat=int(req["seat"]), kind=str(req["kind"]), legal=legal,
                 view=req.get("view") or {})
    return impl.decide(d)


def fallback(legal: list[Action]) -> Action | None:
    for kind in ("pass", "draw"):
        for a in legal:
            if a.kind == kind:
                return a
    return legal[0] if legal else None


def allowed(act: Action, legal: list[Action]) -> bool:
    """报的动作必须在 legal 里，不然引擎会判非法、牌桌直接崩。"""
    for a in legal:
        if a.kind != act.kind:
            continue
        if act.kind in ("discard", "pon", "kan") and a.tile != act.tile:
            continue
        if act.kind == "chi" and a.combo != act.combo:
            continue
        if (a.meta or {}).get("kind") != (act.meta or {}).get("kind"):
            continue
        return True
    return False


def settle(seat: int, round_no: int, limit: float) -> bool:
    """等别人（我）替这一手落子。pending 消失或换了 round 就是接下了。"""
    t0 = time.monotonic()
    while time.monotonic() - t0 < limit:
        req = seat_io.pending(seat)
        if not req or int(req.get("round") or -1) != round_no:
            return True
        time.sleep(0.2)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seat", type=int, default=3)
    ap.add_argument("--interval", type=float, default=0.1)
    ap.add_argument("--brain", choices=["fast", "model", "mine"], default="model",
                    help="model=模型脑（默认）；fast=脚本脑；mine=等我落子，等不到就快脑保底")
    ap.add_argument("--hold", type=float, default=12,
                    help="mine 模式下我的思考预算：超了不替你打，只报一声")
    ap.add_argument("--cap", type=float, default=0,
                    help="硬上限：超了这么多秒还接不上才让脚本兜底一手；0=永不兜底，死等")
    ap.add_argument("--model", default="strata-lowthink")
    ap.add_argument("--timeout", type=float, default=40,
                    help="模型想这么久还没完，就让快脑顶上这一手")
    ap.add_argument("--budget-react", type=int, default=500)
    ap.add_argument("--budget-discard", type=int, default=1200)
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    log = (HERE / "watch.log").open("a", encoding="utf-8")

    def note(text: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {text}"
        print(line, flush=True)
        log.write(line + "\n")
        log.flush()

    def make_impl():
        """每一手一个新脑子：不背上一手的历史，提示词短、回得快。"""
        from llm_player import LLMPlayer, StrataClient
        fresh = LLMPlayer(StrataClient(), args.seat,
                          log_path=HERE / f"seat{args.seat}_log.jsonl", model=args.model,
                          budget_react=args.budget_react, budget_discard=args.budget_discard)

        def tick(kind: str, n_think: int, n_say: int) -> None:
            note(f"　{'想' if kind == 'think' else '说'} {n_think} 字" +
                 (f" / 已开口 {n_say} 字" if n_say else ""))

        fresh.on_progress = tick        # 流式心跳：正在想 / 卡住了，日志里看得出来
        return fresh

    impl = make_impl() if args.brain == "model" else None
    note(f"上工：{args.seat} 号，脑子 {args.brain}" + (f"（{args.model}）" if impl else ""))

    done = 0
    while True:
        req = seat_io.pending(args.seat)
        if not req or req.get("round") == done:
            time.sleep(args.interval)
            continue
        done = int(req["round"])
        t0 = time.monotonic()
        legal = [a for a in (as_action(x) for x in req.get("legal") or []) if a]
        act, say = None, ""
        if args.brain == "mine":
            act, say = fast(req, legal)                 # 先算好自己的保底，超预算也不替打
            note(f"第 {done} 手 {req.get('kind')} 你上，预算 {args.hold:.0f}s")
            if settle(args.seat, done, args.hold):      # 我接了，就不重复落子
                note(f"第 {done} 手 你接了")
                continue
            rest = max(0.0, args.cap - args.hold) if args.cap else float("inf")
            note(f"第 {done} 手 超预算，不替你打" +
                 (f"，最多再等 {rest:.0f}s 就兜底" if args.cap else "，我继续等"))
            if settle(args.seat, done, rest):
                note(f"第 {done} 手 你接了（慢了一步，没替你打）")
                continue
            note(f"第 {done} 手 实在没人接，脚本兜底这一手")
            say = "（兜底）"
        elif impl is None:
            try:
                act, say = fast(req, legal)
            except Exception as exc:
                note(f"这一手快脑出错：{type(exc).__name__} {exc}")
        else:
            # 模型在另一条线里想，想太久就先用快脑顶上，牌桌不许停在那儿
            out: dict = {}

            def think() -> None:
                try:
                    out["r"] = ask(impl, req)
                except Exception as exc:               # 脑子抽风也不能把牌桌卡死
                    out["e"] = exc

            worker = threading.Thread(target=think, daemon=True)
            worker.start()
            worker.join(args.timeout)
            if "r" in out:
                act, say = out["r"]
            elif "e" in out:
                note(f"这一手模型出错：{type(out['e']).__name__} {out['e']}")
                act, say = fast(req, legal)
            else:
                note(f"模型 {args.timeout:.0f}s 没想完，先让快脑顶上")
                act, say = fast(req, legal)
        if act is None or not allowed(act, legal):
            if act is not None:
                note(f"报的 {act.kind} 不在这一手的选项里，按规矩挑一个")
            act = fallback(legal)
        if act is None:
            note("连个能做的动作都没有，跳过")
            continue
        payload = act.to_dict()
        seconds = time.monotonic() - t0
        body = payload.get("tile") or payload.get("combo") or ""
        note(f"第 {done} 手 {req.get('kind')} -> {act.kind} {body} [{seconds:.1f}s] {say}")
        if args.dry:
            continue
        seat_io.atomic_write(seat_io.seat_dir(args.seat) / "answer.json",
                             {"round": done, "action": payload, "say": say})
        if args.brain == "model":
            impl = make_impl()          # 换新脑子，别背着历史越跑越慢
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
