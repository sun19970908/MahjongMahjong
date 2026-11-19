# 四人麻将 · LLM 牌友陪打

112 张（万/饼/条 1–9 各 4 + 红中 4），规则以 `规则.txt` 为唯一权威；这份 README 只讲**代码怎么分层、怎么开一桌**。

## 开一桌

```bash
python play.py                                   # 你坐 1 号，牌友坐 2 号，其余随机
python play.py --seat 2 --buddy 3 --agent 4      # 自己排座
python play.py --players "1=human,2=llm,3=external,4=random"
python play.py --dealer 2 --rounds 3             # 指定 2 号坐庄，连打 3 局（胡了连庄）
python play.py --demo --offline                  # 不调模型，四家随机，只验画面
```

座位号一律从 **1** 起（命令行、TUI、`seats/seatN/` 目录、`state.json` 的 meta 都是）；引擎内部仍用 0 基下标，换算只走 `seats.label()/index()`。

轮到你出牌时可以输入 `/p` 中场暂停，或 `/e` 提前结束这一场（当前局不结算，直接进终场聊天）。

网页桌面（另开浏览器看，不影响 TUI）：

```bash
python board.py --watch 2        # 每 2 秒把 state.json 画成 桌面.html
```

牌面贴图在 `tiles/`（公共领域，来源见 `tiles/README.md`），会被烘进 HTML，单文件就能带走。

## 分层

| 文件 | 管什么 |
| --- | --- |
| `referee.py` | 纯规则裁判：什么算胡、听几张、哪些动作合法。不认人、不记局。 |
| `game.py` | 一局的状态机：摸/打/碰/吃/杠/抢杠/报听/后开花，收局时调 `scoring`。 |
| `scoring.py` | 结算：番种表、番→分、谁付谁。规则原文那串番种只在这一个文件里。 |
| `seats.py` | 座位号换算、掷骰定庄、赛后庄家轮转。 |
| `players.py` | 玩家层：`HumanPlayer` / `LLMSeat` / `ExternalPlayer` / `RandomPlayer`，加 `SeatPlan`/`build_players`/`Table` 这套装配。**要加新玩家类型，只改这里和 `KINDS`。** |
| `llm_player.py` | 牌友的大脑：提示词、工具调用、预算、笔记。 |
| `play.py` | TUI 与主循环：排座、画画面、暂停聊天、多局轮转。 |
| `state.py` | 存档/读档（含庄家、骰子、杠数、闭门听这些新字段）。 |
| `board.py` | 把存档画成网页桌面。 |
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
由三家共同承担；**输家番**（庄家、点炮含抢杠、闷、手把一、流泪）只落在那一个付款人自己身上。闷只算输家的；三清 = 三清 1 + 闷 1 = 2 番；
四归一按整手牌种合计（杠/叉/吃都算）；四红中 = 三红中 1 + 四归一 1 = 2 番。
口径：赢家番三家共同承担；闷、庄家、点炮（含抢杠）、手把一、流泪是各家自己的输家番，只加在付款人自己那一笔上。
底分 1，每番 ×2，封顶 8 番（`--base/--fan-cap` 可改）；自摸三家各按自己的番付，点炮只有那家付。
比如庄家点炮：庄家比别人多一番，只有他多付。底分/封顶都在 `scoring.py` 顶部，改一处即可。

## 笔记（`llm_player.py`）

- `牌友笔记.md`：长期笔记，只在**开桌**时读一次进顶部 system；一整场（`--rounds N`）的顶部提示词不变。
- `临时笔记.md`：本局临时笔记，下一局开始清空；局中/局间聊天都能看见。
- `长期笔记草稿.md`：整场追加的长期笔记草稿。局中、局间只能 `append` 进这里，不落 `牌友笔记.md`；全部局打完的散场聊天才并档，也只有这时能 `replace` 原文。改完是**下次开桌**生效。
- 草稿和临时笔记都落盘，`--resume` 接存档时原样恢复。

## 自测

```bash
python referee.py     # 规则用例
python scoring.py     # 番种用例
python game.py        # 状态机用例 + 30 局随机守恒（约 2–4 分钟）
python play.py --offline --steps 60 --no-save   # 画面冒烟
```
