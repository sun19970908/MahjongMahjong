"""牌友单聊窗口：只拼规则、人设、笔记，工具只有「改笔记」。

跟 play.py 完全分开：不带牌桌、不带手牌、不带打牌工具，独一份对话历史。
它想改笔记就自己改牌友笔记.md，改完接着跟你说话；这里不限制篇幅、不限制改几次。

用法：python chat.py --player 方小满
    （输入 /send 发送，/quit 退出；支持多行，空行原样保留）
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from game import _tool
from llm_player import StrataClient
from player_profile import all_profiles, by_name

HERE = Path(__file__).resolve().parent
RULES_PATH = HERE / "规则.txt"
NOTES_PATH = HERE / "牌友笔记.md"

PERSONA = ("你是一桌麻将里的牌友。"
           "现在不在牌桌上，是单独聊天：没有牌局、没有手牌，只有规则和你的笔记。")

NOTE_TOOL = _tool(
    "edit_note",
    "改你的长期笔记（profile 里的 长期笔记.md）。mode=append 往末尾加一条，写 new_string；"
    "mode=replace 把 old_string 那一小段原文换成 new_string，old_string 必须唯一；"
    "mode=replace_all 把 old_string 的所有出现都换掉；"
    "mode=replace_tail 从 old_string 第一次出现处一直改到文件末尾；"
    "mode=overwrite 用 new_string 整篇覆盖。找不到也不拦，挑能用的方式改。",
    {"mode": {"type": "string", "enum": ["append", "replace", "replace_all", "replace_tail", "overwrite"]},
     "old_string": {"type": "string"},
     "new_string": {"type": "string"}},
    ["mode"])

TOOLS = [NOTE_TOOL]


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def system_prompt(profile=None) -> str:
    rules = _read(RULES_PATH) or "（规则文件没找到）"
    if profile:
        name = profile.display_name
        notes = _read(profile.notes_path) or "（还没记过什么）"
        persona = profile.persona or PERSONA
        head = f"你是「{name}」。\n{persona}"
    else:
        notes = _read(NOTES_PATH) or "（还没记过什么）"
        head = PERSONA
    return f"{head}\n\n【规则】\n{rules}\n\n【你的笔记】\n{notes}"


def _read_message() -> str | None:
    """读一条消息：可多行，/send 发送，/quit 退出；空行原样保留。"""
    lines: list[str] = []
    first = True
    while True:
        prompt = " 你：" if first else "    "
        first = False
        try:
            raw = input(prompt)
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        cmd = raw.strip().lower()
        if cmd == "/quit":
            return None
        if cmd == "/send":
            return "\n".join(lines)
        lines.append(raw)


def apply_note(path: Path, args: dict) -> str:
    """edit_note 落地：append / replace / replace_all / replace_tail / overwrite。"""
    mode = str(args.get("mode") or "").strip().lower()
    old = str(args.get("old_string") or "")
    new = str(args.get("new_string") or args.get("text") or "")
    if not mode:
        mode = "replace" if old else "append"

    body = _read(path)
    if mode == "append":
        if not new.strip():
            return "没写内容，笔记没动"
        fresh = []
        for ln in new.splitlines():
            ln = ln.strip()
            if ln:
                fresh.append(ln if ln[:1] in "-*#" else "- " + ln)
        body = "\n".join(x for x in (body, *fresh) if x)
    elif mode in ("replace", "replace_all"):
        if not old:
            return "replace 得给我 old_string（要改的那段原文）"
        n = body.count(old)
        if n == 0:
            return ("笔记里找不到这段原文，没动："
                    f"{old[:80]}。要整段改到末尾用 mode=replace_tail，整篇重写用 mode=overwrite")
        if n > 1 and mode != "replace_all":
            return f"这段在笔记里出现 {n} 次，不唯一，没动（要全改就用 replace_all）"
        body = body.replace(old, new) if mode == "replace_all" else body.replace(old, new, 1)
    elif mode == "replace_tail":
        if not old:
            return "replace_tail 得给我 old_string（从哪句开始改到末尾）"
        idx = body.find(old)
        if idx < 0:
            return f"笔记里找不到这个起点，没动：{old[:80]}。整篇重写可以用 mode=overwrite"
        body = body[:idx] + new
    elif mode == "overwrite":
        body = new
    else:
        return f"不认识的做法 {mode}"

    path.write_text(body.rstrip() + "\n", encoding="utf-8")
    return "笔记改好了，现在是：\n" + body


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--player", required=True,
                    help="玩家名；用 profiles/<玩家名>/profile.json")
    ap.add_argument("--model", default=None, help="默认用档案里的 model")
    ap.add_argument("--budget", type=int, default=6000, help="思考预算 token；改笔记按这个预算放开")
    args = ap.parse_args()

    profile = by_name(args.player)
    if profile is None:
        names = ", ".join(p.name for p in all_profiles())
        print(f"找不到玩家档案：{args.player}（现有：{names}）")
        return 2
    notes_path = profile.notes_path
    model = args.model or profile.model
    client = StrataClient.from_profile(profile)
    messages: list[dict] = [{"role": "system", "content": system_prompt(profile)}]
    print(f" 牌友单聊：{profile.display_name}；只带规则、人设、自己的笔记；它能改自己的长期笔记。"
          "输入 /send 发送，/quit 退出；换行和空行都会保留。")

    while True:
        message = _read_message()
        if message is None:
            break
        if not message.strip():
            print("（没写内容，没发）")
            continue
        messages.append({"role": "user", "content": message})

        while True:                            # 想改几次就改几次，不设轮数上限
            r = client.chat(messages, model=model, tools=TOOLS,
                            tool_choice="auto",
                            reasoning_budget=args.budget)
            if r.tool_call and r.tool_call.get("name") == "edit_note":
                call_id = f"note_{len(messages)}"
                messages.append({
                    "role": "assistant", "content": r.speech,
                    **({"reasoning_content": r.reasoning} if r.reasoning else {}),
                    "tool_calls": [{"id": call_id, "type": "function", "function": {
                        "name": "edit_note",
                        "arguments": json.dumps(r.tool_call.get("arguments") or {},
                                               ensure_ascii=False)}}]})
                done = apply_note(notes_path, r.tool_call.get("arguments") or {})
                messages.append({"role": "tool", "tool_call_id": call_id, "content": done})
                if r.speech:
                    print(f" 牌友：{r.speech}")
                continue
            print(f" 牌友：{r.speech}")
            messages.append({"role": "assistant", "content": r.speech})
            break

    print(" 走了。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
