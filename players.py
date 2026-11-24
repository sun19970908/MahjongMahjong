"""座位系统：每个座位就是一个 Player，牌局只认这个接口，不认具体是谁。

    HumanPlayer    键盘
    LLMSeat        牌友（内部包着 llm_player.LLMPlayer）
    RandomPlayer   随机
    ExternalPlayer 外部接入（文件信箱，谁都能用；我坐座位时也走这条路）

统一接口只有两件事：

    decide(d) -> (Action | None, 说的话)
        返回 None 表示"这一手我不打，要求中场暂停"。
    chat(text, phase=...) -> str | None
        支持说话的人（牌友）才实现；不支持就返回 None。

外部接入的协议就是 Action/Decision 的序列化，不另造一套：
    seats/seat2/pending.json   ← 牌局写：这一手该你
    seats/seat2/answer.json    ← 外部写：我打这张
    seats/seat2/give-up        ← 外部写：这手我不想了，随机代打
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

import seats as seats_mod
from game import Action, Decision, random_policy
from referee import parse_tile, tile_id, tile_name

DEFAULT_SEAT_ROOT = Path(__file__).resolve().parent / "seats"

# 玩家类型：别在 play.py 里写死"几号是什么人"，都由这张表 + 座位计划决定
KINDS = ("human", "llm", "external", "random")


def tile_label(tid: str | None) -> str:
    """统一牌面中文名（m1 -> 1万，z5 -> 红中）。"""
    if not tid:
        return "?"
    try:
        return tile_name(parse_tile(tid))
    except (ValueError, TypeError):
        return str(tid)


def action_label(a: dict) -> str:
    """把 Action 字典翻成中文短标签（用于 pending / 网页 / TUI）。"""
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


def write_atomic_json(path: Path, obj: dict) -> None:
    """原子写 JSON，兼容 Windows 下多线程/多进程瞬时读锁重试。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}.{threading.get_ident()}")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    for i in range(8):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == 7:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise
            time.sleep(0.015)


def unlink_safe(path: Path) -> None:
    """Windows 下兼容瞬时并发读锁的安全删档。"""
    for i in range(8):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            if i == 7:
                return
            time.sleep(0.015)
        except OSError:
            return


def read_json_safe(path: Path) -> tuple[dict | None, str | None]:
    """容错读取 JSON：处理并发删除/瞬时锁重试，返回 (dict, error_reason)。"""
    for i in range(3):
        try:
            raw = path.read_text(encoding="utf-8")
            if not raw.strip():
                time.sleep(0.02)
                continue
            data = json.loads(raw)
            if not isinstance(data, dict):
                return None, "JSON 顶层必须是对象"
            return data, None
        except FileNotFoundError:
            return None, None
        except PermissionError:
            if i == 2:
                return None, "文件正被占用"
            time.sleep(0.02)
        except json.JSONDecodeError as exc:
            if i < 2:
                time.sleep(0.02)
                continue
            return None, f"JSON 解析失败：{exc}"
    return None, "文件为空"


class Player(Protocol):
    name: str
    def decide(self, d: Decision) -> tuple[Action | None, str]: ...
    def chat(self, text: str, *, phase: str = "pause") -> str | None: ...
    def supports_talk(self) -> bool: ...


class BasePlayer:
    name = "玩家"
    supports = "player"

    def __init__(self, seat: int) -> None:
        self.seat = seat            # 座位号（1 起），不是引擎下标

    @property
    def kind(self) -> str:
        """玩家类型：human / llm / external / random，交给装配层看。"""
        return self.supports

    def decide(self, d: Decision) -> tuple[Action | None, str]:
        raise NotImplementedError

    def chat(self, text: str, *, phase: str = "pause") -> str | None:
        return None

    def supports_talk(self) -> bool:
        return False


class EndEarly(Exception):
    """停止牌桌时叫醒阻塞输入；不是“提前结束上下文”。"""


