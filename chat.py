"""牌友单聊窗口：只拼规则、人设、笔记，工具只有「改笔记」。

跟 play.py 完全分开：不带牌桌、不带手牌、不带打牌工具，独一份对话历史。
它想改笔记就自己改牌友笔记.md，改完接着跟你说话；这里不限制篇幅、不限制改几次。

用法：python chat.py        （空行回车退出）
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from game import _tool
from llm_player import StrataClient, _room_for

HERE = Path(__file__).resolve().parent
RULES_PATH = HERE / "规则.txt"
NOTES_PATH = HERE / "牌友笔记.md"

PERSONA = ("你是「豆包工作」，一桌麻将里的牌友。说话像牌友，记账像会计。"
           "现在不在牌桌上，是单独聊天：没有牌局、没有手牌，只有规则和你的笔记。")

NOTE_TOOL = _tool(
    "edit_note",
    "改你的笔记（牌友笔记.md）。mode=append 往末尾加一条，写 new_string；"
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


def system_prompt() -> str:
    rules = _read(RULES_PATH) or "（规则文件没找到）"
    notes = _read(NOTES_PATH) or "（还没记过什么）"
    return f"{PERSONA}\n\n【规则】\n{rules}\n\n【你的笔记】\n{notes}"


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
    ap.add_argument("--model", default="strata-lowthink")
    ap.add_argument("--budget", type=int, default=6000, help="思考预算 token；改笔记按这个预算放开")
    args = ap.parse_args()

    client = StrataClient()
    messages: list[dict] = [{"role": "system", "content": system_prompt()}]
    print(" 牌友单聊：只带规则、人设、你的笔记；它能改 牌友笔记.md。空行回车退出。")

    while True:
        try:
            line = input(" 你：").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            break
        messages.append({"role": "user", "content": line})

        while True:                            # 想改几次就改几次，不设轮数上限
            r = client.chat(messages, model=args.model, tools=TOOLS,
                            tool_choice="auto", max_tokens=_room_for(args.budget),
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
                done = apply_note(NOTES_PATH, r.tool_call.get("arguments") or {})
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
