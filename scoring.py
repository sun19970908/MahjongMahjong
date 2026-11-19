"""结算与加番 —— 规则里那串番种只在这一个文件里定义。

规则原文（规则.txt「结算加番规则（全都能叠加）」）：
    赢了加一番：庄家 闭门听（之后杠仍然是闭门听） 自摸 四归一（有四张相同牌，可叠加）
                三红中 手把一 飘胡 前开花（杠后马上自摸）后开花
                三清（三家闷）海底捞月（摸到最后四张牌之一胡牌）
    输了加一番：庄家 点炮（被抢杠算点炮） 闷（未开门无附露，闭门听之后杠仍然是闷）
                手把一 流泪（被抢杠或者杠后马上点炮）

设计口径（一手算一个总番，番种逐条拆开相加）：
    * 每个番种 1 番，全部叠加；四归一看「整手这个牌种一共几张」，
      杠、叉、吃都算进来，几种牌各凑四张就叠几番；
    * 三红中 1 番；红中凑齐四张时再叠一个四归一，共 2 番；
    * 闷、庄家、手把一、点炮、流泪这些输家番，一手只加一次，
      不按付款人数重复（三家都闷也只有 1 番闷）；
    * 三清本身 1 番，三家全闷时自然叠上那 1 番闷，共 2 番；
    * 分数 = 底分 × 2^总番（封顶 8 番）：自摸三家同额付，点炮只有那家付。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from referee import KINDS, RED, Meld, is_piao_hu

BASE_POINTS = 1
FAN_CAP = 8            # 封顶 8 番（256 分），防止杠多的局炸掉


@dataclass(frozen=True)
class Fan:
    code: str
    label: str
    note: str = ""
    per: int = 1       # 一份几番


# 赢家番：加番顺序 = 展示顺序
WIN_FANS: tuple[Fan, ...] = (
    Fan("zimo", "自摸", "自己摸上来的"),
    Fan("dealer", "庄家", "庄家胡牌"),
    Fan("closed_ting", "闭门听", "报听时手里一张副露都没有（之后杠了照样算）"),
    Fan("si_gui", "四归一", "一种牌整手凑齐四张，可叠加"),
    Fan("san_hong", "三红中", "凑够三张红中"),
    Fan("hand_one", "手把一", "四组面子全亮出去，手里只剩一张等将"),
    Fan("piao", "飘胡", "整手只有刻子和对子，没有顺子"),
    Fan("front_bloom", "前开花", "杠后补张那一下马上自摸"),
    Fan("back_bloom", "后开花", "胡的是牌墙最后一张"),
    Fan("sanqing", "三清", "另外三家全闷"),
    Fan("haidi", "海底捞月", "摸到牌墙最后四张之一胡牌"),
)

# 输家番：跟着这一手的输家走，一手只加一次
LOSE_FANS: tuple[Fan, ...] = (
    Fan("dealer", "庄家", "庄家点炮/被自摸"),
    Fan("discard", "点炮", "打出这张牌喂了胡（被抢杠也算）"),
    Fan("men", "闷", "整局没开过门、没亮过副露（之后杠了照样算）"),
    Fan("hand_one", "手把一", "四组面子全亮出去，手里只剩一张"),
    Fan("tears", "流泪", "被抢杠，或者杠完马上点炮"),
)

FAN_BY_CODE = {f.code: f for f in WIN_FANS + LOSE_FANS}


@dataclass
class Payer:
    """一个付款人在收局那一刻的样子。"""

    seat: int
    dealer: bool = False
    discard: bool = False      # 点炮（被抢杠也算）
    men: bool = False          # 闷
    hand_tiles: int = 13       # 手里还剩几张
    tears: bool = False        # 被抢杠 / 杠后马上点炮


@dataclass
class Context:
    """结算要用到的一切，都由牌局在收局那一刻填好交进来。"""

    winner: int                       # 引擎下标
    dealer: int                       # 引擎下标
    seats: int = 4
    zimo: bool = False
    bloom: bool = False               # 后开花
    kan_draw: bool = False            # 胡的是杠后补的张（前开花）
    hai: bool = False                 # 胡的是牌墙最后四张之一（海底捞月）
    sanqing: bool = False             # 另外三家全闷（三清）
    closed_ting: bool = False         # 报听那一刻是闭门
    hand_tiles: int = 0               # 收局时赢家手里还有几张（不算胡的那张）
    all_counts: tuple[int, ...] = ()  # 手牌 + 副露合计，用来判四归一/三红中
    counts: tuple[int, ...] = ()      # 赢家手牌（含胡的那张）
    melds: tuple[Meld, ...] = ()      # 赢家副露
    payers: list[Payer] = field(default_factory=list)


@dataclass
class Settlement:
    lines: list[Fan] = field(default_factory=list)           # 赢家番 + 输家番
    total: int = 0
    points: int = 0                                          # 每个付款人各付多少
    payers: dict[int, int] = field(default_factory=dict)     # 引擎下标 → 该付多少

    def text(self) -> str:
        """「自摸、闷、三清 = 3 番，8 分」，重复的番合并成 ×N。"""
        parts: list[str] = []
        counts: dict[str, int] = {}
        for f in self.lines:
            if f.label not in counts:
                parts.append(f.label)
                counts[f.label] = 0
            counts[f.label] += f.per
        detail = "、".join(f"{p}×{counts[p]}" if counts[p] > 1 else p for p in parts) or "无番"
        return f"{detail} = {self.total} 番，{self.points} 分"

    def to_dict(self) -> dict:
        return {
            "fans": [{"code": f.code, "label": f.label, "fan": f.per, "note": f.note} for f in self.lines],
            "total": self.total,
            "points": self.points,
            "payers": {str(k): v for k, v in self.payers.items()},
            "text": self.text(),
        }


def points_for(fan: int, base: int = BASE_POINTS, cap: int = FAN_CAP) -> int:
    """番 → 分：底分 × 2^番，封顶。"""
    return base * (2 ** min(max(fan, 0), cap))


def _win_lines(ctx: Context) -> list[Fan]:
    out: list[Fan] = []

    def add(code: str) -> None:
        out.append(FAN_BY_CODE[code])

    if ctx.zimo:
        add("zimo")
    if ctx.winner == ctx.dealer:
        add("dealer")
    if ctx.closed_ting:
        add("closed_ting")

    if ctx.all_counts:
        for n in ctx.all_counts:
            if n >= 4:
                add("si_gui")               # 杠、叉、吃凑出的四张都算
        if ctx.all_counts[RED] >= 3:
            add("san_hong")                 # 红中四张时自动再叠一个四归一

    if ctx.hand_tiles == 1:
        add("hand_one")
    if ctx.counts and is_piao_hu(ctx.counts, ctx.melds):
        add("piao")
    if ctx.kan_draw and ctx.zimo:
        add("front_bloom")
    if ctx.bloom:
        add("back_bloom")
    if ctx.sanqing:
        add("sanqing")
    if ctx.hai and ctx.zimo:
        add("haidi")
    return out


def _lose_lines(payers: list[Payer]) -> list[Fan]:
    """输家番：一手每类只加一次，不管几个付款人。"""
    out: list[Fan] = []

    def add(code: str) -> None:
        out.append(FAN_BY_CODE[code])

    if any(p.dealer for p in payers):
        add("dealer")
    if any(p.discard for p in payers):
        add("discard")
    if any(p.men for p in payers):
        add("men")
    if any(p.hand_tiles == 1 for p in payers):
        add("hand_one")
    if any(p.tears for p in payers):
        add("tears")
    return out


def settle(ctx: Context, *, base: int = BASE_POINTS, cap: int = FAN_CAP) -> Settlement:
    """一手算一个总番：赢家番 + 输家番逐条相加，再按番算分。"""
    s = Settlement()
    s.lines = _win_lines(ctx) + _lose_lines(ctx.payers)
    s.total = sum(f.per for f in s.lines)
    s.points = points_for(s.total, base, cap)
    s.payers = {p.seat: s.points for p in ctx.payers}
    return s


def _selftest() -> int:
    fails = 0

    def case(name, got, want):
        nonlocal fails
        ok = got == want
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
        if not ok:
            fails += 1
            print(f"      期望 {want}，实际 {got}")

    def codes(s: Settlement) -> list[str]:
        return [f.code for f in s.lines]

    base = dict(winner=2, dealer=0, seats=4)
    others = [Payer(i) for i in (0, 1, 3)]

    s = settle(Context(**base, zimo=True, payers=others))
    case("自摸", (codes(s), s.total, s.points), (["zimo"], 1, 2))
    case("自摸三家同额付", s.payers, {0: 2, 1: 2, 3: 2})

    s = settle(Context(**base, zimo=True, closed_ting=True, payers=others))
    case("闭门自摸 = 闭门听 + 自摸", (codes(s), s.total, s.points), (["zimo", "closed_ting"], 2, 4))

    s = settle(Context(**base, zimo=True, closed_ting=True, sanqing=True,
                       payers=[Payer(i, men=True) for i in (0, 1, 3)]))
    case("自摸 + 三清 = 自摸 + 闷 + 三清", (codes(s), s.total), (["zimo", "closed_ting", "sanqing", "men"], 4))

    s = settle(Context(**base, zimo=True, sanqing=True,
                       payers=[Payer(i, men=True) for i in (0, 1, 3)]))
    case("三家都闷，闷只加一次", (codes(s), s.total), (["zimo", "sanqing", "men"], 3))

    s = settle(Context(**base, closed_ting=True,
                       payers=[Payer(0, discard=True, men=True)]))
    case("闷点炮给闭门听 = 闭门听 + 点炮 + 闷", (codes(s), s.total, s.points),
         (["closed_ting", "discard", "men"], 3, 8))

    s = settle(Context(**base, payers=[Payer(0, dealer=True, discard=True)]))
    case("庄家点炮", (codes(s), s.total), (["dealer", "discard"], 2))

    s = settle(Context(**base, payers=[Payer(0, discard=True, tears=True)]))
    case("抢杠 = 点炮 + 流泪", (codes(s), s.total), (["discard", "tears"], 2))

    s = settle(Context(**base, payers=[Payer(0, discard=True, tears=True)]))
    case("抢杠二番 4 分", s.points, 4)

    s = settle(Context(**base, payers=[Payer(0, discard=True, hand_tiles=1)]))
    case("输家手把一", (codes(s), s.total), (["discard", "hand_one"], 2))

    s = settle(Context(**base, payers=[Payer(1, discard=True)]))
    case("点炮只有那家付", s.payers, {1: 2})

    four = [0] * KINDS
    four[0] = four[1] = 4
    s = settle(Context(**base, zimo=True, all_counts=tuple(four), payers=others))
    case("四归一两门牌叠两番", (codes(s), s.total), (["zimo", "si_gui", "si_gui"], 3))

    red = [0] * KINDS
    red[RED] = 3
    s = settle(Context(**base, zimo=True, all_counts=tuple(red), payers=others))
    case("三红中", (codes(s), s.total), (["zimo", "san_hong"], 2))

    red[RED] = 4
    s = settle(Context(**base, zimo=True, all_counts=tuple(red), payers=others))
    case("四红中 = 三红中 + 四归一", (codes(s), s.total), (["zimo", "si_gui", "san_hong"], 3))

    # 叉 5 条 + 吃 5 条：副露里凑齐四张 5 条（s5 = 22）
    s5 = 22
    counts = [0] * KINDS
    counts[s5] = 0
    melds = (Meld("triplet", (s5, s5, s5)), Meld("seq", (21, s5, 23)))
    allc = list(counts)
    for m in melds:
        for t in m.tiles:
            allc[t] += 1
    s = settle(Context(**base, zimo=True, all_counts=tuple(allc), counts=tuple(counts),
                      melds=melds, payers=others))
    case("叉 + 吃凑齐四张也算四归一", "si_gui" in codes(s), True)

    s = settle(Context(**base, zimo=True, kan_draw=True, all_counts=tuple(four), payers=others))
    case("前开花 = 自摸 + 四归一 + 前开花", (codes(s), s.total),
         (["zimo", "si_gui", "si_gui", "front_bloom"], 4))

    s = settle(Context(**base, zimo=True, bloom=True, all_counts=tuple(four), payers=others))
    case("后开花 = 自摸 + 四归一 + 后开花", "back_bloom" in codes(s), True)

    piao = [0] * KINDS                     # 1万×3 2饼×3 3条×3 4条×2 5条×2
    for i, n in ((0, 3), (10, 3), (20, 3), (21, 2), (22, 2)):
        piao[i] = n
    s = settle(Context(**base, counts=tuple(piao), payers=[Payer(1, discard=True)]))
    case("飘胡", "piao" in codes(s), True)

    case("封顶 8 番", points_for(15), points_for(8))
    print(f"\n共 {fails} 条失败")
    return fails


if __name__ == "__main__":
    raise SystemExit(1 if _selftest() else 0)
