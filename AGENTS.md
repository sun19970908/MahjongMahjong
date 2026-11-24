# 本项目是麻将项目

* 期许是代码扩展性强，灵活性强，结构清晰，少写死代码，不要过度工程
* TUI暂时不维护了，代码保留不删
* 可以接入真人，本地llm，或云端agent
* 对本地llm有额外优化
* 规则暂时为铁岭地区麻将规则，以后会允许自定义规则

## agent的主要任务

* 改好麻将代码
* 用接口陪用户打麻将，顺便测试（打牌的时候别作弊，除非是必要的调试）
* 写好你自己的麻将笔记，你的笔记可能会成为本地llm打麻将的宝贵经验

## 监听/落子口径（必须遵守）

* **监听不是挂 `watch.py` 就完事。** `watch.py --brain fast` / `--brain model` 会自己写 `answer.json`，是脚本/模型在打，不是“我监听”。
* `watch.py --brain mine --cap 0` 只是门铃：它只会在日志里喊“你上”，**不会替你写 `answer.json`**；没人答，牌桌就死等。
* 正确做法是在对话回合里用 `seat_io.py wait` + `seat_io.py answer` 循环：
  1. `python seat_io.py wait --seat <n> --timeout 0 --json` 阻塞等 pending；
  2. 从 `legal` 里选动作，优先 `--pick N`（完整保留 `meta`），写回 `answer`；
  3. 答完一手**立刻再 wait**（或直接用 `answer --wait`），直到局终。
* 加杠/明杠/后开花优先用 `--pick N`。手拼 `--kind` 时，只有当前 `legal` 里唯一匹配才会自动补 `meta`；有多个候选会被拒绝并提示改用 `--pick`，别硬猜。
* 只读 `pending.view`，不要读 `state.json` 的 `hands`；`wait` 返回 `timeout` 是还没轮到，不要乱答。
* 网页牌桌给 harness 留 external 椅子时，接档也要显式 `--players`，否则老存档的座位表可能没有 external 位。
* 详细命令和例子见 `agent打牌接口.md` 第〇、四、八、十节。

