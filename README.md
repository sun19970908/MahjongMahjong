# 四人麻将 · LLM 牌友陪打

112 张（万/饼/条 1–9 各 4 + 红中 4），规则以 `规则.txt` 为唯一权威；这份 README 只讲**代码怎么分层、怎么开一桌**。

## 开一桌

```bash
python play.py                                   # 接当前 state.json
python play.py --new                             # 新开一局：你坐 1 号，牌友坐 2 号，其余随机
python play.py --new --seat 2 --buddy 3 --agent 4      # 自己排座
python play.py --new --players "1=human,2=llm,3=external,4=random"
python play.py --new --dealer 2 --rounds 3             # 指定 2 号坐庄，连打 3 局（胡了连庄）
python play.py --new --demo --offline                  # 不调模型，四家随机，只验画面
```

座位号一律从 **1** 起（命令行、TUI、`seats/seatN/` 目录、`state.json` 的 meta 都是）；引擎内部仍用 0 基下标，换算只走 `seats.label()/index()`。

不带 `--new` 时默认接 `state.json`，座位以存档为准；`--new` 才按命令行重新排座。

“圈数”按庄家轮转算：本场第一个庄家起，四家各坐一次庄、庄家回到起点算一圈；连庄和荒庄会让一圈超过四局。

轮到你出牌时可以输入 `/p` 中场暂停，或 `/e` 提前结束这一场（当前局不结算，直接进终场聊天）；局间聊天里打 `/e` 也一样。

网页桌面（另开浏览器看，不影响 TUI）：

```bash
python board.py --watch 2        # 每 2 秒把 state.json 画成 桌面.html
```

网页牌桌（浏览器里直接打 1 号）：

```bash
python webui.py                  # 默认接 state.json；没有存档会报错
python webui.py --new            # 新开一桌，不读存档
python webui.py --debug          # 调试全牌：四家手牌直接显示在座位里，仅本地教学用
python webui.py --port 8765      # 换端口
```

网页只拿 1 号视角：自己的手牌、四家公开的副露/牌河/报听/杠；别家的手牌只显示张数，别人报听不显示在等什么。

网页牌桌现在是**内核 + UI 适配层**：`core.py` 管牌局、存档、快照、多局、暂停/聊天；`webui.py` 只做 HTTP 和按钮队列，`webui.html/js` 只渲染。TUI 的 `play.py` 同样只做终端画面和键盘输入，不再自己跑牌局。

牌面贴图在 `tiles/`（公共领域，来源见 `tiles/README.md`），会被烘进 HTML，单文件就能带走。

## 快照与回退

每步落子前写一帧 `snapshots/<场次>/step_NNNNN.json`，默认每场保留最近 200 帧：

```bash
python rollback.py              # 列出最近 20 帧
python rollback.py --to 42      # 回到第 42 步落子之前
python rollback.py --latest     # 回到最近一帧
```

恢复前会把当前 `state.json` 备份成 `state.before-rollback.json`；停掉牌桌进程后，再运行 `python play.py` 接着打。

## 牌局存档（`saves/`）

`state.json` 是 live 自动档，只跟当前这一手/这一局。想把一场连续牌局冻结下来，用“存档”：

- 网页顶栏“存档” → “存本场”，把当前 `state.json` 和本场局级账本写进 `saves/<id>/`；
- 一档一个目录：`manifest.json` 存摘要和逐局结果，`state.json` 存可接回的完整牌局；
- “接档”会先备份当前 `state.json` 成 `state.before-load-*.json`，再换档重启内核；
- manifest 不写四家暗手，只有已经公开的赢家胡牌和结算。

命令行等价：

```bash
python save_store.py list
python save_store.py save --name "六局"
python save_store.py load <档id>
python save_store.py delete <档id>
```

旧版本没有局级账本，第一次接旧档只会补记“当前已结算的这一局”；前面的局在 manifest 里标成 `history_partial`，不会伪造。

## 玩家档案（`profiles/<名字>/profile.json`）

模型接入写在档案的 `llm` 段，每座牌友可以各配各的（参考 `G:\AI\livliv-pet\llm.config.json`）：

```json
{
  "name": "方小满",
  "freeThinking": true,
  "llm": {
    "baseUrl": "http://127.0.0.1:11436/v1/chat/completions",
    "apiKey": "",
    "reasoningBudget": true,
    "headers": {"Upstream-Url": "https://..."},
    "request": {
      "model": "strata-nothink",
      "temperature": 0.0,
      "max_tokens": 8192
    }
  }
}
```

`freeThinking` 控制这一座是否允许在别人回合后台预跑“自由思考”，默认 `true`；设 `false` 就不会再发起空闲预跑，也不往上下文里写空想。

