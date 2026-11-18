"""牌局驱动 —— 只管流程，不管谁在打。

牌局代码还没接 UI，所以这里的每一处都为"外部一步步驱动"设计：

    * 不阻塞：pending() 回答"现在轮到谁、有哪些动作"，apply() 提交一个动作。
      UI 可以自由地等人类玩家、等 LLM、等动画，循环不会卡住。
    * 事件流：每个动作产出 Event，UI 订阅渲染，不用反查状态。
    * 观察隔离：observe(seat) 只给这个座位能看的东西（自己的手牌 + 公开牌河），
      牌墙和别人的手牌不外泄；裁判拿到的才是全量。
    * 选手解耦：人 / 随机机器人 / LLM 都用同一个形状 —— 看 pending()，回 Action。

流程按麻将通用规则：
    摸牌 → (可杠/可胡) → 打牌 → 别家反应（胡 > 杠 > 碰 > 吃，只有下家能吃）→ 下一家摸
    杠之后从牌尾补一张；牌墙摸完 → 荒庄。

还没做的（接口已留）：结算番分（Game.settle() 转给裁判的占位）、连庄、包牌。
"""

from __future__ import annotations

import random
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

from referee import (
    COPIES,
    KINDS,
    RED,
    Action,
    HandView,
    Meld,
    Referee,
    RuleConfig,
    RULES_V1,
    counts_from,
    parse_tile,
    tile_id,
    tile_name,
    tiles_from,
)

# 反应优先级：胡 > 杠 > 碰 > 吃
PRIORITY = {"win": 4, "kan": 3, "pon": 2, "chi": 1}
CLAIMS = ("win", "kan", "pon", "chi")


def _tool(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required}}}


# 工具表：整局就这一张，打牌、自由思考、中场暂停、散场闲聊全用它。
# 它渲染在提示词最前面，只要跟着局面变（比如以前 enum 里塞当前能打的牌），
# 整段前缀就从头废掉，会话缓存一次也命不中。
# 这一手能做哪几件事，由 user 消息末尾的【本轮只能做这些】说；真调了不能做的，
# _action_from_call 会挡掉，笔记和暂停另有阶段守卫。

# 笔记：长期那份打完才许动，临时那份中场暂停时记
NOTE_TOOL: dict = _tool(
    "edit_note",
    "改笔记，可以只改其中一段。target=long 是长期笔记（牌友笔记.md，打完才能改，下一局生效）；"
    "target=temp 是临时笔记（只这一局用，随牌桌一起给你看）。"
    "mode=append 往末尾加一条，写 new_string；"
    "mode=replace 把 old_string 那一小段原文换成 new_string，old_string 必须唯一，"
    "找不到或出现多次都不会改，出现多次就用 replace_all。",
    {"target": {"type": "string", "enum": ["long", "temp"]},
     "mode": {"type": "string", "enum": ["append", "replace", "replace_all"]},
     "old_string": {"type": "string"},
     "new_string": {"type": "string"}},
    ["mode"])

# 中场暂停：他想停下来问一句、聊两句
PAUSE_TOOL: dict = _tool(
    "pause", "中场暂停。想跟对面说句话、问一句，或者想让人等你一下，就调它。"
             "调了这一次不出牌，why 写你想说什么。",
    {"why": {"type": "string"}}, [])

TOOLS: list[dict] = [
    _tool("discard", "打出一张手里的牌，tile 写牌名（如 m3 / p5 / s7 / z5）",
          {"tile": {"type": "string"}}, ["tile"]),
    _tool("pon", "碰别人打出的这张牌，tile 就写这张牌", {"tile": {"type": "string"}}, ["tile"]),
    _tool("kan", "杠这张牌：手里四张亮出来杠，或把已有的碰加成杠", {"tile": {"type": "string"}}, ["tile"]),
    _tool("chi", "吃出顺子，给出你手里要用的两张，写成 一张+一张（如 s4+s6）",
          {"combo": {"type": "string"}}, ["combo"]),
    _tool("win", "宣布胡牌", {}, []),
    _tool("bloom", "后开花：轮到你摸牌时不摸了，直接胡牌墙最后一张（手里得有杠，不用报听）", {}, []),
    _tool("pass", "过，不要这张牌", {}, []),
    _tool("ting", "报听。手牌已经听牌、想锁死手牌等胡，就调它", {}, []),
    NOTE_TOOL,
    PAUSE_TOOL,
]


# ---------------------------------------------------------------- 配置与事件

