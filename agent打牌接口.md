# 和 harness 一起打麻将（备忘）

下回要开一局、或者要让我坐到牌桌上，照这份做，别重新猜。

## 〇、监听 external 座位的正确办法（先看这条）

**监听 = 我一直等在 `pending.json` 上，看到就答；不是挂个 `watch.py` 就算监听。**

- `seat_io.py wait`：只读文件、阻塞等下一手；这是监听。
- `seat_io.py answer`：按 `legal` 写回动作；这是落子。
- `watch.py --brain fast` / `--brain model`：会自己写 `answer.json`，是脚本/模型在打，不是“我监听”。
- `watch.py --brain mine --cap 0`：只是门铃，会喊“你上”，但**不会替你写 answer**；没人接就死等。它不能当监听，也不能兜底。

正确循环（座位号按当前桌换，下面以 1 号为例）：

```powershell
# 1. 阻塞等 1 号 pending（可顺带拉增量事件与聊天通知）；超时 0 = 一直等
& 'D:\Program Files\Python313\python.exe' seat_io.py wait --seat 1 --timeout 0 --include-events --include-chat --json

# 2. 从返回 JSON 的 legal 里选一个，推荐 --pick N（也可按牌名/组合选）；加 --wait 可当场校验并顺带等下一手
& 'D:\Program Files\Python313\python.exe' seat_io.py answer --seat 1 --pick 3 --say "…" --wait
```

要点：

- 每答完一手，**马上再 `wait`**（或直接用 `answer --wait`）；答完就停，就是没监听。
- `wait` 返回 `timeout`，说明还没轮到这张座位（牌桌在等别人），继续等，不要乱答；`error.json` 有错会快速报错返回。
- 仍推荐用 `--pick N`；若用 `--kind` 且在当前 `legal` 里唯一匹配，引擎会自动补齐 `meta`；有多个候选会被拒绝并提示改用 `--pick`。支持 `7饼` 等中文牌名。
- 只看 `pending.view` / `pending.json`，不要读 `state.json` 里的 `hands`；那会看到别家手牌。
- 要挂后台进程，只能挂“只读提醒”，真正落子仍由我按 pending 手动作答；不要让 `watch.py --brain mine` 的“保底”替打。

## 一、四个人是谁

`play.py`（TUI）和 `webui.py`（网页）都只是 `core.py` 的 UI 适配层；座位还是这几种：

| 座位参数 | 谁 | 实现 |
| --- | --- | --- |
| `--seat 1` | 用户本人，在 TUI 里手打 | `HumanPlayer` |
| `--buddy 2` | 牌友，进程内的 LLM | `LLMSeat` → `llm_player.LLMPlayer` |
| `--agent 3` | 我（harness）坐这儿 | `ExternalPlayer`，走文件收发 |
| 剩下那个 | 随机 | `RandomPlayer` |

开局命令（新一局）：

```
python play.py --new --seat 1 --buddy 2 --agent 3 --trace
```

接上一局没打完的（`state.json` 里那份）：

```
python play.py --trace
```

不带 `--new` 时按存档里的 `meta` 接档，`--seat/--buddy/--agent` 这轮不算；想换座或新开就加 `--new`。想在新窗口开就 `wt.exe -d <项目根> python play.py ...`。

新玩法（2026-11 这一版加的，都在里面）：

| 参数 | 作用 |
| --- | --- |
| `--players "1=human,2=llm,3=external,4=random"` | 一次说清整桌人，不用再一个个 `--seat/--buddy/--agent` |
| `--dealer 2` | 指定 2 号坐庄；不给就开局掷两颗骰子定 |
| `--rounds 3` | LLM 上下文窗口：每 3 局清空一次，不是牌局总局数；整场只有你停/步数上限才收 |

画面：开局显示「庄 N 号 骰 a+b=合计」，庄家有 ◆庄 标记；收局下方直接列番种、多少分、谁付。

网页桌面（另开浏览器看，不影响 TUI）：

```bash
python board.py --watch 2          # 每 2 秒读 state.json 重画 桌面.html
```

