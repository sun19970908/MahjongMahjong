"""牌桌内核：排座、跑局、存档、快照、多局轮转、计分、聊天。

这里不 import 任何 UI。TUI（play.py）和网页（webui.py）都只做两件事：
1. 把 `SessionUi` 的 `decide / pause / round_end` 接成自己的输入、画面；
2. 把 `TableCore.state_payload()` 画出来。

规则判定仍在 game/referee/scoring；本文件只管「一场牌怎么往下走」。
"""

from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Protocol

import seats as seats_mod
import state as state_mod
from game import Action, Decision, Game, GameConfig
from llm_player import LLMPlayer, StrataClient
from players import (
    BasePlayer,
    EndEarly,
    ExternalPlayer,
    RandomPlayer,
    SeatPlan,
    Table,
    Tracer,
    build_players,
    parse_players,
    plan_from_flags,
)
from referee import parse_tile, tile_id

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "state.json"
SNAP_ROOT = ROOT / "snapshots"
TRACE_PATH = ROOT / "trace.jsonl"
SUIT = {"m": "万", "p": "饼", "s": "条"}
PHASE_LABEL = {
    "idle": "待开局",
    "draw": "摸牌",
    "discard": "出牌",
    "react": "等反应",
    "kan_draw": "杠后补张",
    "ting_declare": "报听",
    "ended": "收局",
}


# ---------------------------------------------------------------- 配置 / UI 接口


@dataclass
class CoreConfig:
    """开一桌要的所有参数；TUI 和网页都只填这一张表。"""

    seat: int = 1
    buddy: int | None = None
    agent: int | None = None
    players: str = ""
    dealer: int | None = None
    base: int = 1
    fan_cap: int = 8
    seed: int | None = None
    rounds: int = 1
    steps: int = 0
    demo: bool = False
    offline: bool = False
    no_chat: bool = False
    no_save: bool = False
    no_prefetch: bool = False
    trace: bool = False
    cache: bool = False
    agent_wait: float = 0.0
    root: Path = ROOT
    state_path: Path = STATE_PATH
    snap_root: Path = SNAP_ROOT
    trace_path: Path = TRACE_PATH

    def normalized(self) -> "CoreConfig":
        self.seat = max(1, int(self.seat or 1))
        self.rounds = max(1, int(self.rounds or 1))
        self.base = max(0, int(self.base if self.base is not None else 1))
        self.fan_cap = max(0, int(self.fan_cap if self.fan_cap is not None else 8))
        self.steps = max(0, int(self.steps or 0))
        return self


class SessionUi(Protocol):
    """UI 只被内核问三件事，不反过来驱动牌局。"""

    def decide(self, core: "TableCore", d: Decision) -> tuple[Action | None, str]:
        ...

    def pause(self, core: "TableCore", actor: str, opener: str) -> None:
        ...

    def round_end(self, core: "TableCore", info: dict) -> bool:
        """返回 True=继续下一局，False=收场。"""
        ...

    def tick(self, core: "TableCore", seconds: float) -> None:
        """外部接入位等待时每秒回调一次；只用来刷新画面。"""
        ...

    def cancel(self, core: "TableCore") -> None:
        """内核要停时叫醒 UI 的阻塞输入。"""
        ...


class LocalSeat(BasePlayer):
    """本地人：真正的输入在 UI 里，牌桌只把 Decision 递过去。

    TUI 和网页各实现一个，不把 `input()` 或 HTTP 塞进内核。
    """

    supports = "human"

    def __init__(self, seat: int, ui: SessionUi, core: "TableCore") -> None:
        super().__init__(seat)
        self.ui = ui
        self.core = core
        self.name = f"{seat}号（你）"

    def decide(self, d: Decision) -> tuple[Action | None, str]:
        return self.ui.decide(self.core, d)


# ---------------------------------------------------------------- 小工具


def tile_label(tid: str | None) -> str:
    if not tid:
        return "?"
    if tid == "z5":
        return "红中"
    if len(tid) == 2 and tid[0] in SUIT and tid[1].isdigit():
        return f"{tid[1]}{SUIT[tid[0]]}"
    return str(tid)


def action_label(a: dict) -> str:
    kind = str(a.get("kind") or "")
    tile = tile_label(a.get("tile"))
    if kind == "discard":
        return f"打 {tile}"
    if kind == "pon":
        return f"碰 {tile}"
    if kind == "kan":
        label = "加杠" if (a.get("meta") or {}).get("kind") == "added" else "杠"
        return f"{label} {tile}"
    if kind == "chi":
        combo = a.get("combo") or []
        return "吃 " + "+".join(tile_label(t) for t in combo)
    return {
        "win": "胡",
        "bloom": "后开花",
        "pass": "过",
        "draw": "摸牌",
        "ting": "报听",
    }.get(kind, kind or "?")


def seat_no(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value) + 1
    except (TypeError, ValueError):
        return None


