"""2 号的自动手：牌局一停下等我就出一手，不用叫。

    python auto.py            # 挂上就不再管
    python auto.py --dry      # 只打印打算怎么打，不落子

只看 pending 里给我的东西（手牌、能做的动作），不碰别人的牌。
砍掉它：Ctrl+C 或直接杀进程。
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import seat_io


def parse(t: str) -> tuple[str, int]:
    return t[0], int(t[1:])


def lonely(hand: list[str], t: str) -> tuple[int, int]:
    """越小越该打：先看邻近有没有搭子，再看自己有几张。"""
    suit, n = parse(t)
    near = sum(1 for h in hand if h[0] == suit and 0 < abs(int(h[1:]) - n) <= 2)
    return near, hand.count(t)


def choose(req: dict) -> dict | None:
    legal = req.get("legal") or []
    for a in legal:
        if a["kind"] == "win":
            return a
    for a in legal:
        if a["kind"] == "kan":
            return a
    hand = req["view"]["hand"]
    for a in legal:
        if a["kind"] == "pon" and hand.count(a["tile"]) >= 2:
            return a
    discards = [a for a in legal if a["kind"] == "discard"]
    if discards:
        return min(discards, key=lambda a: (lonely(hand, a["tile"]), a["tile"]))
    for a in legal:
        if a["kind"] == "pass":
            return a
    return legal[0] if legal else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seat", type=int, default=2)
    ap.add_argument("--interval", type=float, default=0.2)
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    log = Path(__file__).resolve().parent / "auto.log"
    done = -1
    while True:
        req = seat_io.pending(args.seat)
        if not req or req["round"] == done:
            time.sleep(args.interval)
            continue
        act = choose(req)
        if act is None:
            time.sleep(args.interval)
            continue
        body = act.get("tile") or "+".join(act.get("combo") or []) or ""
        line = f"{time.strftime('%H:%M:%S')} 第{req['round']}手 {act['kind']} {body}"
        print(line, flush=True)
        with log.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        if not args.dry:
            payload = {k: v for k, v in act.items() if k != "name"}
            seat_io.atomic_write(seat_io.seat_dir(args.seat) / "answer.json",
                                 {"round": req["round"], "action": payload, "say": ""})
        done = req["round"]
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
