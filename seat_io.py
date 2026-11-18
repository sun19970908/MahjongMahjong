#!/usr/bin/env python
"""外部座位的进出接口。给任何进程（包括我）用：

    python seat_io.py pending --seat 2          # 这一手该你了吗（没有就打印 none）
    python seat_io.py wait    --seat 2 --timeout 600
    python seat_io.py answer  --seat 2 --kind discard --tile m3 --say "这张先走"
    python seat_io.py answer  --seat 2 --kind chi --combo s4+s6
    python seat_io.py answer  --seat 2 --kind pause --say "等一下，这手我有话要说"
    python seat_io.py give-up --seat 2          # 这手不想了，交给随机
    python seat_io.py trace   -n 20             # 复盘：谁做了什么、多久、读了多少 token

只用文件，不占端口，不联网。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from players import DEFAULT_SEAT_ROOT, action_from_dict


def seat_dir(seat: int) -> Path:
    d = DEFAULT_SEAT_ROOT / f"seat{seat}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def pending(seat: int) -> dict | None:
    p = seat_dir(seat) / "pending.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def atomic_write(path: Path, obj: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def show(req: dict) -> None:
    """把待决的这一手讲成人话：手牌、牌桌、能做哪些（标序号）。"""
    v = req.get("view") or {}
    print(f"[第 {req['round']} 手] 轮到你（{req['seat']}号）{req.get('kind')}")
    print("手牌 " + " ".join(v.get("hand") or []))
    fixed = v.get("fixed") or []
    if fixed:
        print("已成面子 " + "  ".join("/".join(t) if isinstance(t, list) else str(t) for t in fixed))
    if v.get("last"):
        last = v["last"]
        print(f"上一张：{last.get('seat')} 号打 {last.get('tile')}")
    print(f"牌墙剩 {v.get('wall_left')} 张")
    for i, a in enumerate(req.get("legal") or [], 1):
        body = a.get("tile") or a.get("combo") or ""
        if isinstance(body, list):
            body = "+".join(body)
        print(f"  {i}. {a['kind']} {body}")


def cmd_pending(args) -> int:
    req = pending(args.seat)
    if not req:
        print("none")
        return 0
    if args.json:
        print(json.dumps(req, ensure_ascii=False))
    else:
        show(req)
    return 0


def cmd_wait(args) -> int:
    t0 = time.monotonic()
    while True:
        req = pending(args.seat)
        if req:
            if args.json:
                print(json.dumps(req, ensure_ascii=False))
            else:
                show(req)
            return 0
        if args.timeout and time.monotonic() - t0 > args.timeout:
            print("timeout")
            return 1
        time.sleep(0.2)


def cmd_answer(args) -> int:
    req = pending(args.seat)
    if not req:
        print("现在不是你出牌（没有 pending）", file=sys.stderr)
        return 1
    if args.pick:
        legal = req.get("legal") or []
        if not 1 <= args.pick <= len(legal):
            print(f"只有 {len(legal)} 个可选", file=sys.stderr)
            return 1
        payload = {k: v for k, v in legal[args.pick - 1].items() if k != "name"}
        atomic_write(seat_dir(args.seat) / "answer.json",
                     {"round": req["round"], "action": payload, "say": args.say or ""})
        print(f"已出：{payload.get('kind')} {payload.get('tile') or payload.get('combo') or ''}".strip())
        return 0
    if args.kind == "pause":
        atomic_write(seat_dir(args.seat) / "answer.json",
                     {"round": req["round"], "action": {"kind": "pause"}, "say": args.say or ""})
        print(f"已举手暂停：{args.say or '（没说话）'}")
        return 0
    if not args.kind:
        print("要么 --pick N，要么 --kind（手动给出动作）", file=sys.stderr)
        return 1
    payload = {"kind": args.kind}
    if args.tile:
        payload["tile"] = args.tile
    if args.combo:
        payload["combo"] = [x for x in args.combo.replace("+", " ").split() if x]
    act = action_from_dict(payload)
    if act is None:
        print("这个动作看不懂，没发出去", file=sys.stderr)
        return 1
    legal = req.get("legal") or []
    ok = any(a.get("kind") == act.kind
             and (a.get("tile") == payload.get("tile") or a.get("combo") == payload.get("combo")
                  or act.kind in ("win", "pass"))
             for a in legal)
    if not ok:
        print("这一手不能这么做。能做的有：", file=sys.stderr)
        for i, a in enumerate(legal, 1):
            print(f"  {i}. {a['kind']} {a.get('tile') or a.get('combo') or ''}", file=sys.stderr)
        return 1
    atomic_write(seat_dir(args.seat) / "answer.json",
                 {"round": req["round"], "action": payload, "say": args.say or ""})
    print(f"已出：{act.kind} {payload.get('tile') or payload.get('combo') or ''}".strip())
    return 0


def cmd_give_up(args) -> int:
    (seat_dir(args.seat) / "give-up").write_text("", encoding="utf-8")
    print("这手交随机了")
    return 0


def cmd_trace(args) -> int:
    path = Path(args.file)
    if not path.exists():
        print("还没有 trace")
        return 0
    rows = path.read_text(encoding="utf-8").splitlines()[-args.n:]
    for line in rows:
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        act = r.get("action") or {}
        body = act.get("tile") or act.get("combo") or ""
        if isinstance(body, list):
            body = "+".join(body)
        extra = ""
        if r.get("prompt"):
            extra = f" | prompt {r['prompt']} 复用 {r.get('cached', 0)} 新读 {r.get('read', 0)}"
        print(f"{r.get('t','')} {r.get('name','?')} {act.get('kind','-')} {body} "
              f"[{r.get('seconds', 0)}s]{extra}")
    return 0


def _talk_path(seat: int, which: str) -> Path:
    return seat_dir(seat) / f"talk_{which}.jsonl"


def _talk_rows(seat: int, which: str) -> list[dict]:
    p = _talk_path(seat, which)
    if not p.exists():
        return []
    rows = []
    for raw in p.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return rows


def cmd_hear(args) -> int:
    rows = _talk_rows(args.seat, "in")
    if not rows:
        print("他没递话")
        return 0
    for i, r in enumerate(rows[-args.n:], max(1, len(rows) - args.n + 1)):
        print(f"[{i}] {r.get('t', '')} {r.get('text', '')}")
    return 0


def cmd_tell(args) -> int:
    p = _talk_path(args.seat, "out")
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"t": time.strftime("%H:%M:%S"), "text": args.text},
                           ensure_ascii=False) + "\n")
    print(f"已回话：{args.text}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("pending")
    p.add_argument("--seat", type=int, default=2)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_pending)

    p = sub.add_parser("wait")
    p.add_argument("--seat", type=int, default=2)
    p.add_argument("--timeout", type=float, default=0)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_wait)

    p = sub.add_parser("answer")
    p.add_argument("--seat", type=int, default=2)
    p.add_argument("--pick", type=int, default=0, help="直接选第几个可选动作（带 meta，加杠/明杠必须用它）")
    p.add_argument("--kind", default="")
    p.add_argument("--tile", default=None)
    p.add_argument("--combo", default=None)
    p.add_argument("--say", default="")
    p.set_defaults(func=cmd_answer)

    p = sub.add_parser("give-up")
    p.add_argument("--seat", type=int, default=2)
    p.set_defaults(func=cmd_give_up)

    p = sub.add_parser("trace")
    p.add_argument("-n", type=int, default=20)
    p.add_argument("--file", default=str(Path(__file__).resolve().parent / "trace.jsonl"))
    p.set_defaults(func=cmd_trace)

    p = sub.add_parser("hear", help="读他在递话窗口里说的话")
    p.add_argument("--seat", type=int, default=2)
    p.add_argument("-n", type=int, default=10)
    p.set_defaults(func=cmd_hear)

    p = sub.add_parser("tell", help="往递话窗口回一句")
    p.add_argument("--seat", type=int, default=2)
    p.add_argument("text")
    p.set_defaults(func=cmd_tell)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