想换成真牌面贴图：把 `tiles/<牌名>.png`（m1…m9 / p1…p9 / s1…s9 / z5）丢进 `tiles/`，`board.py` 自动改用图片，没图就文字画。


## 二、接入位（3 号）的协议：文件协议 + WebUI HTTP 适配

支持自定义显示名：`--players "1=human,2=llm,3=external:桌宠小鲸,4=random"`（显示为 `桌宠小鲸（3号）`）。

目录：`seats/seat3/`（`players.DEFAULT_SEAT_ROOT` = 项目根下的 `seats`）

- `pending.json`：引擎写的「这一手轮到你」，字段自包含：
  - `round` / `seat` / `name` / `kind` / `forced`
  - `table`：桌况元信息（`run_id` / `round` / `lap` / `steps` / `dealer` / `board` / `names` / `said`）
  - `view`：自己的公开+私有视角（不含别家手牌，含公开牌河、副露、`ting` 标记、公共事件日志与近期聊天）
  - `legal`：合法动作列表，每项自带 1 基 `pick`、中文 `label`、中文牌名 `name` 和 `meta`
  - `ting_hints`：出牌阶段直接算好的听牌提示（含听张与 `left_public` 公开余张数）
  - `recent_events`：经当前座位公开视角过滤后的增量事件（含出牌动作与桌面 `--say` 发言，绝不泄露他家暗摸）
- `answer.json`：写回动作，支持两种写法：
  - 推荐直接选下标：`{"round": 同一个数, "pick": 3, "say": "..."}`（引擎按 `legal` 补全 `action` 与 `meta`）
  - 或写动作对象：`{"round": 同一个数, "action": {"kind": "discard", "tile": "7饼"}, "say": "..."}`（支持中文牌名，唯一 `meta` 自动补齐）
- `error.json`：回传若被拒绝（如 `round` 不匹配、动作非法、`meta` 冲突），引擎会立刻写入 `error.json`，不再静默空转。
- `status.json`：座位生命周期状态（`playing` / `waiting_decision` / `chatting` / `paused` / `awaiting_next` / `finished` / `stopped` / `interrupted`）与当前在等谁 `waiting_on`；停桌/终局会自动清理陈旧 `pending.json`。
- `events.jsonl`：持久化生命周期与桌面事件流（`round_start`、`round_end`、`match_end`、`resumed` 等）。
- `chat_pending.json` / `chat_answer.json`：暂停/局间聊天、喊话接话时的外部应答协议（配合 `chat_ready.json` 或 `talk_in/out.jsonl`）。
- `give-up`：写个空文件可把这一手交给随机托管。

## 三、手动答一手 / CLI 常用命令

```bash
python seat_io.py status  --seat 3            # 座位状态、是否在等动作/聊天、最近错误
python seat_io.py pending --seat 3            # 中文牌面摘要（桌况、手牌、牌河、听牌提示、选项）
python seat_io.py events  --seat 3 --since 0  # 查看 round_start / round_end 等事件
python seat_io.py wait    --seat 3 --timeout 600 --include-events --include-chat --json
python seat_io.py answer  --seat 3 --pick 3 --say "打这张" --wait --json  # 答完即校验并纯 JSON 等下一手
python seat_io.py answer  --seat 3 --kind discard --tile 3万        # 支持中文牌名与自动唯一 meta
python seat_io.py answer  --seat 3 --kind chi --combo 4条+6条
python seat_io.py reply   --seat 3 --text "这手真险"               # 回复暂停/局间聊天
python seat_io.py trace   -n 20                # 复盘：谁打了什么、多久、读了多少 token
python seat_io.py hear/tell --seat 3 ...       # 递话窗口
```

## 四、`watch.py`：自动脑或门铃，不是默认监听

`watch.py` 每 0.1 秒看 `pending.json`。它有三种用法，先分清谁在落子：

```bash
python watch.py --seat 1                 # 默认模型脑（本地 strata），它自己打
python watch.py --seat 1 --brain fast    # 纯脚本快脑，它自己打
python watch.py --seat 1 --brain mine --hold 12 --cap 0   # 只喊“你上”，绝不由脚本落子
python watch.py --seat 1 --dry           # 只打印打算怎么打，不落子
```

