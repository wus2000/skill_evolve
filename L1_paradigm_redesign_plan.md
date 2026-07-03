# L1 行为范式改造方案 — 全盘设计文档

> 经多轮探讨对齐后的完整改造方案。涵盖所有改动点、具体设计、数据流、实施顺序。
> 所有设计严格任务无关(task-agnostic)——领域特异性由注入的数据承载。

---

## 一、设计原则(贯穿所有改动)

| 原则 | 旧 | 新 |
|---|---|---|
| 策略的本质 | 认知框架("how to think") | **行为范式**:覆盖 agent 全程 ReAct 轨迹弧线的多阶段方案(有阶段、转移逻辑、回路) |
| 策略的生成方式 | 从根因**推导**(derive not invent) | 由优化器**设计**(design):经验依据(轨迹分析) + 任务理解(优化器自身知识),双源汇流 |
| altitude 边界 | 认知哲学 vs 战术规则 | **轨迹弧线的形状**(阶段结构、行为模式、转移逻辑)vs **阶段内的执行细节**(具体 API/格式/边界情况) |
| altitude 自检 | 无 | 外部观察者光看动作序列(不看内容)就能判断 agent 是否在遵循该范式 → 正确海拔;需检查参数才能判断 → 太低 |
| 多样性判据 | 文本/哲学上不同 | 部署后**行为弧线形状可观察地不同**(动作序列、阶段分布、决策模式) |
| 分析视角 | 纯回溯(什么坏了→怎么修) | **回溯 + 前瞻**(什么坏了 + 这类任务还可以怎么做) |
| 分析管道目标 | 提取"认知观察"(agent 怎么想) | 提取"行为弧线标注"(agent 怎么做、走了什么路径、路径的哪里 work/不 work) |

---

## 二、策略文档格式(新)

**旧格式**:
```markdown
## <Strategy Name>
<一段认知哲学概述>

### Details
<认知理念详细展开>
```

**新格式**:
```markdown
## <Paradigm Name>
<概述:核心思路、为什么有效、与其他范式的本质区别。30 秒可读懂。>

### Phase 1: <阶段名>
<目的、进入条件、agent 在此阶段应做的核心行为(可观察的动作)、退出条件/转移信号>

### Phase 2: <阶段名>
<...>

### Phase N: <阶段名>
<...>

### Transitions & Recovery
<阶段间转移逻辑总结;遇到错误/意外时的回路——回到哪个阶段、怎么调整>
```

---

## 三、新架构全景:数据流总图

```
ALL train rollouts (全量轨迹,如 500×3=1500 条)
    │
    ╔══════════════════════════════════════════════════════════╗
    ║  分析管道(css/analysis/) — 全面改造                     ║
    ║                                                          ║
    ║  Layer 1: 逐条轨迹行为弧线标注                           ║
    ║    - 每条 1 次 LLM → ArcAnnotation                      ║
    ║      (弧线阶段分解 + 关键转折 + 弧线类型标签 + 层次判断) ║
    ║    - 每对对比轨迹 1 次 LLM → ArcDivergence              ║
    ║      (同任务成功/失败弧线的分叉点分析)                   ║
    ║                                                          ║
    ║  Layer 2: 弧线类型发现 + 代表轨迹选择                    ║
    ║    - 按 arc_label 聚类 → ArcType                        ║
    ║    - 每类: 名称、描述、成功率、优劣势、代表轨迹 ID       ║
    ║    - 失败弧线 ↔ 成功弧线配对                            ║
    ║                                                          ║
    ║  Layer 3: 弧线景观追踪(简化,确定性,无 LLM)            ║
    ║    - 每 epoch 的弧线类型分布 + 成功率                    ║
    ╚══════════════════════════════════════════════════════════╝
    │
    │ 产物: ArcType 列表 + ArcDivergence 列表
    │        + 代表轨迹 ID(分析过程天然选出,可追溯)
    │        + 弧线景观统计
    │
    ▼
┌─────────────────────────────────────────────────────────────┐
│ 冷启动 / L1 cycle 共享相同分析产物,各自构建 user 消息     │
│                                                             │
│ Step 1a: 基线/天花板分析(各自版本)                        │
│ Step 1b: 行为弧线全景分析(共享同一提示词,注入分析产物)   │
│ Step 1d: 范式设计简报(各自版本,综合 1a+1b)               │
│ Step 2:  范式设计(冷启动 INITIAL / L1 cycle NEW|REFINE)   │
│ [冷启动] Step 2.5: 自检精修                                │
│ [L1 cycle] Step 3→4: 真跑测试→范式诊断→循环              │
└─────────────────────────────────────────────────────────────┘
```

