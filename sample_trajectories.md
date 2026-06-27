# Sample Agent Trajectories — Cold Start Rollout

> Experiment: `spreadsheetbench_20260626_113146` (cold start, bare rollout, no skill document)
> Model: qwen3.6-35b-a3b via OpenAI-compatible endpoint
> Agent: ReAct (Think → Act → Observe loop, bash tool only)

---

## Trajectory 1: Task 51359 / Rollout 0 — PASS

**Task type**: 计算每列中连续1的总数（仅计算长度>=2的连续组）  
**Result**: PASS (4/4 cells correct)  
**Turns**: 10 messages (5 agent + 4 observations + 1 system verification)  
**Actions**: 4 bash calls

### Turn 1 — 探索数据结构

**Agent Thought**: 需要先理解spreadsheet结构，读取全部数据。

**Action**: 写入 `solution.py` 并用 `python` 运行，读取 xlsx 结构。

**Observation**: 
```
/bin/sh: 22: python: not found
[Exit code: 127]
```

> **分析**: Agent 首先尝试 `python` 命令，在 Ubuntu 20.04 容器中默认不存在，这是几乎所有轨迹中都出现的典型适应模式。

---

### Turn 2 — 环境适应

**Agent Thought**: Python is not available as `python`, let me try `python3` instead.

**Action**: `which python3 && python3 --version`

**Observation**: 
```
/usr/bin/python3
Python 3.8.10
```

> **分析**: Agent 快速适应，检测可用的 python3 路径和版本。这是一个低成本的探测动作。

---

### Turn 3 — 成功读取数据

**Action**: 重新用 `python3` 运行之前的读取脚本。

**Observation**: 
```
Max row: 33, Max col: 5
Row 1: [None, None, None, None, None]
Row 2: [None, None, None, None, None]
Row 3: [None, 0, 1, 1, 0]
Row 4: [None, 0, 0, 1, 0]
Row 5: [None, 1, 1, 0, 0]
Row 6: [None, 0, 1, 0, 1]
Row 7: [None, 0, 1, 1, 0]
Row 8: [None, 1, 0, 1, 1]
Row 9: [None, 1, 1, 1, 1]
Row 10: [None, 0, 0, 1, 0]
Row 11: [None, 1, 1, 1, 0]
Row 12: [None, 0, 1, 0, 1]
Row 13: [None, 1, 1, 1, 1]
Row 14: [None, 1, 0, 1, 0]
Row 15: ['Ans', 4, 6, None, None]
...
```

> **分析**: Agent 成功获取了完整的数据矩阵。注意 Row 15 已有部分答案 (B15=4, C15=6)，D15/E15 为 None 待填写。

---

### Turn 4 — 分析模式 + 计算 + 写入 + 验证（一步完成）

**Agent Thought** (详细推理过程):

Agent 进行了非常详尽的手动分析：
1. 先逐列列出值，手动找连续1的分组
2. 发现 Column B 手算结果与已有答案不匹配（算出5，实际是4）
3. 反复核对，怀疑自己的读数
4. 决定直接写代码精确计算

**Action**: 编写完整的 Python 脚本：
- 遍历 B-E 列（列2-5），rows 3-14
- 用 while 循环检测连续1的分组
- 仅累加长度 ≥ 2 的分组
- 写入 B15:E15
- 保存 output.xlsx
- 重新加载验证

**Observation**: 
```
Column 2: [0, 0, 1, 0, 0, 1, 1, 0, 1, 0, 1, 1]
  Found group of 2 consecutive 1s
  Found group of 2 consecutive 1s
  Total for column 2: 4
Column 3: [1, 0, 1, 1, 1, 0, 1, 0, 1, 1, 1, 0]
  Found group of 3 consecutive 1s
  Found group of 3 consecutive 1s
  Total for column 3: 6
Column 4: [1, 1, 0, 0, 1, 1, 1, 1, 1, 0, 1, 1]
  Found group of 2 consecutive 1s
  Found group of 5 consecutive 1s
  Found group of 2 consecutive 1s
  Total for column 4: 9
Column 5: [0, 0, 0, 1, 0, 1, 1, 0, 0, 1, 1, 0]
  Found group of 2 consecutive 1s
  Found group of 2 consecutive 1s
  Total for column 5: 4

Results: [4, 6, 9, 4]
Output saved successfully
Row 15, Col 2: 4
Row 15, Col 3: 6
Row 15, Col 4: 9
Row 15, Col 5: 4
```

