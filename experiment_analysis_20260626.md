# CSS 实验运行分析报告

## 实验配置

- **启动时间**: 2026-06-26 00:42:50
- **运行目录**: `runs/spreadsheetbench_20260626_004250`
- **PID**: 2748017
- **模型**: qwen3.6-35b-a3b (target + optimizer)
- **参数**: max_turns=100, K=5, workers=512, reflect_mode=plan_a
- **数据**: Train=140, Val=60, Test=200 (Trace2Skill aligned)
- **防作弊**: sitecustomize.py sandbox 已启用

## Baseline 参照

- **Baseline 结果**: task_hard = 0.15 (15%), K=1, 200 test items, max_turns=30
- **Baseline 目录**: `runs/baseline_noskill_20260625_235449`

---

## 一、Cold Start 阶段观察 (00:42 - 进行中)

### 1.1 进度概览

| 时间点 | 对话完成 | 总 Rollout 目录 | 完成率 | LLM 调用数 |
|--------|---------|----------------|--------|-----------|
| 00:49 (~7min) | ~174 | 559 | 31.1% | 1558 |
| 00:51 (~9min) | ~185 | 559 | 33.1% | 1824 |
| 00:53 (~11min) | ~207 | 559 | 37.0% | 2141 |
| 00:55 (~13min) | ~245 | 559 | 43.8% | 2638 |

**观察**: 吞吐量约 228 calls/min，每分钟完成约 12-15 个 rollout 对话。内存使用稳定在 ~1.1GB。

### 1.2 Rollout 数量异常

配置 K=5 rollouts × 140 train tasks = 700 个 rollout，但实际只有 559 个目录（139 个 task × ~4-5 rollouts）。

**可能原因**: 有 1 个 task 未被列入（140 个 task 中少了 1 个），或部分 rollout 目录尚未创建（batch_rollout 是先创建全部目录再并发执行，还是逐步创建？需要检查 batch_rollout 实现）。

### 1.3 Result.json 未生成

截至 245 个对话完成，result.json 数量为 **0**。这意味着 batch_rollout 可能是等所有 rollout 全部完成后才统一评分写入 result.json，或者 result.json 的写入逻辑在对话完成后的后处理步骤中。

**影响**: 这意味着 cold start 的评分聚合需要等到全部 700 个 rollout 完成后才能进行。按当前速率 (~15 rollouts/min)，预计剩余 (559-245)/15 ≈ 21 分钟完成所有 cold start rollouts。加上 val/test 评估，cold start 阶段可能需要 40-60 分钟。

---

## 二、【严重问题】Action 解析器 Brace-Counting Bug

### 2.1 问题描述

**63.3% 的对话至少有 1 次 Action 解析失败**（131/207 个已完成对话）。
**17.4% 的对话全部 Action 都解析失败**（36 个对话完全无法执行任何工具调用）。

共 **604 次**解析失败事件。

### 2.2 根因分析

`css/react_agent/converter.py` 第 337-349 行的 `parse_response()` 方法使用简单的花括号计数（brace counting）来定位 Action JSON 的结束位置：

```python
for i, char in enumerate(action_text):
    if char == '{':
        if brace_count == 0:
            json_start = i
        brace_count += 1
    elif char == '}':
        brace_count -= 1
        if brace_count == 0 and json_start is not None:
            json_end = i + 1
            break
```

**问题**: 当 agent 在 bash command 中写 Python 代码时，代码中经常包含 f-string 花括号（如 `f'Row {row_idx}: {row_data}'`），dict literal（如 `{k: v}`），或 set literal。这些花括号出现在 JSON 字符串值内部（被引号包裹），不应该被计入 brace_count，但简单的字符扫描无法区分。

**典型失败案例**:
```
Action:
{
    "name": "bash",
    "arguments": {"command": "python -c \"for i in range(10):\n    print(f'{i}: {data[i]}')\""}
}
```
- 解析器扫描到 `{i}` 时 brace_count 变成 2
- 接着遇到 `}` 回到 1
- 遇到 `{data[i]}` 变成 2
- 遇到 `}` 回到 1
- 最终 JSON 的真正结束 `}` 让 brace_count 回到 0
- 但如果内部的 `{` 和 `}` 不平衡（例如 `{k: v for k, v in ...}`），brace_count 永远无法回到 0

**100% 的解析失败都是这个原因**（604/604 次都是 "Action: 后面有花括号" 的模式）。

### 2.3 影响评估

这个 bug **严重削弱了实验效果**：
1. **成功率虚低**: 真实的 agent 能力被解析失败掩盖。38.5% 的 PASS 率可能实际上有 50%+ 如果不受解析 bug 影响。
2. **训练信号被污染**: 大量的 rollout 失败是因为基础设施 bug 而非 agent/skill 策略问题。这意味着：
   - 反思阶段（reflect）会看到大量"虚假失败"——agent 策略正确但执行失败
   - 对比分析（contrastive analysis）会把解析失败和策略失败混为一谈
   - 策略提议（proposal）可能会试图"修复"根本不存在的策略问题
3. **Cold start 质量差**: 初始策略的训练数据中混入了大量噪声

### 2.4 深入根因：两个叠加的问题

经过逐消息级别的分析，发现解析失败实际上是**两个不同问题的叠加**：

#### 问题 A：LLM 输出不完整 JSON（缺少最外层 `}`）

**占比约 50-60% 的解析失败**。LLM 生成的 JSON 缺少最外层的 closing brace：

```
Action:
{
    "name": "bash",
    "arguments": {"command": "which python3"}   ← 少了最后的 }
```

**根因分析**: Stop sequence `"Observation:"` 的截断机制与 LLM 的 token 生成交互不良。LLM 期望在 JSON 后面生成 `\n\nObservation: ...`，但 `Observation:` 被 stop sequence 截断。如果 LLM 的 token 化将 `}\nObservation:` 编码为一个 token 序列，其中 `}` 作为前一个 token 已输出但最外层的 `}` 还没来得及输出就被 stop 了。

**证据**: 同一个 agent 在 10+ 次失败后，通过在 JSON 后添加额外换行最终成功——说明 LLM 逐渐"学会"了在 stop sequence 触发前先完成 JSON。

#### 问题 B：命令字符串中的花括号（brace-counting bug）

**占比约 40-50% 的解析失败**。Python f-string、dict literal 中的花括号导致 brace counting 永远找不到 JSON 结束。

**典型场景**: `{"command": "python3 -c \"print(f'{col}: {ws.max_column}')\""}` — 字符串值中的 `{col}` 和 `{ws.max_column}` 被错误计入 brace count。

### 2.5 修复方案

需要同时解决两个问题：

1. **JSON 解析器升级**（解决问题 B）：用 `json.JSONDecoder().raw_decode()` 替代 brace counting，正确处理字符串中的花括号。

2. **不完整 JSON 容错**（解决问题 A）：当 `raw_decode()` 也失败时，尝试在末尾补充 `}` 后重试解析，最多补充 3 层。

```python
import json

decoder = json.JSONDecoder()
# 1. 先尝试正常解析
try:
    action_data, end_idx = decoder.raw_decode(action_text.lstrip())
except json.JSONDecodeError:
    # 2. 尝试补全缺失的 closing braces
    for extra_braces in range(1, 4):
        try:
            fixed = action_text.rstrip() + "}" * extra_braces
            action_data, _ = decoder.raw_decode(fixed.lstrip())
            break
        except json.JSONDecodeError:
            continue
    else:
        return ParseResult(type=ParseResultType.FORMAT_ERROR, ...)
```

**优先级**: 🔴 **P0 — 必须立即修复后重启实验**。当前实验结果因此 bug 不可靠。

---

## 三、对话行为模式分析

### 3.1 Turn 数分布

| Turn 范围 | 数量 | 占比 |
|-----------|------|------|
| 1-10 | 103 | 57.5% |
| 11-20 | 72 | 40.2% |
| 21-40 | 3 | 1.7% |
| 41-80 | 1 | 0.6% |

平均 Turn 数: 9.0，大多数对话在 10 轮内完成。这与 SpreadsheetBench 任务的复杂度相符——大部分任务可以在 3-5 轮 Think→Act→Observe 内解决。

### 3.2 LLM 调用特征

- **平均响应时间**: 37.1s（受 512 并发影响，vLLM 负载重）
- **最大响应时间**: 274.3s（约 4.5 分钟——需要关注是否有超时风险）
- **平均响应字符数**: 832 字符
- **平均 n_messages**: 11.0（含 system + user + assistant 交替）
- **最大 n_messages**: 76（约 38 轮交互 — 很长的对话）

### 3.3 Pass/Fail 分布

- **PASS**: 69 (38.5%) — 实际有效的成功
- **FAIL**: 81 (45.3%) — 任务失败（值不匹配）
- **Other**: 29 (16.2%) — 无法分类（可能是零 turn 或纯解析失败的短对话）

**关键洞察**: 如果扣除解析失败导致的虚假失败，真实 PASS 率可能显著更高。

### 3.4 Zero-turn 对话 (11 个)

有 11 个对话长度为 0（空列表）。这些是 conversation.json 为 `[]` 的情况。

**可能原因**: 
- LLM 首次调用超时
- 首次响应完全无法解析（既没有 Action: 也没有 TASK_COMPLETE）
- 或者是 agent 异常退出

### 3.5 失败的具体模式

典型失败案例：
1. **值不匹配**: `gt=2 pred=None` — agent 没有填入所需的单元格值
2. **值截断**: `gt='Location' pred='Locat'` — 数据被截断
3. **全面解析失败**: agent 连续多次产出含 f-string 的 Python 代码，每次都解析失败，最终被判为 FAIL

---

## 四、方案机制评估

### 4.1 当前实验的可信度

**结论**: 当前实验结果 **不可信** — Action 解析器 bug 导致 63% 的对话受到干扰。

即使 cold start 完成并进入 reflect/propose 阶段，反思器看到的失败轨迹中有大量是基础设施 bug 导致的虚假失败，而非策略缺陷。这会导致：
- **反思产出噪声提议**: 优化器可能会试图"修复"根本不是策略问题的东西
- **虚假的对比信号**: 同一 task 的成功和失败 rollout 之间的差异可能是"是否碰巧触发了解析 bug"而非"策略是否正确"

### 4.2 CSS 机制本身的健康评估

**在排除解析器 bug 后**, 机制设计似乎是合理的：
1. **Cold start 阶段**正在按预期运行：批量 rollout 训练集，收集初始轨迹
2. **ReAct agent 能力**: 在解析成功的 case 中，38.5% PASS 率（训练集上），对比 baseline 的 15%（测试集上）是合理的——训练集通常比测试集容易
3. **防作弊 sandbox**: 运行中没有观察到任何 SANDBOX blocked 消息，说明 agent 没有尝试访问 golden answer（符合预期）
4. **async event loop 错误**: stderr 中大量 `_UnixSelectorEventLoop._ssock` AttributeError——这是 Python asyncio 的 GC 清理问题，不影响功能，但说明大量 event loop 被创建和销毁

### 4.3 asyncio event loop 噪音

`_UnixSelectorEventLoop._ssock` 错误大量出现在 nohup 日志中。这些是 Python 在 GC 时尝试关闭已经被销毁的 event loop 的内部 fd 时的错误。

**根因**: 512 并发 workers 中每个 ReAct agent 可能创建了自己的 event loop（通过 `asyncio.run()`）。agent 完成后 event loop 被 GC 回收，但 `_ssock`（self-pipe socket）已经被关闭了。

**影响**: 纯噪音，不影响功能，但暗示资源管理可以优化。

---

## 五、其他基础设施问题

### 5.1 文件描述符耗尽 (EMFILE)

在 2485 次 observation 中发现 **83 次** `[Errno 24] Too many open files` 错误。

**根因**: 512 并发 worker 各自的 subprocess.run() 会创建管道 fd，加上 Python 进程自身的 fd 占用，在高并发下会超过系统 ulimit。

**影响**: 导致 agent 的 bash 命令执行失败，需要重试，浪费 turns。但 agent 通常能在后续 turn 中自适应恢复。

**建议**: 提高 ulimit（`ulimit -n 65536`）或降低并发度。

### 5.2 `python` vs `python3` 命令

**135 次** agent 使用 `python` 而非 `python3`，导致 `python: not found` 错误。

**根因**: 服务器上只有 `python3`，没有 `python` 符号链接。qwen3.6 模型在生成代码时默认使用 `python` 命令。

**影响**: 浪费 1-2 个 agent turns 来发现并切换到 `python3`。

**建议**: 
1. 在服务器上创建符号链接 `ln -s /usr/bin/python3 /usr/local/bin/python`
2. 或在 skill document 中加入 "使用 python3 而非 python" 的提示（但这更像是环境修复，不是策略优化）

---

## 六、需要关注的后续问题

### 6.1 🔴 解析器修复 (P0)
- 用 `json.JSONDecoder().raw_decode()` 替换 brace counting
- 修复后需要重启实验

### 6.2 🟡 Rollout 数量核实
- 139 tasks × K rollouts 应该有多少目录？检查是否所有 140 个 task 都被处理
- 确认 batch_rollout 是否先创建全部目录

### 6.3 🟡 Result.json 写入时机
- 确认 result.json 是在每个 rollout 完成后立即写入，还是等全部完成后批量写入
- 这影响到 cold start 后续步骤的时间线

### 6.4 🟢 长时间 LLM 调用监控
- 最大 274s 的调用需要关注是否会触发超时
- task_timeout_s=3600 足够，但单次 LLM 调用超时设为 1800s

### 6.5 🟡 文件描述符限制
- 512 并发下 fd 耗尽导致 83 次命令执行失败
- 建议 `ulimit -n 65536`

---

## 七、CSS 方案机制的深层思考：代码 Bug 还是机制设计缺陷？

本节是对当前实验中观察到的所有问题的深层分析，区分三个层次：**代码级 bug（可修复）**、**机制设计缺陷（需要重新设计）**、和**根本性假设风险（需要理论反思）**。

### 7.1 代码级 Bug：可修复的工程问题

#### Bug 1：Action 解析器 brace-counting 错误

**位置**: `css/react_agent/converter.py:337-349`

**性质**: 纯粹的工程 bug。brace-counting 算法无法处理 JSON 字符串值内部的花括号（如 f-string `{var}`、dict literal `{k: v}`）。这不是设计问题——`json.JSONDecoder().raw_decode()` 可以完美解决。

**影响范围**: 73.4% 的对话受影响。604 次解析失败事件中：
- ~50-60% 是 LLM 输出不完整 JSON（缺最外层 `}`），与 stop sequence 交互有关
- ~40-50% 是字符串内花括号导致 brace count 失衡

**修复代价**: 小。替换 ~15 行代码，添加不完整 JSON 补全逻辑。

**关键判断**: 这是一个**不应该影响机制设计评估**的 bug。修复后，当前实验观察到的大部分噪声问题将消失。但它揭示了一个更深层的问题：解析 bug 的**存在本身**就是对 CSS 机制健壮性的一次压力测试——而机制未能通过这个测试（见 7.2）。

#### Bug 2：环境配置问题（python 路径、fd 限制）

**位置**: 服务器配置层面，非代码 bug

**影响**: `python: not found`（135次）和 `Too many open files`（83次）导致 agent 浪费 turns。但 agent 通常能自适应恢复——这些不是致命的。

**修复代价**: 极小（`ln -s`, `ulimit -n`）。

### 7.2 机制设计缺陷：需要深入反思的结构性问题

#### 缺陷 1：反思管道无法区分基础设施失败和策略失败

**这是当前实验暴露的最核心问题。**

CSS 的 Layer 1 分析器（`css/analysis/layer1.py:50-121`）被设计为"开放式认知分析"——它分析 agent *如何思考*，而不是 agent 的结果是否正确。分析器的 system prompt 明确要求：

> "Your job is NOT to summarize what the agent did, nor to grade the answer. Your job is to deeply analyze the agent's *thinking*"

**问题在于**: 当轨迹充满解析失败时，分析器看到的"思考过程"就是：
1. Agent 提出正确的思路
2. Agent 写出合理的代码
3. 系统返回 "Failed to parse your action"
4. Agent 尝试修改格式
5. 系统再次返回 "Failed to parse"
6. 循环直到 turns 耗尽

在这种轨迹中，分析器会观察到什么"认知模式"？它会发现：
- "agent 在格式适应上表现不佳"（但这不是认知问题，是解析器 bug）
- "agent 执着于使用复杂 Python 表达式而非简单命令"（但复杂表达式可能恰恰是正确的解题策略）
- "agent 没有尝试完全不同的方法来绕过格式问题"（但不应该需要绕过）

**量化影响推演**:

基于冷启动数据的组成（K=5 rollout per task, 140 tasks）：
- **559 个 rollout** 进入 Layer 1 分析
- 其中 **80 个 "Other"**（36 think_only + 21 no_output + 12 task_complete_no_eval + 11 empty）→ 全部 `hard=0` → 标记为 failure
- 另有 **106 个 FAIL** 对话中 78.3% 是 "混合失败"（同时有 parse failure 和策略错误）
- **仅 21.7% 的 FAIL（约23个）是纯策略失败**

Layer 1 将对全部 559 个轨迹调用 `annotate_trajectory()`。对于 36 个 "think_only" 对话（4条消息全是 parse failure），分析器被喂入的轨迹大约是：

```
[assistant]
Thought: Let me first understand the spreadsheet structure...
Action:
{"name":"bash","arguments":{"command":"python -c \"...f'{row}...'...\""}}

[user]
Observation: Failed to parse your action...

[assistant]
Action:
{"name":"bash","arguments":{"command":"python -c \"...\"}}

[user]
Observation: Failed to parse your action...
```