`llm.request` 里的参数会原样带给接口（如 `thinking`）；`model`、`messages`、`temperature` 每手由牌局覆盖，`max_tokens` 只认档案里这一份，代码不再按每手改它。`reasoningBudget` 是 Strata 的思考预算开关：`true` 才发 `reasoning_budget_tokens`，出牌提示里也会写“思考预算 N token”；接云端 OpenAI 兼容接口时设 `false`，请求和提示都不再提思考预算，只保留档案里的 `max_tokens` 输出上限。老的顶层 `model` 写法仍能读。

## 分层

| 文件 | 管什么 |
| --- | --- |
| `referee.py` | 纯规则裁判：什么算胡、听几张、哪些动作合法。不认人、不记局。 |
| `game.py` | 一局的状态机：摸/打/碰/吃/杠/抢杠/报听/后开花，收局时调 `scoring`。 |
| `scoring.py` | 结算：番种表、番→分、谁付谁。规则原文那串番种只在这一个文件里。 |
| `seats.py` | 座位号换算、掷骰定庄、赛后庄家轮转。 |
| `players.py` | 玩家层：`HumanPlayer` / `LLMSeat` / `ExternalPlayer` / `RandomPlayer`，加 `SeatPlan`/`build_players`/`Table` 这套装配。**要加新玩家类型，只改这里和 `KINDS`。** |
| `llm_player.py` | 牌友的大脑：提示词、工具调用、预算、笔记。 |
| `core.py` | **牌局内核**：排座、主循环、动作校验、存档、每步快照、多局轮转、暂停/聊天、外部接入等待。TUI 和网页都只是它的 UI。 |
| `play.py` | TUI 适配层：画终端、读键盘、把编号翻成 `Action`；不跑牌局、不存档、不轮庄。 |
| `state.py` | 存档/读档（含庄家、骰子、杠数、闭门听这些新字段），以及每步快照。 |
| `rollback.py` | 列出/恢复每步快照。 |
| `board.py` | 把存档画成网页桌面。 |
| `webui.py` / `webui.html` / `webui.js` | 网页适配层：进程内启动 `TableCore`，HTTP 只收按钮答案和读状态；不判规则、不推进牌局。 |
| `seat_io.py` / `watch.py` | 外部座位的文件协议 / 自动手。 |

## 座位文件协议（外部座位）

目录 `seats/seatN/`：引擎写 `pending.json`，外部写 `answer.json`（`{"round": 同一个数, "action": {...}, "say": "..."}`），不想下就写 `give-up` 空文件。命令：

```bash
python seat_io.py pending --seat 3 --json
python seat_io.py answer  --seat 3 --kind discard --tile s8 --say "这张先走"
python seat_io.py wait    --seat 3 --timeout 60 --json
```

## 结算（`规则.txt`「结算加番规则」）

番种逐条拆开相加：**赢家番**（庄家、闭门听、自摸、四归一可叠加、三红中、手把一、飘胡、前开花、后开花、三清、海底捞月）
由三家共同承担；**输家番**（庄家、点炮含抢杠、闷、手把一、流泪）只落在那一个付款人自己身上。没赢也至少付 1 分，但“保底”本身是 0 番（2^0=1），不算一番。闷只算输家的；三清 = 三清 1 + 闷 1 = 2 番；
四归一按整手牌种合计（杠/叉/吃都算）；四红中 = 三红中 1 + 四归一 1 = 2 番。
口径：只要有人胡，**三家都付**；每家先按 0 番保底，再加上自己的庄家/点炮/闷/手把一/流泪。
底分 1，每番 ×2，封顶 8 番（`--base/--fan-cap` 可改）；自摸三家各按自己的番付，点炮那家再多一份点炮番。
比如庄家点炮：庄家多庄家 + 点炮两番，另外两家只付赢家番，且至少 1 分。底分/封顶都在 `scoring.py` 顶部，改一处即可。

## 笔记（`llm_player.py`）

- `牌友笔记.md`：长期笔记，只在**开桌**时读一次进顶部 system；一整场（`--rounds N`）的顶部提示词不变。
- `临时笔记.md`：本局临时笔记，下一局开始清空；局中/局间聊天都能看见。
- `长期笔记草稿.md`：整场追加的长期笔记草稿。局中、局间只能 `append` 进这里，不落 `牌友笔记.md`；全部局打完的散场聊天才并档，也只有这时能 `replace` 原文。改完是**下次开桌**生效。
- 草稿和临时笔记都落盘，启动接存档时原样恢复。

## 自测

```bash
python referee.py     # 规则用例
python scoring.py     # 番种用例
python game.py        # 状态机用例 + 30 局随机守恒（约 2–4 分钟）
python play.py --new --offline --steps 60 --no-save   # 画面冒烟
```
