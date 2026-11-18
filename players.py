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
import time
from pathlib import Path
from typing import Protocol

from game import Action, Decision, random_policy
from referee import parse_tile

DEFAULT_SEAT_ROOT = Path(__file__).resolve().parent / "seats"


class Player(Protocol):
    name: str
    def decide(self, d: Decision) -> tuple[Action | None, str]: ...
    def chat(self, text: str, *, phase: str = "pause") -> str | None: ...
    def supports_talk(self) -> bool: ...


class BasePlayer:
    name = "玩家"
    supports = "player"

    def __init__(self, seat: int) -> None:
        self.seat = seat

    def decide(self, d: Decision) -> tuple[Action | None, str]:
        raise NotImplementedError

    def chat(self, text: str, *, phase: str = "pause") -> str | None:
        return None

    def supports_talk(self) -> bool:
        return False


class HumanPlayer(BasePlayer):
    """你：键盘输入。打 /p 就是要求中场暂停（返回 None）。"""

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
            if raw.isdigit() and 1 <= int(raw) <= len(opts):
                return opts[int(raw) - 1], ""


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
        self.name = f"{seat}号（牌友）"
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
                 wait: float = 0.0) -> None:
        super().__init__(seat)
        self.dir = Path(root or DEFAULT_SEAT_ROOT) / f"seat{seat}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.name = f"{seat}号（接入）"
        self.supports = "external"
        self.poll = poll
        self.wait = wait
        self.round = 0

    # ---- 文件 ----
    def _pending_path(self) -> Path:
        return self.dir / "pending.json"

    def _answer_path(self) -> Path:
        return self.dir / "answer.json"

    @staticmethod
    def _write_atomic(path: Path, obj: dict) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)

    def decide(self, d: Decision) -> tuple[Action | None, str]:
        self.round += 1
        req = {
            "round": self.round,
            "seat": self.seat,
            "kind": d.kind,
            "view": d.view,
            "legal": [a.to_dict() for a in d.legal],
            "at": time.strftime("%H:%M:%S"),
        }
        for p in (self._answer_path(), self.dir / "give-up"):
            p.unlink(missing_ok=True)
        self._write_atomic(self._pending_path(), req)

        t0 = time.monotonic()
        while True:
            if (self.dir / "give-up").exists():
                (self.dir / "give-up").unlink()
                self._pending_path().unlink(missing_ok=True)
                return random_policy(random.Random())(d), "（这手不想了，随机代打）"
            if self._answer_path().exists():
                try:
                    ans = json.loads(self._answer_path().read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    time.sleep(self.poll)          # 还在写，等下一次
                    continue
                self._answer_path().unlink(missing_ok=True)
                if int(ans.get("round") or -1) == self.round:
                    payload = ans.get("action") or {}
                    if str(payload.get("kind") or "") == "pause":
                        self._pending_path().unlink(missing_ok=True)
                        return None, str(ans.get("say") or "")
                    act = action_from_dict(payload)
                    if act is not None and _legal(d, act):
                        self._pending_path().unlink(missing_ok=True)
                        return act, str(ans.get("say") or "")
            if self.wait and time.monotonic() - t0 > self.wait:
                self._pending_path().unlink(missing_ok=True)
                return random_policy(random.Random())(d), "（等太久，随机代打）"
            time.sleep(self.poll)


def action_from_dict(payload: dict) -> Action | None:
    """外部递进来的动作。看不懂就返回 None，当作没答。

    meta 必须原样带回去：加杠 / 明杠 / 从谁那儿，全靠它，丢了引擎就判非法。
    """
    try:
        kind = str(payload.get("kind") or "")
        meta = dict(payload.get("meta") or {})
        if kind in ("discard", "pon", "kan"):
            return Action(kind, tile=parse_tile(payload.get("tile")), meta=meta)
        if kind == "chi":
            combo = payload.get("combo") or []
            return Action("chi", combo=tuple(parse_tile(t) for t in combo), meta=meta)
        if kind in ("win", "pass"):
            return Action(kind, meta=meta)
    except (ValueError, TypeError):
        return None
    return None


def _legal(d: Decision, act: Action) -> bool:
    """递上来的动作必须是这一手能做的，免得外部手抖把牌局带歪。"""
    for a in d.legal:
        if a.kind != act.kind:
            continue
        if a.kind in ("discard", "pon", "kan") and a.tile == act.tile:
            return True
        if a.kind == "chi" and a.combo == act.combo:
            return True
        if a.kind in ("win", "pass"):
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
