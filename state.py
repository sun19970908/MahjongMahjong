"""牌局存档：把一局的可变状态写成 JSON，重启后原样接上。

只存"这局现在是什么样子"：牌墙、四家手牌、副露、河、轮到谁、待反应的人，
外加谁坐哪、第几步、牌友的对话历史（不存它就不记得这局，调试也没意义）。

注意：`hands` 存的是**计数数组**（长度 28 = 9+9+9+1 种牌各自的张数），
不是牌名序列。直接看会把它当成 28 张手牌——那是错的，手牌张数要 sum()。
"""

from __future__ import annotations

import json
import os
from collections import deque
from pathlib import Path

from game import Game, GameConfig
from referee import Action, Meld, parse_tile, tile_id
import seats

VERSION = 1


def _meld_out(m: Meld) -> dict:
    return {"kind": m.kind, "tiles": [tile_id(t) for t in m.tiles]}


def _meld_in(d: dict) -> Meld:
    return Meld(d["kind"], tuple(parse_tile(t) for t in d["tiles"]))


def _act_out(a: Action) -> dict:
    return a.to_dict()


def _act_in(d: dict) -> Action:
    tile = d.get("tile")
    return Action(d["kind"],
                  tile=parse_tile(tile) if tile is not None else None,
                  combo=tuple(parse_tile(t) for t in d.get("combo") or ()),
                  meta=dict(d.get("meta") or {}))


def snapshot(game: Game, meta: dict | None = None) -> dict:
    return {
        "version": VERSION,
        "cfg": {"seats": game.cfg.seats, "start_hand": game.cfg.start_hand,
                "dealer": game.dealer, "seed": game.cfg.seed,
                "base_points": game.cfg.base_points, "fan_cap": game.cfg.fan_cap,
                "dice": None if game.dice is None else [game.dice.a, game.dice.b, game.dice.total]},
        "game": {
            "hands_format": "counts",          # 计数数组，不是牌名；手牌张数 = sum(counts)
            "wall": [tile_id(t) for t in game.wall],
            "hands": {str(s): list(v) for s, v in game.hands.items()},
            "fixed": {str(s): [_meld_out(m) for m in v] for s, v in game.fixed.items()},
            "discards": {str(s): [tile_id(t) for t in v] for s, v in game.discards.items()},
            "turn": game.turn,
            "phase": game.phase,
            "last": None if game.last is None else [game.last[0], game.last[1], tile_id(game.last[2])],
            "reactors": [[rank, dist, seat, [_act_out(a) for a in opts]]
                         for rank, dist, seat, opts in game.reactors],
            "rob": None if game.rob is None else [game.rob[0], tile_id(game.rob[1]), game.rob_kind],
            "result": game.result,
            "ting": {str(s): bool(v) for s, v in game.ting.items()},
            "ting_closed": {str(s): bool(v) for s, v in game.ting_closed.items()},
            "kans": {str(s): int(v) for s, v in game.kans.items()},
            "dead": {str(s): bool(v) for s, v in game.dead_win.items()},
            "drawn": None if game.drawn is None else tile_id(game.drawn),
            "drawn_after_kan": bool(game.drawn_after_kan),
            "after_kan": game.after_kan,
            "last_draw": None if game.last_draw is None
            else {"seat": game.last_draw[0], "tile": tile_id(game.last_draw[1])},
        },
        "meta": meta or {},
    }


def restore(data: dict) -> tuple[Game, dict]:
    cfg = data.get("cfg") or {}
    game = Game(GameConfig(seats=cfg.get("seats", 4), start_hand=cfg.get("start_hand", 13),
                           dealer=cfg.get("dealer"), seed=cfg.get("seed"),
                           base_points=cfg.get("base_points", 1), fan_cap=cfg.get("fan_cap", 8)))
    dice = cfg.get("dice")
    if dice:
        game.dice = seats.Dice(int(dice[0]), int(dice[1]), int(dice[2]), seats.label(game.dealer))
    gs = data["game"]
    game.wall = deque(parse_tile(t) for t in gs.get("wall") or [])
    game.hands = {int(s): list(v) for s, v in (gs.get("hands") or {}).items()}
    game.fixed = {int(s): [_meld_in(m) for m in v] for s, v in (gs.get("fixed") or {}).items()}
    game.discards = {int(s): [parse_tile(t) for t in v] for s, v in (gs.get("discards") or {}).items()}
    game.turn = gs.get("turn", game.cfg.dealer)
    game.phase = gs.get("phase", "draw")
    last = gs.get("last")
    game.last = None if last is None else (int(last[0]), str(last[1]), parse_tile(last[2]))
    game.reactors = [(int(r), int(d), int(s), [_act_in(a) for a in opts])
                     for r, d, s, opts in (gs.get("reactors") or [])]
    rob = gs.get("rob")
    game.rob = None if rob is None else (int(rob[0]), parse_tile(rob[1]))
    game.rob_kind = None if not rob else (str(rob[2]) if len(rob) > 2 else "added")
    game.result = gs.get("result")
    ting = gs.get("ting") or {}
    game.ting = {s: bool(ting.get(str(s), False)) for s in range(game.cfg.seats)}
    closed = gs.get("ting_closed") or {}
    game.ting_closed = {s: bool(closed.get(str(s), False)) for s in range(game.cfg.seats)}
    kans = gs.get("kans") or {}
    game.kans = {s: int(kans.get(str(s), 0)) for s in range(game.cfg.seats)}
    dead = gs.get("dead") or {}
    game.dead_win = {s: bool(dead.get(str(s), False)) for s in range(game.cfg.seats)}
    drawn = gs.get("drawn")
    game.drawn = None if drawn is None else parse_tile(drawn)
    game.drawn_after_kan = bool(gs.get("drawn_after_kan"))
    game.after_kan = gs.get("after_kan")
    last_draw = gs.get("last_draw")
    game.last_draw = None if not last_draw else (int(last_draw["seat"]), parse_tile(last_draw["tile"]))
    return game, (data.get("meta") or {})


def save(path: Path | str, game: Game, meta: dict | None = None) -> None:
    p = Path(path)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(snapshot(game, meta), ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def load(path: Path | str) -> tuple[Game, dict] | None:
    p = Path(path)
    if not p.exists():
        return None
    return restore(json.loads(p.read_text(encoding="utf-8")))
