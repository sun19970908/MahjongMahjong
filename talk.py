#!/usr/bin/env python
"""递话窗口：你在这里随时打一句，我（2 号座位）在另一边读；我回话也会显示在这。

    python talk.py          # 单开一个窗口跑它
    退出：/q 或 Ctrl+C

只用两个文件，不占端口，不抢牌桌的键盘：
    seats/seat2/talk_in.jsonl    你 -> 我
    seats/seat2/talk_out.jsonl   我 -> 你
"""

import json
import queue
import sys
import threading
import time
from pathlib import Path

SEAT = 2
BASE = Path(__file__).resolve().parent / "seats" / f"seat{SEAT}"
IN = BASE / "talk_in.jsonl"
OUT = BASE / "talk_out.jsonl"
BACKLOG = 3


def stamp() -> str:
    return time.strftime("%H:%M:%S")


def append(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"t": stamp(), "text": text}, ensure_ascii=False) + "\n")


def lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return out


def main() -> int:
    if sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    BASE.mkdir(parents=True, exist_ok=True)
    pos = len(lines(OUT))

    print(" 递话窗口：打一行就递出去，空行不算，/q 退出。")
    for r in lines(OUT)[-BACKLOG:]:
        print(f"  2号：{r.get('t', '')} {r.get('text', '')}")
    print()

    q: queue.Queue[str | None] = queue.Queue()

    def reader() -> None:
        for line in sys.stdin:
            q.put(line.rstrip("\n"))
        q.put(None)

    threading.Thread(target=reader, daemon=True).start()

    while True:
        while not q.empty():
            line = q.get()
            if line is None:
                return 0
            if line.strip() in ("/q", "quit", "exit"):
                return 0
            if not line.strip():
                continue
            append(IN, line)
            print(f" 递出：{line}")
        rows = lines(OUT)
        for r in rows[pos:]:
            print(f" 2号：{r.get('t', '')} {r.get('text', '')}")
        pos = len(rows)
        time.sleep(0.2)


if __name__ == "__main__":
    raise SystemExit(main())
