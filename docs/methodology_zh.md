# CSS (Cognitive Strategy Search) 系统方法

## 1 系统概述

CSS 是一个基于蒙特卡洛树搜索 (MCTS) 的自动化优化系统。它为一个**冻结的任务执行 agent** 搜索最优的"技能文档" (skill document)——一对由认知策略 (strategy.md) 和战术规则 (rules.md) 组成的指导文档。该技能文档被注入 agent 的系统提示词中,引导其完成电子表格操作等复杂任务。

### 1.1 双层技能架构

CSS 的核心设计是将指导文档分为两个层级,每个层级由独立的优化器负责,职责严格隔离:

| 层级 | 文档 | 内容定位 | 优化器 | 运行时角色 |
|------|------|----------|--------|------------|
| L1 | strategy.md | **HOW to think** — 认知策略、心智模型、推理框架 | L1 PROPOSAL 循环 | L0 运行时只读 |
| L0 | rules.md | **WHAT to do** — 战术规则、检查清单、具体操作步骤 | L0 EXPLOITATION 循环 | 可编辑目标 |

这一划分确保 L0 优化器在固定的认知框架下搜索最优战术,而 L1 优化器在更高的抽象层上探索不同的思维方式,二者互不干扰。

### 1.2 搜索树结构

CSS 维护一棵搜索树,每个节点 (TreeNode) 持有一对 (strategy, rules),以及该节点的优化状态:

```
                        ┌──────────────┐
                        │   root       │
                        │ strategy_0   │
                        │ rules_0      │
                        └──────┬───────┘
                   ┌───────────┼───────────┐
              ┌────┴────┐ ┌───┴───┐  ┌────┴────┐
              │ node_1  │ │node_2 │  │ node_3  │
              │ strat_1 │ │strat_2│  │ strat_3 │
              │ rules_1 │ │rules_2│  │ rules_3 │
              └─────────┘ └───────┘  └─────────┘
```

每个节点维护的关键状态包括:
- `strategy` / `rules`: 当前技能文档内容
- `step_buffer` (StepBuffer): L0 步骤历史,用于饱和检测和趋势分析
- `best_score` / `best_rules`: 该节点历史最优的 val 分数及对应 rules
- `val_score`: 最近一次选择集评分 (用于 UCB1)
- `status`: `"active"` | `"pruned"`

## 2 总体架构与轮次循环

系统以轮次 (round) 为单位运行搜索循环,每轮依次执行 SELECT → EPOCH → SYNC 三个阶段:

```
 ┌─────────────────────────────────────────────────────────────────────┐
 │                        CSS MAIN LOOP                               │
 │                                                                     │
 │  冷启动 (epoch 0):                                                  │
 │    根节点 ← (空 strategy, 空 rules)                                 │
 │    初始 val 分数 ← 在 val set 上评估                                │
 │                                                                     │
 │  每轮循环:                                                          │
 │  ┌───────────────────────────────────────────────────────────────┐  │
 │  │ 1. SELECT  (UCB1)                                             │  │
 │  │    活跃节点 → UCB1 评分排序 → 选择 top-K 批次                  │  │
 │  └────────────────────────┬──────────────────────────────────────┘  │
 │                           ↓                                         │
 │  ┌───────────────────────────────────────────────────────────────┐  │
 │  │ 2. EPOCH  (每个选中节点,可并行)                                │  │
 │  │    decide_branch(node):                                       │  │
 │  │      未饱和 → run_exploitation_epoch (L0 内层循环)              │  │
 │  │      已饱和 → run_l1_cycle (L1 PROPOSAL 策略搜索)              │  │
 │  └────────────────────────┬──────────────────────────────────────┘  │
 │                           ↓                                         │
 │  ┌───────────────────────────────────────────────────────────────┐  │
 │  │ 3. SYNC  (同步点)                                              │  │
 │  │    - 在 val set + test set 上评估所有更新的节点                 │  │
 │  │    - PRUNE: 配对 bootstrap 兄弟支配检验                        │  │
 │  │    - BRANCH: L1 循环产出子节点 → 加入搜索树                    │  │
 │  │    - 重置饱和计数器准备下一 epoch                               │  │
 │  └───────────────────────────────────────────────────────────────┘  │
 └─────────────────────────────────────────────────────────────────────┘
```

分支决策由 `css/tree/branching.py:decide_branch` 实现——纯粹基于 L0 饱和状态的确定性规则:
- **未饱和** → `"EXPLOITATION"` (继续 L0 内层优化)
- **已饱和** (连续 N 次 reject) → `"PROPOSAL"` (触发 L1 策略搜索)

## 3 L0 Exploitation (V2) — 内层优化循环