class HumanPlayer(BasePlayer):
    """你：键盘输入。打 /p 就是要求中场暂停。"""

    def __init__(self, seat: int) -> None:
        super().__init__(seat)
        self.name = f"{seat}号（你）"
        self.supports = "human"

    def decide(self, d: Decision) -> tuple[Action | None, str]:
        opts = d.legal
        while True:
            try:
                raw = input(" > ").strip()
            except EOFError:
                raise SystemExit("\n退了。")
            if raw in ("q", "quit"):
                raise SystemExit("退了。")
            if raw in ("/p", "p", "暂停"):
                return None, ""
            if raw.isdigit():
                n = int(raw)
                has_discard = any(a.kind == "discard" for a in opts)
                if d.kind == "discard" and has_discard:
                    # 出牌阶段的序号是手牌位置；杠/胡这类非出牌动作排在
                    # 手牌位置之后，避免重复牌被压掉后九条从 11 变成 9。
                    hand = list((d.view or {}).get("hand") or [])
                    if 1 <= n <= len(hand):
                        tid = parse_tile(hand[n - 1])
                        for a in opts:
                            if a.kind == "discard" and parse_tile(a.tile) == tid:
                                return a, ""
                        print(f" 第 {n} 张现在不能打（报听后只能打摸进来的那张）")
                        continue
                    others = [a for a in opts if a.kind != "discard"]
                    j = n - len(hand) - 1
                    if 0 <= j < len(others):
                        return others[j], ""
                    print(f" 没有第 {n} 号选项")
                    continue
                if 1 <= n <= len(opts):
                    return opts[n - 1], ""


class RandomPlayer(BasePlayer):
    def __init__(self, seat: int, rng: random.Random) -> None:
        super().__init__(seat)
        self.policy = random_policy(rng)
        self.name = f"{seat}号（随机）"
        self.supports = "random"

    def decide(self, d: Decision) -> tuple[Action | None, str]:
        return self.policy(d), ""


class LLMSeat(BasePlayer):
    """他：把 llm_player.LLMPlayer 包成座位，接口和其他人一模一样。"""

    def __init__(self, seat: int, impl) -> None:
        super().__init__(seat)
        self.impl = impl
        display = getattr(impl, "name", "") or f"{seat}号"
        self.name = f"{display}（{seat}号）" if display != f"{seat}号" else f"{seat}号（牌友）"
        self.supports = "llm"

    def decide(self, d: Decision) -> tuple[Action | None, str]:
        return self.impl.decide(d)

    def chat(self, text: str, *, phase: str = "pause") -> str | None:
        self.impl.set_phase(phase)
        return self.impl.chat(text, phase=phase)

    def supports_talk(self) -> bool:
        return True


