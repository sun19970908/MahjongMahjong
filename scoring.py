"""结算与加番 —— 规则里那串番种只在这一个文件里定义。

规则原文（规则.txt「结算加番规则」）：
    赢了加一番：庄家 闭门听（之后杠仍然是闭门听） 自摸 四归一（有四张相同牌，可叠加）
                三红中 手把一 飘胡 前开花（杠后马上自摸）后开花
                三清（三家闷）海底捞月（摸到最后四张牌之一胡牌）
    输了加一番：庄家 点炮 闷（未开门无附露，闭门听之后杠仍然是闷）手把一
                流泪（被抢杠或者杠后马上点炮）

设计口径：
    * 赢家自己一手番 + 每个付款人自己的输家番，分开算；
      付款人应付 = 底分 × 2^(赢家番 + 他自己的输家番)，各自封顶；
    * 自摸三家各按自己的番付，点炮只有点炮那家付；
    * 「四归一」按牌种叠：两种牌各有四张就是两番（杠出来的四张同样算）；
    * 「三红中」＝手里/副露里凑够三张红中。
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
    Fan("dealer", "庄家", "庄家胡牌翻一番"),
    Fan("closed_ting", "闭门听", "报听时手里一张副露都没有（之后杠了照样算）"),
    Fan("si_gui", "四归一", "有一种牌凑齐四张，可叠加"),
    Fan("san_hong", "三红中", "凑够三张红中"),
    Fan("hand_one", "手把一", "四组面子全亮出去，手里只剩一张等将"),
    Fan("piao", "飘胡", "整手只有刻子和对子，没有顺子"),
    Fan("front_bloom", "前开花", "杠后补张那一下马上自摸"),
    Fan("back_bloom", "后开花", "胡的是牌墙最后一张"),
    Fan("sanqing", "三清", "另外三家全闷：谁都没开过门、没亮过副露"),
    Fan("haidi", "海底捞月", "摸到牌墙最后四张之一胡牌"),
)

# 输家番：跟着付款人走
LOSE_FANS: tuple[Fan, ...] = (
    Fan("dealer", "庄家", "庄家点炮/被自摸，多出一番"),
    Fan("discard", "点炮", "打出这张牌喂了胡"),
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
    discard: bool = False      # 点炮（抢杠不算点炮，算流泪）
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
    lines: list[Fan] = field(default_factory=list)           # 赢家番
    total: int = 0                                           # 赢家番数
    points: int = 0                                          # 赢家总共收多少
    payers: dict[int, int] = field(default_factory=dict)     # 引擎下标 → 该付多少
    payer_lines: dict[int, list[Fan]] = field(default_factory=dict)  # 付款人自己的番

    def payer_total(self, seat: int) -> int:
        return self.total + sum(f.per for f in self.payer_lines.get(seat, ()))

    def text(self) -> str:
        fans = "、".join(f.label for f in self.lines) or "无番"
        head = f"{fans} = {self.total} 番"
        tail = []
        for seat, pts in self.payers.items():
            own = self.payer_lines.get(seat) or []
            tag = f"（{'、'.join(f.label for f in own)}）" if own else ""
            tail.append(f"{seat + 1}号{tag} {pts} 分")
        return f"{head}；" + "，".join(tail) if tail else head

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
                add("si_gui")
        if ctx.all_counts[RED] >= 3:
            add("san_hong")

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


def _lose_lines(p: Payer) -> list[Fan]:
    out: list[Fan] = []

    def add(code: str) -> None:
        out.append(FAN_BY_CODE[code])

    if p.dealer:
        add("dealer")
    if p.discard:
        add("discard")
    if p.men:
        add("men")
    if p.hand_tiles == 1:
        add("hand_one")
    if p.tears:
        add("tears")
    return out


def settle(ctx: Context, *, base: int = BASE_POINTS, cap: int = FAN_CAP) -> Settlement:
    """按规则原文一条条判：赢家一手番，付款人各自再加自己的输家番。"""
    s = Settlement()
    s.lines = _win_lines(ctx)
    s.total = sum(f.per for f in s.lines)

    for p in ctx.payers:
        own = _lose_lines(p)
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

    base = dict(winner=2, dealer=0, seats=4)
    others = [Payer(i) for i in (0, 1, 3)]

    s = settle(Context(**base, zimo=True, payers=others))
    case("自摸", codes(s), ["zimo"])
    case("自摸三家付", sorted(s.payers), [0, 1, 3])
    case("自摸每家 2 分", s.payers, {0: 2, 1: 2, 3: 2})

    s = settle(Context(**base, payers=[Payer(0, discard=True)]))
    case("点炮算在付款人头上", (codes(s), [f.code for f in s.payer_lines[0]]), ([], ["discard"]))
    case("点炮那家付 2 分、只有他付", (s.payers, s.points), ({0: 2}, 2))

    s = settle(Context(winner=0, dealer=0, payers=[Payer(1, discard=True)]))
    case("庄家胡了自己翻番（付款人不是庄）", codes(s), ["dealer"])

    s = settle(Context(winner=2, dealer=0, payers=[Payer(0, dealer=True, discard=True, men=True, tears=True)]))
    case("庄家点炮：输家番全叠在付款人身上",
         [f.code for f in s.payer_lines[0]], ["dealer", "discard", "men", "tears"])
    case("付款人 4 番 16 分", (s.total, s.payers[0]), (0, 16))

    s = settle(Context(**base, zimo=True, closed_ting=True, payers=[Payer(0, men=True), Payer(1), Payer(3)]))
    case("闭门听只加赢家，闷只加付款人",
         (codes(s), [f.code for f in s.payer_lines[0]]), (["zimo", "closed_ting"], ["men"]))
    case("自摸各算各的", s.payers, {0: 8, 1: 4, 3: 4})

    s = settle(Context(**base, zimo=True, kan_draw=True, payers=others))
    case("前开花", codes(s), ["zimo", "front_bloom"])

    s = settle(Context(**base, zimo=True, bloom=True, payers=others))
    case("后开花", codes(s), ["zimo", "back_bloom"])

    s = settle(Context(winner=2, dealer=0, seats=4, payers=[Payer(1, discard=True, tears=True)]))
    case("抢杠/杠后点炮算流泪", [f.code for f in s.payer_lines[1]], ["discard", "tears"])

    four = [0] * KINDS
    four[0] = four[1] = 4
    s = settle(Context(**base, zimo=True, all_counts=tuple(four), payers=others))
    case("四归一两门牌叠两番", codes(s), ["zimo", "si_gui", "si_gui"])

    red = [0] * KINDS
    red[RED] = 3
    s = settle(Context(**base, zimo=True, all_counts=tuple(red), payers=others))
    case("三红中", codes(s), ["zimo", "san_hong"])

    piao = [0] * KINDS                     # 1万×3 2饼×3 3条×3 4条×2 5条×2
    for i, n in ((0, 3), (10, 3), (20, 3), (21, 2), (22, 2)):
        piao[i] = n
    s = settle(Context(**base, counts=tuple(piao), payers=[Payer(1, discard=True)]))
    case("飘胡", "piao" in codes(s), True)

    s = settle(Context(**base, zimo=True, sanqing=True, hai=True, payers=others))
    case("三清 + 海底捞月都是赢家番", codes(s), ["zimo", "sanqing", "haidi"])

    s = settle(Context(winner=2, dealer=0, payers=[Payer(1, hand_tiles=1, discard=True)]))
    case("输家手把一也加番", [f.code for f in s.payer_lines[1]], ["discard", "hand_one"])

    case("封顶 8 番", points_for(15), points_for(8))
    print(f"\n共 {fails} 条失败")
    return fails


if __name__ == "__main__":
    raise SystemExit(1 if _selftest() else 0)