---

## 四、分析管道改造(`css/analysis/`)

### 4.1 Layer 1: 逐条轨迹行为弧线标注

**文件**: `css/analysis/layer1.py`
**改动程度**: 重大改造(提示词 + 输出结构)
**LLM 调用模式**: 不变(每条轨迹 1 次,全并行)

#### 4.1a `_SINGLE_SYSTEM` → 行为弧线标注

**旧**: 提取 3-8 个自由形式"认知观察"(what/cognitive_aspect/evidence/polarity)
**新**: 产出 1 个完整的行为弧线标注

**新提示词核心指令**:
```
你在为一条 AI agent 的任务执行轨迹做行为弧线标注。

轨迹是一个 ReAct 循环:Thought → Action → Observation 反复交替。

对这条轨迹,标注以下内容:

1. 弧线阶段分解:把轨迹切分为若干"阶段"——agent 在做同一类事的连续段。
   阶段数量由轨迹实际内容决定。对每个阶段记录:
   - 阶段名(简短描述)
   - 覆盖的轮次范围
   - agent 在此阶段做了什么动作(实际的 Action)
   - 此阶段对最终结果的贡献(推进了/没帮助/造成了问题)

2. 关键转折点:弧线中最影响结果的 1-2 个节点。引用具体轨迹内容。

3. 弧线类型标签:一个简短短语概括整体行为模式
   (如"先探索后提交"、"一步到位反复修补"、"分步验证逐段拼合")。

4. 层次判断:结果主要归因于:
   - "approach":行为路径本身(弧线结构层)
   - "execution":路径对但执行细节有误(执行层)
   - "both"
```

**新输出数据结构** (`ArcAnnotation`):
```python
@dataclass
class ArcAnnotation:
    task_id: str
    rollout_index: int
    outcome: str              # "success" | "failure"
    arc_phases: list[dict]    # [{phase, turns, actions, contribution}]
    arc_label: str            # 弧线类型标签(用于 Layer 2 聚类)
    critical_points: list[dict]  # [{turn, what, impact, evidence}]
    level: str                # "approach" | "execution" | "both"
```

#### 4.1b `_CONTRASTIVE_SYSTEM` → 弧线分叉分析

**旧**: 找"认知差异"(cognitive_difference)
**新**: 找"弧线分叉点"——同任务成功/失败轨迹的弧线在哪里分道、各自做了什么不同动作

**新输出数据结构** (`ArcDivergence`):
```python
@dataclass
class ArcDivergence:
    task_id: str
    divergence_phase: str       # 在哪个阶段分叉
    divergence_turn: int        # 大约在哪一轮
    success_action: str         # 成功轨迹在分叉点做了什么
    failure_action: str         # 失败轨迹在分叉点做了什么
    is_approach_difference: bool  # 分叉是弧线结构差异(True)还是执行细节差异(False)
    evidence: str
```

### 4.2 Layer 2: 弧线类型发现 + 代表轨迹选择

**文件**: `css/analysis/cluster.py` + `css/analysis/label_grouping.py`
**改动程度**: 重大改造

**两阶段流程**:

**阶段一:弧线类型聚类**
1. 预分组(算法):用 `arc_label` 做 Jaccard/编辑距离预分组
2. LLM 归并(批量):把相近的 arc_label 归并为同义组
3. 每组 LLM 提炼(每组 1 次):读该组所有 ArcAnnotation,产出:

**新输出数据结构** (`ArcType`):
```python
@dataclass
class ArcType:
    arc_type_id: str
    name: str                     # 弧线类型名(如"逐步验证型")
    description: str              # 典型阶段结构和行为特征
    n_trajectories: int
    success_rate: float
    task_fit: str                 # 最常出现在什么类型的任务上
    strengths: str                # 什么情况下 work
    weaknesses: str               # 什么情况下 fail
    exemplar_ids: list[tuple]     # [(task_id, rollout_index, reason)] 代表轨迹
    member_ids: list[tuple]       # 所有成员轨迹 ID(可追溯)
    counterpart_id: str | None    # 配对的成功/失败弧线类型 ID
```

**阶段二:弧线类型配对**
- 失败主导弧线类型 ↔ 成功主导弧线类型配对(LLM 或标签匹配)

**可追溯性链**:
```
范式设计引用的"代表轨迹"
  ← Layer 2 的 exemplar_ids(分析过程产物,非外部抽样)
    ← Layer 1 的 ArcAnnotation(逐条标注,含 turn 范围)
      ← 原始轨迹 TaskResult.messages
```

### 4.3 Layer 3: 弧线景观追踪(简化)

**文件**: `css/analysis/pipeline.py` (或 `longitudinal.py`)
**改动程度**: 大幅简化

**删除**: l1_signal 复杂检测(min_task_fraction 门槛、declining trend 检测等)。v3 中 L1 触发由 orchestrator 基于 L0 exploitation 饱和决定。

**保留**(确定性,无 LLM):
```python
def record_arc_landscape(arc_types: list[ArcType], epoch: int):
    """Record per-epoch arc type distribution."""
    # 每个 arc_type 的:轨迹数、成功率
    # 跨 epoch 对比:新出现/消失的弧线类型、成功率变化
```

### 4.4 管道整体编排

**文件**: `css/analysis/pipeline.py`
**新的 `run_analysis_epoch` 流程**:
```
Layer 1: run_layer1(groups) → list[ArcAnnotation] + list[ArcDivergence]
Layer 2: build_arc_types(annotations) → list[ArcType] (含代表轨迹)
Layer 3: record_arc_landscape(arc_types, epoch)
产出: ArcLandscape(arc_types, divergences, statistics)
```

### 4.5 新增渲染函数

```python
def render_arc_landscape_for_design(
    arc_types: list[ArcType],
    divergences: list[ArcDivergence],
    *,
    max_types: int = 15,
) -> str:
    """Render arc landscape as structured text for paradigm design prompts.
    Includes: arc types sorted by trajectory count, success rates,
    strengths/weaknesses, counterpart pairings, representative evidence."""

def render_exemplar_trajectories(
    arc_types: list[ArcType],
    all_results: dict[tuple, TaskResult],  # (task_id, rollout) -> result
    *,
    max_exemplars: int = 8,
    tool_trunc: int = 300,
) -> str:
    """Render the exemplar trajectories (selected by Layer 2) as full text.
    These are analysis-selected representatives, not random samples."""
```

---

## 五、L1 Cycle 提示词改造(`css/proposal/proposal.py`)

### 5.1 `_STEP1A_SYSTEM` — L0 天花板分析 + 当前行为弧线

**改动程度**: 微调

在现有"为什么 L0 停滞"的分析基础上,新增:
- 当前策略引导的行为弧线是什么?
- 这个弧线是否对所有任务类型都适用?

**新增输出字段**: `current_behavioral_arc`, `arc_task_mismatch`

### 5.2 `_STEP1B_SYSTEM` — 行为弧线全景分析

**改动程度**: 重大改造(角色 + 输入 + 输出)

**角色**: 行为模式分析师,做全景式行为弧线分析

**输入**(user 消息构建):
```
1. 弧线类型全景(Layer 2 产物):
   render_arc_landscape_for_design(arc_types, divergences)

2. 对比分歧汇总(Layer 1 ArcDivergence):
   render_divergences(divergences)

3. 代表轨迹原文(Layer 2 选出的 exemplar,可追溯):
   render_exemplar_trajectories(arc_types, results)

4. 代表性任务描述 + 动作空间描述
```