L0 Exploitation 是系统中最频繁运行的核心模块,负责在固定认知策略下迭代优化 `rules.md`。其完整流程分为五个阶段,每个阶段具有独立的检查点 (checkpoint) 以支持断点恢复:

```
┌─────────────────────────────────────────────────────────────────────┐
│  EXPLOITATION EPOCH (css/optimizer/exploitation.py:run_exploitation_epoch)│
│                                                                         │
│  train_items → shuffle(seed) → 按 batch_size 切块                      │
│                                                                         │
│  对每个 batch 循环 (直到饱和 OR 达到 max_l0_steps_per_epoch):           │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │ ON-POLICY ROLLOUT (在策略采样)                                    │  │
│  │   batch 任务 × K 次 rollout → TaskResult[] (新鲜轨迹)             │  │
│  │   检查点: step{N}/rollout/                                        │  │
│  └──────────────────────────┬────────────────────────────────────────┘  │
│                             ↓                                           │
│  ┌───────────────────────────────────────────────────────────────────┐  │
│  │ run_l0_step (基于当前 batch 轨迹的一个 L0 步骤)                    │  │
│  │                                                                   │  │
│  │  阶段 1: REFLECT(轨迹分析)     → raw_patches.json                │  │
│  │  阶段 2: MERGER(整合重构)      → merged_edits.json               │  │
│  │  阶段 3: 逐条 EDIT 消融验证     → edit_verifications.json         │  │
│  │  阶段 4: 集体 APPLY + 大小检查                                    │  │
│  │  阶段 5: 最终 VAL GATE 评估    → gate_decision.json               │  │
│  │  阶段 6: 记录 → StepBufferEntry                                   │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│  Epoch 终止条件:                                                        │
│    - StepBuffer.is_saturated(N) = True (连续 N 次 reject)               │
│    - 或 steps_this_epoch >= max_l0_steps_per_epoch                      │
│  退出时更新: node.best_score / best_step / best_rules                   │
└─────────────────────────────────────────────────────────────────────────┘
```

### 3.1 Stage 1: ON-POLICY 采样

每个 L0 步骤获取一批新鲜的 on-policy 轨迹,而非重复使用旧轨迹。`run_exploitation_epoch` 按 `cfg.batch_size` 切分 train 数据并为每个 batch 执行独立的 on-policy rollout:

- **输入**: batch 中的 task items, 当前 `node.rules` + `node.strategy` 渲染的 skill document
- **输出**: `TaskResult[]` — 每个任务 K 次 rollout 的完整轨迹和评分
- **关键设计**: 每个 step 使用节点*当前*的 rules (可能已在前序 step 中更新),确保分析的是当前策略下的行为,而非过时的 off-policy 轨迹
- **确定性**: 每个 batch round 使用 `seed + epoch + batch_round * 10000` 的种子进行 shuffle

### 3.2 Stage 2: REFLECT — 轨迹分析与编辑提议

**模块**: `css/optimizer/reflect.py:reflect_epoch`

Reflect 阶段将 on-policy 轨迹分析为原始编辑提议 (RawPatch)。系统支持三种 reflect 模式 (由 `cfg.reflect_mode` 控制):

| 模式 | 流程 | 特点 |
|------|------|------|
| `"legacy"` | 失败/成功轨迹分开分析 | 平面 minibatch 拆分 |
| `"plan_a"` (默认) | 三路分析 (失败/成功/对比) → 各自直接产生编辑 | 每 minibatch 独立产出少量单主题编辑 |
| `"plan_b"` | 成功分析作为上下文注入失败分析 | 成功洞察引导失败修复方向 |

以默认的 **Plan A** 为例,其流程如下:

```
          TaskResult[] (on-policy 轨迹)
                    │
        ┌───────────┼───────────┐
        ↓           ↓           ↓
 ┌──────────┐ ┌──────────┐ ┌──────────┐
 │ 按任务   │ │ 分类     │ │          │
 │ 分组     │→│ 三路拆分 │→│ 三路分类  │
 └──────────┘ └──────────┘ └──────────┘
        │           │           │
  纯失败组     纯成功组     混合组
        │           │           │
        ↓           ↓           ↓
  ┌──────────┐┌──────────┐┌──────────┐
  │ 失败分析 ││ 成功分析 ││ 对比分析  │  ← 每 minibatch/每任务
  │ PROPOSER ││ PROPOSER ││ PROPOSER  │    融合分析+编辑生成(并行)
  └──────────┘└──────────┘└──────────┘
        │           │           │
        └───────────┼───────────┘
                    ↓
            list[RawPatch]
            (每条 edit 携带 source_tasks 归因)
```

**关键设计决策**:

