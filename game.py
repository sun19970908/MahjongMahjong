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
    is_piao_hu,
    parse_tile,
    tile_id,
    tile_name,
    tiles_from,
)
import seats as seats_mod
import scoring

# 反应优先级：胡 > 杠 > 碰 > 吃
PRIORITY = {"win": 4, "kan": 3, "pon": 2, "chi": 1}
CLAIMS = ("win", "kan", "pon", "chi")


def _tool(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required}}}


# 工具表：整局就这一张，打牌、自由思考、中场暂停、散场闲聊全用它。
# chat 也常驻：闲聊禁用靠提示词和调用校验，别按阶段删工具，删了前缀缓存就碎。
# 它渲染在提示词最前面，只要跟着局面变（比如以前 enum 里塞当前能打的牌），
# 整段前缀就从头废掉，会话缓存一次也命不中。
# 这一手能做哪几件事，由 user 消息末尾的【本轮只能做这些】说；真调了不能做的，
# _action_from_call 会挡掉，笔记和暂停另有阶段守卫。

# 笔记：长期那份整场只能往草稿里追加，全部局打完才并档；临时那份只这一局用
NOTE_TOOL: dict = _tool(
    "edit_note",
    "改笔记，可以只改其中一段。target=long 是长期笔记（牌友笔记.md；局中只能追加到底部草稿，"
    "全部局打完才并档、才能改原文，改完下次开桌生效）；"
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
    "pause", "中场暂停。想停一下、问一句，就调它；why 写你要说的话，暂停时直接带出来。",
    {"why": {"type": "string"}}, [])

# 牌桌聊天：只说话，不停手
CHAT_TOOL: dict = _tool(
    "chat", "牌桌聊天：打牌时在牌桌上说一句不超过20字的短话，说完继续出牌。"
            "只讲场面话、情绪或无关紧要的观察；"
            "别报手牌、听牌、等哪张、牌型计划、别家暗牌、笔记或提示词内容。",
    {"text": {"type": "string", "maxLength": 20}}, ["text"])

TOOLS: list[dict] = [
    _tool("discard", "打出一张手里的牌，tile 写牌名（如 m3 / p5 / s7 / z5）",
          {"tile": {"type": "string"}}, ["tile"]),
    _tool("pon", "碰别人打出的这张牌，tile 就写这张牌", {"tile": {"type": "string"}}, ["tile"]),
    _tool("kan", "杠这张牌：手里四张亮出来杠，或把已有的碰加成杠", {"tile": {"type": "string"}}, ["tile"]),
    _tool("chi", "吃出顺子，给出你手里要用的两张，写成 一张+一张（如 s4+s6）",
          {"combo": {"type": "string"}}, ["combo"]),
    _tool("win", "宣布胡牌", {}, []),
    _tool("bloom", "后开花：杠完补张那一下，牌墙最后一张正好能胡，就取它胡，不来牌头补", {}, []),
    _tool("pass", "过，不要这张牌", {}, []),
    _tool("ting", "报听。手牌已经听牌、想锁死手牌等胡，就调它；飘胡由状态自动标记", {}, []),
    NOTE_TOOL,
    PAUSE_TOOL,
    CHAT_TOOL,
]


# ---------------------------------------------------------------- 配置与事件