**`--brain mine --cap 0` 不是监听，只是门铃。** 它来了 pending 先算一个内存里的保底动作，然后记一行“你上”并一直等；它**不会写 `answer.json`**。我不接，牌桌就卡在这一手，直到有人用 `seat_io.py answer` 落子。真正监听见第〇节。

`--brain fast` / `--brain model` 会自己落子，适合测试或故意让脚本打；要“我亲自打”就别开。

`--brain mine --cap 0` 的日志长这样：

```
22:46:01 第 3 手 react 你上，预算 12s
22:46:13 第 3 手 超预算，不替你打，我继续等
22:48:04 第 3 手 你接了（慢了一步，没替你打）
```

后台挂（不占窗口，日志进 `watch.log`）：

```powershell
Start-Process python -ArgumentList 'watch.py','--seat','3' `
  -WorkingDirectory 'd:\Users\C\Documents\Empty\Mahjong' -WindowStyle Hidden `
  -RedirectStandardOutput 'watch.out' -RedirectStandardError 'watch.err'
```

**要看守就挂 `watch_up.ps1`**：它用系统 python（`D:\Program Files\Python313\python.exe`）跑 `watch.py`，退出就 2 秒后重拉，崩溃不会让牌桌干等。

```powershell
Start-Process powershell -ArgumentList '-NoProfile','-WindowStyle','Hidden','-ExecutionPolicy','Bypass',
  '-File','d:\Users\C\Documents\Empty\Mahjong\watch_up.ps1' -WindowStyle Hidden
```

砍掉：`Stop-Process -Id <pid> -Force`，或 `Get-CimInstance Win32_Process -Filter "Name='python.exe'"` 里认出 `watch.py` 那条（看守那层是 powershell，别只杀 python）。

### 怎么看「在想」还是「卡了」

模型脑走的是**流式**（`stream: true`），思考过程直接过 11436 反代，反代上能看到 token 在走。同时 `watch.log` 每 1.5 秒一行心跳：

```
06:15:35 　想 638 字
06:15:38 　想 1138 字
06:15:47 　说 2819 字 / 已开口 1 字
06:15:48 第 8 手 discard -> discard s8 [19.1s] …
```

字在涨 = 它真在想；一分钟没有新行 = 它挂了或牌桌没给它 pending，先看 `seats/seat3/pending.json` 有没有、进程还在不在。

它内部做的事：

1. `pending.json` 的 `legal` → `Action`（`as_action()`，支持 `draw/bloom/ting` 这些新阶段，`meta` 原样带回）。
2. **快脑**（纯脚本，不调模型）：能胡就胡（`win`）→ 能后就开（`bloom`）→ 听牌就报（`ting`）→ 出牌时把每个候选都拿裁判 `Referee.waits()` 算一遍，优先「打完立刻单听」的那张，其次是听多张的，都听不了就打邻近最少的孤张。
3. **模型脑**（`--brain model`）：交给 `LLMPlayer.decide`，`strata-lowthink`，日志 `seat3_log.jsonl`。实测一手 16–61 秒，牌桌会明显停住，只适合复盘看它怎么想。
4. **我脑**（`--brain mine --cap 0`）：只在日志里喊“你上”，`settle()` 盯着 `pending.json` 等我用 `seat_io.py answer` 落子；它不会写 `answer.json`，也不会保底。
5. **护栏**：出来的动作必须在 `legal` 里（`allowed()` 比 kind + tile/combo/meta），不在就用 `fallback()`（优先 `pass`、`draw`，否则第一个合法动作）——不让一手胡来的动作把牌桌判崩。
6. 自动脑（`fast` / `model`）写 `answer.json`，同时在 `watch.log` 记一行：手数、阶段、动作、耗时、说的话。

脚本自测过：12 局全随机牌桌，3 号用快脑，0 次崩、0 次非法动作，整局耗时 3–4 秒。

## 五、跑一轮的最小清单