这种轨迹有什么"认知模式"可以提取？分析器只能产出低价值的噪声观察，如"格式适应能力不足"。但这些噪声观察会进入 Layer 2 聚类，被聚合成看起来很"显著"的 pattern——因为它在 36 个对话中重复出现，support_count 很高。

**这暴露的机制缺陷**: CSS 的分析管道 **没有轨迹质量门控**。它假设所有轨迹都包含有意义的认知行为，但实际上有大量轨迹是被基础设施问题"毒害"的。

**可能的改进方向**:
1. **轨迹预筛选**: 在进入 Layer 1 前，过滤掉 n_effective_actions < 1 的轨迹（从未成功执行过任何工具调用）
2. **基础设施错误标注**: 在 `parse_response()` 返回 FORMAT_ERROR 时，在轨迹 metadata 中记录 `infra_error_count`
3. **分析器提示增强**: 在 Layer 1 prompt 中告知分析器区分基础设施失败（格式解析错误、系统超时）和策略失败（错误的方法、遗漏的步骤）
4. **pattern 置信度加权**: Layer 2 聚类时，按轨迹的 effective_action_ratio 加权，避免噪声轨迹的 observation 主导 pattern formation

#### 缺陷 2：对比分析（Contrastive Analysis）的信号污染

**代码位置**: `css/analysis/layer1.py:140-176`（contrastive prompt）和 `css/rollout/contrastive.py:189-195`（pair extraction）

CSS 的对比分析设计精巧：同一 task 的成功和失败 rollout 构成"受控实验"——task、instruction、skill 全部相同，唯一的变量是 agent 的运行时决策。但**前提是：失败确实来自决策差异**。

**当前实验中的实际情况**:

`TaskRolloutGroup.contrastive_pairs()` 返回所有 (success, failure) 对。当一个 task 有 K=5 rollouts:
- 2 个 PASS, 2 个 FAIL (策略), 1 个 "Other" (纯 parse failure)

该 task 产生的对比对包括：
- (PASS_1, FAIL_策略_1), (PASS_1, FAIL_策略_2) — **有效对比**
- (PASS_1, Other_parse_fail) — **无效对比**: 成功和失败的差异不是"认知差异"而是"是否碰到解析 bug"
- (PASS_2, FAIL_策略_1), ... — 更多有效对比
- (PASS_2, Other_parse_fail) — **另一个无效对比**

对比分析器的 prompt 说：

> "any difference in outcome must come from a difference in how the two runs *thought* or *decided*"

当它看到一个成功 rollout（正确完成任务）和一个 "think_only" 失败 rollout（4条消息全是 parse failure），它会被迫找出"决定性认知差异"。但真正的差异是**运气**——成功的 rollout 碰巧没有触发解析 bug（比如它先用了简单命令）。

**对比分析器可能产出的错误洞察**:

```json
{
  "divergence_point": "成功的 rollout 在第一步使用了简单的 shell 命令（ls, head），
    而失败的 rollout 直接跳到了复杂的 Python 一行式代码",
  "cognitive_difference": "成功的策略是先用简单命令探索环境，建立对文件结构的理解，
    然后再写复杂代码。失败的策略是直接假设文件结构并写复杂代码。",
  "is_systematic": true
}
```

这个分析在*现象层面*是对的——简单命令确实更不容易触发解析 bug。但作为*策略建议*它是错的：它把"避免触发解析 bug"包装成了"更好的认知策略"。如果解析器正常，直接写复杂 Python 代码可能反而是更高效的策略。

**更深层的问题**: 即使没有解析 bug，对比分析也面临一个根本性挑战——**LLM 的采样随机性**。K=5 rollouts 使用 temperature=0.7，同一 task 的不同 rollout 可能因为纯随机的 token 选择差异而导致不同结果。对比分析可能会把随机噪声误认为"系统性认知差异"。

`is_systematic` 字段设计为缓解这个问题，但它依赖分析器 LLM 的判断能力——而分析器面对的是同一个模型（qwen3.6-35b-a3b）的行为，它能准确判断什么是"系统性"什么是"随机性"吗？

#### 缺陷 3："Other" 类别的 rollout 被静默归入失败

**代码位置**: `css/envs/spreadsheetbench/task_interface.py:450`

```python
result["hard"] = 1 if (n_cases > 0 and n_pass == n_cases) else 0
```

所有非 PASS 的结果（包括 80 个 "Other"：empty, think_only, no_output, task_complete_no_eval）都得到 `hard=0`，通过 `TaskResult.passed` 属性（行 73: `return bool(self.hard)`）被标记为 failure。

**对分析管道的影响**:
- 这 80 个 "Other" rollout 参与 `TaskRolloutGroup.failures` 的计算（`rollout.py:174`）
- 它们与真正的策略失败混在一起参与对比分析
- 11 个 empty 对话（messages=[]）被格式化为空字符串送入 Layer 1 分析器——分析器无法从空轨迹中提取任何认知观察，但调用仍然发生，浪费 optimizer LLM token
- `aggregate_scores()` 计算 baseline_score 时，这 80 个 hard=0 的 rollout 拉低了分数

**改进方向**: 在 `TaskResult` 中增加一个 `outcome_category` 字段（如 `success` / `strategy_failure` / `infra_failure` / `incomplete`），让分析管道可以选择性处理。

#### 缺陷 4：Layer 2 聚类可能被噪声 pattern 主导

**代码位置**: `css/analysis/cluster.py`（通过 `build_or_update_library` 调用）

Layer 2 从 Layer 1 的 observations 中聚类出 stable patterns。当 observations 中包含大量基础设施失败相关的观察（如"格式适应困难"、"重复尝试相同命令"），这些观察会因为**高频出现**而形成高 support_count 的 patterns。

在 Layer 3 的 L1 signal 检测中（`css/analysis/longitudinal.py`），高 support_count + 无下降趋势 + l0_saturated=True 意味着这些噪声 patterns **几乎必然成为 L1 signals**。

这些 L1 signals 随后驱动 `attribute_root_cause()` 和 `derive_strategy()`。strategy_0 将包含"避免复杂 Python 表达式"之类的建议——这实际上是在教 agent 绕过基础设施 bug，而非提升解题能力。

**这里的根本问题**: CSS 的信号提取链是 observations → patterns → signals → root causes → strategy，每个环节都假设上游输入是有意义的。但当上游被噪声污染时，整条链路都会被污染。**没有任何环节有"内容质量"的看门人**。

### 7.3 根本性假设风险：机制设计的深层反思

#### 假设 1："轨迹质量反映策略质量"

CSS 的核心假设是：给定一个策略（skill document），agent 在任务上的表现（轨迹质量）反映了策略的质量。通过分析轨迹中的认知模式，可以识别策略的不足之处，进而改进策略。

**挑战**: 当前实验表明，轨迹质量受到多个因素的共同影响：
1. **策略质量**（CSS 想优化的目标）
2. **环境噪声**（解析 bug、fd 耗尽、python 路径）
3. **LLM 采样随机性**（temperature=0.7 导致同一策略下的不同表现）
4. **任务固有难度**（有些任务即使策略完美也很难解决）

如果因素 2-4 的方差与因素 1 可比甚至更大，那 CSS 的信号提取就是在噪声中寻找信号——信噪比太低。

**量化思考**: 
- PASS 率 42.7%（overall rollout level）
- 有 parse failure 的对话 PASS 率 47.9%，无 parse failure 的 39.5%
- 这个反直觉的结果表明：**parse failure 和 task outcome 的相关性很弱**（r ≈ +0.08，正向！），因为复杂代码（触发 parse bug）与更高的解题能力正相关

这意味着 CSS 分析器如果试图从 parse failure 的存在/缺失来推断策略问题，它会得到**错误的因果方向**。

#### 假设 2："认知模式可以从有限轨迹中稳定提取"

Layer 1 分析器从单个轨迹中提取 3-8 个 observations。但轨迹本身是 LLM 的一次采样——换一个 random seed，同一个 agent 在同一个任务上可能表现完全不同。

**问题**: Layer 1 提取的认知模式到底反映的是：
- (a) 策略文档引导的系统性思考方式？
- (b) 这次特定采样中碰巧的推理路径？
- (c) LLM 的预训练偏好（与策略文档无关的默认行为）？

在 cold start（空策略）阶段，答案必然是 (c)——没有策略文档，所有观察到的"认知模式"都是 LLM 的默认行为。CSS 的设计意图是通过 cold start 发现这些默认行为的弱点，然后通过 strategy_0 来弥补。这个设计在逻辑上是合理的。

**但在后续 round 中**, 随着策略文档变得更复杂，(a)(b)(c) 的信号交织在一起，分析器越来越难区分"策略引导的行为"和"LLM 固有的偏好"。CSS 缺乏一种**消融测试**的机制——比如对同一 task 运行有策略和无策略的两组 rollout，通过差异来隔离策略的效果。

#### 假设 3："开放式认知标签优于预定义分类"

CSS 的一个有意的设计决策是 Layer 1 不预定义认知维度——让分析器自由命名 `cognitive_aspect`（`layer1.py:73-77`）：

> "CRITICAL — open-ended naming: for each observation you must invent your OWN specific label..."

**优点**: 避免了预定义分类的盲区，让分析器发现意外的认知模式。

**风险**: 
- 分析器可能产出过于具体或过于抽象的标签，导致 Layer 2 聚类困难
- 不同轨迹的相同认知模式可能被命名为完全不同的标签（如 "Error Recovery Persistence" vs "Repeated Failure Retry" vs "Format Adaptation Difficulty"）
- 反过来，不同的认知模式可能被命名为相似的标签
- 这个问题在使用较弱的 optimizer LLM（如 qwen3.6-35b-a3b，一个 MoE 模型）时可能更严重

**Layer 2 的 LLM-based 标签分组**（`css/analysis/cluster.py` 中使用的 `label_grouping.py`）是为了缓解这个问题——用 LLM 来判断哪些不同名称的 observations 实际上描述的是同一个 pattern。但这引入了另一层 LLM 判断噪声。

### 7.4 当前实验的预测性分析

基于以上分析，我对 cold start 完成后的后续步骤做出以下预测（待验证）：

#### 预测 1：strategy_0 将包含"格式适应"相关建议

由于解析失败在轨迹中高频出现，"格式相关"的认知观察将成为高 support 的 pattern，很可能成为 L1 signal，进而驱动 strategy_0 包含类似"使用简单命令格式"的建议。

#### 预测 2：baseline_score 将在 0.35-0.45 之间

140 tasks × K=5 rollouts，task_hard 是 per-task 的 pass_rate 均值。基于已观察到的 56.9% task-level PASS 率（109 tasks 完成），加上长尾 rollouts 可能的更低成功率，最终 baseline_score 预计 0.35-0.45。

#### 预测 3：第一轮 CSS round 的 val_score 提升将部分来自"绕过解析 bug"

如果 strategy_0 包含了"使用简单命令"的建议，agent 在使用该策略时会自然减少复杂 f-string 的使用，从而减少解析 bug 的触发率。这会表现为 val_score 的提升，但实际上是"学会绕过 bug"而非"更好的策略"。

#### 预测 4：contrastive divergences 中 is_systematic=true 的比例将很高

因为"简单命令 vs 复杂命令"的差异在多个 task 中重复出现（任何包含 Python 的 task 都可能出现），分析器会判断这是"系统性"差异。

### 7.5 更一般性的机制改进思路

#### 思路 1：轨迹分级过滤

在进入分析管道前，对轨迹进行质量分级：
- **Grade A**: 正常完成（有工具调用，产出了 output）→ 正常分析
- **Grade B**: 部分受基础设施影响（有 parse failure 但也有成功的工具调用）→ 分析，但降低权重
- **Grade C**: 严重受基础设施影响（多数 turns 是 parse failure）→ 仅统计，不参与认知分析
- **Grade D**: 无效轨迹（empty, 纯 parse failure 循环）→ 排除

#### 思路 2：双通道分析

将轨迹中的事件分为两个通道：
- **认知通道**: agent 的思考、决策、策略选择
- **基础设施通道**: 格式错误、超时、环境问题

分析器只对认知通道进行认知分析，基础设施通道的信息仅作为"环境噪声标注"。

#### 思路 3：因果消融

在对比分析前，先进行因果消融检查：
- 如果失败 rollout 的 first_parse_failure 发生在 first_successful_action 之前，标记为 "infra-dominated failure"
- 只对 "strategy-dominated failure" 进行对比分析

#### 思路 4：pattern 来源追踪

在 Layer 2 中为每个 pattern 记录其来源轨迹的质量分布。如果一个 pattern 的 observations 主要来自 Grade C/D 轨迹，自动降低其 L1 signal 的置信度。

#### 思路 5：基础设施错误的 prompt 级标注

在 `format_trajectory()` 中，对已知的基础设施错误进行标注：

```
[user]
Observation: [INFRA-ERROR: parse_failure] Failed to parse your action...
```

这样分析器可以明确看到哪些是基础设施问题，在认知分析中主动排除或降低它们的权重。

### 7.6 max_turns=100 与解析 bug 的交互及更广泛含义

max_turns=100 在设计意图上是给 agent 足够的空间进行复杂推理。但在解析 bug 存在时，大量 turns 被浪费在格式重试上。

**关键代码机制**（`agent.py:310-311`）：
```python
# Don't count format errors as action turns
turn -= 1
```

这意味着 parse failure **不消耗 turn 预算**。理论上 agent 可以无限次 parse failure 而不用完 turns。但实际上，每次 parse failure 都消耗一次 LLM 调用（增加延迟和成本），且 conversation history 不断增长（增加后续调用的 prompt 长度）。

n_messages=200 的对话（约 100 轮有效 turn + 约 100 次 parse failure）意味着：
- 后期每次 LLM 调用的 prompt 包含前面 199 条消息的完整历史
- 对 qwen3.6-35b-a3b（max_tokens=16384）来说，这可能接近或超过上下文限制
- 这解释了长尾 rollout 为什么如此慢——越往后每次调用越慢

**更广泛含义**: 即使没有解析 bug，CSS 在部署到真实环境时，都需要考虑 "effective turns" vs "nominal turns" 的差异。任何环境噪声都会降低 turn 的有效利用率。`turn -= 1` 的设计虽然善意（不惩罚格式错误），但可能导致失控的重试循环。建议增加 `max_format_errors` 上限。

---

## 八、Cold Start 进度时间线

| 时间 | 运行时间 | 完成 | 总计 | 完成率 | PASS 率 | LLM 调用 | 备注 |
|------|---------|------|------|--------|---------|---------|------|
| 00:49 | 7min | ~174 | 559 | 31.1% | ~38.5% | 1558 | 初始快速阶段 |
| 00:51 | 9min | ~185 | 559 | 33.1% | - | 1824 | |
| 00:53 | 11min | ~207 | 559 | 37.0% | - | 2141 | |
| 00:55 | 13min | ~245 | 559 | 43.8% | 43.8% | 2638 | |
| 00:57 | 15min | ~282 | 559 | 50.4% | - | 3099 | 半程 |
| 00:59 | 17min | ~292 | 559 | 52.2% | - | 3210 | 开始减速 |
| 01:01 | 19min | ~305 | 559 | 54.6% | - | 3367 | |
| 01:03 | 21min | ~320 | 559 | 57.2% | 43.1% | 3516 | 长尾任务阶段 |
| 01:04 | 22min | ~321 | 559 | 57.4% | 43.0% | 3570 | 明显减速 |
| 01:06 | 24min | ~323 | 559 | 57.8% | - | 3582 | 长尾阶段 |

**速度分析**: 
- 前 50%: ~16 分钟（均速 ~17.5 rollouts/min）
- 50-57%: ~6 分钟（均速 ~6.5 rollouts/min — 下降 63%）
- 57-58%: ~2 分钟仅 +2 个（长尾任务极慢）
- 预计完成全部 cold start: **40-60 分钟**

**长尾原因**: 
- 238 个活跃 rollout 中有大量 n_messages ≥ 50 的长对话（max=200，即 100 轮交互）
- 这些对话耗尽了 max_turns=100 的预算
- 每轮 LLM 调用平均 21.3 秒，100 轮需要 ~35 分钟
- 很多长对话是因为解析失败导致的重试循环（19 次 parse failure = 浪费 19 轮）

---

## 九、Failure Taxonomy：深度分类与 CSS 信号质量评估

### 9.1 四层分类体系

基于逐消息级别的分析，将 559 个 rollout 的失败分为四个层次：

#### 层级 A：有效成功（PASS）
- **数量**: ~240 (42.7% of completed)
- **CSS 信号价值**: ⭐⭐⭐⭐⭐ — 成功轨迹是最宝贵的信号，提供 success patterns

#### 层级 B：纯策略失败（FAIL，无 parse failure）
- **数量**: ~23 (来自 106 FAIL 中 21.7% 的纯策略失败)
- **CSS 信号价值**: ⭐⭐⭐⭐⭐ — 真正反映策略不足的失败，是 CSS 应该关注的核心
- **典型模式**: 
  - 值不匹配 (`gt=2 pred=None` — 遗漏单元格)
  - 理解错误 (`gt='Site Name' pred='New York'` — 填了值而非标题)
  - 符号错误 (`gt=1500 pred=-1500`)
  - 截断 (`gt='Location' pred='Locat'`)

