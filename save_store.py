"""牌局存档：把一场牌（同一个 run_id 的若干局）冻结成一个可接回的档。

和 `state.json` 的分工：

* `state.json` 是 live 自动存档，永远只代表“现在这一手/这一局”；
* `saves/<id>/state.json` 是用户主动存下的一档，之后不会被下一局覆盖；
* `saves/<id>/manifest.json` 是给列表和复盘看的小摘要，不读四家暗手。

一个档只复制一份完整牌局；不做每步全量回放，避免一档几百 MB。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SAVE_ROOT = ROOT / "saves"
VERSION = 1


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _seat_no(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value) + 1
    except (TypeError, ValueError):
        return None


def _read_state(state_path: Path) -> dict:
    return json.loads(Path(state_path).read_text(encoding="utf-8"))


def _result_summary(raw: dict | None) -> dict | None:
    """把内核的 0 基 result 压成 manifest 里的 1 基摘要。"""
    if not raw:
        return None
    settle = raw.get("settle") or {}
    return {
        "type": raw.get("type"),
        "seat": _seat_no(raw.get("seat")),
        "from": _seat_no(raw.get("from")),
        "tile": raw.get("tile"),
        "zimo": bool(raw.get("zimo")),
        "rob": bool(raw.get("rob")),
        "bloom": bool(raw.get("bloom")),
        "kan_draw": bool(raw.get("kan_draw")),
        "hai": bool(raw.get("hai")),
        "hand": list(raw.get("hand") or []),
        "melds": list(raw.get("melds") or []),
        "reason": raw.get("reason"),
        "last_draw": raw.get("last_draw"),
        "settle_text": str(settle.get("text") or ""),
        "fans": list(settle.get("fans") or []),
    }


def _normalise_result(raw: dict | None) -> dict | None:
    """补一个扁平 settle_text，旧账本/新账本都能给前端直接用。"""
    if not raw:
        return None
    out = dict(raw)
    if "settle_text" not in out:
        out["settle_text"] = str((out.get("settle") or {}).get("text") or "")
    return out


def _board(meta: dict) -> dict[str, int]:
    raw = meta.get("board") or {}
    out = {}
    for k, v in raw.items():
        try:
            out[str(int(k) + 1)] = int(v)
        except (TypeError, ValueError):
            continue
    return out


def _players(meta: dict) -> list[dict]:
    names = meta.get("names") or {}
    out = []
    for plan in meta.get("plans") or []:
        try:
            seat = int(plan.get("seat") or 0)
        except (TypeError, ValueError):
            continue
        if not 1 <= seat <= 4:
            continue
        kind = str(plan.get("kind") or "")
        options = plan.get("options") or {}
        profile = str(options.get("profile") or "").strip()
        name = str(names.get(str(seat - 1)) or names.get(str(seat)) or "")
        if not name:
            if kind == "human":
                name = f"{seat}号（你）"
            elif kind == "external":
                name = f"{profile}（{seat}号）" if profile else f"{seat}号（接入）"
            elif profile:
                name = f"{profile}（{seat}号）"
            else:
                name = f"{seat}号"
        out.append({"seat": seat, "name": name, "kind": kind,
                    "profile": profile})
    return out


def _round_record(state: dict) -> dict | None:
    """当前局已经结算、但旧账本没记时，补一条本局记录。"""
    game = state.get("game") or {}
    meta = state.get("meta") or {}
    raw = game.get("result")
    if not raw:
        return None
    try:
        rnd = int(meta.get("round") or 0)
    except (TypeError, ValueError):
        return None
    if rnd <= 0:
        return None
    settle = raw.get("settle") or {}
    return {
        "round": rnd,
        "lap": int(meta.get("lap") or 1),
        "dealer": int(game.get("dealer") or 0) + 1,
        "result": _result_summary(raw),
        "board_after": _board(meta),
        "steps": int(meta.get("steps") or 0),
        "started_at": str(meta.get("round_started_at") or ""),
        "ended_at": _now(),
        "log": list(meta.get("log") or [])[-80:],
        "settle_text": str(settle.get("text") or ""),
    }


def _rounds(state: dict) -> list[dict]:
    meta = state.get("meta") or {}
    records = []
    for r in (meta.get("round_history") or []):
        if not isinstance(r, dict) or not r.get("round"):
            continue
        r = dict(r)
        r["result"] = _normalise_result(r.get("result"))
        records.append(r)
    seen = {int(r["round"]) for r in records}
    current = _round_record(state)
    if current and int(current["round"]) not in seen:
        records.append(current)
    return sorted(records, key=lambda r: int(r.get("round") or 0))


def build_manifest(state_path: Path | str, name: str | None = None) -> dict:
    """只读 state.json，生成一档的摘要；不会读取或暴露四家暗手。"""
    state = _read_state(Path(state_path))
    game = state.get("game") or {}
    meta = state.get("meta") or {}
    rounds = _rounds(state)
    try:
        current_round = int(meta.get("round") or 1)
    except (TypeError, ValueError):
        current_round = 1
    ended = bool(game.get("result") or game.get("phase") == "ended")
    expected = current_round if ended else max(0, current_round - 1)
    missing = [r for r in range(1, expected + 1)
               if r not in {int(x.get("round") or 0) for x in rounds}]
    last = (rounds[-1].get("result") if rounds
            else _result_summary(game.get("result")))
    last = _normalise_result(last)
    run_id = str(meta.get("snapshot_run") or "")
    default_name = f"第{current_round}局"
    if last:
        if last.get("type") == "draw_game":
            default_name += " · 荒庄"
        elif last.get("seat"):
            default_name += f" · {last['seat']}号胡"
    return {
        "version": VERSION,
        "id": "",
        "name": str(name or "").strip() or default_name,
        "created_at": _now(),
        "updated_at": _now(),
        "run_id": run_id,
        "round": current_round,
        "lap": int(meta.get("lap") or 1),
        "rounds_played": len(rounds),
        "rounds_expected": expected,
        "history_partial": bool(missing),
        "missing_rounds": missing,
        "board": _board(meta),
        "players": _players(meta),
        "status": "ended" if ended else "playing",
        "last_result": last,
        "rounds": rounds,
        "state_file": "state.json",
        "note": "",
        "app": "mahjong",
    }


def _slug(text: str) -> str:
    text = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", str(text or "").strip())
    return text.strip("-")[:40] or "save"


def _unique_dir(root: Path, name: str) -> tuple[str, Path]:
    base = f"{time.strftime('%Y%m%d-%H%M%S')}-{_slug(name)}"
    for i in range(1, 1000):
        sid = base if i == 1 else f"{base}-{i}"
        path = root / sid
        if not path.exists():
            return sid, path
    raise RuntimeError("存档目录重名太多")


def _root(save_root: Path | str | None = None) -> Path:
    return Path(save_root) if save_root is not None else SAVE_ROOT


def create(state_path: Path | str = ROOT / "state.json", *,
           name: str | None = None, save_root: Path | str | None = None) -> dict:
    """把当前 state.json 冻结成一档；manifest 最后写，半截档不会被列出。"""
    root = _root(save_root)
    root.mkdir(parents=True, exist_ok=True)
    state_src = Path(state_path)
    if not state_src.exists():
        raise FileNotFoundError(f"没有可存的牌局：{state_src}")
    manifest = build_manifest(state_src, name=name)
    sid, final_dir = _unique_dir(root, manifest["name"])
    tmp_dir = root / f".tmp-{sid}-{uuid.uuid4().hex[:8]}"
    tmp_dir.mkdir(parents=True, exist_ok=False)
    try:
        shutil.copyfile(state_src, tmp_dir / "state.json")
        manifest["id"] = sid
        (tmp_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        os.replace(tmp_dir, final_dir)
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    return manifest


def list_saves(save_root: Path | str | None = None) -> list[dict]:
    root = _root(save_root)
    if not root.exists():
        return []
    out = []
    for manifest_path in root.glob("*/manifest.json"):
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict) or not data.get("id"):
            continue
        data["path"] = str(manifest_path.parent)
        out.append(data)
    out.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
    return out


def get(save_id: str, save_root: Path | str | None = None) -> dict | None:
    for item in list_saves(save_root):
        if save_id in (item.get("id"), Path(item.get("path", "")).name):
            return item
    return None


def state_path(save_id: str, save_root: Path | str | None = None) -> Path | None:
    item = get(save_id, save_root)
    if not item:
        return None
    p = Path(item["path"]) / str(item.get("state_file") or "state.json")
    return p if p.exists() else None


def restore_state(save_id: str, target_state: Path | str = ROOT / "state.json", *,
                  save_root: Path | str | None = None,
                  backup: bool = True) -> Path:
    """把档里的 state.json 原子换到 live；调用者负责先停掉内核。"""
    src = state_path(save_id, save_root)
    if src is None:
        raise FileNotFoundError(f"找不到存档：{save_id}")
    dst = Path(target_state)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if backup and dst.exists():
        backup_path = dst.with_name(
            f"state.before-load-{time.strftime('%Y%m%d-%H%M%S')}.json")
        shutil.copyfile(dst, backup_path)
    tmp = dst.with_suffix(".tmp")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)
    return dst


def delete(save_id: str, save_root: Path | str | None = None) -> bool:
    item = get(save_id, save_root)
    if not item:
        return False
    shutil.rmtree(Path(item["path"]), ignore_errors=False)
    return True


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="牌局存档：列出/保存/接档")
    ap.add_argument("action", choices=["list", "save", "load", "delete"])
    ap.add_argument("value", nargs="?", default="", help="档 id 或档名")
    ap.add_argument("--name", default="", help="存档时用的名字")
    ap.add_argument("--state", default=str(ROOT / "state.json"))
    ap.add_argument("--root", default=str(SAVE_ROOT))
    args = ap.parse_args()
    if args.action == "list":
        for item in list_saves(args.root):
            print(f"{item['id']}  {item.get('name')}  "
                  f"第{item.get('round')}局  "
                  f"明细{item.get('rounds_played')}/{item.get('rounds_expected')}")
        return 0
    if args.action == "save":
        item = create(args.state, name=args.name or args.value, save_root=args.root)
        print(f"已存档：{item['id']}  {item['name']}")
        return 0
    if args.action == "load":
        if not args.value:
            print("要指定档 id")
            return 2
        restore_state(args.value, args.state, save_root=args.root)
        print(f"已接档：{args.value}；重启牌桌后生效")
        return 0
    if args.action == "delete":
        if not args.value:
            print("要指定档 id")
            return 2
        print("已删除" if delete(args.value, args.root) else "找不到这个档")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