**分析目标**:
1. 弧线全景:发现了哪些弧线类型?哪些通向成功、哪些通向失败?
2. 弧线-任务匹配:不同弧线类型适合什么任务?失败弧线为什么在特定任务上失效?
3. 失败任务需求:从任务本身出发,这些任务需要怎样的行为弧线?当前弧线缺什么?

**输出**: 行为弧线全景分析 JSON

### 5.3 `_STEP1C_SYSTEM` — 合并进 5.2,取消独立步骤

对比分析已融入 5.2 的输入(ArcDivergence)和分析目标。

### 5.4 `_STEP1D_SYSTEM` — 范式设计简报

**改动程度**: 重大改造

**角色**: 为行为范式设计师准备设计简报

**输入**: 1a 分析 + 1b 全景 + 弧线景观统计(Layer 3)+ 循环台账

**产出**:
```json
{
  "paradigm_space": {
    "current": "<当前范式的行为弧线>",
    "tried": [{"paradigm": "...", "arc_shape": "...", "result": "..."}],
    "unexplored": ["<值得尝试的弧线构想>"]
  },
  "design_requirements": {
    "target_failures": "<最紧迫的失败类群及其弧线需求>",
    "constraints": {
      "preserve": ["<有效行为>"],
      "avoid": ["<已验证有害方向>"]
    }
  },
  "design_materials": {
    "success_arcs": ["<可供系统化的成功弧线>"],
    "agent_capabilities": "<动作空间概述>",
    "task_understanding": "<对任务本质需求的理解>"
  }
}
```

**关键**: `unexplored` 和 `task_understanding` 允许且鼓励优化器基于自身理解做前瞻性提议。

### 5.5 `_STEP2_SYSTEM` — 行为范式设计(核心改造)

**改动程度**: 核心改造——角色、任务、格式全变

**角色**: 行为范式设计师

**两种模式**(L1 cycle 用):
- **MODE = NEW**: 设计与所有已试范式在弧线形状上根本不同的新范式
- **MODE = REFINE**: 保持当前弧线结构,调整某些阶段的运作方式

**设计依据要求**: 经验依据(弧线分析产物) + 任务理解(优化器知识),缺一不可

**altitude 重新定义**(含自检方法):
- 你管: 弧线形状(阶段结构 + 转移逻辑 + 回路)
- 不管: 阶段内执行细节(API/格式/边界情况)
- 自检: 光看动作序列能判断是否在遵循 → 正确;需检查参数 → 太低

**输出格式**: Phase 结构文档 + `behavioral_difference` + `design_grounding` + `expected_behavioral_change`

### 5.6–5.8 Step 4 分析器(cracked/regressed/still_failed)

**统一改动**: 从"认知动作"视角转为"范式阶段"视角

- **cracked**: 新范式的哪个**阶段/转移**解锁了任务?→ `phase_that_unlocked`
- **regressed**: 保留 handicap/harm,重新定义:
  - handicap = 弧线结构合理,阶段内缺执行细节(L0 补回即恢复)
  - harm = 弧线结构本身误导(阶段设计/转移逻辑问题)
  - 新增: `if_harm_which_phase`
- **still_failed**: 任务需要什么弧线?当前范式缺什么阶段/行为?→ `paradigm_gap`

### 5.9 `_DIAGNOSE_AGGREGATE_SYSTEM` — 范式评估

**改动程度**: 重大改造

**新产出维度**:
1. **adherence**(范式遵循度):agent 是否真的按阶段结构行动?
2. **effective_phases** / **problematic_phases**:哪些阶段有效/有问题?
3. **harm_summary**:真正的弧线设计问题(非 handicap)
4. **remaining_needs**:残余失败需要什么弧线/阶段
5. **next_action**: propose_new | refine_current(含理由)
6. **design_feedback**:给下轮设计师的具体输入