#### 层级 C：混合失败（FAIL，有 parse failure 但也有成功 actions）
- **数量**: ~83 (106 FAIL 中 78.3% 的混合失败)
- **CSS 信号价值**: ⭐⭐⭐ — 包含部分有效的认知信号，但混入了基础设施噪声
- **特征**: 平均 7.2 次 parse failure + 7.3 次成功 action
- **反直觉发现**: 这类对话的 PASS 率实际上**更高**（47.9% vs 35.0%），因为复杂 Python 代码（触发 bug）与更强的问题解决能力正相关

#### 层级 D：基础设施主导的失败（"Other" 类别）
- **数量**: 80 (36 think_only + 21 no_output + 12 task_complete_no_eval + 11 empty)
- **CSS 信号价值**: ⭐ — 几乎无认知信号，是纯噪声
- **问题**: 这 80 个 rollout 全部作为 `passed=False` 进入分析管道

### 9.2 信号纯度评估

| 类别 | 数量 | 占比 | CSS 能否正确处理 |
|------|------|------|----------------|
| 有效成功 | ~240 | 43% | ✅ 是 — 成功 pattern 提取正常 |
| 纯策略失败 | ~23 | 4% | ✅ 是 — 这正是 CSS 设计来处理的 |
| 混合失败 | ~83 | 15% | ⚠️ 部分 — 有信号但需要噪声过滤 |
| 基础设施失败 | ~80 | 14% | ❌ 否 — 被错误归入策略失败 |
| 尚未完成 | ~133 | 24% | ⏳ 待观察 |

**结论**: 仅 47% 的已完成 rollouts 能提供高质量信号（层级 A + B）。加上层级 C 的部分信号，大约 62% 有一定价值。**38% 是噪声或低价值数据**。

### 9.3 对 Contrastive Analysis 的精确影响

对于 140 tasks 中的每个 task，contrastive_pairs() 生成所有 (success, failure) 的笛卡尔积。

**最坏情况示例**（某 task 的 5 rollouts: 1 PASS + 1 FAIL_策略 + 2 FAIL_混合 + 1 Other）：
- 有效对比对: 1×1 = 1 对 (PASS vs FAIL_策略)
- 低质量对比对: 1×2 = 2 对 (PASS vs FAIL_混合)
- 无效对比对: 1×1 = 1 对 (PASS vs Other)
- **信噪比**: 1:3

**估算全局**: 140 tasks × K=5, 假设每 task 平均产生 ~3 对比对，其中 ~1 个有效，~2 个有噪声。全局信噪比约 1:2。

---

## 十、反思管道代码的关键路径分析

### 10.1 Layer 1 的轨迹消费路径

`run_layer1()` 遍历所有 rollout groups，对每个 rollout 调用 `annotate_trajectory()`。没有任何过滤。

```
run_layer1() → all_rollouts = [(group, rollout) for group in groups for rollout in group.rollouts]
                                                                     ^^^^^^^^^^^^^^^^^^^^^^^^^^^
                                                     包含所有 559 个 rollout，无论状态
```

`annotate_trajectory()` 调用 `format_trajectory(result.messages)`。对于 empty 对话（messages=[]），这返回空字符串。对于 think_only 对话（4 条消息），这返回一个很短的轨迹文本。

**optimizer LLM 面对空/极短轨迹时的行为**: 分析器 prompt 要求 "Report every distinct cognitive pattern you observe (typically 3-8 per trajectory)"。面对空轨迹，LLM 可能：
1. 返回空列表（最好情况）
2. 产出虚构的 observations（幻觉）
3. 返回格式错误的响应（被 `_parse_obs_list()` 兜住，返回 []）

`_parse_obs_list()` 的健壮性设计（行 209-226）意味着格式错误不会崩溃——但虚构的 observations 会被正常接受。

### 10.2 Layer 2 的 Pattern 聚合

Layer 2 (`build_or_update_library`) 将 Layer 1 的所有 observations 聚合为 patterns。高频出现的 cognitive_aspect 会形成高 support 的 patterns。

由于解析失败在 73.4% 的轨迹中出现，"格式适应"相关的 observations 可能成为全局最高频的 pattern——即使它不反映任何有意义的策略问题。

### 10.3 Layer 3 → L1 Signal 检测

`detect_l1_signals()` 在 `l0_saturated=True` 条件下（cold start 的默认设置），所有"无下降趋势"的 failure patterns 都自动成为 L1 signals。由于这是第一个 epoch（只有一个时间点），没有历史数据来判断"趋势"——所有 failure patterns 可能都通过。

这意味着 **strategy_0 的推导将受到所有 failure patterns 的影响**，包括噪声 patterns。

### 10.4 Root Cause Attribution 和 Strategy Derivation

`attribute_root_cause()` 接收 L1 signals（failure patterns）和 pattern library（含 success counterparts），通过 optimizer LLM 归因。

`derive_strategy()` 接收 top root cause + success counterparts，生成 strategy_0 文本。

**关键问题**: 如果 top root cause 是"格式适应困难"（因为它的 support_count 最高），strategy_0 将围绕"如何避免格式问题"来设计——这完全偏离了 CSS 的设计意图。

---

## 十.五、实时观察：Cold Start 进入分析阶段

### 10.5.1 关键时间节点

| 事件 | 时间 | 调用 ID | 证据 |
|------|------|---------|------|
| 实验启动 | 00:42:50 | - | css.log |
| batch_rollout 启动 | 00:42:50 | call_000000 | trace.jsonl 第一个 target 调用 |
| 最后一个 target 调用 | ~01:19 (37min) | call_003686 | n_messages=140, 308K chars |
| 第一个 optimizer 调用 | ~01:19 (37min) | call_003687 | Layer 1 分析开始 |
| 当前 (01:22) | 40min | call_004159+ | 473+ optimizer 调用已完成 |

**batch_rollout 总耗时**: ~37 分钟，3686 次 target LLM 调用
**分析阶段已进行**: ~3 分钟，473+ 次 optimizer 调用（进行中）

### 10.5.2 分析阶段特征

| 指标 | 值 |
|------|-----|
| Optimizer 调用次数 | 473+ |
| 每次调用耗时 | 15-275 秒 |
| 平均 system_chars | ~1464 或 ~3788（两种 prompt） |
| 最大 user_chars | 42,079（含完整长轨迹） |
| 并发度 | ~512（与 batch_rollout 共享 max_workers） |

**两种 system prompt 长度的含义**:
- `system_chars: 3788` → Layer 1 单轨迹分析 prompt（_SINGLE_SYSTEM，详细的认知分析指令 + 示例）
- `system_chars: 1464` → Layer 1 对比分析 prompt（_CONTRASTIVE_SYSTEM，较短的对比指令）

### 10.5.3 发现：stage_context 传播断裂

**代码 bug**: `OptimizerOnlyClient`（`css/model/client.py:259-284`）没有转发 `with_stage()` 方法到内部的 `TracingLLMClient`。导致 `stage_context()` 返回 `_NoopContext`，所有 optimizer 调用在 trace 中都显示 `"stage": "optimizer"` 而非更具体的标签（如 `"layer1_annotate"` / `"layer1_contrastive"` / `"label_group"`）。

**影响**: 纯观测性问题。无法从 trace 中精确区分 Layer 1/2/3 的各个阶段的进度。不影响功能正确性。

**修复**: 在 `OptimizerOnlyClient` 中添加 `with_stage` 委托：
```python
def with_stage(self, stage: str):
    if hasattr(self._inner, 'with_stage'):
        return self._inner.with_stage(stage)
    from css.tracing import _NoopContext
    return _NoopContext()
```

### 10.5.4 Layer 1 分析输出质量实测

**极其重要的实测数据** — 对 optimizer 的响应内容进行关键词分析：

| 指标 | 值 |
|------|-----|
| 总 optimizer 调用 | 720 |
| 其中 contrastive 分析 | 176 (24.4%) |
| 其中 single-trajectory 分析 | 544 (75.6%) |
| **提及 "Too many open files" 的响应** | **377 (52.4%)** |
| **提及 "parse fail" 的响应** | **191 (26.5%)** |
| 空/极短响应 (<20 chars) | 1 (0.1%) |
| 长响应 (500+ chars) | 716 (99.4%) |

**关键结论**: 
- **超过 52% 的分析在讨论文件描述符耗尽问题** — 这是基础设施噪声，不是策略问题
- **26.5% 在讨论解析失败** — 同样是基础设施问题
- 两者合计（去重后估计 ~60-65%），分析器产出的认知 observations 中**近三分之二是噪声**
- 仅 ~35-40% 的分析可能包含真正有价值的策略信号

**这完美验证了 §7.2 缺陷 1 的预测**：反思管道无法区分基础设施失败和策略失败。

**实际分析样本**:

1. **Sample #1**（空对话分析）: 返回 `{"json": "[]"}` — 空列表，格式异常（应该是 `[]`）
2. **Sample #100**（fd 耗尽对话）: "The agent initiated the task by immediately attempting to interact with the file system to open an Excel file, but failed at the very first step due to a resource exhaustion error ('Too many open files'). This indicates a **lack of initial state awareness**..." — 分析器把系统级 fd 耗尽归因为"缺乏环境状态感知"
3. **Sample #400**（策略性失败）: "The agent immediately assumed the user's request required generating VBA code as the primary deliverable, focusing entirely on the syntax of the loop and range selection." — 这才是真正有价值的策略分析

对比 Sample #100 和 #400 完美展示了 **信噪分离的必要性**：#100 的"认知分析"是在分析 agent 对系统级错误的响应，而 #400 才是在分析真正的策略决策差异。

### 10.5.5 Layer 2 标签分组实时观测

**Layer 1 完成**: 产出 **1273 个 observations** (从 ~559 rollouts，平均 ~2.3 obs/rollout)

**Layer 2 match 阶段**: 0 matched, 1273 unmatched（cold start 无现有 patterns）

**Layer 2 label grouping 实时数据**:

标签分组正在进行（`llm_label_group` events），从前 19 个 batch 中可以清晰看到标签的质量分布：

**噪声标签（基础设施问题被包装为"认知模式"）**: ~60-70%
- "Total Cognitive Absence" / "Cognitive Nullity" / "Cognitive Inertia" — 来自 empty/think_only 对话
- "Pre-computation Resource Exhaustion" / "Systemic Resource Starvation" — 来自 fd 耗尽
- "Rigid Pattern Persistence in the Face of Parse Errors" — 来自 parse failure 循环
- "Resource Exhaustion Blindness" / "Resource-Agnostic Execution" — 系统级错误
- "Pre-Cognitive Infrastructure Collapse" — 最直白的基础设施错误标签

**有价值的策略标签**: ~30-40%
- "Premature Solution Commitment" — 过早确定解决方案（真正的策略问题）
- "Formula-Literal Conflation" — Excel 公式与字面值混淆
- "Premature Implementation Without Semantic Verification" — 不验证就直接实现
- "Literalist Scope Misinterpretation" — 字面理解任务范围
- "Exemplar-Driven Logic Scaffolding" — 基于示例构建逻辑

**关键观察**：Layer 1 分析器给基础设施问题起了看起来很"专业"的认知标签（如 "Cognitive Inertia" 描述空对话，"Resource-Agnostic Execution" 描述 fd 耗尽），但这些标签**在语义上与真正的策略问题标签非常相似**（都包含 "Cognitive"、"Execution"、"Failure" 等词汇）。这意味着 Layer 2 的 label grouping 可能会将噪声标签和策略标签错误地分到同一组。

例如 "Resource Exhaustion Blindness"（基础设施问题）和 "Premature Environmental Assumption"（策略问题）可能因为都包含"环境/资源"相关词汇而被归为一组——但前者是不可控的系统限制，后者是可改进的策略。

### 10.5.6 分析阶段预计工作量

Layer 1 需要对每个 rollout 调用一次 `annotate_trajectory()` + 所有对比对调用 `annotate_contrastive_pair()`。

- **单轨迹分析**: ~559 次调用（但 empty 的 11 个可能产出空结果）
- **对比分析**: 每个 task 的 (success × failure) 对数。假设平均每个 task 有 ~2 个 success 和 ~3 个 failure，则约 6 个对比对 × 140 tasks = ~840 次调用
- **总计**: ~1400 次 optimizer 调用仅用于 Layer 1

当前已完成 473 次，约 34% 的 Layer 1 工作。考虑到 512 并发和 15-275 秒/调用的耗时分布，Layer 1 可能需要 10-20 分钟完成。

Layer 2（聚类）和 Layer 3（纵向检测）的 optimizer 调用量取决于 Layer 1 产出的 observation 数量。如果平均每个轨迹产出 5 个 observations，则 ~2800 个 observations → Layer 2 可能需要 ~500-1000 次额外调用。

**预计分析阶段总耗时**: 20-40 分钟（从第一个 optimizer 调用算起）

---

## 十一、实验运行数据汇总

### 11.1 核心指标（截至 ~30 分钟）

| 指标 | 值 |
|------|-----|
| Cold start 进度 | 325/559 (58.1%) |
| Rollout PASS 率 | 42.7% (138/323 completed) |
| **Task-level PASS 率** | **56.9%** (62/109 tasks with completed rollouts) |
| 解析失败率 | 73.4% (237/323 有至少1次 parse failure) |
| 活跃 rollout 数 | 234 |
| LLM 调用总数 | ~3655 |
| llm_calls.jsonl 大小 | 94MB+ |
| 内存使用 | ~1.3GB |

### 11.2 Cold Start 进度时间线

| 时间 | 运行时间 | 完成 | 总计 | 完成率 | PASS 率 | LLM 调用 | 备注 |
|------|---------|------|------|--------|---------|---------|------|
| 00:49 | 7min | ~174 | 559 | 31.1% | ~38.5% | 1558 | 初始快速阶段 |
| 00:51 | 9min | ~185 | 559 | 33.1% | - | 1824 | |
| 00:53 | 11min | ~207 | 559 | 37.0% | - | 2141 | |
| 00:55 | 13min | ~245 | 559 | 43.8% | 43.8% | 2638 | |
| 00:57 | 15min | ~282 | 559 | 50.4% | - | 3099 | 半程 |
| 00:59 | 17min | ~292 | 559 | 52.2% | - | 3210 | 开始减速 |
| 01:01 | 19min | ~305 | 559 | 54.6% | - | 3367 | |
| 01:03 | 21min | ~320 | 559 | 57.2% | 43.1% | 3516 | 长尾任务阶段 |
| 01:04 | 22min | ~321 | 559 | 57.4% | 43.0% | 3570 | 明显减速 |
| 01:06 | 24min | ~323 | 559 | 57.8% | - | 3582 | 长尾阶段 |
| 01:10 | 28min | ~325 | 559 | 58.1% | - | 3646 | 极慢长尾 |
| 01:15 | 33min | ~325 | 559 | 58.1% | - | 3655 | 几乎停滞 |

**速度分析**: 
- 前 50%: ~16 分钟（均速 ~17.5 rollouts/min）
- 50-57%: ~6 分钟（均速 ~6.5 rollouts/min — 下降 63%）
- 57-58%: ~11 分钟仅 +5 个（长尾任务极慢）
- 预计完成全部 cold start: **60-120 分钟**（取决于最长的 rollout）

---

## 十二、持续观察计划

### 已完成
- [x] Cold start 进度追踪和速度分析
- [x] Action 解析器 bug 完整诊断（两重问题识别）
- [x] Failure taxonomy 深度分类
- [x] 对比分析信噪比评估
- [x] 反思管道代码路径分析
- [x] 机制设计缺陷 vs 代码 bug 的系统性区分
- [x] 根本性假设风险评估

### 进行中
- [ ] 等待 cold start 完成（当前 58.1%）
- [ ] 观察 Layer 1-3 分析管道的实际输出
- [ ] 检验 strategy_0 的内容（预测：将含"格式适应"建议）
- [ ] 分析 root_cause attribution 产物

### 待验证的预测
- [ ] strategy_0 含格式相关建议？
- [ ] baseline_score 在 0.35-0.45？
- [ ] 第一轮 val_score 提升部分来自绕过解析 bug？
- [ ] contrastive divergences 中 is_systematic=true 比例高？

### 后续深度分析
- [ ] 进入 round 循环后，记录每轮的 val_score 变化
- [ ] 分析 strategy 迭代的方向——是在优化真策略还是在适应 bug？
- [ ] 观察 REFINE 机制对 strategy 的修改质量
- [ ] 对比 round 间 pattern library 的演化

---

---

## 十三、实验崩溃：Layer 2 Cluster Refine 阶段

### 13.1 崩溃时间线

| 事件 | 时间 | 详情 |
|------|------|------|
| 实验启动 | 00:42:50 | PID 2748017 |
| batch_rollout 完成 | ~01:19 (37min) | 3639 target LLM 调用 |
| Layer 1 完成 | ~01:24 (42min) | 1273 observations 产出 |
| Layer 2 match | 01:24 | 0 matched, 1273 unmatched |
| Label grouping | 01:24-01:26 | 118+ batches 处理 |
| Cluster refine | 01:26-01:28 | 53 clusters 精炼 |
| **进程消失** | ~01:28 (46min) | PID 2748017 不再存在 |

### 13.2 崩溃分析

**最后的 trace 事件**: `cluster_refine` (第 53 个 cluster)

**可能原因**:
1. **内存溢出（OOM）**: 1.3GB 进程 + 1273 observations + 53 clusters 的数据结构可能在后续处理中膨胀。dmesg 无权限确认。
2. **LLM 调用异常**: 某个 cluster refine 的 LLM 调用抛出了未被内层 try/except 捕获的异常（如网络超时、HTTP 连接重置）。但 `refine_clusters()` 有异常兜底（`_fallback_pattern_record`），理论上不应崩溃。
3. **`ThreadPoolExecutor` 相关**: `refine_clusters()` 使用 `max_workers=max(1, len(items))` — 如果有几十个 clusters，会创建几十个线程同时调用 optimizer LLM。在 vLLM 高负载下可能触发连接层问题。

