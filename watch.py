"""2 号的手：牌局一停就自己出，不用叫。

    python watch.py --seat 2          # 挂上就不再管
    python watch.py --seat 2 --dry    # 只打印打算怎么打，不落子

盯 seat2/pending.json，看到新的一手就自己想、自己写 answer.json。
砍掉它：Ctrl+C 或直接杀进程。
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import seat_io
from game import Action, Decision, parse_tile
from llm_player import LLMPlayer, StrataClient

HERE = Path(__file__).resolve().parent


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


def ask(impl: LLMPlayer, req: dict) -> tuple[Action | None, str]:
    legal = [a for a in (as_action(x) for x in req.get("legal") or []) if a]
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
    """模型给的动作必须在 legal 里，不然引擎会判非法、牌桌直接崩。"""
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seat", type=int, default=2)
    ap.add_argument("--interval", type=float, default=0.2)
    ap.add_argument("--model", default="strata-lowthink")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    log = (HERE / "watch.log").open("a", encoding="utf-8")

    def note(text: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {text}"
        print(line, flush=True)
        log.write(line + "\n")
        log.flush()

    impl = LLMPlayer(StrataClient(), args.seat,
                     log_path=HERE / f"seat{args.seat}_log.jsonl", model=args.model)
    note(f"上工：{args.seat} 号，模型 {args.model}")

    done = 0
    while True:
        req = seat_io.pending(args.seat)
        if not req or req.get("round") == done:
            time.sleep(args.interval)
            continue
        done = int(req["round"])
        t0 = time.monotonic()
        legal = [a for a in (as_action(x) for x in req.get("legal") or []) if a]
        try:
            act, say = ask(impl, req)
        except Exception as exc:                       # 模型抽风也不能把牌桌卡死
            act, say = None, ""
            note(f"这一手模型出错：{type(exc).__name__} {exc}")
        if act is None or not allowed(act, legal):
            if act is not None:
                note(f"模型报的 {act.kind} 不在这一手的选项里，按规矩挑一个")
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