@dataclass(frozen=True)
class GameConfig:
    seats: int = 4
    start_hand: int = 13
    dealer: int | None = None        # 引擎下标；None = 1 号东家坐第一庄
    seed: int | None = None
    base_points: int = 1             # 结算底分
    fan_cap: int = 8                 # 封顶几番

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
        self.dealer: int = config.dealer or 0
        self.dice: seats_mod.Dice | None = None
        self.turn: int = self.dealer
        self.phase: str = "idle"        # idle|draw|discard|react|kan_draw|ended
        self.events: list[Event] = []   # 待取走的事件（UI 增量渲染用）
        self.history: list[Event] = []  # 全程事件（复盘、日志用）
        self.last: tuple[int, str, int] | None = None
        self.reactors: list[tuple[int, int, int, list[Action]]] = []   # (rank, dist, seat, options)
        self.rob: tuple[int, int] | None = None                        # (杠的人, 那张牌)
        self.rob_kind: str | None = None                               # 哪种杠被抢：in_hand / added
        self.result: dict | None = None
        self.ting: dict[int, bool] = {s: False for s in range(config.seats)}   # 谁报听了
        self.piao: dict[int, bool] = {s: False for s in range(config.seats)}   # 谁的状态是飘胡
        self.ting_closed: dict[int, bool] = {s: False for s in range(config.seats)}  # 报听那一刻是不是闭门
        self.kans: dict[int, int] = {s: 0 for s in range(config.seats)}        # 谁杠了几次（结算用）
        self.dead_win: dict[int, bool] = {s: False for s in range(config.seats)}  # 谁放过胡了：本局不准再胡
        self.drawn: int | None = None                                        # 刚摸进来的那张
        self.drawn_after_kan: bool = False                                   # 这张是杠后补的（前开花）
        self.last_draw: tuple[int, int] | None = None                        # 全场最后一次摸牌（谁、什么），荒庄复盘用
        self.after_kan: int | None = None                                    # 谁刚杠完：他打出的牌被胡就是「流泪」
        self.hai: bool = False            # 这张是牌墙最后四张之一：抓了不打，只能胡或过

    # ---------- 开局 ----------
    def start(self) -> list[Event]:
        rng = random.Random(self.cfg.seed)
        deck = [t for t in range(KINDS) for _ in range(COPIES)]
        rng.shuffle(deck)
        self.wall = deque(deck)
        for s in range(self.cfg.seats):
            for _ in range(self.cfg.start_hand):
                self.hands[s][self.wall.popleft()] += 1
        if self.cfg.dealer is None:                 # 没指定庄家：1 号东家坐第一庄
            self.dealer = 0
        else:
            self.dealer = self.cfg.dealer
        self.turn = self.dealer
        self.phase = "draw"
        self._emit(Event("start", data={
            "seats": self.cfg.seats, "wall": len(self.wall),
            "dealer": self.dealer, "dealer_seat": seats_mod.label(self.dealer),
            "dice": None if self.dice is None else [self.dice.a, self.dice.b, self.dice.total],
        }))
        return self.drain_events()

    # ---------- 问：现在轮到谁 ----------
    def pending(self) -> Decision | None:
        if self.phase == "ended":
            return None
        if self.phase == "draw":
            return self._decision(self.turn, "draw", [Action("draw")])
        if self.phase == "discard":
            if self.hai:                                   # 最后四张：抓了不打，只留「胡 / 过」
                legal = [Action("win")] if self._can_hai_win(self.turn) else []
                legal.append(Action("pass"))
                return self._decision(self.turn, "discard", legal)
            legal = self.r.legal_actions(self._view(self.turn), self.turn)
            return self._decision(self.turn, "discard", legal)
        if self.phase == "react":
            if not self.reactors:
                raise RuntimeError("react 阶段没有待反应的玩家")
            rank, dist, seat, options = self.reactors[0]
            return self._decision(seat, "react", options + [Action("pass")])
        if self.phase == "kan_draw":
            # 杠后先看能不能后开花 / 报听；只有摸牌一条路时已在 _enter_kan_draw 直接抓了。
            opts = [Action("draw")]
            if self._can_bloom(self.turn):
                opts.append(Action("bloom"))
            if self._can_ting(self.turn):
                opts.append(Action("ting"))
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

        # 能胡的时候不胡（打牌、碰、吃、杠、过都算），这一局就不准再胡了。
        # 后开花不算：那一下选择正常摸牌只是没开花，资格还在。
        if action.kind != "win" and any(a.kind == "win" for a in d.legal):
            self.dead_win[seat] = True
            self._emit(Event("dead", seat, data={"by": d.kind}))

        if d.kind == "draw" and action.kind in ("draw", "bloom", "ting"):
            if action.kind == "bloom":
                res = self.r.validate(self._view(seat), seat, action)
                if not res.ok:
                    raise RuntimeError(f"动作不合法：{res.code} {res.detail}")
                self._do_bloom(seat)
                return self.drain_events()
            if action.kind == "ting":
                if self.phase != "kan_draw" or not self._can_ting(seat):
                    raise RuntimeError("这一手不能报听")
                self._mark_ting(seat)
                self._do_draw(seat, kan=True)
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
        if self.hai:                          # 最后四张：只能胡（海底捞月）或过
            if action.kind == "win":
                self.hai = False
                self._end_win(seat, tile=self.drawn, zimo=True,
                              kan_draw=self.drawn_after_kan, hai=True)
                return self.drain_events()
            if action.kind == "pass":
                self.hai = False
                self._next_draw()
                return self.drain_events()
            raise RuntimeError(f"最后四张抓了不打，只能胡或过，收到 {action.kind}")

        res = self.r.validate(self._view(seat), seat, action)
        if not res.ok:
            raise RuntimeError(f"动作不合法：{res.code} {res.detail}")

        if action.kind == "win":
            self._end_win(seat, tile=self.drawn, zimo=True,
                          kan_draw=self.drawn_after_kan, hai=self.hai)
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
        if self.dead_win[seat]:
            return False
        return self.r.validate(self._view(seat), seat, Action("bloom")).ok

    def _do_bloom(self, seat: int) -> None:
        tile = self.wall.pop()               # 取的是牌墙最后一张（明牌）
        self.hands[seat][tile] += 1
        self._emit(Event("bloom", seat, tile, {"wall": len(self.wall)}))
        self._end_win(seat, tile=tile, zimo=True, bloom=True)

    def _ready(self, seat: int) -> bool:
        """听没听牌。规则里「不能有两张或以上胡牌」，所以双听不算，只认单听。"""
        return self.r.tenpai(self.hands[seat], self.fixed[seat]).ok

    def _can_ting(self, seat: int) -> bool:
        """杠后能不能先报听：还没报过、没弃胡，且当前手牌已经是合法单听。"""
        return (not self.ting.get(seat) and not self.dead_win.get(seat)
                and self._ready(seat))

    def _mark_ting(self, seat: int) -> None:
        self.ting[seat] = True
        self.piao[seat] = is_piao_hu(tuple(self.hands[seat]), tuple(self.fixed[seat]))
        self.ting_closed[seat] = not self.fixed[seat]      # 报听那一刻没副露 → 闭门听 + 闷
        waits = [tile_id(t) for t in self.r.waits(self.hands[seat], self.fixed[seat])]
        self._emit(Event("ting", seat, data={"waits": waits, "closed": self.ting_closed[seat],
                                             "piao": self.piao[seat]}))

    def _enter_kan_draw(self, seat: int) -> None:
        """进入杠后补张。只有抓牌一条路时直接抓；能开花或报听才停下来问。"""
        self.turn = seat
        self.phase = "kan_draw"
        self.last = None
        if not self._can_bloom(seat) and not self._can_ting(seat):
            self._do_draw(seat, kan=True)

    def _do_ting_declare(self, seat: int, action: Action) -> list[Event]:
        if action.kind not in ("ting", "pass"):
            raise RuntimeError(f"报听阶段只接受 ting / pass，收到 {action.kind}")
        if action.kind == "ting":
            self._mark_ting(seat)
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
        self.kans[seat] += 1
        self.after_kan = seat            # 他杠完这一下要摸补张：打出去被胡就是「流泪」
        self._enter_kan_draw(seat)

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
            self.kans[seat] += 1
            self.after_kan = seat
            self._emit(Event("kan", seat, tile, {"kind": "open", "from": src}))
            self._enter_kan_draw(seat)
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
            if not self.ting[s] or self.dead_win[s]:
                continue
            v = HandView(seat=s, seats=self.cfg.seats, hands={s: tuple(self.hands[s])},
                         fixed={s: tuple(self.fixed[s])}, last=(owner, "discard", tile))
            c = list(self.hands[s]); c[tile] += 1
            if (self.r.is_win(tuple(c), tuple(self.fixed[s])).ok
                    and self.r.waiting_on(self.hands[s], tile, tuple(self.fixed[s]))):
                out.append((s, [Action("win", tile=tile, meta={"from": owner, "rob": True})]))
        return out

    def _build_robbers(self, owner: int, tile: int) -> None:
        robbers = self._robbers(owner, tile)
        self.reactors = [(PRIORITY["win"], (s - owner) % self.cfg.seats, s, opts) for s, opts in robbers]
        self.reactors.sort(key=lambda x: (-x[0], x[1]))
        self.phase = "react" if self.reactors else "draw"

    # ---------- 摸牌 ----------
    def _do_draw(self, seat: int, kan: bool = False) -> None:
        """摸牌一律从牌头取；杠后补张也是牌头那一张，只是事件名不同。

        牌墙最后四张有特殊规矩：抓了不打。摸到就留在手里，不能打出去；
        能胡就胡（海底捞月），不能胡直接过给下一家接着摸。
        """
        if not self.wall:
            self._end_draw_game()
            return
        tile = self.wall.popleft()
        self.hands[seat][tile] += 1
        self.drawn = tile
        self.drawn_after_kan = kan
        # 「流泪」只认杠完这一下：等下一家正常摸牌了，这笔账就翻篇
        if self.after_kan is not None and not (kan and seat == self.after_kan):
            self.after_kan = None
        self.last_draw = (seat, tile)      # 摸一张记一张：荒庄时最后一张明牌归谁，就靠这个
        self._emit(Event("kan_draw" if kan else "draw", seat, tile, {"wall": len(self.wall)}))
        self.last = None
        self.hai = len(self.wall) <= 3     # 摸到的是最后四张之一
        if self.hai and not self._can_hai_win(seat):
            self.hai = False               # 胡不了：这张留着不打，下一家摸
            self._next_draw()
            return
        self.phase = "discard"

    def _can_hai_win(self, seat: int) -> bool:
        """最后四张抓进来能不能胡（海底捞月只认自摸，所以不查 waiting_on）。"""
        if self.dead_win[seat] or not self.ting[seat]:
            return False
        return self.r.is_win(tuple(self.hands[seat]), tuple(self.fixed[seat])).ok

    def _next_draw(self) -> None:
        """这一手不打了，轮到下一家摸；牌墙空了就荒庄。"""
        self.turn = (self.turn + 1) % self.cfg.seats
        self.phase = "draw"
        self.last = None
        self.drawn = None
        if not self.wall:
            self._end_draw_game()

    # ---------- 结束 ----------
    def _end_win(self, seat: int, tile: int | None, *, zimo: bool, from_seat: int | None = None,
                 rob: bool = False, bloom: bool = False, kan_draw: bool = False,
                 hai: bool = False) -> None:
        self.phase = "ended"
        self.hai = False
        # 收局牌谱：点炮/抢杠的那张还没并进手牌，展示前先补上。
        final = list(self.hands[seat])
        if tile is not None and not zimo:
            final[tile] += 1
        win_hand = [tile_id(t) for t, n in enumerate(final) for _ in range(n)]
        self.result = {
            "type": "win",
            "seat": seat,
            "tile": tile_id(tile) if tile is not None else None,
            "zimo": zimo,
            "from": from_seat,
            "rob": rob,
            "bloom": bloom,
            "kan_draw": kan_draw,
            "hai": hai,
            "hand": win_hand,
            "melds": [{"kind": m.kind, "tiles": [tile_id(t) for t in m.tiles]}
                      for m in self.fixed[seat]],
            "settle": self.settle(seat, zimo=zimo, from_seat=from_seat, rob=rob,
                                  bloom=bloom, kan_draw=kan_draw, hai=hai, tile=tile),
        }
        self._emit(Event("win", seat, tile, {"zimo": zimo, "from": from_seat, "rob": rob,
                                             "bloom": bloom, "kan_draw": kan_draw}))

    def _end_draw_game(self) -> None:
        self.phase = "ended"
        last = self.last_draw
        self.result = {"type": "draw_game", "reason": "wall_empty", "wall": 0,
                       "last_draw": None if last is None else {"seat": last[0], "tile": tile_id(last[1])}}
        self._emit(Event("draw_game", data={"reason": "wall_empty",
                                            "last_draw": self.result["last_draw"]}))

    def _men(self, seat: int) -> bool:
        """闷：整局没开过门、没亮过副露；闭门听之后杠了仍然算闷。"""
        return bool(self.ting_closed.get(seat)) or not self.fixed[seat]

    def settle(self, seat: int, *, zimo: bool, from_seat: int | None = None,
               rob: bool = False, bloom: bool = False, kan_draw: bool = False,
               hai: bool = False, tile: int | None = None) -> dict:
        """收局那一刻把番算出来：番种表在 scoring.py，这里只负责凑上下文。"""
        counts = list(self.hands[seat])
        if not zimo and tile is not None:      # 点炮/抢杠：胡的那张不在手里，先并进来再判牌型
            counts[tile] += 1
        all_counts = list(counts)
        for m in self.fixed[seat]:
            for t in m.tiles:
                all_counts[t] += 1
        # 飘胡不用谁口头声明：收局时按牌型直接打状态标记，结算也认这个形状。
        self.piao[seat] = is_piao_hu(tuple(counts), tuple(self.fixed[seat]))
        payers = []
        for s in range(self.cfg.seats):
            if s == seat:
                continue
            payers.append(scoring.Payer(
                s, dealer=s == self.dealer, men=self._men(s),
                discard=(not zimo and s == from_seat),
                hand_tiles=sum(self.hands[s]),
                tears=not zimo and s == from_seat and (rob or from_seat == self.after_kan),
            ))
        ctx = scoring.Context(
            winner=seat, dealer=self.dealer, seats=self.cfg.seats,
            zimo=zimo, bloom=bloom, kan_draw=kan_draw, hai=hai,
            closed_ting=self.ting_closed.get(seat, False),
            hand_tiles=max(0, sum(counts) - 1),   # 手里剩几张（不算胡的那张）
            all_counts=tuple(all_counts), counts=tuple(counts), melds=tuple(self.fixed[seat]),
            sanqing=all(self._men(s) for s in range(self.cfg.seats) if s != seat),
            payers=payers,
        )
        return scoring.settle(ctx, base=self.cfg.base_points, cap=self.cfg.fan_cap).to_dict()

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
            "can_win": (not self.dead_win[seat]) and self.r.is_win(tuple(self.hands[seat]), tuple(self.fixed[seat])).ok if self.phase == "discard" and self.turn == seat else False,
            "ting": {s: self.ting[s] for s in range(self.cfg.seats)},
            "piao": {s: self.piao[s] for s in range(self.cfg.seats)},
            "dead": {s: self.dead_win[s] for s in range(self.cfg.seats)},
            "dealer": self.dealer,
            "dice": None if self.dice is None else [self.dice.a, self.dice.b, self.dice.total],
            "kans": {s: self.kans.get(s, 0) for s in range(self.cfg.seats)},
            "ting_closed": {s: bool((self.ting_closed or {}).get(s)) for s in range(self.cfg.seats)},
            "drawn": tile_id(self.drawn) if self.drawn is not None and seat == self.turn else None,
            "hai": self.hai and self.turn == seat,
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
            dead=dict(self.dead_win),
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

    # 7 后开花：杠完补张那一下，牌墙最后一张正好能把牌型补成胡
    g = Game(GameConfig(seed=14))
    g.start()
    for s in range(4):
        g.hands[s] = [0] * KINDS
        g.fixed[s] = []
        g.discards[s] = []
    p4, p9 = parse_tile("4饼"), parse_tile("9饼")
    p5 = parse_tile("5饼")
    g.fixed[0] = [Meld("kan", (p4, p4, p4, p4))]
    g.hands[0] = _h("1万 2万 3万 4万 5万 6万 7条 8条 9条 9饼")
    g.wall = deque([parse_tile("1万")] * 5 + [p9])
    g.turn, g.phase, g.last, g.drawn = 0, "kan_draw", None, None
    d = g.pending()
    case("杠完就能后开花", [a.kind for a in d.legal], ["draw", "bloom", "ting"])
    ev = g.apply(Action("bloom"))
    case("后开花就是胡", (g.result["type"], g.result["seat"], g.result["bloom"]), ("win", 0, True))
    case("取走的是牌墙最后一张", (ev[0].tile, len(g.wall)), (p9, 5))

    g = Game(GameConfig(seed=18))
    g.start()
    for s in range(4):
        g.hands[s] = [0] * KINDS
        g.fixed[s] = []
        g.discards[s] = []
    g.fixed[0] = [Meld("kan", (p4, p4, p4, p4))]
    g.hands[0] = _h("1万 2万 3万 4万 5万 6万 7条 8条 9条 9饼")
    g.wall = deque([parse_tile("1万")] * 5 + [p9])
    g.turn, g.phase, g.last, g.drawn = 0, "draw", None, None
    case("普通摸牌那一下不后开花", [a.kind for a in g.pending().legal], ["draw"])

    g = Game(GameConfig(seed=16))
    g.start()
    for s in range(4):
        g.hands[s] = [0] * KINDS
        g.fixed[s] = []
        g.discards[s] = []
    g.fixed[0] = [Meld("kan", (p4, p4, p4, p4))]
    g.hands[0] = _h("1万 2万 3万 4万 5万 6万 7条 8条 9条 9饼")
    g.wall = deque([parse_tile("1万")] * 5 + [parse_tile("4饼")])
    g.turn, g.phase, g.last, g.drawn = 0, "kan_draw", None, None
    case("杠完但尾张补不成胡，也不能开花", [a.kind for a in g.pending().legal], ["draw", "ting"])

    # 杠后可以直接报听：报完自动补张，补进来的牌就是唯一能打的那张
    g = Game(GameConfig(seed=16))
    g.start()
    for s in range(4):
        g.hands[s] = [0] * KINDS
        g.fixed[s] = []
        g.discards[s] = []
    g.fixed[0] = [Meld("kan", (p4, p4, p4, p4))]
    g.hands[0] = _h("1万 2万 3万 4万 5万 6万 7条 8条 9条 9饼")
    g.wall = deque([parse_tile("1万")] * 5 + [parse_tile("4饼")])
    g.turn, g.phase, g.last, g.drawn = 0, "kan_draw", None, None

    head = g.wall[0]
    ev = g.apply(Action("ting"))
    case("杠后报听：听牌标记立起来", g.ting[0], True)
    case("杠后报听：直接补张", g.phase, "discard")
    case("杠后报听：补张从牌头摸", [e.tile for e in ev if e.type == "kan_draw"], [head])
    case("杠后报听：只能打回补张", [a.tile for a in g.pending().legal], [head])

    g = Game(GameConfig(seed=15))
    g.start()
    for s in range(4):
        g.hands[s] = [0] * KINDS
        g.fixed[s] = []
        g.discards[s] = []
    g.hands[0] = _h("1万 2万 3万 4万 5万 6万 7条 8条 9条 9饼 9饼 1饼")
    g.wall = deque([parse_tile("1万")] * 5 + [p9])
    g.turn, g.phase, g.last, g.drawn = 0, "kan_draw", None, None
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
    ev = g.apply(kan)
    case("加杠后没别的选，直接补张", g.phase, "discard")
    draws = [e for e in ev if e.type == "kan_draw"]
    case("补张从牌头摸", draws[0].tile if draws else None, before_head)
    case("补张后牌墙少一张", len(g.wall), before_wall - 1)
    case("补张后没有弃胡", g.dead_win[0], False)

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
        g.hands[1] = _h("1万 2万 3万 4万 5万 6万 5条 5条 5条 9饼 9饼 6饼 8饼")   # 单听 7 饼
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
    codes = [f["code"] for f in g.result["settle"]["payer_fans"]["0"]]
    case("抢杠结算：点炮和流泪都在", ("discard" in codes, "tears" in codes), (True, True))
    case("没报听的人抢不了杠", rob_setup(listen=False).phase, "discard")

    # 6b 手里四张直接亮出来杠，别人能抢
    g = Game(GameConfig(seed=12))
    g.start()
    for s in range(4):
        g.hands[s] = [0] * KINDS
        g.fixed[s] = []
        g.discards[s] = []
    p7 = parse_tile("7饼")
    g.hands[0] = _h("7饼 7饼 7饼 7饼 2条 3条 4条 1饼 2饼 3饼 9条 9条 9条 4饼")
    # 单听 7 饼：多留一张 8 饼就成了双听（等 7 饼和 8 饼），双听不算听，抢不了杠
    g.hands[1] = _h("1万 2万 3万 4万 5万 6万 5条 5条 5条 9饼 9饼 6饼 8饼")
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
    case("别人打的牌杠出来没人抢", g.phase, "discard")

    # 6d 抢杠优先于后开花：杠的人尾张本来能开花，也得先让人抢
    def bloom_rob_setup() -> Game:
        g = Game(GameConfig(seed=19))
        g.start()
        for s in range(4):
            g.hands[s] = [0] * KINDS
            g.fixed[s] = []
            g.discards[s] = []
            g.ting[s] = False
        g.hands[0] = _h("7饼 7饼 7饼 7饼 1万 2万 3万 4条 5条 6条 7条 8条 9条 9饼")
        g.hands[1] = _h("1万 2万 3万 4万 5万 6万 5条 5条 5条 8饼 9饼 1饼 1饼")
        g.ting[1] = True
        g.wall = deque([parse_tile("1万")] * 5 + [p9])
        g.turn, g.phase, g.last, g.drawn = 0, "discard", None, None
        g.apply(Action("kan", tile=p7, meta={"kind": "in_hand"}))
        return g

    g = bloom_rob_setup()
    case("先给抢杠，不给开花", (g.pending().seat, g.pending().kind), (1, "react"))
    g.apply(next(a for a in g.pending().legal if a.kind == "win"))
    case("抢杠胡了就没有后开花", (g.result["seat"], g.result["rob"], g.result["bloom"]), (1, True, False))

    g = bloom_rob_setup()
    for _ in range(5):                   # 抢杠的人都过，才轮到杠的人补张
        if g.phase != "react":
            break
        g.apply(Action("pass"))
    case("没人抢才轮到杠的人补张", g.phase, "kan_draw")
    case("没人抢，尾张能胡才给开花", [a.kind for a in g.pending().legal], ["draw", "bloom", "ting"])

    # 6e 放过胡就锁死本局：能胡的时候 pass，这局不准再胡
    def pass_setup() -> Game:
        """1 号报听等 7 饼，0 号打出 7 饼，1 号面前有 win / pass。"""
        g = Game(GameConfig(seed=21))
        g.start()
        for s in range(4):
            g.hands[s] = [0] * KINDS
            g.fixed[s] = []
            g.discards[s] = []
        g.hands[0] = _h("7饼 3万 4万 5万 1条 2条 3条 5饼 6饼 1饼 4饼 1饼 9条")
        g.hands[1] = _h("1万 2万 3万 4万 5万 6万 5条 5条 5条 9饼 9饼 6饼 8饼")   # 单听 7 饼
        g.ting[1] = True
        g.wall.extend([parse_tile("1万")] * 20)
        g.turn, g.phase, g.last, g.drawn = 0, "discard", None, None
        g.apply(Action("discard", tile=p7))
        return g

    g = pass_setup()
    case("听牌的人面前有胡和过", (g.pending().seat, [a.kind for a in g.pending().legal]), (1, ["win", "pass"]))
    g.apply(Action("pass"))
    case("放过胡就锁死", g.dead_win[1], True)
    g.hands[1][p7] += 1
    g.turn, g.phase, g.last = 1, "discard", None
    case("锁死之后自摸也不给胡", [a.kind for a in g.pending().legal if a.kind == "win"], [])
    case("硬报胡被裁判拦", g.r.validate(g._view(1), 1, Action("win")).code, "WIN_DEAD")

    g = pass_setup()
    g.apply(next(a for a in g.pending().legal if a.kind == "win"))
    case("胡了就不算弃胡", g.dead_win[1], False)

    # 自己摸进来的牌就能胡，却打回去：也算弃胡
    g = Game(GameConfig(seed=22))
    g.start()
    for s in range(4):
        g.hands[s] = [0] * KINDS
        g.fixed[s] = []
        g.discards[s] = []
    g.hands[1] = _h("1万 2万 3万 4万 5万 6万 1条 2条 3条 7饼 7饼 7饼 9饼")
    g.ting[1] = True
    g.wall.extend([parse_tile("1万")] * 20)
    g.hands[1][p9] += 1                       # 真实流程里摸进来的那张是加进手牌的
    g.turn, g.phase, g.last, g.drawn = 1, "discard", None, p9
    case("自摸能胡也有胡的选项", any(a.kind == "win" for a in g.pending().legal), True)
    g.apply(Action("discard", tile=p9))
    case("自摸不胡也锁死", g.dead_win[1], True)

    # 锁死的人不能后开花
    g = Game(GameConfig(seed=14))
    g.start()
    for s in range(4):
        g.hands[s] = [0] * KINDS
        g.fixed[s] = []
        g.discards[s] = []
    g.fixed[0] = [Meld("kan", (p4, p4, p4, p4))]
    g.hands[0] = _h("1万 2万 3万 4万 5万 6万 7条 8条 9条 9饼")
    g.wall = deque([parse_tile("1万")] * 5 + [p9])
    g.dead_win[0] = True
    g.turn, g.phase, g.last, g.drawn = 0, "kan_draw", None, None
    case("弃胡的人不给后开花", [a.kind for a in g.pending().legal], ["draw"])

    # 6h 双听的手：不算听牌、不许报听、也胡不了（图里那手：碰 5饼 + 445566万 + 123饼 + 77条）
    g = Game(GameConfig(seed=23))
    g.start()
    for s in range(4):
        g.hands[s] = [0] * KINDS
        g.fixed[s] = []
        g.discards[s] = []
    g.hands[0] = _h("4万 4万 5万 5万 6万 1饼 2饼 3饼 7条 7条")
    g.fixed[0] = [Meld("triplet", (p5, p5, p5))]
    g.wall.extend([parse_tile("1万")] * 20)
    g.turn, g.phase, g.last, g.drawn = 0, "discard", None, None
    case("双听不算听牌", g._ready(0), False)
    case("双听的听牌集合是 3万6万", [tile_id(t) for t in g.r.waits(g.hands[0], g.fixed[0])], ["m3", "m6"])
    g.ting[0] = True
    g.hands[0][parse_tile("6万")] += 1
    g.drawn = parse_tile("6万")
    case("双听的手不给胡", g.r.validate(g._view(0), 0, Action("win")).code, "MULTI_WAIT")

    # 6f 后开花那一下选择正常摸牌，不算弃胡
    g = Game(GameConfig(seed=14))
    g.start()
    for s in range(4):
        g.hands[s] = [0] * KINDS
        g.fixed[s] = []
        g.discards[s] = []
    g.fixed[0] = [Meld("kan", (p4, p4, p4, p4))]
    g.hands[0] = _h("1万 2万 3万 4万 5万 6万 7条 8条 9条 9饼")
    g.wall = deque([parse_tile("1万")] * 5 + [p9])
    g.turn, g.phase, g.last, g.drawn = 0, "kan_draw", None, None
    g.apply(Action("draw"))
    case("不开花不算弃胡", g.dead_win[0], False)
    g.hands[0] = _h("1万 2万 3万 4万 5万 6万 7条 8条 9条 9饼 9饼")   # 杠在桌上，所以手里 11 张就够胡
    g.ting[0] = True
    g.drawn = p9                              # 摸进来的就是这张 9 饼（单钓）
    g.turn, g.phase, g.last = 0, "discard", None
    case("不开花之后照样能胡", any(a.kind == "win" for a in g.pending().legal), True)

    # 6g 抢杠不抢也算弃胡
    g = rob_setup(listen=True)
    case("抢杠面前有胡和过", [a.kind for a in g.pending().legal], ["win", "pass"])
    g.apply(Action("pass"))
    case("不抢杠就锁死", g.dead_win[1], True)

    # 5 荒庄：牌墙摸空
    g = Game(GameConfig(seats=3, seed=3))
    g.start()
    g.wall.clear()
    g.turn, g.phase = 0, "draw"
    g.apply(Action("draw"))
    case("牌墙空了判荒庄", (g.phase, g.result["type"]), ("ended", "draw_game"))

    # 6h 牌墙最后四张：抓了不打
    g = Game(GameConfig(seed=31))
    g.start()
    for s in range(4):
        g.hands[s] = [0] * KINDS
        g.fixed[s] = []
        g.discards[s] = []
        g.ting[s] = False
    g.hands[0] = _h("1万 2万 3万 4万 5万 6万 1条 2条 3条 7条 8条 9条 9饼")
    m1 = parse_tile("1万")
    g.wall = deque([p4, m1, m1, m1])          # 摸完这张，墙里只剩 3 张：最后四张之一
    g.turn, g.phase, g.last, g.drawn = 0, "draw", None, None
    g.apply(Action("draw"))
    case("最后四张抓了不打: 不胡就直接过给下家", (g.turn, g.phase, g.drawn), (1, "draw", None))
    case("最后四张抓了不打: 牌留在手里没打出去", (sum(g.hands[0]), len(g.discards[0])), (14, 0))

    # 6i 海底捞月：摸到最后四张之一正好胡
    g = Game(GameConfig(seed=32, dealer=0))
    g.start()
    for s in range(4):
        g.hands[s] = [0] * KINDS
        g.fixed[s] = []
        g.discards[s] = []
        g.ting[s] = False
    g.hands[0] = _h("1万 2万 3万 4万 5万 6万 5条 5条 5条 7条 8条 9条 9饼")
    g.ting[0] = True
    g.wall = deque([p9, m1, m1, m1])          # 摸的 9 饼正好是最后四张之一，且凑成将
    g.turn, g.phase, g.last, g.drawn = 0, "draw", None, None
    g.apply(Action("draw"))
    case("海底捞月: 最后四张摸到能胡就给胡/过", [a.kind for a in g.pending().legal], ["win", "pass"])
    g.apply(Action("win"))
    case("海底捞月算番", ("haidi" in [f["code"] for f in g.result["settle"]["fans"]],
                          g.result["hai"]), (True, True))

    print()
    print(f"共 {fails} 条失败")
    return fails


if __name__ == "__main__":
    raise SystemExit(_selftest())