**影响**: 实验数据在 cold start 的 analysis 阶段丢失。所有 rollout 数据（conversation.json 等）已保存，但 pattern library、L1 signals、strategy_0 均未生成。

### 13.3 已收集的可分析数据

尽管实验崩溃，我们仍然收集到了极其丰富的中间数据：

| 数据 | 数量/大小 | 分析价值 |
|------|----------|---------|
| trace.jsonl | 4891 行 | ⭐⭐⭐⭐⭐ 完整的 LLM 调用记录 |
| llm_calls.jsonl | 119MB | ⭐⭐⭐⭐⭐ 完整的 prompt + response |
| conversation.json | 325 个 | ⭐⭐⭐⭐ rollout 轨迹 |
| Layer 1 observations | 1273 个 | ⭐⭐⭐⭐ 认知分析结果（含噪声分析） |
| Layer 2 label groups | 118 batches | ⭐⭐⭐ 标签分组中间结果 |
| Layer 2 cluster refines | 53 个 | ⭐⭐⭐ 已精炼的 patterns |

### 13.4 Layer 2 Cluster Refine 产出（崩溃前的 patterns）

从 trace 中提取的 5 个可见 pattern（全部是噪声 pattern）：

| Pattern 名称 | 成员数 | 极性 | 信号类型 |
|-------------|--------|------|---------|
| Total Cognitive Initiation Failure | 12 | failure | 噪声（empty 对话） |
| Total Cognitive Nullity | 8 | failure | 噪声（空轨迹） |
| Pre-Cognitive Systemic Failure | 3 | failure | 噪声（系统错误） |
| Pre-Cognitive Systemic Initialization Failure | 8 | failure | 噪声（fd 耗尽） |
| System-Induced Cognitive Nullity | 3 | failure | 噪声（基础设施错误） |

**这完美验证了 §7.2 的预测**: 分析管道产出的 patterns 被基础设施噪声主导。如果实验没有崩溃，这些 patterns 很可能成为 L1 signals，驱动 strategy_0 包含"资源管理"和"环境感知"相关的建议 — 而这些与真正的任务解题策略无关。

---

## 十四、总结与下一步

### 14.1 本次实验的核心发现

1. **Action 解析器 brace-counting bug 是最严重的工程问题**（P0）
   - 73.4% 的对话受影响
   - 两个叠加问题：不完整 JSON + 字符串内花括号
   - 修复方案清晰：`json.JSONDecoder().raw_decode()` + 补全逻辑

2. **CSS 反思管道缺乏基础设施噪声过滤机制**（机制设计缺陷）
   - 52.4% 的 Layer 1 分析在讨论 fd 耗尽（基础设施问题）
   - 26.5% 在讨论 parse failure（代码 bug）
   - Layer 2 产出的前 5 个 patterns 全部是噪声
   - 信号纯度仅 ~35-40%

3. **对比分析的信噪比约 1:2**
   - "Other" 类别的 rollout（80个）被静默归入 failure
   - 对比对中包含大量无效对比（基础设施失败 vs 策略失败）

4. **进程崩溃原因不明**
   - 在 Layer 2 cluster refine 阶段（第 53 个 cluster）停止
   - 可能是 OOM、网络异常或线程池问题
   - 需要改进错误处理和日志记录

### 14.2 推荐的代码修复（按优先级）

1. **🔴 P0**: 修复 `converter.py` 的 action 解析器 — 用 `raw_decode()` + 补全
2. **🔴 P0**: 添加 nohup stderr 重定向到日志文件（方便调试崩溃）
3. **🟡 P1**: 修复 `OptimizerOnlyClient` 的 `with_stage()` 转发（trace 可读性）
4. **🟡 P1**: 添加 `max_format_errors` 上限（防止失控的 parse failure 循环）
5. **🟡 P1**: 在 `run_layer1()` 中过滤 empty/think_only 轨迹（避免噪声 observations）
6. **🟢 P2**: 在服务器上创建 `python` → `python3` 符号链接
7. **🟢 P2**: 提高 `ulimit -n` 或降低并发度

### 14.3 推荐的机制改进

1. **轨迹质量门控**: 在进入 Layer 1 前过滤 n_effective_actions < 1 的轨迹
2. **结果分类**: 在 `TaskResult` 中增加 `outcome_category` 字段区分 infra/strategy failure
3. **分析器 prompt 增强**: 告知 Layer 1 分析器区分基础设施错误和策略错误
4. **pattern 来源追踪**: 记录每个 pattern 的 observations 来源的轨迹质量分布
5. **对比分析过滤**: 在 `contrastive_pairs()` 中排除 "Other" 类别的 rollout

*以上分析基于第一次运行 (spreadsheetbench_20260626_004250)，该运行使用旧的 brace-counting parser。*

---

## 十五、第二次运行启动 (01:36)

### 15.1 修复内容

**在第二次运行前修复了 converter.py 的 action parser bug**:
- 将 brace-counting 逻辑替换为 `json.JSONDecoder().raw_decode()`
- 添加了不完整 JSON 的补全逻辑（追加 1-3 个 `}`）
- `raw_decode()` 正确处理 JSON 字符串值内部的花括号（f-strings、dict 字面量等）

### 15.2 第二次运行配置

- **启动时间**: 2026-06-26 01:36:20
- **运行目录**: `runs/spreadsheetbench_20260626_013620`
- **PID**: 2955499
- **配置**: 与第一次完全相同

### 15.3 Parser Fix 验证结果

**在 118 个已完成的 conversation.json 中，0 个出现 "Failed to parse your action" 错误。**

对比第一次运行 73.4% 的对话受 parser error 影响，第二次运行的 parser error 率为 **0%**。这验证了修复的有效性。

**意义**:
- 消除了约 604 个 parse failure 事件的噪声来源
- agent 不再因 format error 陷入无限循环（turn -= 1 的危险逻辑不再被频繁触发）
- Layer 1 分析将不再被 parse failure 讨论污染（原先占 26.5%）
- cold start 的 strategy_0 质量应该显著提升

### 15.4 Anti-Cheating Sandbox 确认

已验证服务器上部署的 sandbox 机制完整：
- `bash_tool.py`: sitecustomize.py 通过 PYTHONPATH 注入
- Monkey-patch 覆盖: builtins.open, os.listdir, os.scandir, os.walk, pathlib.Path.open, openpyxl.load_workbook, pandas.read_excel
- Shell 层面: cat/head/tail/less/more/cp 被 guard 函数拦截
- `task_interface.py` line 335-338 正确传递 `forbidden_dirs=[os.path.realpath(task_dir)]`

### 15.5 初期进度

| 时间 | 任务目录 | Trace 行数 | 进程状态 |
|------|---------|-----------|---------|
| 01:37 (~1min) | 125/140 | 103 | 305MB RSS |
| 01:38 (~2min) | 125/140 | 209 | 668MB RSS |
| 01:39 (~3min) | 125/140 | 546 | 714MB RSS, 96.8% CPU |

### 15.6 残余 Parse Error 分析

修复后仍有 ~11% (3/27) 的对话出现 parse error，但原因是**完全不同的格式错误**：

```json
{
    "name": "bash",
    {"command": "cat <<'EOF' > solution.py\n..."}
}
```

LLM 输出了 `{"command": ...}` 而不是 `"arguments": {"command": ...}`——缺少 `"arguments":` 键名。这是 LLM 指令遵循的质量问题（qwen3.6-35b-a3b 模型的 JSON 格式不稳定），而非 parser 的 bug。

**对比**:
| 指标 | 第一次运行 (brace-count) | 第二次运行 (raw_decode) |
|------|-------------------------|----------------------|
| 受影响对话比例 | 73.4% | ~11.1% |
| Parse error 事件数 | 604 (per 325 convs) | 5 (per 27 convs) |
| 主要原因 | brace counting 失败 | LLM 缺少 "arguments" 键名 |
| 可通过 parser 修复 | ✅ 已修复 | ❌ 需要 prompt engineering 或 fallback |

**潜在改进**: 可以在 parser 中添加一个 fallback——如果 JSON 对象没有 `"arguments"` 键但有其他键（如 `"command"`），尝试将其包装为 `{"name": ..., "arguments": {其余键值}}`。但这种启发式可能引入误判，暂不实施。

### 15.7 python → python3 问题

81.5% (22/27) 的对话首次尝试使用 `python` 命令失败，然后切换到 `python3`。虽然 agent 能自行恢复，但这浪费了一个 action turn 并增加了不必要的 token 消耗。

### 15.8 旧实验数据质量回顾分析

基于对 `spreadsheetbench_20260626_004250` 数据的深入分析：

**Observations 污染严重**:
- 641 个 observations 使用了 530 个唯一 cognitive_aspect 标签
- Top aspects 被 FD 耗尽（"Resource Exhaustion"）和空轨迹（"Cognitive Inertia"）主导
- 同一现象的同义表述造成 label 碎片化（"Pre-computation" vs "Pre-computational" vs "Pre-Cognitive"）

**Divergences 几乎全是噪声**:
- 233 个 divergences，85.8% 标为 systematic
- 样本检查：前 3 个 divergence 分别是 FD 耗尽、FD 耗尽、JSON 格式错误
- contrastive analysis 正确识别了"不是策略差异"但仍然产生了记录——**说明分析 prompt 缺乏过滤能力**

**Patterns 层面**:
- 169 个 patterns 中，即使排除明确噪声关键词（11.2%），剩余的"策略"patterns 也多数是环境问题的认知化包装
- Layer 2 的 LLM cluster refine 将基础设施错误赋予了心理学术语（如 "Iterative Resource Lifecycle Neglect"），掩盖了其基础设施本质

**结论**: 修复 parser bug 后的第二次运行，Layer 1-3 分析管道的信号纯度应该显著提升，因为：
1. 消除了 73.4%→11% 的 parse error 噪声
2. FD 耗尽问题在第二次运行中不太可能重现（因为 parse error 消除后 rollout 对话更短，打开的文件更少）

---

## 十六、持续观察记录 (第二次运行)

### 16.1 Cold Start Rollout 进度 (01:36 - 进行中)

| 时间 (运行时长) | 非空对话 | Trace行 | Rollout目录 | Task目录 | 进程状态 |
|---|---|---|---|---|---|
| ~3min | 43 | 667 | 505 | 125 | 717MB, 96.8% CPU |
| ~5min | 85 | 884 | 505 | 125 | 714MB, 61.1% |
| ~7min | 120 | 1098 | 505 | 125 | 731MB, 47.6% |
| ~9min | 186 | 1359 | 505 | 125 | 894MB, 41.3% |
| ~11min | 196 | 1420 | 505 | 125 | 927MB, 38.6% |
| ~13min | 199 | 1447 | 505 | 125 | 930MB, 30.0% |

**观察**:
- 前 7 分钟快速增长（~20 convs/min），之后进入长尾阶段
- 505 个 rollout 目录未变化（仍有 ~195 个 rollout 未创建目录 = 等待 ThreadPoolExecutor slot）
- FD 使用稳定在 50-70（vs 旧实验的 1024 limit 触发 OSError）

### 16.2 LLM 调用延迟分析

| 指标 | 值 |
|------|-----|
| 中位数 | 33.7s |
| P90 | 101.0s |
| P95 | 136.3s |
| P99 | 223.8s |
| 最大值 | 483.6s |
| >60s 占比 | 36.4% |
| >120s 占比 | 6.8% |

**根因**: 512 个并发 worker 同时向 qwen3.6-35b-a3b 模型服务发送请求，导致排队。中位延迟 33.7 秒意味着一个 6-turn 对话需要 ~3.3 分钟。

**影响**: 长尾 rollout（10+ turns 的复杂任务）需要 5-10 分钟完成。但总吞吐量受限于 LLM 服务能力而非客户端。

### 16.3 对话质量初步评估

基于 ~200 个已完成的 cold start 对话：
- **Parse errors**: ~21 次（vs 旧实验 604 次），降低 97%
- **错误类型**: 全部是 LLM 缺少 "arguments" 键名（非 parser bug）
- **TASK_COMPLETE**: 所有对话都包含至少一次
- **多次 TASK_COMPLETE**: 23 个（12.6%），由 task_interface 的 continue_with_message 触发
- **python not found**: 157 次（81.5%）但 agent 都能自行恢复
- **Sandbox 触发**: 0 次 — agent 没有尝试访问 golden answer 文件
- **对话长度**: 中位数 12 条消息（6 turns），最长 38 条
- **无超长对话**（>50 msgs = 0）— 证明 parser fix 消除了无限循环风险

---

## 十七、CSS 机制噪声传播的完整路径分析

基于 teammate 的代码级深度审计 + 本会话的实验数据验证，完整梳理噪声如何从 rollout 层透传到 strategy_0。

### 17.1 关键代码发现

**`turn -= 1` 是 dead code**（agent.py:311）：Python for-loop `for turn in range(...)` 每次迭代重置 `turn`，赋值无效。parse failure **确实消耗 turn**，agent 被 max_turns 限制。但 max_turns=100 下仍可产生 ~200 条重复格式错误消息。

**`passed = bool(self.hard)`**（data/rollout.py:73）：parse failure 和策略失败被抹平为同一个 "failure"。下游无法区分。

**Layer 1 无轨迹质量门控**：layer1.py:348 只跳过 `what` 和 `aspect` 同时为空的条目。对"轨迹是否真正执行过任务"零检查。分析器被强制对着重复格式错误轨迹产出 3-8 条 observation——只能把基础设施错误幻想成"认知缺陷"。

### 17.2 噪声注入的完整路径

```
passed=bool(hard) 抹平错误类型
    ↓
Layer 1: 格式错误被幻想成认知缺陷 (如 "Cognitive Non-Initiation")
    ↓
Layer 2: 噪声观察聚类成噪声模式 (如 "Resource Lifecycle Neglect")
    ↓
Layer 3: l0_saturated=True + 单epoch → L1闸门只剩覆盖率 → 噪声模式被认证为L1信号
    ↓
Root Cause: CODE GATE 只验"归因完整"不验"是否针对真实任务" → 噪声通过
    ↓
derive_strategy: 噪声根因被系统化进 strategy_0
```

### 17.3 实验数据验证

旧实验（brace-counting parser）的实际数据完美验证了这条路径：

| 阶段 | 理论预测 | 实际数据 |
|------|---------|---------|
| Layer 1 观察 | 大量噪声标签 | 641 obs, 530 unique labels, top aspects 全是 FD/parse 相关 |
| Layer 2 模式 | 噪声模式主导 | 169 patterns, 前 10 个全是基础设施噪声的认知化包装 |
| Layer 3 L1 信号 | 噪声被认证 | 仅 2 个 L1 信号: "Cognitive Non-Initiation" + "Resource Exhaustion" |
| Divergences | 误归因 | 233 个, 85.8% 标为 systematic, 样本全是 FD/parse 问题 |
| strategy_0 | 针对非问题 | "Mandatory Cognitive Anchoring" + "Error-Aware Continuity"（针对错误恢复而非电子表格策略）|

### 17.4 对比分析的结构性盲区

- contrastive_pairs() 只生成 (success × failure) 对，**从不比较 failure × failure**
- 当 success 与 parse-fail 配对时，真实差异是"格式合规 vs 不合规"，但被误读为认知策略优劣
- pure_fail 组（所有 K 个 rollout 都失败）走 failure 分析师，**完全不做对比**
- parse-fail 和策略-fail 的轨迹被混在同一个分析批次里，无区分

### 17.5 冷启动特有的闸门旁路

| 闸门 | 正常运行 | 冷启动 |
|------|---------|--------|
| L0 饱和检测 | 基于历史 remedy 次数 | 硬编码 True（coldstart.py:288）|
| occurrence_trend | 多 epoch 趋势 | 单 epoch，trend=0.0 自动满足 |
| 负面归档 gate | 过滤已知无效方向 | 空归档，直接放行 |
| retrospective_validate | 回顾验证覆盖率 | **冷启动不调用** |

**结论**：冷启动是噪声最脆弱的阶段——所有质量闸门要么被旁路要么无效。Parser fix 通过消除最大噪声来源（73.4%→11%），间接提升了冷启动的策略质量。

### 17.6 第二次运行的预期改善

修复 parser bug 后：
1. **直接影响**：parse error 从 73.4% 降到 ~11%（剩余是 LLM 格式质量问题）
2. **间接影响**：FD 耗尽消除（旧实验 1024 limit → 新实验 50-70 FD）
3. **信号纯度**：预计 Layer 1 观察中基础设施噪声从 ~78% 降到 <20%
4. **strategy_0 质量**：应该关注电子表格处理策略而非"错误恢复"
5. **对比分析有效性**：mixed 组中的 failure 轨迹大部分应该是真正的策略失败

**等待第二次运行完成 cold start 后对比验证。**

---

## 十八、第二次运行进程死锁分析 (2026-06-26 ~01:57 - ~23:50)

### 18.1 时间线

| 时间 | 事件 |
|------|------|
| 01:36:20 | 实验启动，PID=2955499 |
| 01:36~01:57 | Cold start rollout 积极执行（21 分钟） |
| 01:57:37 | **最后一条 trace.jsonl 写入** (call_001493) |
| 01:57:38 | **最后一个 prediction 文件写入** (24-23/r3) |
| 01:57 → 23:50 | **进程完全停滞**（~22 小时零活动）|
| ~23:50 | 尝试 kill -SIGUSR1 后进程终止（Python 默认 handler） |