1. 确认 Strata 服务在（只有模型脑才用得上：`http://127.0.0.1:11436/v1/chat/completions`）。
2. 开牌桌：`python play.py --new --seat 1 --buddy 2 --agent 3 --trace`（或直接 `python play.py` 接档）。
3. 挂监听：`python seat_io.py wait --seat 3 --timeout 0 --json`，出现 pending 就按第〇节循环答。
4. 自己坐 1 号打；轮到我（3 号）时我看 `pending.json` 自己落子，不让 `watch.py --brain fast/model` 替打。
5. 打完看 `python seat_io.py trace -n 40` 复盘。

## 六、牌桌上的规矩（改代码前先看 `规则.txt`，那是唯一权威）

- 13 张牌，杠一次多一张；必须先报听才能胡（后开花不用报听）；报听后手牌锁死、只能打回刚摸的那张，但可以杠。
- 不允许暗杠：手里四张要杠就亮出来算明杠；别人打牌后直接杠的那种（大明杠）不能抢，手里四张亮出来的杠、加杠能抢。
- 牌墙最后一张是明牌，TUI 上画成一张麻将摆在左上角。
- 后开花只在「杠完补张那一下」判：要单听、等的正好是尾张，不要求报听。
- 能胡的时候不胡（别人打的牌、自己摸的牌、抢杠这三处 `win` 不要），本局就锁死不准再胡（`dead_win`，TUI 标 `✕已弃胡`）；后开花那一下选择正常摸牌不算弃胡。

## 七、文件速查

| 文件 | 干什么 |
| --- | --- |
| `play.py` | 终端 UI 适配层（牌局主循环在 `core.py`） |
| `core.py` | 牌局内核：排座、主循环、动作校验、存档、快照、多局、暂停/聊天 |
| `game.py` / `referee.py` | 引擎 / 裁判（规则校验、`pending`、`apply`） |
| `llm_player.py` | 牌友的脑子（提示词、工具解析、预算） |
| `players.py` | 四种座位玩家，`ExternalPlayer` 的收发就在这儿 |
| `seat_io.py` | 接入位的命令行进出接口 |
| `watch.py` | 自动脑/门铃；`fast`/`model` 自动打，`mine --cap 0` 只提醒不落子 |
| `state.py` / `state.json` | 存档、启动默认接档 |
| `webui.py` / `webui.html` / `webui.js` | 网页 UI 适配层，进程内启动 `core.TableCore` |
| `trace.jsonl` | 每步一行，复盘用 |
| `规则.txt` / `牌友笔记.md` | 规则原文 / 牌友的长期笔记 |

## 八、我（harness）亲自监听时怎么打

起局只要一条：`python play.py --seat 1 --buddy 2 --agent 3`。3 号是接入位，牌桌会往 `seats/seat3/pending.json` 写一手，剩下全是我的事——**不需要谁点我**，我这边一条命令就能一直在场：

```powershell
python seat_io.py answer --seat 3 --kind discard --tile s8 --say "…"; python seat_io.py wait --seat 3 --timeout 120 --json
```

一条命令干两件事：先把上一手落子，再挂在那里等下一手；等到 pending 就返回整段 JSON（手牌、面子、牌河、明牌、`legal`、`last`），我照着判，写下一手。循环起来就是整局跟着走，中间不用人喊。

要点：

