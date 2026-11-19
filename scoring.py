"""结算与加番 —— 规则里那串番种只在这一个文件里定义。

规则原文（规则.txt「结算加番规则（全都能叠加）」）：
    赢了加一番：庄家 闭门听（之后杠仍然是闭门听） 自摸 四归一（有四张相同牌，可叠加）
                三红中 手把一 飘胡 前开花（杠后马上自摸）后开花
                三清（三家闷）海底捞月（摸到最后四张牌之一胡牌）
    输了加一番：庄家 点炮（被抢杠算点炮） 闷（未开门无附露，闭门听之后杠仍然是闷）
                手把一 流泪（被抢杠或者杠后马上点炮）

设计口径（赢家番三家共担，输家番自己付自己的）：
    * 每个番种 1 番，全部叠加；四归一看「整手这个牌种一共几张」，
      杠、叉、吃都算进来，几种牌各凑四张就叠几番；
    * 三红中 1 番；红中凑齐四张时再叠一个四归一，共 2 番；
    * 三清本身 1 番，三家全闷时统一再记 1 番闷，共 2 番；
    * 没赢至少输一番：三家都付，每家先记「保底」1 番，再叠自己的
      庄家/点炮/闷/手把一/流泪；
    * 分数 = 底分 × 2^番（封顶 8 番）：自摸各家按自己的番付，点炮那家再多一份点炮番。
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
    Fan("piao", "飘胡", "整手只有刻子和对子，附露也不能有顺子"),
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
    Fan("floor", "保底", "有人胡，三家都至少按 1 番付"),
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
    lines: list[Fan] = field(default_factory=list)           # 赢家番（三家共同承担）
    total: int = 0
    points: int = 0                                          # 赢家总共收多少
    payers: dict[int, int] = field(default_factory=dict)     # 引擎下标 → 该付多少
    payer_lines: dict[int, list[Fan]] = field(default_factory=dict)  # 各家自己的输家番

    def text(self) -> str:
        """「自摸、四归一 = 2 番；2号（庄家）8 分，1号 4 分…」，重复的番合并成 ×N。"""
        parts: list[str] = []
        counts: dict[str, int] = {}
        for f in self.lines:
            if f.label not in counts:
                parts.append(f.label)
                counts[f.label] = 0
            counts[f.label] += f.per
        detail = "、".join(f"{p}×{counts[p]}" if counts[p] > 1 else p for p in parts) or "无番"
        tail = []
        for seat, pts in self.payers.items():
            own = self.payer_lines.get(seat) or []
            tag = f"（{'、'.join(f.label for f in own)}）" if own else ""
            tail.append(f"{seat + 1}号{tag} {pts} 分")
        return f"{detail} = {self.total} 番；" + "，".join(tail) if tail else f"{detail} = {self.total} 番"

    def to_dict(self) -> dict:
        return {
            "fans": [{"code": f.code, "label": f.label, "fan": f.per, "note": f.note} for f in self.lines],
            "total": self.total,
            "points": self.points,
            "payers": {str(k): v for k, v in self.payers.items()},
            "payer_fans": {str(k): [{"code": f.code, "label": f.label, "fan": f.per}
                                    for f in v] for k, v in self.payer_lines.items()},
            "text": self.text(),
        }


def points_for(fan: int, base: int = BASE_POINTS, cap: int = FAN_CAP) -> int:
    """番 → 分：底分 × 2^番，封顶。"""
    return base * (2 ** min(max(fan, 0), cap))


def _win_lines(ctx: Context) -> list[Fan]:
    """赢家番：三家共同承担；三清时连那 1 番闷也记在这里。"""
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
        add("men")                          # 三清 = 三清 1 + 闷 1，只记一次
    if ctx.hai and ctx.zimo:
        add("haidi")
    return out


def _own_lines(p: Payer, sanqing: bool) -> list[Fan]:
    """某个付款人自己的输家番：只落在他这一笔上，不摊给别家。"""
    out: list[Fan] = [FAN_BY_CODE["floor"]]   # 有人胡，三家都至少输这一番

    def add(code: str) -> None:
        out.append(FAN_BY_CODE[code])

    if p.dealer:
        add("dealer")
    if p.discard:
        add("discard")
    if p.men and not sanqing:               # 三清已经统一给过闷，不再按人重复
        add("men")
    if p.hand_tiles == 1:
        add("hand_one")
    if p.tears:
        add("tears")
    return out


def settle(ctx: Context, *, base: int = BASE_POINTS, cap: int = FAN_CAP) -> Settlement:
    """赢家番三家共担，各家再叠自己的输家番；每家至少 1 番。"""
    s = Settlement()
    s.lines = _win_lines(ctx)
    s.total = sum(f.per for f in s.lines)
    for p in ctx.payers:
        own = _own_lines(p, ctx.sanqing)
        s.payer_lines[p.seat] = own
        pts = points_for(s.total + sum(f.per for f in own), base, cap)
        s.payers[p.seat] = pts
        s.points += pts
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

    def own(s: Settlement, seat: int) -> list[str]:
        return [f.code for f in s.payer_lines.get(seat, ())]

    base = dict(winner=2, dealer=0, seats=4)
    others = [Payer(i) for i in (0, 1, 3)]

    s = settle(Context(**base, zimo=True, payers=others))
    case("自摸", (codes(s), s.total), (["zimo"], 1))
    case("自摸三家都付保底", (s.payers, s.points), ({0: 4, 1: 4, 3: 4}, 12))

    s = settle(Context(**base, zimo=True, closed_ting=True, payers=others))
    case("闭门自摸 = 闭门听 + 自摸", (codes(s), s.total, s.payers), (["zimo", "closed_ting"], 2, {0: 8, 1: 8, 3: 8}))

    s = settle(Context(**base, zimo=True, closed_ting=True, sanqing=True,
                       payers=[Payer(i, men=True) for i in (0, 1, 3)]))
    case("自摸 + 三清 = 自摸 + 闷 + 三清", (codes(s), s.total), (["zimo", "closed_ting", "sanqing", "men"], 4))
    case("三清时闷不再按人重复", [own(s, 0), own(s, 1), own(s, 3)], [["floor"], ["floor"], ["floor"]])

    s = settle(Context(**base, zimo=True, sanqing=True,
                       payers=[Payer(i, men=True) for i in (0, 1, 3)]))
    case("三家都闷，闷只加一次", (codes(s), s.total, s.payers), (["zimo", "sanqing", "men"], 3, {0: 16, 1: 16, 3: 16}))

    s = settle(Context(**base, closed_ting=True,
                       payers=[Payer(0, discard=True, men=True), Payer(1), Payer(3)]))
    case("闷点炮给闭门听 = 闭门听 + 点炮 + 闷",
         (codes(s), own(s, 0), 4, s.payers[0]), (["closed_ting"], ["floor", "discard", "men"], 4, 16))

    s = settle(Context(**base, payers=[Payer(0, dealer=True, discard=True), Payer(1), Payer(3)]))
    case("庄家点炮：庄家番只算他这一笔", (codes(s), own(s, 0), s.payers),
         ([], ["floor", "dealer", "discard"], {0: 8, 1: 2, 3: 2}))

    s = settle(Context(**base, payers=[Payer(0, discard=True, tears=True), Payer(1), Payer(3)]))
    case("抢杠 = 点炮 + 流泪", (own(s, 0), s.payers[0]), (["floor", "discard", "tears"], 8))

    s = settle(Context(**base, payers=[Payer(0, discard=True, hand_tiles=1), Payer(1), Payer(3)]))
    case("输家手把一", own(s, 0), ["floor", "discard", "hand_one"])

    s = settle(Context(**base, payers=[Payer(0, discard=True), Payer(1), Payer(3)]))
    case("点炮那家多付，另外两家保底", s.payers, {0: 4, 1: 2, 3: 2})

    s = settle(Context(**base, zimo=True,
                       payers=[Payer(0, dealer=True), Payer(1), Payer(3)]))
    case("庄家是付款人时只他多付", (s.total, s.payers), (1, {0: 8, 1: 4, 3: 4}))

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
    case("飘胡自动算番", "piao" in codes(s), True)

    case("封顶 8 番", points_for(15), points_for(8))
    print(f"\n共 {fails} 条失败")
    return fails


if __name__ == "__main__":
    raise SystemExit(1 if _selftest() else 0)