def _at(d: dict | None, idx: int, default=None):
    if not isinstance(d, dict):
        return default
    if str(idx) in d:
        return d[str(idx)]
    return d.get(idx, default)


def _result_payload(result: dict | None) -> dict | None:
    if not result:
        return None
    out = {
        "type": result.get("type"),
        "seat": seat_no(result.get("seat")),
        "from": seat_no(result.get("from")),
        "tile": result.get("tile"),
        "zimo": bool(result.get("zimo")),
        "rob": bool(result.get("rob")),
        "bloom": bool(result.get("bloom")),
        "kan_draw": bool(result.get("kan_draw")),
        "hai": bool(result.get("hai")),
        "hand": list(result.get("hand") or []),
        "melds": list(result.get("melds") or []),
        "reason": result.get("reason"),
        "last_draw": result.get("last_draw"),
    }
    settle = result.get("settle") or {}
    if settle:
        def payers(raw):
            return {str(seat_no(k)): int(v)
                    for k, v in (raw or {}).items() if seat_no(k) is not None}

        out["settle"] = {
            "text": settle.get("text") or "",
            "fans": list(settle.get("fans") or []),
            "payers": payers(settle.get("payers")),
            "payer_fans": {str(seat_no(k)): v
                           for k, v in (settle.get("payer_fans") or {}).items()
                           if seat_no(k) is not None},
        }
    return out


def make_broadcast(rnd: int, rounds: int, res: dict, board: dict[int, int],
                   you: int | None, ended_early: bool = False) -> str:
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
        tile = tile_label(res.get("tile")) if res.get("tile") else "?"
        if res.get("zimo"):
            how = "自摸"
        elif res.get("rob"):
            how = f"抢杠，{seats_mod.label(res.get('from'))}号"
        else:
            how = f"{seats_mod.label(res.get('from'))}号点炮"
        head += f"{who} 胡 {tile}（{how}）。"
        hand = res.get("hand") or []
        if hand:
            head += "赢家牌：" + " ".join(tile_label(t) for t in hand) + "。"
        melds = res.get("melds") or []
        if melds:
            head += "副露：" + "；".join(
                " ".join(tile_label(t) for t in m.get("tiles") or ())
                for m in melds) + "。"
    settle = res.get("settle") or {}
    if settle.get("text"):
        head += f"结算：{settle['text']}。"
    if board:
        head += "累计：" + "，".join(
            f"{seats_mod.label(s)}号 {board[s]:+d}" for s in range(len(board)))
        if you is not None:
            head += f"；你 {board.get(you, 0):+d}。"
    return head


# ---------------------------------------------------------------- 内核