- 落子优先 `--pick N`（完整保留 `meta`）。手拼 `--kind` 时，若动作在当前 `legal` 里唯一匹配，引擎会自动补 `meta`；加杠/明杠/后开花有多个候选时会被拒绝，必须改用 `--pick`。
- `wait` 超时返回 `timeout`，说明牌桌正等别人（比如 1 号你），继续等就是，别乱答。
- 我落子之后 `pending.json` 会被 `ExternalPlayer` 删掉；如果另开了 `watch.py --brain mine`，它看到就记一行「你接了」，不会重复落子。
- 我慢了，不会有脚本兜底；`watch.py --brain mine` 只是门铃，`--cap 0` 时它会一直等，牌桌也就停在这一手。要避免卡住，就答完一手立刻再 `wait`。
- 我只能在“对话回合开着”的时候听：我不像后台进程，没人给我发消息就不会自己醒。所以要么开局跟我说一句「开」，我就在这个回合里一直 `seat_io.py wait/answer` 到局终；要么另开一个只读提醒，但看到 pending 仍必须由我落子。**不要靠 `watch.py --brain fast/model` 替我打。**
- 交叉验证：`python seat_io.py trace -n 20` 看每手是谁、多久；`watch.log` 看「你上 / 你接了」。
- **别闷头打牌**：看见规则的窟窿、引擎的怪招、TUI 的显示问题，牌桌上用 `--say "⚠️ …"` 直接说一句（1 号看得见），同时 `python seat_io.py log "…"` 记进 `问题.md`；`python seat_io.py log -l` 看清单，局终一次性交代。
- **只看 `pending.json` 给的 `view`，别去翻 `state.json` 的 `hands`。** 那里面是四家的计数数组，看一眼就知道别人手里剩什么、在等哪张——我上一局就这么把 1 号的单吊 6 饼算出来还念到桌上了。`view` 里只有该我看见的：自己的手牌、四家的面子、牌河、牌墙剩几张、最后一张明牌。别人报没报听（`ting` 旗标）是牌桌公开信息，可以提；`hands` 不是。

## 九、已知的坑

- `--kind` 省略 `meta` 时，只有当前 `legal` 里唯一匹配才会自动补齐；有歧义会拒绝并提示 `--pick`，别硬猜。
- `answer.json` 的 `round` 抄错，等于没答，牌桌会死在原地等。
- **别把 `watch.py --brain fast/model` 当默认监听**：它会自己落子；`--brain mine --cap 0` 只是门铃，不写 answer。真正监听是第〇节的 `seat_io.py wait/answer` 循环。
- 存档恢复后 external 的 `round` 会按 `meta.ext_rounds` 接上，`events.jsonl` 也会续写；只有旧档没存 `ext_rounds` 时才从 1 重计。`trace.jsonl` 和旧事件不一定完整，复盘时别只拿 TUI 回合号硬对。
- 两个进程同时写 `牌友笔记.md`（牌友 + 3 号）会互相覆盖，别指望它俩共享记忆。
- 空想没结论时**别往历史里塞占位话**：「（闲下来想这手牌，还没想完）」这类句子会被模型当成自己上一句台词，下一手照着念，牌桌上整排复读。

## 十、网页牌桌里让 harness 接入

网页版和 TUI 是同一套内核：`webui.py` 进程内启动 `core.TableCore`，不再拉 `play.py` 子进程。网页按钮只把 `Action` 放进内核队列，合法性仍由核心判断。

网页默认座位：**1 号网页本地人、2 号 LLM 牌友、3/4 随机**。要给 harness 留椅子，必须显式把 3 号设成 `external`：

```powershell
cd 'd:\Users\C\Documents\Empty\Mahjong'

# 新开一桌，3 号留给 harness；1 号在网页里打
& 'D:\Program Files\Python313\python.exe' webui.py --new `
  --players "1=human,2=llm,3=external,4=random"

# 接当前没打完的 state.json，同样要显式写 3 号，否则 3 号会按老 meta 变成随机
& 'D:\Program Files\Python313\python.exe' webui.py --no-open `
  --players "1=human,2=llm,3=external,4=random"
```

这样：

- 1 号：网页 UI，动作走 `POST /api/action`，由内核转给 1 号本地座位。
- 2 号：进程内 LLM 牌友，不是 external。
- 3 号：harness 的 external 座位，文件协议在 `seats/seat3/`。
- 4 号：随机。

网页 UI **只给 1 号视角**；3 号的私牌只在 `seats/seat3/pending.json` 的 `view` 里，网页正常模式只显示 3 号的公开副露/牌河/手牌张数。调试模式 `--debug` 会把四家手牌直接铺在各自座位里，给本地教学/复盘用；不要把调试页投给 LLM。

### harness 侧最小循环（文件协议，和 TUI 完全一样）

```powershell
# 1. 等 3 号的一手（可带事件和聊天）；timeout 0 = 一直等
& 'D:\Program Files\Python313\python.exe' seat_io.py wait --seat 3 --timeout 0 --include-events --include-chat --json

# 2. 从返回 JSON 的 legal 里选一个下标（1 基）；加 --wait 可当场校验并等下一手
& 'D:\Program Files\Python313\python.exe' seat_io.py answer --seat 3 --pick 1 --say "这手这么打" --wait
```

