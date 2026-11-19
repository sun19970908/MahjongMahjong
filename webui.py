#!/usr/bin/env python
"""本地网页牌桌：浏览器里打牌，规则和 AI 仍然走原来的 play.py。

    python webui.py                 # 默认接当前 state.json（没有存档就新开）
    python webui.py --new           # 强制新开一桌
    python webui.py --port 8765     # 换端口
    python webui.py --players "1=external,2=llm,3=random,4=random"

网页只拿到 1 号视角：自己的手牌、四家公开的副露/牌河/报听/杠，
别家的手牌只给张数，不给牌面；别人报听也不显示在等什么。
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import seat_io
import seats as seats_mod
import state

HERE = Path(__file__).resolve().parent
STATE_PATH = HERE / "state.json"
SEAT_ROOT = HERE / "seats"
TILES_DIR = HERE / "tiles"
HTML_PATH = HERE / "webui.html"
JS_PATH = HERE / "webui.js"
LOG_PATH = HERE / "webui.log"
OWN_SEAT = 1

PHASE_LABEL = {
    "idle": "待开局",
    "draw": "摸牌",
    "discard": "出牌",
    "react": "等反应",
    "kan_draw": "杠后补张",
    "ting_declare": "报听",
    "ended": "收局",
}
SUIT = {"m": "万", "p": "饼", "s": "条"}
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
TIME_RE = re.compile(r"\s*\[[0-9]+(?:\.[0-9]+)?s\]")


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _at(d: dict | None, seat: int, default=None):
    """存档里 JSON 键是字符串，引擎里是整数；两边都认。"""
    if not isinstance(d, dict):
        return default
    if str(seat) in d:
        return d[str(seat)]
    return d.get(seat, default)


def _seat_no(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value) + 1
    except (TypeError, ValueError):
        return None


def _tile_label(tid: str | None) -> str:
    if not tid:
        return "?"
    if tid == "z5":
        return "红中"
    if len(tid) == 2 and tid[0] in SUIT and tid[1].isdigit():
        return f"{tid[1]}{SUIT[tid[0]]}"
    return str(tid)


def _action_label(a: dict) -> str:
    kind = str(a.get("kind") or "")
    tile = _tile_label(a.get("tile"))
    if kind == "discard":
        return f"打 {tile}"
    if kind == "pon":
        return f"碰 {tile}"
    if kind == "kan":
        label = "加杠" if (a.get("meta") or {}).get("kind") == "added" else "杠"
        return f"{label} {tile}"
    if kind == "chi":
        combo = a.get("combo") or []
        return "吃 " + "+".join(_tile_label(t) for t in combo)
    return {
        "win": "胡",
        "bloom": "后开花",
        "pass": "过",
        "draw": "摸牌",
        "ting": "报听",
    }.get(kind, kind or "?")


def _result_payload(result: dict | None) -> dict | None:
    if not result:
        return None
    out = {
        "type": result.get("type"),
        "seat": _seat_no(result.get("seat")),
        "from": _seat_no(result.get("from")),
        "tile": result.get("tile"),
        "zimo": bool(result.get("zimo")),
        "rob": bool(result.get("rob")),
        "bloom": bool(result.get("bloom")),
        "kan_draw": bool(result.get("kan_draw")),
        "hai": bool(result.get("hai")),
        "hand": list(result.get("hand") or []),
        "melds": list(result.get("melds") or []),
        "reason": result.get("reason"),
    }
    settle = result.get("settle") or {}
    if settle:
        def _payers(raw):
            return {str(_seat_no(k)): int(v)
                    for k, v in (raw or {}).items() if _seat_no(k) is not None}
        out["settle"] = {
            "text": settle.get("text") or "",
            "fans": list(settle.get("fans") or []),
            "payers": _payers(settle.get("payers")),
            "payer_fans": {str(_seat_no(k)): v
                           for k, v in (settle.get("payer_fans") or {}).items()
                           if _seat_no(k) is not None},
        }
    return out


class GameRunner:
    """管 play.py 子进程：开桌、换桌、停桌。"""

    def __init__(self, players: str) -> None:
        self.players = players
        self.lock = threading.RLock()
        self.proc: subprocess.Popen | None = None
        self.error = ""
        self._log = LOG_PATH.open("a", encoding="utf-8")

    # ---- 文件 ----
    def _clear_seat_files(self, seat: int = OWN_SEAT) -> None:
        d = SEAT_ROOT / seats_mod.seat_dir(seat)
        d.mkdir(parents=True, exist_ok=True)
        for name in ("pending.json", "answer.json", "give-up"):
            (d / name).unlink(missing_ok=True)

    def _status(self) -> dict:
        p = self.proc
        return {
            "running": bool(p and p.poll() is None),
            "pid": p.pid if p else None,
            "returncode": None if p is None else p.poll(),
            "error": self.error,
        }

    # ---- 进程 ----
    def _stop_locked(self) -> None:
        p = self.proc
        self.proc = None
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()
                try:
                    p.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass

    def stop(self) -> dict:
        with self.lock:
            self._stop_locked()
            self.error = ""
            return self._status()

    def start(self, *, resume: bool) -> dict:
        with self.lock:
            self._stop_locked()
            self._clear_seat_files()
            self.error = ""
            cmd = [
                sys.executable, "-u", "play.py",
                "--players", self.players,
                "--no-chat",
                "--rounds", "1",
            ]
            if resume and STATE_PATH.exists():
                cmd.append("--resume")
            try:
                self._log.write(f"\n[{time.strftime('%H:%M:%S')}] {' '.join(cmd)}\n")
                self._log.flush()
                self.proc = subprocess.Popen(
                    cmd,
                    cwd=str(HERE),
                    stdout=self._log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                )
            except Exception as exc:              # noqa: BLE001 - 要把错误显示到网页
                self.error = str(exc)
                self.proc = None
            return self._status()

    def close(self) -> None:
        with self.lock:
            self._stop_locked()
            try:
                self._log.close()
            except Exception:
                pass


def _names(meta: dict) -> dict[int, str]:
    you = meta.get("you")
    buddy = meta.get("buddy")
    agent = meta.get("agent")
    try:
        you = int(you) if you is not None else OWN_SEAT
    except (TypeError, ValueError):
        you = OWN_SEAT
    try:
        buddy = int(buddy) if buddy is not None else None
    except (TypeError, ValueError):
        buddy = None
    try:
        agent = int(agent) if agent is not None else None
    except (TypeError, ValueError):
        agent = None
    names = {}
    for s in range(1, 5):
        if s == you:
            names[s] = f"{s}号（你）"
        elif s == buddy:
            names[s] = f"{s}号（牌友）"
        elif s == agent:
            names[s] = f"{s}号（接入）"
        else:
            names[s] = f"{s}号"
    return names


def public_state(runner: GameRunner) -> dict:
    """从存档/文件信箱抽出网页该看的牌桌。任何别家手牌都不在这里。"""
    saved = state.load(STATE_PATH)
    if saved is None:
        return {"ok": False, "message": "还没有牌局存档，正在等 play.py 开局。",
                **runner._status(), "updated": None}

    game, meta = saved
    view = game.observe(seats_mod.index(OWN_SEAT, game.cfg.seats))
    pending = seat_io.pending(OWN_SEAT)
    if game.result is not None or game.phase == "ended":
        pending = None
    elif pending and STATE_PATH.exists():
        # state.json 比 pending 新，说明这一手已经落子，pending 是旧文件。
        p = SEAT_ROOT / f"seat{OWN_SEAT}" / "pending.json"
        try:
            if p.exists() and p.stat().st_mtime < STATE_PATH.stat().st_mtime:
                pending = None
        except OSError:
            pass
    if pending and int(pending.get("seat") or 0) == OWN_SEAT:
        # pending 里的 view 是这一手最新的，手牌刚摸进来时 state.json 可能还没落盘。
        view = pending.get("view") or view
        legal = list(pending.get("legal") or [])
        pending_kind = pending.get("kind")
        pending_seat = OWN_SEAT
    else:
        legal = []
        pending_kind = None
        pending_seat = None

    names = _names(meta)
    my_hand = list(view.get("hand") or [])
    hand_sizes = view.get("hand_sizes") or {}
    melds = view.get("melds") or {}
    discards = view.get("discards") or {}
    ting = view.get("ting") or {}
    piao = view.get("piao") or {}
    ting_closed = view.get("ting_closed") or {}
    kans = view.get("kans") or {}
    dead = view.get("dead") or {}
    dealer = view.get("dealer")
    turn = view.get("turn")
    last = view.get("last")
    result = _result_payload(view.get("result"))

    seats = []
    for s in range(1, int(view.get("seats") or 4) + 1):
        idx = s - 1
        seat_melds = []
        for m in (_at(melds, idx, []) or []):
            if isinstance(m, dict):
                seat_melds.append({"kind": m.get("kind"), "tiles": list(m.get("tiles") or [])})
            else:
                seat_melds.append({"kind": None, "tiles": list(m or [])})
        seats.append({
            "no": s,
            "name": names.get(s, f"{s}号"),
            "is_me": s == OWN_SEAT,
            "hand_count": int(_at(hand_sizes, idx, 0) or 0),
            "melds": seat_melds,
            "discards": list(_at(discards, idx, []) or []),
            "ting": bool(_at(ting, idx, False)),
            "piao": bool(_at(piao, idx, False)),
            "ting_closed": bool(_at(ting_closed, idx, False)),
            "kans": int(_at(kans, idx, 0) or 0),
            "dead": bool(_at(dead, idx, False)),
            "dealer": dealer is not None and int(dealer) == idx,
        })

    actions = []
    for i, a in enumerate(legal):
        actions.append({
            "index": i,
            "kind": a.get("kind"),
            "tile": a.get("tile"),
            "combo": list(a.get("combo") or []),
            "meta": dict(a.get("meta") or {}),
            "label": _action_label(a),
        })

    said = []
    for k, v in sorted((meta.get("said") or {}).items()):
        try:
            s = int(k)
        except (TypeError, ValueError):
            continue
        if str(v).strip():
            said.append({"seat": _seat_no(s), "name": names.get(_seat_no(s), ""), "text": str(v)})

    return {
        "ok": True,
        **runner._status(),
        "own_seat": OWN_SEAT,
        "seats": seats,
        "hand": my_hand,
        "hand_count": len(my_hand),
        "drawn": view.get("drawn"),
        "wall_left": int(view.get("wall_left") or 0),
        "wall_last": view.get("wall_last"),
        "dealer": None if dealer is None else int(dealer) + 1,
        "dice": view.get("dice"),
        "turn": None if turn is None else int(turn) + 1,
        "phase": view.get("phase") or "?",
        "phase_label": PHASE_LABEL.get(view.get("phase") or "", view.get("phase") or "?"),
        "last": None if last is None else {"seat": int(last.get("seat")) + 1,
                                           "tile": last.get("tile")},
        "pending": None if pending_kind is None else {
            "seat": pending_seat, "kind": pending_kind,
        },
        "actions": actions,
        "result": result,
        "steps": int(meta.get("steps") or 0),
        "run_id": str(meta.get("snapshot_run") or ""),
        "log": [TIME_RE.sub("", ANSI_RE.sub("", line))
                for line in list(meta.get("log") or [])[-6:]],
        "said": said[-3:],
        "updated": time.strftime("%H:%M:%S", time.localtime(STATE_PATH.stat().st_mtime))
        if STATE_PATH.exists() else None,
    }


class Handler(BaseHTTPRequestHandler):
    runner: GameRunner

    def log_message(self, fmt: str, *args) -> None:      # 不刷终端
        return

    def _send(self, body: bytes, *, status=HTTPStatus.OK, ctype="application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if "json" in ctype or "html" in ctype or "javascript" in ctype else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: dict, status=HTTPStatus.OK) -> None:
        self._send(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   status=status, ctype="application/json")

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 1024 * 1024:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            return {}

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/state":
            try:
                self._json(public_state(self.runner))
            except Exception as exc:                  # noqa: BLE001 - 页面要看见错误
                self._json({"ok": False, "message": str(exc), **self.runner._status()},
                           HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if path in ("/", "/index.html"):
            self._send(HTML_PATH.read_bytes(), ctype="text/html")
            return
        if path == "/webui.js":
            self._send(JS_PATH.read_bytes(), ctype="application/javascript")
            return
        if path.startswith("/tiles/"):
            name = Path(unquote(path[len("/tiles/"):])).name
            if not name.endswith(".png"):
                self._json({"ok": False, "message": "not found"}, HTTPStatus.NOT_FOUND)
                return
            p = TILES_DIR / name
            if not p.exists():
                self._json({"ok": False, "message": "not found"}, HTTPStatus.NOT_FOUND)
                return
            ctype = mimetypes.guess_type(name)[0] or "image/png"
            self._send(p.read_bytes(), ctype=ctype)
            return
        if path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return
        self._json({"ok": False, "message": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/action":
            self._action()
            return
        if path == "/api/new":
            self._json({"ok": True, **self.runner.start(resume=False)})
            return
        if path == "/api/resume":
            self._json({"ok": True, **self.runner.start(resume=True)})
            return
        if path == "/api/quit":
            self._json({"ok": True, **self.runner.stop()})
            return
        self._json({"ok": False, "message": "not found"}, HTTPStatus.NOT_FOUND)

    def _action(self) -> None:
        pending = seat_io.pending(OWN_SEAT)
        if not pending:
            self._json({"ok": False, "message": "现在不是你出牌"}, HTTPStatus.CONFLICT)
            return
        data = self._body()
        try:
            idx = int(data.get("index"))
        except (TypeError, ValueError):
            self._json({"ok": False, "message": "缺少动作序号"}, HTTPStatus.BAD_REQUEST)
            return
        legal = pending.get("legal") or []
        if not 0 <= idx < len(legal):
            self._json({"ok": False, "message": "这个动作不在当前可选范围"}, HTTPStatus.BAD_REQUEST)
            return
        answer_path = seat_io.seat_dir(OWN_SEAT) / "answer.json"
        if answer_path.exists():
            self._json({"ok": False, "message": "这一手已经提交过了"}, HTTPStatus.CONFLICT)
            return
        action = {k: v for k, v in legal[idx].items() if k != "name"}
        try:
            seat_io.atomic_write(answer_path, {
                "round": pending.get("round"),
                "action": action,
                "say": str(data.get("say") or "")[:200],
            })
        except Exception as exc:                      # noqa: BLE001
            self._json({"ok": False, "message": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._json({"ok": True, "action": action})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--players", default="1=external,2=llm,3=random,4=random",
                    help="传给 play.py 的座位表；默认 1号网页、2号牌友、3/4随机")
    ap.add_argument("--new", action="store_true", help="强制新开，不接 state.json")
    ap.add_argument("--resume", action="store_true", help="强制接 state.json")
    ap.add_argument("--no-start", action="store_true", help="只起网页，不开牌桌")
    ap.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()

    runner = GameRunner(args.players)
    if not args.no_start:
        resume = not args.new
        if not args.resume and not args.new:
            saved = state.load(STATE_PATH)
            if saved is None or saved[0].result is not None or saved[0].phase == "ended":
                resume = False
        if args.resume:
            resume = True
        runner.start(resume=resume)
    elif args.resume:
        print(" --no-start 和 --resume 一起用没有意义：没有开桌进程就没有可接的牌局")

    Handler.runner = runner
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    url = f"http://{args.host}:{args.port}/"
    print(f" 网页牌桌：{url}")
    print(" 1号在网页里出牌，2号牌友照旧走 LLM；Ctrl+C 收摊。")
    if not args.no_open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n 收摊。")
    finally:
        httpd.server_close()
        runner.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