class ExternalPlayer(BasePlayer):
    """外部接入：写一份 pending，等一份 answer。

    默认死等（不设超时，按你的口味）；想放手就写 give-up，这一手交随机。
    """

    def __init__(self, seat: int, root: Path | str | None = None, poll: float = 0.05,
                 wait: float = 0.0, on_wait: Callable[[float], None] | None = None,
                 cancel_event=None, name: str = "", chat_wait: float = 60.0,
                 table_id: str = "") -> None:
        super().__init__(seat)
        self.dir = Path(root or DEFAULT_SEAT_ROOT) / seats_mod.seat_dir(seat)
        self.dir.mkdir(parents=True, exist_ok=True)
        label = str(name or "").strip()
        self.name = f"{label}（{seat}号）" if label else f"{seat}号（接入）"
        self.supports = "external"
        self.poll = poll
        self.wait = wait
        self.chat_wait = chat_wait
        self.round = 0
        self.table_id = table_id
        self._event_seq = self._scan_event_seq()
        self._chat_seq = 0
        self._turn_context: dict = {}
        self.on_wait = on_wait
        self.cancel_event = cancel_event

    # ---- 文件 ----
    def _pending_path(self) -> Path:
        return self.dir / "pending.json"

    def _answer_path(self) -> Path:
        return self.dir / "answer.json"

    def _error_path(self) -> Path:
        return self.dir / "error.json"

    def _status_path(self) -> Path:
        return self.dir / "status.json"

    def _events_path(self) -> Path:
        return self.dir / "events.jsonl"

    def _chat_pending_path(self) -> Path:
        return self.dir / "chat_pending.json"

    def _chat_answer_path(self) -> Path:
        return self.dir / "chat_answer.json"

    def _chat_ready_path(self) -> Path:
        return self.dir / "chat_ready.json"

    def _talk_path(self, which: str) -> Path:
        return self.dir / f"talk_{which}.jsonl"

    @staticmethod
    def _write_atomic(path: Path, obj: dict) -> None:
        write_atomic_json(path, obj)

    def _scan_event_seq(self) -> int:
        p = self._events_path()
        if not p.exists():
            return 0
        seq = 0
        try:
            for raw in p.read_text(encoding="utf-8").splitlines():
                if not raw.strip():
                    continue
                try:
                    row = json.loads(raw)
                    seq = max(seq, int(row.get("seq") or 0))
                except (ValueError, TypeError):
                    continue
        except OSError:
            return 0
        return seq

    def set_turn_context(self, *, table: dict | None = None,
                         ting_hints: list[dict] | None = None,
                         recent_events: list[str] | None = None) -> None:
        """由内核在 decide 前注入本手公共桌面信息与听牌提醒（绝不含别家暗手）。"""
        self._turn_context = {
            "table": table or {},
            "ting_hints": list(ting_hints or []),
            "recent_events": list(recent_events or []),
        }

    def begin_session(self, table_id: str, *, reset_events: bool = False,
                      game_round: int | None = None, lap: int | None = None,
                      steps: int | None = None) -> None:
        """开新桌或接档时清理残留信箱文件，同步 table_id 与事件序号。"""
        self.table_id = str(table_id or "")
        for p in (self._pending_path(), self._answer_path(), self._error_path(),
                  self.dir / "give-up", self._chat_pending_path(), self._chat_answer_path()):
            unlink_safe(p)
        if reset_events:
            unlink_safe(self._events_path())
            self._event_seq = 0
        else:
            max_seq = 0
            try:
                for raw in self._events_path().read_text(encoding="utf-8").splitlines():
                    try:
                        max_seq = max(max_seq, int(json.loads(raw).get("seq") or 0))
                    except (ValueError, TypeError, json.JSONDecodeError, AttributeError):
                        continue
            except OSError:
                pass
            self._event_seq = max_seq
        self.set_status("playing", game_round=game_round, lap=lap, steps=steps, waiting_on=None)

    def set_status(self, state: str, *, game_round: int | None = None,
                   lap: int | None = None, steps: int | None = None,
                   pending_round: int | None = None, chat_id: str | None = None,
                   error: str | None = None, waiting_on: dict | None = None) -> None:
        prev, _ = read_json_safe(self._status_path())
        prev = prev or {}
        payload = {
            "seat": self.seat,
            "name": self.name,
            "table_id": self.table_id,
            "state": state,
            "game_round": game_round if game_round is not None else prev.get("game_round"),
            "lap": lap if lap is not None else prev.get("lap"),
            "steps": steps if steps is not None else prev.get("steps"),
            "pending_round": pending_round,
            "chat_pending_id": chat_id,
            "error": error,
            "waiting_on": waiting_on,
            "event_seq": self._event_seq,
            "updated_at": time.strftime("%H:%M:%S"),
        }
        try:
            self._write_atomic(self._status_path(), payload)
        except OSError:
            pass

    def emit_event(self, event_type: str, data: dict | None = None) -> dict:
        self._event_seq += 1
        row = {
            "seq": self._event_seq,
            "type": event_type,
            "table_id": self.table_id,
            "seat": self.seat,
            "at": time.strftime("%H:%M:%S"),
            **(data or {}),
        }
        try:
            p = self._events_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            lines = []
            try:
                lines = p.read_text(encoding="utf-8").splitlines()
            except OSError:
                lines = []
            lines.append(json.dumps(row, ensure_ascii=False))
            if len(lines) > 500:
                lines = lines[-500:]
            tmp = p.with_suffix(p.suffix + f".tmp.{os.getpid()}.{threading.get_ident()}")
            tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
            for i in range(8):
                try:
                    os.replace(tmp, p)
                    break
                except PermissionError:
                    if i == 7:
                        unlink_safe(tmp)
                        raise
                    time.sleep(0.015)
        except OSError:
            pass
        return row

    def cleanup(self, state: str = "stopped") -> None:
        """停桌、回退或结束时清掉过期 pending，防止下一次启动误读。"""
        for p in (self._pending_path(), self._error_path(), self._chat_pending_path()):
            unlink_safe(p)
        self.set_status(state, pending_round=None, chat_id=None, error=None)

    def _write_error(self, message: str, *, got_round=None) -> None:
        err = {
            "round": self.round,
            "got_round": got_round,
            "seat": self.seat,
            "error": message,
            "at": time.strftime("%H:%M:%S"),
        }
        self._write_atomic(self._error_path(), err)
        self.set_status("waiting_decision", pending_round=self.round, error=message)

    def decide(self, d: Decision) -> tuple[Action | None, str]:
        self.round += 1
        ctx = self._turn_context or {}
        self._turn_context = {}
        table_meta = dict(ctx.get("table") or {})
        legal_rows = []
        for idx, a in enumerate(d.legal, 1):
            row = a.to_dict()
            row["pick"] = idx
            row["label"] = action_label(row)
            legal_rows.append(row)

        req = {
            "round": self.round,
            "table_id": self.table_id or str(table_meta.get("run_id") or ""),
            "seat": self.seat,
            "name": self.name,
            "kind": d.kind,
            "forced": len(d.legal) == 1,
            "table": table_meta,
            "recent_events": list(ctx.get("recent_events") or []),
            "view": d.view,
            "ting_hints": list(ctx.get("ting_hints") or []),
            "legal": legal_rows,
            "at": time.strftime("%H:%M:%S"),
        }
        for p in (self._answer_path(), self._error_path(), self.dir / "give-up"):
            unlink_safe(p)
        self._write_atomic(self._pending_path(), req)
        self.set_status(
            "waiting_decision",
            game_round=table_meta.get("round"),
            lap=table_meta.get("lap"),
            steps=table_meta.get("steps"),
            pending_round=self.round,
            error=None,
        )

        t0 = time.monotonic()
        beat = t0
        while True:
            if self.cancel_event is not None and self.cancel_event.is_set():
                unlink_safe(self._pending_path())
                unlink_safe(self._error_path())
                raise EndEarly()
            if (self.dir / "give-up").exists():
                unlink_safe(self.dir / "give-up")
                unlink_safe(self._pending_path())
                unlink_safe(self._error_path())
                self.set_status("playing", pending_round=None)
                return random_policy(random.Random())(d), "（这手不想了，随机代打）"
            if self._answer_path().exists():
                ans, read_err = read_json_safe(self._answer_path())
                unlink_safe(self._answer_path())
                if read_err or ans is None:
                    self._write_error(read_err or "answer.json 内容无效")
                    time.sleep(self.poll)
                    continue
                matched, is_pause, say, err = resolve_external_answer(
                    ans, d.legal, expected_round=self.round)
                if err:
                    self._write_error(err, got_round=ans.get("round"))
                    time.sleep(self.poll)
                    continue
                unlink_safe(self._pending_path())
                unlink_safe(self._error_path())
                self.set_status("playing", pending_round=None, error=None)
                if is_pause:
                    return None, say
                act = action_from_dict(matched) if isinstance(matched, dict) else matched
                if act is not None:
                    return act, say
                self._write_error("动作解析失败")
            if self.wait and time.monotonic() - t0 > self.wait:
                unlink_safe(self._pending_path())
                unlink_safe(self._error_path())
                self.set_status("playing", pending_round=None)
                return random_policy(random.Random())(d), "（等太久，随机代打）"
            if self.on_wait and time.monotonic() - beat >= 1.0:   # 每秒报一次，TUI 上那个秒数在走=还活着
                beat = time.monotonic()
                self.on_wait(beat - t0)
            time.sleep(self.poll)

    # ---- 聊天支持（暂停 / 局间） ----
    def supports_talk(self) -> bool:
        return True

    def _talk_rows(self, which: str) -> list[dict]:
        p = self._talk_path(which)
        if not p.exists():
            return []
        rows = []
        try:
            for raw in p.read_text(encoding="utf-8").splitlines():
                try:
                    rows.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
        except OSError:
            pass
        return rows

    def _append_talk(self, which: str, text: str, *, phase: str = "") -> None:
        p = self._talk_path(which)
        p.parent.mkdir(parents=True, exist_ok=True)
        row = {"t": time.strftime("%H:%M:%S"), "text": text}
        if phase:
            row["phase"] = phase
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _is_chat_listener_ready(self) -> bool:
        data, _ = read_json_safe(self._chat_ready_path())
        if not data:
            return False
        try:
            return time.time() - float(data.get("ts") or 0) <= 120.0
        except (TypeError, ValueError):
            return False

    def chat(self, text: str, *, phase: str = "pause") -> str | None:
        self._chat_seq += 1
        cid = f"{self.round}_{self._chat_seq}_{int(time.time() * 1000) % 100000}"
        self._append_talk("in", text, phase=phase)
        out_base = len(self._talk_rows("out"))
        unlink_safe(self._chat_answer_path())
        self._write_atomic(self._chat_pending_path(), {
            "id": cid,
            "table_id": self.table_id,
            "seat": self.seat,
            "name": self.name,
            "phase": phase,
            "text": text,
            "at": time.strftime("%H:%M:%S"),
        })
        self.set_status("chatting", chat_id=cid)
        limit = self.chat_wait if self._is_chat_listener_ready() else min(self.chat_wait, 8.0)
        t0 = time.monotonic()
        try:
            while time.monotonic() - t0 <= limit:
                if self.cancel_event is not None and self.cancel_event.is_set():
                    return None
                if self._chat_answer_path().exists():
                    ans, _ = read_json_safe(self._chat_answer_path())
                    unlink_safe(self._chat_answer_path())
                    if ans:
                        ans_id = ans.get("id")
                        reply = str(ans.get("text") or ans.get("reply") or "").strip()
                        if (ans_id is None or str(ans_id) == cid) and reply:
                            self._append_talk("out", reply, phase=phase)
                            return reply
                out_rows = self._talk_rows("out")
                if len(out_rows) > out_base:
                    reply = str(out_rows[-1].get("text") or "").strip()
                    if reply:
                        return reply
                time.sleep(0.1)
            return None
        finally:
            unlink_safe(self._chat_pending_path())
            unlink_safe(self._chat_answer_path())
            self.set_status("playing", chat_id=None)