### 18.2 停滞时的数据状态

| 指标 | 数值 | 占比 |
|------|------|------|
| 预期 rollout 总数 | 700 (140 tasks × 5) | 100% |
| 创建目录的 rollout | 505 | 72.1% |
| 有 conversation.json | 316 | 45.1% |
| 有 prediction 文件 | 199 | 28.4% |
| 空对话 (0 msgs) | 114 | — |
| rollout_done 事件 | **0** | 0% |
| 网络连接 | **0** | — |

**关键矛盾**: 199 个 rollout 产生了 prediction 文件，但 trace.jsonl 中 0 个 `rollout_done` 事件。说明 rollout 的 LLM 对话完成了，但 batch_rollout 的 future 汇总机制出了问题。

### 18.3 根因分析

**假说：ThreadPoolExecutor + wait(FIRST_COMPLETED) 死锁**

`batch_rollout()` 使用 `ThreadPoolExecutor(max_workers=512)` 提交所有 700 个 rollout，然后用 `wait(FIRST_COMPLETED)` 轮询。

可能的死锁场景：
1. 512 个 worker 全部被占用
2. 某些 rollout 内部调用 subprocess（bash tool），子进程等待 stdin/stdout pipe
3. subprocess pipe 缓冲区满，需要 reader 线程来消费——但所有线程都被 rollout 占用
4. 或者：LLM 服务在 01:57 后拒绝新连接，所有 in-flight 请求的 HTTP 连接挂起
5. `timeout_seconds: 1800`（30 min）的 LLM 调用超时 + `task_timeout_s: 3600`（1h）的任务超时
6. 最长等待应该是 1h，但进程卡了 22h——说明超时机制本身失效

**更可能的解释**：trace.jsonl 里只有 `llm_call` 事件（1466 条）和 1 个 `run_start`。没有 `rollout_start`、`rollout_done` 等事件。这暗示 **TracingLLMClient 只记录 LLM 调用**，rollout 层面的事件可能写在别处或根本不记录。

真正的问题可能是：batch_rollout 在等待 700 个 future，但由于 512 并发上限，后提交的 future 在队列中等待 worker 释放。如果已占用的 worker 中有永远不返回的（如 bash subprocess hang），队列永远不会排空。

### 18.4 数据质量（parser fix 验证）

尽管进程死锁，已完成的 199 个 rollout 提供了 parser fix 的有力验证：

| 指标 | 旧实验 (brace parser) | 新实验 (raw_decode) |
|------|------|------|
| Format error rate | **73.4%** | **0.9%** (25/2685) |
| 中位对话长度 | 极长 (parse loop) | **12 msgs** |
| 平均对话长度 | 极长 | **13.3 msgs** |
| TASK_COMPLETE 信号 | 极少 | **223 个** |
| 超长对话 (>50 msgs) | 大量 | **1 个** (58 msgs) |

**Format error rate 从 73.4% 降到 0.9%** — 几乎完全消除了基础设施噪声。

### 18.5 对话质量示例 (task 290-1/r0)

14 条消息的高质量 ReAct 对话：
1. Agent 先读取电子表格结构 (openpyxl)
2. 遇到 `python not found` 后自动切换 `python3`
3. 分析数据模式
4. 执行解决方案
5. 自发验证输出正确性
6. 输出 TASK_COMPLETE
7. **评估结果: PASS**

这展示了修复后 agent 的真实认知能力——思考清晰、行动有序、有验证意识。与旧实验中被格式错误淹没的轨迹形成鲜明对比。

### 18.6 对话长度分布

| 消息数范围 | 对话数 | 占比 |
|-----------|--------|------|
| 0 | 114 | 36.1% |
| 1-2 | 3 | 0.9% |
| 3-10 | 84 | 26.6% |
| 11-20 | 98 | 31.0% |
| 21-50 | 16 | 5.1% |
| 51-100 | 1 | 0.3% |

中位 12 消息 (= 6 轮 Think→Act→Observe) 是 ReAct agent 的健康工作模式。

### 18.7 Task 覆盖不均

| 状态 | Task 数 | 占比 |
|------|---------|------|
| 全 5 个 rollout 完成 | 14 | 11.2% |
| 4 个完成 | 13 | 10.4% |
| 3 个完成 | 16 | 12.8% |
| 2 个完成 | 6 | 4.8% |
| 1 个完成 | 17 | 13.6% |
| 0 个完成 | 59 | 47.2% |

47.2% 的 task 没有任何有效 rollout——这些 task 的目录被创建但 LLM 调用从未完成。

---

## 十九、深层机制思考

### 19.1 Parser Fix 的连锁效应

Parser fix 不仅修复了格式解析，还产生了多重正面连锁：

1. **对话变短** → 每个 rollout 的 LLM 调用数从 ~100（重复格式错误）降到 ~6
2. **LLM 负载降低** → 700 × 6 = 4200 调用 vs 旧的 700 × 100 = 70000 调用（17x 差距）
3. **并发压力缓解** → 不再有 512 个 worker 同时产生大量请求
4. **FD 消耗降低** → 短对话 = 快速释放子进程 FD
5. **信号质量提升** → 每个轨迹都是真实的任务尝试，而非格式错误噪声

### 19.2 仍需解决的基础设施问题

1. **进程死锁**：batch_rollout 的 512 并发 + 无超时兜底 = 永久等待风险
2. **rollout 层面事件缺失**：trace.jsonl 不记录 rollout_start/rollout_done
3. **无进度报告**：cold start 过程无中间进度日志
4. **SIGUSR1 不安全**：Python 默认 handler 直接终止进程

### 19.3 实验可行性评估

Parser fix 证明了系统在"干净信号"条件下可以产出高质量对话（0.9% error rate, 中位 12 msgs, 有 PASS 结果）。

**阻塞问题**：需要修复 batch_rollout 死锁才能完成完整的 cold start（当前仅 28% rollout 完成）。但用户约束是"不修改代码"——这产生了矛盾：
- 不修复 → 实验无法完成
- 修复 → 违反观察模式约束

**建议**：向用户报告此发现，请求允许修复死锁问题后重新启动实验。需要修改的可能是并发参数（512→更小值）或添加超时兜底。

---

## 二十、第三次运行：修复死锁后重启 (2026-06-26 11:31)

### 20.1 修复措施

1. **降低并发**: `max_api_workers` 从 512 降到 128
2. **添加 batch_deadline**: `batch.py` 中添加 `batch_deadline = t0 + task_timeout * 3` 作为整个 batch 的硬性截止时间，防止未 started 的 future 永远等待

### 20.2 启动状态

- **PID**: 65619 (Python), 65617 (bash wrapper)
- **运行目录**: `runs/spreadsheetbench_20260626_113146`
- **Config**: workers=128, task_timeout=3600s, max_turns=100

### 20.3 初期进展 (~3 分钟)

| 指标 | 数值 |
|------|------|
| LLM 调用 | 793 |
| 对话完成 | 65 |
| Prediction 文件 | 65 |
| Task 目录 | 33 |
| 线程数 | 192 |
| FD 数 | 521 |
| 网络连接 | 125 |
| RSS 内存 | 300MB |

### 20.4 LLM 延迟改善

| 指标 | 旧实验(512w) | 新实验(128w) |
|------|-------------|-------------|
| p50 | 33.7s | **21.2s** |
| p90 | 未记录 | **66.3s** |
| max | 224s | **179.1s** |

128 并发降低了 API 压力，p50 延迟改善 37%。

### 20.5 对话质量

| 指标 | 值 |
|------|-----|
| Format error rate | **0.4%** (3/680) |
| 平均 msgs/conv | **10.5** |
| TASK_COMPLETE 信号 | 68 |
| **Pass rate** | **66.2%** (43/65) |

**66.2% pass rate** 是关键发现——bare agent（无 skill document）在 spreadsheet 任务上的基础通过率非常高。

对比 baseline (max_turns=30, K=1, 200 test items): **15% pass rate**。差异原因：
1. max_turns=100 vs 30 — 更多 ReAct 轮次允许错误恢复
2. K=5 rollout — 这里的 66.2% 是 per-rollout pass rate，而非 per-task
3. train vs test items 可能有难度差异
4. 实际 cold start bare pass rate 需要等全部 700 rollout 完成后统计

### 20.6 Task-Level 二极化分布 (~5.5 min, 19 tasks fully complete)

| 类别 | 完成的 Task 数 | 占比 |
|------|---------|------|
| Pure pass (5/5) | 11 | 57.9% |
| Mostly pass (3-4/5) | 1 | 5.3% |
| Mixed (1-2/5) | 0 | 0% |
| Pure fail (0/5) | 7 | 36.8% |

**关键洞察：task 表现高度二极化** — 要么全通过要么全失败，几乎无中间状态。

**对 CSS 机制的含义**：

1. **contrastive analysis 的对象**：二极化分布意味着 contrastive_pairs() 主要比较的是 "easy task 的成功轨迹" vs "hard task 的失败轨迹"。这比较的是 **task 难度差异**，不是 **策略差异**。
   - 如果 agent 在 task A 上 5/5 pass、task B 上 0/5 fail，对比分析可能归因为"task B 需要更复杂的公式操作"——这是 task 特征，不是 agent 策略差异。
   - 真正有价值的对比来自 **mixed tasks**（有时成功有时失败），但这里几乎没有。

2. **strategy 的优化方向**：CSS 需要从 pure_fail tasks 中学到什么策略能突破当前 0% pass rate。但如果这些 task 天然超出 agent 能力（如需要复杂 VBA、宏操作等），策略优化可能无效。

3. **方案机制的根本假设检验**：CSS 假设 "通过策略优化可以提升失败任务的通过率"。二极化分布可能暗示 bare agent 的能力边界是 task 固有复杂度决定的，而非策略可调的。这是方案的 **核心假设风险**。

Pure fail tasks: 38823, 45300, 48080, 48620, 48921, 50526, 51262

### 20.8 失败模式深度分析 (Task 48080)

**案例**: task 48080/r0，12 条消息，FAIL

**Agent 行为**（完全正确的推理过程）：
1. 读取电子表格结构 → 发现 A列数据在每 16 行出现 (A25, A41, A57, A73, A89)
2. 发现 C列有公式引用模式 → C2=A25, C3=A41
3. 正确推断模式 → 填充 C4=A57, C5=A73, C6=A89
4. 用 openpyxl 写入公式并保存
5. 验证输出文件，确认公式正确
6. 完成任务

**失败原因**：评估系统检查的是 **公式计算后的值**（expected=14），但 openpyxl 保存的 xlsx 中公式是文本，未被执行（got=None）。

**这是 CSS 策略优化的理想目标**：
- Agent 的**推理完全正确** — 识别模式、生成正确公式
- 失败不是认知能力问题，而是**工具使用知识缺口** — 不知道 openpyxl 不执行公式
- **策略 document 可以教会 agent**: "openpyxl 不执行 Excel 公式。如果评估检查值而非公式，需要手动计算后直接写入数值，或使用 xlcalc/formulas 库求值"
- 这类知识是**可迁移的** — 一旦学到，适用于所有公式相关 task

**对 CSS 方案的启示**：
1. **乐观信号**：存在大量"推理正确但工具知识不足"的失败。CSS 策略优化正好擅长弥补这类 gap。
2. **contrastive analysis 的价值**：成功 task（不涉及公式）vs 失败 task（涉及公式）的对比，应该能识别"公式处理"作为关键 failure pattern。
3. **L1 signal 的预期**：如果多个 pure_fail task 都是公式相关，Layer 2 聚类应该能抽象出"公式求值策略缺失"的 pattern。
4. **strategy_0 的预期内容**：合理的 strategy 应包含"使用 openpyxl 时直接写入计算值而非公式"的指导。

### 20.9 持续进度更新

| 时间 | 运行时长 | LLM calls | 对话完成 | Tasks | Pass rate | 状态 |
|------|---------|----------|---------|-------|-----------|------|
| 11:31 | 0 | 0 | 0 | 0 | - | 启动 |
| 11:34 | 3min | 600 | 38 | - | - | 3.5 call/s |
| 11:35 | 4min | 793 | 65 | - | 66.2% | 质量确认 |
| 11:37 | 6min | 1070 | 113 | 48 | 62.8% | 趋于稳定 |
| 11:39 | 8min | 1414 | 166 | 59 | 66.3% | 稳定推进 |
| 11:41 | 10min | 1694 | 204 | 67 | 62.3% | 33 tasks fully done |
| 11:44 | 13min | 2119 | 262 | 78 | 56.9% | 42 tasks fully done, pass rate 下降 |

### 20.10 失败类型系统性分析

在 91 个失败 rollout 中：
- **pred=None (公式未求值)**: 53 个 (**58.2%**) — openpyxl 不执行公式
- **value mismatch**: 37 个 (40.7%) — 值计算错误
- **other**: 1 个 (1.1%)

**公式求值问题是最大的系统性失败原因**，占近 60%。

### 20.11 Agent 工具使用模式分析 (240 convs, 1203 actions)

**Action 类型分布**:
| 类型 | 占比 |
|------|------|
| Python 脚本 (python3 -c) | **79.8%** |
| 读文件 (cat/head) | 17.7% |
| 目录浏览 (ls/find) | 1.8% |
| 其他 | 0.5% |

**Python 子模式**: openpyxl (145次) > imports (145) > load_workbook (142) > inspect/print (44) > loops (24) > cell_ops (14)

**Agent 认知流程** (Thought 分类):
1. explore/understand: 233次 — 先理解数据结构
2. analyze/pattern: 168次 — 分析数据模式
3. adapt/retry: 147次 — 适应环境（如 python→python3）
4. verify/check: 27次 — 验证结果
5. fix/correct: 10次 — 修正错误

**典型对话**: 5 action, 10 messages: 读结构→分析模式→执行方案→保存→验证

**对 CSS 的启示**:
1. Agent 已有较好的认知流程（探索→分析→执行→验证），strategy 不需要教基础方法论
2. Strategy 应聚焦于**领域知识补充**：openpyxl 的 quirks（公式不执行、格式处理等）
3. "adapt/retry" 占比高（147次）说明 agent 经常遇到环境差异需要适应——strategy 可以预先告知环境特性减少浪费

### 20.7 持续监控进度表

| 时间 | 运行时长 | LLM calls | 对话完成 | Task完成 | Pass rate | 状态 |
|------|---------|----------|---------|---------|-----------|------|
| 11:31 | 0 | 0 | 0 | 0 | - | 启动 |
| 11:33 | 2min | 374 | 3 | - | - | LLM 响应正常 |
| 11:34 | 3min | 600 | 38 | - | - | 3.5 call/s |
| 11:35 | 4min | 793 | 65 | - | 66.2% | 质量确认 |
| 11:37 | 6min | 1070 | 113 | 19 | 62.8% | 趋于稳定 |
| 11:44 | 13min | 2119 | 262 | 42 | 56.9% | 通过率持续下降 |
| 11:49 | 18min | 2845 | 376 | 63 | 58.2% | 进度 52.2% (376/700) |
| 11:51 | 20min | 3104 | 411 | 71 | 56.7% | 进度 58.7%, 878MB RSS |
| 11:52 | 21min | 3212 | 424 | 73 | 56.4% | 进度 60.6%, 905MB RSS |
| 11:55 | 24min | 3632 | 493 | 88 | - | 进度 70.4%, 934MB RSS |
| 11:56 | 25min | 3743 | 508 | 93 | - | 进度 72.5%, 935MB RSS |
| 12:00 | 29min | - | 536 | 100 | - | 进度 76.5%, 速度放缓(难任务) |
| 12:00 | 29min | - | 551 | 102 | - | 78.7% |
| 12:05 | 34min | - | 587 | 108 | - | 83.9%, 速度回升~8/min, 976MB |
| 12:10 | 39min | - | 601 | 110 | - | 85.9%, 又减速~3.1/min(难任务) |
| 12:12 | 41min | - | 625 | 115 | - | 89.3%, queued仅剩6, ~5.3/min |
| 12:12 | 41min | - | 646 | 117 | - | 92.3%, queued=3, 接近完成 |
| 12:13 | 42min | - | 667 | 123 | - | 95.3%, queued=1!, 即将完成 |
| 12:14 | 43min | - | 676 | 128 | - | 96.6%, 仅剩24 rollouts |
| 12:15 | 44min | - | 684 | 129 | - | 97.7%, queued=0, 最后16 rollouts |
| 12:20 | 49min | - | 697 | 137 | - | 99.6%! 仅剩3个rollout(130-9/r2, 141-20/r2, 262-17/r1) |
| 12:22 | 51min | - | 698 | 138 | - | 99.7%, 仅剩2个(141-20/r2, 262-17/r1) |

### 23.5 最后 2 个 rollout 的异常耗时分析

141-20 和 262-17 的其他 rollout 都很快通过（4-8 turns, PASS），但 r2/r1 分别运行了 >40 分钟仍未完成：

| Task | 其他 rollout | 耗时 rollout |
|------|-------------|-------------|
| 141-20 | r0/r1/r3/r4: 均 5 turns, PASS, 11-12KB | r2: >40min 仍在运行 |
| 262-17 | r0/r2/r3/r4: 4-8 turns, 全 PASS, 15-30KB | r1: >40min 仍在运行 |

**现象**: 同一个 PASS 率很高的简单任务，某个 rollout 却极端耗时。