> **分析**: Agent 之前手算 Column B 时误将 Row 7 的值读为1（实际为0），代码计算纠正了这个错误。这体现了"用代码验证而非纯推理"的正确策略。

---

### Turn 5 — 完成声明

**Agent**: 总结结果并声明 `ACTION: TASK_COMPLETE`

### System Verification

```
## Eval Result (case 1): PASS

  Sheet2!B15: got=4, expected=4 ✓
  Sheet2!C15: got=6, expected=6 ✓
  Sheet2!D15: got=9, expected=9 ✓
  Sheet2!E15: got=4, expected=4 ✓
```

### 轨迹总结

| 维度 | 值 |
|------|-----|
| 总 turn 数 | 5 个 agent turn |
| bash 调用数 | 4 次 |
| 认知模式 | 探索→适应→分析→计算+验证 |
| 关键成功因素 | 用代码计算而非纯推理；发现并纠正手算错误 |
| 典型模式 | python→python3 适应；先读后算；一次性完成计算+写入+验证 |

---

## Trajectory 2: Task 208-20 / Rollout 0 — FAIL

**Task type**: 对 Actual Results 表按 Expected Results 表的顺序重排行  
**Result**: FAIL (F7 公式值: gt='UPL.NS', pred=None)  
**Turns**: 18 messages (9 agent + 8 observations + 1 system verification)  
**Actions**: 8 bash calls

### Turn 1 — 初始数据探索

**Agent Thought**: 需要理解 spreadsheet 结构，识别 Actual Results 和 Expected Results 表，然后排序。

**Action**: 用 `python -c` 读取 xlsx，打印所有行的 cell 值。

**Observation**: 
```
/bin/sh: 1: python: not found
[Exit code: 127]
```

---

### Turn 2 — python3 适应

**Agent Thought**: Python isn't available as `python`, let me try `python3`.

**Action**: 用 `python3 -c` 重试，但遇到 MergedCell 错误。

**Observation**: 
```
Sheet name: Q
Max row: 37, Max col: 14

[STDERR]
AttributeError: 'MergedCell' object has no attribute 'column_letter'
[Exit code: 1]
```

> **分析**: 该 spreadsheet 包含合并单元格，这是 openpyxl 常见的陷阱。Agent 需要额外一步来处理。

---

### Turn 3 — 处理合并单元格

**Agent Thought**: There are merged cells causing issues. Let me adjust the code to handle merged cells properly.

**Action**: 改用 `ws.cell(row=, column=)` 和 `get_column_letter()` 来避免 MergedCell 属性访问。

**Observation**: 成功读取数据结构。可以看到：
- Columns A-F: Actual Results（行3-37，7个股票符号×5天）
- Columns I-N: Expected Results（同样结构，但顺序不同）
- F 列含有公式如 `=IF(NOT(ISBLANK(E3)),A3,"")`

> **关键观察**: 
> - Actual 顺序: ^NSEI → TCS.NS → LT.NS → SUNPHARMA.NS → UPL.NS → AXISBANK.NS → JUBLFOOD.NS
> - Expected 顺序: UPL.NS → AXISBANK.NS → ^NSEI → JUBLFOOD.NS → SUNPHARMA.NS → TCS.NS → LT.NS

---

### Turn 4 — 分别打印两表对比

**Action**: 详细打印 Actual Results (A2:F37) 和 Expected Results (I2:N37)。

**Observation**: 确认了数据完整性和两表的行顺序差异。每个符号都有5行数据。

---

### Turn 5 — 第一次重排尝试（BUG）

**Agent Thought**: 理解了任务——需要将 Actual Results 的行按 Expected Results 的顺序重排。

**Action**: 写 Python 脚本，按 expected_order 读取 actual_data，写入新顺序。

**BUG**: 
```python
for symbol in expected_order:
    if symbol in actual_data:
        rows = actual_data[symbol]
        for row_data in rows:  # <-- 每次遍历全部5行
            ...
            write_row += 1
```

**Observation**: 输出全是 UPL.NS 重复——因为 `expected_order` 有35个元素（每个符号出现5次），内层循环每次都写全部5行。

> **分析**: 经典的嵌套循环逻辑错误。Agent 后来误判为"引用同一列表对象"，实际是遍历逻辑问题。

---

### Turn 6 — 第二次尝试（仍然错误）