1. **Per-minibatch 融合 proposer**: 每个 minibatch 直接产生少量 (至多 `l0_edit_budget=3`) 单主题编辑,而非先做全局分析再生成编辑。小作用域保证了编辑的聚焦性
2. **source_tasks 归因**: 每条 edit 携带 `source_tasks` 列表,追溯其来源任务,为后续 per-edit ablation verification 提供 target tasks
3. **并发执行**: 所有 minibatch 的 proposer 通过 `ThreadPoolExecutor` 并行调用
4. **失败免疫**: 单个 analyst 调用失败不会崩溃整个流程,而是产出空 patch
5. **历史注入**: 通过 `step_buffer` 提供近期失败模式 (`failure_patterns`) 和被拒绝编辑 (`rejected_edits`),避免重复无效提议

**输入/输出**:
- **输入**: optimizer_client, strategy (只读), rules (编辑目标), epoch_results, step_buffer
- **输出**: `list[RawPatch]` — 每个 RawPatch 包含 `Patch` (一组 `Edit` 对象), `source_type` ("failure" / "success" / "contrastive"), `batch_size`

### 3.3 Stage 3: MERGER — Section 级整合

**模块**: `css/optimizer/aggregate.py:merger`

Merger 将多个 RawPatch 中的原始编辑整合为 section 级别的 MergedEdit 对象,每个 MergedEdit 对应 `rules.md` 中一个 `###` section 的完整目标状态。

```
  list[RawPatch] (来自 Reflect)
  + current rules.md
  + step_buffer history (可选)
         │
         ↓
  ┌──────────────────────────────────────────┐
  │  MERGER (单次 LLM 调用)                    │
  │                                            │
  │  System prompt: 7+1 条合并原则              │
  │  User prompt:                              │
  │    1. Current rules.md                     │
  │    2. Section index (现有 ### 标题列表)     │
  │    3. Raw edits (带 source_tasks)          │
  │    4. Optimization history (可选, 窗口=3)  │
  │    5. Budget (max_edits_per_step=6)        │
  │                                            │
  │  Output: JSON {reasoning, edits: [...]}    │
  └──────────────────┬─────────────────────────┘
                     ↓
  ┌──────────────────────────────────────────┐
  │  _validate_merged_edits (后验证)           │
  │  - content 必须以 "### " 开头             │
  │  - delta_type ∈ {new_section,              │
  │    section_rewrite, section_refinement}    │
  │  - target_tasks 非空                       │
  │  - 不允许重复 section_target               │
  │  - 自动修正: rewrite 指向不存在的 section   │
  │    → 转为 new_section                      │
  └──────────────────┬─────────────────────────┘
                     ↓
            list[MergedEdit]
```

**7 条合并原则** (定义于 `_MERGER_PRINCIPLES_1_7`):

| # | 原则 | 含义 |
|---|------|------|
| 1 | ONE EDIT PER SECTION | 多个指向同一 section 的原始编辑必须合并为一条 |
| 2 | GROUP BY CONTENT | 按内容主题分组,不按来源类型 (失败/成功) |
| 3 | GAP-ALIGN | 根据 section index 选择 delta_type: 已有 section → rewrite/refinement, 新主题 → new_section |
| 4 | PRESERVE EXISTING | rewrite/refinement 必须输出完整 section (现有保留 + 变更) |
| 5 | RESOLVE CONTRADICTIONS | 冲突时保留支持度更高的版本 |
| 6 | DERIVATION TRANSPARENCY | rationale 和 derivation 必须详尽记录审计轨迹 |
| 7 | QUALITY OVER QUANTITY | 少量高置信度编辑优于大量投机性编辑 |

当 `merger_inject_history=True` 时追加**原则 8: LEARN FROM HISTORY** — 注入最近 `merger_history_window=3` 步的 per-edit verification 历史,使 merger 能从历史验证结果中学习。

**MergedEdit 数据结构** (定义于 `css/data/edit.py`):