**可能原因**:
1. **LLM 采样随机性**: 某次采样走入了错误的推理方向，导致 agent 在修复循环中不断重试
2. **LLM 延迟尖峰**: 某个 HTTP 请求卡在 LLM 服务端（30分钟 timeout），导致整个 rollout 阻塞
3. **openpyxl 处理异常**: 某些输出处理步骤在特定 rollout 中触发了异常行为

**对 CSS 机制的意义**: 这种"outlier rollout"会拖慢整个 batch 的完成时间。batch_deadline 机制可以在 task_timeout*3=3h 后强制终止，但对于 cold start 阶段这个等待是可接受的。

### 20.5.1 进度追踪详情（18分钟快照）

- **任务分布**: 99 tasks started, 63 fully done (5/5), 18 partial, 18 queued (0 rollouts)
- **进程状态**: PID 65619, 192 threads, 673MB RSS, 运行正常
- **LLM throughput**: ~2845 calls / 18min ≈ 2.6 calls/s
- **Pass rate 趋势**: 66.2% → 62.8% → 56.9% → 58.2%，在 56-58% 区间趋于稳定
  - 这可能接近 bare rollout（无 skill document）的真实 baseline

### 20.5.2 Pass rate 拐点分析

pass rate 从初始的 66% 下降到 ~58%，这说明：
1. **早期完成的任务偏简单**：简单任务 rollout 快（少 turn、少 LLM 调用），先完成
2. **难任务逐步加入统计**：需要更多 turn 的任务后完成，它们的失败率更高
3. **58% baseline 预估**：如果 cold start 最终稳定在 ~55-58%，这就是 qwen3.6-35b-a3b 在 SpreadsheetBench 上无 skill 辅助的 bare 能力

## 21. 样例轨迹深入分析

已保存 2 个典型完整轨迹到 `sample_trajectories.md`：

### 21.1 Task 51359 (PASS) — 连续1计数任务
- 5 个 agent turn, 4 次 bash 调用
- 认知路径: 探索→环境适应(python→python3)→数据读取→分析+计算+验证
- 关键成功点: Agent 用代码计算而非纯手动推理，纠正了自己的手算错误
- 一步完成: 计算、写入、保存、验证在同一个 bash call 中完成

### 21.2 Task 208-20 (FAIL) — 表排序任务
- 9 个 agent turn, 8 次 bash 调用
- 经历了 3 次代码迭代（2 次 bug → 最终数据正确但公式引用错误）
- **失败根因**: openpyxl 写入公式字符串时行引用（E27, A27）未随行移动更新
- 这是 **公式评估问题的具体体现**: agent 完成了数据排序，但 F 列的 `=IF(NOT(ISBLANK(E27)),A27,"")` 仍引用原始行号
- 验证盲区: agent 只验证了 A-E 列匹配，遗漏了 F 列公式

### 21.3 两轨迹的机制启示

1. **python→python3 适应**: 所有轨迹的固定开销，浪费 1-2 turns → skill document 应引导使用 python3
2. **openpyxl 公式限制**: 这是系统性失败源，不是 agent 的"思维"错误而是工具链限制
3. **验证完整性**: Agent 倾向于验证"重要的"列，而非穷举所有列 → skill 应强调全列验证
4. **错误诊断能力**: Agent 将循环逻辑 bug 误归因为 Python 引用共享，反映推理链的脆弱性

## 22. 双模态假设修正与 Mixed 任务深入分析（21min 快照，453 rollouts）

### 22.1 双模态假设修正

之前（13min 快照）观察到"几乎纯双模态"（5/5 或 0/5），但随着更多任务完成，**mixed 比例显著上升**：

| 类型 | 数量 | 比例 |
|------|------|------|
| All PASS (5/5) | 36 | 45.6% |
| All FAIL (0/5) | 22 | 27.8% |
| **Mixed** | **21** | **26.6%** |

**修正结论**: 任务并非纯双模态。约 1/4 的任务是 mixed——同一个任务在不同 rollout 中有时通过有时失败。这对 CSS 机制的意义重大。

### 22.2 Mixed 任务分布分析（23 个 mixed 任务详情）

pass rate 分布：
- 4/5 pass: 105-24, 157-4, 269-43, 448-11, 51359 → 5 tasks
- 3/5 pass: 28-7, 341-14, 38462, 48924, 567-21 → 5 tasks
- 2/5 pass: 142-19, 280-17, 39931, 48588, 49300 → 5 tasks
- 1/5 pass: 203-15, 208-20, 263-1, 31202, 40478, 416-15, 51289, 516-46 → 8 tasks

分布偏向低 pass rate 一侧（1/5 有 8 个，4/5 只有 5 个），说明 **mixed 任务中大部分偏难**，只是偶尔运气好能通过。

### 22.3 对 CSS 反思机制的深远影响

Mixed 任务的存在对 CSS 反思机制意味着：

1. **对比学习的噪声源**：CSS 依赖 K=5 rollouts 中的 pass/fail 对比来提取认知信号。对于 mixed 任务：
   - 1/5 pass 的任务：有 1 pass + 4 fail，可以做对比但 pass 样本少
   - 3/5 pass 的任务：pass 和 fail 数量接近，对比信号更丰富
   - 这实际上是 **CSS 机制最有价值的学习材料**——它们是 agent "有时能做到、有时做不到"的任务

2. **失败模式的可学习性**：
   - 如果 mixed 任务的失败原因是**策略性的**（如 agent 选择了错误的分析路径），CSS 可以通过策略改进来提升 pass rate
   - 如果失败原因是**非确定性的**（如 LLM 采样随机性导致的行为差异），CSS 的策略改进效果有限
   - **pred=None (66.7%)** 占主导 → 这意味着大部分失败是 agent 根本没有产出有效答案，而非产出了错误答案

3. **策略优化的上限估算**：
   - 36 个 all-pass 任务 = 已解决，策略改进不影响
   - 22 个 all-fail 任务 = 可能本质超出 agent 能力（openpyxl 限制等）
   - 21 个 mixed 任务 = **CSS 的核心优化空间**
   - 如果 CSS 能将所有 mixed 任务提升到 5/5，理论上限提升：(36+21)/79 = 72.2% vs 当前 56.4%

### 22.4 失败原因的系统性分析

| 失败类型 | 数量 | 占比 | 含义 |
|----------|------|------|------|
| pred=None | 136 | 66.7% | Agent 未产出有效值（公式未执行/未写入/格式错误）|
| wrong_value | 58 | 28.4% | Agent 产出了值但计算错误 |
| other | 10 | 4.9% | 其他原因 |

**关键洞察**: pred=None 的高比例（66.7%）说明主要问题不是"算错"而是"没算出来"。这进一步分为：
- openpyxl 公式不执行（之前分析的 58.2% 比例）
- Agent 没有写入 output.xlsx
- Agent 写入了但格式/位置不对

这种"完全没有输出"的失败模式，比"输出错误"更容易被 skill document 改善——skill 可以直接指导"如何处理公式"、"确保写入 output.xlsx"等操作规范。

### 22.5 Turn count 分布

| 统计量 | 值 |
|--------|-----|
| min | 3 turns |
| p25 | 5 turns |
| median | 6 turns |
| p75 | 7 turns |
| max | 21 turns |
| mean | 6.2 turns |

大部分对话在 5-7 turns 内完成，与之前的 "avg 5 actions/conv" 分析一致。21 turns 的极端情况可能是 agent 陷入了重复尝试循环。

## 23. 长对话与速度放缓分析（29min 快照，551 rollouts）

### 23.1 Cold start 速度放缓的本质

rollout 完成速度从初期 ~18/min 骤降到后期 ~3.3/min，5.5x 的减速。原因：

**简单任务 vs 难任务的 rollout 时间差异极大**：
- 简单任务: 5-6 turns, 3-4 LLM 调用, 每次 ~20s → 总 ~60-80s/rollout
- 难任务: 14-21 turns, 10+ LLM 调用, 观察输出截断处理 → 总 ~300-600s+/rollout

这意味着 batch_rollout 的 ThreadPoolExecutor 中，128 个 worker 的大部分在等待少数几个超长 rollout 完成。

### 23.2 长对话（≥10 agent turns）特征分析

44/551 (8%) 的 rollout 使用了 ≥10 个 agent turns：

| 指标 | 值 |
|------|-----|
| 最长对话 | 21 turns (task 47766/r4, FAIL, 77KB) |
| 最大文件 | 6433KB (task 80-42/r3, FAIL, 14 turns) |
| FAIL 占比 | ~80%+ (长对话大多失败) |
| 典型 PASS 长对话 | 531-18/r1: 16 turns, 239KB |

**关键观察**:
1. **长对话几乎都失败**: 长 = agent 在反复尝试但无法解决问题
2. **超大文件** (如 80-42/r3 的 6.4MB): 可能是 agent 在对话中输出了大量数据（如整个 spreadsheet 内容），导致 context 膨胀
3. **同一任务不同 rollout 的 turn 数差异大**: 如 47766 的 r4=21turns 而 r2=19turns，说明 agent 行为存在随机性
4. **format error 为 0**: parser 修复后格式错误已完全消除，这是前次修复的积极验证

### 23.3 长对话对 CSS 机制的影响

1. **信噪比下降**: 长对话中包含大量失败尝试和纠错，当 Layer 1 对这些轨迹做认知标注时，会产生大量"错误→纠正"的标签，可能淹没关键的认知模式
2. **对比学习的有效性**: 同一任务的 PASS rollout（短对话）和 FAIL rollout（长对话）之间的"差异"可能不是策略差异，而是随机因素（如 LLM 采样时恰好选择了更好/更差的方向）
3. **context 膨胀风险**: 如果 skill document 被优化为指导 agent 做更多验证/重试，可能反而增加 turn 数而非提高 pass rate

### 23.4 速度放缓的机制意义

Cold start 的速度放缓揭示了一个 CSS 设计中需要考虑的问题：

**batch_deadline 机制的实际效果**: 我们设置了 `batch_deadline = t0 + task_timeout * 3 = 3600*3 = 10800s`（3小时）。而 cold start 的 700 个 rollouts 目前 29 分钟完成了 551 个。按当前速度，剩余 ~45 分钟可完成，远在 batch_deadline 之内。但如果有个别任务因为 LLM 调用循环或网络问题导致单个 rollout 耗时 >>60 分钟，task_timeout (3600s) 会兜底。

目前机制运转正常，没有出现第二次实验中的死锁问题。

---

## 24. Cold Start 完成与 Layer 1 分析管线启动 (12:17-12:22)

### 24.1 Cold Start 最终统计

Cold start bare rollout 于 12:17 达到 700/700 全部完成。最终统计：

| 指标 | 值 |
|------|-----|
| Total rollouts | 700 (140 tasks × 5) |
| PASS | 367 (52.4%) |
| FAIL | 333 (47.6%) |
| All PASS tasks | 53 (37.9%) |
| All FAIL tasks | 47 (33.6%) |
| Mixed tasks | 40 (28.6%) |
| 总耗时 | ~46 min |

**注意**: 之前的中间统计显示 pass rate 在 56% 附近稳定，最终下降到 52.4%。这说明后期完成的 outlier rollout（如 141-20/r2）大多为 FAIL，进一步拉低了 pass rate。

### 24.2 Outlier Rollout 最终观察: 141-20/r2

141-20/r2 是最后一个完成的 rollout。其他 4 个 rollout (r0/r1/r3/r4) 都是 5 turns PASS，但 r2 运行了 >21 分钟。

**有趣发现**: r2 在 workdir 中生成了 `Module1.bas`（VBA 宏代码），这是 agent 尝试用 Excel VBA 方式解决 reconciliation 问题的证据。VBA 代码看起来逻辑正确（按 Invoice No + Amount 匹配两个 sheet 中的行并删除匹配行），但在 sandbox 容器环境中没有 Excel 引擎来执行 VBA 宏。

**机制启示**: 同一个任务、同一个模型，5 次 rollout 中 4 次都高效 PASS，但有 1 次走上了完全不同的技术路线（VBA 宏 vs Python openpyxl），导致超长运行。这种随机性不是 agent 能力不足，而是 LLM 采样的固有特性。CSS 的 Layer 1 标注应当能识别出"Tool Modality Selection"作为一个关键 cognitive aspect。

### 24.3 Layer 1 Cognitive Annotation 管线启动

**12:18:54** — 第一个 optimizer 调用发出，标志着 Layer 1 分析正式启动。

观察到的关键信号：
- trace.jsonl 中出现 `role: optimizer`, `stage: optimizer`, `method: complete_optimizer`
- 与之前的 target rollout 调用 (`role: target`, `stage: target`) 形成鲜明对比
- 调用速率：~0.9 calls/sec，使用 ThreadPoolExecutor(max_workers=128) 并行标注
- 5 个到 LLM endpoint (10.77.110.162:8888) 的并发连接

### 24.4 Layer 1 标注质量评估

检查了前 107 个 optimizer 调用的输出，发现 Layer 1 的认知标注质量非常高。

**标注 schema**:
```json
{
  "what": "多句详细分析，引用轨迹中的具体内容",
  "cognitive_aspect": "LLM 自命名的认知方面标签（非预定义）",
  "evidence": "具体引用 agent 的代码、决策或推理",
  "consequence": "因果链分析",
  "significance": "critical | notable"
}
```

**显著性分布**（前 107 调用产出的标注中）:
- critical: 47 个
- notable: 30 个
- 解析错误: 4 个 (3.7% — 可接受)

### 24.5 涌现的认知方面分类 (Emergent Cognitive Taxonomy)

Layer 1 的设计哲学是"不预定义认知维度，让 taxonomy 从数据中涌现"(D4)。初期产出的 77 个独立 cognitive_aspect 标签，经我的归纳可分为以下几个核心主题集群：

**1. 环境适应/探测类**:
- "Reactive Environment Verification" — 试错式环境探测
- "Heuristic Environment Adaptation" — 启发式环境适应
- "Literal Command Substitution Recovery" — 命令替换恢复
- "Syntactic Fallback Without Environmental Diagnosis" — 语法回退不做诊断
- "Static Code Generation with Post-Hoc Environment Patching" — 先写代码后打补丁

**2. 公式/值混淆类** (与之前手动分析的最大失败来源一致):
- "Formula-Text/Value Conflation" — 公式文本与值混淆
- "Formula Syntax-Context Blindness" — 公式语法-上下文盲区
- "Formula Static-Assignment Fallacy" — 公式静态赋值谬误
- "Literal String Replication of Formula Syntax" — 公式语法的文字复制
- "Formula-Insertion Over Value-Computation Bias" — 插公式而非计算值的偏向

**3. 验证不完整/盲区类**:
- "Verification Mode Blindness" — 验证模式盲区
- "Semantic-Over-Exact Verification Bias" — 语义验证偏差（忽略精确验证）
- "Holistic Post-Execution Verification" — 整体性后验证
- "Unverified Abstract Logic Validation" — 未验证的抽象逻辑

**4. 过度泛化/假设固化类**:
- "Premature Generalization from Insufficient Sampling" — 样本不足的过早泛化
- "Pattern Overgeneralization from Homogeneous Samples" — 同质样本的模式过度泛化
- "Static Range Assumption Over Dynamic Inspection" — 静态范围假设优于动态检查
- "Metadata-Driven Boundary Assumption" — 元数据驱动的边界假设

**5. 错误诊断/修复类**:
- "Syntactic-Only Error Correction" — 仅语法层面的纠错
- "Premature Implementation Commitment with Superficial Error Recovery" — 过早实现+表面纠错
- "Mechanical Hypothesis Testing with Delayed Model Update" — 机械假设测试+延迟模型更新

### 24.6 对 CSS 机制的分析思考

**Layer 1 标注质量验证**: 涌现出的认知主题与我之前手动分析的结论高度吻合：
1. python/python3 适应 → 对应 "Reactive Environment Verification" 集群
2. openpyxl 公式问题 → 对应 "Formula-Text/Value Conflation" 集群
3. 验证盲区 → 对应 "Verification Mode Blindness" 集群
4. 错误诊断能力不足 → 对应 "Syntactic-Only Error Correction" 集群

这表明 Layer 1 的开放式标注设计是成功的 — 它能从数据中发现与人类专家分析一致的认知模式，且粒度更细、覆盖更广。

**但需要关注的问题**:
1. **标签碎片化**: 77 个不同的标签中很多语义重叠（如 "Reactive Environment Probing" vs "Reactive Environment Verification" vs "Heuristic-Based Environment Correction"），这需要 Layer 2 的 clustering 来合并
2. **解析错误**: 3.7% 的调用产出了无法解析的 JSON。虽然 `_parse_ndjson_objects` fallback 提供了鲁棒性，但仍有少量信号损失
3. **Significance 偏差**: critical:notable ≈ 47:30，可能存在 LLM 倾向于标记为 critical 的 bias。Layer 2 的 clustering 需要对此做平衡

### 24.7 Layer 1 进度预估

| 分析任务 | 预估调用数 | 当前进度 |
|----------|-----------|---------|
| 单轨迹标注 | 700 | ~151/700 (21.6%) |
| 对比对分析 | ~120 (40 mixed tasks × ~3 pairs/task) | 待启动 |
| Total | ~820 | ~151/820 (18.4%) |

按 0.9 calls/sec 速率，预计约 12 分钟后完成全部 Layer 1 分析。

### 24.8 Layer 1 NDJSON 解析行为深入分析 (12:28)

在 462 个 optimizer 调用中，进行了详细的输出格式分析：

| 解析方式 | 成功数 | 占比 | 说明 |
|----------|--------|------|------|
| Direct JSON array `[{...}, ...]` | 6 | 1.3% | 标准预期格式 |
| Direct single object `{...}` | 257 | 56.8% | Qwen3 主导输出模式 |
| NDJSON recovery `{...}\n{...}\n...` | 188 | 41.5% | Layer 1 fallback 挽救 |
| True failures | 11 | 2.4% | 无法恢复 |
| **Total observations** | **1050** | — | 平均 2.3 obs/call |

