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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

import seats as seats_mod
from game import Action, Decision, random_policy
from referee import parse_tile

DEFAULT_SEAT_ROOT = Path(__file__).resolve().parent / "seats"

# 玩家类型：别在 play.py 里写死"几号是什么人"，都由这张表 + 座位计划决定
KINDS = ("human", "llm", "external", "random")


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
    """人在键盘上按了提前结束：不再打后面的局，直接收场。"""


class HumanPlayer(BasePlayer):
    """你：键盘输入。打 /p 就是要求中场暂停（返回 None），/e 提前结束整场。"""

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
            if raw in ("/e", "提前结束"):
                raise EndEarly()
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
                 wait: float = 0.0, on_wait: Callable[[float], None] | None = None) -> None:
        super().__init__(seat)
        self.dir = Path(root or DEFAULT_SEAT_ROOT) / seats_mod.seat_dir(seat)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.name = f"{seat}号（接入）"
        self.supports = "external"
        self.poll = poll
        self.wait = wait
        self.round = 0
        self.on_wait = on_wait          # 等待期间每秒回一次（给 TUI 报个"已等 N 秒"）

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
        beat = t0
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
            if self.on_wait and time.monotonic() - beat >= 1.0:   # 每秒报一次，TUI 上那个秒数在走=还活着
                beat = time.monotonic()
                self.on_wait(beat - t0)
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
        if kind in ("win", "pass", "ting", "bloom", "draw"):
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


def parse_players(text: str, seats: int = 4) -> list[SeatPlan]:
    """把命令行的玩家配置读成计划。

    两种写法：
        "human,llm,external,random"  按 1..4 号依次排
        "2=llm,3=external"           点名叫座，剩下的补随机
    """
    spec = (text or "").strip()
    if not spec:
        return [SeatPlan(s + 1, "random") for s in range(seats)]
    assigns: dict[int, str] = {}
    if "=" in spec:
        for item in spec.split(","):
            item = item.strip()
            if not item:
                continue
            left, _, kind = item.partition("=")
            _check_seat(int(left.strip()), seats)
            assigns[int(left.strip())] = kind.strip()
    else:
        for i, kind in enumerate((x.strip() for x in spec.split(",")), start=1):
            if kind:
                _check_seat(i, seats)
                assigns[i] = kind
    return [SeatPlan(s + 1, assigns.get(s + 1, "random")) for s in range(seats)]


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

    def talker(self) -> BasePlayer | None:
        """中场能聊天的那个座位（现阶段就是牌友 LLM）。"""
        llms = self.of_kind("llm")
        return llms[0] if llms else None


def build_players(plans: list[SeatPlan], *, seats: int = 4,
                  llm_factory: Callable[[SeatPlan], BasePlayer] | None = None,
                  rng: random.Random | None = None) -> Table:
    """按计划摆人。别的类型自己会造，llm 那张椅子由外面递进来的工厂盖（要网、要模型）。"""
    made: list[BasePlayer] = []
    for plan in plans:
        if plan.kind == "human":
            made.append(HumanPlayer(plan.seat))
        elif plan.kind == "llm":
            if llm_factory is None:
                raise RuntimeError("这桌要坐牌友，但没给造牌友的工厂")
            made.append(llm_factory(plan))
        elif plan.kind == "external":
            made.append(ExternalPlayer(plan.seat,
                                       wait=float(plan.options.get("wait", 0)),
                                       root=plan.options.get("root")))
        else:
            made.append(RandomPlayer(plan.seat, rng=rng))
    return Table(made, seats)
