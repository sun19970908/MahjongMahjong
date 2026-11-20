# 本项目是麻将项目

* 期许是扩展性强，结构清晰，少写死代码
* 可以接入真人，本地llm，或云端agent
* 对本地llm有额外优化
* 规则暂时为铁岭地区麻将规则，以后会允许自定义规则

## agent的主要任务

* 改好麻将代码
* 用接口陪用户打麻将，顺便测试
* 写好你自己的麻将笔记，你的笔记可能会成为本地llm打麻将的宝贵经验

## 监听/落子口径（必须遵守）

* **监听不是挂 `watch.py` 就完事。** `watch.py --brain fast` / `--brain model` 会自己写 `answer.json`，是脚本/模型在打，不是“我监听”。
* `watch.py --brain mine --cap 0` 只是门铃：它只会在日志里喊“你上”，**不会替你写 `answer.json`**；没人答，牌桌就死等。
* 正确做法是在对话回合里用 `seat_io.py wait` + `seat_io.py answer` 循环：
  1. `python seat_io.py wait --seat <n> --timeout 0 --json` 阻塞等 pending；
  2. 从 `legal` 里选动作，优先 `--pick N`（保留 `meta`），写回 `answer`；
  3. 答完一手**立刻再 wait**，直到局终。
* 加杠/明杠/后开花必须用 `--pick N`；`--kind` 会丢 `meta`。
* 只读 `pending.view`，不要读 `state.json` 的 `hands`；`wait` 返回 `timeout` 是还没轮到，不要乱答。
* 详细命令和例子见 `agent打牌接口.md` 第〇节、第四节和第八节。