def action_from_dict(payload: dict) -> Action | None:
    """外部递进来的动作。看不懂就返回 None，当作没答。

    meta 必须原样带回去：加杠 / 明杠 / 从谁那儿，全靠它，丢了引擎就判非法。
    """
    try:
        kind = str(payload.get("kind") or "").strip().lower()
        meta = dict(payload.get("meta") or {})
        if kind in ("discard", "pon", "kan"):
            return Action(kind, tile=parse_tile(payload.get("tile")), meta=meta)
        if kind == "chi":
            raw_combo = payload.get("combo") or []
            if isinstance(raw_combo, str):
                raw_combo = [x for x in raw_combo.replace("+", " ").split() if x]
            return Action("chi", combo=tuple(parse_tile(t) for t in raw_combo), meta=meta)
        if kind in ("win", "pass", "ting", "bloom", "draw"):
            return Action(kind, meta=meta)
    except (ValueError, TypeError):
        return None
    return None


def _legal_item_action(item) -> Action | None:
    if isinstance(item, Action):
        return item
    if isinstance(item, dict):
        return action_from_dict(item)
    return None


def resolve_external_answer(
    ans: dict,
    legal: Sequence[Action] | Sequence[dict],
    *,
    expected_round: int | None = None,
) -> tuple[dict | None, bool, str, str | None]:
    """统一校验 external 的回答（供 ExternalPlayer / seat_io / webui 共用）。

    返回 (cleaned_action_dict | None, is_pause, say, error_message)。
    - 支持 {"round": R, "pick": N, "say": "..."}（1 基下标，完整保留 meta）
    - 支持 {"round": R, "action": {"kind": "pause"}, "say": "..."}
    - 支持 {"round": R, "action": {...}, "say": "..."}，缺省 meta 时若在 legal 里唯一匹配则自动补全。
    """
    if not isinstance(ans, dict):
        return None, False, "", "回答必须是 JSON 对象"
    if expected_round is not None:
        try:
            got_round = int(ans.get("round") if ans.get("round") is not None else -1)
        except (TypeError, ValueError):
            return None, False, "", f"round 格式无效：{ans.get('round')!r}"
        if got_round != expected_round:
            return None, False, "", f"round 不匹配：收到 {got_round}，当前是 {expected_round}"

    say = str(ans.get("say") or "")
    legal_acts: list[Action] = []
    for item in legal:
        act = _legal_item_action(item)
        if act is not None:
            legal_acts.append(act)

    pick = ans.get("pick")
    if pick is not None and pick != "":
        try:
            idx = int(pick)
        except (TypeError, ValueError):
            return None, False, say, f"pick 必须是整数，收到 {pick!r}"
        if not (1 <= idx <= len(legal_acts)):
            return None, False, say, f"pick={idx} 超出范围（当前可选 1..{len(legal_acts)}）"
        return legal_acts[idx - 1].to_dict(), False, say, None

    raw_payload = ans.get("action")
    if isinstance(raw_payload, str):
        raw_payload = {"kind": raw_payload}
    if not isinstance(raw_payload, dict) or not raw_payload.get("kind"):
        return None, False, say, "缺少 pick 或 action.kind"

    if str(raw_payload.get("kind") or "").strip().lower() == "pause":
        return {"kind": "pause"}, True, say, None

    act = action_from_dict(raw_payload)
    if act is None:
        return None, False, say, f"无法解析动作：{raw_payload!r}"

    has_meta = bool(raw_payload.get("meta"))
    matches: list[tuple[int, Action]] = []
    for idx, cand in enumerate(legal_acts, 1):
        if cand.kind != act.kind:
            continue
        if act.kind in ("discard", "pon", "kan") and cand.tile != act.tile:
            continue
        if act.kind == "chi" and tuple(sorted(cand.combo)) != tuple(sorted(act.combo)):
            continue
        if has_meta and dict(cand.meta or {}) != dict(act.meta or {}):
            continue
        matches.append((idx, cand))

    if len(matches) == 1:
        return matches[0][1].to_dict(), False, say, None
    if len(matches) > 1:
        picks = "、".join(f"--pick {i} ({action_label(c.to_dict())})" for i, c in matches)
        return None, False, say, f"动作有多种可能，请改用具体序号：{picks}"

    avail = "、".join(f"{i}.{action_label(c.to_dict())}" for i, c in enumerate(legal_acts, 1))
    return None, False, say, f"当前不能做 {action_label(act.to_dict())}（可选：{avail}）"


