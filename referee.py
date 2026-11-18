"""麻将裁判 —— 只判定，不驱动牌局。

它只回答三个问题，别的都不管：
    1) 这 14 张算不算胡        is_win()
    2) 这 13 张听什么、合不合法  tenpai() / waits()
    3) 这个座位现在允许做什么    legal_actions() / validate()

为什么这么切（为"牌局代码还没写、规则还没补完"留的口子）：
    * 纯函数：不持有牌局状态，所有输入都是参数 → 以后加杠、荒庄、结算，不用动判定主体。
    * 规则参数化：所有口径都在 RuleConfig；再加条件用 register_condition()，不改现有代码。
    * 牌只认编号 0..27：字符串只在边界（工具参数、日志）出现，避免"7饼 / 7万"这种混淆。
    * 结算留空：settle() 现在返回 {}，接口先占位。

和模型的分工：
    * 工具 schema 的 enum 由 legal_actions() 生成 → 它选不出非法的牌。
    * 它说"我胡了"不算数，is_win() 说算才算。

规则原文（d:/Users/C/Documents/Empty/Mahjong/规则.txt）：
    手里13张牌 / 无风牌 无发白 有红中
    胡牌条件：手里有且仅有一个对子 / 必须有刻子 双红中可以充当刻子 /
              必须有幺九 红中可以充当幺九 / 万饼条必须都有 /
              不能有两张或以上胡牌 飘胡除外
    飘胡：手里只有刻子或对子
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator, Sequence

# ---------------------------------------------------------------- 牌

WAN0, PIN0, SUO0, RED = 0, 9, 18, 27
KINDS = 28
COPIES = 4

_SUIT_BASE = {"m": WAN0, "p": PIN0, "s": SUO0}
_SUIT_CN = {"万": WAN0, "饼": PIN0, "条": SUO0}

TERMINALS = {WAN0, WAN0 + 8, PIN0, PIN0 + 8, SUO0, SUO0 + 8}  # 1/9 万饼条


def tile_name(t: int) -> str:
    """给人看的名字：1万 / 9饼 / 5条 / 红中。"""
    if t == RED:
        return "红中"
    if t < PIN0:
        return f"{t - WAN0 + 1}万"
    if t < SUO0:
        return f"{t - PIN0 + 1}饼"
    return f"{t - SUO0 + 1}条"


def tile_id(t: int) -> str:
    """给工具参数用的稳定 id：m1..m9 / p1..p9 / s1..s9 / z5。"""
    if t == RED:
        return "z5"
    if t < PIN0:
        return f"m{t - WAN0 + 1}"
    if t < SUO0:
        return f"p{t - PIN0 + 1}"
    return f"s{t - SUO0 + 1}"


def parse_tile(x) -> int:
    """接受 编号 / m1 / p7 / s3 / z5 / 1万 / 9饼 / 红中。"""
    if isinstance(x, bool):
        raise ValueError(f"看不懂的牌: {x!r}")
    if isinstance(x, int):
        if 0 <= x < KINDS:
            return x
        raise ValueError(f"牌编号越界: {x}")
    s = str(x).strip()
    low = s.lower()
    if low in ("z5", "red", "红中", "中"):
        return RED
    if len(low) == 2 and low[0] in "mps" and low[1] in "123456789":
        return _SUIT_BASE[low[0]] + int(low[1]) - 1
    if len(s) == 2 and s[1] in _SUIT_CN and s[0] in "123456789":
        return _SUIT_CN[s[1]] + int(s[0]) - 1
    raise ValueError(f"看不懂的牌: {x!r}")


def suit_of(t: int) -> str | None:
    if t == RED:
        return None
    return "m" if t < PIN0 else "p" if t < SUO0 else "s"


def counts_from(tiles: Iterable) -> tuple[int, ...]:
    c = [0] * KINDS
    for x in tiles:
        c[parse_tile(x)] += 1
    if any(v > COPIES for v in c):
        raise ValueError("同一张牌超过 4 张")
    return tuple(c)


def tiles_from(counts: Sequence[int]) -> list[int]:
    return [i for i, n in enumerate(counts) for _ in range(n)]


def _as_counts(x) -> tuple[int, ...]:
    if isinstance(x, tuple) and len(x) == KINDS and all(isinstance(v, int) for v in x):
        return x
    if isinstance(x, list) and len(x) == KINDS and all(isinstance(v, int) for v in x):
        return tuple(x)
    return counts_from(x)


# ---------------------------------------------------------------- 面子

@dataclass(frozen=True)
class Meld:
    kind: str                      # 'seq' | 'triplet' | 'kan'
    tiles: tuple[int, ...]

    @property
    def is_triplet_like(self) -> bool:
        return self.kind in ("triplet", "kan")


def _walk_melds(c: list[int], need: int, acc: list[Meld], out: list[tuple[Meld, ...]]) -> None:
    i = next((k for k, v in enumerate(c) if v), -1)
    if i < 0:
        if need == 0:
            out.append(tuple(acc))
        return
    if need == 0:
        return
    if c[i] >= 3:
        c[i] -= 3
        acc.append(Meld("triplet", (i, i, i)))
        _walk_melds(c, need - 1, acc, out)
        acc.pop()
        c[i] += 3
    if i < RED and i % 9 <= 6 and c[i + 1] and c[i + 2]:
        c[i] -= 1; c[i + 1] -= 1; c[i + 2] -= 1
        acc.append(Meld("seq", (i, i + 1, i + 2)))
        _walk_melds(c, need - 1, acc, out)
        acc.pop()
        c[i] += 1; c[i + 1] += 1; c[i + 2] += 1


def decompose(counts: Sequence[int], need_melds: int) -> Iterator[tuple[int, tuple[Meld, ...]]]:
    """穷举「将 + need_melds 组面子」的拆法，yield (将牌, 面子元组)。"""
    c = list(counts)
    for p in range(KINDS):
        if c[p] >= 2:
            c[p] -= 2
            out: list[tuple[Meld, ...]] = []
            _walk_melds(c, need_melds, [], out)
            c[p] += 2
            for melds in out:
                yield p, melds


# ---------------------------------------------------------------- 规则配置

@dataclass(frozen=True)
class RuleConfig:
    """所有会影响判定的口径。补规则先看这里，能改这里就别改判定。"""

    # 结构
    melds_total: int = 4                # 四组面子
    strict_tile_count: bool = True      # 手牌数必须 = 3*need + 2

    # 「有且仅有一个对子」
    #   麻将里的意思就是「四组面子 + 一组将」的那组将，由拆解本身保证，不另设口径。

    # 刻子
    require_triplet: bool = True
    red_pair_is_triplet: bool = True    # 双红中作将时同时算刻子

    # 幺九
    require_terminal: bool = True
    red_is_terminal: bool = True        # 红中在手里即算幺九，张数不论

    # 三色
    require_all_suits: bool = True

    # 听牌
    max_waits: int = 1                  # 不能有两张或以上胡牌
    piao_hu_exempt_waits: bool = True   # 飘胡除外
    require_ting: bool = True           # 没报听不许胡；报听后手牌锁死（杠除外）

    # 动作
    allow_chi: bool = True
    allow_pon: bool = True
    allow_kan: bool = True
    allow_closed_kan: bool = False      # 这局不许暗杠；明杠、加杠照旧

    # 以后补规则的容器，避免为了加个开关去改 dataclass
    extra: dict = field(default_factory=dict)


RULES_V1 = RuleConfig()


# ---------------------------------------------------------------- 判定

@dataclass(frozen=True)
class WinContext:
    counts: tuple[int, ...]
    fixed: tuple[Meld, ...]
    melds: tuple[Meld, ...]
    pair: int
    cfg: RuleConfig


@dataclass(frozen=True)
class WinResult:
    ok: bool
    code: str
    detail: str = ""
    pair: int | None = None
    melds: tuple[Meld, ...] = ()

    def __bool__(self) -> bool:
        return self.ok


Condition = Callable[[WinContext], tuple[bool, str]]


def cond_pair_unique(ctx: WinContext) -> tuple[bool, str]:
    # 麻将里「手里有且仅有一个对子」指的就是四组面子 + 一组将里的那组将。
    # 拆解只产出这一组将，所以恒成立；留着它是为了让条件链和规则原文逐条对应。
    return True, f"PAIR({tile_name(ctx.pair)})"


def cond_triplet(ctx: WinContext) -> tuple[bool, str]:
    if not ctx.cfg.require_triplet:
        return True, "TRIPLET_NOT_REQUIRED"
    for m in ctx.melds:
        if m.is_triplet_like:
            return True, f"TRIPLET({tile_name(m.tiles[0])})"
    for m in ctx.fixed:
        if m.is_triplet_like:
            return True, f"TRIPLET_FIXED({tile_name(m.tiles[0])})"
    if ctx.cfg.red_pair_is_triplet and ctx.pair == RED and ctx.counts[RED] >= 2:
        return True, "TRIPLET_BY_RED_PAIR"
    return False, "NO_TRIPLET"


def cond_terminal(ctx: WinContext) -> tuple[bool, str]:
    if not ctx.cfg.require_terminal:
        return True, "TERMINAL_NOT_REQUIRED"
    for i, n in enumerate(ctx.counts):
        if not n:
            continue
        if i in TERMINALS:
            return True, f"TERMINAL({tile_name(i)})"
        if ctx.cfg.red_is_terminal and i == RED:
            return True, "TERMINAL_BY_RED"
    for m in ctx.fixed:
        for t in m.tiles:
            if t in TERMINALS:
                return True, f"TERMINAL_FIXED({tile_name(t)})"
            if ctx.cfg.red_is_terminal and t == RED:
                return True, "TERMINAL_BY_RED_FIXED"
    return False, "NO_TERMINAL"


def cond_all_suits(ctx: WinContext) -> tuple[bool, str]:
    if not ctx.cfg.require_all_suits:
        return True, "SUITS_NOT_REQUIRED"
    seen = {suit_of(i) for i, n in enumerate(ctx.counts) if n}
    for m in ctx.fixed:
        seen.update(suit_of(t) for t in m.tiles)
    seen.discard(None)
    missing = {"m", "p", "s"} - seen
    if missing:
        return False, f"MISSING_SUIT({''.join(sorted(missing))})"
    return True, "ALL_SUITS"


DEFAULT_CONDITIONS: tuple[tuple[str, Condition], ...] = (
    ("pair", cond_pair_unique),
    ("triplet", cond_triplet),
    ("terminal", cond_terminal),
    ("suits", cond_all_suits),
)


@dataclass(frozen=True)
class TenpaiResult:
    ok: bool
    code: str
    waits: tuple[int, ...]
    detail: str = ""
    piao_hu: bool = False

    def __bool__(self) -> bool:
        return self.ok


@dataclass(frozen=True)
class Result:
    ok: bool
    code: str
    detail: str = ""

    def __bool__(self) -> bool:
        return self.ok


# ---------------------------------------------------------------- 动作

@dataclass(frozen=True)
class Action:
    kind: str                    # discard | pon | kan | chi | win | pass
    tile: int | None = None
    combo: tuple[int, ...] = ()  # 吃牌时手里的两张
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {"kind": self.kind}
        if self.tile is not None:
            d["tile"] = tile_id(self.tile)
            d["name"] = tile_name(self.tile)
        if self.combo:
            d["combo"] = [tile_id(t) for t in self.combo]
        if self.meta:
            d["meta"] = dict(self.meta)
        return d


@dataclass
class HandView:
    """给裁判看的一张桌子。只放它该知道的东西；别人手牌可以不给。"""

    seat: int
    seats: int
    hands: dict[int, tuple[int, ...]] = field(default_factory=dict)
    fixed: dict[int, tuple[Meld, ...]] = field(default_factory=dict)
    discards: dict[int, tuple[int, ...]] = field(default_factory=dict)
    wall_left: int | None = None
    last: tuple[int, str, int] | None = None   # (出牌者, 'discard', 牌)
    ting: dict[int, bool] = field(default_factory=dict)   # 谁已经报听了
    drawn: int | None = None                    # 自己回合刚摸进来的那张


class Referee:
    """裁判本体。无状态，所有输入走参数，方便以后加杠/荒庄/结算。"""

    def __init__(self, config: RuleConfig = RULES_V1) -> None:
        self.cfg = config
        self.conditions: list[tuple[str, Condition]] = list(DEFAULT_CONDITIONS)

    # ---- 扩展口：加一条新规则，不动现有代码 ----
    def register_condition(self, name: str, fn: Condition, *, before: str | None = None) -> None:
        item = (name, fn)
        if before is None:
            self.conditions.append(item)
            return
        idx = next((k for k, (n, _) in enumerate(self.conditions) if n == before), len(self.conditions))
        self.conditions.insert(idx, item)

    # ---- 胡牌 ----
    def is_win(self, tiles, fixed: Sequence[Meld] = ()) -> WinResult:
        counts = _as_counts(tiles)
        need = self.cfg.melds_total - len(fixed)
        if need < 0:
            return WinResult(False, "TOO_MANY_FIXED_MELDS", f"固定面子 {len(fixed)} 组，超过 {self.cfg.melds_total}")
        if self.cfg.strict_tile_count:
            want = 3 * need + 2
            if sum(counts) != want:
                return WinResult(False, "TILE_COUNT", f"手牌 {sum(counts)} 张，应为 {want} 张")
        tried = 0
        first_fail = ""
        for pair, melds in decompose(counts, need):
            tried += 1
            ctx = WinContext(counts, tuple(fixed), melds, pair, self.cfg)
            fail = ""
            for _, cond in self.conditions:
                ok, code = cond(ctx)
                if not ok:
                    fail = code
                    break
            if not fail:
                return WinResult(True, "WIN", "", pair, melds)
            first_fail = first_fail or fail
        if not tried:
            return WinResult(False, "NO_DECOMPOSITION", "凑不成四组面子 + 一组将")
        return WinResult(False, "CONDITION_FAILED", first_fail)

    # ---- 听牌 ----
    def waits(self, tiles13, fixed: Sequence[Meld] = ()) -> list[int]:
        counts = _as_counts(tiles13)
        used = list(counts)
        for m in fixed:
            for t in m.tiles:
                used[t] += 1
        out: list[int] = []
        for t in range(KINDS):
            if used[t] >= COPIES:
                continue
            c = list(counts)
            c[t] += 1
            if self.is_win(tuple(c), fixed).ok:
                out.append(t)
        return out

    def _piao_hu_shape(self, counts: Sequence[int], fixed: Sequence[Meld]) -> bool:
        """飘胡：手里只有刻子或对子（没有顺子），且剩两对可碰。"""
        for m in fixed:
            if not m.is_triplet_like:
                return False
        c = list(counts)

        def walk() -> bool:
            i = next((k for k, v in enumerate(c) if v >= 3), -1)
            if i < 0:
                pairs = [k for k, v in enumerate(c) if v == 2]
                return all(v in (0, 2) for v in c) and len(pairs) == 2
            c[i] -= 3
            ok = walk()
            c[i] += 3
            return ok

        return walk()

    def tenpai(self, tiles13, fixed: Sequence[Meld] = ()) -> TenpaiResult:
        counts = _as_counts(tiles13)
        ws = self.waits(counts, fixed)
        if not ws:
            return TenpaiResult(False, "NOT_TENPAI", (), "一张都胡不了")
        if len(ws) == 1:
            return TenpaiResult(True, "SINGLE_WAIT", tuple(ws), f"单听 {tile_name(ws[0])}")
        if self.cfg.piao_hu_exempt_waits and self._piao_hu_shape(counts, fixed):
            return TenpaiResult(True, "PIAO_HU_EXEMPT", tuple(ws), "飘胡，可多面听", True)
        names = "、".join(tile_name(t) for t in ws)
        return TenpaiResult(False, "MULTI_WAIT", tuple(ws), f"听 {len(ws)} 张：{names}")

    # ---- 动作合法性 ----
    def _chi_combos(self, counts: Sequence[int], t: int) -> list[tuple[int, int]]:
        s = suit_of(t)
        if s is None:
            return []
        base = _SUIT_BASE[s]
        rank = t - base
        out = []
        for a, b in ((rank - 2, rank - 1), (rank - 1, rank + 1), (rank + 1, rank + 2)):
            if 0 <= a <= 8 and 0 <= b <= 8 and counts[base + a] and counts[base + b]:
                out.append((base + a, base + b))
        return out

    def legal_actions(self, view: HandView, seat: int | None = None) -> list[Action]:
        seat = view.seat if seat is None else seat
        if seat not in view.hands:
            raise KeyError(f"没有 {seat} 号座的手牌")
        counts = _as_counts(view.hands[seat])
        fixed = tuple(view.fixed.get(seat, ()))
        out: list[Action] = []

        own_turn = view.last is None or view.last[0] == seat
        ting = bool(view.ting.get(seat))
        if own_turn:
            if self.is_win(counts, fixed).ok and (not self.cfg.require_ting or ting):
                out.append(Action("win"))
            if ting:
                # 报听之后手牌锁死：只能把刚摸进来的那张打回去（杠例外）
                if view.drawn is not None and counts[view.drawn]:
                    out.append(Action("discard", tile=view.drawn))
            else:
                for t in range(KINDS):
                    if counts[t]:
                        out.append(Action("discard", tile=t))
            if self.cfg.allow_kan:
                for t in range(KINDS):
                    if self.cfg.allow_closed_kan and counts[t] >= 4:
                        out.append(Action("kan", tile=t, meta={"kind": "closed"}))
                    elif counts[t] >= 1 and any(m.is_triplet_like and m.tiles[0] == t for m in fixed):
                        out.append(Action("kan", tile=t, meta={"kind": "added"}))
            return out

        src, kind, tile = view.last
        if kind == "discard":
            c = list(counts); c[tile] += 1
            if self.is_win(tuple(c), fixed).ok and (not self.cfg.require_ting or ting):
                out.append(Action("win", tile=tile, meta={"from": src}))
            if not ting:
                if self.cfg.allow_pon and counts[tile] >= 2:
                    out.append(Action("pon", tile=tile, meta={"from": src}))
            if self.cfg.allow_kan and counts[tile] >= 3:
                out.append(Action("kan", tile=tile, meta={"kind": "open", "from": src}))
            if not ting and self.cfg.allow_chi and src == (seat - 1) % view.seats:
                for combo in self._chi_combos(counts, tile):
                    out.append(Action("chi", tile=tile, combo=combo, meta={"from": src}))
        out.append(Action("pass"))
        return out

    def validate(self, view: HandView, seat: int, action: Action) -> Result:
        if seat not in view.hands:
            return Result(False, "NO_SEAT", f"没有 {seat} 号座")
        counts = _as_counts(view.hands[seat])
        fixed = tuple(view.fixed.get(seat, ()))
        own_turn = view.last is None or view.last[0] == seat

        if action.kind == "discard":
            if not own_turn:
                return Result(False, "NOT_YOUR_TURN", "还没轮到你出牌")
            if action.tile is None or counts[action.tile] <= 0:
                return Result(False, "NOT_IN_HAND", "这张不在手里")
            if view.ting.get(seat) and view.drawn is not None and action.tile != view.drawn:
                return Result(False, "TING_LOCKED", "报听后只能把刚摸进来的那张打回去")
            return Result(True, "OK")
        if action.kind == "win":
            if self.cfg.require_ting and not view.ting.get(seat):
                return Result(False, "NEED_TING", "还没报听，不能胡")
            c = list(counts)
            if view.last and view.last[0] != seat and view.last[1] == "discard":
                c[view.last[2]] += 1
            r = self.is_win(tuple(c), fixed)
            return Result(r.ok, r.code if r.ok else "ILLEGAL_WIN", r.detail)
        if action.kind == "kan" and own_turn:
            kind = (action.meta or {}).get("kind")
            if action.tile is None:
                return Result(False, "NO_TILE", "没说杠哪张")
            if kind == "closed":
                if not self.cfg.allow_closed_kan:
                    return Result(False, "KAN_CLOSED", "这局不许暗杠")
                if counts[action.tile] < 4:
                    return Result(False, "NOT_ENOUGH", f"暗杠要手里四张{tile_name(action.tile)}")
                return Result(True, "OK")
            if kind == "added":
                has_pong = any(m.kind == "triplet" and m.tiles[0] == action.tile for m in fixed)
                if counts[action.tile] < 1 or not has_pong:
                    return Result(False, "NO_PONG", "加杠要先有这组碰，且手里还有一张")
                return Result(True, "OK")
            return Result(False, "KAN_KIND", f"自己回合只能暗杠或加杠，收到 {kind}")
        if action.kind in ("pon", "kan", "chi"):
            if view.ting.get(seat) and action.kind in ("pon", "chi"):
                return Result(False, "TING_LOCKED", "报听之后不能碰、不能吃")
            if view.last is None or view.last[0] == seat or view.last[1] != "discard":
                return Result(False, "NO_DISCARD_TO_CLAIM", "没有可吃的牌")
            t = view.last[2]
            if action.tile is not None and action.tile != t:
                return Result(False, "TILE_MISMATCH", "报的牌和桌上那张不一致")
            need = {"pon": 2, "kan": 3}.get(action.kind)
            if need and counts[t] < need:
                return Result(False, "NOT_ENOUGH", f"手里只有 {counts[t]} 张{tile_name(t)}")
            if action.kind == "chi":
                if not self.cfg.allow_chi:
                    return Result(False, "CHI_DISABLED", "规则不允许吃")
                if view.last[0] != (seat - 1) % view.seats:
                    return Result(False, "CHI_NOT_DOWNSTREAM", "只能吃上家")
                if any(counts[a] <= 0 for a in action.combo):
                    return Result(False, "NOT_IN_HAND", "吃牌的两张不在手里")
            return Result(True, "OK")
        if action.kind == "pass":
            return Result(True, "OK")
        return Result(False, "UNKNOWN_ACTION", f"不认识的动作 {action.kind}")

    # ---- 给模型看的工具 ----
    def tools_for(self, actions: Sequence[Action]) -> list[dict]:
        """按可用动作生成 OpenAI tools 定义，enum 就是合法值。

        约定：手牌也用 tile_id 表示（m1/p7/z5），模型看到什么就回什么，天然对齐。
        """
        tools: list[dict] = []
        kinds = {a.kind for a in actions}
        if "discard" in kinds:
            tiles = sorted({tile_id(a.tile) for a in actions if a.kind == "discard" and a.tile is not None})
            tools.append(_fn("discard", "打出一张手牌", {"tile": {"type": "string", "enum": tiles}}, ["tile"]))
        if "pon" in kinds:
            tiles = sorted({tile_id(a.tile) for a in actions if a.kind == "pon" and a.tile is not None})
            tools.append(_fn("pon", "碰这张牌", {"tile": {"type": "string", "enum": tiles}}, ["tile"]))
        if "kan" in kinds:
            tiles = sorted({tile_id(a.tile) for a in actions if a.kind == "kan" and a.tile is not None})
            tools.append(_fn("kan", "杠这张牌", {"tile": {"type": "string", "enum": tiles}}, ["tile"]))
        if "chi" in kinds:
            combos = sorted({"+".join(tile_id(t) for t in a.combo) for a in actions if a.kind == "chi"})
            tools.append(_fn("chi", "吃这张牌，给出你手里要用的两张", {"combo": {"type": "string", "enum": combos}}, ["combo"]))
        if "win" in kinds:
            tools.append(_fn("win", "宣布胡牌", {}, []))
        if "pass" in kinds:
            tools.append(_fn("pass", "过，不要这张牌", {}, []))
        return tools

    def parse_tool_call(self, name: str, args: dict) -> Action:
        args = args or {}
        if name == "discard":
            return Action("discard", tile=parse_tile(args.get("tile")))
        if name in ("pon", "kan"):
            return Action(name, tile=parse_tile(args.get("tile")))
        if name == "chi":
            combo = str(args.get("combo", "")).split("+")
            return Action("chi", combo=tuple(parse_tile(x) for x in combo if x))
        if name == "win":
            return Action("win")
        if name == "pass":
            return Action("pass")
        if name == "ting":
            return Action("ting")
        raise ValueError(f"不认识的动作 {name}")

    # ---- 以后补的东西，先把位置占住 ----
    def settle(self, ctx: dict) -> dict:
        """TODO：结算/番分以后补。现在返回空表，保证调用方已经在用这个接口。"""
        return {}

    def is_exhausted(self, view: HandView) -> bool:
        """荒庄判定（最简版：牌墙摸完）。以后要改流局规则，改这里。"""
        return view.wall_left is not None and view.wall_left <= 0


def _fn(name: str, desc: str, props: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required},
        },
    }


# ---------------------------------------------------------------- 自测

def _h(s: str) -> tuple[int, ...]:
    return counts_from(s.split())


def _selftest() -> int:
    r = Referee()
    fails = 0

    def case(name: str, got, want) -> None:
        nonlocal fails
        ok = got == want
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
        if not ok:
            fails += 1
            print(f"      期望 {want}，实际 {got}")

    # 1 标准胡：四组面子 + 双红中当将（同时算刻子和幺九）
    hand = "1万 2万 3万 4万 5万 6万 7饼 8饼 9饼 4条 5条 6条 红中 红中"
    res = r.is_win(_h(hand))
    case("标准胡（红中双用）", (res.ok, res.code), (True, "WIN"))

    # 2 打 1万 后单吊红中 → 合法
    h13 = "1万 2万 3万 4万 5万 6万 7饼 8饼 9饼 4条 5条 6条 红中"
    t = r.tenpai(_h(h13))
    case("打1万后单吊红中", (t.ok, len(t.waits)), (True, 1))
    case("单吊的是红中", [tile_name(x) for x in t.waits], ["红中"])

    # 3 打红中后：补 4万/7万 都凑不出刻子 → 根本不是听牌（比双听更严重）
    h13b = "1万 1万 2万 3万 4万 5万 6万 7饼 8饼 9饼 4条 5条 6条"
    tb = r.tenpai(_h(h13b))
    case("打红中后是死手，不是听牌", (tb.ok, tb.code, tb.waits), (False, "NOT_TENPAI", ()))

    # 4 边张 12 等 3：单听，合法
    edge = "1万 2万 4万 5万 6万 7饼 8饼 9饼 4条 5条 6条 红中 红中"
    te = r.tenpai(_h(edge))
    case("12等3 判单听合法", (te.ok, [tile_name(x) for x in te.waits]), (True, ["3万"]))

    # 5 两面 34 等 2/5：违规
    tw = "3万 4万 5万 6万 7万 7饼 8饼 9饼 4条 5条 6条 红中 红中"
    rw = r.tenpai(_h(tw))
    case("34等2/5 判双听违规", (rw.ok, rw.code), (False, "MULTI_WAIT"))

    # 6 飘胡：三刻 + 两对，双碰听，豁免
    ph = "1万 1万 1万 2饼 2饼 2饼 3条 3条 3条 4条 4条 5条 5条"
    tp = r.tenpai(_h(ph))
    case("飘胡双碰听获豁免", (tp.ok, tp.code, tp.piao_hu), (True, "PIAO_HU_EXEMPT", True))

    # 7 无刻子 → 不胡
    nt = "1万 1万 2万 3万 4万 5万 6万 7万 7饼 8饼 9饼 4条 5条 6条"
    rn = r.is_win(_h(nt))
    case("无刻子判不胡", (rn.ok, rn.code), (False, "CONDITION_FAILED"))

    # 8 缺一门 → 不胡
    ms = "1万 1万 1万 2万 3万 4万 5万 6万 7万 7饼 8饼 9饼 9饼 9饼"
    rm = r.is_win(_h(ms))
    case("缺条判不胡", (rm.ok, "MISSING" in rm.detail), (False, True))

    # 9 无幺九 → 不胡
    nt2 = "2万 2万 2万 3万 4万 5万 6万 7万 8万 2条 3条 4条 8饼 8饼"
    rn2 = r.is_win(_h(nt2))
    case("无幺九判不胡", (rn2.ok, "NO_TERMINAL" in rn2.detail), (False, True))

    # 10 七对（三门齐全）：凑不成四面子一将，不算胡
    qd = "1万 1万 2万 2万 3万 3万 4饼 4饼 5饼 5饼 6条 6条 7条 7条"
    rq = r.is_win(_h(qd))
    case("七对凑不成四面子一将", (rq.ok, rq.code), (False, "NO_DECOMPOSITION"))

    # 10b 牌面上有两三副对子、但都被拆进顺子 —— 按麻将读法就是能胡
    dup = "1万 1万 1万 2万 3万 3万 4万 4万 5万 7饼 8饼 9饼 5条 5条"
    case("三副对子拆进顺子，能胡", r.is_win(_h(dup)).ok, True)

    # 10c 杠：固定一组杠（4 张），手上剩 11 张，仍按四面子一将判
    fixed_kan = (Meld("kan", (WAN0, WAN0, WAN0, WAN0)),)
    rk = r.is_win(_h("2万 3万 4万 7饼 8饼 9饼 4条 5条 6条 红中 红中"), fixed_kan)
    case("带一组杠的胡牌", (rk.ok, rk.code), (True, "WIN"))

    # 11 动作：该碰的能碰，不该胡的不能胡
    view = HandView(
        seat=0, seats=3,
        hands={0: _h("1万 1万 2万 3万 4万 5万 6万 7饼 8饼 9饼 4条 5条 6条"), 1: _h("1万 1万 1万"), 2: _h("2饼 2饼 2饼")},
        fixed={}, wall_left=40,
        last=(2, "discard", parse_tile("1万")),
    )
    acts = r.legal_actions(view, 0)
    kinds = {a.kind for a in acts}
    case("有碰且没胡", ("pon" in kinds, "win" in kinds), (True, False))
    case("工具 enum 只给合法牌", [t["function"]["parameters"]["properties"]["tile"]["enum"] for t in r.tools_for(acts) if t["function"]["name"] == "pon"], [["m1"]])

    bad = r.validate(view, 0, Action("win"))
    case("没报听就胡被拒", (bad.ok, bad.code), (False, "NEED_TING"))

    # 11b 报听：手牌锁死、禁碰禁吃、可以杠
    t14 = _h("1万 1万 2万 3万 4万 5万 6万 7饼 8饼 9饼 4条 5条 6条 9条")
    vT = HandView(seat=0, seats=3, hands={0: t14}, fixed={}, last=None,
                  ting={0: True}, drawn=parse_tile("9条"))
    kT = {a.kind for a in r.legal_actions(vT, 0)}
    case("报听后只能打摸进来的那张", [ (a.kind, a.tile) for a in r.legal_actions(vT, 0) if a.kind == "discard"],
         [("discard", parse_tile("9条"))])
    case("报听后打别的牌被拒", r.validate(vT, 0, Action("discard", tile=parse_tile("1万"))).code, "TING_LOCKED")
    case("报听后不许碰", r.validate(
        HandView(seat=0, seats=3, hands={0: _h("1万 1万 2万 3万 4万 5万 6万 7饼 8饼 9饼 4条 5条"), 1: _h("1万 1万 1万"), 2: _h("2饼 2饼 2饼")},
                 fixed={}, wall_left=40, last=(2, "discard", parse_tile("1万")), ting={0: True}),
        0, Action("pon", tile=parse_tile("1万"))).code, "TING_LOCKED")

    # 11c 报听后加杠照旧可以做（用户口径：听牌之后能杠）
    vK = HandView(seat=0, seats=3,
                  hands={0: _h("7饼 1万 2万 3万 4条 5条 6条 8条 8条 9饼 9饼")},
                  fixed={0: (Meld("triplet", (parse_tile("7饼"),) * 3),)}, last=None,
                  ting={0: True}, drawn=parse_tile("9饼"))
    KK = [a for a in r.legal_actions(vK, 0) if a.kind == "kan"]
    case("报听后还能加杠", [(a.tile, (a.meta or {}).get("kind")) for a in KK],
         [(parse_tile("7饼"), "added")])

    # 12 出牌校验
    v2 = HandView(seat=0, seats=3, hands={0: _h(h13)}, last=None)
    case("打红中（手里有）通过", r.validate(v2, 0, Action("discard", tile=RED)).ok, True)
    case("打 9条（手里没有）被拒", r.validate(v2, 0, Action("discard", tile=parse_tile("9条"))).code, "NOT_IN_HAND")

    print()
    print(f"共 {fails} 条失败")
    return fails


def _demo() -> None:
    r = Referee()
    hand = _h("1万 1万 2万 3万 4万 5万 6万 7饼 8饼 9饼 4条 5条 6条 红中")
    view = HandView(seat=0, seats=3, hands={0: hand}, last=None, wall_left=60)
    acts = r.legal_actions(view, 0)
    print("合法动作：", [a.to_dict() for a in acts][:6], f"... 共 {len(acts)} 个")
    import json
    print("工具定义：", json.dumps(r.tools_for(acts), ensure_ascii=False))
    print("手牌：", " ".join(tile_id(t) for t in tiles_from(hand)))


if __name__ == "__main__":
    raise SystemExit(_selftest())