@dataclass(frozen=True)
class GameConfig:
    seats: int = 4
    start_hand: int = 13
    dealer: int = 0
    seed: int | None = None

    allow_chi: bool = True
    allow_pon: bool = True
    allow_kan: bool = True
    allow_rob_kan: bool = True       # 抢杠：加杠的那张被别人胡走
    multi_win: bool = False          # 一炮多响；默认只认最近的一家

    think_seconds: float | None = 3600.0   # 思考多久随它，一小时之内都算没超时

    rules: RuleConfig = RULES_V1


@dataclass
class Event:
    type: str                       # start|draw|discard|pon|chi|kan|kan_draw|win|draw_game|pass
    seat: int | None = None
    tile: int | None = None
    data: dict = field(default_factory=dict)

    def to_dict(self, *, reveal: bool = True) -> dict:
        d: dict = {"type": self.type}
        if self.seat is not None:
            d["seat"] = self.seat
        if self.tile is not None:
            if reveal:
                d["tile"] = tile_id(self.tile)
                d["name"] = tile_name(self.tile)
            else:
                d["tile"] = None
        if self.data:
            d["data"] = dict(self.data)
        return d


@dataclass
class Decision:
    """现在轮到谁、能做什么。UI 拿这个去渲染按钮，LLM 拿这个去选动作。"""

    seat: int
    kind: str                       # draw | discard | react
    legal: list[Action]
    view: dict
    deadline: float | None = None

    def tools(self) -> list[dict]:
        """工具表是**固定**的，一手牌到下一手一个字都不变。

        它渲染在提示词最前面，只要随牌变（以前 enum 里塞的是当前能打的牌），
        整段前缀就从头废掉，会话缓存一次也命不中。能打哪几张牌由 user 消息里的
        【可选动作】说，非法调用照旧由 _action_from_call 挡掉。
        """
        return TOOLS


# ---------------------------------------------------------------- 牌局