class TableCore:
    """一场牌的内核；线程安全，UI 只读 state_payload，输入只走 submit/chat。"""

    def __init__(self, config: CoreConfig, ui: SessionUi) -> None:
        self.cfg = config.normalized()
        self.ui = ui
        self.lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._running = False
        self._error = ""
        self._finished = False
        self._paused = False
        self._awaiting_next = False
        self._round_info: dict | None = None
        self.pending_decision: Decision | None = None
        self._pending_since: float | None = None
        self._waiting_seat: int | None = None
        self._waiting_since: float | None = None
        self._waiting_seconds = 0.0

        self.game: Game | None = None
        self.table: Table | None = None
        self.me: int | None = None
        self.buddy: int | None = None
        self.agent: int | None = None
        self.llm = None
        self.talker = None
        self.names: dict[int, str] = {}
        self.plans: list[SeatPlan] = []
        self.log: list[str] = []
        self.said: dict[int, str] = {}
        self.chat_messages: list[dict] = []
        self.board: dict[int, int] = {}
        self.steps = 0
        self.round = 1
        self.rounds = self.cfg.rounds
        self.run_id = ""
        self.snap_dir = self.cfg.snap_root / "unstarted"
        self._ended_early = False
        self._stopped_by_limit = False
        self._last_result: dict | None = None
        self._broadcast = ""
        self._auto_reply = ""
        self._ready = threading.Event()

    # ---------- 生命周期 ----------

    def start(self, *, resume: bool, background: bool = True) -> dict:
        with self.lock:
            if self._thread and self._thread.is_alive():
                return self.status()
            self._stop.clear()
            self._finished = False
            self._error = ""
            self._load_or_new(resume=resume)
            self._running = True
            self._ready.set()
        if background:
            self._thread = threading.Thread(target=self._run_guard, name="mahjong-core", daemon=True)
            self._thread.start()
        return self.status()

    def run_sync(self, *, resume: bool) -> None:
        with self.lock:
            self._load_or_new(resume=resume)
            self._running = True
            self._ready.set()
        self._run_guard()

    def _run_guard(self) -> None:
        try:
            self._loop()
        except EndEarly:
            with self.lock:
                self._ended_early = True
                self._finished = True
                self._broadcast = make_broadcast(
                    self.round, self.rounds, {}, self.board, self.me, True)
                info = {
                    "round": self.round, "rounds": self.rounds, "result": None,
                    "board": {str(s + 1): int(self.board.get(s, 0)) for s in range(4)},
                    "broadcast": self._broadcast, "auto_reply": "",
                    "ended_early": True, "last": True, "steps": self.steps,
                }
                self._round_info = info
                self._safe_persist_locked()
            if self.talker and not self.cfg.no_chat and not self._stop.is_set():
                try:
                    reply = self.talker.chat(self._broadcast, phase="after")
                    if reply:
                        with self.lock:
                            info["auto_reply"] = str(reply)
                            self._auto_reply = str(reply)
                            self.chat_messages.append({"role": "buddy", "phase": "after",
                                                       "text": str(reply)})
                            self._round_info = info
                            self._safe_persist_locked()
                except Exception:
                    pass
            try:
                self.ui.round_end(self, info)
            except Exception:
                pass
        except Exception as exc:                       # noqa: BLE001 - 网页要看见错误
            with self.lock:
                self._error = f"{type(exc).__name__}: {exc}"
                self.log.append("*** 内核出错：" + str(exc))
                self._finished = True
        finally:
            with self.lock:
                self._running = False
                self._ready.set()
            if not self._stop.is_set():
                self._safe_persist()

    def stop(self) -> dict:
        self._stop.set()
        try:
            self.ui.cancel(self)
        except Exception:
            pass
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=2.0)
        with self.lock:
            self._running = False
        return self.status()

    def status(self) -> dict:
        with self.lock:
            t = self._thread
            return {
                "running": bool(self._running and t and t.is_alive()) if t else bool(self._running),
                "finished": bool(self._finished),
                "error": self._error,
                "round": self.round,
                "rounds": self.rounds,
                "steps": self.steps,
            }

    # ---------- 载入 / 新开 ----------

    def _load_or_new(self, *, resume: bool) -> None:
        saved = state_mod.load(self.cfg.state_path) if resume else None
        if resume and saved is None:
            raise RuntimeError(f"没有存档：{self.cfg.state_path}")

        if saved:
            game, meta = saved
            self.game = game
            self._load_meta(meta)
            self.rounds = max(self.rounds, int(meta.get("rounds") or self.rounds))
            self.steps = int(meta.get("steps") or 0)
            self.round = int(meta.get("round") or 1)
            self.board = {int(k): int(v) for k, v in (meta.get("board") or {}).items()}
            self.cfg.agent_wait = max(0.0, float(meta.get("agent_wait") or self.cfg.agent_wait or 0))
            self.cfg.steps = max(0, int(meta.get("step_limit") or self.cfg.steps or 0))
            self.log = list(meta.get("log") or [])
            self.said = {int(k): str(v) for k, v in (meta.get("said") or {}).items() if str(v).strip()}
            self.chat_messages = list(meta.get("chat") or [])
            self._build_table(meta=meta)
            self._restore_llm(meta)
            if game.result is not None or game.phase == "ended":
                self._finished = True
                self._last_result = game.result
                self._broadcast = str(meta.get("broadcast") or "")
            return

        self.round = 1
        self.rounds = self.cfg.rounds
        self.steps = 0
        self.board = {}
        self.log = []
        self.said = {}
        self.chat_messages = []
        self.plans = self._make_plans()
        self._build_table(meta=None)
        dealer = None if self.cfg.dealer is None else seats_mod.index(self.cfg.dealer, 4)
        self.game = Game(GameConfig(
            seats=4, seed=self.cfg.seed, dealer=dealer,
            base_points=self.cfg.base, fan_cap=self.cfg.fan_cap,
        ))
        self.game.start()
        self.run_id = time.strftime("%Y%m%d-%H%M%S")
        self.snap_dir = self.cfg.snap_root / self.run_id
        self.board = {s: 0 for s in range(4)}
        self.log.append(f"—— 第 1 局：{seats_mod.label(self.game.dealer)} 号坐庄 ——")
        if self.llm:
            self.llm.impl.reset_temp_notes()
        self._safe_persist()

    def _make_plans(self) -> list[SeatPlan]:
        if self.cfg.offline:
            base = parse_players(self.cfg.players, 4) if self.cfg.players else plan_from_flags(
                seats=4, human=None if self.cfg.demo else self.cfg.seat,
                buddy=self.cfg.buddy if self.cfg.buddy is not None else (self.cfg.seat % 4 + 1),
                agent=self.cfg.agent)
            return [SeatPlan(p.seat, "random", dict(p.options)) for p in base]
        if self.cfg.players:
            plans = parse_players(self.cfg.players, 4)
        else:
            human = None if self.cfg.demo else self.cfg.seat
            buddy = self.cfg.buddy if self.cfg.buddy is not None else (self.cfg.seat % 4 + 1)
            plans = plan_from_flags(seats=4, human=human, buddy=buddy, agent=self.cfg.agent)
        if self.cfg.agent_wait:
            for p in plans:
                if p.kind == "external":
                    p.options["wait"] = float(self.cfg.agent_wait)
        return plans

    def _plans_from_meta(self, meta: dict) -> list[SeatPlan] | None:
        raw = meta.get("plans")
        if isinstance(raw, list):
            try:
                return [SeatPlan(int(x["seat"]), str(x["kind"])) for x in raw
                        if 1 <= int(x.get("seat", 0)) <= 4]
            except (KeyError, TypeError, ValueError):
                pass
        return None

    def _build_table(self, *, meta: dict | None) -> None:
        if meta is not None and not self.cfg.players:
            saved_plans = self._plans_from_meta(meta)
            if saved_plans:
                self.plans = saved_plans
            else:
                you = int(meta["you"]) if meta.get("you") else None
                buddy = int(meta["buddy"]) if meta.get("buddy") else None
                agent = int(meta["agent"]) if meta.get("agent") else None
                # 老存档里 agent 可能和 you 同号（早期把“你”也写成接入位）；
                # 网页、TUI 的本地座位必须是人，不能把同一张椅子同时当 external。
                if agent == you:
                    agent = None
                self.plans = plan_from_flags(
                    seats=4, human=you, buddy=buddy, agent=agent,
                )
        if not self.plans:
            self.plans = self._make_plans()
        if self.cfg.agent_wait:
            for p in self.plans:
                if p.kind == "external":
                    p.options["wait"] = float(self.cfg.agent_wait)

        rng = random.Random(self.cfg.seed)

        def make_llm(plan: SeatPlan):
            impl = LLMPlayer(StrataClient(), plan.seat, debug_cache=self.cfg.cache)
            from players import LLMSeat
            return LLMSeat(plan.seat, impl)

        def make_human(plan: SeatPlan):
            return LocalSeat(plan.seat, self.ui, self)

        self.table = build_players(
            self.plans, seats=4, llm_factory=make_llm, human_factory=make_human, rng=rng,
        )
        self.names = {i: p.name for i, p in self.table.players.items()}
        self.llm = next(iter(self.table.of_kind("llm")), None)
        self.talker = self.table.talker()
        self.buddy = seats_mod.index(self.llm.seat, 4) if self.llm else None
        humans = self.table.of_kind("human")
        self.me = humans[0].seat - 1 if humans else seats_mod.index(self.cfg.seat, 4)
        externals = self.table.of_kind("external")
        self.agent = externals[0].seat - 1 if externals else None
        for p in self.table.players.values():
            if isinstance(p, ExternalPlayer):
                p.on_wait = self._on_external_wait
                p.cancel_event = self._stop

    def _restore_llm(self, meta: dict) -> None:
        if not (self.llm and meta.get("llm")):
            return
        try:
            msgs = list(meta["llm"].get("messages") or self.llm.impl.messages)
            # 回退/接档时如果牌局还没收，旧上下文里的“终场聊天”必须剪掉，
            # 否则牌友会带着上一支的收场话继续打，还会把“提前结束”记进笔记。
            if self.game is not None and self.game.result is None \
                    and self.game.phase != "ended":
                cut = None
                for i, msg in enumerate(msgs):
                    content = str(msg.get("content") or "")
                    if msg.get("role") == "user" and "【终场聊天】" in content:
                        cut = i
                if cut is not None:
                    msgs = msgs[:cut]
            self.llm.impl.messages = msgs or self.llm.impl.messages
            self.llm.impl._pause_calls = int(meta["llm"].get("pause_calls") or 0)
        except (TypeError, ValueError):
            pass

    def _load_meta(self, meta: dict) -> None:
        self.run_id = str(meta.get("snapshot_run") or "")
        if not self.run_id:
            self.run_id = time.strftime("%Y%m%d-%H%M%S")
        self.snap_dir = self.cfg.snap_root / self.run_id

    # ---------- 主循环 ----------

    def _loop(self) -> None:
        while True:
            if self._stop.is_set():
                return
            game = self.game
            if game is None:
                return
            if self._finished:
                return
            if game.result is not None or game.phase == "ended":
                if not self._finish_round():
                    return
                continue

            with self.lock:
                d = game.pending()
            if d is None:
                if not self._finish_round():
                    return
                continue

            self._snapshot(d)
            # 普通摸牌只有一个动作：内核直接摸，不打扰 UI。
            if d.kind == "draw" and len(d.legal) == 1:
                with self.lock:
                    events = game.apply(Action("draw"))
                    self._note_events(events, d.seat)
                    self.steps += 1
                    self._pending_decision = None
                    self._safe_persist_locked()
                continue

            with self.lock:
                self.pending_decision = d
                self._pending_since = time.monotonic()

            prefetched = False
            if d.seat == self.me and self.llm and not self.cfg.no_prefetch:
                try:
                    self.llm.impl.prefetch_async(game.observe(self.buddy))
                    prefetched = True
                except Exception:
                    pass

            p = self.table.at(d.seat) if self.table else None
            if p is None:
                raise RuntimeError(f"没有 {d.seat + 1} 号座位")
            if isinstance(p, ExternalPlayer):
                p.on_wait = self._on_external_wait
                with self.lock:
                    self._waiting_seat = d.seat
                    self._waiting_since = time.monotonic()
            t0 = time.time()
            try:
                act, speech = p.decide(d)
            finally:
                with self.lock:
                    self._waiting_seat = None
                    self._waiting_since = None
                    self._waiting_seconds = 0.0
                    self.pending_decision = None
            took = time.time() - t0

            if self._stop.is_set():
                return

            if speech and speech.strip():
                with self.lock:
                    self.said[d.seat] = speech.strip()

            if act is None:                         # 要求中场暂停
                with self.lock:
                    self.log.append("牌先停着")
                    self._paused = True
                opener = speech
                actor = p.name
                if opener:
                    with self.lock:
                        self.chat_messages.append({"role": "buddy", "phase": "pause", "text": str(opener)})
                        self._safe_persist_locked()
                try:
                    self.ui.pause(self, actor, opener)
                finally:
                    with self.lock:
                        self._paused = False
                continue

            with self.lock:
                self.log.append(f"{seats_mod.label(d.seat)}号 出手 [{took:.1f}s]")
            if self.cfg.trace:
                try:
                    row = {"seat": d.seat, "name": p.name, "decision": d.kind,
                           "action": act.to_dict(), "seconds": round(took, 2),
                           "say": (speech or "")[:60]}
                    if self.llm and d.seat == self.llm.seat and self.llm.impl.cache_stats:
                        st = self.llm.impl.cache_stats[-1]
                        row.update(prompt=st["prompt"], cached=st["cached"],
                                   read=max(0, st["prompt"] - st["cached"]))
                    Tracer(self.cfg.trace_path).log(**row)
                except Exception:
                    pass

            with self.lock:
                try:
                    events = game.apply(act)
                except Exception as exc:
                    self._error = f"动作被内核拒绝：{exc}"
                    self.log.append("*** " + self._error)
                    # 这一手不合法时不能直接吞掉；让 UI 重新选。
                    continue
                self._note_events(events, d.seat)
                self.steps += 1
                self._safe_persist_locked()

            if self.cfg.steps and self.steps >= self.cfg.steps:
                with self.lock:
                    self._stopped_by_limit = True
                    self.log.append(f"走到第 {self.steps} 步打住")
                break

        if not self._finish_round():
            return

    @property
    def _pending_decision(self):
        return self.pending_decision

    @_pending_decision.setter
    def _pending_decision(self, value):
        self.pending_decision = value

    def _on_external_wait(self, seconds: float) -> None:
        with self.lock:
            self._waiting_seconds = seconds
        try:
            self.ui.tick(self, seconds)
        except Exception:
            pass

    # ---------- 一手结束 / 多局 ----------

    def _finish_round(self) -> bool:
        with self.lock:
            game = self.game
            if game is None:
                return False
            res = game.result or {}
            self._last_result = res
            if res.get("settle"):
                settle = res.get("settle") or {}
                for k, v in (settle.get("payers") or {}).items():
                    self.board[int(k)] = self.board.get(int(k), 0) - int(v)
                if res.get("seat") is not None:
                    winner = int(res["seat"])
                    self.board[winner] = self.board.get(winner, 0) + sum(
                        int(v) for v in (settle.get("payers") or {}).values())
            self._broadcast = make_broadcast(
                self.round, self.rounds, res, self.board, self.me, self._ended_early)
            last = self.round >= self.rounds or self._ended_early or self._stopped_by_limit
            info = {
                "round": self.round,
                "rounds": self.rounds,
                "result": _result_payload(res) if res else None,
                "board": {str(s + 1): int(self.board.get(s, 0)) for s in range(4)},
                "broadcast": self._broadcast,
                "auto_reply": "",
                "ended_early": self._ended_early,
                "last": last,
                "steps": self.steps,
            }
            self._round_info = info
            self._awaiting_next = bool(res and not last)
            self._finished = bool(last)
            self._safe_persist_locked()

        # 牌友的局末播报不拿 core.lock：模型可能想很久，网页轮询不能被卡住。
        if self.talker and not self.cfg.no_chat and (res or self._ended_early) \
                and not self._stop.is_set():
            try:
                phase = "after" if last or self._ended_early else "between"
                reply = self.talker.chat(self._broadcast, phase=phase)
                if reply:
                    with self.lock:
                        info["auto_reply"] = str(reply)
                        self._auto_reply = str(reply)
                        self.chat_messages.append({"role": "buddy", "phase": phase, "text": str(reply)})
                        self._round_info = info
                        self._safe_persist_locked()
            except Exception:
                pass

        if self._ended_early or self._stopped_by_limit:
            try:
                self.ui.round_end(self, info)
            except Exception:
                pass
            with self.lock:
                self._finished = True
            return False

        reply = False
        if not self._stop.is_set():
            try:
                reply = bool(self.ui.round_end(self, info))
            except EndEarly:
                with self.lock:
                    self._ended_early = True
                    self._finished = True
                return False
            except Exception as exc:
                with self.lock:
                    self._error = str(exc)
                    reply = False
        with self.lock:
            self._awaiting_next = False
        if not reply or self.round >= self.rounds:
            with self.lock:
                self._finished = True
            return False

        # 下一局
        with self.lock:
            winner = (self._last_result or {}).get("seat") if self._last_result else None
            nxt = seats_mod.next_dealer(self.game.dealer, winner, 4)
            seed = None if self.cfg.seed is None else self.cfg.seed + self.round
            self.round += 1
            self.game = Game(GameConfig(
                seats=4, seed=seed, dealer=nxt,
                base_points=self.cfg.base, fan_cap=self.cfg.fan_cap,
            ))
            self.game.start()
            self.log.clear()
            self.log.append(f"—— 第 {self.round} 局：{seats_mod.label(nxt)} 号坐庄 ——")
            self.said.clear()
            self._last_result = None
            self._broadcast = ""
            self._auto_reply = ""
            self._round_info = None
            self._awaiting_next = False
            if self.llm:
                self.llm.impl.reset_temp_notes()
            self._safe_persist_locked()
        return True

    # ---------- 持久化 / 快照 ----------

    def _meta_now(self) -> dict:
        return {
            "you": None if self.me is None else seats_mod.label(self.me),
            "buddy": None if self.buddy is None else seats_mod.label(self.buddy),
            "agent": None if self.agent is None else seats_mod.label(self.agent),
            "steps": self.steps,
            "said": {str(k): v for k, v in self.said.items()},
            "log": self.log[-30:],
            "phase": self.game.phase if self.game else "idle",
            "snapshot_run": self.run_id,
            "round": self.round,
            "rounds": self.rounds,
            "board": {str(k): v for k, v in self.board.items()},
            "chat": self.chat_messages[-30:],
            "broadcast": self._broadcast,
            "agent_wait": self.cfg.agent_wait,
            "step_limit": self.cfg.steps,
            "plans": [{"seat": p.seat, "kind": p.kind} for p in self.plans],
            "llm": None if not self.llm else {
                "messages": self.llm.impl.messages,
                "pause_calls": self.llm.impl._pause_calls,
            },
        }

    def _safe_persist(self) -> None:
        with self.lock:
            self._safe_persist_locked()

    def _safe_persist_locked(self) -> None:
        if self.cfg.no_save or self.game is None:
            return
        try:
            self.cfg.state_path.parent.mkdir(parents=True, exist_ok=True)
            state_mod.save(self.cfg.state_path, self.game, self._meta_now())
        except Exception as exc:                         # 不让存盘错误吞掉牌局
            self._error = f"存盘失败：{exc}"

    def _snapshot(self, d: Decision) -> None:
        if self.cfg.no_save:
            return
        try:
            meta = self._meta_now()
            meta["snapshot"] = {"step": self.steps, "next": d.kind,
                                "seat": d.seat + 1, "wall": len(self.game.wall)}
            self.cfg.snap_root.mkdir(parents=True, exist_ok=True)
            self.snap_dir.mkdir(parents=True, exist_ok=True)
            state_mod.save_step(self.snap_dir / f"step_{self.steps:05d}.json", self.game, meta)
            old = sorted(self.snap_dir.glob("step_*.json"))[:-200]
            for p in old:
                p.unlink(missing_ok=True)
        except Exception:
            pass

    def snapshots(self) -> list[dict]:
        if not self.snap_dir.exists():
            return []
        out = []
        for p in sorted(self.snap_dir.glob("step_*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                meta = data.get("meta") or {}
                snap = meta.get("snapshot") or {}
                out.append({
                    "step": int(meta.get("steps", snap.get("step", 0)) or 0),
                    "name": p.name,
                    "path": str(p),
                    "next": snap.get("next"),
                    "seat": snap.get("seat"),
                    "wall": snap.get("wall", len((data.get("game") or {}).get("wall") or [])),
                    "phase": (data.get("game") or {}).get("phase"),
                })
            except Exception:
                continue
        return out

    def rollback_from_snapshot(self, name: str) -> bool:
        """把快照写回 state.json；调用者负责停掉当前内核再重新载入。"""
        p = Path(name)
        if not p.is_absolute():
            p = self.snap_dir / p.name
        if not p.exists():
            return False
        try:
            import shutil
            if self.cfg.state_path.exists():
                shutil.copyfile(self.cfg.state_path, self.cfg.state_path.with_name("state.before-rollback.json"))
            tmp = self.cfg.state_path.with_suffix(".tmp")
            shutil.copyfile(p, tmp)
            tmp.replace(self.cfg.state_path)
            return True
        except Exception:
            return False

    # ---------- UI 输入 ----------

    def submit(self, index: int, say: str = "") -> dict:
        with self.lock:
            d = self.pending_decision
            if d is None or self.game is None:
                return {"ok": False, "message": "现在不是你出牌"}
            if not (0 <= int(index) < len(d.legal)):
                return {"ok": False, "message": "这个动作不在当前可选范围"}
            act = d.legal[int(index)]
            # 把 UI 选项交还给 waiting 的座位
            p = self.table.at(d.seat) if self.table else None
            if not isinstance(p, LocalSeat):
                return {"ok": False, "message": "这一手不在网页/本地座位"}
            if hasattr(self.ui, "answer"):
                self.ui.answer(act, say)
                return {"ok": True, "action": act.to_dict()}
            return {"ok": False, "message": "当前 UI 不能接收动作"}

    def pause_request(self) -> dict:
        if hasattr(self.ui, "request_pause"):
            self.ui.request_pause()
            return {"ok": True}
        return {"ok": False, "message": "当前 UI 不支持暂停"}

    def end_request(self) -> dict:
        if hasattr(self.ui, "request_end"):
            self.ui.request_end()
            return {"ok": True}
        return {"ok": False, "message": "当前 UI 不支持提前结束"}

    def chat(self, text: str, phase: str | None = None) -> dict:
        text = str(text or "").strip()
        if not text:
            return {"ok": False, "message": "消息是空的"}
        if not self.talker or self.cfg.no_chat:
            return {"ok": False, "message": "这桌没有能说话的座位"}
        phase = phase or "pause"
        with self.lock:
            self.chat_messages.append({"role": "user", "phase": phase, "text": text})
            self._safe_persist_locked()
        try:
            reply = self.talker.chat(text, phase=phase)
        except Exception as exc:                       # noqa: BLE001
            return {"ok": False, "message": str(exc)}
        with self.lock:
            if reply:
                self.chat_messages.append({"role": "buddy", "phase": phase, "text": str(reply)})
            self._safe_persist_locked()
        return {"ok": True, "reply": reply or ""}

    def next_round_request(self) -> dict:
        if hasattr(self.ui, "allow_next"):
            self.ui.allow_next()
            return {"ok": True}
        return {"ok": False, "message": "当前 UI 不接下一局"}

    def resume_after_pause(self) -> dict:
        if hasattr(self.ui, "resume_play"):
            self.ui.resume_play()
            return {"ok": True}
        return {"ok": False, "message": "当前 UI 不支持继续"}

    # ---------- 供 UI 读的状态 ----------

    def state_payload(self, *, debug: bool = False, seat: int | None = None) -> dict:
        with self.lock:
            if self.game is None:
                return {"ok": False, "message": "牌桌还没起来", **self.status(),
                        "running": self._running, "updated": None}
            own = own_seat = int(seat or (seats_mod.label(self.me) if self.me is not None else self.cfg.seat))
            own_idx = seats_mod.index(own, 4)
            view = self.game.observe(own_idx)
            if self.pending_decision and self.pending_decision.seat == own_idx:
                view = self.pending_decision.view
            d = self.pending_decision
            hand = list(view.get("hand") or [])
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
            for no in range(1, int(view.get("seats") or 4) + 1):
                idx = no - 1
                seat_melds = []
                for m in (_at(melds, idx, []) or []):
                    if isinstance(m, dict):
                        seat_melds.append({"kind": m.get("kind"), "tiles": list(m.get("tiles") or [])})
                    else:
                        seat_melds.append({"kind": None, "tiles": list(m or [])})
                if debug and idx != own_idx:
                    other_hand = list(self.game.observe(idx).get("hand") or [])
                elif idx == own_idx:
                    other_hand = hand
                else:
                    other_hand = None
                seats.append({
                    "no": no,
                    "name": self.names.get(idx, f"{no}号"),
                    "kind": (self.table.at(idx).kind if self.table else "random"),
                    "is_me": no == own,
                    "hand_count": int(_at(hand_sizes, idx, 0) or 0),
                    "hand": other_hand if debug or no == own else None,
                    "melds": seat_melds,
                    "discards": list(_at(discards, idx, []) or []),
                    "ting": bool(_at(ting, idx, False)),
                    "piao": bool(_at(piao, idx, False)),
                    "ting_closed": bool(_at(ting_closed, idx, False)),
                    "kans": int(_at(kans, idx, 0) or 0),
                    "dead": bool(_at(dead, idx, False)),
                    "dealer": dealer is not None and int(dealer) == idx,
                })

            legal = d.legal if (d and d.seat == own_idx) else []
            actions = []
            for i, a in enumerate(legal):
                rec = a.to_dict()
                rec["index"] = i
                rec["label"] = action_label(rec)
                actions.append(rec)

            said = []
            for k, v in sorted(self.said.items()):
                try:
                    sn = seat_no(k)
                except (TypeError, ValueError):
                    sn = None
                if sn is not None and str(v).strip():
                    said.append({"seat": sn, "name": self.names.get(int(k), ""), "text": str(v)})

            pending = None
            if self.pending_decision:
                pending = {"seat": seats_mod.label(self.pending_decision.seat),
                           "kind": self.pending_decision.kind}
                turn = self.pending_decision.seat
            elif self.game.pending_peek():
                peek = self.game.pending_peek()
                pending = {"seat": seats_mod.label(peek.seat), "kind": peek.kind}
                turn = peek.seat

            return {
                "ok": True,
                "running": bool(self._running),
                "finished": bool(self._finished),
                "error": self._error,
                "debug": bool(debug),
                "own_seat": own,
                "seats": seats,
                "hand": hand,
                "hand_count": len(hand),
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
                "pending": pending,
                "actions": actions,
                "result": result,
                "steps": int(self.steps),
                "round": int(self.round),
                "rounds": int(self.rounds),
                "board": {str(s + 1): int(self.board.get(s, 0)) for s in range(4)},
                "paused": bool(self._paused),
                "awaiting_next": bool(self._awaiting_next),
                "round_info": self._round_info,
                "broadcast": self._broadcast,
                "chat": list(self.chat_messages[-30:]),
                "chat_busy": False,
                "can_chat": bool(self.talker and not self.cfg.no_chat),
                "talker": self.names.get(self.buddy, "") if self.buddy is not None else "",
                "said": said[-4:],
                "log": list(self.log[-12:]),
                "waiting": {
                    "seat": None if self._waiting_seat is None else seats_mod.label(self._waiting_seat),
                    "seconds": round(float(self._waiting_seconds or 0), 1),
                } if self._waiting_seat is not None else None,
                "run_id": self.run_id,
                "config": {
                    "players": self.cfg.players, "seat": self.cfg.seat,
                    "dealer": self.cfg.dealer, "base": self.cfg.base,
                    "fan_cap": self.cfg.fan_cap, "rounds": self.rounds,
                    "seed": self.cfg.seed, "offline": self.cfg.offline,
                    "no_chat": self.cfg.no_chat,
                    "steps": self.cfg.steps, "agent_wait": self.cfg.agent_wait,
                },
                "updated": time.strftime("%H:%M:%S", time.localtime(
                    self.cfg.state_path.stat().st_mtime)) if self.cfg.state_path.exists() else None,
            }

    def summary(self) -> dict:
        with self.lock:
            return {
                "round": self.round, "rounds": self.rounds, "steps": self.steps,
                "board": {str(s + 1): self.board.get(s, 0) for s in range(4)},
                "finished": self._finished, "error": self._error,
                "last_result": _result_payload(self._last_result),
                "llm_records": len(self.llm.impl.records) if self.llm else 0,
                "llm_fallback": sum(1 for r in self.llm.impl.records if r.used_fallback) if self.llm else 0,
                "cache_report": self.llm.impl.cache_report() if self.llm and self.cfg.cache else "",
            }

    # ---------- 事件日志 ----------

    def _note_events(self, events, me_idx: int) -> None:
        for e in events:
            t = e.type
            name = tile_label(tile_id(e.tile)) if e.tile is not None else ""
            if t == "draw" and e.seat == me_idx:
                self.log.append(f"你摸到 {name}")
            elif t == "kan_draw" and e.seat == me_idx:
                self.log.append(f"你补张 {name}")
            elif t == "discard":
                self.log.append(f"{seats_mod.label(e.seat)}号 打 {name}")
            elif t == "pon":
                self.log.append(f"{seats_mod.label(e.seat)}号 碰 {name}")
            elif t == "chi":
                self.log.append(f"{seats_mod.label(e.seat)}号 吃 " + "+".join(
                    tile_label(x) for x in e.data.get("use", [])))
            elif t == "kan":
                label = {"in_hand": "明杠", "added": "加杠"}.get((e.data or {}).get("kind"), "杠")
                self.log.append(f"{seats_mod.label(e.seat)}号 {label} {name}")
            elif t == "ting":
                self.log.append(f"{seats_mod.label(e.seat)}号 报听")
            elif t == "bloom":
                self.log.append(f"{seats_mod.label(e.seat)}号 后开花 {name}")
            elif t == "dead":
                self.log.append(f"{seats_mod.label(e.seat)}号 放过胡，本局不准再胡")
            elif t == "win":
                tag = "抢杠" if (e.data or {}).get("rob") else ("后开花" if (e.data or {}).get("bloom") else "")
                self.log.append(f"*** {seats_mod.label(e.seat)}号 {tag}胡了！")
            elif t == "draw_game":
                self.log.append("*** 荒庄：牌墙摸空")
        del self.log[:-30]