---

## 六、冷启动改造(`css/coldstart.py`)

### 6.1 新冷启动流程

```
裸跑 rollout(不变)
    ↓
分析管道(同 L1 用的同一套 Layer 1/2/3,喂裸跑轨迹)
    ↓
Step 1a: 自然能力基线分析(冷启动专用 _COLDSTART_BASELINE_SYSTEM)
Step 1b: 行为弧线全景分析(复用 L1 的 _STEP1B_SYSTEM,注入裸跑数据)
    ↓
Step 1d: 初始范式设计简报(冷启动专用 _COLDSTART_BRIEF_SYSTEM)
    ↓
Step 2: 初始范式设计(冷启动专用 _COLDSTART_DESIGN_SYSTEM,MODE=INITIAL)
    ↓
Step 2.5: 自检精修(_COLDSTART_CRITIQUE_SYSTEM)
    聚焦安全性(不破坏自然成功行为) + 可执行性(每阶段可用工具执行)
    verdict=revise 时取 revised_strategy_text,不做迭代循环
    ↓
strategy_0 = 最终范式
```

### 6.2 冷启动专用提示词

#### `_COLDSTART_BASELINE_SYSTEM` (Step 1a)
分析 agent 裸跑时的自然能力边界:能做什么、不能做什么、指导空间多大。

#### `_COLDSTART_BRIEF_SYSTEM` (Step 1d)
为"从零设计第一个范式"准备设计简报:自然行为地图 + 任务需求 + 设计素材 + 设计目标。无台账(首次)。

#### `_COLDSTART_DESIGN_SYSTEM` (Step 2)
独立提示词(不混入 L1 的 NEW/REFINE)。
两个设计来源: ① 系统化裸跑中偶然出现的成功行为 ② 补充任务需要但缺失的阶段。
强调可遵循性(agent 首次接受行为指导)。

#### `_COLDSTART_CRITIQUE_SYSTEM` (Step 2.5)
拿成功轨迹检查安全性(不破坏自然成功行为)。
拿动作空间检查可执行性(每阶段可做)。
不预测有效性(不可靠)。verdict=revise 时自带修订版,一次即止。

### 6.3 冷启动兜底

`_fallback_strategy_0()` 改为新的 Phase 结构:
```markdown
## Systematic Explore-Then-Execute
<概述>

### Phase 1: Understand
### Phase 2: Explore
### Phase 3: Solve
### Phase 4: Verify
### Transitions & Recovery
```

### 6.4 移除旧依赖

移除 `coldstart.py` 对 `root_cause.attribute_root_cause()` 和 `derivation.derive_strategy()` 的调用。

---

## 七、新增接口(`css/envs/`)

### 7.1 `TaskEnv.action_space_description()`

在 `css/envs/base.py` 的 `TaskEnv` 协议中新增:
```python
def action_space_description(self) -> str:
    """Human-readable description of the agent's available actions
    and interaction loop structure."""
    return ""
```

各环境实现:
- **Bird**: execute_sql + submit_final_sql + ReAct 循环说明
- **SpreadsheetBench**: Python 代码执行 + 文件读写 + ReAct 循环说明

**注入点**: Step 1d 设计简报 + Step 2 范式设计 + 冷启动对应步骤的 user 消息中。

---

## 八、辅助改动

### 8.1 `css/skill_document.py`

```python
# 旧
"# Cognitive Strategy\n" + strategy
# 新
"# Task-Solving Approach\n" + strategy
```

### 8.2 `css/optimizer/reflect.py`

**`_EDIT_SPEC`** 小幅改措辞:
```
旧: strategy.md is READ-ONLY context — rules.md executes that strategy
新: strategy.md describes the agent's task-solving approach (phase structure).
    It is READ-ONLY. rules.md provides execution details within that approach.
```

**三个 proposer 提示词**: 加一句 altitude 下界说明,明确 L0 在阶段内工作。

### 8.3 Legacy 标注

