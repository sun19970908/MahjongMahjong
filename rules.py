"""麻将规则集注册表：把具体地区规则与 core.TableCore 解耦。

`core.py` 只认 `RuleSet` 提供的通用能力（默认选项、开局、读/存盘、单局 `Game` 对象），
具体番型、和牌判定、牌张体系与序列化格式都由各个规则实现自己决定。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol


class RuleSet(Protocol):
    name: str
    label: str
    seats: int

    def default_options(self) -> dict[str, Any]:
        ...

    def normalize_options(self, options: dict[str, Any] | None = None) -> dict[str, Any]:
        ...

    def new_game(
        self,
        *,
        seats: int | None = None,
        seed: int | None = None,
        dealer: int | None = None,
        options: dict[str, Any] | None = None,
    ) -> Any:
        ...

    def game_options(self, game: Any) -> dict[str, Any]:
        ...

    def load_game(self, path: Path) -> tuple[Any, dict] | None:
        ...

    def save_game(self, path: Path, game: Any, meta: dict) -> None:
        ...

    def save_step(self, path: Path, game: Any, meta: dict) -> None:
        ...


class TielingRuleSet:
    """铁岭麻将规则集（默认规则，包住 game/referee/scoring/state）。"""

    name = "tieling"
    label = "铁岭麻将"
    seats = 4

    def default_options(self) -> dict[str, Any]:
        return {"base": 1, "fan_cap": 8}

    def normalize_options(self, options: dict[str, Any] | None = None) -> dict[str, Any]:
        raw = dict(self.default_options())
        if options:
            for k, v in options.items():
                if v is not None:
                    raw[k] = v
        raw["base"] = max(0, int(raw.get("base", 1)))
        raw["fan_cap"] = max(0, int(raw.get("fan_cap", 8)))
        return raw

    def new_game(
        self,
        *,
        seats: int | None = None,
        seed: int | None = None,
        dealer: int | None = None,
        options: dict[str, Any] | None = None,
    ) -> Any:
        from game import Game, GameConfig

        opts = self.normalize_options(options)
        cfg = GameConfig(
            seats=int(seats or self.seats),
            seed=seed,
            dealer=dealer,
            base_points=opts["base"],
            fan_cap=opts["fan_cap"],
        )
        return Game(cfg)

    def game_options(self, game: Any) -> dict[str, Any]:
        cfg = getattr(game, "cfg", None)
        if cfg is None:
            return self.default_options()
        return self.normalize_options({
            "base": getattr(cfg, "base_points", 1),
            "fan_cap": getattr(cfg, "fan_cap", 8),
        })

    def load_game(self, path: Path) -> tuple[Any, dict] | None:
        import state as state_mod
        return state_mod.load(path)

    def save_game(self, path: Path, game: Any, meta: dict) -> None:
        import state as state_mod
        state_mod.save(path, game, meta)

    def save_step(self, path: Path, game: Any, meta: dict) -> None:
        import state as state_mod
        state_mod.save_step(path, game, meta)


_REGISTRY: dict[str, Any] = {}


def register_ruleset(ruleset: RuleSet) -> None:
    key = str(getattr(ruleset, "name", "") or "").strip().lower()
    if not key:
        raise ValueError("RuleSet 缺少非空 name")
    _REGISTRY[key] = ruleset


def get_ruleset(name: str | None = None) -> RuleSet:
    key = str(name or "tieling").strip().lower() or "tieling"
    rs = _REGISTRY.get(key)
    if rs is None:
        raise ValueError(f"未注册的麻将规则：{name!r}（可选：{', '.join(sorted(_REGISTRY))}）")
    return rs


def list_rulesets() -> list[dict[str, Any]]:
    return [
        {"name": rs.name, "label": rs.label, "seats": rs.seats, "options": rs.default_options()}
        for rs in _REGISTRY.values()
    ]


def load_game(path: Path | str) -> tuple[Any, dict] | None:
    """按存档里写的 rule 找规则集读档，core 不需要知道当前桌用的是什么规则。"""
    p = Path(path)
    if not p.exists():
        return None
    meta: dict[str, Any] = {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        meta = dict(data.get("meta") or {})
    except (OSError, ValueError, TypeError):
        meta = {}
    rs = get_ruleset(str(meta.get("rule") or "tieling"))
    return rs.load_game(p)


register_ruleset(TielingRuleSet())