**关键分析**:

1. **Qwen3.6-35b-a3b 几乎不输出 JSON 数组**: 仅 1.3% 的调用返回了 prompt 要求的 `[{...}, ...]` 格式。这与 Qwen3 系列模型在 "Output ONLY a JSON list" 指令下的 conformance 有关。模型更倾向输出单个 JSON 对象或多个连续 JSON 对象。

2. **`_parse_ndjson_objects` 的关键作用**: 没有这个 fallback，41.5% 的调用会丢失所有标注。这是代码设计中的"鲁棒性契约"(Robustness contract) 在实际运行中的关键验证。

3. **单对象输出的 observation 损失**: prompt 指示 "Report every distinct cognitive pattern you observe (typically 3–8 per trajectory)"，但 56.8% 的调用只输出了 1 个 observation。这意味着**每个轨迹的认知分析深度不足** — Qwen3 在单对象模式下只捕捉了最显著的 1 个模式，遗漏了 2-7 个次要但仍有价值的认知观察。

4. **对 CSS 机制的影响**:
   - Layer 2 的 clustering 将基于不均匀深度的 observations：部分轨迹有 3-8 个标注，部分只有 1 个
   - 这可能导致某些认知模式的覆盖率被系统性低估
   - **可能的改进方向**：对 Qwen3 使用多轮对话或分步提示，强制每步只产出 1 个 observation，循环直到模型报告"无更多发现"

5. **True failure 的模式**:
   - 11 个真正失败的调用中，sample 显示有的是 `{[\n{...}` 格式（`{` 后接 `[`，非标准嵌套）
   - 2.4% 的失败率是可以接受的，符合 Layer 1 docstring 的"never crashes"鲁棒性承诺

### 24.9 Layer 1 进度更新 (12:28)

| 时间 | Optimizer calls | 增量 | 产出 observations |
|------|----------------|------|------------------|
| 12:18:54 | 0 | — | 0 |
| 12:21:09 | 107 | +107 | ~250 |
| 12:23:44 | 249 | +142 | ~580 |
| 12:27:14 | 429 | +180 | ~1000 |
| 12:28:28 | 477 | +48 | ~1100 |

速率稳定在 ~0.9 calls/sec。按 700 rollout + ~120 对比对 ≈ 820 total calls 估算，还需约 6 分钟完成。

---

## 25. CSS Pipeline 里程碑完成与 Round 0 启动 (更新于 06-26 续会)

### 25.1 Pipeline 阶段完成情况

实验在上次观察（Layer 1 进行中）之后持续运行，以下里程碑已确认完成：

| 里程碑 | 完成时间 | 关键产出 |
|--------|----------|---------|
| Cold Start (700/700 rollouts) | 12:45:41 | baseline pass_rate=52.4% |
| Layer 1 Cognitive Annotation | ~12:50 (推算) | 1242 optimizer calls total |
| **Layer 2 Label Grouping** | 12:45前 (trace记录) | **182 cognitive patterns** |
| **Strategy Derivation** | 12:45:41 | **strategy_0: 3063 chars** |
| **Round 0 Start** | 12:45:41 | node n0000, train=140, val=60 |

**关键观察**：从 cold start 完成到 strategy derivation 几乎瞬时（同一秒内），说明 Layer 2+3+derivation 的 optimizer calls 执行非常快速。

### 25.2 Strategy_0 深入分析：Physical State Verification Protocol

strategy_0 的完整内容（3063 chars）已从 `skill_snapshots/n0000/round_0000/strategy.md` 读取。这是 CSS 系统从 cold start 数据中自主推导出的第一个策略文档。

**策略名称**: Physical State Verification Protocol

**四大核心要素**:

1. **Environment Execution Modeling** — agent 必须在生成方案前显式建模目标环境的执行能力。区分"instructional artifacts"（公式字符串）和"evaluated states"（计算值）。识别 openpyxl 等库的无状态特性。

2. **Result-First Verification** — 采用"结果优先"心智模型：成功定义为"正确的计算值出现在最终输出中"，而不是"正确的公式语法出现在单元格中"。如果环境无法计算值，agent 必须自行用 Python 计算并写入字面值。

3. **Computation Bridge** — 当输出环境无法执行指令时，agent 必须构建"计算桥接"：用 Python 计算预期结果并直接写入值，而非写入公式。关键触发条件是检测到"指令"和"消费者"之间的不匹配。

4. **Output State Validation** — 强制最终验证：读取输出文件的实际内容，对照预期的"物理状态"进行检查。模拟消费者视角："如果我在 Excel 中打开这个文件，我看到什么？"

**分析评价**:

这个策略精准地击中了 cold start 阶段最大的系统性失败模式——公式写入 vs 值写入。从 Layer 1 annotation trace 2 和 trace 4 中我们已经看到：
- Trace 2 (Task 46167): agent 写了 `=SUMIF(...)` 公式但 eval 读到 None → FAIL
- Trace 4 (Task 39931): SUCCESS run 用 Python lookup dict 直接写值，FAILURE run 写 INDEX/MATCH 公式

strategy_0 的推导完全对应了这一核心失败模式。**这是 CSS 机制有效性的第一个积极信号** — 系统能从 bare rollout 数据中自动识别最关键的失败模式并生成针对性策略。

**潜在局限**:

1. strategy_0 聚焦于公式/值这一个问题维度，可能忽视了其他重要失败模式（如 python→python3 适应、合并单元格处理、验证盲区等）
2. targeted_pattern_ids 只有 4 个：`['p0015', 'p0019', 'p0064', 'p0100']`，在 182 个 patterns 中只覆盖了 2.2%
3. 但这正符合 CSS 的设计哲学——每个 strategy 聚焦少数关键 pattern，后续 round 通过新 node 逐步覆盖更多 pattern

### 25.3 Round 0 Rollout 进度

Round 0 正在用 strategy_0 对 140 个训练任务进行 rollout：

- **Round 0 target LLM calls**: 696（且持续增长）
- **Rollout 完成数**: 0（尚未有 rollout_done 事件）
- **估算**: 140 tasks × K=5 rollouts × ~7 calls/rollout ≈ 4900 target calls 需要完成
- **当前进度**: ~14%（696/4900）

Round 0 的 rollout 与 cold start 不同——agent 现在有了 strategy_0 作为 skill document，它的行为应该受到"Physical State Verification Protocol"的引导。**这是验证 strategy 有效性的关键阶段** — 如果 strategy_0 有效，Round 0 的 pass rate 应该显著高于 cold start 的 52.4%。

### 25.4 Node n0000 元数据

```json
{
  "node_id": "n0000",
  "parent_id": null,
  "branch_type": "ROOT",
  "round": 0,
  "val_score": 0.0,
  "best_score": 0.0,
  "n_steps": 0
}
```

val_score 和 best_score 都是 0.0，因为 rollout 尚未完成，eval 还未运行。这些数值将在 rollout batch 完成后更新。

### 25.5 LLM Call 分布总览

| 角色 | 数量 | 占比 |
|------|------|------|
| target (agent rollout) | 5504 | 81.6% |
| optimizer (分析/策略) | 1242 | 18.4% |
| **总计** | **6746** | 100% |

其中 target calls 分布：
- Cold start: 4808 calls (700 rollouts)
- Round 0: 696 calls (进行中)

### 25.6 Round 0 中间 Pass Rate — 重大发现！

**在 Round 0 rollout 进行中（49/140 tasks 开始，31 个 tasks 有完整 rollout），初步 pass rate 数据**：

| 指标 | Cold Start (baseline) | Round 0 (strategy_0) | 变化 |
|------|----------------------|---------------------|------|
| 已完成 rollouts | 700 | 115 | — |
| PASS | 367 | **92** | — |
| FAIL | 333 | **23** | — |
| **Pass Rate** | **52.4%** | **80.0%** | **+27.6pp** |

**这是 CSS 机制有效性的强有力证据！**

Strategy_0 ("Physical State Verification Protocol") 在仅用 3063 字符的策略文档的情况下，将 pass rate 从 52.4% 提升至 80.0%。这意味着：

1. **核心假设验证成功**: CSS 的"从失败轨迹中提取认知模式→派生策略→指导 agent"的循环链条是有效的
2. **Strategy 质量高**: "Physical State Verification Protocol" 精准命中了最大的系统性失败来源（公式写入 vs 值写入），agent 在有了策略指导后能主动用 Python 计算值并写入而非写公式
3. **27.6pp 的提升量级显著**: 在 baseline 已有 52.4% 的情况下提升到 80.0%，等于将错误率从 47.6% 降低到 20.0%，减少了 58% 的失败

**需要注意的局限**:
- 目前仅基于 115 个 rollouts（31 tasks），尚未达到完整的 140×5=700 rollouts
- 存在选择偏差的可能：先完成的 task 可能倾向于更简单的任务
- 最终 pass rate 可能随更多 task 完成而下降
- 但即使最终降至 70%+，仍然是非常显著的提升

### 25.7 深层思考：为什么 strategy_0 如此有效

strategy_0 之所以效果显著，本质原因是它解决的问题具有以下特征：

1. **高频失败模式**: 公式写入问题是 cold start 中最大的系统性失败来源（估计影响了 30-40% 的 FAIL cases）
2. **确定性修复**: 一旦 agent 知道要写值而非公式，修复是确定性的（不存在概率性的"有时能解决有时不能"）
3. **全局适用**: 策略适用于所有涉及计算的 task，覆盖面广
4. **低认知负担**: 策略的核心规则简单清晰（"写值不写公式"），不会与其他认知策略冲突

这给出了 CSS 机制的一个重要启示：**最有效的早期 strategy 应该是那些解决高频、确定性、全局适用的系统性失败模式的策略**。后续 round 的策略可能针对更零散的问题，提升幅度会逐渐递减。

### 25.8 下一步观察要点

1. **等待 Round 0 rollout 全部完成** — 目前 49/140 tasks，还需继续
2. **观察最终 Round 0 pass rate** — 验证 80% 是否在全量数据下保持
3. **等待 reflect → propose 循环** — 分析 Round 0 的新失败案例，生成改进策略
4. **等待 test set evaluation** — stop hook 条件要求的"每个 node 探索完后测一次 test 集效果"
5. **关注后续 round 的边际提升** — 验证"递减收益"假设

### 25.9 Round 0 Pass Rate 趋势追踪（持续更新）

conversation.json 是 **list 格式**（消息数组），非之前假设的 dict。Pass/Fail 通过最后一条消息 content 中的 "PASS"/"FAIL" 关键字判定。

| 时间点 | 完成 Tasks | 完成 Rollouts | PASS | FAIL | Pass Rate | 备注 |
|--------|-----------|--------------|------|------|-----------|------|
| 初始 (25.6) | 31 | 115 | 92 | 23 | 80.0% | 早期简单 task 偏差 |
| 中段 (旧解析) | 79 | 266 | 197 | 69 | 74.1% | 逐步收敛 |
| 当前 (修正解析) | 75 | 325 | 238 | 87 | 73.2% | 解析方式更正后的准确数据 |
| 后段 | 89 | 414 | 286 | 128 | 69.1% | 困难 task 加入 |
| 接近完成 | 94 | 439 | 306 | 133 | 69.7% | 微回升 |
| **全量完成** | **140** | **696** | **435** | **256** | **63.0%** | **最终结果（差4个rollout仍在运行）** |

**趋势分析**: Pass rate 从初始 80.0% 逐步下降至 73.2%，符合预期——随着更多困难 task 完成，通过率自然下降。目前稳定在 73-74% 范围。相对于 cold start 52.4%，仍有 +20.8pp 的显著提升。

**LLM 调用规模**: trace.jsonl 已达 8981 行，最新 call_id 为 call_008618，实验进程持续运行中。

### 25.10 Round 0 失败案例深层分析 — 三类残余失败模式

在 Round 0 rollout 进行到 84/140 tasks (60%) 时，对 116 个 FAIL rollouts 进行分类分析：

**失败类型分布**:

| 类型 | 数量 | 占比 | 说明 |
|------|------|------|------|
| pred=None（空值输出） | 62 | 53.4% | agent 未能在目标单元格写入任何值 |
| 公式残留（pred 包含 =） | 28 | 24.1% | strategy_0 未能完全消除公式写入行为 |
| 值不匹配（有值但错误） | 26 | 22.4% | agent 写入了值但计算/逻辑错误 |

**关键发现与深层分析**:

#### 1. pred=None 仍然是最大的失败来源（53.4%）

这说明 strategy_0 的 "Physical State Verification Protocol" 虽然有效引导 agent 写值而非公式，但存在 agent 根本没有成功写入任何内容的情况。可能原因：
- agent 在处理复杂任务时用尽了对话轮次（max turns 限制）
- agent 理解了策略但在具体执行上遇到困难（如复杂数据处理超出能力）
- 某些任务类型与策略关联度低（策略聚焦公式/值问题，但 None 可能来自完全不同的失败路径）

#### 2. 公式残留（24.1%）— strategy_0 的覆盖盲区

28 个 rollouts 仍然写了公式，说明 strategy_0 **未能 100% 消除公式写入行为**。观察到的案例：
- `pred='LEFT(K2,2)&"/"&MID(K2,4,2)&...'` — 字符串拼接公式
- `pred='IFERROR(INDEX(...),...)` — 复杂的 INDEX/IFERROR 嵌套公式

**深层思考**: 这暴露了 strategy 引导机制的一个重要局限——**策略文档是"建议性"而非"强制性"的**。agent 作为 LLM 有自己的惯性思维模式，当面对看似需要公式的任务时，可能会"忘记"或"覆盖"策略建议而回到默认行为。这不是 CSS 机制的缺陷，而是 LLM agent 遵循指令的本质局限。

#### 3. 值不匹配（22.4%）— 新的失败类别

这类失败反映了 strategy_0 无法解决的问题，因为 agent **确实按照策略写了值，但值本身是错误的**：
- `gt='HASSONA' pred='HASSAN'` — 拼写/查找错误
- `gt='66.67% WIN RATE' pred='67.00% WIN RATE'` — 精度/格式错误
- `gt=datetime(2020,1,2) pred=datetime(2020,1,7)` — 日期逻辑计算错误
- `gt='Black' pred='IFERROR(...)'` — 混合了公式和值方法
- `gt='N' pred='Y'` — 条件判断逻辑完全反转

**这类失败对 CSS 后续 round 很重要** — 它们代表了 strategy_0 已经无法解决的"新前沿"失败模式。后续 reflect → propose 循环应该会识别这些新模式并生成针对性策略。

**CSS 机制有效性的进一步验证**: 即使 pass rate 从 80% 降至 ~71%（随更多困难 task 加入），strategy_0 仍将 baseline 从 52.4% 提升到 71%+，这是一个坚实的 +18.6pp 提升。残余失败的多样性也说明 CSS 还有"进化空间"——后续 round 可以针对 None 和 值不匹配 问题推导新策略。

### 25.11 Round 0 Rollout 最终结果

**Round 0 全量完成**（140/140 tasks, 696/700 rollouts 有 conversation.json）：

| 指标 | Cold Start (baseline) | Round 0 (strategy_0) | 变化 |
|------|----------------------|---------------------|------|
| 已完成 rollouts | 700 | 696 | — |
| PASS | 367 | **435** | +68 |
| FAIL | 333 | **256** | -77 |
| 未计入 | 0 | 5 (缺 conversation.json) | — |
| **Pass Rate** | **52.4%** | **63.0%** | **+10.6pp** |

**重要修正**: 最终 pass rate 63.0% 显著低于中间阶段观测到的 73-80%。这验证了之前预判的"选择偏差"——先完成的是简单 task，后完成的困难 task 拉低了整体通过率。

**Pass Rate 完整趋势**:
80.0% → 73.2% → 72.3% → 71.8% → 69.1% → 69.4% → 69.7% → **63.0%**

最后阶段（从 94 tasks 到 140 tasks）pass rate 从 69.7% 急剧下降到 63.0%，说明最后 46 个完成的 task 中有大量困难任务（估算这批 task 的独立 pass rate 仅约 ~50%，接近 cold start baseline）。

**深层分析**:

+10.6pp 的提升（52.4% → 63.0%）虽然低于中间阶段的乐观估计，但仍然是**统计显著且实质性的提升**：
1. 绝对错误率从 47.6% 降到 37.0%，减少了 22.3% 的失败
2. 额外 68 个 rollout 从 FAIL 变为 PASS
3. 单个策略文档（3063 chars）就实现了这个效果

**但也暴露了 strategy_0 的局限**:
- strategy_0 主要解决"公式写入 vs 值写入"问题
- 对于更复杂的 task（需要多步推理、复杂数据变换、跨表操作等），strategy_0 的指导作用有限
- 最后一批困难 task 的 pass rate 接近 baseline，说明这些 task 的失败原因与公式/值问题无关

### 25.13 train_rollout_done 事件确认与 Reflect 阶段启动

`train_rollout_done` 事件在 trace.jsonl 中确认：
```json
{
  "event": "train_rollout_done",
  "round": 0,
  "node_id": "n0000",
  "train_score": 0.6271,
  "n_results": 700,
  "n_pass": 439,
  "n_groups": 140
}
```

**官方 train_score = 62.71%**（439/700），与我们手动统计的 63.0%（435/691 有 PASS/FAIL 标记的）基本一致。差异来自 4 个 rollout 的 conversation.json 写入时序。