以下文件在 v3 中不再被调用,统一在文件开头 docstring 加注:
- `css/proposal/root_cause.py` → "LEGACY in v3"
- `css/proposal/derivation.py` → "LEGACY in v3"
- `css/proposal/refine.py` → "LEGACY in v3"
- `css/proposal/inheritance.py` → 已标注 "DEAD in v3"

---

## 九、不变的部分

| 组件 | 不变理由 |
|---|---|
| Step 3 测试机制(空 rules 跑 K 次) | 客观测试,设计正确 |
| keep-best 排序(net_lift → deploy_net → lift) | 客观选择,无需改 |
| handicap/harm 区分框架 | 保留,只在弧线层重新定义 |
| GT 防火墙 | 贯穿不变 |
| JSON-only 输出 + LLM JSON 修复 | 不变 |
| negative archive 写入 | 保留(归档失败范式) |
| Layer 1 处理模式(逐条并行) | 不变(只改提取内容) |
| 轨迹格式 / format_trajectory | 格式无关,不变 |

---

## 十、完整改动清单(按编号)

### 分析管道(`css/analysis/`)
| # | 文件 | 改动 | 程度 |
|---|---|---|---|
| ① | `layer1.py` `_SINGLE_SYSTEM` | → 行为弧线标注 | 重大 |
| ② | `layer1.py` `_CONTRASTIVE_SYSTEM` | → 弧线分叉分析 | 重大 |
| ③ | `layer1.py` 数据结构 | Observation → ArcAnnotation | 重大 |
| ④ | `cluster.py` | → 弧线类型聚类 + 代表轨迹选择 | 重大 |
| ⑤ | `cluster.py` 数据结构 | PatternRecord → ArcType | 重大 |
| ⑥ | `pipeline.py` | 适配新 Layer 1/2 接口 | 中等 |
| ⑦ | Layer 3 | 简化为弧线景观追踪,删 l1_signal 检测 | 中等 |
| ⑧ | 新增渲染函数 | render_arc_landscape_for_design / render_exemplar_trajectories | 新增 |

### L1 Cycle(`css/proposal/proposal.py`)
| # | 提示词 | 改动 | 程度 |
|---|---|---|---|
| ⑨ | `_STEP1A_SYSTEM` | 微调:加当前弧线描述 | 小 |
| ⑩ | `_STEP1B_SYSTEM` | → 行为弧线全景分析(消费分析管道产物) | 重大 |
| ⑪ | `_STEP1C_SYSTEM` | 合并进 ⑩,取消 | 删除 |
| ⑫ | `_STEP1D_SYSTEM` | → 范式设计简报 | 重大 |
| ⑬ | `_STEP2_SYSTEM` | → 行为范式设计(MODE=NEW/REFINE) | 核心 |
| ⑭ | `_CRACKED_ANALYZER` | → 阶段视角 | 中等 |
| ⑮ | `_REGRESSED_ANALYZER` | → 阶段视角(保留 handicap/harm) | 中等 |
| ⑯ | `_STILLFAILED_ANALYZER` | → 阶段视角 | 中等 |
| ⑰ | `_DIAGNOSE_AGGREGATE` | → 范式评估 | 重大 |
| ⑱ | `_step1b()` user 消息构建 | 注入分析管道产物 + 代表轨迹 + 任务描述 | 中等 |
| ⑲ | `_step1d()` user 消息构建 | 注入弧线景观统计 | 小 |
| ⑳ | `_run_step2()` user 消息构建 | 注入动作空间 + 任务描述 | 小 |

### 冷启动(`css/coldstart.py`)
| # | 改动 | 程度 |
|---|---|---|
| ㉑ | 新增 `_COLDSTART_BASELINE_SYSTEM` (Step 1a) | 新增 |
| ㉒ | Step 1b 复用 L1 的 `_STEP1B_SYSTEM`,构建裸跑版 user 消息 | 中等 |
| ㉓ | 新增 `_COLDSTART_BRIEF_SYSTEM` (Step 1d) | 新增 |
| ㉔ | 新增 `_COLDSTART_DESIGN_SYSTEM` (Step 2, MODE=INITIAL) | 新增 |
| ㉕ | 新增 `_COLDSTART_CRITIQUE_SYSTEM` (Step 2.5) | 新增 |
| ㉖ | `_fallback_strategy_0()` → Phase 结构 | 小 |
| ㉗ | 移除 root_cause/derivation 调用 | 中等 |