**Agent Thought**: 误以为问题是 Python 列表引用共享，决定用 `copy.deepcopy`。

**Action**: 添加 `copy.deepcopy(cell.value)` 但未修复核心循环逻辑。

**Observation**: 结果仍然完全是 UPL.NS 重复。

> **分析**: Agent 的诊断方向完全错误——cell.value 是基本类型（字符串/数字/datetime），不存在引用共享问题。真正的 bug 是 for 循环逻辑。

---

### Turn 7 — 第三次尝试（数据正确，公式残留）

**Agent Thought**: 现在正确识别了问题——expected_order 是35个元素的平铺列表，需要逐个消费每个 symbol 的行。

**Action**: 引入 `used_count` 字典跟踪每个 symbol 已使用的行数：
```python
used_count = {sym: 0 for sym in actual_data}
for symbol in expected_order:
    idx = used_count[symbol]
    if idx < len(actual_data[symbol]):
        row_data = actual_data[symbol][idx]
        ...
        used_count[symbol] += 1
```

**Observation**: 数据值（A-E列）完全正确匹配了 Expected Results。

> **但**: F 列的公式仍然引用原始行号，如 `=IF(NOT(ISBLANK(E23)),A23,"")` 而非更新后的 `=IF(NOT(ISBLANK(E3)),A3,"")`。

---

### Turn 8 — 验证对齐

**Action**: 逐行比较 A/I, B/J, C/K, D/L, E/M 列。

**Observation**: 
```
All rows match between Actual Results (A) and Expected Results (I)!
Columns B and J match for all data rows!
Columns C and K match for all data rows!
Columns D and L match for all data rows!
Columns E and M match for all data rows!
```

> **分析**: Agent 认为任务完成——但它忽略了 F 列的公式。验证只检查了 A-E 列。

---

### Turn 9 — 声明完成

**Agent**: "The output file has been successfully created." → `ACTION: TASK_COMPLETE`

### System Verification

```
## Eval Result (case 1): FAIL
value@Q!F7: gt='UPL.NS' pred=None
```

**失败原因**: F7 的公式 `=IF(NOT(ISBLANK(E27)),A27,"")` 引用的是原始行号，重排后 E27/A27 不再是 UPL.NS 的数据。在 Excel 中执行时公式会产生错误结果或空值。

> openpyxl 无法执行 Excel 公式，所以 agent 写入的公式字符串中的行引用（E27, A27 等）没有随行的移动而更新。这是 openpyxl 处理公式的根本性限制。

### 轨迹总结

| 维度 | 值 |
|------|-----|
| 总 turn 数 | 9 个 agent turn |
| bash 调用数 | 8 次 |
| 认知模式 | 探索→适应×2→分析→错误实现→错误诊断→正确实现→不完整验证 |
| 失败根因 | 公式中的单元格引用未随行移动更新（openpyxl 限制） |
| 次要问题 | 前两次实现有 bug（循环逻辑错误 + 错误归因于引用共享）|
| 典型教训 | (1) 移动含公式的行时必须更新公式引用；(2) 验证必须覆盖所有列包括公式列 |

---

## 两个轨迹的对比分析

| 维度 | Task 51359 (PASS) | Task 208-20 (FAIL) |
|------|-------------------|---------------------|
| 任务复杂度 | 中等（数值计算） | 高（数据重排+公式处理） |
| Agent turns | 5 | 9 |
| 代码迭代 | 1次（直接正确） | 3次（2次 bug 修复） |
| python 适应 | 1 turn | 1 turn + MergedCell 处理 |
| 核心失败点 | 无 | openpyxl 不执行/不更新公式 |
| 验证完整性 | 完整验证所有目标 cell | 只验证了 A-E 列，遗漏 F 列 |
| 认知深度 | 合理的自我质疑+代码纠正 | 3次错误诊断后才找到正确实现 |

### 关键观察

1. **python/python3 适应**是几乎所有轨迹的第一个 turn，浪费1-2轮交互
2. **openpyxl 公式问题**是最大的系统性失败来源（占所有失败的58%+）：agent 无法执行公式，也不会主动更新公式中的引用
3. **验证盲区**：agent 倾向于验证自己认为重要的列，而非exhaustively验证所有列
4. **错误诊断能力**有限：Task 208-20 中 agent 将循环逻辑 bug 归因于 Python 引用共享，反映了推理能力的不足