class Game:
    def __init__(self, config: GameConfig = GameConfig()) -> None:
        self.cfg = config
        self.r = Referee(config.rules)
        self.wall: deque[int] = deque()
        self.hands: dict[int, list[int]] = {s: [0] * KINDS for s in range(config.seats)}
        self.fixed: dict[int, list[Meld]] = {s: [] for s in range(config.seats)}
        self.discards: dict[int, list[int]] = {s: [] for s in range(config.seats)}
        self.turn: int = config.dealer
        self.phase: str = "idle"        # idle|draw|discard|react|kan_draw|ended
        self.events: list[Event] = []   # 待取走的事件（UI 增量渲染用）
        self.history: list[Event] = []  # 全程事件（复盘、日志用）
        self.last: tuple[int, str, int] | None = None
        self.reactors: list[tuple[int, int, int, list[Action]]] = []   # (rank, dist, seat, options)
        self.rob: tuple[int, int] | None = None                        # (杠的人, 那张牌)
        self.rob_kind: str | None = None                               # 哪种杠被抢：in_hand / added
        self.result: dict | None = None
        self.ting: dict[int, bool] = {s: False for s in range(config.seats)}   # 谁报听了
        self.drawn: int | None = None                                        # 刚摸进来的那张

    # ---------- 开局 ----------
    def start(self) -> list[Event]:
        rng = random.Random(self.cfg.seed)
        deck = [t for t in range(KINDS) for _ in range(COPIES)]
        rng.shuffle(deck)
        self.wall = deque(deck)
        for s in range(self.cfg.seats):
            for _ in range(self.cfg.start_hand):
                self.hands[s][self.wall.popleft()] += 1
        self.turn = self.cfg.dealer
        self.phase = "draw"
        self._emit(Event("start", data={"seats": self.cfg.seats, "wall": len(self.wall), "dealer": self.cfg.dealer}))
        return self.drain_events()

    # ---------- 问：现在轮到谁 ----------
    def pending(self) -> Decision | None:
        if self.phase == "ended":
            return None
        if self.phase == "draw":
            opts = [Action("draw")]
            if self._can_bloom(self.turn):
                opts.append(Action("bloom"))
            return self._decision(self.turn, "draw", opts)
        if self.phase == "discard":
            legal = self.r.legal_actions(self._view(self.turn), self.turn)
            return self._decision(self.turn, "discard", legal)
        if self.phase == "react":
            if not self.reactors:
                raise RuntimeError("react 阶段没有待反应的玩家")
            rank, dist, seat, options = self.reactors[0]
            return self._decision(seat, "react", options + [Action("pass")])
        if self.phase == "kan_draw":
            opts = [Action("draw")]
            if self._can_bloom(self.turn):
                opts.append(Action("bloom"))
            return self._decision(self.turn, "draw", opts)
        if self.phase == "ting_declare":
            return self._decision(self.turn, "ting_declare", [Action("ting"), Action("pass")])
        raise RuntimeError(f"未知阶段 {self.phase}")

    def _decision(self, seat: int, kind: str, legal: list[Action]) -> Decision:
        dl = None
        if self.cfg.think_seconds:
            dl = time.monotonic() + self.cfg.think_seconds
        return Decision(seat=seat, kind=kind, legal=legal, view=self.observe(seat), deadline=dl)

    # ---------- 答：提交一个动作 ----------
    def apply(self, action: Action | dict, seat: int | None = None) -> list[Event]:
        action = self.coerce(action)
        d = self.pending()
        if d is None:
            raise RuntimeError("牌局已结束")
        if seat is not None and seat != d.seat:
            raise RuntimeError(f"现在轮 {d.seat} 号，不是 {seat} 号")
        seat = d.seat

        if d.kind == "draw" and action.kind in ("draw", "bloom"):
            if action.kind == "bloom":
                res = self.r.validate(self._view(seat), seat, action)
                if not res.ok:
                    raise RuntimeError(f"动作不合法：{res.code} {res.detail}")
                self._do_bloom(seat)
                return self.drain_events()
            self._do_draw(seat, kan=(self.phase == "kan_draw"))
            return self.drain_events()

        if d.kind == "discard":
            return self._do_own_action(seat, action)

        if d.kind == "react":
            return self._do_reaction(seat, action)

        if d.kind == "ting_declare":
            return self._do_ting_declare(seat, action)

        raise RuntimeError(f"{d.kind} 阶段不接受动作 {action.kind}")

    def coerce(self, action: Action | dict) -> Action:
        """UI / LLM 都可以传 dict：{'kind':'discard','tile':'m1'} 或 {'name':..,'arguments':..}"""
        if isinstance(action, Action):
            return action
        if "kind" in action:
            kind = action["kind"]
            tile = action.get("tile")
            combo = action.get("combo", ())
            return Action(
                kind,
                tile=parse_tile(tile) if tile is not None and not isinstance(tile, int) else tile,
                combo=tuple(parse_tile(t) for t in combo) if combo else (),
                meta=action.get("meta", {}),
            )
        if "name" in action:
            return self.r.parse_tool_call(action["name"], action.get("arguments") or {})
        raise ValueError(f"看不懂的动作 {action!r}")

    # ---------- 自己的回合 ----------
    def _do_own_action(self, seat: int, action: Action) -> list[Event]:
        res = self.r.validate(self._view(seat), seat, action)
        if not res.ok:
            raise RuntimeError(f"动作不合法：{res.code} {res.detail}")

        if action.kind == "win":
            self._end_win(seat, tile=None, zimo=True)
            return self.drain_events()

        if action.kind == "discard":
            self._hand_sub(seat, action.tile, 1)
            self.discards[seat].append(action.tile)
            self.last = (seat, "discard", action.tile)
            self.drawn = None
            self._emit(Event("discard", seat, action.tile, {"left": sum(self.hands[seat])}))
            if not self.ting[seat] and self._ready(seat):
                # 报听是主动的：刚好听牌了才问一句，不想报可以直接过
                self.phase = "ting_declare"
            else:
                self._build_reactors(seat, action.tile)
            return self.drain_events()

        if action.kind == "kan":
            self._do_kan(seat, action)
            return self.drain_events()

        raise RuntimeError(f"自己回合不接受 {action.kind}")

    def _can_bloom(self, seat: int) -> bool:
        """后开花：该摸牌的时候不摸，直接胡牌墙最后那张（手里得有杠）。"""
        if not self.r.cfg.allow_bloom or not self.wall:
            return False
        if not any(m.kind == "kan" for m in self.fixed[seat]):
            return False
        c = list(self.hands[seat])
        c[self.wall[-1]] += 1
        return self.r.is_win(tuple(c), tuple(self.fixed[seat])).ok

    def _do_bloom(self, seat: int) -> None:
        tile = self.wall.pop()               # 取的是牌墙最后一张（明牌）
        self.hands[seat][tile] += 1
        self._emit(Event("bloom", seat, tile, {"wall": len(self.wall)}))
        self._end_win(seat, tile=tile, zimo=True, bloom=True)

    def _ready(self, seat: int) -> bool:
        """手里 13 张（或减掉副露后对应的张数）听没听牌。"""
        return bool(self.r.waits(self.hands[seat], self.fixed[seat]))

    def _do_ting_declare(self, seat: int, action: Action) -> list[Event]:
        if action.kind not in ("ting", "pass"):
            raise RuntimeError(f"报听阶段只接受 ting / pass，收到 {action.kind}")
        if action.kind == "ting":
            self.ting[seat] = True
            waits = [tile_id(t) for t in self.r.waits(self.hands[seat], self.fixed[seat])]
            self._emit(Event("ting", seat, data={"waits": waits}))
        # 报完（或不报）再让下家反应 / 摸牌
        self._build_reactors(self.last[0], self.last[2])
        return self.drain_events()

    def _do_kan(self, seat: int, action: Action) -> None:
        tile = action.tile
        kind = (action.meta or {}).get("kind")
        if kind == "in_hand":
            # 手里四张亮出来杠，别人能抢；明杠（别人打牌后杠的）不能抢
            if self.cfg.allow_rob_kan and self._robbers(seat, tile):
                self.rob, self.rob_kind = (seat, tile), "in_hand"
                self._build_robbers(seat, tile)
                return
            self._finish_kan(seat, tile, "in_hand")
            return
        if kind == "added":
            if self.cfg.allow_rob_kan and self._robbers(seat, tile):
                self.rob, self.rob_kind = (seat, tile), "added"
                self._build_robbers(seat, tile)
                return
            self._finish_kan(seat, tile, "added")
            return
        raise RuntimeError(f"自己回合不能明杠（{kind}）")

    def _finish_kan(self, seat: int, tile: int, kind: str) -> None:
        if kind == "in_hand":
            self._hand_sub(seat, tile, 4)
            self.fixed[seat].append(Meld("kan", (tile,) * 4))
            self._emit(Event("kan", seat, tile, {"kind": "in_hand"}))
        else:
            self._hand_sub(seat, tile, 1)
            for i, m in enumerate(self.fixed[seat]):
                if m.is_triplet_like and m.kind == "triplet" and m.tiles[0] == tile:
                    self.fixed[seat][i] = Meld("kan", (tile,) * 4)
                    break
            else:
                raise RuntimeError("加杠找不到原来的碰")
            self._emit(Event("kan", seat, tile, {"kind": "added"}))
        self.phase = "kan_draw"
        self.last = None

    # ---------- 反应 ----------
    def _build_reactors(self, discarder: int, tile: int) -> None:
        self.reactors = []
        for s in range(self.cfg.seats):
            if s == discarder:
                continue
            v = self._view(s, last=(discarder, "discard", tile))
            opts = [a for a in self.r.legal_actions(v, s) if a.kind in CLAIMS]
            if not opts:
                continue
            rank = max(PRIORITY[a.kind] for a in opts)
            dist = (s - discarder) % self.cfg.seats
            self.reactors.append((rank, dist, s, opts))
        self.reactors.sort(key=lambda x: (-x[0], x[1]))
        self.phase = "react" if self.reactors else "draw"
        if not self.reactors:
            self.turn = (discarder + 1) % self.cfg.seats
            self.last = None

    def _do_reaction(self, seat: int, action: Action) -> list[Event]:
        if action.kind == "pass":
            self.reactors.pop(0)
            if not self.reactors:
                if self.rob:
                    owner, tile = self.rob
                    kind = self.rob_kind
                    self.rob, self.rob_kind = None, None
                    self._finish_kan(owner, tile, kind)
                else:
                    self.turn = (self.last[0] + 1) % self.cfg.seats
                    self.phase = "draw"
                    self.last = None
            return self.drain_events()

        if self.rob:
            src, tile = self.rob
        else:
            src, _, tile = self.last
        if action.kind == "win":
            self._end_win(seat, tile=tile, zimo=False, from_seat=src, rob=self.rob is not None)
            return self.drain_events()

        self.reactors = []
        if action.kind == "pon":
            self._hand_sub(seat, tile, 2)
            self.fixed[seat].append(Meld("triplet", (tile,) * 3))
            self._take_discard(src, tile)
            self._emit(Event("pon", seat, tile, {"from": src}))
            self.turn, self.phase, self.last = seat, "discard", None
            return self.drain_events()

        if action.kind == "chi":
            for t in action.combo:
                self._hand_sub(seat, t, 1)
            meld = tuple(sorted((action.combo[0], action.combo[1], tile)))
            self.fixed[seat].append(Meld("seq", meld))
            self._take_discard(src, tile)
            self._emit(Event("chi", seat, tile, {"from": src, "use": [tile_id(t) for t in action.combo]}))
            self.turn, self.phase, self.last = seat, "discard", None
            return self.drain_events()

        if action.kind == "kan":
            self._hand_sub(seat, tile, 3)
            self.fixed[seat].append(Meld("kan", (tile,) * 4))
            self._take_discard(src, tile)
            self._emit(Event("kan", seat, tile, {"kind": "open", "from": src}))
            self.turn, self.phase, self.last = seat, "kan_draw", None
            return self.drain_events()

        raise RuntimeError(f"反应阶段不接受 {action.kind}")

    def _take_discard(self, seat: int, tile: int) -> None:
        pool = self.discards[seat]
        for i in range(len(pool) - 1, -1, -1):
            if pool[i] == tile:
                pool.pop(i)
                return
        raise RuntimeError("牌河里找不到这张牌")

    # ---------- 抢杠 ----------
    def _robbers(self, owner: int, tile: int) -> list[tuple[int, list[Action]]]:
        out = []
        for s in range(self.cfg.seats):
            if s == owner:
                continue
            if not self.ting[s]:
                continue
            v = HandView(seat=s, seats=self.cfg.seats, hands={s: tuple(self.hands[s])},
                         fixed={s: tuple(self.fixed[s])}, last=(owner, "discard", tile))
            c = list(self.hands[s]); c[tile] += 1
            if self.r.is_win(tuple(c), tuple(self.fixed[s])).ok:
                out.append((s, [Action("win", tile=tile, meta={"from": owner, "rob": True})]))
        return out

    def _build_robbers(self, owner: int, tile: int) -> None:
        robbers = self._robbers(owner, tile)
        self.reactors = [(PRIORITY["win"], (s - owner) % self.cfg.seats, s, opts) for s, opts in robbers]
        self.reactors.sort(key=lambda x: (-x[0], x[1]))
        self.phase = "react" if self.reactors else "draw"

    # ---------- 摸牌 ----------
    def _do_draw(self, seat: int, kan: bool = False) -> None:
        """摸牌一律从牌头取；杠后补张也是牌头那一张，只是事件名不同。"""
        if not self.wall:
            self._end_draw_game()
            return
        tile = self.wall.popleft()
        self.hands[seat][tile] += 1
        self.drawn = tile
        self._emit(Event("kan_draw" if kan else "draw", seat, tile, {"wall": len(self.wall)}))
        self.phase = "discard"
        self.last = None

    # ---------- 结束 ----------
    def _end_win(self, seat: int, tile: int | None, *, zimo: bool, from_seat: int | None = None,
                 rob: bool = False, bloom: bool = False) -> None:
        self.phase = "ended"
        self.result = {
            "type": "win",
            "seat": seat,
            "tile": tile_id(tile) if tile is not None else None,
            "zimo": zimo,
            "from": from_seat,
            "rob": rob,
            "bloom": bloom,
            "settle": self.settle({"seat": seat, "zimo": zimo, "from": from_seat}),
        }
        self._emit(Event("win", seat, tile, {"zimo": zimo, "from": from_seat, "rob": rob, "bloom": bloom}))

    def _end_draw_game(self) -> None:
        self.phase = "ended"
        self.result = {"type": "draw_game", "reason": "wall_empty", "wall": 0}
        self._emit(Event("draw_game", data={"reason": "wall_empty"}))

    def settle(self, ctx: dict) -> dict:
        """结算以后再说，现在把口子留给裁判。"""
        return self.r.settle(ctx)

    # ---------- 观察 ----------
    def observe(self, seat: int) -> dict:
        """只给这个座位能看的东西。"""
        v = self._view(seat)
        d = self.pending_peek()
        return {
            "seat": seat,
            "seats": self.cfg.seats,
            "phase": self.phase,
            "turn": self.turn,
            "hand": [tile_id(t) for t in tiles_from(tuple(self.hands[seat]))],
            "fixed": [[tile_id(t) for t in m.tiles] for m in self.fixed[seat]],
            "melds": {s: [[tile_id(t) for t in m.tiles] for m in self.fixed[s]] for s in range(self.cfg.seats)},
            "discards": {s: [tile_id(t) for t in self.discards[s]] for s in range(self.cfg.seats)},
            "hand_sizes": {s: sum(self.hands[s]) for s in range(self.cfg.seats)},
            "wall_left": len(self.wall),
            "wall_last": tile_id(self.wall[-1]) if self.wall else None,   # 最后一张是明牌，谁都能看
            "last": None if self.last is None else {"seat": self.last[0], "tile": tile_id(self.last[2])},
            "can_win": self.r.is_win(tuple(self.hands[seat]), tuple(self.fixed[seat])).ok if self.phase == "discard" and self.turn == seat else False,
            "ting": {s: self.ting[s] for s in range(self.cfg.seats)},
            "drawn": tile_id(self.drawn) if self.drawn is not None and seat == self.turn else None,
            "pending": None if d is None else {"seat": d.seat, "kind": d.kind},
            "result": self.result,
        }

    def pending_peek(self) -> Decision | None:
        try:
            return self.pending()
        except Exception:
            return None

    def _view(self, seat: int, last: tuple[int, str, int] | None = None) -> HandView:
        return HandView(
            seat=seat,
            seats=self.cfg.seats,
            hands={s: tuple(self.hands[s]) for s in range(self.cfg.seats)},
            fixed={s: tuple(self.fixed[s]) for s in range(self.cfg.seats)},
            discards={s: tuple(self.discards[s]) for s in range(self.cfg.seats)},
            wall_left=len(self.wall),
            wall_last=self.wall[-1] if self.wall else None,
            last=self.last if last is None else last,
            ting=dict(self.ting),
            drawn=self.drawn if (self.drawn is not None and seat == self.turn) else None,
        )

    # ---------- 杂务 ----------
    def _hand_sub(self, seat: int, tile: int, n: int) -> None:
        if self.hands[seat][tile] < n:
            raise RuntimeError(f"{seat} 号手里没有 {n} 张 {tile_name(tile)}")
        self.hands[seat][tile] -= n

    def _emit(self, e: Event) -> None:
        self.events.append(e)
        self.history.append(e)

    def drain_events(self) -> list[Event]:
        out, self.events = self.events, []
        return out

    def total_tiles(self) -> int:
        """守恒校验：手牌 + 面子 + 牌河 + 牌墙 == 112"""
        n = sum(sum(h) for h in self.hands.values())
        n += sum(len(m.tiles) for fs in self.fixed.values() for m in fs)
        n += sum(len(d) for d in self.discards.values())
        n += len(self.wall)
        return n


