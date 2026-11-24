#!/usr/bin/env python
"""外部座位的进出接口。给任何进程（包括 agent / 外部脚本）用：

    python seat_io.py status  --seat 3          # 看座位当前状态、挂起动作、报错和最近事件
    python seat_io.py pending --seat 3          # 这一手该你了吗（没有就打印 none）
    python seat_io.py wait    --seat 3 --timeout 600 [--include-events] [--include-chat]
    python seat_io.py answer  --seat 3 --pick 3 --say "这张先走" [--wait]
    python seat_io.py answer  --seat 3 --kind discard --tile m3 [--wait]
    python seat_io.py answer  --seat 3 --kind pause --say "等一下，这手我有话要说"
    python seat_io.py reply   --seat 3 "刚才顺子没凑齐"
    python seat_io.py events  --seat 3 -n 10    # 看局始、收局结算播报
    python seat_io.py give-up --seat 3          # 这手不想了，交给随机
    python seat_io.py trace   -n 20             # 复盘：谁做了什么、多久、读了多少 token

只用文件，不占端口，不联网。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from players import (
    DEFAULT_SEAT_ROOT,
    action_label,
    read_json_safe,
    resolve_external_answer,
    tile_label,
    unlink_safe,
    write_atomic_json,
)
from referee import parse_tile, tile_id
import seats as seats_mod


def seat_dir(seat: int) -> Path:
    d = DEFAULT_SEAT_ROOT / seats_mod.seat_dir(seat)
    d.mkdir(parents=True, exist_ok=True)
    return d


def pending(seat: int) -> dict | None:
    data, _ = read_json_safe(seat_dir(seat) / "pending.json")
    return data


def last_error(seat: int) -> dict | None:
    data, _ = read_json_safe(seat_dir(seat) / "error.json")
    return data


def seat_status(seat: int) -> dict | None:
    data, _ = read_json_safe(seat_dir(seat) / "status.json")
    return data


def chat_pending(seat: int) -> dict | None:
    data, _ = read_json_safe(seat_dir(seat) / "chat_pending.json")
    return data


def read_events(seat: int, *, since: int = 0, limit: int = 0) -> list[dict]:
    p = seat_dir(seat) / "events.jsonl"
    if not p.exists():
        return []
    rows: list[dict] = []
    try:
        for raw in p.read_text(encoding="utf-8").splitlines():
            if not raw.strip():
                continue
            try:
                r = json.loads(raw)
                if int(r.get("seq") or 0) > since:
                    rows.append(r)
            except (json.JSONDecodeError, ValueError, TypeError):
                continue
    except OSError:
        return []
    return rows[-limit:] if limit > 0 else rows


def touch_chat_ready(seat: int) -> None:
    try:
        write_atomic_json(seat_dir(seat) / "chat_ready.json", {
            "ts": time.time(),
            "pid": os.getpid(),
            "at": time.strftime("%H:%M:%S"),
        })
    except OSError:
        pass


def atomic_write(path: Path, obj: dict) -> None:
    write_atomic_json(path, obj)


def submit_and_confirm(seat: int, ans_payload: dict, *, expected_round: int,
                       timeout: float = 1.5) -> tuple[bool, str | None]:
    """写回 answer.json 并等待引擎受理；若立即被 error.json 拒绝则返回失败原因。"""
    sdir = seat_dir(seat)
    unlink_safe(sdir / "error.json")
    atomic_write(sdir / "answer.json", ans_payload)
    t0 = time.monotonic()
    while time.monotonic() - t0 <= timeout:
        err_obj = last_error(seat)
        if err_obj and int(err_obj.get("round") or -1) == int(expected_round):
            return False, str(err_obj.get("error") or "引擎拒绝")
        cur = pending(seat)
        if cur is None or int(cur.get("round") or -1) != int(expected_round):
            return True, None
        time.sleep(0.05)
    return True, None


def _fmt_tile_with_id(tid: str) -> str:
    return f"{tile_label(tid)}({tid})"


def show(req: dict) -> None:
    """把待决的这一手讲成人话：局况、四家公开牌桌、动向、手牌、听牌提示与可做选项。"""
    v = req.get("view") or {}
    tbl = req.get("table") or {}
    my_seat = int(req.get("seat") or (int(v.get("seat") or 0) + 1))
    game_round = tbl.get("round") or v.get("round")
    lap = tbl.get("lap") or v.get("lap")
    steps = tbl.get("steps")
    dealer_lbl = tbl.get("dealer") or (
        seats_mod.label(v["dealer"]) if v.get("dealer") is not None else None
    )

    head_parts = []
    if game_round:
        head_parts.append(f"第 {game_round} 局" + (f" 第 {lap} 圈" if lap else ""))
    if steps is not None:
        head_parts.append(f"步 {steps}")
    if dealer_lbl is not None:
        head_parts.append(f"庄 {dealer_lbl}号")
    prefix = f"[{' · '.join(head_parts)}] " if head_parts else ""
    forced_tag = " [唯一必选]" if req.get("forced") else ""
    who = req.get("name") or f"{my_seat}号"
    print(f"{prefix}[hand_round={req['round']}] 轮到 {who} {req.get('kind')}{forced_tag}")

    err = last_error(my_seat)
    if err and err.get("error"):
        print(f"⚠️ 上次提交被拒：{err['error']}")

    recent = req.get("recent_events") or []
    if recent:
        print("动向：" + "；".join(recent))

    # 四家公开情况（严格按 view 渲染，不含别家暗手）
    board = tbl.get("board") or v.get("board") or {}
    names = tbl.get("names") or v.get("names") or {}
    melds = v.get("melds") or {}
    discards = v.get("discards") or {}
    hand_sizes = v.get("hand_sizes") or {}
    ting = v.get("ting") or {}
    piao = v.get("piao") or {}
    dead = v.get("dead") or {}
    kans = v.get("kans") or {}
    n_seats = int(v.get("seats") or 4)
    for idx in range(n_seats):
        no = idx + 1
        sname = str(names.get(no) or names.get(str(no)) or f"{no}号")
        score = board.get(no, board.get(str(no)))
        score_s = f" {int(score):+d}" if score is not None else ""
        sz = hand_sizes.get(idx, hand_sizes.get(str(idx), "?"))
        flags = []
        if v.get("dealer") == idx:
            flags.append("庄")
        if ting.get(idx) or ting.get(str(idx)):
            flags.append("报听")
        if piao.get(idx) or piao.get(str(idx)):
            flags.append("飘")
        if kans.get(idx) or kans.get(str(idx)):
            flags.append(f"杠×{kans.get(idx) or kans.get(str(idx))}")
        if dead.get(idx) or dead.get(str(idx)):
            flags.append("弃胡")
        flag_s = f"[{'/'.join(flags)}]" if flags else ""
        s_melds = melds.get(idx, melds.get(str(idx), [])) or []
        meld_s = " ".join("/".join(tile_label(t) for t in (m if isinstance(m, list) else m.get("tiles", [])))
                          for m in s_melds) or "无"
        s_riv = discards.get(idx, discards.get(str(idx), [])) or []
        riv_s = " ".join(tile_label(t) for t in s_riv) if s_riv else "—"
        me_tag = "(你)" if no == my_seat else ""
        print(f"  {no}号{sname}{me_tag}{score_s} {flag_s} 手{sz} 副[{meld_s}] 河[{riv_s}]")

    wall_s = f"牌墙剩 {v.get('wall_left')} 张" + (
        f"，明牌 {_fmt_tile_with_id(v['wall_last'])}" if v.get("wall_last") else ""
    )
    if v.get("last"):
        last = v["last"]
        last_no = int(last.get("seat", 0)) + 1 if "seat" in last and int(last.get("seat", -1)) >= 0 else "?"
        # game.observe 中的 last.seat 是 0 基下标
        lt = last.get("tile")
        wall_s += f" | 上一张：{last_no}号 打 {_fmt_tile_with_id(lt)}"
    print(wall_s)

    hand = v.get("hand") or []
    drawn = v.get("drawn")
    drawn_s = f"（刚摸 {_fmt_tile_with_id(drawn)}）" if drawn else ""
    print(f"手牌({len(hand)})：" + " ".join(_fmt_tile_with_id(t) for t in hand) + drawn_s)
    fixed = v.get("fixed") or []
    if fixed:
        print("已成面子：" + "  ".join("/".join(tile_label(t) for t in m) if isinstance(m, list) else str(m) for m in fixed))

    hints = req.get("ting_hints") or []
    if hints:
        print("听牌提醒：" + "；".join(
            f"[pick {h['pick']}] {h['text']}" if h.get("pick") else str(h.get("text") or "")
            for h in hints
        ))

    print("可选动作（用 --pick N）：")
    for i, a in enumerate(req.get("legal") or [], 1):
        pick = a.get("pick") or i
        lbl = a.get("label") or action_label(a)
        body = a.get("tile") or a.get("combo") or ""
        if isinstance(body, list):
            body = "+".join(body)
        code = f" [{body}]" if body else ""
        print(f"  {pick}. {lbl}{code}")


def _print_event(ev: dict, as_json: bool) -> None:
    if as_json:
        out = {"event_type": ev.get("type"), **ev}
        print(json.dumps(out, ensure_ascii=False))
        return
    t = ev.get("type")
    if t == "round_end":
        print(f"[第 {ev.get('game_round')} 局结束] {ev.get('broadcast') or ''}")
    elif t == "match_end":
        print(f"[本场结束] 累计分：{ev.get('board')}")
    elif t == "round_start":
        print(f"[第 {ev.get('game_round')} 局开始] 庄家 {ev.get('dealer')}号")
    elif t == "chat":
        print(f"[收到聊天 | phase={ev.get('phase')}] {ev.get('text')}")
    else:
        print(f"[事件 {t}] {json.dumps(ev, ensure_ascii=False)}")


def cmd_pending(args) -> int:
    req = pending(args.seat)
    if not req:
        print("none")
        return 0
    if args.json:
        print(json.dumps(req, ensure_ascii=False))
    else:
        show(req)
    return 0


def cmd_status(args) -> int:
    st = seat_status(args.seat)
    req = pending(args.seat)
    cp = chat_pending(args.seat)
    err = last_error(args.seat)
    evs = read_events(args.seat, limit=5)
    payload = {
        "seat": args.seat,
        "status": st,
        "pending_round": req.get("round") if req else None,
        "pending_kind": req.get("kind") if req else None,
        "chat_pending": cp,
        "error": err,
        "recent_events": evs,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    st_name = (st or {}).get("state") or "unknown"
    print(f"{args.seat}号状态：{st_name} | 局 {(st or {}).get('game_round') or '-'} 步 {(st or {}).get('steps') or '-'}")
    w = (st or {}).get("waiting_on")
    if isinstance(w, dict) and w.get("name"):
        print(f"当前在等：{w.get('name')}（{w.get('kind') or '?'}·{w.get('decision') or '?'}）")
    if req:
        print(f"当前待落子：hand_round={req['round']} ({req.get('kind')})")
    if cp:
        print(f"当前待回聊天：[id={cp.get('id')}] {cp.get('text')}")
    if err:
        print(f"最近错误：{err.get('error')}")
    if evs:
        print("最近事件：")
        for ev in evs[-3:]:
            _print_event(ev, as_json=False)
    return 0


def cmd_events(args) -> int:
    evs = read_events(args.seat, since=args.since, limit=args.n)
    if args.json:
        print(json.dumps(evs, ensure_ascii=False))
        return 0
    if not evs:
        print("暂无事件")
        return 0
    for ev in evs:
        print(f"#{ev.get('seq')} [{ev.get('at', '')}] ", end="")
        _print_event(ev, as_json=False)
    return 0


def _wait_loop(seat: int, *, timeout: float = 0.0, as_json: bool = False,
               include_chat: bool = False, include_events: bool = False,
               since_event: int | None = None) -> int:
    t0 = time.monotonic()
    last_touch = 0.0
    if since_event is not None:
        event_cursor = int(since_event)
    elif include_events:
        # 若调用时本局正好刚结束而 pending 为空，立即返回最近一条 round_end/match_end；否则从当前末尾开始等新事件
        existing = read_events(seat)
        st = seat_status(seat) or {}
        if not pending(seat) and st.get("state") in ("awaiting_next", "finished") and existing:
            for ev in reversed(existing):
                if ev.get("type") in ("round_end", "match_end"):
                    _print_event(ev, as_json=as_json)
                    return 0
        event_cursor = int(existing[-1]["seq"]) if existing else 0
    else:
        event_cursor = 0

    st0 = seat_status(seat) or {}
    saw_active = st0.get("state") in ("playing", "waiting_decision", "chatting", "paused", "awaiting_next")
    active_table_id = str(st0.get("table_id") or "")

    while True:
        if include_chat and time.monotonic() - last_touch >= 30.0:
            touch_chat_ready(seat)
            last_touch = time.monotonic()

        req = pending(seat)
        if req:
            if as_json:
                print(json.dumps(req, ensure_ascii=False))
            else:
                show(req)
            return 0

        if include_chat:
            cp = chat_pending(seat)
            if cp:
                ev = {"type": "chat", **cp}
                _print_event(ev, as_json=as_json)
                return 0

        if include_events:
            new_evs = read_events(seat, since=event_cursor)
            for ev in new_evs:
                event_cursor = max(event_cursor, int(ev.get("seq") or 0))
                if ev.get("type") in ("round_end", "match_end"):
                    _print_event(ev, as_json=as_json)
                    return 0

        st_now = seat_status(seat) or {}
        cur_state = st_now.get("state")
        cur_tid = str(st_now.get("table_id") or "")
        if cur_tid and cur_tid != active_table_id:
            active_table_id = cur_tid
            saw_active = cur_state in ("playing", "waiting_decision", "chatting", "paused", "awaiting_next")
        elif cur_state in ("playing", "waiting_decision", "chatting", "paused", "awaiting_next"):
            saw_active = True
        elif saw_active and cur_state in ("stopped", "interrupted"):
            _print_event({"type": f"table_{cur_state}", "status": st_now}, as_json=as_json)
            return 0 if cur_state == "stopped" else 1

        if timeout and time.monotonic() - t0 > timeout:
            print("timeout")
            return 1
        time.sleep(0.15)


def cmd_wait(args) -> int:
    return _wait_loop(
        args.seat,
        timeout=args.timeout,
        as_json=args.json,
        include_chat=args.include_chat,
        include_events=args.include_events,
        since_event=args.since_event,
    )


def _normalize_tile_arg(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        return tile_id(parse_tile(raw))
    except ValueError:
        return str(raw).strip()


def _normalize_combo_arg(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    parts = [x for x in str(raw).replace("+", " ").split() if x]
    out = []
    for p in parts:
        try:
            out.append(tile_id(parse_tile(p)))
        except ValueError:
            out.append(p)
    return out


def cmd_answer(args) -> int:
    req = pending(args.seat)
    if not req:
        st = seat_status(args.seat) or {}
        state_s = st.get("state")
        hint = f"（当前状态：{state_s}）" if state_s else ""
        print(f"现在不是你出牌（没有 pending）{hint}", file=sys.stderr)
        return 1

    ans: dict = {"round": req["round"], "say": args.say or ""}
    if args.pick:
        ans["pick"] = args.pick
    elif args.kind:
        payload: dict = {"kind": args.kind}
        if args.tile:
            payload["tile"] = _normalize_tile_arg(args.tile)
        if args.combo:
            payload["combo"] = _normalize_combo_arg(args.combo)
        ans["action"] = payload
    else:
        print("请提供 --pick N，或 --kind <动作>", file=sys.stderr)
        return 1

    matched, is_pause, say, err = resolve_external_answer(
        ans, req.get("legal") or [], expected_round=req["round"])
    if err or matched is None:
        print(err or "无效动作", file=sys.stderr)
        return 1

    existing_evs = read_events(args.seat)
    pre_seq = int(existing_evs[-1]["seq"]) if existing_evs else 0
    payload = {
        "round": req["round"],
        "action": matched,
        "say": say,
    }

    if not args.wait:
        unlink_safe(seat_dir(args.seat) / "error.json")
        atomic_write(seat_dir(args.seat) / "answer.json", payload)
        if args.json:
            print(json.dumps({
                "ok": True, "round": req["round"], "pause": is_pause,
                "action": matched, "say": say,
            }, ensure_ascii=False))
        elif is_pause:
            print(f"已举手暂停：{say or '（没说话）'}")
        else:
            print(f"已出：{action_label(matched)}".strip())
        return 0

    if not args.json:
        if is_pause:
            print(f"已举手暂停：{say or '（没说话）'}")
        else:
            print(f"已出：{action_label(matched)}".strip())

    ok, reject_err = submit_and_confirm(
        args.seat, payload, expected_round=req["round"], timeout=1.5)
    if not ok:
        if args.json:
            print(json.dumps({"ok": False, "round": req["round"], "error": reject_err}, ensure_ascii=False))
        else:
            print(f"引擎拒绝该动作：{reject_err}", file=sys.stderr)
        return 1

    since_ev = args.since_event if args.since_event is not None else pre_seq
    return _wait_loop(
        args.seat,
        timeout=args.timeout,
        as_json=args.json,
        include_chat=args.include_chat,
        include_events=args.include_events,
        since_event=since_ev,
    )


def cmd_give_up(args) -> int:
    (seat_dir(args.seat) / "give-up").write_text("", encoding="utf-8")
    print("这手交随机了")
    return 0


def cmd_reply(args) -> int:
    text = args.text
    if text == "-":
        text = sys.stdin.read().strip()
    text = str(text or "").strip()
    if not text:
        print("回复内容不能为空", file=sys.stderr)
        return 1
    touch_chat_ready(args.seat)
    cp = chat_pending(args.seat)
    payload = {"text": text, "at": time.strftime("%H:%M:%S")}
    if cp and cp.get("id"):
        payload["id"] = cp["id"]
    atomic_write(seat_dir(args.seat) / "chat_answer.json", payload)
    print(f"已回聊：{text}")
    return 0


def cmd_trace(args) -> int:
    path = Path(args.file)
    if not path.exists():
        print("还没有 trace")
        return 0
    rows = path.read_text(encoding="utf-8").splitlines()[-args.n:]
    for line in rows:
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        act = r.get("action") or {}
        body = act.get("tile") or act.get("combo") or ""
        if isinstance(body, list):
            body = "+".join(body)
        extra = ""
        if r.get("prompt"):
            extra = f" | prompt {r['prompt']} 复用 {r.get('cached', 0)} 新读 {r.get('read', 0)}"
        print(f"{r.get('t','')} {r.get('name','?')} {act.get('kind','-')} {body} "
              f"[{r.get('seconds', 0)}s]{extra}")
    return 0


def cmd_log(args) -> int:
    """打牌时看见的问题，随手记一条；局终一次性交代。"""
    path = Path(args.file)
    if args.list:
        print(path.read_text(encoding="utf-8").rstrip() if path.exists() else "还没有问题记录")
        return 0
    text = " ".join(args.text).strip()
    if not text:
        print("要写点什么", file=sys.stderr)
        return 1
    line = f"- {time.strftime('%m-%d %H:%M')} {text}"
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)
    return 0


def _talk_path(seat: int, which: str) -> Path:
    return seat_dir(seat) / f"talk_{which}.jsonl"


def _talk_rows(seat: int, which: str) -> list[dict]:
    p = _talk_path(seat, which)
    if not p.exists():
        return []
    rows = []
    for raw in p.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return rows


def cmd_hear(args) -> int:
    rows = _talk_rows(args.seat, "in")
    if not rows:
        print("他没递话")
        return 0
    for i, r in enumerate(rows[-args.n:], max(1, len(rows) - args.n + 1)):
        print(f"[{i}] {r.get('t', '')} {r.get('text', '')}")
    return 0


def cmd_tell(args) -> int:
    p = _talk_path(args.seat, "out")
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"t": time.strftime("%H:%M:%S"), "text": args.text},
                           ensure_ascii=False) + "\n")
    print(f"已回话：{args.text}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("pending", help="查看当前待决这一手")
    p.add_argument("--seat", type=int, default=3)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_pending)

    p = sub.add_parser("status", help="查看座位状态、错误和最近事件")
    p.add_argument("--seat", type=int, default=3)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("events", help="查看局始、收局结算事件")
    p.add_argument("--seat", type=int, default=3)
    p.add_argument("-n", type=int, default=10)
    p.add_argument("--since", type=int, default=0)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_events)

    p = sub.add_parser("wait", help="阻塞等下一手（可选等局末事件或聊天）")
    p.add_argument("--seat", type=int, default=3)
    p.add_argument("--timeout", type=float, default=0)
    p.add_argument("--json", action="store_true")
    p.add_argument("--include-events", "--events", action="store_true", dest="include_events")
    p.add_argument("--include-chat", "--chat", action="store_true", dest="include_chat")
    p.add_argument("--since-event", type=int, default=None)
    p.set_defaults(func=cmd_wait)

    p = sub.add_parser("answer", help="落子（支持 --wait 提交后直接等下一手）")
    p.add_argument("--seat", type=int, default=3)
    p.add_argument("--pick", type=int, default=0, help="直接选第几个可选动作（1 基，完整保留 meta）")
    p.add_argument("--kind", default="")
    p.add_argument("--tile", default=None)
    p.add_argument("--combo", default=None)
    p.add_argument("--say", default="")
    p.add_argument("--wait", "--next", action="store_true", dest="wait", help="校验受理后立刻等下一手")
    p.add_argument("--timeout", type=float, default=0)
    p.add_argument("--json", action="store_true")
    p.add_argument("--include-events", "--events", action="store_true", dest="include_events")
    p.add_argument("--include-chat", "--chat", action="store_true", dest="include_chat")
    p.add_argument("--since-event", type=int, default=None)
    p.set_defaults(func=cmd_answer)

    p = sub.add_parser("reply", help="回复来自网页或 TUI 的聊天请求")
    p.add_argument("--seat", type=int, default=3)
    p.add_argument("text")
    p.set_defaults(func=cmd_reply)

    p = sub.add_parser("give-up")
    p.add_argument("--seat", type=int, default=3)
    p.set_defaults(func=cmd_give_up)

    p = sub.add_parser("trace")
    p.add_argument("-n", type=int, default=20)
    p.add_argument("--file", default=str(Path(__file__).resolve().parent / "trace.jsonl"))
    p.set_defaults(func=cmd_trace)

    p = sub.add_parser("log", help="打牌时发现问题就记一条，局终一起交代")
    p.add_argument("text", nargs="*")
    p.add_argument("-l", "--list", action="store_true")
    p.add_argument("--file", default=str(Path(__file__).resolve().parent / "问题.md"))
    p.set_defaults(func=cmd_log)

    p = sub.add_parser("hear", help="读他在递话窗口里说的话")
    p.add_argument("--seat", type=int, default=3)
    p.add_argument("-n", type=int, default=10)
    p.set_defaults(func=cmd_hear)

    p = sub.add_parser("tell", help="往递话窗口回一句")
    p.add_argument("--seat", type=int, default=3)
    p.add_argument("text")
    p.set_defaults(func=cmd_tell)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