def _legal(d: Decision, act: Action) -> bool:
    """递上来的动作必须是这一手能做的，且若携带 meta 必须一致。"""
    for a in d.legal:
        if a.kind != act.kind:
            continue
        if act.meta and dict(a.meta or {}) != dict(act.meta or {}):
            continue
        if a.kind in ("discard", "pon", "kan") and a.tile == act.tile:
            return True
        if a.kind == "chi" and tuple(sorted(a.combo)) == tuple(sorted(act.combo)):
            return True
        if a.kind in ("win", "pass", "ting", "bloom", "draw"):
            return True
    return False


class Tracer:
    """每一步一行 JSONL：谁、做了什么、多久、模型侧读了多少。只记账，不看牌。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, **row) -> None:
        row.setdefault("t", time.strftime("%H:%M:%S"))
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- 座位计划
# 一桌怎么坐，全由 SeatPlan 说；play.py 不再认识具体类型，只管问 Table 要人。


@dataclass(frozen=True)
class SeatPlan:
    """一个座位：坐第几号（1 起）、坐的是哪种玩家、各自的参数。"""

    seat: int                               # 座位号（1 起，显示用）
    kind: str = "random"                    # human / llm / external / random
    options: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"没这种玩家：{self.kind}（只有 {'/'.join(KINDS)}）")
        if self.seat < 1:
            raise ValueError(f"座位号从 1 起，收到 {self.seat}")


def _read_player_spec(spec: str) -> tuple[str, dict]:
    """读一个玩家配置：kind 或 kind:玩家名。"""
    kind, _, profile = spec.partition(":")
    kind = kind.strip()
    if kind not in KINDS and not profile.strip():
        # 只写玩家名，默认按牌友处理
        return "llm", {"profile": kind}
    opts = {"profile": profile.strip()} if profile.strip() else {}
    return kind, opts


def parse_players(text: str, seats: int = 4) -> list[SeatPlan]:
    """把命令行的玩家配置读成计划。

    没写座位的项一律随机落座：
    - "human,external,llm:夏栀,llm:方小满"：四个都随机
    - "2=llm:夏栀,3=external"：2/3 点名，剩下的空位随机补
    """
    spec = (text or "").strip()
    if not spec:
        return [SeatPlan(s + 1, "random") for s in range(seats)]
    assigns: dict[int, tuple[str, dict]] = {}
    pending: list[str] = []
    items = [x.strip() for x in spec.split(",") if x.strip()]
    if "=" in spec:
        for item in items:
            if "=" not in item:
                pending.append(item)
                continue
            left, _, raw = item.partition("=")
            _check_seat(int(left.strip()), seats)
            assigns[int(left.strip())] = _read_player_spec(raw)
    else:
        pending = items

    free = [s for s in range(1, seats + 1) if s not in assigns]
    random.shuffle(free)
    if len(pending) > len(free):
        raise ValueError(f"没座位的玩家有 {len(pending)} 个，只剩 {len(free)} 个空位")
    for item in pending:
        kind, opts = _read_player_spec(item)
        seat = free.pop(0)
        assigns[seat] = (kind, opts)

    plans: list[SeatPlan] = []
    for s in range(seats):
        kind, opts = assigns.get(s + 1, ("random", {}))
        plans.append(SeatPlan(s + 1, kind, opts))
    return plans


def _check_seat(seat: int | None, seats: int) -> None:
    if seat is not None and not 1 <= seat <= seats:
        raise ValueError(f"没这个座位：{seat}（这桌只有 1…{seats} 号）")


def plan_from_flags(*, seats: int = 4, human: int | None = None,
                    buddy: int | None = None, agent: int | None = None) -> list[SeatPlan]:
    """老参数（--seat / --buddy / --agent）也能读成计划，默认谁都没坐就全随机。"""
    for v in (human, buddy, agent):
        _check_seat(v, seats)
    kinds: dict[int, str] = {}
    if human is not None:
        kinds[human] = "human"
    if buddy is not None:
        kinds[buddy] = "llm"
    if agent is not None:
        kinds[agent] = "external"
    return [SeatPlan(s + 1, kinds.get(s + 1, "random")) for s in range(seats)]


class Table:
    """一桌人：座位号 → 玩家。引擎只按 0 基下标要人，换算都在这层下面。"""

    def __init__(self, players: list[BasePlayer], seats: int = 4) -> None:
        self.players = {seats_mod.index(p.seat, seats): p for p in players}
        self.seats = seats

    def at(self, index: int) -> BasePlayer:
        return self.players[index]

    def names(self) -> dict[int, str]:
        """下标 → 显示名，给画面用。"""
        return {i: p.name for i, p in self.players.items()}

    def of_kind(self, kind: str) -> list[BasePlayer]:
        return [p for p in self.players.values() if p.kind == kind]

    def chat_targets(self) -> list[BasePlayer]:
        """能聊天的座位，不限 LLM；以后接真人只要实现 supports_talk/chat。"""
        return [p for p in self.players.values() if p.supports_talk()]

    def talker(self) -> BasePlayer | None:
        """默认聊天对象：第一个能聊的座位。"""
        targets = self.chat_targets()
        return targets[0] if targets else None


def build_players(plans: list[SeatPlan], *, seats: int = 4,
                  llm_factory: Callable[[SeatPlan], BasePlayer] | None = None,
                  human_factory: Callable[[SeatPlan], BasePlayer] | None = None,
                  rng: random.Random | None = None) -> Table:
    """按计划摆人。别的类型自己会造，llm 那张椅子由外面递进来的工厂盖（要网、要模型）。

    human 也一样：TUI 用键盘、网页用 HTTP，内核不认具体 UI，只认外面递进来的
    human_factory；不传时才退回 players.HumanPlayer（兼容老脚本）。
    """
    made: list[BasePlayer] = []
    for plan in plans:
        if plan.kind == "human":
            made.append(human_factory(plan) if human_factory else HumanPlayer(plan.seat))
        elif plan.kind == "llm":
            if llm_factory is None:
                raise RuntimeError("这桌要坐牌友，但没给造牌友的工厂")
            made.append(llm_factory(plan))
        elif plan.kind == "external":
            made.append(ExternalPlayer(
                plan.seat,
                wait=float(plan.options.get("wait", 0)),
                root=plan.options.get("root"),
                name=str(plan.options.get("profile") or "").strip(),
            ))
        else:
            made.append(RandomPlayer(plan.seat, rng=rng))
    return Table(made, seats)