# ---------------------------------------------------------------- 简易选手

def random_policy(rng: random.Random | None = None) -> Callable[[Decision], Action]:
    """随机机器人：有胡就胡，偶尔碰杠，其余随机出牌。"""
    rng = rng or random.Random()

    def pick(d: Decision) -> Action:
        kinds = {a.kind for a in d.legal}
        if "win" in kinds:
            return next(a for a in d.legal if a.kind == "win")
        if "bloom" in kinds:         # 后开花也是胡，先拿了
            return next(a for a in d.legal if a.kind == "bloom")
        if "ting" in kinds:          # 听牌就报，不报永远胡不了
            return next(a for a in d.legal if a.kind == "ting")
        for k, p in (("kan", 0.9), ("pon", 0.5), ("chi", 0.3)):
            if k in kinds and rng.random() < p:
                return rng.choice([a for a in d.legal if a.kind == k])
        if d.kind == "react":
            return Action("pass")
        return rng.choice([a for a in d.legal if a.kind == "discard"]) if d.kind == "discard" else Action("draw")

    return pick


def play_out(game: Game, policies: dict[int, Callable[[Decision], Action]], limit: int = 5000) -> dict:
    """把一手打完；返回结果。UI 不在时用这个跑回归。"""
    steps = 0
    while game.pending() is not None:
        d = game.pending()
        act = policies[d.seat](d)
        game.apply(act)
        steps += 1
        if steps > limit:
            raise RuntimeError("疑似死循环")
    return game.result or {"type": "unknown"}


