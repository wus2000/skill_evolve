# SpreadsheetBench run `spreadsheetbench_20260708_154932` — max_tokens 截断取证

> 取证时间 2026-07-08 20:0x;实验仍在运行。全部材料逐字未删减。
> 索引见 `INDEX.md`,每条调用一个目录。

## 一、截断总览(扫描 llm_calls.jsonl 全部 25,536 条调用)

| 类别 | 条数 | completion 上限 | 备注 |
|---|---|---|---|
| optimizer / **materials interpret** | **65** | 16384 | **输入轨迹为空**(见第二节) |
| optimizer / proposer | 6 | **12288** | 上限常量漏在 16K 裁定外 |
| optimizer / editpipe3-draft | 3 | 16384 | |
| optimizer / editpipe3-review | 1 | 16384 | |
| optimizer / editpipe3-applier | 1 | 16384 | 响应 160,271 字符 |
| optimizer / json-repair | 2 | 16384 | 修复调用自身又被截断 |
| optimizer / other | 1 | 16384 | |
| target / spreadsheet agent | 9 | 16384 | ReAct 单次输出打满 |
| **合计** | **88** | | |

`css.log` 里可见 84 条 WARNING(其余在 warning 打点之外的路径)。

## 二、头号发现:materials 读到的轨迹全是空的(540/540)

`## The trajectory` 段在**全部 540 次** interpret prose 调用里都是**零字符**。
LLM 在响应里明说 "the trajectory is not provided"、"I cannot analyze a
trajectory that isn't there",随后自我辩论并**幻觉出完整的行为分析**。

- 540 条 interpretation record 全部落盘(`_error` 计数 = 0),已进入 L1 供血链。
- 其中 66 条散文 >2 万字符;65 次调用打满 16K。
- 该批调用共烧掉 **1,468,405 completion tokens**。

**因果链**(已逐段验证):

1. `css/envs/spreadsheetbench/task_interface.py:538`
   `slim = {k: v for k, v in result.items() if k != "conversation"}`
   —— 该 env **有意**把轨迹从 `result.json` 剔除,单独写 `conversation.json`
   (设计注释见同文件 `hydrate_trajectory` 与 526-531 行 docstring)。
2. `css/materials/common.py:load_burst_trajectories` 是**通用**加载器,只
   `read_json(<pred>/result.json)` → `TaskResult.from_dict(d)`。
3. `TaskResult.from_dict` 找不到 `messages`/`conversation` 键 → `messages = []`。
4. `_interpret_one` 里 `format_trajectory([])` → 空串 → prose prompt 的轨迹段为空。

**只影响 SpreadsheetBench。** 对照验证:
- AppWorld 同期 run 的 675 次 interpret 调用,空轨迹 **0** 条(body 4,414–144,631 字符);
  其 `result.json` 直接带 `conversation`(33 条消息)。
- `slim = {... if k != "conversation"}` 全库仅此一处;
  共享的 `css/envs/common/persist_result` 保留 conversation。

**旁证**:L0 不受影响——exploitation 在内存里直接持有 `TaskResult`,不经磁盘往返;
所以同一个 run 的 editpipe3 差分链读到的是真实轨迹(34 raw edits 等)。
只有 L1 的 materials 层从磁盘重载时踩空。

真实轨迹已随包提供:每条 interpret 目录下 `real_trajectory/conversation.json`
(8–63 条消息,均值 17.4)+ `result.json`,以及该调用产出的
`interpretation_record.json`(即那份幻觉解读)。匹配方式:用响应前 300 字符
与落盘 record 的 `interp_prose` 精确对齐,65/65 全部命中。

## 三、第二发现:12288 上限漏在「16K 下限」裁定之外

- `css/optimizer/reflect.py:60` `_PROPOSER_MAX_TOKENS = 12288`
- `css/l1gen/new_pipeline.py:40` `_DRAFT_MAX = 12288`
- `css/l1gen/merge_pipeline.py:55` `_DRAFT_MAX = 12288`

commit faf11e3(16K completion 下限)只改了 client/tracing/json_repair/env agent
的**默认参数**,这三个显式常量未纳入。本 run 里 6 次 proposer 截断都停在 12288。

## 四、第三发现:退化重复生成(11/88)

尾部字符级退化(单字符连跑 ≥800,或 2 字符循环)的调用:

| call | kind | 退化形态 |
|---|---|---|
| call_002770 | target agent | `'\'\'\'…` 转义引号无限循环 |
| call_015723 | target agent | 同上 |
| call_009771/2/3, call_017703 | proposer | 800+ 连续制表符 |
| call_009805, call_017720 | editpipe3-draft | 800 连续同字符 |
| call_022331 | editpipe3-applier | 2 字符循环,响应 160K 字符 |
| call_025460, call_025506 | interpret | 尾部字符集仅 2 种 |

其余 77 条不是字符级退化,而是**语义级发散**(interpret 尤甚:无输入锚点 →
5–9 万字符长文)。两者的成因与对策不同,建议分开论。

## 五、材料结构

```
INDEX.md                     88 条一览(prompt/completion token、响应字符、轨迹块字符、真实轨迹条数)
optimizer/<call_id>/
    meta.json                usage、kind、匹配到的 traj_id/task_id/step/rollout、outcome
    system.md                系统提示词全文
    user.md                  用户提示词全文(interpret 类可见 "## The trajectory" 后为空)
    response.md              响应全文(未截断)
    interpretation_record.json   该调用落盘的解读产物(仅 interpret)
    real_trajectory/         该调用本应读到的真实轨迹
        conversation.json    完整对话
        result.json          评测结果
target/<call_id>/
    meta.json, messages.json(完整对话请求), response.md
    real_trajectory/conversation.json, result.json
```
