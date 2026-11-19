"""座位、庄家、色子 —— 全桌只有这一处定义"几号是谁"。

为什么要单独一层：以前 0 号是你、1 号是牌友、2 号是接入位，全写死在命令行、
TUI、文件信箱里，换一张桌子就得改代码。现在：

    * 牌局引擎（game/referee/state）内部仍按 0..n-1 记账，快、不用改；
    * 人看得见的地方（命令行、TUI、trace、seats/seatN、state 的 meta）一律从 **1 号** 起；
    * 换算只走本模块的 `label()` / `index()`，别在别处 ±1。

庄家与色子也归这里：开局掷两颗骰子定庄，胡了连庄、没胡下家坐庄。
"""

from __future__ import annotations

import random
from dataclasses import dataclass

FIRST_SEAT = 1          # 人看见的第一个座位号
DICE_SIDES = 6          # 两颗六面骰
DICE_COUNT = 2


def label(index: int) -> int:
    """引擎下标 → 座位号（0 → 1 号）。"""
    return index + FIRST_SEAT


def index(seat: int, seats: int = 4) -> int:
    """座位号 → 引擎下标（1 号 → 0）。"""
    if not FIRST_SEAT <= seat < FIRST_SEAT + seats:
        raise ValueError(f"没这个座位：{seat}（1…{FIRST_SEAT + seats - 1}）")
    return seat - FIRST_SEAT


def seat_dir(seat: int) -> str:
    """文件信箱的目录名，按座位号。"""
    return f"seat{seat}"


@dataclass(frozen=True)
class Dice:
    """一次掷骰的结果：两颗的点数、合计、以及它定出来的庄家（座位号）。"""

    a: int
    b: int
    total: int
    seat: int

    def text(self) -> str:
        return f"骰子 {self.a}+{self.b}={self.total} → {self.seat} 号坐庄"


def roll(rng: random.Random | None = None, seats: int = 4) -> Dice:
    """掷骰定庄：合计点数从 1 号往下数，数到谁谁坐庄。"""
    rng = rng or random.Random()
    a = rng.randint(1, DICE_SIDES)
    b = rng.randint(1, DICE_SIDES)
    total = a + b
    seat = FIRST_SEAT + (total - FIRST_SEAT) % seats
    return Dice(a=a, b=b, total=total, seat=seat)


def dealer_index(dice: Dice) -> int:
    """骰子定出来的庄家，换成引擎下标。"""
    return index(dice.seat)


def next_dealer(current: int, winner: int | None, seats: int) -> int:
    """这一局打完之后，下一局谁坐庄（引擎下标）。

    庄家自己胡了就连庄；荒庄或别人胡，庄家下移一位（下家坐庄）。
    """
    if winner is not None and winner == current:
        return current
    return (current + 1) % seats