# ---------------------------------------------------------------- 自测

def _selftest() -> int:
    fails = 0

    def case(name: str, got, want) -> None:
        nonlocal fails
        ok = got == want
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
        if not ok:
            fails += 1
            print(f"      期望 {want}，实际 {got}")

    def _h(tiles: str) -> list[int]:
        out = [0] * KINDS
        for x in tiles.split():
            out[parse_tile(x)] += 1
        return out

    # 1 发牌
    g = Game(GameConfig(seats=3, seed=7))
    g.start()
    case("三家各 13 张", [sum(g.hands[s]) for s in range(3)], [13, 13, 13])
    case("牌墙 112-39=73", len(g.wall), 73)
    case("开局轮到庄家摸牌", g.pending().kind, "draw")

    # 2 守恒 + 能打完（跑 30 手随机）
    bad = 0
    for seed in range(30):
        g = Game(GameConfig(seats=3, seed=seed))
        g.start()
        props = [random_policy(random.Random(seed * 10 + s)) for s in range(3)]
        r = play_out(g, {0: props[0], 1: props[1], 2: props[2]})
        if g.total_tiles() != 112:
            bad += 1
        if g.result is None or g.phase != "ended":
            bad += 1
    case("30 手随机：牌数守恒且都收局", bad, 0)

    # 四个座位：一人 13 张，牌墙还剩 60
    g4 = Game(GameConfig(seed=8))
    g4.start()
    case("四家各 13 张", [sum(g4.hands[s]) for s in range(4)], [13, 13, 13, 13])
    case("四家牌墙 60 张", len(g4.wall), 60)
    props4 = [random_policy(random.Random(100 + s)) for s in range(4)]
    play_out(g4, dict(enumerate(props4)))
    case("四家也能收局", (g4.phase, g4.total_tiles()), ("ended", 112))

    # 3 碰优先于吃：构造一家打 3万，下家能吃、对家能碰
    g = Game(GameConfig(seats=3, seed=1))
    g.start()
    g.hands[0] = [0] * KINDS; g.hands[1] = [0] * KINDS; g.hands[2] = [0] * KINDS
    # 0 打 3万；1 有 2万4万（能吃）；2 有 3万3万（能碰）
    g.hands[0][parse_tile("3万")] = 1
    g.hands[1][parse_tile("2万")] = 1; g.hands[1][parse_tile("4万")] = 1
    g.hands[2][parse_tile("3万")] = 2
    g.wall.extend([parse_tile("1万")] * 30)
    g.turn, g.phase, g.last = 0, "discard", None
    g.apply(Action("discard", tile=parse_tile("3万")))
    case("碰压过吃", (g.pending().seat, g.pending().kind), (2, "react"))
    case("碰的选项里有碰", any(a.kind == "pon" for a in g.pending().legal), True)

    # 7 后开花：手里有杠，牌墙最后一张正好能把牌型补成胡
    g = Game(GameConfig(seed=14))
    g.start()
    for s in range(4):
        g.hands[s] = [0] * KINDS
        g.fixed[s] = []
        g.discards[s] = []
    p4, p9 = parse_tile("4饼"), parse_tile("9饼")
    g.fixed[0] = [Meld("kan", (p4, p4, p4, p4))]
    g.hands[0] = _h("1万 2万 3万 4万 5万 6万 7条 8条 9条 9饼")
    g.wall = deque([parse_tile("1万")] * 5 + [p9])
    g.turn, g.phase, g.last, g.drawn = 0, "draw", None, None
    d = g.pending()
    case("有杠就能后开花", [a.kind for a in d.legal], ["draw", "bloom"])
    ev = g.apply(Action("bloom"))
    case("后开花就是胡", (g.result["type"], g.result["seat"], g.result["bloom"]), ("win", 0, True))
    case("取走的是牌墙最后一张", (ev[0].tile, len(g.wall)), (p9, 5))

    g = Game(GameConfig(seed=16))
    g.start()
    for s in range(4):
        g.hands[s] = [0] * KINDS
        g.fixed[s] = []
        g.discards[s] = []
    g.fixed[0] = [Meld("kan", (p4, p4, p4, p4))]
    g.hands[0] = _h("1万 2万 3万 4万 5万 6万 7条 8条 9条 9饼")
    g.wall = deque([parse_tile("1万")] * 5 + [p9])
    g.turn, g.phase, g.last, g.drawn = 0, "draw", None, None
    g.apply(Action("draw"))
    case("先摸了牌就不能后开花", [a.kind for a in g.pending().legal].count("bloom"), 0)

    g = Game(GameConfig(seed=15))
    g.start()
    for s in range(4):
        g.hands[s] = [0] * KINDS
        g.fixed[s] = []
        g.discards[s] = []
    g.hands[0] = _h("1万 2万 3万 4万 5万 6万 7条 8条 9条 9饼 9饼 1饼")
    g.wall = deque([parse_tile("1万")] * 5 + [p9])
    g.turn, g.phase, g.last, g.drawn = 0, "draw", None, None
    case("手里没杠不能后开花", [a.kind for a in g.pending().legal], ["draw"])

    # 4 杠后从牌头补一张（用加杠验同一条路径）
    g = Game(GameConfig(seats=3, seed=2))
    g.start()
    g.hands[0] = [0] * KINDS
    tile = parse_tile("5条")
    g.fixed[0] = [Meld("triplet", (tile, tile, tile))]
    g.hands[0][tile] = 1
    g.hands[0][parse_tile("1万")] = 9
    g.turn, g.phase, g.last = 0, "discard", None
    before_wall = len(g.wall)
    before_head = g.wall[0]
    d = g.pending()
    case("没有暗杠这回事，只有手里四张的明杠",
         any(a.kind == "kan" and (a.meta or {}).get("kind") == "closed" for a in d.legal), False)
    kan = next(a for a in d.legal if a.kind == "kan" and a.tile == tile)
    g.apply(kan)
    case("加杠后进入补张", g.phase, "kan_draw")
    ev = g.apply(Action("draw"))
    case("补张从牌头摸", ev[0].tile, before_head)
    case("补张后牌墙少一张", len(g.wall), before_wall - 1)

    # 6 抢杠：已报听的人可以胡别人加杠的那张
    def rob_setup(listen: bool) -> Game:
        g = Game(GameConfig(seed=11))
        g.start()
        for s in range(4):
            g.hands[s] = [0] * KINDS
            g.fixed[s] = []
            g.discards[s] = []
        p7 = parse_tile("7饼")
        g.hands[0] = _h("7饼 1万 2万 3万 4条 5条 6条 8条 8条 9饼 9饼")
        g.fixed[0] = [Meld("triplet", (p7, p7, p7))]
        g.hands[1] = _h("1万 2万 3万 4万 5万 6万 1条 2条 3条 9饼 9饼 7饼 7饼")
        g.hands[2] = _h("2万 2万 2万 4万 4万 4万 6条 6条 6条 8饼 8饼 8饼 9条")
        g.hands[3] = _h("3万 3万 3万 5万 5万 5万 7条 7条 7条 1饼 1饼 1饼 9条")
        g.ting = {0: True, 1: listen, 2: False, 3: False}
        g.wall.extend([parse_tile("1万")] * 20)
        g.turn, g.phase, g.last, g.drawn = 0, "discard", None, p7
        g.apply(Action("kan", tile=p7, meta={"kind": "added"}))
        return g

    g = rob_setup(listen=True)
    case("报听的人能抢别人加的杠", (g.pending() and g.pending().kind), "react")
    d = g.pending()
    case("抢杠的选项里有胡", [a.kind for a in d.legal], ["win", "pass"])
    g.apply(next(a for a in d.legal if a.kind == "win"))
    case("抢杠胡成立", (g.result["type"], g.result["seat"], g.result["rob"]), ("win", 1, True))
    case("没报听的人抢不了杠", rob_setup(listen=False).phase, "kan_draw")

    # 6b 手里四张直接亮出来杠，别人能抢
    g = Game(GameConfig(seed=12))
    g.start()
    for s in range(4):
        g.hands[s] = [0] * KINDS
        g.fixed[s] = []
        g.discards[s] = []
    p7 = parse_tile("7饼")
    g.hands[0] = _h("7饼 7饼 7饼 7饼 2条 3条 4条 1饼 2饼 3饼 9条 9条 9条 4饼")
    g.hands[1] = _h("1万 2万 3万 4万 5万 6万 5条 5条 5条 9饼 9饼 8饼 9饼")
    g.ting = {0: True, 1: True, 2: False, 3: False}
    g.wall.extend([parse_tile("1万")] * 20)
    g.turn, g.phase, g.last, g.drawn = 0, "discard", None, p7
    d = g.pending()
    case("手里四张是明杠", any(a.kind == "kan" and (a.meta or {}).get("kind") == "in_hand" and a.tile == p7
                        for a in d.legal), True)
    g.apply(next(a for a in d.legal if a.kind == "kan" and a.tile == p7))
    d = g.pending()
    case("明杠也有人抢", (d.seat, d.kind), (1, "react"))
    g.apply(next(a for a in d.legal if a.kind == "win"))
    case("抢的是手里四张的杠", g.result["rob"], True)
    case("抢杠之后杠没算成", sum(1 for m in g.fixed[0] if m.kind == "kan"), 0)

    # 6c 别人打牌后杠的那种，抢不了
    g = Game(GameConfig(seed=13))
    g.start()
    for s in range(4):
        g.hands[s] = [0] * KINDS
        g.fixed[s] = []
        g.discards[s] = []
    g.hands[0] = _h("7饼 7饼 7饼 2条 3条 4条 1饼 2饼 3饼 9条 9条 9条 4饼")
    g.ting = {0: False, 1: True, 2: False, 3: False}
    g.discards[2] = [p7]
    g.turn, g.phase, g.last = 2, "react", (2, "discard", p7)
    g.rob = None
    g.reactors = [(1, 1, 0, [Action("kan", tile=p7, meta={"kind": "open", "from": 2})])]
    g.apply(Action("kan", tile=p7, meta={"kind": "open", "from": 2}))
    case("别人打的牌杠出来没人抢", g.phase, "kan_draw")

    # 5 荒庄：牌墙摸空
    g = Game(GameConfig(seats=3, seed=3))
    g.start()
    g.wall.clear()
    g.turn, g.phase = 0, "draw"
    g.apply(Action("draw"))
    case("牌墙空了判荒庄", (g.phase, g.result["type"]), ("ended", "draw_game"))

    print()
    print(f"共 {fails} 条失败")
    return fails


if __name__ == "__main__":
    raise SystemExit(_selftest())