`pending.json` 关键字段：

```json
{
  "round": 12,
  "seat": 3,
  "name": "桌宠小鲸（3号）",
  "kind": "discard",
  "forced": false,
  "table": { "run_id": "...", "round": 1, "lap": 1, "steps": 12, "dealer": 1, "board": {"1": 0, "2": 0, "3": 0, "4": 0}, "names": {"1": "1号（你）", "3": "桌宠小鲸（3号）"} },
  "view": { "hand": ["m1", "..."] },
  "ting_hints": [ { "pick": 2, "tile": "m2", "tile_name": "2万", "waits": ["p3"], "wait_names": ["3饼"], "left_public": [3], "text": "..." } ],
  "recent_events": [ { "seq": 5, "type": "draw", "text": "..." } ],
  "legal": [
    { "pick": 1, "label": "打 m1", "name": "1万", "kind": "discard", "tile": "m1", "meta": {} }
  ]
}
```

`answer.json` 格式（二选一）：

```json
{ "round": 12, "pick": 1, "say": "这手这么打" }
```

或显式 `action`（支持中文牌名，且会在唯一匹配时自动补 `meta`）：

```json
{ "round": 12, "action": { "kind": "discard", "tile": "1万" }, "say": "" }
```

- **加杠、明杠、后开花仍推荐 `--pick N`**；若手拼 `--kind` 省略 `meta` 且在 `legal` 里唯一，引擎会自动补齐；若填错则立刻写 `error.json` 拒绝。
- `round` 必须抄 pending 的；抄错会立刻写入 `error.json` 并在 `answer --wait` / HTTP 接口报错返回。
- 只看 `pending.view` / `/api/seat/<n>/*`，不要读 `state.json` 的 `hands`；别人报听只有 `ting` 布尔，不显示等张。
- 3 号是 external，没人接会等至超时（`--agent-wait 0` 为死等）；开桌后尽快把 harness 挂上，或先通过 `seat_io.py wait` / `/api/seat/3/pending?wait=30` 等开局。
- 当前 `state.json` 的 `meta.agent` 可能和 `you` 同号（老存档），此时内核会忽略同号 agent；所以接档时**必须**显式 `--players` 才能保证 3 号有 external 椅子。

### 如果 harness 走 HTTP（已内置薄适配，不污染网页 JS）

`webui.py` 已内置面向 external 座位的只看自己视角 HTTP 接口（非 external 座位访问一律 `403`，也不会写入 `webui.js`）：

| 接口 | 作用 |
| --- | --- |
| `GET /api/seat/<n>/status` | 外部座位状态、`table.waiting`（当前在等谁与已耗时）、`pending`/`chat_pending` 摘要与最近错误 |
| `GET /api/seat/<n>/pending` | 支持长轮询 `?wait=0..30&include_chat=1&include_events=1&since_event=0` |
| `GET /api/seat/<n>/events` | 增量事件流 `?since=0&limit=0` |
| `POST /api/seat/<n>/answer` | `{round?, pick? \| action?, say?}` 提交动作并校验 |
| `POST /api/seat/<n>/give-up` | 当前这手交给随机托管 |
| `POST /api/seat/<n>/chat-reply` | `{id?, text}` 回复暂停/局间聊天 |

### 网页按钮/接口速查

| 接口 | 作用 |
| --- | --- |
| `GET /api/state` | 1 号视角状态；`--debug` 时含四家手牌 |
| `POST /api/action` | `{index, say}`，1 号动作 |
| `POST /api/pause` / `/api/resume-play` | 暂停聊天 / 继续 |
| `POST /api/end` | 提前结束这一场 |
| `POST /api/next-round` | 局间点“下一局” |
| `POST /api/chat` | `{text, phase}`，暂停/局间聊天 |
| `GET /api/snapshots` / `POST /api/rollback` | 快照列表 / `{name}` 或 `{step}` 回退 |
| `POST /api/new` / `/api/quit` | 新桌 / 停桌 |