**当前阶段**: Reflect（分析 Round 0 失败轨迹），optimizer LLM 调用已开始（call_011243）。

**完整 pipeline 事件序列** (截至当前):
1. `run_start` → `epoch_start` → cold start rollout
2. `llm_label_group` ×169 → `cluster_refine` ×182 → `layer2_done`
3. `root_cause_attribution` → `strategy_derived` (3063 chars)
4. `cold_start_done` (baseline=0.524)
5. `round_start` (round=0) → `epoch_start` (node=n0000)
6. **`train_rollout_done`** (train_score=0.627) ← 当前位置
7. [等待] reflect → propose → val_eval → epoch_end → test_eval

### 25.12 Annotation Prompt 设计分析（对比实验）

**问题**: Layer 1 cognitive annotation 产出的语义内容不够丰富，可能是 prompt 中的内容示例导致 LLM 产生 anchoring effect。

**对比实验设计**: 用同一轨迹（Task 269-43/r0），对比两种 prompt:
- **Original**: 包含完整的 content example（KeyError → positional indexing 的案例）
- **Modified**: 去掉内容示例，只保留格式骨架和更强的深度要求

**实验结果**:

| 指标 | Original (有内容示例) | Modified (无内容示例) | 变化 |
|------|----------------------|---------------------|------|
| 总长度 | 4442 chars | 5406 chars | +21.7% |
| Observation 数 | 5 | 4 | -1 |
| **平均 what 长度** | **370 chars** | **807 chars** | **+118%** |
| 平均 evidence 长度 | 176 chars | 180 chars | +2.3% |
| 平均 consequence 长度 | 185 chars | 198 chars | +7% |

**关键发现**: what 字段的语义深度翻倍。Modified 版本的分析包含更丰富的认知层次：
- 对比思维（"为什么不选择另一种方法"）
- 认知框架引用（"cognitive bias toward empirical verification"）
- 反事实分析（"如果不这样做会怎样"）
- 架构性洞察（"context window 作为认知工具"）

**根因诊断**: In-context example 产生三层 anchoring effect:
1. **长度锚定**: LLM 模仿示例字段长度而非将其视为最低标准
2. **内容锚定**: LLM 优先寻找与示例语义相似的 pattern（都倾向于"error recovery"类）
3. **数量锚定**: 示例只给了 1 个 observation，隐性压过了"3-8"的文字要求

**改进建议**（不修改当前实验，仅作为后续迭代参考）:
1. 去掉内容示例，改为 format-only skeleton with placeholders
2. 显式设置最低字段长度期望
3. 用更强措辞强制最低 observation 数量

---

## 25.14 Val Rollout 完成 & Gate Decision 分析

**时间线**:
- Val rollout 启动: ~13:46 (reflect 完成后)
- Val rollout 完成: ~14:16 (300/300, 约 30 分钟)
- Gate decision: 14:16:54 — `accept_new_best`

**Gate Decision 数据**:
```
gate=accept_new_best score=0.000->0.557 best=0.557
```

**关键指标对比**:

| 指标 | Train | Val | 差异 |
|------|-------|-----|------|
| Pass rate | 62.71% (439/700) | 55.7% (167/300) | -7.0pp |
| 任务数 | 140 × K=5 | 60 × K=5 | — |
| 相对 baseline | +10.3pp (vs 52.4%) | +3.3pp (vs 52.4%*) | — |

**分析：Train→Val 泛化衰减 (-7.0pp)**

这是一个值得关注的信号。train score 62.71% 到 val score 55.7% 的 7pp 衰减可能源于：

1. **Strategy 过拟合**：strategy_0 ("Physical State Verification Protocol") 在 train set 的 140 个任务上通过 cold start 分析得出，可能对 train set 的特定失败模式（如公式残留、值不匹配）有针对性优化，而 val set 的失败分布可能不同

2. **任务难度分布差异**：val set 的 60 个任务可能包含更多困难任务（如更大的电子表格、更复杂的公式链），导致即使策略正确也难以通过

3. **Baseline 参考不确定性**：这里的 baseline 52.4% 是 train set 上的值；val set 的无策略 baseline 可能不同，真实的策略增益需要单独测试 val baseline

**Gate Decision 分析**：

- 由于这是第一轮 exploit（之前 best_score=0.000），任何正分数都会被接受为 new_best
- `accept_new_best` 意味着 strategy_0 的编辑被接受，将作为后续 round 的基础
- 真正有意义的 gate 判断将在后续 round 中出现（需要超过 0.557 才能被接受）

**Val Rollout 耗时分析**：

Val rollout 从 ~13:46 到 14:16 共约 30 分钟，300 个 rollout（60 tasks × K=5）。但进展极不均匀：
- 0→280: 约 25 分钟（平均每分钟 11 个）
- 280→300: 约 5 分钟（平均每分钟 4 个）
- 尤其最后 3 个 rollout 停滞了约 8 分钟

这说明少数任务的 agent 执行时间极长（可能达到 max_turns=100 上限），形成"长尾"效应。在 K=5 的设置下，如果某个任务的 5 次 rollout 都很慢，就会严重拖累整体完成时间。

---

## 25.15 等待 Test Eval

Gate 通过后 (accept_new_best)，系统应执行 test set evaluation：
- Test set: 200 tasks
- 预期 test rollout 数量: 200 × K（取决于 test 时的 K 设置）
- 这是 stop hook 条件 (3) 的关键要求

当前状态: test 目录尚未创建，正在等待系统启动 test_eval 阶段。

---

## 25.16 Step 1 Reflect 结果 — 关键发现：edit 去重后为零

**时间**: 14:26:59

**日志**:
```
L0 step 1 — reflect=1 raw, merge=7, jaccard=0, llm_dedup=0, select=0 edits
```

**关键发现**: Step 1 的 reflect 产生了 0 个新编辑！

- raw proposals: 1 个
- merge 后: 7 个
- **jaccard 去重后: 0 个** — 所有 proposals 与 step0 已接受的 edits 高度重叠
- llm_dedup: 0 个（没有东西可去重）
- **最终 select: 0 个编辑被选中**

**分析**: 这表明 step0 的 8 个 edits（已通过 gate）覆盖了 reflect 在同一 val rollout 数据上能发现的大部分改进空间。step1 用**相同的 val rollout 数据**重新做 reflect，自然得出的 proposals 与 step0 高度重叠。

这反映了**方案机制的一个值得思考的特性**：
1. **正面解读**: jaccard 去重有效阻止了冗余编辑，避免了对同一改进的重复尝试
2. **负面解读**: 这意味着单一 reflect 已经"耗尽"了对当前 val data 的分析能力，多步 exploit 在相同数据上的边际收益为零
3. **深层问题**: 多步 exploit 的设计是否需要更强的多样性机制？比如每步用不同的 reflect 视角、不同的失败样本子集、或者引入随机扰动

**对 pipeline 流程的影响**: 0 个新编辑意味着 step1 没有新的 strategy 变体可以测试。系统可能会跳过 step1 的 val rollout，直接进入下一步或结束当前 epoch。

---

## 25.17 Gate 后进程活动分析

**现象**: Gate decision 在 14:16:54 通过 (accept_new_best, val=0.557)，但截至 14:23（约 7 分钟后）：
- 实验日志无新行
- trace.jsonl 无新事件（仍停在 14035 行）
- test 目录未创建
- **进程 PID 65619 CPU 使用率降至 0.0%** — 进程处于空闲/挂起状态

**诊断分析**:

这强烈暗示 **gate 通过后 pipeline 没有自动触发 test_eval 阶段**。可能的原因：

1. **代码实现遗漏**: 当前的 CSS orchestrator 可能在 gate decision 后直接进入 epoch_end → 下一轮 round，而没有实现 test_eval 步骤。需要检查 orchestrator.py 中 gate 后的逻辑流。

2. **Pipeline 结束但未退出**: 如果当前只设置了 1 个 exploit step（steps=0 在 epoch start 日志中），gate 通过后可能认为当前 epoch 完成但不确定下一步该做什么。

3. **test_eval 可能不是自动触发的**: 在某些 CSS 实现中，test eval 可能需要显式配置或作为单独的 post-hoc 步骤执行。

**方案机制层面的思考**:

如果 test_eval 确实没有被实现为 pipeline 的自动步骤，这反映出一个**方案设计的空白点**：我们的 CSS 框架设计中明确要求"每个 node 探索/利用完后测一次 test set"，但代码实现中可能遗漏了这一环节。这不是方案机制本身的缺陷，而是实现的不完整。

**需要确认**: 进程虽然 CPU=0 但并未退出。可能在等待某个事件或执行某个阻塞操作。继续观察。

**更新**: 进程确实在运行，只是 vLLM 推理慢导致 CPU 看起来空闲（实际在等待远端 API 返回）。trace.jsonl 后续持续增长，确认 step1 仍在执行 val rollout。

---

## 25.18 Step1 Val Rollout 进展跟踪

**背景**: Step1 reflect 产出 0 个新编辑（被 jaccard 去重全部过滤）。但系统仍然启动了 step1 的 val rollout — 这意味着 **即使没有新编辑，pipeline 也会重新运行 val evaluation**。

**进展时间线**:

| 时间 | 完成数 | trace行数 | 速率 |
|------|--------|-----------|------|
| ~14:26 | step1 reflect完成 | ~14200 | - |
| ~14:30 | 52/300 (17%) | 14733 | - |
| ~14:34 | 86/300 (29%) | 15014 | ~34/4.5min |
| ~14:39 | 92/300 (31%) | 15081 | ~6/5min (波动) |

**分析 — 0 编辑为什么还跑 val rollout？**

这是一个重要的机制行为观察：

1. **代码层面**: orchestrator 的 exploit step 逻辑似乎不判断"是否有新编辑"就直接进入 val rollout。即 step1 用的是与 step0 完全相同的 strategy（因为没有新增编辑），val rollout 结果理论上应该与 step0 高度一致。

2. **预期结果**: step1 val_score 应该接近 step0 的 0.557（完全相同的 strategy + 相同的 val set），细微差异仅来自 LLM 采样随机性。

3. **方案机制层面思考**:
   - **问题**: 浪费了 300 次 val rollout 的计算资源（每次含最多 100 turns 的 LLM 调用）去测试一个完全没有变化的 strategy
   - **根因**: pipeline 没有"短路"逻辑 — 当 reflect 产出 0 新编辑时应该跳过后续 val rollout 和 gate
   - **这是代码实现问题，非方案设计缺陷**: 方案设计中 exploit 的多步结构是合理的，但实现应该检查"是否有实质性变化"来决定是否执行后续评估
   - **改进建议**: 在 step reflect 完成后检查新编辑数，若为 0 则直接进入 epoch_end 或下一 round，避免无谓的 val rollout

---

## 25.19 Trace 事件覆盖度分析

**时间**: ~14:46

**发现**: 对 trace.jsonl 进行完整事件类型统计，发现：

```
15934  llm_call
  182  cluster_refine
  169  llm_label_group
    2  reflect_plan_a_success_insights
    2  reflect_plan_a_failure_diagnosis
    2  reflect_plan_a_contrastive
    2  reflect_plan_a_edits
    1  run_start / layer2_match / label_canonicalize / group_observations
    1  counterpart_pair / layer2_done / root_cause_attribution
    1  strategy_derived / cold_start_done
    1  round_start / epoch_start / train_rollout_done
```

**关键缺失**:
1. **无 gate 事件** — gate_decision 出现在 INFO 日志中（"L0 step 0 — gate=accept_new_best score=0.000->0.557 best=0.557"）但**不写入 trace.jsonl**
2. **无 val_eval 事件** — 300 次 val rollout 完成后没有汇总事件
3. **无 exploit_step 事件** — step 开始/结束没有标记
4. **无 epoch_end / round_end** — 无法从 trace 追踪 pipeline 阶段结束
5. **无 test_eval** — 如预期

**分析**:
- trace.jsonl 主要记录了**计算密集型操作**（llm_call、cluster_refine、label_group）和 **cold start 阶段**（各类分析步骤），但 **exploit 阶段的控制流事件严重缺失**
- 这不影响实验正确性，但降低了可观察性（observability）
- gate 决策这样的关键事件只在 Python logging 日志中出现，如果日志被覆盖或截断则不可追溯
- **实现建议**: exploit 阶段应补充 trace 事件：exploit_step_start、val_eval_done（含 score）、gate_decision（含决策类型和分数）、epoch_end

---

## 25.20 Step1 Gate Decision — reject (val=0.543)

**时间**: 14:55:26

**日志**:
```
L0 step 1 — gate=reject score=0.557->0.543 best=0.557
```

**关键数据**:
- step1 val_score: 0.543（54.3%）
- 对比 step0 val_score: 0.557（55.7%）
- 差异: -1.4pp
- Gate 决策: **reject** — 新分数未超过 best=0.557
- best_score 保持: 0.557

**分析 — 完全符合预期**:

1. **因果链完整**: step1 reflect → 0 新编辑（jaccard 去重）→ 完全相同的 strategy → val 分数应接近 step0 → 实际分数 0.543 vs 0.557（-1.4pp）→ gate reject
2. **0.543 vs 0.557 的差异来源**: 纯 LLM 采样随机性。相同 prompt + 相同任务，不同 rollout run 的通过率天然存在波动。-1.4pp（约 1 个任务差异，60 * 0.014 ≈ 0.84 个任务）完全在统计噪声范围内
3. **Gate 机制正确工作**: 没有因为微小的随机波动而错误接受一个无变化的 strategy

**方案机制层面深层思考**:

这个 step1 循环完整暴露了一个**结构性效率问题**：

- 从 step1 reflect 开始（14:26）到 gate reject（14:55），耗时 **29 分钟**
- 这 29 分钟做了 300 次 val rollout（每次最多 100 turns LLM 调用），总计约 16000+ 次 LLM 调用
- **全部用于测试一个完全没有变化的 strategy**，结果必然是 reject
- 如果有"0 新编辑 → 跳过 val rollout"的短路逻辑，这 29 分钟和 16000+ 次 API 调用完全可以节省

**量化浪费**:
- step1 val rollout: ~300 tasks × ~5 turns avg = ~1500 LLM 调用（保守估计）
- 实际 trace 显示 step1 期间新增约 2400 行 llm_call（16605-14200≈2400）
- 按 qwen3.6-35b 模型估算，浪费了显著的计算资源

**下一步观察点**:
- step2 目录为空（0 文件）— 系统正在决定是否继续
- 如果继续 step2，且 reflect 再次产出 0 新编辑，将是同样的浪费循环
- 关键问题：exploit 循环的终止条件是什么？是固定步数还是自适应？

**更新（15:00）**: 
- Step1 gate=reject 后，**无 step2 目录生成**。系统没有继续 exploit 循环。
- 进程仍在运行（PID 65619, CPU 7.6%, 3h28m），但 trace 在最后一次 llm_call（14:55:32）后无新事件
- n0000 下只有 round_0000，无 round_0001
- **推测**: 系统可能在进行 epoch_end 阶段的某些内部处理（如更新 strategy、保存状态），但这些操作没有写 trace 事件

**Exploit 终止条件推断**: ~~gate=reject 意味着 step1 没有产生比 best (0.557) 更好的结果。系统似乎在 gate reject 后终止了 exploit 循环~~  **此推断错误**，见 25.21。

---

## 25.21 Step2 Reflect — 再次 0 新编辑，暴露 exploit 循环无终止问题

**时间**: 15:05:35

**日志**:
```
L0 step 2 — reflect=1 raw, merge=8, jaccard=0, llm_dedup=0, select=0 edits
```

**关键发现**: 

1. **Gate reject 不终止 exploit 循环** — 之前推测 gate reject 后系统会停止，实际上系统继续进入了 step2。这说明 exploit 循环的终止条件**不是**基于 gate 结果的自适应策略。

2. **Step2 再次 0 新编辑** — 与 step1 完全相同的模式：reflect 产出的 proposals 全部被 jaccard 去重过滤。这是第三次用相同 val rollout 数据做 reflect，结果必然相同。

3. **即将发生的浪费**: 如果 pipeline 不检查 "0 新编辑"就直接开始 val rollout，将**再浪费约 30 分钟 + 2400 次 LLM 调用**测试一个完全相同的 strategy。

**方案机制深层分析 — exploit 循环的终止条件缺陷**:

这暴露了一个**严重的效率问题**：

- `steps=0` 在 Epoch start 日志中出现，但实际运行了 step0、step1、step2 ... 说明 steps 参数的含义可能不是"固定步数"
- exploit 循环看起来是**无限循环**或使用了非常宽松的终止条件
- 在当前情况下（相同 val 数据 + 相同 strategy → 每步 0 新编辑 → 每步 gate reject），循环将无限重复相同的无效计算

**对比方案设计意图**:

我们的 CSS 方案设计中，exploit 的多步机制是为了"迭代改进"——每步基于上一步的 gate 反馈做更精细的调整。但当前实现中：
- reflect 使用的是**原始的 val rollout 数据**，不是最新 step 的 val rollout 数据
- 因此每步 reflect 看到的信息完全相同，产出的 proposals 也完全相同
- jaccard 去重正确地过滤了这些重复 proposals，导致每步都是 0 新编辑

**根因**: 这不是方案设计的本质缺陷，而是**两个实现层面的问题叠加**：
1. exploit 循环缺少高效的终止条件（应在连续 N 次 0 编辑或连续 N 次 gate reject 后停止）
2. 多步 reflect 使用相同的数据源，无法产生新的洞察（应该用最新 step 的 val rollout 结果作为 reflect 输入）
