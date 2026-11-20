#!/usr/bin/env python
"""本地网页牌桌：HTTP/静态资源适配层。

规则、动作校验、存档、快照、多局轮转、聊天都在 core.TableCore。
这个文件只做三件事：
1. 起停一个 TableCore（网页座位用 WebUi 阻塞等按钮，不占 stdin）；
2. 把 core.state_payload() 原样给前端；
3. 把前端的按钮翻成 core.submit / pause / chat / next_round。

    python webui.py                 # 默认 1号网页、2号牌友、3/4随机
    python webui.py --new           # 强制新开
    python webui.py --port 8765
    python webui.py --players "1=human,2=llm,3=external,4=random"
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import queue
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import seats as seats_mod
from core import CoreConfig, TableCore
from players import EndEarly

HERE = Path(__file__).resolve().parent
STATE_PATH = HERE / "state.json"
TILES_DIR = HERE / "tiles"
HTML_PATH = HERE / "webui.html"
JS_PATH = HERE / "webui.js"
DEFAULT_PLAYERS = ""


class WebUi:
    """网页这一侧：按钮就是输入，HTTP 只负责往队列里放答案。

    `decide` 是内核调用的阻塞点；页面轮询看到 pending，用户点按钮后 HTTP
    把答案塞进队列，内核就被放行。所有合法性仍由 core.submit 判断。
    """

    def __init__(self) -> None:
        self.core: TableCore | None = None
        self.answers: queue.Queue = queue.Queue()
        self._pause_requested = False
        self._end_requested = False
        self._cancelled = False
        self._resume_requested = False
        self._pause_event = threading.Event()
        self._next_event = threading.Event()
        self._lock = threading.RLock()

    def bind(self, core: TableCore) -> None:
        self.core = core

    # ---- 内核调的三个阻塞点 ----
    def decide(self, core: TableCore, d):
        while True:
            with self._lock:
                if self._cancelled or self._end_requested:
                    raise EndEarly()
                if self._pause_requested:
                    self._pause_requested = False
                    return None, ""
            try:
                item = self.answers.get(timeout=0.2)
            except queue.Empty:
                continue
            kind = item[0]
            if kind == "answer":
                return item[1], item[2]
            if kind == "pause":
                continue
            if kind == "end":
                raise EndEarly()
            if kind == "cancel":
                raise EndEarly()

    def pause(self, core: TableCore, actor: str, opener: str) -> None:
        self._pause_event.clear()
        # 有可能用户在牌友开口之前就点了继续：这时不必再等。
        with self._lock:
            if self._resume_requested:
                self._resume_requested = False
                return
        while not self._pause_event.wait(0.2):
            with self._lock:
                if self._cancelled or self._end_requested:
                    raise EndEarly()
        with self._lock:
            self._resume_requested = False

    def round_end(self, core: TableCore, info: dict) -> bool:
        if info.get("last") or info.get("ended_early"):
            return False
        while True:
            if self._next_event.wait(0.2):
                self._next_event.clear()
                return True
            with self._lock:
                if self._cancelled or self._end_requested:
                    return False

    def tick(self, core: TableCore, seconds: float) -> None:
        return

    def cancel(self, core: TableCore) -> None:
        with self._lock:
            self._cancelled = True
            self._end_requested = True
        self.answers.put(("cancel", None, ""))
        self._pause_event.set()
        self._next_event.set()

    # ---- HTTP 按钮 ----
    def answer(self, act, say: str = "") -> None:
        with self._lock:
            if self._end_requested or self._cancelled:
                return
        self.answers.put(("answer", act, say))

    def request_pause(self) -> None:
        with self._lock:
            self._pause_requested = True
            self._resume_requested = False
        self.answers.put(("pause", None, ""))

    def request_context_cut(self) -> None:
        """局间标记上下文结束：不推进下一局，留给用户聊完再点“下一局”。"""
        return

    def allow_next(self) -> None:
        self._next_event.set()

    def resume_play(self) -> None:
        with self._lock:
            self._resume_requested = True
        self._pause_event.set()


class WebTable:
    """一个网页进程只有一桌；起停和换桌都在这里，不碰牌局规则。"""

    def __init__(self, players: str = DEFAULT_PLAYERS, context_rounds: int = 1) -> None:
        self.lock = threading.RLock()
        self.players = players
        self.context_rounds = max(1, int(context_rounds or 1))
        self.ui: WebUi | None = None
        self.core: TableCore | None = None
        self.error = ""
        self._generation = 0

    def _config(self, overrides: dict | None = None, *, resume: bool = False) -> CoreConfig:
        overrides = overrides or {}
        players = str(overrides.get("players") or self.players or DEFAULT_PLAYERS)
        return CoreConfig(
            seat=int(overrides.get("seat") or 1),
            players=players,
            dealer=(int(overrides["dealer"]) if overrides.get("dealer") not in (None, "", 0) else None),
            base=int(overrides.get("base") or 1),
            fan_cap=int(overrides.get("fan_cap") or 8),
            seed=(int(overrides["seed"]) if overrides.get("seed") not in (None, "") else None),
            rounds=self.context_rounds,
            steps=max(0, int(overrides.get("steps") or 0)),
            agent_wait=max(0.0, float(overrides.get("agent_wait") or 0)),
            offline=bool(overrides.get("offline")),
            no_save=False,
            no_chat=bool(overrides.get("no_chat")),
            state_path=STATE_PATH,
        )

    def _stop_core(self) -> None:
        old = self.core
        if old:
            try:
                old.stop()
            except Exception:
                pass
        self.core = None
        self.ui = None

    def start(self, *, resume: bool = False, overrides: dict | None = None) -> dict:
        with self.lock:
            self._stop_core()
            self.error = ""
            self._generation += 1
            if overrides and overrides.get("players"):
                self.players = str(overrides["players"])
            cfg = self._config(overrides, resume=resume)
            ui = WebUi()
            core = TableCore(cfg, ui)
            ui.bind(core)
            self.ui = ui
            self.core = core
            try:
                core.start(resume=resume, background=True)
                self.context_rounds = core.rounds
            except Exception as exc:                     # noqa: BLE001
                self.error = str(exc)
            return self.status()

    def stop(self) -> dict:
        with self.lock:
            self._stop_core()
            self.error = ""
            return {"ok": True, "running": False, "finished": True,
                    "message": "牌桌已停"}

    def status(self) -> dict:
        with self.lock:
            c = self.core
            if c is None:
                return {"ok": False, "running": False, "error": self.error,
                        "message": "牌桌还没起来"}
            return {"ok": True, "running": c.status().get("running", False),
                    "error": self.error or c.status().get("error", ""),
                    **c.status()}

    def state(self, *, debug: bool = False) -> dict:
        with self.lock:
            c = self.core
            if c is None:
                return {"ok": False, "running": False, "error": self.error,
                        "message": "牌桌还没起来"}
            try:
                # 网页坐的那一家人可能在随机后的任何座位；按内核记的 human 座位取视角，
                # 不能再写死 1 号，否则自己的手牌会被当成上家亮出来。
                return c.state_payload(debug=debug)
            except Exception as exc:                     # noqa: BLE001
                return {"ok": False, "running": False, "error": str(exc),
                        "message": str(exc)}

    def action(self, index: int, say: str = "") -> dict:
        with self.lock:
            if self.core is None:
                return {"ok": False, "message": "牌桌没起来"}
            return self.core.submit(index, say)

    def chat(self, text: str, phase: str | None = None,
             seat: int | str | None = None) -> dict:
        with self.lock:
            c = self.core
        if c is None:
            return {"ok": False, "message": "牌桌没起来"}
        return c.chat(text, phase=phase, seat=seat)

    def pause(self) -> dict:
        with self.lock:
            return self.core.pause_request() if self.core else {"ok": False}

    def end(self) -> dict:
        with self.lock:
            return self.core.end_request() if self.core else {"ok": False}

    def set_context_rounds(self, rounds: int) -> dict:
        with self.lock:
            if self.core is None:
                return {"ok": False, "message": "牌桌没起来"}
            result = self.core.set_context_rounds(rounds)
            if result.get("ok"):
                self.context_rounds = int(result["rounds"])
            return result

    def resume_play(self) -> dict:
        with self.lock:
            return self.core.resume_after_pause() if self.core else {"ok": False}

    def next_round(self) -> dict:
        with self.lock:
            return self.core.next_round_request() if self.core else {"ok": False}

    def snapshots(self) -> dict:
        with self.lock:
            return {"ok": True, "frames": self.core.snapshots() if self.core else []}

    def rollback(self, name: str) -> dict:
        with self.lock:
            c = self.core
            if c is None:
                return {"ok": False, "message": "牌桌没起来"}
            frames = c.snapshots()
            target = None
            for f in frames:
                if name in (f.get("name"), str(f.get("step")), f.get("path")):
                    target = f
                    break
            if target is None:
                return {"ok": False, "message": "找不到这个快照"}
            old_cfg = c.cfg
            self._stop_core()
            if not c.rollback_from_snapshot(target["path"]):
                return {"ok": False, "message": "快照写入失败"}
            ui = WebUi()
            core = TableCore(old_cfg, ui)
            ui.bind(core)
            self.ui = ui
            self.core = core
            try:
                core.start(resume=True, background=True)
            except Exception as exc:                     # noqa: BLE001
                self.error = str(exc)
                return {"ok": False, "message": str(exc)}
            return {"ok": True, "message": "已回退，牌局从这一手之前接上"}


class Handler(BaseHTTPRequestHandler):
    table: WebTable
    debug = False

    def log_message(self, fmt: str, *args) -> None:
        return

    def _send(self, body: bytes, *, status=HTTPStatus.OK,
              ctype="application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype + (
            "; charset=utf-8" if "json" in ctype or "html" in ctype or "javascript" in ctype else ""))
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
            self._json(self.table.state(debug=self.debug))
            return
        if path == "/api/snapshots":
            self._json(self.table.snapshots())
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
        body = self._body()
        if path == "/api/action":
            try:
                idx = int(body.get("index"))
            except (TypeError, ValueError):
                self._json({"ok": False, "message": "缺少动作序号"}, HTTPStatus.BAD_REQUEST)
                return
            self._json(self.table.action(idx, str(body.get("say") or "")))
            return
        if path == "/api/new":
            self._json({"ok": True, **self.table.start(resume=False, overrides=body)})
            return
        if path == "/api/resume":
            self._json({"ok": True, **self.table.start(resume=True, overrides=body)})
            return
        if path == "/api/rollback":
            self._json(self.table.rollback(str(body.get("name") or body.get("step") or "")))
            return
        if path == "/api/chat":
            self._json(self.table.chat(str(body.get("text") or ""),
                                       body.get("phase") or None,
                                       body.get("seat")))
            return
        if path == "/api/pause":
            self._json(self.table.pause())
            return
        if path == "/api/resume-play":
            self._json(self.table.resume_play())
            return
        if path == "/api/next-round":
            self._json(self.table.next_round())
            return
        if path == "/api/end":
            self._json(self.table.end())
            return
        if path == "/api/context-rounds":
            try:
                rounds = int(body.get("rounds"))
            except (TypeError, ValueError):
                self._json({"ok": False, "message": "缺少上下文局数"}, HTTPStatus.BAD_REQUEST)
                return
            self._json(self.table.set_context_rounds(rounds))
            return
        if path == "/api/quit":
            self._json({"ok": True, **self.table.stop()})
            return
        self._json({"ok": False, "message": "not found"}, HTTPStatus.NOT_FOUND)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--players", default=DEFAULT_PLAYERS,
                    help="传给内核的座位表；留空=1号 human、2号 llm、3/4 random；"
                         "3号外部接入写 external")
    ap.add_argument("--new", action="store_true")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--debug", action="store_true",
                    help="调试全牌：网页可见四家手牌（本地教学用）")
    ap.add_argument("--context-rounds", type=int, default=1,
                    help="LLM 上下文窗口：每几局清空一次（不是牌局总局数）")
    ap.add_argument("--no-start", action="store_true")
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    table = WebTable(args.players, args.context_rounds)
    if not args.no_start:
        resume = not args.new
        if not args.resume and not args.new:
            try:
                from state import load
                saved = load(STATE_PATH)
                if saved is None or saved[0].result is not None or saved[0].phase == "ended":
                    resume = False
            except Exception:
                resume = False
        if args.resume:
            resume = True
        table.start(resume=resume)

    Handler.table = table
    Handler.debug = args.debug
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    url = f"http://{args.host}:{args.port}/"
    print(f" 网页牌桌：{url}")
    print(" 牌局内核在 core.py；网页只做 HTTP 和按钮。")
    if args.debug:
        print(" DEBUG：网页可见四家手牌（只在本机教学用）")
    if not args.no_open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n 收摊。")
    finally:
        httpd.server_close()
        table.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