| 字段 | 说明 |
|------|------|
| `section_target` | `"### Data Loading"` — 目标 section 的精确标题 |
| `delta_type` | `"new_section"` / `"section_rewrite"` / `"section_refinement"` |
| `after_section` | 仅 new_section 使用: `"_end"` / `"_start"` / 现有标题 |
| `content` | 完整 section 文本 (### 标题 + 正文), 永不截断 |
| `target_tasks` | 来源任务 ID 列表 (来自 raw edits 的 source_tasks 并集) |
| `rationale` | 详细的改进理由 (问题 + 预期效果) |
| `derivation` | 从原始编辑到合并编辑的推导过程审计记录 |

### 3.4 Stage 4: 逐条编辑消融验证

**模块**: `css/optimizer/exploitation.py:_run_parallel_edit_verification`, `_verify_single_edit`

每条 MergedEdit 独立验证其对 target_tasks 的影响。这是 V2 架构中保证编辑质量的关键环节:

```
  list[MergedEdit] (来自 Merger)
         │
         ↓  (并行, ThreadPoolExecutor)
  ┌────────────────────────────────────────────────┐
  │  _verify_single_edit (单条 edit 验证)            │
  │                                                 │
  │  1. LLM 应用: rules + edit → 候选 rules          │
  │     (通过 write_rules_md 工具调用)                │
  │                                                 │
  │  2. Rollout: 候选 rules × 目标任务 × K 次        │
  │     → 每个任务的 TaskRolloutGroup                 │
  │                                                 │
  │  3. 逐任务可解性分类:                             │
  │     对比 incumbent 基线 vs 候选:                  │
  │     GAINED / LOST / RETAINED / STILL_UNSOLVED    │
  │                                                 │
  │  4. 二值可解性判定 → 接受 / 拒绝                  │
  │     (详见 §4)                                    │
  │                                                 │
  │  输出: EditVerification                          │
  └────────────────────────────────────────────────┘
```

**关键设计决策**:

1. **独立性保证**: 每条 edit 在独立的 candidate_rules 上测试 (仅应用该条编辑),互不干扰,实现了真正的消融
2. **target_tasks 精准对焦**: 每条 edit 仅在其声称影响的任务上验证 (来自 merger 的 `target_tasks` 字段),而非全集
3. **incumbent_baselines**: 从当前 epoch 的 on-policy rollout 中提取每个 task 的基线表现 (`pass_rate`, `solvable`),作为对比基准
4. **失败安全**: 单条 edit 验证失败 (异常) 不会崩溃流程,标记为 `passed=False` 继续

**EditVerification 数据结构** (定义于 `css/data/step_buffer.py`):

| 字段 | 说明 |
|------|------|
| `section_target` | 目标 section 标题 |
| `delta_type` | 编辑类型 |
| `content` | 完整 section 内容 (永不截断, 用于 checkpoint 和 history 注入) |
| `target_tasks` | 验证使用的任务 ID 列表 |
| `passed` | 是否通过二值可解性判定 |
| `task_results` | `{task_id: {inc_pr, cand_pr, inc_solvable, cand_solvable, status}}` |

### 3.5 Stage 5: 集体 APPLY + 最终 VAL GATE

存活 (passed=True) 的编辑被集体应用到 `rules.md` 上,然后在完整 val set 上评分:

```
  存活的 EditVerification[]
         │
         ↓
  ┌──────────────────────────────────────┐
  │ llm_apply_edits (单次 LLM 调用)       │
  │   所有存活编辑 → 一次性应用            │
  │   工具调用: write_rules_md             │
  │   失败时 fallback 到确定性 apply       │
  └──────────────────┬───────────────────┘
                     ↓
  ┌──────────────────────────────────────┐
  │ 大小检查 (软上限)                      │
  │   rules_max_chars=60000              │
  │   仅打印警告日志, 不截断内容            │
  └──────────────────┬───────────────────┘
                     ↓
  ┌──────────────────────────────────────┐
  │ 候选评分 (完整 val set)                │
  │   候选 rules × val 任务 × K 次        │
  │   → task_hard (任务级平均通过率)        │
  └──────────────────┬───────────────────┘
                     ↓
  ┌──────────────────────────────────────┐
  │ Gate 判定 (纯决策函数,无副作用)         │
  │   候选分 > 当前分:                     │
  │     → 接受 (若同时 > 历史最优则记录)    │
  │   否则:                               │
  │     → 拒绝                            │
  └──────────────────────────────────────┘
```

**LLM Apply 机制** (定义于 `css/optimizer/section_apply.py`):

LLM apply 使用 function calling 模式 (工具 `write_rules_md`),确保输出格式的结构化:
- System prompt 指示模型做纯机械性的文档编辑 (忠实复制 edit content, 保留未修改部分)
- 通过 `complete_tool_call` 提取结构化输出
- 失败时 fallback 到确定性的 `apply_section_edit` (基于 section heading 精确匹配的确定性替换)

**Gate 决策** (定义于 `css/evaluation/gate.py`):

Gate 是纯函数,无 I/O、无副作用:

| 条件 | 动作 |
|------|------|
| `cand_score > current_score` 且 `cand_score > best_score` | `accept_new_best` |
| `cand_score > current_score` 且 `cand_score <= best_score` | `accept` |
| `cand_score <= current_score` | `reject` |

Accept 时 `node.rules` 更新为 `candidate_rules`; reject 时保持不变。两种情况下 best_score/best_rules 均保持或更新但不回退。

### 3.6 断点恢复 (Checkpoint/Resume)

`run_l0_step` 中每个计算密集的中间产物都有独立的检查点文件:

| 阶段 | 检查点文件 | 路径模式 |
|------|-----------|---------|
| Reflect | `raw_patches.json` | `step{N}/raw_patches.json` |
| Merger | `merged_edits.json` | `step{N}/merged_edits.json` |
| Per-edit verification | `edit_verifications.json` | `step{N}/edit_verifications.json` |
| Gate decision | `gate_decision.json` | `step{N}/gate_decision.json` |
| Candidate rules | `candidate_rules.md` | `step{N}/candidate_rules.md` |
| Step summary | `step_summary.json` | `step{N}/step_summary.json` |

恢复逻辑: 函数入口检查对应文件是否存在——若存在则跳过该阶段直接反序列化。检查点写入使用原子操作 (写入 `.tmp` 文件后 `os.replace`)。

## 4 二值可解性判定准则

**模块**: `css/optimizer/exploitation.py:_evaluate_edit_criterion`

每条编辑的验证使用两层决策逻辑,确保对**任务可解性变化**的敏感性优先于连续分数变化:

```
┌─────────────────────────────────────────────────────────────────┐
│  逐任务状态分类 (基于 Pass@K)                                     │
│                                                                   │
│  对每个目标任务:                                                   │
│    incumbent 可解 = (incumbent pass_rate > 0)                      │
│    candidate 可解 = (candidate pass_rate > 0)                      │
│                                                                   │
│    GAINED(新增可解)    : incumbent 不可解 且 candidate 可解         │
│    LOST(丧失可解)      : incumbent 可解   且 candidate 不可解      │
│    RETAINED(保持可解)  : incumbent 可解   且 candidate 可解        │
│    STILL_UNSOLVED(仍不可解): 两者均不可解                           │
└─────────────────────────────┬───────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│  第一层: 二值可解性判定 (主判据)                                   │
│                                                                   │
│  n_gained = GAINED 任务数                                          │
│  n_lost   = LOST 任务数                                            │
│                                                                   │
│  若 n_gained > 0 或 n_lost > 0:                                    │
│    n_lost > 0 且 n_gained == 0       → 拒绝                       │
│    n_gained > 0 且 n_lost == 0       → 接受                       │
│    n_gained > n_lost                 → 接受                       │
│    n_gained <= n_lost                → 拒绝                       │
└─────────────────────────────┬───────────────────────────────────┘
                              ↓ (only if n_gained == 0 AND n_lost == 0)
┌─────────────────────────────────────────────────────────────────┐
│  第二层: 连续 Delta 值 (平局裁定)                                  │
│                                                                   │
│  continuous_delta = Σ(候选 pass_rate - incumbent pass_rate)       │
│                     对所有目标任务求和                              │
│                                                                   │
│  continuous_delta > 0  → 接受                                     │
│  continuous_delta <= 0 → 拒绝                                     │
└─────────────────────────────────────────────────────────────────┘
```

**设计原理**: 二值可解性 (是否在 K 次 rollout 中至少一次通过) 是比连续 pass rate 更稳定的信号。在任务级别,一个从不可解变为可解的"GAINED"是明确的进步信号,而 pass rate 从 0.33 到 0.67 的变化可能仅是噪声。

## 5 L1 Proposal 循环

**模块**: `css/proposal/proposal.py:run_l1_cycle`

当 L0 在某节点饱和后,L1 循环启动以搜索新的认知策略。这是一个多轮 diverse-iterate 搜索,每轮产生一个不同哲学的策略候选,客观评估后选择最优。

```
┌─────────────────────────────────────────────────────────────────────┐
│  L1 CYCLE (run_l1_cycle)                                             │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────┐     │
│  │ 初始化 (每个循环执行一次)                                     │     │
│  │  _select_l1_test_set:                                        │     │
│  │    残留任务: 至多 l1_diagnostic_tasks (24) 个                  │     │
│  │      基线 persistent_fail (0/K 通过) 的任务                   │     │
│  │    回归任务: 至多 l1_regression_tasks (12) 个                  │     │
│  │      基线 K/K 全通过的任务                                    │     │
│  │    baseline_map: task_id → TaskRolloutGroup (轨迹+结果)       │     │
│  └─────────────────────────────────────────────────────────────┘     │
│                             ↓                                         │
│  ┌─────────────────────────────────────────────────────────────┐     │
│  │ STEP 1: GROUNDING (一次性,可缓存)                             │     │
│  │  1a: L0 天花板分析 (为何规则优化停滞)                          │     │
│  │  1b: 深层失败轨迹分析 (5 个代表性失败)                         │     │
│  │  1c: L0 对比局限性审视                                        │     │
│  │       (通过 ThreadPoolExecutor 并行执行)                       │     │
│  │  1d: 综合 1a+1b+1c + 循环账本 → 搜索方向                      │     │
│  └──────────────────────────┬──────────────────────────────────┘     │
│                             ↓                                         │
│  ┌─────────────────────────────────────────────────────────────┐     │
│  │ 迭代循环 (至多 max_l1_iterations=8 轮)                         │     │
│  │                                                               │     │
│  │  Step 2: 策略提案                                              │     │
│  │    mode=NEW: 提出全新的认知机制                                 │     │
│  │    mode=REFINE: 相同思路,改进操作化方式                        │     │
│  │    输入: grounding + 循环账本 (所有历史轮次)                    │     │
│  │    输出: {philosophy, mechanism_difference, strategy_text}     │     │
│  │                                                               │     │
│  │  Step 3: 候选测试                                              │     │
│  │    strategy_text + 空 rules × K 次 rollout (固定测试集)        │     │
│  │    → 候选 TaskRolloutGroup[]                                   │     │
│  │                                                               │     │
│  │  客观分类 (无 LLM 参与):                                       │     │
│  │    cracked:      基线失败, 候选通过  → +lift(突破)             │     │
│  │    still_failed: 基线失败, 候选仍失败                          │     │
│  │    regressed:    基线通过, 候选失败  → +regression(回归)       │     │
│  │    maintained:   基线通过, 候选通过  (保持)                    │     │
│  │                                                               │     │
│  │  Step 4: 对比诊断                                              │     │
│  │    第一层: 逐任务分析器 (并行)                                  │     │
│  │      cracked     → 识别有效成分 (active ingredient)            │     │
│  │      regressed   → 区分 handicap(可恢复) vs harm(真损害)      │     │
│  │      still_failed → 残留问题本质描述                            │     │
│  │    第二层: 聚合综合 → 诊断报告                                  │     │
│  │      {有效成分, 真损害, 残留性质,                               │     │
│  │       下一步方向提示, next_action}                              │     │
│  │                                                               │     │
│  │  保留最优: effective (lift>0 且 net_lift>=0)?                   │     │
│  │    是 → bank 候选, 与当前最优比较                               │     │
│  │    否 → 归档 或 refine (取决于 next_action)                    │     │
│  │                                                               │     │
│  │  下一轮模式决策:                                                │     │
│  │    有效 或 已 refine 过 → NEW (探索不同方向)                    │     │
│  │    无效, 诊断=refine_current → REFINE                          │     │
│  │    无效, 诊断=propose_new → NEW                                │     │
│  └──────────────────────────┬──────────────────────────────────┘     │
│                             ↓                                         │
│  退出条件:                                                            │
│    - 已收集 l1_target_effective=3 个有效策略 → 选择最优                │
│    - 用完 max_l1_iterations=8 轮 → 选最优 (若无则归档)                │
│                                                                       │
│  输出:                                                                │
│    成功 → 新 TreeNode (最优策略, 空 rules)                             │
│    失败 → NegativeArchiveEntry (失败方向归档保存)                      │
└─────────────────────────────────────────────────────────────────────┘
```

### 5.1 固定测试集选择

L1 循环在开始时一次性选定测试集,此后所有轮次在完全相同的任务上评估,保证 lift/regression 的可比性:

- **Residual tasks** (至多 `l1_diagnostic_tasks=24`): 基线 (父节点 full-skill rollout) 上 persistent_fail (0/K) 的任务——L1 唯一可能"crack"的任务
- **Regression tasks** (至多 `l1_regression_tasks=12`): 基线上 K/K 全通过的任务——回归检测哨兵

### 5.2 空 Rules 测试

候选策略以 **空 rules** 进行测试。这一设计保证了:
- **Lift 是保守下界**: 在无战术规则支撑下仍能 crack 的 residual,在 L0 恢复 rules 后几乎必然继续 crack
- **Regression 是保守上界**: 因缺少 rules 而"退化"的任务 (handicap),在 L0 恢复 rules 后几乎必然恢复
- **纯认知信号**: 消除了战术规则的干扰,衡量的是认知策略本身的价值

### 5.3 客观分类

分类完全基于 Pass@K 的确定性比较 (`_categorize`),无 LLM 参与:

| 分类 | 条件 | 语义 |
|------|------|------|
| `cracked` | baseline 0/K → candidate ≥1/K | 策略解锁了新任务 |
| `still_failed` | baseline 0/K → candidate 0/K | 任务仍未解决 |
| `regressed` | baseline ≥1/K → candidate 0/K | 策略破坏了已解决的任务 |
| `maintained` | baseline ≥1/K → candidate ≥1/K | 已解决的任务保持正常 |

### 5.4 多样化迭代与退出

- **Cross-round ledger**: `_IterationContext` 记录每轮的哲学、客观结果、诊断,渲染为 `render_ledger()` 注入下一轮的 Step 2 prompt
- **Mode 决策**: effective → NEW (bank 并探索不同方向); ineffective + core sound → REFINE (一次机会); ineffective + dead end → NEW
- **Keep-best ranking**: 在 effective (lift>0 且 net_lift≥0) 的候选中,按 `(net_lift, deploy_net, lift, -regression)` 排序选择最优
- **deploy_net** = `lift - harm_reg`: 排除 handicap 退化 (L0 可恢复的) 后的"部署净收益"下界

## 6 树管理

### 6.1 UCB1 节点选择

**模块**: `css/tree/select.py`

每轮 SELECT 阶段使用 UCB1 变体公式对活跃节点排序:

```
UCB1(node_i) = val_score_i + α · accept_slope(W)_i + β · √(ln(T) / n_i)

其中:
  val_score_i      = 节点 i 最近的 val set task_hard 分数 (exploitation)
  accept_slope(W)_i = 最近 W 步 accept indicator 的线性回归斜率 (学习趋势)
  T                = 全局总 L0 步数 (所有节点之和)
  n_i              = 节点 i 的 L0 步数
  α = 0.5          = accept_slope 权重
  β = 0.5          = 探索权重
```

- **未访问节点** (`n_i = 0`) 的 UCB1 分数为 `+∞`,强制优先探索
- `select_batch` 返回 UCB1 分数最高的 top-K 节点 (K = `concurrency_limit`)

### 6.2 分支

**模块**: `css/tree/branching.py:decide_branch`

分支决策是纯确定性的:
- `node.is_saturated(cfg.N) == False` → `"EXPLOITATION"`: 继续 L0 内层优化
- `node.is_saturated(cfg.N) == True` → `"PROPOSAL"`: 触发 L1 策略搜索

L1 cycle 成功时在树中创建新子节点 (PROPOSAL 操作), 子节点继承父节点的 strategy,但使用空 rules (L0 从零开始重建战术)。

### 6.3 剪枝

**模块**: `css/tree/prune.py`

剪枝基于配对 bootstrap 兄弟支配检验,必须同时满足三个条件:

1. **充分投资**: `n_steps >= min_steps` (默认 10)
2. **已饱和**: `node.is_saturated(N) == True`
3. **统计显著劣势**: 配对 bootstrap 检验的 CI 下界 > 0

**配对 bootstrap 检验** (`paired_bootstrap_diff_ci`):
- 在共享的 val set 上,每个 task 产生二值 pass/fail indicator
- 重采样 `prune_bootstrap_resamples=1000` 次 (每次随机抽取 n 个 task 索引并同时应用于 node 和 sibling)
- 计算 `mean(sibling) - mean(node)` 的分布
- 取 `prune_ci=0.95` 对应的置信区间
- **CI 下界 > 0** → sibling 统计显著优于 node → 剪枝

## 7 基础设施

### 7.1 真实答案防火墙 (Ground-Truth Firewall)

**模块**: `css/model/client.py:OptimizerOnlyClient`

系统的核心不变式:**优化器可以看到 ground truth 以理解任务,但其产出永远不能依赖 ground truth**。

该不变式通过 `OptimizerOnlyClient` 这一单一、不可绕过的强制点实现:

```
┌────────────────────┐     ┌─────────────────────┐
│  TargetOnlyClient  │     │ OptimizerOnlyClient  │
│  complete_target() │     │ complete_optimizer() │
│  ↑ 调用 optimizer  │     │ ↑ 自动注入 FIREWALL   │
│    时抛出异常       │     │   到 system prompt   │
│                    │     │ ↑ 调用 target        │
│                    │     │   时抛出异常          │
└────────┬───────────┘     └────────┬─────────────┘
         │                          │
         └──────────┬───────────────┘
                    ↓
         ┌──────────────────┐
         │  内层 LLMClient   │
         │  (共享后端)        │
         └──────────────────┘
```

- 所有优化器 LLM 调用 (reflect, merger, proposal 等) 都经由 `OptimizerOnlyClient`,其 `complete_optimizer` / `complete_tool_call` 方法自动在 system prompt 中注入 ground-truth firewall 声明
- `OptimizerOnlyClient.complete_target()` 抛出 `RuntimeError`,结构性地阻止优化器调用目标 agent
- 反之,`TargetOnlyClient.complete_optimizer()` 同样抛出异常

### 7.2 Step Buffer

**模块**: `css/data/step_buffer.py`

StepBuffer 是每个节点的 L0 优化历史记录,服务三个角色:

1. **饱和检测**: `consecutive_rejects() >= N` → L0 饱和
2. **SELECT 信号**: `accept_slope(W)` 计算最近 W 步的接受率趋势 (线性回归斜率)
3. **优化器上下文**: `recent_failure_patterns(W)` 和 `recent_rejected_edits(W)` 注入下一步的 prompt,避免重复无效提议

**StepBufferEntry** 记录每步的:
- `action`: `accept_new_best` / `accept` / `reject` / `reject_no_survivor` / `epoch_reset`
- `score_before` / `score_after`: gate 前后分数
- `edit_verifications`: 完整的 per-edit ablation 验证结果 (V2)
- `failure_patterns`: LLM 识别的失败模式摘要

`reset_saturation()` 在新 epoch/round 开始时插入合成的 `epoch_reset` 条目,打破连续 reject 计数以防止跨 epoch 饱和死锁。

### 7.3 数据划分

系统维护 train/val/test 三组数据划分:

| 划分 | 默认大小 | 用途 |
|------|---------|------|
| train | `n_train=80` | L0 on-policy rollout + reflect 分析 |
| val | `n_val=40` | 选择集评分 (gate 决策, UCB1 val_score) |
| test | `n_test=20` | 终态报告 (不参与优化决策) |

`held_out_rotation_T=0` 表示不轮换;设为正整数时每 T 个 epoch 重新划分以减少过拟合风险。

### 7.4 评分指标

**模块**: `css/rollout/selection_eval.py`

候选评分使用 `task_hard` 指标:
- 每个 task 执行 K 次 rollout
- 若该 task 多数 (>50%) rollout 通过 → task 判为"passed"
- `task_hard` = passed tasks / total tasks
- 此指标对每个 task 权重相等,不因某些 task 的 K 次 rollout 中噪声较大而被过度加权

## 8 配置参数表

**模块**: `css/config.py:CSSConfig`

### 8.1 饱和与分支

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `N` | 5 | 连续 reject 饱和阈值 |
| `W` | 10 | 趋势窗口 (accept_slope / 历史注入) |
| `K` | 3 | (已废弃) REFINE→PROPOSAL 升级计数 |
| `remedy_threshold` | 3 | (已废弃) L1 信号所需的 remedy_resistance |

### 8.2 树管理: SELECT / PRUNE

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `min_steps` | 10 | PRUNE 最低投资步数 |
| `alpha` | 0.5 | UCB1 accept_slope 权重 |
| `beta` | 0.5 | UCB1 探索权重 |
| `prune_bootstrap_resamples` | 1000 | 配对 bootstrap 重采样次数 |
| `prune_ci` | 0.95 | bootstrap 置信水平 |

### 8.3 数据使用

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `k_rollouts` | 3 | 每个任务的 rollout 次数 (K=3) |
| `n_train` / `n_val` / `n_test` | 80 / 40 / 20 | 数据划分大小 |
| `held_out_rotation_T` | 0 | 数据划分轮换周期 (0=不轮换) |

### 8.4 L0 Exploitation

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `minibatch_size` | 8 | Reflect 每个 minibatch 的轨迹数 |
| `batch_size` | 40 | 每个 batch-step 的任务数 |
| `max_l0_steps_per_epoch` | 20 | epoch 内 L0 步数硬上限 |
| `l0_edit_budget` | 3 | 每个 minibatch proposer 的最大编辑数 (L) |
| `max_edits_per_step` | 6 | merger 后每步最大编辑数 |
| `exploitation_val_k` | 1 | val gate 的 K (1=快速; train K 保持 k_rollouts) |
| `merger_inject_history` | True | 是否向 merger 注入 per-edit verification 历史 |
| `merger_history_window` | 3 | merger 历史注入窗口 (最近 N 步) |
| `rules_max_chars` | 60000 | rules.md 软上限 (仅警告, 不截断) |
| `reflect_mode` | `"plan_a"` | Reflect 模式: `"legacy"` / `"plan_a"` / `"plan_b"` |

### 8.5 L1 策略循环

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `max_l1_iterations` | 8 | L1 循环最大轮次 |
| `l1_target_effective` | 3 | 收集足够多 effective 策略后停止 |
| `l1_diagnostic_tasks` | 24 | residual 任务数上限 |
| `l1_regression_tasks` | 12 | regression guard 任务数上限 |
| `l1_diagnosis_per_category` | 5 | 每类任务的 per-task 诊断数上限 |

### 8.6 上下文与截断

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `context_cap` | 256000 | 全局上下文窗口 (tokens) |
| `context_use_frac` | 0.80 | 有效阈值 = cap × frac |
| `tool_trunc` | 8000 | 单个工具结果截断阈值 |

### 8.7 运行时

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `seed` | 42 | 全局随机种子 |
| `concurrency_limit` | 4 | 每轮并行节点数 |
| `max_api_workers` | 32 | 并行 rollout 工作线程数 |
| `task_timeout_s` | 600 | 每个 rollout 的超时 (秒) |
| `bash_timeout_s` | 180 | 单条 bash 命令超时 (秒) |
| `max_turns` | 30 | 每次 rollout 的最大对话轮数 |
| `target_model` | `claude-sonnet-4-6` | 冻结任务 agent 使用的模型 |
| `optimizer_model` | `claude-sonnet-4-6` | 优化器使用的模型 |
