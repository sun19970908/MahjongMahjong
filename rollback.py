#!/usr/bin/env python
"""按步快照回退。

    python rollback.py              # 列出快照
    python rollback.py --to 42      # 恢复第 42 步落子之前
    python rollback.py --latest     # 恢复最近一帧

恢复前会先把当前 state.json 备份成 state.before-rollback.json。
恢复后运行 play.py --resume 接着打；别在牌桌进程还开着时恢复。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SNAP_ROOT = ROOT / "snapshots"
STATE = ROOT / "state.json"
BACKUP = ROOT / "state.before-rollback.json"


def frames() -> list[Path]:
    return sorted(SNAP_ROOT.glob("*/step_*.json"))


def describe(p: Path) -> str:
    data = json.loads(p.read_text(encoding="utf-8"))
    meta = data.get("meta") or {}
    game = data.get("game") or {}
    snap = meta.get("snapshot") or {}
    return (f"{p.parent.name}/{p.stem}"
            f"  第 {meta.get('steps', snap.get('step', '?'))} 步"
            f"  下一手={snap.get('next', '?')} {snap.get('seat', '?')}号"
            f"  牌墙{snap.get('wall', len(game.get('wall') or []))}张"
            f"  阶段={game.get('phase', '?')}")


def choose(to: str, latest: bool, run: str) -> Path | None:
    if latest:
        fs = frames()
        return fs[-1] if fs else None
    if not to:
        return None
    name = f"step_{int(to):05d}.json" if to.isdigit() else to
    if not name.endswith(".json"):
        name += ".json"
    dirs = [p for p in SNAP_ROOT.iterdir() if p.is_dir()] if SNAP_ROOT.exists() else []
    if run:
        dirs = [p for p in dirs if p.name == run]
    for d in sorted(dirs, key=lambda p: p.name, reverse=True):
        p = d / name
        if p.exists():
            return p
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", default="", help="要回到第几步落子之前")
    ap.add_argument("--latest", action="store_true", help="回到最近一帧")
    ap.add_argument("--run", default="", help="只在这一场里找（目录名）")
    args = ap.parse_args()

    fs = frames()
    if not args.to and not args.latest:
        if not fs:
            print(f"还没有快照：{SNAP_ROOT}")
            return 0
        for p in fs[-20:]:
            print(describe(p))
        if len(fs) > 20:
            print(f"…… 共 {len(fs)} 帧，只列出最近 20 帧")
        return 0

    p = choose(args.to, args.latest, args.run)
    if p is None:
        print(f"找不到快照：{args.to or 'latest'}")
        return 1

    if STATE.exists():
        shutil.copyfile(STATE, BACKUP)
    tmp = STATE.with_suffix(".tmp")
    shutil.copyfile(p, tmp)
    os.replace(tmp, STATE)
    print(f"已恢复：{p.parent.name}/{p.name}")
    print("当前 state.json 已备份到 state.before-rollback.json")
    print("停掉牌桌进程后运行： python play.py --resume")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
