"""玩家档案目录：一个玩家一个文件夹，目录名就是玩家名。

profile.json 放姓名和这一座 LLM 的接入配置；完整人设单独写在 性格档案.md，
由 persona 读进来拼进系统提示词。笔记、日志、战绩也都放在同一个目录里。
座位是牌桌临时分配，不写进档案；没指定座位时由牌桌随机安排。

profile.json 的 llm 段照 llm.config.json 的写法：
    "llm": {
      "baseUrl": "...",
      "apiKey": "...",
      "reasoningBudget": true,
      "headers": {...},
      "request": {"model": "...", "temperature": 0.0, "max_tokens": 8192}
    }
老档案里顶层的 model 仍然兼容。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PROFILE_ROOT = HERE / "profiles"
PROFILE_FILE = "profile.json"
PERSONA_FILE = "性格档案.md"
DEFAULT_LLM_URL = "http://127.0.0.1:11436/v1/chat/completions"
DEFAULT_MODEL = "strata-lowthink"


@dataclass(frozen=True)
class PlayerProfile:
    name: str
    root: Path
    meta: dict[str, Any]

    @property
    def display_name(self) -> str:
        return str(self.meta.get("display_name") or self.name)

    @property
    def kind(self) -> str:
        return str(self.meta.get("kind") or "llm")

    @property
    def llm(self) -> dict[str, Any]:
        """这一座 LLM 的接入配置；没有新配置时返回空字典。"""
        raw = self.meta.get("llm")
        return raw if isinstance(raw, dict) else {}

    @property
    def base_url(self) -> str:
        return str(self.llm.get("baseUrl") or self.llm.get("base_url") or DEFAULT_LLM_URL)

    @property
    def api_key(self) -> str:
        return str(self.llm.get("apiKey") or self.llm.get("api_key") or "")

    @property
    def headers(self) -> dict[str, str]:
        raw = self.llm.get("headers")
        return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}

    @property
    def request(self) -> dict[str, Any]:
        raw = self.llm.get("request")
        return dict(raw) if isinstance(raw, dict) else {}

    @property
    def reasoning_budget(self) -> bool:
        """是否给这一座发 Strata 的 reasoning_budget_tokens；云端接口一般关掉。"""
        raw = self.llm.get("reasoningBudget", self.llm.get("reasoning_budget"))
        if isinstance(raw, str):
            return raw.strip().lower() not in ("0", "false", "no", "off")
        return True if raw is None else bool(raw)

    @property
    def model(self) -> str:
        # 新写法在 llm.request.model；老档案仍可读顶层 model
        return str(self.request.get("model") or self.meta.get("model") or DEFAULT_MODEL)

    @property
    def concurrency_slot(self) -> str:
        """同一个并发槽的 LLM 共用一条模型通道；空表示不跟别人抢。"""
        return str(self.meta.get("concurrency_slot") or "").strip()

    @property
    def persona(self) -> str:
        try:
            return (self.root / PERSONA_FILE).read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    @property
    def notes_path(self) -> Path:
        return self.root / "长期笔记.md"

    @property
    def temp_notes_path(self) -> Path:
        return self.root / "临时笔记.md"

    @property
    def draft_path(self) -> Path:
        return self.root / "长期笔记草稿.md"

    @property
    def log_path(self) -> Path:
        return self.root / "对局日志.jsonl"

    @property
    def stat_path(self) -> Path:
        return self.root / "战绩.json"


def _load_one(meta_path: Path) -> PlayerProfile | None:
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    root = meta_path.parent
    # 目录名就是玩家名；profile.json 里的 name 只是说明，不参与匹配。
    return PlayerProfile(name=root.name, root=root, meta=meta)


def all_profiles() -> list[PlayerProfile]:
    if not PROFILE_ROOT.is_dir():
        return []
    out: list[PlayerProfile] = []
    for meta in sorted(PROFILE_ROOT.glob(f"*/{PROFILE_FILE}")):
        profile = _load_one(meta)
        if profile:
            out.append(profile)
    return out


def by_name(key: str) -> PlayerProfile | None:
    key = str(key).strip()
    if not key:
        return None
    for profile in all_profiles():
        if key == profile.name or key == profile.display_name:
            return profile
    return None
