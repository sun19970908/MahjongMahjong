"""结算与加番 —— 规则里那串番种只在这一个文件里定义。

规则原文（规则.txt「结算规则」）：
    庄家 点炮 闭门听（闭门听之后杠仍然是闭门听，闭门听之后杠仍然算闷）
    闷（未开门无附露） 自摸 杠（可叠加，输了不加番）
    手把一 飘胡（输了不加番）前开花（杠后马上自摸）后开花
    流泪（被抢杠或者杠后马上点炮）

设计口径：
    * 每个番种 = 1 番，杠按张数叠（一杠一番）；
    * 「输了不加番」的写法统一理解为：这些番只加在赢家头上，输的那家不算，
      所以结算只需要算赢家这一手；
    * 分数 = 底分 × 2^总番（封顶 8 番，可在 RuleConfig 里改），
      自摸每家付、点炮由点炮那家一家付。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from referee import Meld, is_piao_hu

BASE_POINTS = 1
FAN_CAP = 8            # 封顶 8 番（256 分），防止杠多的局炸掉


@dataclass(frozen=True)
class Fan:
    code: str
    label: str
    note: str = ""
    per: int = 1       # 一份几番


# 番种表：加番顺序 = 展示顺序
FANS: tuple[Fan, ...] = (
    Fan("zimo", "自摸", "自己摸上来的"),
    Fan("discard", "点炮", "别人打出来喂的"),
    Fan("dealer", "庄家", "庄家胡牌翻一番"),
    Fan("closed_ting", "闭门听", "报听时手里一张副露都没有（之后杠了照样算）"),
    Fan("men", "闷", "整局没开过门、没亮过副露（之后杠了照样算）"),
    Fan("kan", "杠", "赢家每杠加一番，可叠加；输的那家不加", per=1),
    Fan("hand_one", "手把一", "四组面子全亮出去，手里只剩一张等将"),
    Fan("piao", "飘胡", "整手只有刻子和对子，没有顺子"),
    Fan("front_bloom", "前开花", "杠后补张那一下马上自摸"),
    Fan("back_bloom", "后开花", "胡的是牌墙最后一张"),
    Fan("tears", "流泪", "被抢杠，或者杠完马上点炮"),
)

FAN_BY_CODE = {f.code: f for f in FANS}


@dataclass
class Context:
    """结算要用到的一切，都由牌局在收局那一刻填好交进来。"""

    winner: int                       # 引擎下标
    dealer: int                       # 引擎下标
    seats: int = 4
    zimo: bool = False
    from_seat: int | None = None      # 点炮/被抢杠的那家
    rob: bool = False                 # 抢杠胡
    bloom: bool = False               # 后开花
    kan_draw: bool = False            # 胡的那张是杠后补的张（前开花）
    payer_just_kanned: bool = False   # 点炮那家刚杠完就点炮（流泪）
    kans: int = 0                     # 赢家杠了几次
    melds: tuple[Meld, ...] = ()      # 赢家副露
    counts: tuple[int, ...] = ()      # 赢家手牌（含胡的那张）
    closed_ting: bool = False         # 报听那一刻是闭门
    melds_at_ting: int = 0            # 报听那一刻已有几组副露
    hand_tiles: int = 0               # 收局时手里还有几张


@dataclass
class Settlement:
    lines: list[Fan] = field(default_factory=list)
    total: int = 0
    points: int = 0
    payers: dict[int, int] = field(default_factory=dict)     # 引擎下标 → 该付多少
    notes: list[str] = field(default_factory=list)

    def text(self) -> str:
        fans = "、".join(f.label for f in self.lines) or "无番"
        return f"{fans} = {self.total} 番，{self.points} 分"

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


def settle(ctx: Context, *, base: int = BASE_POINTS, cap: int = FAN_CAP) -> Settlement:
    """按规则原文一条条判，谁满足就给谁加番。"""
    s = Settlement()

    def add(code: str) -> None:
        s.lines.append(FAN_BY_CODE[code])

    if ctx.zimo:
        add("zimo")
    elif ctx.from_seat is not None:
        add("discard")

    if ctx.winner == ctx.dealer:
        add("dealer")

    # 闭门听 / 闷：看的是"报听那一刻"和"整局有没有亮过副露"，
    # 报听之后再杠不影响这两条（规则原文特别点明过）。
    # 所以「闷」就等于「报听时是闭门」——两个番种是叠加的，不是二选一。
    if ctx.closed_ting:
        add("closed_ting")
        add("men")

    for _ in range(max(ctx.kans, 0)):
        add("kan")

    if ctx.hand_tiles == 1:
        add("hand_one")

    if ctx.counts and is_piao_hu(ctx.counts, ctx.melds):
        add("piao")

    if ctx.kan_draw and ctx.zimo:
        add("front_bloom")
    if ctx.bloom:
        add("back_bloom")

    if ctx.rob or ctx.payer_just_kanned:
        add("tears")

    s.total = sum(f.per for f in s.lines)
    s.points = points_for(s.total, base, cap)

    if ctx.zimo:
        s.payers = {i: s.points for i in range(ctx.seats) if i != ctx.winner}
    elif ctx.from_seat is not None:
        s.payers = {ctx.from_seat: s.points}
    for line in s.lines:
        if line.code in ("kan", "piao"):
            s.notes.append(f"{line.label}：输了不加番，只算在赢家头上")
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

    base = dict(winner=2, dealer=0, seats=4)

    s = settle(Context(**base, zimo=True, kans=1))
    case("自摸 + 杠", [f.code for f in s.lines], ["zimo", "kan"])
    case("两番 4 分", (s.total, s.points), (2, 4))
    case("自摸三家付", sorted(s.payers), [0, 1, 3])

    s = settle(Context(winner=2, dealer=0, from_seat=0))
    case("点炮（赢家不是庄，不加庄家番）", [f.code for f in s.lines], ["discard"])
    case("点炮只由那家付", s.payers, {0: 2})

    s = settle(Context(winner=0, dealer=0, from_seat=1))
    case("庄家胡了自己翻番", [f.code for f in s.lines], ["discard", "dealer"])

    s = settle(Context(**base, from_seat=1, closed_ting=True, kans=2))
    case("闭门听同时算闷，杠照样加", [f.code for f in s.lines],
         ["discard", "closed_ting", "men", "kan", "kan"])
    case("闭门听五番 32 分", (s.total, s.points), (5, 32))

    s = settle(Context(**base, zimo=True, kan_draw=True))
    case("前开花", [f.code for f in s.lines], ["zimo", "front_bloom"])

    s = settle(Context(**base, bloom=True, zimo=True))
    case("后开花", [f.code for f in s.lines], ["zimo", "back_bloom"])

    s = settle(Context(**base, from_seat=1, rob=True))
    case("抢杠算流泪", [f.code for f in s.lines], ["discard", "tears"])

    s = settle(Context(**base, from_seat=1, payer_just_kanned=True))
    case("杠后马上点炮也算流泪", [f.code for f in s.lines], ["discard", "tears"])

    s = settle(Context(**base, from_seat=1, hand_tiles=1))
    case("手把一", "hand_one" in [f.code for f in s.lines], True)

    piao = [0] * 28                     # 1万×3 2饼×3 3条×3 4条×2 5条×2
    for i, n in ((0, 3), (10, 3), (20, 3), (21, 2), (22, 2)):
        piao[i] = n
    s = settle(Context(**base, from_seat=1, counts=tuple(piao)))
    case("飘胡（三刻两对）", "piao" in [f.code for f in s.lines], True)

    case("封顶 8 番", points_for(15), points_for(8))
    print(f"\n共 {fails} 条失败")
    return fails


if __name__ == "__main__":
    raise SystemExit(1 if _selftest() else 0)