### 新增接口
| # | 文件 | 改动 |
|---|---|---|
| ㉘ | `css/envs/base.py` | 新增 `action_space_description()` |
| ㉙ | `css/envs/bird/task_interface.py` | 实现 |
| ㉚ | SpreadsheetBench 环境 | 实现 |

### 辅助改动
| # | 文件 | 改动 | 程度 |
|---|---|---|---|
| ㉛ | `skill_document.py` | 标题改 | 小 |
| ㉜ | `reflect.py` `_EDIT_SPEC` | 措辞微调 | 小 |
| ㉝ | `reflect.py` 三个 proposer | 加 altitude 下界说明 | 小 |
| ㉞ | `root_cause.py` / `derivation.py` / `refine.py` | 标注 LEGACY | 小 |

**总计**: 34 项改动(8 分析管道 + 12 L1 cycle + 7 冷启动 + 3 接口 + 4 辅助)

---

## 十一、实施顺序

```
═══ 第一批:核心闭环(最小可验证单元)═══

  ⑬ _STEP2_SYSTEM       → 行为范式设计(MODE=NEW/REFINE)
  ⑫ _STEP1D_SYSTEM       → 范式设计简报
  ⑰ _DIAGNOSE_AGGREGATE  → 范式评估
  ㉘㉙㉚ TaskEnv.action_space_description() + 各环境实现
  ⑳ Step 2 user 消息注入动作空间

  验证: 跑一轮 L1 cycle(用旧分析管道的产物,暂用旧 1b),
  观察策略是否产出 Phase 结构、是否行为上真正不同

═══ 第二批:分析管道全面改造 ═══

  ①②③ Layer 1 提示词 + 数据结构(ArcAnnotation)
  ④⑤ Layer 2 聚类 + 数据结构(ArcType)
  ⑥ pipeline.py 适配
  ⑦ Layer 3 简化
  ⑧ 渲染函数
  ⑩⑱ Step 1b 改造 + user 消息接入分析产物
  ⑨⑲ Step 1a 微调 + Step 1d user 消息
  ⑪ 取消 Step 1c

  验证: 跑完整 analysis → L1 cycle,确认分析产物正确接入策略设计

═══ 第三批:冷启动统一 ═══

  ㉑㉒㉓㉔㉕ 冷启动全部新提示词
  ㉖ fallback 新格式
  ㉗ 移除旧依赖

  验证: 从零跑一次完整实验(冷启动 → L0 → L1)

═══ 第四批:诊断层增强 + 收尾 ═══

  ⑭⑮⑯ cracked/regressed/still_failed 分析器改为阶段视角
  ㉛㉜㉝ 辅助改动(标题/措辞)
  ㉞ legacy 标注
```

---

## 十二、风险与缓解

| 风险 | 缓解 |
|---|---|
| Layer 1 新提示词产出质量不稳定(弧线阶段分解不准) | 逐步验证:先对 10 条轨迹跑新 Layer 1,人工审查产出后再全量 |
| ArcType 聚类结果太粗/太细 | 调 Jaccard 阈值 + LLM 归并粒度;先在小集上调参 |
| 新格式策略(Phase 结构)agent 不遵循 | Step 4 诊断的 adherence 维度会捕获此问题;不遵循说明需改进 operationalization |
| 分析管道数据结构大改导致下游全断 | 第一批先不改分析管道(用旧 PatternLibrary),验证核心闭环;第二批再换 |
| 冷启动新流程比旧流程更多 LLM 调用 | 多 3-4 次调用(1a + 1d + 2 + 2.5);Layer 1/2 调用数不变(只改内容) |
