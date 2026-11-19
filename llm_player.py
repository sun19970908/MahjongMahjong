"""把 LLM 接到牌局上。

三件事：
    1) StrataClient —— 只说 HTTP，不懂麻将。
    2) LLMPlayer   —— 看 Decision，回 Action。两遍调用：先想清楚再调工具。
    3) Thinker     —— 空闲时在后台预跑一遍"本手计划"，到它回合直接拿来用。

调用节奏（和实测数据对齐）：
    pass 1  auto + strata       思考 + 说话 + 给结论     ~15-25s
    pass 2  required + nothink  只把结论变成工具调用      ~1-2s
    两遍加起来 20-30s，塞得进 60 秒预算。

prompt 布局（按你定的）：
    [system] 人设 + 规则 + 截断提醒  ← 静态，可被 prompt cache 命中
    [历史]   之前每一手的牌桌播报 + 它自己说过的话
    [user]   当前牌桌 + 可选动作
    [笔记]   最下边（每次从 牌友笔记.md 现读，改完立刻生效）
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

import seats
from game import TOOLS, Decision, Game
from referee import KINDS, RED, Action, parse_tile, tile_id, tile_name

HERE = Path(__file__).resolve().parent
DEFAULT_NOTES = HERE / "牌友笔记.md"
DEFAULT_DRAFT = HERE / "长期笔记草稿.md"
DEFAULT_LOG = HERE / "llm_log.jsonl"

# 旧存档里写进历史的占位话：模型会把它当成自己上一句台词照着念，一句都别再摆上桌
_FILLERS = frozenset({"（闲下来想这手牌，还没想完）", "（闲下来想这手牌,还没想完）"})

# 任何时候都能喊的两件事（不算这一手）：说两句停一下、记条笔记。
# 以前只有"中场暂停 / 散场闲聊"才提，出牌那手它根本不知道还能这么干。
_ALWAYS = ("；另外这两件随手就能喊：pause（这手先不打，停下来跟他说两句）、"
           "edit_note（临时笔记 target=temp；长期笔记局中只能 target=long 追加草稿，记完接着打这一手）")


# ---------------------------------------------------------------- HTTP

@dataclass
class ChatResult:
    content: str = ""
    reasoning: str = ""
    tool_call: dict | None = None      # {"name": str, "arguments": dict}
    tokens: int = 0
    seconds: float = 0.0
    finish: str = ""
    aborted: bool = False              # 流被掐断：里面只有半截
    prompt_tokens: int = 0             # 这次请求的 prompt 有多少 token
    cached_tokens: int = 0             # 其中命中前缀缓存的有多少

    @property
    def speech(self) -> str:
        return (self.content or "").strip()

    @property
    def hit_rate(self) -> float:
        return self.cached_tokens / self.prompt_tokens if self.prompt_tokens else 0.0

    @property
    def read_tokens(self) -> int:
        """真正要重读的部分：新增的那一段。前缀没被动过时它就该只剩这点。"""
        return max(0, self.prompt_tokens - self.cached_tokens)


def _room_for(budget: int) -> int:
    """引擎会从 max_tokens 里留一块给答案（至少 512，或四分之一），留完才轮到思考。
    想真拿到 budget，就得把这块一起报上去，否则 3000 会被砍成 2700。"""
    return budget + max(512, budget // 3)


def _usage_of(usage: dict | None) -> tuple[int, int]:
    """Strata 把命中的部分放在 prompt_tokens_details.cached_tokens（OpenAI 同名字段）。"""
    u = usage or {}
    prompt = int(u.get("prompt_tokens") or 0)
    cached = int((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    return prompt, cached


class StrataClient:
    def __init__(self, url: str = "http://127.0.0.1:11436/v1/chat/completions", timeout: float = 3600.0) -> None:
        self.url = url
        self.timeout = timeout

    def chat(
        self,
        messages: list[dict],
        *,
        model: str = "strata",
        tools: list[dict] | None = None,
        tool_choice=None,
        max_tokens: int = 1400,
        temperature: float = 0.0,
        reasoning_budget: int | None = None,
        on_response: Callable | None = None,
        stream: bool = False,
        on_delta: Callable | None = None,
    ) -> ChatResult:
        body: dict = {"model": model, "messages": messages, "max_tokens": max_tokens,
                      "temperature": temperature}
        if reasoning_budget is not None:
            # Strata #123：思考到这么多 token 就自己收尾（注入一句 wrap-up + </think>），
            # 把剩下的预算留给回答。0 表示不限。
            body["reasoning_budget_tokens"] = int(reasoning_budget)
        if tools:
            body["tools"] = tools
            body["tool_choice"] = tool_choice if tool_choice is not None else "auto"
        if stream:
            # 只有流式它才会一边生成一边往外写；非流式要等生成完才写，
            # 客户端断了它也看不见（server.py 的断连靠 wfile.write 抛 OSError 发现）。
            body["stream"] = True
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.url, data=data,
                                     headers={"Content-Type": "application/json"})
        t0 = time.monotonic()
        if stream:
            content: list[str] = []
            reasoning: list[str] = []
            calls: dict[int, dict] = {}      # 流式也把工具调用拼起来，不然拿不到落子
            finish = ""
            aborted = False
            usage: dict | None = None
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if on_response:
                    on_response(resp)
                try:
                    for raw in resp:
                        line = raw.decode("utf-8", "replace").strip()
                        if not line.startswith("data:"):
                            continue
                        chunk = line[5:].strip()
                        if chunk == "[DONE]":
                            break
                        try:
                            obj = json.loads(chunk)
                        except json.JSONDecodeError:
                            continue
                        if obj.get("usage"):          # 最后一个 chunk 带着命中情况
                            usage = obj["usage"]
                        choice = (obj.get("choices") or [{}])[0]
                        delta = choice.get("delta") or {}
                        if delta.get("content"):
                            content.append(delta["content"])
                        if delta.get("reasoning_content"):
                            reasoning.append(delta["reasoning_content"])
                        for tcall in delta.get("tool_calls") or []:
                            slot = calls.setdefault(int(tcall.get("index", 0)),
                                                    {"id": "", "name": "", "arguments": ""})
                            if tcall.get("id"):
                                slot["id"] = tcall["id"]
                            fn = tcall.get("function") or {}
                            if fn.get("name"):
                                slot["name"] = fn["name"]
                            if fn.get("arguments"):
                                slot["arguments"] += fn["arguments"]
                        if choice.get("finish_reason"):
                            finish = choice["finish_reason"]
                        if on_delta:
                            on_delta("think" if delta.get("reasoning_content") else "say",
                                     len("".join(reasoning)), len("".join(content)))
                except Exception:
                    aborted = True       # 连接被掐：已经吐出来的照样带走，别扔
            prompt_tokens, cached_tokens = _usage_of(usage)
            tc = None
            for slot in calls.values():
                if not slot["name"]:
                    continue
                try:
                    args = json.loads(slot["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                tc = {"name": slot["name"], "arguments": args}
                break
            return ChatResult(content="".join(content), reasoning="".join(reasoning),
                              tool_call=tc, seconds=round(time.monotonic() - t0, 2), finish=finish,
                              aborted=aborted, prompt_tokens=prompt_tokens, cached_tokens=cached_tokens)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            if on_response:
                on_response(resp)       # 让别人能在读的过程中把它掐掉
            payload = json.loads(resp.read().decode("utf-8"))
        dt = time.monotonic() - t0
        msg = payload["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
        tc = None
        if calls:
            fn = calls[0]["function"]
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            tc = {"name": fn.get("name"), "arguments": args}
        prompt_tokens, cached_tokens = _usage_of(payload.get("usage"))
        return ChatResult(
            content=msg.get("content") or "",
            reasoning=msg.get("reasoning_content") or "",
            tool_call=tc,
            tokens=payload.get("usage", {}).get("completion_tokens", 0),
            seconds=round(dt, 2),
            finish=payload["choices"][0].get("finish_reason", ""),
            prompt_tokens=prompt_tokens,
            cached_tokens=cached_tokens,
        )


# ---------------------------------------------------------------- 文本兜底

_TILE_RE = re.compile(r"\b([mpsz])([1-9])\b")
_CN_RE = re.compile(r"([1-9])(万|饼|条)|红中")
_KIND_WORDS = {"胡": "win", "碰": "pon", "杠": "kan", "吃": "chi", "过": "pass", "不要": "pass"}


def actions_from_text(text: str, legal: Sequence[Action]) -> list[Action]:
    """从它说的话里拣出合法动作（只认 legal 里有的）。"""
    found_tiles = set()
    for suit, rank in _TILE_RE.findall(text or ""):
        found_tiles.add(f"{suit}{rank}")
    for rank, suit in _CN_RE.findall(text or ""):
        found_tiles.add(f"{suit}{rank}")
    if "红中" in (text or ""):
        found_tiles.add("z5")

    found_kinds = {v for k, v in _KIND_WORDS.items() if k in (text or "")}
    out: list[Action] = []
    for a in legal:
        if a.kind in ("win", "pass") and a.kind in found_kinds:
            out.append(a)
            continue
        if a.tile is not None and tile_id(a.tile) in found_tiles and a.kind in ({"discard"} | found_kinds):
            out.append(a)
    # 只认唯一解：同一张牌只能说一次
    uniq = {(a.kind, a.tile, a.combo): a for a in out}
    return list(uniq.values())


# ---------------------------------------------------------------- 选手

@dataclass
class TurnRecord:
    index: int
    speech: str
    action: Action
    used_fallback: bool = False
    note: str = ""
    reasoning: str = ""
    seconds: float = 0.0
    tokens: int = 0
    source: str = ""          # auto / tool / tool2 / text / fallback
    finish: str = ""


class LLMPlayer:
    def __init__(
        self,
        client: StrataClient,
        seat: int,
        *,
        notes_path: Path | str = DEFAULT_NOTES,
        draft_path: Path | str | None = None,
        soft_notes_path: Path | str | None = None,
        log_path: Path | str | None = DEFAULT_LOG,
        model: str = "strata-lowthink",
        answer_tokens: int = 600,
        think_seconds: float = 3600.0,
        budget_react: int = 1000,
        budget_discard: int = 3000,
        budget_ting: int = 200,
        budget_chat: int = 1200,
        budget_final: int = 6000,
        budget_tiny: int = 40,
        debug_cache: bool = False,
    ) -> None:
        self.client = client
        self.seat = seat
        self.notes_path = Path(notes_path)
        if draft_path:
            self.draft_path = Path(draft_path)
        elif self.notes_path == DEFAULT_NOTES:
            self.draft_path = DEFAULT_DRAFT
        else:
            # 自测会用临时笔记路径，草稿跟着各自走，别串台
            self.draft_path = self.notes_path.with_name(self.notes_path.stem + ".草稿.md")
        self.rules_path = HERE / "规则.txt"
        # 临时笔记：只这一局用，随牌桌一起贴在每手的 user 消息里（在尾巴上，不毁前缀）
        self.temp_notes_path = (Path(soft_notes_path) if soft_notes_path
                                else HERE / "临时笔记.md")
        self.log_path = Path(log_path) if log_path else None
        self.model = model
        self.answer_tokens = answer_tokens         # 预算之外留给"说话 + 调工具"的空间
        # 一小时之内都算没超时：想多久随它，别拿秒数去剪它的思路
        self.think_seconds = think_seconds
        # Strata #123：思考到这么多 token 由引擎自己收尾（注入 wrap-up + </think>），
        # 剩下的 max_tokens 留给说话和工具调用。吃叉 1000、打牌 3000。
        self.budget_react = budget_react
        self.budget_discard = budget_discard
        self.budget_ting = budget_ting         # 报听之后手牌锁死，没什么好想的
        self.budget_chat = budget_chat         # 中场/局间聊天：够记必要信息就行
        self.budget_final = budget_final       # 终场改长期笔记：放开让它随便改
        self.budget_tiny = budget_tiny       # 补刀那一遍：够它把话翻成调用就行
        self.debug_cache = debug_cache       # 每次请求后打一行命中率
        self.cache_stats: list[dict] = []
        self._chatted = False                # 中场暂停 / 散场闲聊有没有开过口
        self._last_chat_phase: str | None = None   # 上次聊天的场景：换场景要重新递笔记
        self._phase = "play"                 # play / pause / between / after：笔记权限按这个分
        self._pause_calls = 0                # 这一回合它喊了几次暂停
        self._long_draft: list[str] = []     # 整场追加的长期笔记草稿，全部打完再并档
        self._long_draft_in_notes = False    # 草稿是否已经落进长期笔记文件
        self._load_long_draft()
        self.on_progress: Callable | None = None   # 流式心跳钩子：(kind, 想字, 说字)
        self._tick_at = 0.0
        self._last_say = ""                  # 上一手说出口的那句：同一句再说一遍就是复读

        self.messages: list[dict] = [{"role": "system", "content": self._system_prompt()}]
        self.records: list[TurnRecord] = []
        self._prefetching: bool = False
        self._prefetch_thread: threading.Thread | None = None
        self._resp = None              # 正在读的那条自由思考连接：轮到自己时掐掉
        self._lock = threading.Lock()

    # ---- prompt 拼装 ----
    def _read_rules(self) -> str:
        """规则原文直接读 规则.txt：以前这里是硬拷贝一份，改规则就得改两处。"""
        try:
            return self.rules_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return "每个人13张牌；无风牌 无发白 有红中。"

    def _system_prompt(self) -> str:
        head = (
            "你是「豆包工作」，一桌麻将里的牌友。四个玩家，另外两人随机出牌。\n"
            "说话像牌友，记账像会计。你说的话会直接显示在牌桌上。\n"
            "嘴上不交底：自己手上有什么、在等哪张、还差几组面子，一概不说；"
            "从别人亮出的面子、弃牌里推出来的结论也别在牌桌上讲。\n"
            "要开口就说场面话（催牌、玩笑、叹手气），真判断只写进笔记。\n"
            "规则（原文，以此为准）：\n"
            f"{self._read_rules()}\n"
            "报听要主动调 ting：手牌听牌之后报听，报听后手牌锁死——只能把刚摸进来的那张打回去，"
            "可以杠，不能碰不能吃；没报听不能胡。\n"
            "出牌必须调用工具，不许只用文字回答；牌用 m1..m9 / p1..p9 / s1..s9 / z5 表示。"
        )
        # 笔记只在这里插一次：system 是整段对话的开头，每一手都省下一千多字的重复
        notes = self._read_notes()
        return f"{head}\n\n【你的笔记】\n{notes}" if notes else head

    def _read_notes(self) -> str:
        try:
            return self.notes_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ""

    def _read_soft_notes(self) -> str:
        """临时笔记：中场暂停时记的，只这一局用。"""
        try:
            return self.temp_notes_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ""

    def add_soft_note(self, line: str, keep: int = 20) -> None:
        """它自己记的临时观察，不碰长期笔记。"""
        old = []
        try:
            old = self.temp_notes_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            pass
        old.append("- " + line.strip())
        self.temp_notes_path.write_text("\n".join(old[-keep:]) + "\n", encoding="utf-8")

    def _describe(self, view: dict) -> str:
        hand = " ".join(view["hand"])
        fixed = view.get("fixed") or []
        fixed_s = "；".join("/".join(m) for m in fixed) if fixed else "无"
        melds = {s: ms for s, ms in (view.get("melds") or {}).items() if ms}
        others = "；".join(f"{seats.label(s)}号 " + "/".join("/".join(m) for m in ms) for s, ms in melds.items()) or "无"
        table = "；".join(f"{seats.label(s)}号：{' '.join(t) if t else '—'}" for s, t in view["discards"].items())
        last = view.get("last")
        last_s = f"{seats.label(last['seat'])}号 打了 {last['tile']}" if last else "—"
        wall_s = f"牌墙还剩 {view['wall_left']} 张"
        if view.get("dealer") is not None:
            wall_s += f"；庄家 {seats.label(view['dealer'])} 号（庄家胡牌加一番）"
        if view.get("wall_last"):
            wall_s += f"，最后一张（明牌）是 {view['wall_last']}"
        if (view.get("dead") or {}).get(view["seat"]):
            wall_s += "\n（你本局放过胡，已经不准再胡了）"
        return (
            f"你手里的 {len(view['hand'])} 张：{hand}\n"
            f"你已成的面子：{fixed_s}\n"
            f"场上已成的面子（公开）：{others}\n"
            f"牌河：{table}\n"
            f"{wall_s}；上一张打出：{last_s}"
        )

    def _actions_text(self, legal: Sequence[Action], *, always: bool = True) -> str:
        lines = []
        for a in legal:
            if a.kind == "discard":
                lines.append(f"discard {tile_id(a.tile)}")
            elif a.kind in ("pon", "kan"):
                lines.append(f"{a.kind} {tile_id(a.tile)}")
            elif a.kind == "chi":
                lines.append(f"chi {'+'.join(tile_id(t) for t in a.combo)}")
            elif a.kind == "bloom":
                lines.append("bloom（取牌墙最后一张胡）")
            else:
                lines.append(a.kind)
        return "、".join(lines) + (_ALWAYS if always else "")

    def _tick(self, kind: str, n_think: int, n_say: int) -> None:
        """流式心跳：正在想还是卡住了，外面想知道就接这个钩子（1.5 秒一次，别刷屏）。"""
        if not self.on_progress:
            return
        now = time.monotonic()
        if now - self._tick_at < 1.5:
            return
        self._tick_at = now
        self.on_progress(kind, n_think, n_say)

    def _budget_for(self, d: Decision) -> int:
        """吃叉、报不报听只是判断接不接，1000 够；打牌要摊四组面子，给 3000。

        已经报了听的座位手牌锁死，只剩"打回刚摸的这张"这一件事：
        除了能胡、能杠（还有后开花）值得认真想，其余一律给最低预算。
        """
        if self._declared(d):
            return self.budget_discard if self._big_choice(d) else self.budget_ting
        return self.budget_react if d.kind in ("react", "ting_declare") else self.budget_discard

    @staticmethod
    def _declared(d: Decision) -> bool:
        """这一手是不是发生在"已经报过听"之后。"""
        ting = d.view.get("ting") or {}
        return bool(ting.get(d.seat) or ting.get(str(d.seat)))

    @staticmethod
    def _big_choice(d: Decision) -> bool:
        """能胡、能杠、能后开花：这几下值得把预算给满。"""
        return any(a.kind in ("win", "kan", "bloom") for a in d.legal)

    def _speak(self, d: Decision, budget: int) -> str:
        """锁死的手也要出声：只要一句话，不要结论。

        问和答成对落历史，不然下一手就出现"没有问题的 assistant"，模型和缓存都别扭。
        """
        prompt = "\n".join([
            self._headline(d),
            "【牌桌】",
            self._describe(d.view),
            f"【本轮只能做这些】{self._actions_text(d.legal, always=False)}",
            "这一手没得选，照着打下去就行。牌桌上说一句人话，不超过 20 字（别报手牌、别报在等哪张），不用调工具。",
        ])
        try:
            r = self.client.chat(self.messages + [{"role": "user", "content": prompt}],
                                 model=self.model, tools=TOOLS, tool_choice="auto",
                                 max_tokens=_room_for(budget), reasoning_budget=budget)
        except Exception:                                     # 说不出话就别拦着出牌
            return ""
        said = r.speech.strip()
        if said in _FILLERS or (said and said == self._last_say):
            said = ""
        if said:
            self._last_say = said
        self.messages.append({"role": "user", "content": prompt})
        self.messages.append({"role": "assistant", "content": said,
                              **({"reasoning_content": r.reasoning} if r.reasoning else {})})
        return said

    def _headline(self, d: Decision) -> str:
        """一句话说清现在是什么时刻，别让它自己猜。"""
        if d.kind == "ting_declare":
            return "【手牌听牌了，报不报？】"
        if any(a.kind == "bloom" for a in d.legal):
            return "【能后开花】"
        if any(a.kind == "win" for a in d.legal):
            return "【能胡了】"
        if d.kind == "react":
            return "【现在可以吃叉】"
        return "【轮到你出牌了】"

    def _user_prompt(self, d: Decision, budget: int) -> str:
        soft = self._read_soft_notes()
        parts = [
            self._headline(d),
            f"（思考预算 {budget} token，到点引擎会强制收尾；收尾之后先说一句话，再调用工具。）",
            "",
        ]
        if soft:
            parts += ["【本局临时笔记】", soft, ""]
        parts += [
            "【牌桌】",
            self._describe(d.view),
            "",
            # 工具表是固定的一整张（顶部，永不改动）；这一手到底能做哪几个，只写在这里。
            # 这一行每手都变，但它本来就在最尾巴上，前面几万 token 照样复用。
            f"【本轮只能做这些】{self._actions_text(d.legal)}",
            "只能从上表里挑一个，调表外的动作会被判无效。",
        ]
        draft = self._draft_block()
        if draft:
            parts += ["", draft]
        parts += ["先说一句话跟牌桌上说点什么（不超过 25 字，别报手牌、别报在等哪张），然后必须调用工具选一个。"]
        return "\n".join(parts)

    # ---- 决策 ----
    def decide(self, d: Decision) -> tuple[Action | None, str]:
        # 只有一个选项（摸牌、必过、报听后打回刚摸的那张）：不用问模型，省一次二十秒。
        # 但报听之后牌桌上还得有人味——那一手虽然锁死了，也低预算问一句，让它开口。
        if len(d.legal) == 1:
            act = d.legal[0]
            say = ""
            if d.kind == "discard" and self._declared(d):
                self.cut_prefetch()
                say = self._speak(d, self.budget_ting)
            self.records.append(TurnRecord(len(self.records), say, act, source="auto"))
            return act, say

        # 真要落子了：先掐断还在飞的自由思考，把通道让出来；
        # 然后照常进"打牌思考"——吃叉 1000 token，打牌 3000 token。
        self.cut_prefetch()

        budget = self._budget_for(d)
        prompt = self._user_prompt(d, budget)
        self.messages.append({"role": "user", "content": prompt})
        t0 = time.monotonic()

        # 一遍搞定：强制工具调用 + 思考预算。
        # 预算到点由引擎自己收尾，剩下的 token 留给"说一句话 + 调工具"。
        r1 = self.client.chat(self.messages, model=self.model, tools=d.tools(),
                              tool_choice="required", max_tokens=_room_for(budget),
                              reasoning_budget=budget, stream=True,
                              on_delta=self._tick)
        self._note_cache("吃叉" if d.kind == "react" else "打牌", r1)
        speech = r1.speech

        # 它这一手可能只想先记一笔临时笔记：写下去，再让它接着走这一手（记笔记不算一次动作）
        if (r1.tool_call or {}).get("name") == "edit_note":
            note_args = r1.tool_call.get("arguments") or {}
            done = self._apply_note(note_args)
            call_id = f"note_{len(self.messages)}"
            self.messages += [
                {"role": "assistant", "content": r1.speech,
                 **({"reasoning_content": r1.reasoning} if r1.reasoning else {}),
                 "tool_calls": [{"id": call_id, "type": "function",
                                 "function": {"name": "edit_note",
                                              "arguments": json.dumps(note_args, ensure_ascii=False)}}]},
                {"role": "tool", "tool_call_id": call_id, "content": done},
            ]
            r1 = self.client.chat(self.messages, model=self.model, tools=d.tools(),
                                  tool_choice="required", max_tokens=_room_for(self.budget_tiny),
                                  reasoning_budget=self.budget_tiny, stream=True,
                                  on_delta=self._tick)
            self._note_cache("记完接着打", r1)
            speech = speech or r1.speech

        # 它要中场暂停：这一手不打，把它的话原样带回牌桌，由外面接暂停流程
        # 同一个回合最多喊两次，防止它一直"我再想想"把牌局拖死
        if (r1.tool_call and r1.tool_call.get("name") == "pause"
                and self._pause_calls < 2):
            self._pause_calls += 1
            why = str((r1.tool_call.get("arguments") or {}).get("why") or "").strip()
            said = speech or why or "（停一下）"
            call_id = f"pause_{len(self.messages)}"
            self.messages.append({
                "role": "assistant", "content": said,
                **({"reasoning_content": r1.reasoning} if r1.reasoning else {}),
                "tool_calls": [{"id": call_id, "type": "function", "function": {
                    "name": "pause",
                    "arguments": json.dumps(r1.tool_call.get("arguments") or {}, ensure_ascii=False)}}]})
            self.messages.append({"role": "tool", "tool_call_id": call_id,
                                  "content": "已暂停，牌先别动，等他说话。"})
            return None, said

        action = self._action_from_call(r1.tool_call, d.legal)
        source = "tool" if action else ""

        # 万一没给出调用：把"它自己说的话"交给 nothink，只让它把这句话变成调用。
        # 不带这句话，nothink 看不出结论，会随手挑一个——实测它说过"不碰，过"，却挑了个碰。
        # 万一没给出调用：顺着这条对话再问一句，把它自己的话变成调用。
        # 不另起会话——前缀全在原处，只 prefill 新加的这句；另起会冷跑整段。
        r2 = None
        if action is None and time.monotonic() - t0 < self.think_seconds - 6:
            said = speech.strip()
            tail = (f"你刚才说的话：『{said}』。把它变成工具调用；话里说不碰、不要、过，就调用 pass。"
                    if said else "把上面的结论变成工具调用。")
            msgs2 = self.messages + [{"role": "user", "content": tail + " 不要说话，不要解释。"}]
            r2 = self.client.chat(msgs2, model=self.model, tools=d.tools(),
                                  tool_choice="required",
                                  max_tokens=_room_for(self.budget_tiny),
                                  reasoning_budget=self.budget_tiny)
            self._note_cache("补刀", r2)
            action = self._action_from_call(r2.tool_call, d.legal)
            if action:
                source = "tool2"

        # 还不行就拣它说的话；再不行才兜底（兜底一定合法）
        used_fallback = False
        if action is None:
            cand = actions_from_text(speech, d.legal)
            if len(cand) == 1:
                action, source = cand[0], "text"
            else:
                action, source = self._pick_fallback(d.legal), "fallback"
                used_fallback = True

        # 复读闸：和上一手一模一样的话不再摆到牌桌上（牌桌本来就有动作记录）
        said = speech.strip()
        if said in _FILLERS:                  # 老存档里留下的占位话，一句都别再念
            said, speech = "", ""
        if said and said == self._last_say:
            speech = ""
        if said:
            self._last_say = said

        rec = TurnRecord(len(self.records), speech, action, used_fallback, source=source,
                         reasoning=r1.reasoning, seconds=r1.seconds, tokens=r1.tokens,
                         finish=r1.finish)
        self.records.append(rec)
        self._pause_calls = 0                  # 这手落了子，暂停额度重新给
        self._remember(action, speech, rec.reasoning, from_tool=source in ("tool", "tool2"))
        self._log(d, prompt, r1, r2, action, used_fallback, source)
        return action, speech

    @staticmethod
    def _call_args(a: Action) -> dict:
        """按引擎真正执行的那个动作还原参数——回执不能拿模型的原话冒充。"""
        if a.kind == "chi":
            return {"combo": "+".join(tile_id(t) for t in a.combo)}
        if a.tile is not None:
            return {"tile": tile_id(a.tile)}
        return {}

    def _remember(self, action: Action, speech: str, reasoning: str, *, from_tool: bool) -> None:
        """按 OpenAI 标准形态记一轮：assistant 带 tool_calls，紧跟 role=tool 的回执。

        它自己没调工具（文本拣选/兜底）时不编造调用，只记它说过的话。
        """
        if speech.strip():
            said = speech.strip()
        elif action.tile is not None:
            said = f"（{action.kind} {tile_name(action.tile)}）"
        else:
            said = f"（{action.kind}）"
        if not from_tool:
            self.messages.append({"role": "assistant", "content": said,
                                  "reasoning_content": reasoning})
            return
        args = self._call_args(action)
        call_id = f"call_{len(self.records)}"
        self.messages.append({
            "role": "assistant", "content": said, "reasoning_content": reasoning,
            "tool_calls": [{"id": call_id, "type": "function",
                            "function": {"name": action.kind,
                                         "arguments": json.dumps(args, ensure_ascii=False)}}],
        })
        done = f"已执行：{action.kind} {args.get('tile') or args.get('combo') or ''}".strip()
        self.messages.append({"role": "tool", "tool_call_id": call_id, "content": done})

    def _action_from_call(self, tc: dict | None, legal: Sequence[Action]) -> Action | None:
        if not tc:
            return None
        name, args = tc.get("name"), tc.get("arguments") or {}
        # 吃只认那两张手牌：被吃的那张由牌桌定，工具里根本没有这个字段
        if name == "chi":
            combo = tuple(sorted(parse_tile(x) for x in str(args.get("combo", "")).split("+") if x))
            for cand in legal:
                if cand.kind == "chi" and tuple(sorted(cand.combo)) == combo:
                    return cand
            return None
        try:
            a = self._parse_call(name, args)
        except Exception:
            return None
        for cand in legal:
            if cand.kind == a.kind and cand.tile == a.tile and tuple(cand.combo) == tuple(a.combo):
                return cand
        return None

    def _parse_call(self, name: str | None, args: dict) -> Action:
        if name in ("discard", "pon", "kan"):
            return Action(name, tile=parse_tile(args.get("tile")))
        if name == "win":
            return Action("win")
        if name == "pass":
            return Action("pass")
        if name == "ting":
            return Action("ting")
        if name == "bloom":
            return Action("bloom")
        raise ValueError(f"不认识的动作 {name}")

    @staticmethod
    def _pick_fallback(legal: Sequence[Action]) -> Action:
        for a in legal:
            if a.kind == "draw":
                return a
        for a in legal:               # 报听阶段兜底也报：不报等于自断胡路
            if a.kind == "ting":
                return a
        for a in legal:
            if a.kind == "pass":
                return a
        for a in legal:
            if a.kind == "discard":
                return a
        return legal[0]

    # ---- 说话：中场暂停 / 散场闲聊共用 ----
    def chat(self, text: str, *, budget: int | None = None, phase: str = "after") -> str:
        """跟它说话。phase=pause 局中暂停，between 局间，after 整场打完。

        接在同一段历史后面发，工具表整局一张，前缀缓存照旧命中。
        预算按场景分：终场要改长期笔记，给 budget_final，放开让它随便改；
        中场/局间只给 budget_chat，够记必要信息就行。
        报听后的低预算只管出牌，聊天不吃那一档；要它别磨蹭，靠的是点暂停那一刻
        先把还在飞的自由思考掐掉，不是砍预算。
        """
        self._phase = phase
        if budget is None:
            budget = self.budget_final if phase == "after" else self.budget_chat
        # 有人开口之前，先把还在飞的自由思考掐掉：不然这条聊天排在它后面，
        # 你得对着"牌先停着"等它把那手空想完。
        self.cut_prefetch()
        if phase == "pause":
            head = ("【中场暂停】牌先停在这儿，不急着出。随便聊，想记就记临时笔记"
                    "（edit_note 用 target=temp）；长期笔记也能追加，用 target=long，"
                    "先记在底部草稿里，整场打完再并进长期笔记。\n")
        elif phase == "between":
            head = ("【局间闲聊】这一局打完了，下一局还没开。随便聊，可以聊刚才那手。"
                    "现在只能追加长期笔记（target=long，mode=append），草稿整场留着，"
                    "全部局打完才并档；要改笔记原文等最后一场打完。\n")
        else:
            head = ("【终场聊天】这一场到这儿了，现在是闲聊时间。就照你自己的性子说话，"
                    "可以聊刚才那几手，也可以聊别的。现在可以改长期笔记：追加或改原文都行，"
                    "改完下次开桌的顶部生效。\n")
        if phase == "after":
            self.finish_session()          # 整场结束才并档；本场顶部 system 仍不动
        # 聊天也把这一局的临时笔记带上：局中聊完接着打，散场聊完才好把经验写进长期笔记
        soft = self._read_soft_notes()
        if soft:
            head += f"\n【本局临时笔记】\n{soft}\n"
        draft = self._draft_block()
        if draft:
            head += f"\n{draft}\n"
        if phase == "between":
            head += "\n（这一局的经验要留，就追加长期笔记：target=long；草稿整场有效，全部打完才并档。）\n"
        elif phase == "after":
            head += "\n（这一场的经验要留，就追加或改长期笔记：target=long；改完下次开桌顶部生效。临时笔记下一局会清空。）\n"
        # 换场景或新一局的第一句，再把当前笔记和规矩递一遍；同一场里后面的话不重复贴
        fresh = (not self._chatted) or phase != self._last_chat_phase
        said = (head + text) if fresh else text
        self._chatted = True
        self._last_chat_phase = phase
        self.messages.append({"role": "user", "content": said})
        try:
            r = self._chat_call(budget)
        except Exception as e:                                  # 聊不上就算了，别把 TUI 带崩
            self.messages.pop()
            return f"（没接上话：{e}）"
        if r.tool_call and r.tool_call.get("name") == "edit_note":
            # 笔记是它的，它想记就记：拦住只会让它以为引擎坏了（"你再试试"也照样被拒）。
            # 局中能动的只有临时笔记和长期笔记草稿，改原文要等整场打完，这条界线由 _apply_note 把。
            done = self._apply_note(r.tool_call.get("arguments") or {})
            call_id = f"note_{len(self.messages)}"
            self.messages.append({
                "role": "assistant", "content": r.speech,
                **({"reasoning_content": r.reasoning} if r.reasoning else {}),
                "tool_calls": [{"id": call_id, "type": "function", "function": {
                    "name": "edit_note",
                    "arguments": json.dumps(r.tool_call.get("arguments") or {}, ensure_ascii=False)}}]})
            self.messages.append({"role": "tool", "tool_call_id": call_id, "content": done})
            try:
                r = self._chat_call(budget)                     # 让它知道改完了，再接着说
            except Exception:
                return f"（{done}）"
        reply = r.speech
        if not reply and r.reasoning:
            reply = r.reasoning.strip().splitlines()[-1]        # 话没说、只想了一通：拿最后一行顶上
        self.messages.append({"role": "assistant", "content": reply or "……",
                              "reasoning_content": r.reasoning or ""})
        return reply

    def set_phase(self, phase: str) -> None:
        """play / pause / after：决定笔记权限，也决定下一句话怎么开场。"""
        self._phase = phase

    def _chat_call(self, budget: int) -> ChatResult:
        r = self.client.chat(self.messages, model=self.model,
                             tools=TOOLS, tool_choice="auto",
                             max_tokens=_room_for(budget), reasoning_budget=budget)
        self._note_cache("暂停" if self._phase == "pause" else "闲聊", r)
        return r

    def reset_temp_notes(self) -> None:
        """新一局开始：只清临时笔记；整场草稿留着，全部打完才并档。"""
        try:
            self.temp_notes_path.write_text("", encoding="utf-8")
        except OSError:
            pass
        self._chatted = False
        self._last_chat_phase = None

    def finish_session(self) -> None:
        """整场打完：把草稿并进长期笔记文件（下次开桌的顶部才带上）。"""
        self._flush_long_draft()

    def _load_long_draft(self) -> None:
        """草稿也要能恢复：开桌时从草稿文件读回来。"""
        try:
            lines = self.draft_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        self._long_draft = [ln.strip() for ln in lines if ln.strip()]

    def _save_long_draft(self) -> None:
        try:
            if not self._long_draft:
                self.draft_path.unlink(missing_ok=True)
                return
            self.draft_path.write_text("\n".join(self._long_draft) + "\n", encoding="utf-8")
        except OSError:
            pass

    def _draft_block(self) -> str:
        """长期笔记追加草稿：只贴在底部，不动顶部 system。"""
        if not self._long_draft:
            return ""
        if self._long_draft_in_notes:
            head = "【长期笔记追加草稿（已记入长期笔记，下次开桌顶部生效）】"
        else:
            head = "【长期笔记追加草稿（整场暂存，全部打完再并入长期笔记）】"
        return head + "\n" + "\n".join(self._long_draft)

    def _flush_long_draft(self) -> None:
        """把草稿真正追加到长期笔记文件里；重复调用不会写两遍。"""
        if not self._long_draft or self._long_draft_in_notes:
            return
        try:
            body = ""
            try:
                body = self.notes_path.read_text(encoding="utf-8").rstrip()
            except FileNotFoundError:
                pass
            body = "\n".join(x for x in (body, *self._long_draft) if x)
            self.notes_path.write_text(body[:4000].rstrip() + "\n", encoding="utf-8")
            self._long_draft_in_notes = True
            try:                                  # 并档后草稿文件清空，重启不会再并一遍
                self.draft_path.unlink(missing_ok=True)
            except OSError:
                pass
        except OSError:
            pass

    def _add_long_draft(self, new: str) -> None:
        if self._long_draft_in_notes:        # 上一批已经并档了，从现在起只记新的
            self._long_draft.clear()
            self._long_draft_in_notes = False
        fresh = []
        for ln in new.splitlines():
            ln = ln.strip()
            if ln:
                fresh.append(ln if ln[:1] in "-*#" else "- " + ln)
        self._long_draft.extend(fresh)
        self._save_long_draft()

    def _apply_note(self, args: dict) -> str:
        """edit_note 落地：append 追加 / replace 定点换一段 / replace_all 全换。

        学的是编辑器那一套：replace 要求 old_string 在笔记里唯一出现，
        找不到就不动、出现多次也不动（免得一刀切错地方）。
        只写文件，历史里的 system 不动，所以下一局才生效；
        这次改完的样子放在回执里给它看，免得它记的还是旧的。
        """
        mode = str(args.get("mode") or "").strip().lower()
        target = str(args.get("target") or "").strip().lower()
        old = str(args.get("old_string") or "")
        new = str(args.get("new_string") or args.get("text") or "")
        if not mode:
            mode = "replace" if old else "append"
        # 长期笔记：整场先记进底部草稿，不动顶部 system；全部局打完才直接落盘
        if not target:
            target = "temp" if self._phase != "after" else "long"
        if target == "long" and self._phase != "after":
            if mode != "append":
                return "局中只能在草稿里追加长期笔记（mode=append），改原文等全部局打完再动"
            if not new.strip():
                return "没写内容，草稿没动"
            self._add_long_draft(new)
            return "长期笔记追加草稿记在牌桌底部了，整场打完再并进长期笔记。现在是：\n" + "\n".join(self._long_draft)
        if target == "long":
            # 散场：直接改长期笔记文件；追加的同时留在草稿块里给它看一眼
            try:
                body = ""
                try:
                    body = self.notes_path.read_text(encoding="utf-8").rstrip()
                except FileNotFoundError:
                    pass
                if mode == "append":
                    if not new.strip():
                        return "没写内容，笔记没动"
                    self._add_long_draft(new)
                    self._flush_long_draft()
                    body = self.notes_path.read_text(encoding="utf-8").rstrip()
                elif mode in ("replace", "replace_all"):
                    if not old:
                        return "replace 得给我 old_string（要改的那段原文）"
                    n = body.count(old)
                    if n == 0:
                        return f"笔记里找不到这段原文，没动：{old[:80]}"
                    if n > 1 and mode != "replace_all":
                        return f"这段在笔记里出现 {n} 次，不唯一，没动（要全改就用 replace_all）"
                    body = body.replace(old, new) if mode == "replace_all" else body.replace(old, new, 1)
                    self.notes_path.write_text(body[:4000].rstrip() + "\n", encoding="utf-8")
                else:
                    return f"不认识的做法 {mode}"
                return "长期笔记改好了（下次开桌顶部生效）。现在是：\n" + body[-1200:]
            except OSError as e:
                return f"没写成：{e}"

        path = self.temp_notes_path
        if path is None:
            return "临时笔记没接，记不了"
        try:
            body = ""
            try:
                body = path.read_text(encoding="utf-8").rstrip()
            except FileNotFoundError:
                pass

            if mode == "append":
                if not new.strip():
                    return "没写内容，笔记没动"
                # 它有时一口气发一整段（每行自己就带 - ），别再加一个 - 变成 "- - xxx"
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
                    return f"笔记里找不到这段原文，没动：{old[:80]}"
                if n > 1 and mode != "replace_all":
                    return f"这段在笔记里出现 {n} 次，不唯一，没动（要全改就用 replace_all）"
                body = body.replace(old, new) if mode == "replace_all" else body.replace(old, new, 1)
            else:
                return f"不认识的做法 {mode}"

            path.write_text(body[:4000].rstrip() + "\n", encoding="utf-8")
            return "临时笔记改好了，这局就看得见。现在是：\n" + body[-1200:]
        except OSError as e:
            return f"没写成：{e}"

    # ---- 空闲思考 ----
    def prefetch(self, view: dict, *, max_tokens: int = 4096) -> str:
        """别人回合时跑一遍：只让它想本手怎么打，不催它出牌。

        这一趟**不设思考预算**——预算只在真要落子那一手才用。
        自由时间它想多久都行，只留一个输出上限当刹车，免得跑飞了堵住下一手。
        同一时刻只准有一个预跑在飞：只有一个模型服务，抢不到并发。
        """
        with self._lock:
            if self._prefetching:
                return ""
            self._prefetching = True
        try:
            parts: list[str] = ["【自由思考时间】现在是别人的回合，你不用出牌。有一整段空时间，"
                                "想多深都行，先把这手牌想透。"]
            parts += ["【牌桌】", self._describe(view),
                      "想完了用三行写下来：目标形 / 浮牌 / 目标听牌。",
                      "这趟只是空想，不要调用任何工具。"]
            prompt = "\n".join(parts)
            try:
                # 接在整段对话后面想：之前的牌桌、它说过的话、它想过的思维链都在前缀里，
                # 前缀缓存也命中。另起一条只会从头冷跑。
                msgs = self.messages + [{"role": "user", "content": prompt}]
                r = self.client.chat(msgs, model=self.model,
                                     tools=TOOLS, tool_choice="auto",   # 和出牌那一趟的工具表一模一样，前缀才对得上
                                     max_tokens=max_tokens,                       # 不传 reasoning_budget
                                     stream=True,                                 # 能一边想一边被掐断
                                     on_response=self._hold)
                self._note_cache("自由思考", r)
                plan = "" if r.aborted else r.speech
                # 被掐断时它已经想出来的那部分不扔，下一趟接着想
                leftover = ((r.reasoning or "") + "\n" + (r.content or "")).strip()
            except Exception:
                plan, leftover = "", ""
            with self._lock:
                # 这次空闲思考本身要落在历史里：下一手它就是"我说过/我想过的话"，
                # 不落历史等于白想。被掐断的半截也留在 reasoning_content 里，
                # 下一趟它一抬头就看见，不用再把半截贴进 prompt。
                # 问和答成对写：只写答会出现"没有问题的 assistant"，下一手就成了
                # 「assistant → user」的错序，模型和缓存都别扭。
                if plan or leftover:
                    self.messages.append({"role": "user", "content": prompt})
                    # 结论留在 content，半截思路留在 reasoning_content。
                    # 空想没结论时 content 留空——早先这里塞了句「（还没想完）」的占位话，
                    # 模型拿它当自己上一句台词，下一手照着念，牌桌上就整排复读。
                    self.messages.append({"role": "assistant",
                                          "content": plan.strip(),
                                          **({"reasoning_content": leftover} if leftover else {})})
            return plan
        finally:
            with self._lock:
                self._prefetching = False
                self._resp = None

    def prefetch_async(self, view: dict) -> threading.Thread:
        t = threading.Thread(target=self.prefetch, args=(view,), daemon=True)
        with self._lock:
            self._prefetch_thread = t
        t.start()
        return t

    def _hold(self, resp) -> None:
        with self._lock:
            self._resp = resp

    def cut_prefetch(self) -> None:
        """掐断还在飞的自由思考，把模型通道立刻让给出牌。

        关掉这条连接，读的那头会立刻抛错，服务端也能看到断连、停掉这次生成。
        """
        with self._lock:
            resp, thread = self._resp, self._prefetch_thread
            self._resp = None
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)     # 等它把 _prefetching 放掉，别和出牌那次请求叠在一起

    # ---- 缓存命中 ----
    def _note_cache(self, label: str, r: ChatResult) -> None:
        row = {"label": label, "prompt": r.prompt_tokens, "cached": r.cached_tokens}
        self.cache_stats.append(row)
        if self.debug_cache and r.prompt_tokens:
            flag = ""
            if r.cached_tokens == 0 and r.prompt_tokens > 200:
                flag = "   ← 一个都没复用：前缀被改了？"
            print(f"  缓存 {label} 复用 {r.cached_tokens} 新读 {r.read_tokens}"
                  f"（prompt {r.prompt_tokens}）{flag}", flush=True)

    def cache_report(self) -> str:
        """看总账：复用了多少、真正重读了多少。前缀干净的话，新读只该是这一手的增量。"""
        if not self.cache_stats:
            return "没有缓存数据"
        prompt = sum(x["prompt"] for x in self.cache_stats)
        cached = sum(x["cached"] for x in self.cache_stats)
        lines = [f"缓存 {len(self.cache_stats)} 次请求：复用 {cached}，新读 {prompt - cached}"]
        for x in self.cache_stats:
            if x["cached"] == 0 and x["prompt"] > 200:
                lines.append(f"  一次没复用：{x['label']} prompt {x['prompt']}"
                             "（前缀被改了，或这一手是第一条）")
        return "\n".join(lines)

    # ---- 日志 ----
    def _log(self, d: Decision, prompt: str, r1: ChatResult, r2: ChatResult | None,
             action: Action, fallback: bool, source: str) -> None:
        if not self.log_path:
            return
        row = {
            "t": time.strftime("%Y-%m-%d %H:%M:%S"),
            "seat": self.seat,
            "kind": d.kind,
            "prompt_chars": len(prompt),
            "prompt_tokens": r1.prompt_tokens,
            "cached_tokens": r1.cached_tokens,
            "hit_rate": round(r1.hit_rate, 3),
            "seconds": r1.seconds,
            "seconds_pass2": r2.seconds if r2 else 0.0,
            "tokens": r1.tokens,
            "finish": r1.finish,
            "speech": r1.speech[:400],
            "reasoning_tail": (r1.reasoning or "")[-300:],
            "tool_call": r1.tool_call,
            "source": source,
            "action": action.to_dict(),
            "fallback": fallback,
        }
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- 接进牌局

def play_with_llm(
    game: Game,
    player: LLMPlayer,
    *,
    on_event: Callable[[list[dict]], None] | None = None,
    prefetch_when_idle: bool = False,
) -> dict:
    """让 LLM 坐 player.seat，其余座位用随机。UI 以后把 on_event 换成渲染。

    prefetch_when_idle：随机对手是瞬时的，一局里几乎没有"空闲"，
    预跑只会跟真正的出牌抢同一个模型服务。等你真人上桌、每手要想半分钟时再打开。
    """
    import random
    from game import random_policy

    bots = {s: random_policy(random.Random(200 + s)) for s in range(game.cfg.seats) if s != player.seat}
    steps = 0
    while game.pending() is not None:
        d = game.pending()
        if d.seat == player.seat:
            act, speech = player.decide(d)
        else:
            act = bots[d.seat](d)
            if prefetch_when_idle:
                player.prefetch_async(game.observe(player.seat))
        events = game.apply(act)
        if on_event:
            on_event([e.to_dict() for e in events])
        steps += 1
        if steps > 5000:
            raise RuntimeError("疑似死循环")
    return game.result or {"type": "unknown"}


# ---------------------------------------------------------------- 自测

class _FakeClient(StrataClient):
    """离线测试用：按脚本回话，不联网。"""

    def __init__(self, script: list[ChatResult]) -> None:
        self.script = script
        self.calls: list[dict] = []

    def chat(self, messages, **kw):  # type: ignore[override]
        self.calls.append({"messages": [dict(m) for m in messages], **kw})   # 存快照，别被后续追加带跑
        return self.script.pop(0) if self.script else ChatResult()


def _selftest() -> int:
    fails = 0

    def case(name, got, want):
        nonlocal fails
        ok = got == want
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
        if not ok:
            fails += 1
            print(f"      期望 {want}，实际 {got}")

    from game import GameConfig

    # 造一个"轮到你出牌"的 Decision
    g = Game(GameConfig(seats=3, seed=11, dealer=0))
    g.start()
    d_draw = g.pending()   # 0 号摸牌，只有一个选项
    g.apply(Action("draw"))
    d = g.pending()
    case("构造出牌决策", (d.seat, d.kind), (0, "discard"))

    legal = d.legal
    probe = next(a for a in legal if a.kind == "discard")
    pid = tile_id(probe.tile)
    hand_ids = set(d.view["hand"])
    notin = next(tile_id(t) for t in range(KINDS) if tile_id(t) not in hand_ids)

    # 0 只有一个选项（摸牌）→ 不花那二十秒
    fc0 = _FakeClient([])
    act0, _ = LLMPlayer(fc0, 0, log_path=None).decide(d_draw)
    case("单选项直接执行", (act0.kind, len(fc0.calls)), ("draw", 0))

    # 1 工具调用命中
    fc = _FakeClient([ChatResult(content=f"我打 {pid}。", tool_call={"name": "discard", "arguments": {"tile": pid}})])
    p = LLMPlayer(fc, 0, log_path=None)
    act, speech = p.decide(d)
    case("工具调用直接命中", (act.kind, tile_id(act.tile)), ("discard", pid))
    case("说话被保留", speech, f"我打 {pid}。")
    case("第一遍就是强制调用 + 带了思考预算", (fc.calls[0]["tool_choice"], fc.calls[0]["reasoning_budget"]), ("required", 3000))
    case("打牌上限 = 预算 + 引擎要留的答案空间", fc.calls[0]["max_tokens"], 4000)
    case("用的是 lowthink", fc.calls[0]["model"], "strata-lowthink")

    # 1b 吃叉只给 1000
    t0id = d.view["hand"][0]
    react = Decision(seat=1, kind="react", view=d.view, legal=[
        Action("pass"), Action("pon", tile=parse_tile(t0id)), Action("win")])
    fcR = _FakeClient([ChatResult(content="碰一个。", tool_call={"name": "pon", "arguments": {"tile": t0id}})])
    pR = LLMPlayer(fcR, 1, log_path=None)
    actR, _ = pR.decide(react)
    case("吃叉预算 1000", fcR.calls[0]["reasoning_budget"], 1000)
    case("吃叉上限 = 1000 + 512", fcR.calls[0]["max_tokens"], 1512)
    case("吃叉也说得上话", actR.kind, "pon")
    case("吃叉提醒写的是 1000", any("思考预算 1000 token" in (m.get("content") or "") for m in pR.messages), True)

    # 1c 吃：合法动作带被吃的那张，工具只给 combo —— 只按 combo 认
    chi_legal = [Action("pass"),
                 Action("chi", tile=parse_tile("s5"), combo=(parse_tile("s4"), parse_tile("s6")))]
    dchi = Decision(seat=0, kind="react", view=d.view, legal=chi_legal)
    fcC = _FakeClient([ChatResult(content="吃了就听牌。",
                                  tool_call={"name": "chi", "arguments": {"combo": "s4+s6"}})])
    pC = LLMPlayer(fcC, 0, log_path=None)
    actC, _ = pC.decide(dchi)
    case("吃按 combo 认得上", actC.kind, "chi")
    case("回执写的是真执行的动作", pC.messages[-1]["content"], "已执行：chi s4+s6")
    case("调用参数取自执行的动作",
         json.loads(pC.messages[-2]["tool_calls"][0]["function"]["arguments"]), {"combo": "s4+s6"})

    # 2 不给工具调用 → 第二遍逼出来
    fc = _FakeClient([
        ChatResult(content=f"我想打 {pid}。"),
        ChatResult(content="", tool_call={"name": "discard", "arguments": {"tile": pid}}),
    ])
    p = LLMPlayer(fc, 0, log_path=None)
    act, _ = p.decide(d)
    case("第二遍逼出工具调用", (act.kind, tile_id(act.tile)), ("discard", pid))
    case("第二遍顺着同一段对话（前缀没变）",
         [m["content"] for m in fc.calls[1]["messages"][:len(fc.calls[0]["messages"])]]
         == [m["content"] for m in fc.calls[0]["messages"]], True)
    case("第二遍只多一句话", len(fc.calls[1]["messages"]) - len(fc.calls[0]["messages"]), 1)
    case("第二遍不给它重新想", (fc.calls[1]["model"], fc.calls[1]["reasoning_budget"]), ("strata-lowthink", 40))

    # 3 两遍都没工具 → 文本兜底
    fc = _FakeClient([ChatResult(content=f"我打 {pid}。"), ChatResult(content="")])
    p = LLMPlayer(fc, 0, log_path=None)
    act, _ = p.decide(d)
    case("文本兜底拣出那张", (act.kind, tile_id(act.tile)), ("discard", pid))
    case("兜底不算强制兜底", p.records[-1].used_fallback, False)

    # 4 它说了手里没有的牌 → 落到合法兜底
    fc = _FakeClient([ChatResult(content=f"我要打 {notin}。"), ChatResult(content="")])
    p = LLMPlayer(fc, 0, log_path=None)
    act, _ = p.decide(d)
    case("非法建议被丢弃", p.records[-1].used_fallback, True)
    case("兜底动作一定合法", any(a.kind == act.kind and a.tile == act.tile for a in legal), True)

    # 5 prompt 布局：笔记只在 system 里出现一次，user 只带局面和牌桌
    p2 = LLMPlayer(_FakeClient([ChatResult(content=f"打 {pid}")]), 0, log_path=None)
    prompt = p2._user_prompt(d, p2._budget_for(d))
    case("笔记进 system 只一次", p2.messages[0]["content"].count("【你的笔记】"), 1)
    case("开场就交代嘴上不交底", "嘴上不交底" in p2.messages[0]["content"], True)
    case("出牌那句也提醒别报手牌", "别报手牌" in prompt, True)
    case("user 里不再重复笔记", "【你的笔记】" in prompt, False)
    case("局面提醒在最上方", prompt.startswith("【轮到你出牌了】"), True)
    case("牌桌在提醒之后", prompt.index("【牌桌】") > prompt.index("【轮到你出牌了】"), True)
    case("预算写的是这一手的数", "思考预算 3000 token" in prompt, True)
    case("可选动作是枚举出来的", f"discard {pid}" in prompt, True)
    case("限制写在最下面的尾巴上", prompt.rstrip().split("\n")[-3].startswith("【本轮只能做这些】"), True)
    case("越界会被判无效", "调表外的动作会被判无效" in prompt, True)

    # 5b 四种局面各有各的话
    win_only = Decision(seat=1, kind="react", view=d.view,
                        legal=[Action("pass"), Action("win")])
    pon_only = Decision(seat=1, kind="react", view=d.view,
                        legal=[Action("pass"), Action("pon", tile=parse_tile(t0id))])
    case("能胡了", p2._headline(win_only), "【能胡了】")
    case("现在可以吃叉", p2._headline(pon_only), "【现在可以吃叉】")
    case("轮到你出牌了", p2._headline(d), "【轮到你出牌了】")

    # 5b-2 报听之后只给最低预算，除非这一手能胡/能杠
    tinged = dict(d.view)
    tinged["ting"] = {**{s: False for s in range(4)}, 0: True}
    tired = Decision(seat=0, kind="discard", view=tinged,
                     legal=[Action("discard", tile=parse_tile(notin)),
                            Action("discard", tile=parse_tile(pid))])
    case("报听之后打牌只给最低预算", p2._budget_for(tired), 200)
    kanning = Decision(seat=0, kind="discard", view=tinged,
                       legal=[Action("kan", tile=parse_tile(notin)), Action("discard", tile=parse_tile(notin))])
    case("报听之后能杠，预算给满", p2._budget_for(kanning), 3000)
    tingwin = Decision(seat=0, kind="react", view=tinged,
                       legal=[Action("win"), Action("pass")])
    case("报听之后能胡，预算给满", p2._budget_for(tingwin), 3000)
    case("没报听时该想的还想", p2._budget_for(d), 3000)

    # 5c 工具表必须一个字都不变：它渲染在提示词最前面，随牌变就等于整段前缀作废
    shape = lambda dd: json.dumps(dd.tools(), ensure_ascii=False, sort_keys=True)
    case("打牌和吃叉用的是同一张工具表", shape(d) == shape(pon_only), True)
    case("能胡和不能胡也是同一张", shape(win_only) == shape(d), True)
    case("工具表带齐整套动作", [t["function"]["name"] for t in d.tools()],
         ["discard", "pon", "kan", "chi", "win", "bloom", "pass", "ting", "edit_note", "pause"])
    case("打牌那几件 schema 里不塞牌名",
         any("enum" in json.dumps(t) for t in d.tools()[:6]), False)

    # 5d 散场闲聊：接在同一段历史后面，工具表照旧整张带着
    fcC = _FakeClient([ChatResult(content="你那张 s1 打早了。"),
                       ChatResult(content="随便聊，你平时打多久？")])
    pC = LLMPlayer(fcC, 0, log_path=None)
    reply1 = pC.chat("我刚才打得怎么样")
    case("第一句带开场说明", "【终场聊天】" in fcC.calls[0]["messages"][-1]["content"], True)
    case("闲聊也带整张工具表", len(fcC.calls[0]["tools"]), 10)
    case("闲聊不强制调用", fcC.calls[0]["tool_choice"], "auto")
    case("它说什么就显示什么", reply1, "你那张 s1 打早了。")
    case("一问一答都进历史", (pC.messages[-2]["role"], pC.messages[-1]["role"]), ("user", "assistant"))
    pC.chat("你平时打多久")
    case("第二句不再重复开场", "【终场聊天】" in fcC.calls[1]["messages"][-1]["content"], False)
    case("终场闲聊预算放开到 6000", fcC.calls[1]["reasoning_budget"], 6000)
    case("终场 max_tokens 跟着给足", fcC.calls[1]["max_tokens"], 8000)

    # 5d-2 报听的低预算只管出牌，散场改笔记用终场档
    fcT = _FakeClient([ChatResult(content="打这张", tool_call={"name": "discard", "arguments": {"tile": notin}}),
                       ChatResult(content="这局你打得太保守了。")])
    pT = LLMPlayer(fcT, 0, log_path=None)
    pT.decide(tired)
    case("出牌吃的是低预算", fcT.calls[0]["reasoning_budget"], 200)
    pT.chat("我打得怎么样")
    case("报听之后聊天预算恢复终场档", fcT.calls[-1]["reasoning_budget"], 6000)

    # 5d-3 暂停给够记事的预算，点暂停那一刻要先把自由思考掐断（见 play.py）
    fcP2 = _FakeClient([ChatResult(content="先停着，你说。")])
    pP2 = LLMPlayer(fcP2, 0, log_path=None)
    pP2.chat("停一下", phase="pause")
    case("暂停聊天只给够记事的预算", fcP2.calls[0]["reasoning_budget"], 1200)

    # 5e 打完能改笔记
    import tempfile
    note = Path(tempfile.gettempdir()) / "_mahjong_note.md"
    note.write_text("- 老的一条\n", encoding="utf-8")
    fcN = _FakeClient([
        ChatResult(tool_call={"name": "edit_note",
                              "arguments": {"mode": "append", "text": "p2 别急着打"}}),
        ChatResult(content="记下了，下局留神。")])
    pN = LLMPlayer(fcN, 0, log_path=None, notes_path=note)
    case("改完还会接着说一句", pN.chat("加一条：p2 别急着打"), "记下了，下局留神。")
    case("笔记真的落盘了", "p2 别急着打" in note.read_text(encoding="utf-8"), True)
    case("旧的那条没被冲掉", "老的一条" in note.read_text(encoding="utf-8"), True)
    case("调用带回了回执", any(m["role"] == "tool" for m in pN.messages), True)
    # 谁都没提"笔记"，它自己想记也得让它记：以前按字眼卡，硬试两次都被挡，像引擎坏了
    fcN2 = _FakeClient([ChatResult(tool_call={"name": "edit_note",
                                              "arguments": {"mode": "append", "text": "没人提也照记"}}),
                        ChatResult(content="记好了。")])
    pN2 = LLMPlayer(fcN2, 0, log_path=None, notes_path=note)
    pN2.chat("你再试试，不应该不让改啊")
    case("没人提笔记时它想记也照记", "没人提也照记" in note.read_text(encoding="utf-8"), True)
    case("回执不再是那句挡人的话",
         any("没人让你动笔记" in m["content"] for m in pN2.messages), False)
    case("整局就这一张工具表", len(TOOLS), 10)

    # 5f 随时能暂停、随时能记条临时笔记：这三处都得把它告诉到位
    case("出牌那手也告诉它能暂停", "pause" in p2._user_prompt(d, 3000), True)
    case("出牌那手也告诉它能记临时笔记", "edit_note" in p2._user_prompt(d, 3000), True)
    case("报听锁死那手不吹这两件", _ALWAYS in p2._actions_text(tired.legal, always=False), False)

    soft = Path(tempfile.gettempdir()) / "_mahjong_soft.md"
    soft.write_text("", encoding="utf-8")
    fcS = _FakeClient([
        ChatResult(content="记一笔", tool_call={"name": "edit_note",
                                               "arguments": {"mode": "append", "text": "p2 别急着打"}}),
        ChatResult(tool_call={"name": "discard", "arguments": {"tile": pid}})])
    pS = LLMPlayer(fcS, 0, log_path=None, soft_notes_path=soft)
    actS, _ = pS.decide(d)
    case("记完笔记照样把这一手打出来", (actS.kind, tile_id(actS.tile)), ("discard", pid))
    case("临时笔记落盘了", "p2 别急着打" in soft.read_text(encoding="utf-8"), True)
    case("记完再问一遍（共两次请求）", len(fcS.calls), 2)
    case("第二遍只给小预算", fcS.calls[1]["reasoning_budget"], pS.budget_tiny)
    tips = [m["content"] for m in pS.messages if m["role"] == "tool"]
    case("调用带回了回执", bool(tips and tips[-1]), True)

    # 5i 中场暂停：他喊停，这一手就不打
    fcP = _FakeClient([ChatResult(content="等一下，我想问一句。",
                                  tool_call={"name": "pause", "arguments": {"why": "问他为什么打 m9"}})])
    pP = LLMPlayer(fcP, 0, log_path=None)
    actP, sayP = pP.decide(d)
    case("暂停时不出牌", actP, None)
    case("它说的话带回来", sayP, "等一下，我想问一句。")
    case("暂停也落进历史", pP.messages[-1]["role"], "tool")

    # 5j 局中只许记临时笔记，长期笔记要等打完
    temp = Path(tempfile.gettempdir()) / "_mahjong_temp.md"
    temp.write_text("", encoding="utf-8")
    fcT = _FakeClient([
        ChatResult(tool_call={"name": "edit_note", "arguments": {
            "target": "temp", "mode": "append", "new_string": "他老打 m9，留意"}}),
        ChatResult(content="记下了")])
    pT = LLMPlayer(fcT, 0, log_path=None, soft_notes_path=temp)
    pT.chat("记一下：他老打 m9", phase="pause", budget=600)
    case("临时笔记写进去了", "他老打 m9" in temp.read_text(encoding="utf-8"), True)
    case("临时笔记贴到牌桌上", "【本局临时笔记】" in pT._user_prompt(d, 3000), True)

    fcC = _FakeClient([ChatResult(content="知道")])
    pC = LLMPlayer(fcC, 0, log_path=None, soft_notes_path=temp)
    pC.chat("中场说两句", phase="pause")
    case("中场聊天也带上临时笔记", "【本局临时笔记】" in fcC.calls[0]["messages"][-1]["content"], True)

    fcA = _FakeClient([ChatResult(content="记下了")])
    pA = LLMPlayer(fcA, 0, log_path=None, soft_notes_path=temp)
    pA.chat("散场聊两句", phase="after")
    case("散场聊天也带上临时笔记", "【本局临时笔记】" in fcA.calls[0]["messages"][-1]["content"], True)
    case("散场提醒写长期笔记", "target=long" in fcA.calls[0]["messages"][-1]["content"], True)

    noteB = Path(tempfile.gettempdir()) / "_mahjong_note_b.md"
    noteB.write_text("", encoding="utf-8")
    noteB.with_name(noteB.stem + ".草稿.md").unlink(missing_ok=True)
    fcB = _FakeClient([ChatResult(content="局间聊两句")])
    pB = LLMPlayer(fcB, 0, log_path=None, notes_path=noteB)
    pB.chat("局间随便聊", phase="between")
    case("局间有单独的开场", "【局间闲聊】" in fcB.calls[0]["messages"][-1]["content"], True)
    case("局间只让追加长期笔记", "mode=append" in fcB.calls[0]["messages"][-1]["content"], True)
    case("局间聊天也只给够记事的预算", fcB.calls[0]["reasoning_budget"], 1200)

    noteL = Path(tempfile.gettempdir()) / "_mahjong_note_long.md"
    noteL.write_text("", encoding="utf-8")
    draftL = noteL.with_name(noteL.stem + ".草稿.md")
    draftL.unlink(missing_ok=True)
    fcL = _FakeClient([
        ChatResult(tool_call={"name": "edit_note", "arguments": {
            "target": "long", "mode": "append", "new_string": "不该现在写"}}),
        ChatResult(content="先记草稿"),
        ChatResult(content="散场了")])
    pL = LLMPlayer(fcL, 0, log_path=None, notes_path=noteL)
    pL.chat("把笔记加一条", phase="pause")
    case("局中长笔记先记草稿", (noteL.read_text(encoding="utf-8").strip(), "不该现在写" in pL._draft_block()),
         ("", True))
    case("草稿贴在牌桌底部", "【长期笔记追加草稿" in pL._user_prompt(d, 3000), True)
    case("草稿落盘能恢复", "不该现在写" in draftL.read_text(encoding="utf-8"), True)
    pR = LLMPlayer(_FakeClient([ChatResult(content="接着打")]), 0, log_path=None, notes_path=noteL)
    case("重启后草稿还在", "不该现在写" in pR._draft_block(), True)
    pL.chat("打完聊两句", phase="after")
    case("散场并进长期笔记", "不该现在写" in noteL.read_text(encoding="utf-8"), True)
    case("并完草稿标已记入", "已记入长期笔记" in pL._draft_block(), True)
    case("并完草稿文件清掉", draftL.exists(), False)

    # 换局只清临时笔记，草稿整场留着；两者都要能恢复
    noteP = Path(tempfile.gettempdir()) / "_mahjong_note_p.md"
    noteP.write_text("", encoding="utf-8")
    tempP = Path(tempfile.gettempdir()) / "_mahjong_temp_p.md"
    tempP.write_text("- 本局临时记的\n", encoding="utf-8")
    draftP = noteP.with_name(noteP.stem + ".草稿.md")
    draftP.unlink(missing_ok=True)
    pP1 = LLMPlayer(_FakeClient([ChatResult(content="记着")]), 0, log_path=None,
                    notes_path=noteP, soft_notes_path=tempP)
    pP1._add_long_draft("整场攒的草稿")
    pP2 = LLMPlayer(_FakeClient([ChatResult(content="下一局")]), 0, log_path=None,
                    notes_path=noteP, soft_notes_path=tempP)
    case("重开后草稿能恢复", "整场攒的草稿" in pP2._draft_block(), True)
    case("重开后临时笔记能恢复", "本局临时记的" in pP2._user_prompt(d, 3000), True)
    pP2.reset_temp_notes()
    case("换局草稿不并档", "整场攒的草稿" in pP2._draft_block(), True)
    case("换局临时笔记清空", tempP.read_text(encoding="utf-8").strip(), "")
    pP2.finish_session()
    case("整场打完才并档", "整场攒的草稿" in noteP.read_text(encoding="utf-8"), True)

    # 5f 没让它改也可以自己记：以前按字眼卡，硬试两次都被挡，它会以为引擎坏了
    note2 = Path(tempfile.gettempdir()) / "_mahjong_note2.md"
    note2.write_text("- 原来的\n", encoding="utf-8")
    fcU = _FakeClient([
        ChatResult(tool_call={"name": "edit_note", "arguments": {"mode": "append", "text": "我顺手记一条"}}),
        ChatResult(content="说回牌，你那张 m9 留得对。")])
    pU = LLMPlayer(fcU, 0, log_path=None, notes_path=note2)
    case("没让它改也照样聊下去", pU.chat("刚才那手怎么样"), "说回牌，你那张 m9 留得对。")
    case("它想记就记，不再拦", "我顺手记一条" in note2.read_text(encoding="utf-8"), True)

    # 5g 多行笔记别被揉成一行
    note3 = Path(tempfile.gettempdir()) / "_mahjong_note3.md"
    note3.write_text("", encoding="utf-8")
    fcM = _FakeClient([
        ChatResult(tool_call={"name": "edit_note",
                              "arguments": {"mode": "append", "text": "- 一\n- 二"}}),
        ChatResult(content="好")])
    pM = LLMPlayer(fcM, 0, log_path=None, notes_path=note3)
    pM.chat("把笔记加两行")
    case("两行各占一行", note3.read_text(encoding="utf-8").split("- ")[1:],
         ["一\n", "二\n"])
    case("没有 - - 这种叠字", "- - " in note3.read_text(encoding="utf-8"), False)

    # 5h 定点改一段：像编辑器那样，old_string 必须唯一
    note4 = Path(tempfile.gettempdir()) / "_mahjong_note4.md"
    note4.write_text("- 起手先数对子\n- 幺九优先留\n", encoding="utf-8")
    fcE = _FakeClient([
        ChatResult(tool_call={"name": "edit_note", "arguments": {
            "mode": "replace", "old_string": "幺九优先留", "new_string": "幺九优先留，带搭子更金贵"}}),
        ChatResult(content="改好了")])
    pE = LLMPlayer(fcE, 0, log_path=None, notes_path=note4)
    pE.chat("把笔记里幺九那条改一下")
    case("定点改到了", "幺九优先留，带搭子更金贵" in note4.read_text(encoding="utf-8"), True)
    case("旁的行没被碰", note4.read_text(encoding="utf-8").splitlines()[0], "- 起手先数对子")

    note5 = Path(tempfile.gettempdir()) / "_mahjong_note5.md"
    note5.write_text("- 只有一条\n", encoding="utf-8")
    fcX = _FakeClient([
        ChatResult(tool_call={"name": "edit_note", "arguments": {
            "mode": "replace", "old_string": "压根没有的段落", "new_string": "x"}}),
        ChatResult(content="没找到那段")])
    pX = LLMPlayer(fcX, 0, log_path=None, notes_path=note5)
    pX.chat("把笔记改一下")
    case("找不到原文就不动", note5.read_text(encoding="utf-8").strip(), "- 只有一条")

    note6 = Path(tempfile.gettempdir()) / "_mahjong_note6.md"
    note6.write_text("- 重复\n- 重复\n", encoding="utf-8")
    fcD = _FakeClient([
        ChatResult(tool_call={"name": "edit_note", "arguments": {
            "mode": "replace", "old_string": "重复", "new_string": "改掉"}}),
        ChatResult(content="出现两次，没敢动")])
    pD = LLMPlayer(fcD, 0, log_path=None, notes_path=note6)
    pD.chat("把这行改掉")
    case("不唯一时也不动手", note6.read_text(encoding="utf-8").count("重复"), 2)

    # 6 空闲思考要落在历史里，不能白想
    view = d.view
    p3 = LLMPlayer(_FakeClient([ChatResult(content="目标形：四面子+红中将\n浮牌：s3\n目标听牌：m3")]),
                   0, log_path=None)
    p3.prefetch(view)
    last = p3.messages[-1]
    case("空闲思考进了历史", last["role"], "assistant")
    case("问和答成对写进去", [m["role"] for m in p3.messages[-2:]], ["user", "assistant"])
    case("它想好的三行成了它说过的话", last["content"].startswith("目标形"), True)

    # 7 自由思考不带思考预算（预算只给真要落子那一手）
    fc4 = _FakeClient([ChatResult(content="目标形：四面子一将")])
    p4 = LLMPlayer(fc4, 0, log_path=None)
    p4.prefetch(view)
    case("预跑没带 reasoning_budget", "reasoning_budget" in fc4.calls[0], False)
    case("预跑的刹车只是输出上限", fc4.calls[0]["max_tokens"], 4096)
    case("预跑带着整段对话", [m["role"] for m in fc4.calls[0]["messages"]], ["system", "user"])
    case("预跑的问题在最后一句", "【自由思考时间】" in fc4.calls[0]["messages"][-1]["content"], True)

    # 8 轮到它出牌：先掐断自由思考，再进打牌思考
    class _Sock:
        closed = False

        def close(self):
            self.closed = True

    sock = _Sock()
    fc5 = _FakeClient([ChatResult(content="打这张", tool_call={"name": "discard", "arguments": {"tile": pid}})])
    p5 = LLMPlayer(fc5, 0, log_path=None)
    p5._resp = sock
    act5, _ = p5.decide(d)
    case("出牌前掐断自由思考", sock.closed, True)
    case("掐断后照常进打牌思考", len(fc5.calls), 1)

    sock2 = _Sock()
    p6 = LLMPlayer(_FakeClient([]), 0, log_path=None)
    p6._resp = sock2
    p6.decide(d_draw)
    case("摸牌不掐自由思考（留给紧接着的出牌）", sock2.closed, False)

    # 8b 你点暂停：先把还在飞的空闲思考掐掉，别让你对着"牌先停着"等它想完
    sock3 = _Sock()
    fcQ = _FakeClient([ChatResult(content="先停着，你说。")])
    pQ = LLMPlayer(fcQ, 0, log_path=None)
    pQ._resp = sock3
    pQ.chat("停一下", phase="pause")
    case("一开口就掐断空闲思考", sock3.closed, True)

    # 9 被打断的半截不看丢：留在历史里，下一趟它自己看得见
    fc7 = _FakeClient([ChatResult(content="", reasoning="条子这边还差一张就成", aborted=True)])
    p7 = LLMPlayer(fc7, 0, log_path=None)
    case("被打断的不算计划", p7.prefetch(view), "")
    case("半截留在历史里", "条子这边" in (p7.messages[-1].get("reasoning_content") or ""), True)
    p7.prefetch(view)
    case("下一趟不再另贴半截", "上次没想完" in fc7.calls[-1]["messages"][-1]["content"], False)

    # 10 思维链全留在历史里：只追加、不改旧消息，前缀缓存才命中得了
    fc8 = _FakeClient([ChatResult(content=f"第{i}手", reasoning=f"想{i}", tool_call={"name": "discard", "arguments": {"tile": pid}})
                       for i in range(6)])
    p8 = LLMPlayer(fc8, 0, log_path=None)
    for _ in range(5):
        p8.decide(d)
    case("五手的思维链都还在", len([m for m in p8.messages if m.get("reasoning_content")]), 5)
    case("每手一条 assistant + 一条 tool", len(p8.messages), 1 + 5 * 3)
    last_a = [m for m in p8.messages if m["role"] == "assistant"][-1]
    case("assistant 带着 tool_calls", bool(last_a.get("tool_calls")), True)
    case("回执跟在调用后面", p8.messages[-1]["role"], "tool")
    case("回执的 id 对得上", p8.messages[-1]["tool_call_id"], last_a["tool_calls"][0]["id"])

    # 11 缓存命中率：正常该接近 100%，低了就是前缀被动过
    case("命中率算得对", ChatResult(prompt_tokens=1000, cached_tokens=990).hit_rate, 0.99)
    case("Strata 的 usage 能解析",
         _usage_of({"prompt_tokens": 1200, "prompt_tokens_details": {"cached_tokens": 1180}}), (1200, 1180))
    fc9 = _FakeClient([ChatResult(prompt_tokens=900, cached_tokens=890,
                                  tool_call={"name": "discard", "arguments": {"tile": pid}})])
    p9 = LLMPlayer(fc9, 0, log_path=None)
    p9.decide(d)
    case("请求记进了缓存表", p9.cache_stats[0]["label"], "打牌")
    case("报表给出总账", "复用 890，新读 10" in p9.cache_report(), True)
    case("没复用才点名", "一次没复用" in p9.cache_report(), False)

    print()
    print(f"共 {fails} 条失败")
    return fails


if __name__ == "__main__":
    raise SystemExit(_selftest())
