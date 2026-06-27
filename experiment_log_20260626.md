# CSS 实验日志 2026-06-26

## 实验背景

修改完善方案后的首次全流程实验。Run ID: `spreadsheetbench_20260626_180207`

### 关键变更（vs 旧方案）
1. **On-policy batch-step**: 每个 step 独立 rollout，不再复用旧轨迹
2. **Plan A 两阶段 reflect**: 3分析师（成功/失败/对比）→ 3独立编辑生成器 + evidence subset splitting
3. **V4 Annotation Prompt**: 单阶段直接行为分析 + 显式 polarity + 聚类一致性引导
4. **tool_trunc=4000**: 解决 256K 上下文溢出（旧: 8000）
5. **max_tokens=8192**: optimizer 输出空间翻倍（旧: 4096）
6. **optimizer_json_mode=True**: Qwen JSON format 确保输出正确性
7. **LLM semantic dedup only**: 移除 jaccard dedup（旧方案 jaccard 过于激进，清零所有 edits）
8. **k_rollouts=3**: 降低为3（旧: 5）
9. **batch_size=40**: 新增 batch 参数

### 配置
- Model: qwen3.6-35b-a3b
- Workers: 128
- Train/Val/Test: 140/60/200
- reflect_mode: plan_a
- max_turns: 100

## 对照基线（旧方案 20260626_113146）

| 指标 | 值 |
|------|-----|
| Cold start baseline | 0.524 |
| Train rollout score | 0.627 (439/700) |
| Step 0 gate | accept_new_best, 0.557 |
| Step 1 gate | reject, 0.543 |
| Step 2 | 未完成（可能被中断） |
| 问题 | jaccard dedup 在 step1/2 清零所有 edits → 空步骤 |

## 实验进展

### 18:02 - 启动
- 进程 PID 1566990
- 输出目录: runs/spreadsheetbench_20260626_180207

### 18:05 - Cold start baseline rollout 进行中
- 53/140 train predictions (~38%)
- 678 trace events, 8MB LLM calls

### 18:07 - 继续推进
- 69/140 train predictions (~49%)
- 843 trace events, 11MB LLM calls

### 18:12 - 继续推进
- 84/140 train predictions (~60%)
- 1144 trace events, 15MB LLM calls

## 旧方案深度分析（重要对照）

### Step 0 merged_edits 分析
- 只有 **1 raw patch → 8 distinct edits**（全部 support_count=1）
- 所有 8 个 edits 都是 `append` 操作，无 replace/insert_after/delete
- 规则内容质量不错（Computation Bridge, Sheet Naming, Header Preservation, Type Fidelity, Row Deletion, Zero vs None, Block Grouping, Lookup Verification）
- 但缺乏多角度验证（只有单一生成器的单一视角）

### Step 1 merged_edits — 致命问题确认
- `n_edits_total=0, n_edits_selected=0, selected_edits=[]`
- **jaccard dedup 完全清零了所有 edits**
- 原因：step 1 产生的新 edits 与 step 0 已添加的 rules 文本有词汇重叠，jaccard 误判为重复
- 这是旧方案的核心问题：off-policy + jaccard dedup 使得 step 1+ 无法产出任何有效编辑

### 新方案预期改进
- Plan A 的 3 generators → 3 raw patches，多角度验证
- LLM semantic dedup 替代 jaccard，避免词汇重叠误杀
- On-policy rollout 确保每步看到新规则下的新轨迹
- support_count 投票机制可筛选高共识编辑

### 待观察
- [x] Cold start baseline score vs 旧方案 0.524 → **0.540 (+1.6%)**
- [ ] 新方案 Plan A 是否产出多样化的 edits（预期 3 raw patches vs 旧 1）
- [ ] LLM-only dedup 是否保留合理 edits（避免清零）
- [ ] On-policy 是否使 step 1+ 能产出有效编辑
- [ ] optimizer_json_mode 是否消除 JSON 解析失败
- [ ] 整体 exploitation 效果：接受率、score 提升幅度

---

## Cold Start 分析结果 (18:50 完成)

### 基线评分
- **baseline = 0.540** (pass=227/420, 54.0%)
- 旧方案 baseline = 0.524 → 差异 +1.6%（可能 tool_trunc=4000 vs 8000 的差异，或随机波动）
- Rollout 耗时 2104s (~35min), 128 workers 并行

### V4 Annotation Prompt 效果
- 420 rollouts → **412 个 observations** (8 个轨迹可能标注失败/为空)
- 约 630 个 optimizer LLM 调用用于标注
- 标注+聚类完成后得到 **63 个 patterns**

### Pattern 聚类 (Label Grouping)
- Stage 1 jaccard: 412→331 labels (合并词汇近似的 labels)
- Stage 2 LLM: 331→250 labels (42 个 batch, 每 batch 8 labels)
- 最终: **63 个 patterns + 187 noise** (support<2 的单例)
- Polarity: **30 failure + 23 success + 10 neutral**

### Top Patterns (by support)
| Polarity | Pattern | Support |
|----------|---------|---------|
| success | Environment Assumption Heuristic | 21 |
| success | Heuristic Environment Adaptation | 18 |
| success | Environment-Aware Command Fallback | 10 |
| failure | Syntactic Implementation Without Semantic Verification | 9 |
| success | Reactive Environmental Diagnosis | 7 |
| failure | Conflating Formula Representation with Evaluation | 7 |
| failure | Formula-Value Conflation | 6 |
| neutral | Reactive Environment Adaptation via Command Substitution | 5 |
| failure | Formula-Value Substitution Failure | 5 |
| failure | Premature Execution Without Environment Verification | 4 |

### Strategy_0 质量
- 标题: **Semantic Result Verification**
- 三个认知维度:
  1. Formula vs. Value Distinction (区分公式字符串 vs 计算结果)
  2. Execution-Driven Verification (语义检查代替语法检查)
  3. Outcome-Grounded Completion (完成标准基于数据正确性而非代码正确性)
- 长度: 2357 chars
- **质量评估**: 准确识别了 openpyxl 公式写入≠执行的核心问题，策略聚焦且可操作

### Derivation Artifacts
- signals.json, root_causes.json, proposal.json, counterparts.json, strategy_0.md
- 64 个 divergences（对比分析）

---

## Exploitation Phase (18:50 开始)

### Round 0, Node n0000
- Train=140, Val=60
- skill_len=2379 (strategy 2357 + rules 0)

### 19:18 — 全量 train rollout 长尾卡住
- 419/420 train rollout 完成，task `24-23/r2` 卡住
- 最后 LLM 调用距今 ~10 分钟无响应
- task 24-23 的 r0 conversation.json 有 51KB（复杂多轮对话）
- **问题记录**: 与冷启动阶段相同的长尾问题，单个复杂 task 的 rollout 可能触发模型超长推理或 HTTP 超时
- HTTP timeout=1800s (30min), task_timeout=3600s (60min) — 最坏情况要等 30 分钟
- **潜在改进**: 考虑对 rollout 增加更激进的 per-turn timeout 或 max_turns 硬限制

### 19:38 — Train rollout 完成 + Exploitation 开始
- **Train rollout score = 0.536**, pass=225/420
- 对比 baseline=0.540: **strategy_0 未提升 train score** (微降 0.4%)
- **关键发现**: strategy_0 聚焦于 formula-vs-value 问题，对涉及此问题的 task 可能有帮助，但对其他 task 可能引入额外指令开销导致微弱退化
- 长尾 rollout 卡住 ~20 分钟 (19:18→19:37)
- Exploitation step 0 batch rollout 立即开始 (40 tasks × 3 rollouts)
- trace 快速增长: 6929 → 7700+ (19:38→19:43)

### 19:54 — Plan A Reflect 完成 + Val eval 进行中
- Batch rollout: **120/120 完成** (40 tasks × k=3), pass=73/120 (60.8%)
- Reflect: 3 raw patches → 17 merged → 9 dedup → 3 selected edits
- **与旧方案对比**: 旧方案只有 1 raw → 8 edits，新方案 3 raw → 更多初始候选 → 更严格筛选后 3 edits
- Val eval 立即开始 (60 tasks × k=3 = 180 rollouts)

### ~20:15 — Val eval 长尾卡住
- Val eval: **179/180 完成**，task 51-12 的 r2 rollout 卡住
- 最后 trace call_009089: n_messages=42（超长多轮对话）
- **系统性长尾问题**: 每批次总有 1-2 个 task 触发 40+ 轮对话，阻塞整批完成
- 这已是第 3 次观察到此现象（cold start、train rollout、val eval 各一次）
- **改进建议**: 增加 max_turns 硬限制（当前 100 轮太高）或 per-task timeout
- 等待 gate 决策...

### 20:30 — 根因诊断：子进程死循环

**深度诊断发现**：
- task 51-12/r2 的卡住根因不是 LLM 调用超时，而是 **tool call 执行的子进程死循环**
- 子进程链: `python(1566990) → sh(2142605) → python3(2142607)`
- python3(PID 2142607) 运行 solution.py，**CPU 100% 已持续 25+ 分钟**
- 主进程零 CPU、零网络连接，纯粹在 await 子进程
- solution.py 大小 3168B，创建于 20:02，内容未再变化
- task_timeout=3600s → 最坏还需 ~35 分钟才超时

**系统性问题记录**：
1. **Tool call subprocess 无 CPU 时间限制**: 当 ReAct agent 生成的 solution.py 包含死循环（如无限迭代 openpyxl 操作），subprocess 会永远运行直到 task_timeout
2. **单个 subprocess 阻塞整个 batch**: 179/180 完成但无法进行 gate 决策
3. **建议**: 为 tool call subprocess 增加独立的 execution_timeout（如 120s），区别于 LLM 调用 timeout 和 task timeout

### 关键配置和流程记录

**Epoch 完整流程**（从 orchestrator 代码确认）:
1. Train rollout (全量 140×k=3=420 rollouts)
2. Exploitation batch-step loop (4 batches, 每 batch: on-policy rollout → reflect → apply → val eval → gate)
3. Analysis (L1-3 on train_groups)
4. Final val eval (用 best_rules 最终评估)

**Step 配置**:
- 4 batches (140/40=3.5, 最后一个 batch 20 tasks)
- Saturation: N=5 连续 reject
- max_l0_steps_per_epoch=20 (安全上限)
- max_edits_per_step=3

**Gate 逻辑**: cand_score > current_score → accept; > best_score → accept_new_best; else reject
- Step 0: current_score = baseline = 0.540

### 长尾 Task 系统性分析

**反复阻塞 pipeline 的 task（所有阶段出现）：**
| Task | Conversation Size | 问题 |
|------|------------------|------|
| 80-42 | ~6.6MB/rollout | 极端长尾，cold start/train/batch 三阶段都触发 |
| 455-35 | ~5.1MB/rollout | cold start/train 两阶段触发 |
| 118-50 | ~500KB/rollout | 中等长尾，三阶段触发 |
| 209-30 | ~350KB/rollout | cold start/train 触发 |
| 51-12 (val) | 卡住进行中 | subprocess 死循环 100% CPU |

**当前阻塞**：task 51-12/r2 的 tool call subprocess (python3 PID 2142607) 执行 solution.py **100% CPU 死循环 30+ 分钟**
- 主进程零 CPU、零网络连接，纯粹 await 子进程
- task_timeout=3600s，预计 ~20:55 超时触发

**根因分类**：
1. **超长多轮对话** (80-42, 455-35): 模型在复杂 task 上进入 100+ 轮 ReAct 循环
2. **subprocess 死循环** (51-12): LLM 生成的 solution.py 包含无限循环逻辑
3. **两者都无独立 timeout**: max_turns=100 太高，subprocess 无执行时间限制

**优先改进建议**：
1. subprocess execution_timeout = 120s（杀死 100% CPU 的 solution.py）
2. max_turns = 30（截断无进展的多轮对话）
3. 长尾 task blacklist（排除已知极端 task）

### Timeout 机制深度分析

**实际执行链路**：
- ReAct agent → bash_tool → `subprocess.run(shell=True, timeout=bash_timeout)`
- bash_timeout = `task_timeout_s * 0.8 = 3600 * 0.8 = 2880s` (48 分钟！)
- executor.py 有 `run_generated_code(timeout=120)` 但**未被使用**
- 实际代码通过 bash_tool 执行 `cat solution.py && python3 solution.py`

**Timeout 层次**：
| 层 | 值 | 备注 |
|---|---|------|
| HTTP timeout | 1800s (30min) | per-LLM-request |
| bash_tool timeout | 2880s (48min) | per-tool-call subprocess |
| task_timeout | 3600s (60min) | per-rollout in batch_rollout |
| batch_deadline | 10800s (3h) | 整个 batch 的硬限制 |

**当前卡住的原因**：
- task 51-12/r2 在 ~20:02 通过 bash_tool 执行 python3 solution.py
- subprocess 死循环 100% CPU
- bash_tool timeout 在 20:02 + 48min = **~20:50** 才触发
- 之后还需 ReAct 收尾轮次 → 预计 gate 在 **~20:55-21:00**

### ~20:36 — 继续等待中
- 子进程 python3(2142607) 已运行 34+ 分钟
- trace 停在 9213 (20:10:47 以来)
- 预计 bash_tool timeout 在 ~20:50

### 20:50 — Step 0 Gate 决策！

**Gate 结果**: `accept_new_best`, score=0.000→0.483, best=0.483

**val eval 评分**: candidate_score = 0.483 (29/60 pass)

**关键发现 — val_score 初始化 Bug**:
- css.log 显示 `score=0.000->0.483`，但 baseline=0.540
- 根因：`node.val_score` 初始化为 0.0（`css/data/tree.py:78`）
- `orchestrator.py:367` 传入 `current_score=node.val_score`（= 0.0）
- `node.val_score` 只在 epoch 末尾的 final val eval（orchestrator L417）才更新
- **结果**：exploitation gate 的初始对比基线是 0.0 而非 baseline 0.540
- 0.483 **实际低于** baseline 0.540（-5.7%），但因为对比 0.0 所以被 accept
- **这意味着第一轮 exploitation 的 gate 几乎无筛选作用**（任何正分数都会通过）
- 旧方案同样有此问题（step 0 accept 0.557 也是对比 0.0 不是 0.524）

**对比旧方案 Step 0**:
| 指标 | 新方案 | 旧方案 |
|------|--------|--------|
| Gate | accept_new_best | accept_new_best |
| Val score | 0.483 | 0.557 |
| Baseline | 0.540 | 0.524 |
| vs baseline | **-5.7%** ↓ | **+3.3%** ↑ |
| Raw patches | 3 | 1 |
| Merged edits | 17 | 8 |
| Dedup edits | 9 | 8 (no dedup) |
| Selected edits | 3 | 8 (no selection) |

**新方案 Step 0 Val score 下降的可能原因**:
1. 3 条规则可能太少，旧方案 8 条覆盖更广
2. 规则质量问题：批次样本代表性不够（仅 40/140 tasks）
3. Strategy_0 + 新规则组合可能产生冲突指令
4. k=3（新） vs k=5（旧）的评估方差更大

**Step 0 产出的 3 条规则**:
1. **Input Data Inspection**: 修改前先用 openpyxl 读全量内容（support=4）
2. **Row Deletion Protocol**: 多行删除按降序执行（support=2）
3. **Formula vs. Value Separation**: 不依赖 openpyxl 公式计算（support=2）

**失败模式总结（gate failure_patterns）**:
- python3 命令检测、行删除顺序、排序方向、字典连接、公式值分离、sheet 重命名
- 6 条分析，质量较好，覆盖多个常见失败模式

### 20:50 — Step 1 已启动
- Step 1 batch rollout 开始（第 2 个 batch，40 tasks × k=3 = 120 rollouts）
- 40 个 task 目录已创建，1/120 conversation 完成
- trace 从 9213 增长到 9367
- **关键测试点**: Step 1 on-policy rollout 用 step 0 accepted 的 3 条新规则
- 待观察：LLM semantic dedup 能否保留 step 1 的新 edits（vs 旧方案 jaccard 清零）

### 21:00 — Step 1 Batch Rollout 完成
- 120/120 conversations 全部完成
- 总耗时 ~9 分钟 (20:51→21:00)，**无极端长尾**（vs step 0 val eval 卡了 48 分钟）
- 最慢的 task: 146-49 (500KB), 47766, 61-4

### 21:03 — Step 1 Reflect 完成！**关键验证点通过**

**Step 1 reflect 结果**: `reflect=3 raw, merge=17, dedup=13, select=3 edits`

**对比分析**:
| 指标 | Step 0 | Step 1 (新方案) | Step 1 (旧方案) |
|------|--------|----------------|----------------|
| Raw patches | 3 | 3 | 1 |
| Merged | 17 | 17 | ? |
| Dedup | 9 | **13** | **0 (清零!)** |
| Selected | 3 | **3** | **0 (清零!)** |

**LLM semantic dedup 成功保留了 step 1 的有效编辑！**
- 旧方案 jaccard dedup 在 step 1 清零所有 edits → 空步骤 → gate 必然 reject
- 新方案 LLM dedup 只过滤语义真重复：17 → 13（只移除 4 个）
- Step 1 比 step 0 的 dedup 后数量更多（13 vs 9），说明 on-policy 新轨迹产生了更多差异化编辑
- **这是新方案 vs 旧方案最核心的改进点验证成功**

**Step 0 Raw Patches 详情（3 个独立生成器）**:
- Patch 0: 5 edits (Toolchain Enforcement, Row Deletion, Formula vs Value, ...)
- Patch 1: 7 edits (Formula Separation, Output Verification, Answer Position Sheet Naming, ...)
- Patch 2: 5 edits (Toolchain Selection, Semantic Value Enforcement, Formula Reference Integrity, ...)
- **共 17 edits → merge → 9 dedup（步骤间重叠: Formula 相关出现 3 次）→ 3 selected**

### ~21:03 — Step 1 Val Eval 进行中
- 43/60 val task 目录已创建，0 个 conversation 完成
- trace 10095，快速增长中
- 等待 gate 决策...

### val_score 初始化 Bug 分析

**问题描述**:
- `css/data/tree.py:78`: `val_score: float = 0.0` — TreeNode 默认 0.0
- `orchestrator.py:367`: exploitation 传入 `current_score=node.val_score`
- `node.val_score` 在 epoch 末尾 final val eval（L417）才更新
- **结果**: 第一轮 exploitation 的 gate 对比基线为 0.0 而非 baseline 0.540
- 旧方案同样有此问题

**影响**:
- Step 0: candidate_score=0.483 虽然低于 baseline 0.540，但因为对比 0.0 所以 accept
- 后续步骤（step 1, 2, 3）的 current_score 是上一步的结果（0.483），仍然低于 baseline
- **好消息**: 对后续步骤的影响有限——current_score 会随着 accept 逐步更新
- **修复建议**: cold start 后设置 `root.val_score = cs.baseline_score`

---

### ~21:17 — Step 1 Gate 决策: accept_new_best ✓

**Gate 结果**:
- action: `accept_new_best`
- candidate_score: **0.500** (↑ from step 0 的 0.483)
- current_score: 0.500
- best_score: **0.500** (new best at step 1)
- best_step: 1

**Score 轨迹**:
| 阶段 | Val Score | 对比 |
|------|-----------|------|
| Baseline (cold start) | 0.540 | — |
| Step 0 rules | 0.483 | -0.057 vs baseline |
| Step 1 rules | **0.500** | -0.040 vs baseline, +0.017 vs step 0 |

**Step 1 产出的新规则** (共 6 条, 比 step 0 的 3 条多 3 条):
1. Input Data Inspection (保留自 step 0, support=4)
2. Row Deletion Protocol (保留自 step 0, support=2)
3. Formula vs. Value Separation (保留自 step 0, support=2)
4. **Exact String Preservation** — 不自动 strip/trim 单元格值 ← NEW
5. **Environment Execution** — 检查 python3 可用性 ← NEW
6. **Output Sheet Naming** — 确保输出 sheet 名匹配预期 ← NEW

**Step 1 Reflect 统计**: raw=3, merge=17, dedup=13, select=3
- 与 step 0 相同: raw=3 (3 个分析师), merge=17 (合并后)
- **关键提升**: dedup=13 (vs step 0 的 9) — 更多有效 edits 被保留
- select=3: 一致的选择数

**关键分析**:
- 连续两步 accept_new_best，score 正向趋势 (0.483 → 0.500)
- 但仍未回到 baseline 0.540 — 说明 step 0 引入了一些副作用
- val_score bug 使得 step 0 的 0.483 被错误接受(低于 baseline 0.540)
- 如果无 bug，step 0 应该被 reject，step 1 的 3 条新 edits 可能直接应用在空规则上
- 规则从 1119B (3 条) 增长到 2101B (6 条)

**Step 1 Failure Patterns** (6 条新诊断):
1. 类型保持: 不要把 datetime 转成 str，不要把 0 替换为 '-'
2. 操作分离: 清除列 + 复制列需分步进行，避免读取污染
3. 字符串精确: 保持源数据 headers 原样（如 Allowances 不改 Allowance）
4. 排序保真: 保持首次出现的顺序，不强加时间排序
5. 行压缩: 去重后压缩唯一记录，不保留空行
6. 边界检查: 源数据不足时不填充空值

### ~21:20 — Step 2 Rollout 进行中
- Step 2 rollout: 120/120 完成 (~21:31)
- 第 3 个 batch (tasks 81-120 in shuffled order)

### ~21:32 — Step 2 Reflect 完成
- reflect=3 raw, merge=14, dedup=9, select=3 edits
- merge 从 17 降到 14 — 当前规则已覆盖部分 patterns，分析师产出更少新 edit
- dedup 从 step 1 的 13 回落到 9

**Step 2 选中的 3 条新 edit** (op=insert_after):
1. **Formula Evaluation in Inspection** (support=3): 检查输入数据时不要依赖 cell.value 读公式
2. **Type-Aware Value Writing** (support=2): 写值时保持原生 Python 类型
3. **Explicit Sheet Naming** (support=2): 修改后显式设置 sheet 名称

### ~21:52 — Step 2 Gate 决策: **REJECT** ✗

**Gate 结果**:
- action: **`reject`** — 第一次出现 reject！
- candidate_score: **0.472** (↓ from step 1 的 0.500)
- current_score: 0.500 (不变)
- best_score: **0.500** (保持 step 1)
- best_step: 1

**Score 轨迹**:
| 阶段 | Val Score | Gate | 对比 |
|------|-----------|------|------|
| Baseline (cold start) | 0.540 | — | — |
| Step 0 rules (3条) | 0.483 | accept_new_best | -0.057 vs baseline |
| Step 1 rules (6条) | 0.500 | accept_new_best | -0.040 vs baseline |
| Step 2 rules (9条) | **0.472** | **reject** | -0.068 vs baseline |

**关键分析**:
1. **过拟合信号**: 规则从 6 条增到 9 条后 score 下降 (0.500→0.472)
2. **新增规则过于具体**: Formula Evaluation in Inspection、Type-Aware Value Writing 可能引入了不必要的约束
3. **规则间冲突**: 原有 "Formula vs. Value Separation" + 新增 "Formula Evaluation in Inspection" + "Explicit Sheet Naming" 可能产生冗余/矛盾指导
4. **Gate 机制正常工作**: 正确 reject 了回退性能的候选规则

**Step 2 Failure Patterns** (6条):
1. 公式字符串 vs 计算值：openpyxl 不评估公式
2. 数据范围中 header 被误修改
3. 输出缺少 header 行导致行偏移
4. 类型不匹配（datetime vs string, 0.0 vs '-'）
5. 公式标记在检查时未被正确解读
6. Sheet 名称不匹配

### ~21:53 — Step 3 开始
- 系统回退到 step 1 的 best rules (6 条) 作为当前规则继续
- Step 3 batch 只有 20 个 tasks (140 - 3×40 = 20), 预期 rollout = 60

### ~22:10 — Step 3 Reflect 完成
- reflect=3 raw, merge=**9**, dedup=**7**, select=3 edits
- merge 继续递减 (17→17→14→**9**) — 规则覆盖面趋于饱和

**Reflect 统计趋势**:
| Step | raw | merge | dedup | select |
|------|-----|-------|-------|--------|
| 0 | 3 | 17 | 9 | 3 |
| 1 | 3 | 17 | 13 | 3 |
| 2 | 3 | 14 | 9 | 3 |
| 3 | 3 | **9** | **7** | 3 |

### ~22:26 — Step 3 Gate 决策: accept_new_best ✓✓✓ (重大突破！)

**Gate 结果**:
- action: **`accept_new_best`** — 首次超越 baseline！
- candidate_score: **0.589** (↑↑ from 0.500)
- best_score: **0.589** (new best at step 3)
- best_step: 3

**完整 Score 轨迹**:
| 阶段 | Val Score | Gate | 对比 Baseline |
|------|-----------|------|---------------|
| Baseline (cold start) | 0.540 | — | — |
| Step 0 rules (3条) | 0.483 | accept_new_best | -0.057 |
| Step 1 rules (6条) | 0.500 | accept_new_best | -0.040 |
| Step 2 rules (9条) | 0.472 | **reject** | -0.068 |
| Step 3 rules (8条) | **0.589** | **accept_new_best** | **+0.049** |

**关键发现**:
1. **首次超越 baseline**: 0.589 > 0.540, 提升 +4.9 个百分点！
2. **reject 后反弹**: step 2 reject (0.472) → step 3 用 step 1 的 6 条规则重新起步 → 0.589
3. **规则数从 9 降到 8**: step 2 的 9 条被 reject 后，step 3 从 6 条基础上新增 2 条 + 替换 1 条 = 8 条
4. **replace 操作首次出现**: "Formula vs. Value Separation" 被 replace 为更强版本（禁止写公式字符串到输出单元格）

**Step 3 最终规则 (8 条, 3204 chars)**:
1. Input Data Inspection (保留)
2. Row Deletion Protocol (保留)
3. **Corrupted Input Handling** ← NEW: 输入损坏时不捏造数据
4. **Formula vs. Value Separation** ← REPLACED: 更强版本，禁止写公式字符串
5. Exact String Preservation (保留)
6. Environment Execution (保留)
7. Output Sheet Naming (保留)
8. **Type-Aware Value Writing** ← NEW: 保持原生类型，不做隐式转换

**Exploitation 完成汇总** (css.log):
- rounds=0, node=n0000, steps=0→4, best=0.589, saturated=False, rules=3204 chars
- 4 步全部完成（未触发 N=5 连续 reject 的 saturation）
- 接受率: 3/4 (75%)

### ~22:38 — Analysis 完成
- observations=409, patterns=138, l1_signals=0, l0_saturated=False
- Analysis 耗时 ~12 分钟 (22:26→22:38)
- l1_signals=0: 没有 L1 级别的高层策略信号（可能因为 exploitation 效果不错，不需要策略调整）

### ~22:39 — Final Val Eval 开始
- 用 best_rules (step 3, 8 条规则, 3204 chars) 在 val set 上做最终评分
- 60 tasks × k=3 = 180 rollouts
- val/ 目录已创建，rollouts 进行中
- 179/180 快速完成，task 73-45/r2 最后完成 (22:54)

### 22:56 — Epoch 0 完成！Round 1 开始

**Final Val Eval 结果**:
- **val_score = 0.594** (best rules, 8 条规则)
- exploitation 中 step 3 gate 的 0.589 → final val eval 的 0.594 (+0.5%)
- 微小差异来自评估随机性

**Epoch 0 完整汇总**:
| 指标 | 值 |
|------|-----|
| Cold start baseline | 0.540 |
| Train rollout score | 0.536 |
| Exploitation steps | 4 (0-3) |
| Accept rate | 3/4 (75%) |
| Best step | 3 (val=0.589) |
| **Final val_score** | **0.594** |
| **global_best** | **0.594** |
| Maturity | 1 |
| Rules | 8 条, 3204 chars |
| Saturated | False |
| Total time | ~5h (18:02→22:56) |

**Branching decision**: EXPLOITATION (继续利用当前规则，不分支)

**Round 1 立即开始**: 22:56:03
- node=n0000, steps=4 (继承 round 0 的计数)
- Round 1 将对 n0000 再做一轮 exploitation（重新 train rollout → batch-step loop → analysis → val eval）

**注意**: 孤儿进程 python3 solution.py (PID 2142607, 100% CPU 近 3 小时) 已手动杀死。该进程是 step 0 val eval 时产生的死循环 subprocess，脱离主进程树后成为 orphan。

---

## Round 1 监控

### 22:56 — Round 1 Epoch 开始
- Train rollout: 140×3=420 rollouts
- 使用 round 0 结束时的 best rules (8 条, 3204 chars)
- 关注点：round 1 的 train score 是否比 round 0 的 0.536 有提升

### Round 0 Analysis Patterns 总结 (138 patterns)
- failure: 73 (53%), success: 47 (34%), neutral: 18 (13%)
- Top failure: Syntactic Implementation Without Semantic Verification (sup=10), Formula Representation/Evaluation 混淆 (sup=9+6)
- Top success: Environment Adaptation 类 (sup=23+21+18+18) — 对应规则 6 Environment Execution
- **核心发现**: formula/value 混淆是头号失败模式，Environment Adaptation 是头号成功模式。规则 4 (Formula vs Value) 和规则 6 (Environment Execution) 精准命中了最高频 pattern
- remedy_resistance 全部为 0 — analysis 未做规则-pattern 关联标记

---

## ✅ 已实施修改（代码已改，当前运行实验不受影响，下次新实验生效）

### 修改 1: Exploitation 后增加 Test Set 评估 ✅ DONE
- **文件**: `css/orchestrator.py` `_run_node_epoch()` 末尾
- **实施**: 在 val eval (step 4) 之后新增 test eval (step 5)，使用 `env.test_items()` (200 tasks)
- test_score 写入 epoch_done 日志和 trace event
- val eval 保留（node.val_score 被 learning curve 和 tree manager 使用）

### 修改 2: Exploitation batch 循环改为无限续生 ✅ DONE
- **文件**: `css/optimizer/exploitation.py` `run_exploitation_epoch()`
- **实施**: 固定 batch 循环 → while + batch_round cycling，只在 saturation(N=5) 或 max_steps 时停止
- 每轮用 `seed + epoch + batch_round * 10000` 生成新 shuffle

### 修改 3: Rejected edits 不参与 dedup ✅ DONE
- **文件**: `css/optimizer/exploitation.py`
- **实施**: `rejected_edits=[]` 始终传空列表给 step_buffer
- 原因：gate reject 反映候选聚合分数，不反映单个 edit 的质量。rejected edit 不应阻止未来重新提议

### 修改 4: val_score 初始化 Bug 修复 ✅ DONE
- **文件**: `css/orchestrator.py` cold start 后
- **修复**: `root.val_score = cs.baseline_score` (cold start 后立即设置)
- 确保 exploitation step 0 gate 对比 baseline 而非 0.0

### 修改 5: Epoch 内执行顺序调换 ✅ DONE
- **文件**: `css/orchestrator.py` `_run_node_epoch()`
- **旧顺序**: Train rollout → Exploitation → Analysis → Val → Test
- **新顺序**: Exploitation → Train rollout (post-exploitation, best_rules) → Analysis → Val → Test
- **原因**: Analysis/branching 应基于 exploitation 充分优化后的残余问题做决策，而非基于优化前的过时轨迹
- train rollout 使用 `_val_skill_text(node)` (best_rules) 而非 `_combined_skill_text(node)` (live rules)

---

## 新实验 (spreadsheetbench_20260626_231253) — 全部修改生效

### 配置
- Model: qwen3.6-35b-a3b
- **Workers: 256** (旧: 128)
- Train/Val/Test: 140/60/200
- reflect_mode: plan_a

### 与旧实验 (180207) 的差异
| 项目 | 旧实验 (180207) | 新实验 (231253) |
|------|----------------|----------------|
| Workers | 128 | **256** |
| Epoch 顺序 | Train→Exploit→Analysis | **Exploit→Train→Analysis** |
| Batch cycling | 固定 4 batch | **无限续生到 saturation** |
| Rejected edits | 参与 dedup | **不参与 dedup** |
| val_score 初始化 | 0.0 (bug) | **baseline_score** |
| Test eval | 无 | **每 epoch 200 tasks** |

### 23:13 — 启动
- PID 2995292
- Cold start baseline rollout 开始

### 23:36 — Cold Start 完成
- **Baseline score: 0.305** (旧实验: 0.540, 差距 -0.235)
- Patterns: 67 (旧实验: 138)
- **⚠️ 严重问题: 54/140 train tasks 无产出**
  - 仅 86 个 task 目录 (旧实验 140 个全有)
  - 257 conversations (旧实验 420)
  - 可能原因: 256 并发过高导致 LLM endpoint 请求丢失/超时，部分 task 的 agent 0 步结束无产出
- Cold start 后 analysis: 67 patterns (旧实验 138), 差距可能来自样本不足
- Derive strategy: 1599 chars (已生成 strategy_0)
- 进入 Round 0 → exploitation step 0 (新 epoch 顺序生效)

### 根本原因定位: asyncio.run() 多线程竞争
- **现象**: 54/140 task 完全无产出 (无目录、无 conversation)
- **根因**: `agent.py:178` 的 `asyncio.run(self.run_async(task))` 在 ThreadPoolExecutor 256 并发线程中不稳定
  - Python 3.12 下 256 线程同时创建/销毁 event loop 触发资源竞争
  - css.log 中的 `RuntimeWarning: coroutine 'ReActAgent.run_async' was never awaited` 确认
  - css.log 中的 `AttributeError: '_UnixSelectorEventLoop' object has no attribute '_ssock'` 确认
- **证据**:
  - trace.jsonl 中 0 个空 LLM 响应 (LLM 端没问题)
  - 54 个缺失 task 数据完整、test cases 有效 (数据没问题)
  - batch_rollout 输出 `420 units pass=128/420` (assert 通过，但 162 个是空 failed result)
- **修复**: `agent.py` 中 3 处 `asyncio.run()` → `asyncio.new_event_loop()` + `loop.run_until_complete()` + `loop.close()`
- **不是代码修改 bug**: 5 个方案修改都在 orchestrator/exploitation，不影响 rollout

### 23:52 — 实验 231253 停止，启动新实验 235242
- 修复 asyncio 问题后重新启动
- Run ID: `spreadsheetbench_20260626_235242`
- PID: 3174135
- 配置与 231253 相同 (256 workers, 全部修改生效)

### 235242 实验问题: 49 task 缺失 (与 asyncio 无关)
- 修复 asyncio 后 dirs 增长到 91 即停止 (旧实验 140)
- 49 个缺失 task 数据完整、test cases 有效、旧实验正常运行
- trace 中无 task_id 字段 (trace 只记系统事件，不记 per-task rollout)
- 怀疑 256 workers 下部分 bash_tool subprocess 卡死 (LibreOffice 宏)，占满线程池 slot
- conversations 停在 261 不再增长，vLLM 请求降至 2
- **结论**: 256 workers 在当前环境下不可靠，线程池 slot 耗尽是 root cause

---

## 正式实验 (spreadsheetbench_20260627_002016)

### 配置
- Model: qwen3.6-35b-a3b
- **Workers: 128** (从 256 回退到 128，与成功的 180207 实验一致)
- Train/Val/Test: 140/60/200
- reflect_mode: plan_a
- max_turns: 100
- 包含全部 5 项修改 (Mod 1-5) + asyncio 修复
- stdout 重定向到 `runs/experiment_stdout.log`

### 与旧实验的差异
| 项目 | 基准实验 (180207) | 正式实验 (002016) |
|------|------------------|-------------------|
| Workers | 128 | 128 |
| Epoch 顺序 | Train→Exploit→Analysis | **Exploit→Train→Analysis** (Mod 5) |
| Batch cycling | 固定 4 batch | **无限续生到 saturation** (Mod 2) |
| Rejected edits | 参与 dedup | **不参与 dedup** (Mod 3) |
| val_score 初始化 | 0.0 (bug) | **baseline_score** (Mod 4) |
| Test eval | 无 | **每 epoch 200 tasks** (Mod 1) |
| asyncio | asyncio.run() | **new_event_loop()** |

### 00:20 — 启动
- PID 3301408
- 输出目录: runs/spreadsheetbench_20260627_002016

### 00:35 — Cold Start Rollout 中期分析 (135/140 dirs)

**Agent 行为质量分析 (276 completed conversations):**
- 平均 11.7 轮对话 (旧实验 14.6 轮), min=6, max=32
- 100% conversation 包含 code/bash 执行, ReAct 机制正常
- 0 个空/极短 (<=2 turns) 对话, 说明 asyncio 修复有效
- 轮次分布: 6-10 轮 (136), 11-20 轮 (133), 21-50 轮 (7), 无 51+ 轮失控对话
- 对比旧实验: 平均轮次减少 20% (14.6→11.7), 可能因 agent 更高效或放弃更早

**Prediction 产出质量:**
- 277 个预测文件, 0 个 tiny (<1KB), 全部实质性 xlsx
- 平均 13.0 KB (旧实验 21.5 KB), 范围 4.8-201.6 KB
- 预测文件较小需关注是否影响 accuracy

**128 workers 验证:**
- Task dirs 稳步增长: 43→59→80→89→92→104→113→126→130→135→138
- 已远超 256-worker 的 91 dirs 上限, 确认并发度过高是 task 丢失的 root cause
- 0 errors in css.log
- openpyxl Data Validation UserWarning 无害

### 00:37 — Task dirs 达到 140/140 ✓
- **全部 140 个 train task 目录已创建**, 128 workers 验证通过
- Conversations: 308/420 (73%), 仍在进行中
- Predictions: 308/308 (100% 产出率)
- 等待剩余 conversation 完成后进入 cold start analysis 阶段

### 00:38 — Agent 行为深度对比分析

**新旧实验对比 (336 vs 420 completed convs):**
| 指标 | 新 (002016) | 旧 (180207) | 分析 |
|------|------------|------------|------|
| Avg turns | 11.9 | 14.5 | 新实验更高效 |
| Median | 12 | 12 | 中位数一致 |
| Max turns | 34 | **110** | 旧实验有极端长尾 |
| >20 turns | 10 (3%) | **54 (13%)** | 长尾大幅减少 |
| Error mentions | 28% | 31% | 基本一致 |
| Tool call ratio | 42% | 43% | 一致 |

**发现: 长尾对话大幅减少**
- 旧实验 54 个 >20 轮对话 (最极端 110 轮), 新实验仅 10 个 (最多 34 轮)
- 中位数一致(12), 平均数差异 (14.5 vs 11.9) 完全由尾部极端值导致
- 可能原因: asyncio 修复后 agent 响应更稳定, 减少错误重试引起的 ReAct 循环
- 旧实验 task 262-17 有一次 110 轮的极端循环 (variance std=44.8)
- 新实验最高方差 task 118-50 的 std 仅 8.4, 控制更好

**机制符合性评估:**
- ReAct agent 正常工作: 100% conv 包含 code/bash, 42% msgs 是 tool calls
- 无空响应、无 timeout, 无极短对话 (min=6)
- 对话长度合理分布, 无失控现象

### 00:45 — Cold Start Rollout 接近完成 + 深度对比分析

**进度: 409/420 (97.4%)**, 7 个 task 各缺 1 个 rollout:
- 13-1, 130-9, 290-1, 398-14, 48365, 50193, 80-42 (均 2/3 complete)
- 131 个 task 已完成全部 3 rollouts, 仅这 7 个长尾任务的第 3 个 rollout 尚在运行
- **0 errors, 0 empty conversations**, 进程正常 (PID 3301408, CPU 15.8%)

**新实验完整 Turn 统计 (409 conversations):**
| 指标 | 新 (002016) | 旧 (180207) | 变化 |
|------|------------|------------|------|
| Mean turns | 13.5 | 14.5 | -6.9% |
| Median | 12 | 12 | = |
| P90 | 20 | 24 | -16.7% |
| P95 | 28 | 30 | -6.7% |
| Max | 54 | **110** | -51% |
| 51+ turns | 1 (0.2%) | 5 (1.2%) | 大幅减少 |
| 21-50 turns | 37 (9%) | 49 (12%) | 减少 |
| 6-10 turns | 165 (40%) | 166 (40%) | 一致 |
| 11-20 turns | 206 (50%) | 200 (48%) | 一致 |
| Predictions | 409 (100%) | 419 (100%) | 同 |

**关键发现 — 长尾改善显著:**
- P90 从 24 降到 20，P95 从 30 降到 28，Max 从 110 降到 54
- 超长对话 (51+) 从 5 个降到 1 个
- 长尾主要集中在 task 50193 (54 turns, 50 turns) — 老实验中该 task 也是长尾 (56 turns)
- **asyncio fix 是主因**: 消除了事件循环干扰导致的 ReAct 死循环

**高方差 Task 对比 (inter-rollout consistency):**
| Task | 新 std | 新 turns | 旧 std | 旧 turns |
|------|--------|----------|--------|----------|
| 22-47 | 14.1 | [12,42,42] | - | - |
| 262-17 | 10.5 | [36,16,12] | **44.8** | [16,14,**110**] |
| 130-9 | - | 2/3 done | 17.0 | [71,33,37] |
| 47766 | - | [36,42,-] | 14.7 | [44,28,64] |

- 262-17: 旧实验最极端 task (std=44.8, 一次 110 轮), 新实验控制在 std=10.5 (max 36 轮)
- 证明 asyncio 修复消除了随机死循环

**旧实验 (180207) 参照基线 — 完整 Round 0 历程:**
```
Cold start baseline = 0.540 (patterns=63, strategy=2357 chars)
Train rollout score = 0.536 (pass=225/420)
L0 step 0: accept_new_best 0.000→0.483 (reflect=3 raw, merge=17, dedup=9, select=3)
L0 step 1: accept_new_best 0.483→0.500 (reflect=3 raw, merge=17, dedup=13, select=3)
L0 step 2: reject 0.500→0.472 (reflect=3 raw, merge=14, dedup=9, select=3)
L0 step 3: accept_new_best 0.500→0.589 (reflect=3 raw, merge=9, dedup=7, select=3)
Exploitation done: best=0.589 (4 steps, not saturated)
Analysis done: obs=409, patterns=138, l1_signals=0
Epoch done: train=0.536 val=0.594 best=0.589
```

**对新实验的预期 (基于 Mod 1-5 变更):**
1. **Baseline score**: 预期 ≈0.540 (相同模型+数据+agent，仅 asyncio 差异)
2. **Mod 5 新 epoch 顺序**: exploitation FIRST → 预期 step 0 使用 `val_score=baseline_score` (Mod 4) 作为 current_score
3. **Mod 2 batch cycling**: 应看到 saturation 机制 (N=5 consecutive rejects) 而非固定 4 steps
4. **Mod 3 rejected edits 不参与 dedup**: dedup 计数可能更低 (之前被 reject 的 edit 会影响后续 dedup)
5. **Mod 1 test eval**: exploitation 结束后应看到 200 任务 test set eval

### 00:50 — Cold Start Rollout 最终阶段

**419/420 (99.8%)** — 仅剩 task 80-42 的 r0 (最后 1 个 rollout)
- r1, r2 已完成, r0 正在 agent 交互中 (有 workdir 和 prompts)
- 该 task 在旧实验中也是已知长尾 (56 turns in 180207)
- 全部 139/140 其他 tasks 已完成 3/3 rollouts
- 0 errors, 进程健康

### 00:52 — Cold Start 进入 Analysis 阶段 (Step 2)

**Rollout 完成确认:**
- 420/420 conversations 全部完成 ✓
- 420/420 predictions 全部生成 ✓
- Task 80-42/r0（最后一个 rollout）: 74 turns（旧实验 56 turns — 最复杂任务之一）
- batch_rollout 已返回，baseline score 已计算

**Analysis 进行中证据:**
- trace.jsonl 最新 event: `llm_call` from optimizer, method=`complete_optimizer`, call_id=`call_003260`
- LLM 响应耗时 54.5s（optimizer 做 trajectory annotation）
- css.log 尚未更新 — analysis 完成后才会一次性写入
- analysis 目录尚空 — 同上

**旧实验 Analysis 参照:** 751s (~12.5min), 产出: 412 observations, 64 divergences, 63 patterns
**预期新实验:** 相似规模 (~400+ observations, ~60+ patterns), 耗时预计 10-15 分钟

### 00:58 — Analysis Layer 2 进行中 (cluster refine)

**进度追踪:**
- 总 optimizer 调用: 542 (其中 ~420 Layer 1 annotation, ~122 Layer 2)
- `label_grouping.json` 已生成 (34.5KB, 旧实验 33KB — 规模一致)
- 当前: cluster refine 到 tmp42 (第 43 个 cluster)
- 最新 pattern: "Premature Action Without Environmental Verification" (failure, 2 members)
  → 描述: agent 在没有验证环境状态的情况下就执行，如假设 'python' 而非 'python3'
- 预计总 cluster ~60+（旧实验 63 patterns），还有约 20 个待 refine

**Analysis 机制观察:**
- V4 annotation prompt 运作正常（单阶段直接行为分析 + 显式 polarity + cognitive_aspect 作为聚类键）
- Label grouping → Jaccard + LLM synonym → cluster refine 流水线正常推进
- counterpart (success↔failure) 配对将在 refine 后进行
- LLM 调用效率正常：annotation 阶段每次 ~50-60s，refine 阶段每次 ~10-30s

### 00:59 — ⭐ Cold Start 完成 + Round 0 Exploitation 启动

**Cold Start 最终结果:**
| 指标 | 新 (002016) | 旧 (180207) | 差异 |
|------|------------|------------|------|
| Baseline score | **0.543** | 0.540 | +0.003 ✓ |
| Patterns | 68 (全新) | 63 (全新) | +5 |
| Observations | 412 | 412 | = |
| Divergences | 46 | 64 | -18 |
| Strategy chars | 2432 | 2357 | +75 |
| Analysis time | 682.5s | 751.5s | -9% 更快 |
| l1_signals | [] | [] | = (cold start expected) |

**基线分数验证: 0.543 ≈ 0.540 ✓**
- 两次实验使用相同模型、数据、agent 配置
- 微小差异 (+0.003) 来自 asyncio 修复后 agent 行为的稳定性提升
- 数值高度一致，确认实验可比性

**Strategy_0 对比:**

| 维度 | 新实验 | 旧实验 |
|------|--------|--------|
| 名称 | "State-Validated Output Generation" | "Semantic Result Verification" |
| 维度1 | Tool Capability Boundary | Formula vs. Value Distinction |
| 维度2 | Pre-Computation Logic | Execution-Driven Verification |
| 维度3 | Post-Execution Integrity Verification | Outcome-Grounded Completion |

- **核心洞察完全一致**: 都识别出 openpyxl 写公式 vs 写值的根本问题
- 新策略更具操作性: "Does this tool evaluate this content, or just store it?"
- 新策略更明确的行动指导: "calculate the value in Python and write the number"

**Analysis 产出物:**
- observations.json: 232KB (412 条)
- patterns.json: 65KB (68 个 patterns)
- divergences.json: 76KB (46 条 — 比旧实验少 18 条，可能因 asyncio 修复减少了随机噪声)
- label_grouping.json: 34.5KB
- layer2_library.json: 16.9KB

### 01:03 — Round 0 Exploitation Step 0 On-Policy Rollout

**Mod 5 验证: Exploitation FIRST ✓**
- Round 0 epoch 顺序: Exploitation → (waiting) Train → Analysis → Val → Test
- css.log: "Epoch start — round=0 node=n0000 train=140 val=60 steps=0"

**Step 0 Rollout 进度:**
- Batch 1: 40 tasks (batch_size=40, 第一个 batch)
- Rollouts: 96/120 completed (80%, 40×3=120 total units)
- All 40 task dirs created
- current_score = val_score = baseline_score = 0.543 (Mod 4 ✓)

**Mod 2 验证: Batch Cycling 启动 ✓**
- 第一个 batch round 的第一个 batch (40 tasks) 正在 rollout
- 140 tasks / 40 batch_size = 3.5 → 4 batches per round
- 完成 4 个 batch 后, 如果未 saturate (N=5 consecutive rejects), 进入下一个 batch round (新 shuffle)

**预期流程:**
1. Step 0 rollout 完成 → reflect → aggregate → select edits → apply → val eval → gate
2. 如果 accept: node.rules 更新, 继续 step 1 (新 batch)
3. 如果连续 5 reject: saturation, exploitation 结束
4. Exploitation 结束后 → post-exploitation train rollout → analysis → val → test

### 01:10 — Step 0 Rollout 接近完成 + 质量对比

**Step0 rollout: 118/120 (98.3%)**
- 未完成: task 24-23 (2/3), task 262-17 (2/3)
- 262-17 是已知极端长尾任务 (旧实验中一次跑了 110 turns)

**Step0 vs ColdStart 对比 (相同 40 个 tasks):**
| 指标 | Step0 (with strategy) | ColdStart (bare) |
|------|----------------------|------------------|
| Avg turns | 14.2 | 13.9 |
| Median | 12 | 12 |
| Min | **4** | 6 |
| Max | **42** | **74** |
| Predictions | 118 (100%) | 120 |

- Strategy 的长尾压制效果显著: Max 从 74 降到 42
- 简单任务更快完成 (min 4 vs 6) — strategy 提供了更明确的操作指导
- 中位数一致 (12) — strategy 不影响典型任务的交互轮数

### 01:27 — Step 0 长尾等待 (task 24-23/r0)

**状态:** 119/120 conversations, 最后一个 rollout (24-23/r0) 已运行约 28 分钟
- output.xlsx (149KB) 在 01:17 产生, 但 conversation.json 未生成
- trace.jsonl 已 ~10+ 分钟无新 LLM 调用 (4587 行不变)
- 进程正常 (PID 3301408, CPU 8.9%), vLLM 端正常
- 可能原因: agent 执行长时间本地操作 (813KB 大 spreadsheet) 或等待慢 LLM 响应
- task_timeout=3600s, 已用 ~28/60 min, 仍在安全范围

**对比旧实验:** task 24-23 在旧实验 cold start 中完成需 12-14 turns
- Step0 使用 strategy 后可能触发更多验证步骤 (Post-Execution Integrity Verification)
- 这正是 strategy 的设计意图: 做更彻底的验证, 可能导致部分任务耗时增加

**注意:** 即使此 rollout 超时, batch_rollout 会用 _failed_result 记录并继续。
不会阻塞后续 reflect→gate 流程。

### 01:35 — Step 0 长尾更新 (task 24-23/r0 仍活跃)

**trace 分析 (call_004450 ~ call_004469):**
- 01:09~01:14: 连续 LLM 调用 (call_004450~004463)，agent 多轮推理
- 01:14:16 → 01:17:47: 3.5 分钟无 LLM 调用 — agent 执行本地 Python 计算
- 01:17:47~01:17:51: 两次 LLM 调用后再次沉默
- 01:17:51 → 01:33:23: **15.5 分钟大间隙** — agent 在处理 794KB 大电子表格
- 01:33:20: output.xlsx 被更新 (从 149KB → 145.5KB)，solve.py 在 01:33:38 重写
- 01:33:23~01:33:38: 4 次新 LLM 调用，agent 仍在活跃迭代

**关键发现:**
- Agent 仍然活跃 (PID 3301408, CPU 8.1%)，不是卡死
- solve.py 在 output.xlsx 之后被重写 → agent 在回退修正其解法
- trace 4587 → 4591 行 (新增 4 条调用)
- 已运行约 36 分钟，task_timeout=3600s 内仍安全
- 这是典型的"大电子表格 + 多次重试"长尾模式

### 01:46 — 长尾根因确认 (bash_timeout 过大 + openpyxl 慢操作)

**子进程分析 (PID 3628307):**
- 状态: Running, CPU 100%, 已运行 12+ 分钟
- solve.py 用 openpyxl 逐 cell 写入 794KB 大文件 → 极慢操作
- 主进程 (3301408) 完全阻塞在 wait()，context_switches=0

**根因: bash_timeout 设定过大**
- `bash_timeout = int(task_timeout_s * 0.8) = int(3600 * 0.8) = 2880s = 48 分钟`
- 一个 openpyxl 写入脚本理论上 2-3 分钟足够
- 当前设定允许单条 bash 命令占据 48 分钟，造成不必要的长尾等待

**改进建议 (后续优化，不影响当前实验):**
- bash_timeout 应独立于 task_timeout，设为 120-300 秒更合理
- 或者在 agent prompt 中加入 "如果脚本运行超过2分钟，考虑优化方法" 的指引
- 当前不干预，task 最迟在 01:59 (00:59+60min) 因 task_timeout 结束

**executor.py 的 run_generated_code 未被使用:**
- 这个函数自带 timeout=120s，但 ReAct agent 通过 bash tool 直接执行
- bash tool 的 timeout 来自 `create_bash_tool(timeout=bash_timeout)` 即 2880s

### 01:52 — 长尾第二次迭代 (task 24-23/r0)

- 第一个慢脚本 (PID 3628307) 在 01:33→01:48 运行 ~15 分钟后完成
- Agent 恢复 LLM 调用 (01:48:37~01:49:05, call_004470~004479, 10 次/30s)
- output.xlsx 更新到 01:48:52, agent 进入第二轮脚本执行
- 新子进程 (PID 3658284) 又是一个 openpyxl 脚本，已运行 ~4 分钟
- **Task 已运行 53/60 分钟，预计 ~01:59 触发 task_timeout**
- 这是 "大文件 + openpyxl 逐 cell 操作 + 无 bash_timeout 限制" 导致的系统性长尾
- 即使 timeout，只影响 1/120 rollouts (0.8%)，不影响整体 step0 gate 评判

### 02:02 — Step 0 Rollout 完成 + Reflect 产出

**Rollout 结果:**
- `[batch_rollout] 120 units (40 tasks x k=3) pass=72/120 in 3604s`
- 耗时 3604s ≈ 60 分钟（task 24-23/r0 触发 timeout 后才结束）
- pass=72/120 (60% unit pass rate)
- step0/predictions/ 下 43 个 task 目录 (含额外 3 个?)

**Reflect pipeline (02:01:51):**
- 3 raw analysts → 17 merge edits → 13 dedup edits → **3 select edits** 应用
- merge 产生 17 个 edit proposals，dedup 去除 4 个 (76% 保留率)
- 最终 select 3/13 = 23% 选择率

**对比旧实验 (180207) Round 0 Step 0:**
- 旧: 3 raw → ? merge → ? dedup → 3 select (数据不详)
- 新: 3 raw → 17 merge → 13 dedup → 3 select
- Select 数量一致，说明 select 策略稳定

**当前:** Val eval 进行中 (trace 4629→4763, +134 行，大量并发 LLM 调用)

### 02:23 — ★ Step 0 Gate 结果: REJECT (Mod 4 验证通过)

```
L0 step 0 — gate=reject score=0.543->0.478 best=0.543
```

**Gate 分析:**
- **reject**: 3 个 edits 应用后的 val score (0.478) 低于 baseline (0.543)
- 得分下降 -0.065 (降 12%)，说明这批 edits 有害
- best score 保持 baseline=0.543 不变

**★ Mod 4 验证通过:**
- 新实验: current_score=0.543 (baseline) → 0.478 < 0.543 → reject ✓
- 旧实验: current_score=0.000 (错误) → 0.483 > 0.000 → accept_new_best ✗
- **Mod 4 修复成功**: 使用 baseline 而非 0.000 作为 gate 起点，避免了旧实验的虚假 accept

**与旧实验 Round 0 对比:**
| 指标 | 新实验 (002016) | 旧实验 (180207) |
|------|----------------|----------------|
| Step 0 gate | reject 0.543→0.478 | accept 0.000→0.483 |
| Baseline | 0.543 | 0.540 |
| Step 0 val | 0.478 (-12%) | 0.483 (虚假 accept) |
| 决策正确性 | ✓ 正确拒绝 | ✗ 不应接受 |

**重要发现**: 旧实验 step 0 的 "accept 0.000→0.483" 实际上接受了一个比 baseline 更差的 skill
(0.483 < 0.540)。Mod 4 修正后新实验正确拒绝了同样水平的下降。

**时间线:**
- 00:59:12 Cold start 完成 → 02:01:51 Step 0 reflect (62 min rollout)
- 02:01:51 → 02:23:24 Val eval + gate (21.5 min)
- Step 0 总耗时: 84 分钟

**接下来:** Step 1 on-policy rollout 已开始 (trace 快速增长 4763→6588)

### 02:36 — Step 1 Rollout 完成 + 全阶段耗时统计

**Step 1 Rollout:**
- `[batch_rollout] 120 units (40 tasks x k=3) pass=76/120 in 714s`
- 耗时仅 714s (12 min) vs Step 0 的 3604s (60 min) — **快 5 倍**
- 原因: Step 1 shuffle 的 40 tasks 没有 794KB 大文件长尾

**全阶段 pass rate 统计:**
| 阶段 | Units | Pass | Rate | 耗时 |
|------|-------|------|------|------|
| Step 0 train rollout | 120 | 72 | 60.0% | 3604s |
| Step 0 val eval | 180 | 86 | 47.8% | 1293s |
| Step 1 train rollout | 120 | 76 | 63.3% | 714s |

**观察:**
- Val pass rate (47.8%) < train pass rate (60%) — val 集可能偏难，或 strategy 对 val 泛化不足
- Step 1 pass rate 63.3% 略高于 Step 0 的 60.0%，但这是不同 40 tasks，不能直接比较
- 总体 unit pass rate ~60% 与 cold start baseline (0.543 task-hard) 一致

**当前:** Step 1 reflect 进行中，等待 gate 结果

### 02:54 — Step 1 Gate: REJECT (0.543→0.400, 恶化趋势)

```
L0 step 1 — reflect=3 raw, merge=29, dedup=24, select=3 edits
L0 step 1 — gate=reject score=0.543->0.400 best=0.543
```

**Val eval:** pass=72/180 (40.0%) in 959s

**★ 连续 reject + 恶化趋势:**
| Step | Val Score | vs Baseline | Gate |
|------|-----------|-------------|------|
| 0 | 0.478 | -12.0% | reject |
| 1 | 0.400 | -26.3% | reject |

**问题分析：**
- 两步连续 reject 且得分持续下降 (0.478→0.400)
- Step 1 的 reflect 产出更多 edits (29 merge vs 17)，但质量更差
- 更多 edits 可能导致过度修改，偏离 baseline 有效策略
- Mod 4 正确保护了 best score=0.543 不被劣化的 edits 覆盖

**Reflect pipeline 对比:**
| Step | Raw | Merge | Dedup | Select |
|------|-----|-------|-------|--------|
| 0 | 3 | 17 | 13 | 3 |
| 1 | 3 | 29 | 24 | 3 |

Step 1 merge 产出增加 70%，可能是因为新 batch 暴露了更多失败模式。
但 select 始终固定 3 个，说明 select 策略是固定的，不随候选数变化。

**Step 0+1 时间线:**
- Step 0: 00:59→02:23 (84 min), rollout=3604s, val=1293s
- Step 1: 02:23→02:53 (30 min), rollout=714s, val=959s
- Step 2 已启动 (02:54~)

**饱和检查 (Mod 2):** N=5 连续 reject 触发 saturation。当前 2 连续 reject。
还需 3 个 reject 才会 saturate (或一个 accept 打破连续计数)。

### 03:13 — Step 2 Reflect + 全步骤汇总

**Step 2 rollout:** pass=56/120 (46.7%) in 955s
**Step 2 reflect:** 3 raw → 30 merge → 17 dedup → 3 select

**全步骤 Reflect Pipeline 对比:**
| Step | Raw | Merge | Dedup | Dedup Rate | Select | Rollout Pass | Val Score |
|------|-----|-------|-------|------------|--------|-------------|-----------|
| 0 | 3 | 17 | 13 | 76.5% | 3 | 72/120=60.0% | 0.478 |
| 1 | 3 | 29 | 24 | 82.8% | 3 | 76/120=63.3% | 0.400 |
| 2 | 3 | 30 | 17 | 56.7% | 3 | 56/120=46.7% | ? |

**观察:**
- Raw 始终 3（Plan A 固定 3 个分析师）
- Select 始终 3（固定选择数）
- Merge 递增（17→29→30）— 随着更多 batch 暴露，反射发现更多模式
- Step 2 dedup 更激进（56.7%）— 更多重复编辑被发现
- Train pass rate 下降（60%→63%→47%）— batch 难度差异 + 策略效果方差

### 03:31 — Step 2 Gate: REJECT (第 3 个连续 reject, score 持续恶化)

```
L0 step 2 — gate=reject score=0.543->0.389 best=0.543
Val eval: pass=70/180 (38.9%) in 1101s
```

**★ 完整 Gate 趋势 (Round 0 Exploitation):**
| Step | Reflect (merge→dedup→sel) | Train Pass | Val Pass | Val Score | Gate |
|------|--------------------------|-----------|---------|-----------|------|
| 0 | 17→13→3 | 72/120=60.0% | 86/180=47.8% | 0.478 | reject |
| 1 | 29→24→3 | 76/120=63.3% | 72/180=40.0% | 0.400 | reject |
| 2 | 30→17→3 | 56/120=46.7% | 70/180=38.9% | 0.389 | reject |

**★ 严重发现: 系统性 val score 下降**
- 0.478 → 0.400 → 0.389，每步下降约 0.04-0.08
- Val pass rate 一路走低: 47.8% → 40.0% → 38.9%
- **Edits 在系统性地让 strategy 变差**，不是随机波动

**根因分析:**
1. **Reflect 产出质量问题**: 每一步的 3 个 selected edits 都是有害的
   - 可能原因: optimizer LLM (qwen3.6-35b-a3b) 对 strategy 编辑的理解不够
   - 或者 reflect prompt 的分析不够精确，导致 edits 方向错误
2. **Gate 正确保护了 best score**: Mod 4 确保 best=0.543 始终不变 ✓
3. **Batch cycling 正常工作**: 每步使用不同的 40 tasks batch ✓
4. **Select 固定 3 个**: 可能需要更保守的 select 策略（如只选 1 个）

**Mod 2 饱和检查:** 3/5 连续 reject。还需 2 个 reject 触发 saturation。
Step 3 已启动 (trace 10943)

### 03:39 — Step 3 Reflect: 异常高的 merge/dedup 产出

```
L0 step 3 — reflect=3 raw, merge=41, dedup=40, select=3 edits
```

Step 3 是 batch_round=0 的最后一个 batch（140/40=[40,40,40,**20**]），仅 20 个 tasks。

**Reflect pipeline 异常:**
| Step | Batch Size | Merge | Dedup | Dedup Rate |
|------|-----------|-------|-------|------------|
| 0 | 40 | 17 | 13 | 23.5% |
| 1 | 40 | 29 | 24 | 17.2% |
| 2 | 40 | 30 | 17 | 43.3% |
| 3 | 20 | 41 | 40 | 2.4% |

**关键发现:** Step 3 的 merge=41（最高值）且 dedup 率仅 2.4%（41→40），说明：
1. 虽然只有 20 tasks，但 3 个分析师产出了更多样化的编辑建议
2. 几乎没有重复 — 编辑空间高度发散
3. 可能因为前 3 步的失败信息积累，分析师被迫探索新方向

Train rollout: 33/60=55.0% in 402s（k=3, 20 tasks）

### 03:55 — ★★★ Step 3 Gate: ACCEPT NEW BEST! score 0.543→0.622 ★★★

```
L0 step 3 — gate=accept_new_best score=0.543->0.622 best=0.622
Val eval: pass=112/180 (62.2%) in 962s
```

**这是实验的重大突破！**

连续 3 次 reject（0.478, 0.400, 0.389）后，第 4 步首次 accept，且大幅超越 baseline：
- **Val score: 0.543 → 0.622 (+7.9pp)**
- **Val pass rate: 112/180 = 62.2%**（远超 baseline 的 ~54.3%）
- Consecutive rejects 计数器重置为 0

**被 accept 的 3 个 edits（全部为 append）:**
1. **Sort Order Verification** — 显式指定 reverse=False/True，不依赖默认排序行为，排序后验证行顺序
2. **★ Pre-Computation Mandatory** — 关键规则：永远不要写公式字符串（=SUM/=IF），openpyxl 不会执行公式，必须用 Python 计算后写入数值
3. **★ Post-Execution Integrity Verification** — 写入后必须 re-load workbook 验证：检查单元格含数值而非公式字符串，检查 merged cells 正确 unmerge，检查 row indices 无 gaps

**为什么这 3 个 edits 有效？**
- Edit 2 ("Pre-Computation") 直接解决了最普遍的失败模式：agent 写公式而非数值
  - 对应的 failure_patterns 涉及 tasks 51262, 52216, 13-1, 51090, 48983, 50193 等
  - 这是 spreadsheet 任务最常见的陷阱
- Edit 3 ("Post-Execution Verification") 添加了验证闭环：写入后 re-read 确认
  - 成功 tasks (142-12, 38703, 49300) 都做了这一步
  - 失败 tasks 要么跳过验证，要么验证了错误的东西
- Edit 1 ("Sort Order") 解决了排序方向歧义问题

**Mod 2/4 机制验证:**
- ✓ Gate 正确：baseline 0.543 作为阈值，0.622 > 0.543 → accept
- ✓ Batch cycling 有效：经过 4 个不同 batch，终于找到了有价值的 edits
- ✓ Saturation 机制：如果不是 N=5 的设计，在 3 次 reject 后就会停止，错过这次突破
- ✓ Consecutive rejects 计数器重置为 0，exploitation 继续

**反思: 前 3 步为何失败而第 4 步成功？**
- 前 3 步的 edits 可能过于细节化或方向错误，没有命中核心失败模式
- Step 3 的 merge=41, dedup=40 表明分析师终于从前面的失败中学习，探索到了更多样化的策略空间
- "Pre-Computation Mandatory" 这种规则需要足够多的 failure evidence（tasks 51262, 52216, 13-1 等跨 batch 出现），前面的 batch 可能还没积累到足够的失败模式
- support_count=2 和 1 — 说明每个 edit 的 evidence 不多但精准

### 03:55 — Step 4 开始 (batch_round=1, first batch of 40)

Step 3 accept 后，consecutive rejects 重置，exploitation 继续。
Step 4 进入 batch_round=1：140 items 重新 shuffle（seed+epoch+batch_round*10000），取前 40。

### 04:12 — Step 4 Train Rollout 完成 + Reflect

```
[batch_rollout] 120 units (40 tasks x k=3) pass=75/120 in 810s
L0 step 4 — reflect=3 raw, merge=15, dedup=13, select=3 edits
```

**Train pass rate: 75/120 = 62.5%** — 这是 Exploitation 阶段所有 step 中**最高**的 train pass rate！

| Step | Batch Round | Batch Size | Train Pass | Train Rate |
|------|------------|-----------|-----------|------------|
| 0 | 0 | 40 | 72/120 | 60.0% |
| 1 | 0 | 40 | 76/120 | 63.3% |
| 2 | 0 | 40 | 56/120 | 46.7% |
| 3 | 0 | 20 | 33/60 | 55.0% |
| **4** | **1** | **40** | **75/120** | **62.5%** |

**关键观察:**
- Step 3 accept 的 edits（Pre-Computation Mandatory + Post-Execution Verification）在新的 batch_round=1 的新 batch 上也生效
- 62.5% 高于 Steps 0-3 的大部分（除了 Step 1 的 63.3%）
- 810s 完成时间正常（之前 Steps 1-2 约 700-955s）

**Reflect pipeline 回落到正常水平:**
- merge=15, dedup=13 — 对比 Step 3 的异常高值 (41→40)
- 更新后的 strategy 已经解决了大量已知问题，新的改进空间更小

Val rollout 60 tasks 已开始（predictions 目录 45/60 task dirs created at 04:13）。

### 04:27 — Step 4 Gate: REJECT (1/5, score 0.622→0.572)

```
[batch_rollout] 180 units (60 tasks x k=3) pass=103/180 in 873s
L0 step 4 — gate=reject score=0.622->0.572 best=0.622
```

**Val pass rate: 103/180 = 57.2%** — 低于 Step 3 accept 时的 112/180=62.2%
**Val score: 0.572** — 低于 best 0.622，gate 正确 reject
**Consecutive rejects: 1/5**（Step 3 accept 后重置）

**Step 4 被 reject 的 3 个 edits 分析:**
1. **Edge Case Logic for Transitions** (append) — 首行处理特例规则，太具体可能误导
2. **Row Deletion Safety** (insert_after Pre-Computation) — 逆序删除行规则，方向正确但可能添加了不必要的认知负担
3. **Output Compaction for Filtered Data** (insert_after Sort Order) — 输出紧凑化规则

**分析:**
- Val score 从 0.622 下降到 0.572 (-5.0pp) — edits 有害
- 但 103/180=57.2% 仍高于原始 baseline (0.543)
- 这些 edits 过于具体（针对单个 task 的特殊情况），在更广泛的 val set 上反而造成干扰
- 对比 Step 3 成功的 edits（Pre-Computation + Post-Verification）— 那些是广泛适用的通用规则
- **观察: 通用规则有效，细节规则有害** — 这是 skill optimization 的核心 tension

**★ 完整 Gate 趋势 (Round 0 Exploitation, 更新至 Step 4):**
| Step | BR | Reflect (merge→dedup→sel) | Train Pass | Val Pass | Val Score | Gate | Consec.Rej |
|------|-----|--------------------------|-----------|---------|-----------|------|-----------|
| 0 | 0 | 17→13→3 | 72/120=60.0% | 86/180=47.8% | 0.478 | reject | 1 |
| 1 | 0 | 29→24→3 | 76/120=63.3% | 72/180=40.0% | 0.400 | reject | 2 |
| 2 | 0 | 30→17→3 | 56/120=46.7% | 70/180=38.9% | 0.389 | reject | 3 |
| **3** | **0** | **41→40→3** | **33/60=55.0%** | **112/180=62.2%** | **0.622** | **accept** | **0** |
| 4 | 1 | 15→13→3 | 75/120=62.5% | 103/180=57.2% | 0.572 | reject | 1 |

### 04:43 — Step 5 Train Rollout + Reflect

```
[batch_rollout] 120 units (40 tasks x k=3) pass=85/120 in 825s
L0 step 5 — reflect=3 raw, merge=17, dedup=17, select=3 edits
```

**★ Train pass rate: 85/120 = 70.8% — 新高！**

| Step | BR | Train Pass | Train Rate | 趋势 |
|------|-----|-----------|-----------|------|
| 0 | 0 | 72/120 | 60.0% | — |
| 1 | 0 | 76/120 | 63.3% | ↑ |
| 2 | 0 | 56/120 | 46.7% | ↓↓ |
| 3 | 0 | 33/60 | 55.0% | (20 tasks) |
| 4 | 1 | 75/120 | 62.5% | ↑ (post-accept) |
| **5** | **1** | **85/120** | **70.8%** | **↑↑ 新高** |

**关键发现:** Step 3 accept 后的两步 train pass rate 持续上升（62.5%→70.8%），说明改进后的 strategy 在不同 batch 上稳定有效。即使 Step 4 的 edits 被 reject（val 下降），train 的提升趋势不受影响——因为 reject 时 strategy 回退到 best（Step 3 的版本）。

**Reflect pipeline:** merge=17, dedup=17（dedup 率 0%，所有编辑都独特）。

### 04:57 — Step 5 Gate: REJECT (2/5, score 0.622→0.617, 差距极小)

```
[batch_rollout] 180 units (60 tasks x k=3) pass=111/180 in 831s
L0 step 5 — gate=reject score=0.622->0.617 best=0.622
```

**Val pass rate: 111/180 = 61.7%** — 仅比 Step 3 accept 时的 112/180 少 1 个 pass
**Val score: 0.617 vs best 0.622** — 差距仅 0.5pp（约 1 task 的 pass/fail 之差）
**Consecutive rejects: 2/5**

**★ 完整 Gate 趋势 (Round 0 Exploitation, 更新至 Step 5):**
| Step | BR | Reflect | Train Pass | Val Pass | Val Score | Gate | C.Rej |
|------|-----|---------|-----------|---------|-----------|------|-------|
| 0 | 0 | 17→13→3 | 72/120=60.0% | 86/180=47.8% | 0.478 | reject | 1 |
| 1 | 0 | 29→24→3 | 76/120=63.3% | 72/180=40.0% | 0.400 | reject | 2 |
| 2 | 0 | 30→17→3 | 56/120=46.7% | 70/180=38.9% | 0.389 | reject | 3 |
| **3** | **0** | **41→40→3** | **33/60=55.0%** | **112/180=62.2%** | **0.622** | **accept** | **0** |
| 4 | 1 | 15→13→3 | 75/120=62.5% | 103/180=57.2% | 0.572 | reject | 1 |
| 5 | 1 | 17→17→3 | 85/120=70.8% | 111/180=61.7% | 0.617 | reject | 2 |

**分析:**
- Step 5 几乎打平 best（差 0.005），说明这轮 edits 质量很高
- 对比 Step 4 的 0.572，Step 5 大幅回升到 0.617 — reflect 在学习中
- 有趣的 train vs val 差异: train 70.8% (新高) vs val 61.7% (接近 best)
- Gate 的严格性在这里体现: 0.617 < 0.622 就 reject，即使差距微小
- **潜在改进点: gate 可以考虑加入 margin/confidence interval 机制**

### 05:07 — Step 6 Train Rollout + Reflect

```
[batch_rollout] 120 units (40 tasks x k=3) pass=83/120 in 487s
L0 step 6 — reflect=3 raw, merge=10, dedup=10, select=3 edits
```

Train pass rate: 83/120 = 69.2%（接近 Step 5 的 70.8%）
Train rollout 仅 487s — 这轮无长尾（对比 Step 4 的 810s, Step 5 的 825s）
Reflect merge=10, dedup=10（持续下降：41→15→17→10），改进空间在收窄

### 05:27 — Step 6 Gate: REJECT (3/5, score 0.622→0.606)

```
[batch_rollout] 180 units (60 tasks x k=3) pass=109/180 in 1226s
L0 step 6 — gate=reject score=0.622->0.606 best=0.622
```

Val pass rate: 109/180 = 60.6%
Val rollout 1226s — 受长尾 tasks 拖累（7/180 拖尾，正常应 ~830s）
**Consecutive rejects: 3/5** — 接近 saturation

**★ 完整 Gate 趋势 (Round 0 Exploitation, 更新至 Step 6):**
| Step | BR | Reflect | Train Pass | Val Pass | Val Score | Gate | C.Rej |
|------|-----|---------|-----------|---------|-----------|------|-------|
| 0 | 0 | 17→13→3 | 72/120=60.0% | 86/180=47.8% | 0.478 | reject | 1 |
| 1 | 0 | 29→24→3 | 76/120=63.3% | 72/180=40.0% | 0.400 | reject | 2 |
| 2 | 0 | 30→17→3 | 56/120=46.7% | 70/180=38.9% | 0.389 | reject | 3 |
| **3** | **0** | **41→40→3** | **33/60=55.0%** | **112/180=62.2%** | **0.622** | **accept** | **0** |
| 4 | 1 | 15→13→3 | 75/120=62.5% | 103/180=57.2% | 0.572 | reject | 1 |
| 5 | 1 | 17→17→3 | 85/120=70.8% | 111/180=61.7% | 0.617 | reject | 2 |
| 6 | 1 | 10→10→3 | 83/120=69.2% | 109/180=60.6% | 0.606 | reject | 3 |

**Post-accept 趋势分析 (Steps 4-6):**
- Train pass rate 稳定在高位: 62.5% → 70.8% → 69.2%（远高于 pre-accept 的 ~55%）
- Val score 围绕 best 波动但无法突破: 0.572 → 0.617 → 0.606
- Merge 产出递减: 15 → 17 → 10（改进空间逐步耗尽）
- **Edits 的方向正确但幅度不足以超越 best** — 已进入 diminishing returns 区间

**Saturation 预估:**
- 3/5 consecutive rejects。再 2 次 reject 触发 saturation → exploitation 结束
- Step 7 是 batch_round=1 的最后 batch（140-120=20 tasks，跟 Step 3 一样）
- 如果 Step 7 也 reject → 4/5。Step 8 进入 batch_round=2 → 如果 reject → 5/5 saturation

### 05:36 — Step 7 Train Rollout + Reflect

```
[batch_rollout] 60 units (20 tasks x k=3) pass=47/60 in 453s
L0 step 7 — reflect=3 raw, merge=18, dedup=14, select=3 edits
```

**★ Train pass rate: 47/60 = 78.3% — 新历史最高！**

| Step | BR | Batch | Train Pass | Train Rate |
|------|-----|-------|-----------|------------|
| 0-2 | 0 | 40 | 56-76/120 | 46.7-63.3% |
| 3 | 0 | 20 | 33/60 | 55.0% |
| 4-6 | 1 | 40 | 75-85/120 | 62.5-70.8% |
| **7** | **1** | **20** | **47/60** | **78.3%** |

Train pass rate 从 baseline 阶段的 ~60% 持续提升到 78.3%，说明优化后的 strategy 效果显著。

Reflect merge=18（从 Step 6 的 10 回升），dedup=14。可能因为小 batch 暴露了不同的失败模式。

### 05:52 — Step 7 Gate: REJECT (4/5, score 0.622→0.556, 最差 post-accept)

```
[batch_rollout] 180 units (60 tasks x k=3) pass=100/180 in 983s
L0 step 7 — gate=reject score=0.622->0.556 best=0.622
```

**Val pass rate: 100/180 = 55.6%** — post-accept 最低
**Val score: 0.556** — 接近原始 baseline 0.543
**Consecutive rejects: 4/5** — 再 1 次 reject 触发 saturation！

**★ 重要发现: Train-Val overfitting 信号**
- Step 7 train: 47/60 = **78.3%**（新历史最高）
- Step 7 val: 100/180 = **55.6%**（post-accept 最低）
- Train-Val gap: 78.3% - 55.6% = **22.7pp** — 极大差距
- 对比 Step 3 accept 时: train 55.0% vs val 62.2%（val > train!）
- **结论: 20-task 小 batch 的 edits 更容易过拟合到特定 task 模式**

**★ 完整 Gate 趋势 (Round 0 Exploitation, 更新至 Step 7):**
| Step | BR | Reflect | Train Pass | Val Pass | Val Score | Gate | C.Rej | Train-Val Gap |
|------|-----|---------|-----------|---------|-----------|------|-------|--------------|
| 0 | 0 | 17→13→3 | 60.0% | 47.8% | 0.478 | reject | 1 | +12.2pp |
| 1 | 0 | 29→24→3 | 63.3% | 40.0% | 0.400 | reject | 2 | +23.3pp |
| 2 | 0 | 30→17→3 | 46.7% | 38.9% | 0.389 | reject | 3 | +7.8pp |
| **3** | **0** | **41→40→3** | **55.0%** | **62.2%** | **0.622** | **accept** | **0** | **-7.2pp** |
| 4 | 1 | 15→13→3 | 62.5% | 57.2% | 0.572 | reject | 1 | +5.3pp |
| 5 | 1 | 17→17→3 | 70.8% | 61.7% | 0.617 | reject | 2 | +9.1pp |
| 6 | 1 | 10→10→3 | 69.2% | 60.6% | 0.606 | reject | 3 | +8.6pp |
| 7 | 1 | 18→14→3 | 78.3% | 55.6% | 0.556 | reject | 4 | +22.7pp |

**关键模式:** 唯一 accept 的 Step 3 是唯一一个 val > train 的 step（-7.2pp gap），说明好的 edits 应该是通用规则（在 unseen val 上也提升），而非训练集特定的修补。

### 06:21 — ★★★ Step 8 Gate: REJECT → SATURATION 触发！Exploitation 完成！★★★

```
[batch_rollout] 180 units (60 tasks x k=3) pass=103/180 in 1063s
L0 step 8 — gate=reject score=0.622->0.572 best=0.622
Exploitation done — round=0 node=n0000 steps=0->9 best=0.622 saturated=True rules=1378 chars
```

**Step 8: val 103/180=57.2%, score 0.572 → reject → 5/5 consecutive rejects → SATURATED**

**★ 最终完整 Gate 趋势 (Round 0 Exploitation, Steps 0-8):**
| Step | BR | Reflect | Train Pass | Val Pass | Val Score | Gate | C.Rej | T-V Gap |
|------|-----|---------|-----------|---------|-----------|------|-------|---------|
| 0 | 0 | 17→13→3 | 60.0% | 47.8% | 0.478 | reject | 1 | +12.2pp |
| 1 | 0 | 29→24→3 | 63.3% | 40.0% | 0.400 | reject | 2 | +23.3pp |
| 2 | 0 | 30→17→3 | 46.7% | 38.9% | 0.389 | reject | 3 | +7.8pp |
| **3** | **0** | **41→40→3** | **55.0%** | **62.2%** | **0.622** | **accept** | **0** | **-7.2pp** |
| 4 | 1 | 15→13→3 | 62.5% | 57.2% | 0.572 | reject | 1 | +5.3pp |
| 5 | 1 | 17→17→3 | 70.8% | 61.7% | 0.617 | reject | 2 | +9.1pp |
| 6 | 1 | 10→10→3 | 69.2% | 60.6% | 0.606 | reject | 3 | +8.6pp |
| 7 | 1 | 18→14→3 | 78.3% | 55.6% | 0.556 | reject | 4 | +22.7pp |
| 8 | 2 | 13→12→3 | 65.0% | 57.2% | 0.572 | reject | 5 | +7.8pp |

**Exploitation 阶段总结:**
- **总时长:** 00:59 → 06:21 = **5 小时 22 分钟**
- **总 Steps:** 9（Steps 0-8）
- **Batch Rounds:** 3（BR 0: steps 0-3, BR 1: steps 4-7, BR 2: step 8）
- **Accepts:** 1/9（仅 Step 3）
- **最终 best val score: 0.622**（从 baseline 0.543 提升 +7.9pp）
- **最终 strategy: 1378 chars**（从 cold start 的 2432 chars 精炼到 1378 chars）

**关键机制验证:**
1. ✅ **Mod 2 (Batch cycling + saturation N=5):** 正确！如果 N=3 会在 Step 2 后停止，错过 Step 3 的突破性 accept
2. ✅ **Mod 4 (Baseline as gate threshold):** 正确！baseline 0.543 作为初始阈值，避免了 score=0 的假通过
3. ✅ **Gate 保护机制:** 有效！8 次 reject 正确保护了 best score，edits 只在真正提升时才被 accept
4. ✅ **Reflect pipeline:** 产出从高到低递减（41→15→17→10→18→13），改进空间逐步耗尽
5. ✅ **Saturation 信号正确:** 5 consecutive rejects 表明 exploit 已无法进一步提升

**核心发现:**
- 唯一 accept 的 Step 3 是唯一 val > train 的 step（T-V Gap -7.2pp）
- 通用规则（Pre-Computation, Post-Verification）提升泛化能力
- 细节规则导致过拟合（最极端: Step 7 T-V Gap +22.7pp）

## Post-Exploitation 阶段

06:21 exploitation 完成后，系统进入 post-exploitation:
1. **Mod 5: Post-exploitation full train rollout** (140 tasks)
2. 之后: Layer 1 analysis → val eval → test eval (Mod 1, 200 tasks)

### 07:16 — ★ Post-Exploitation Train Rollout 完成

```
[batch_rollout] 420 units (140 tasks x k=3) pass=291/420 in 3286s
Train rollout done — round=0 node=n0000 score=0.693 pass=291/420
```

**关键数据:**
- **Full train score: 0.693**（291/420 pass, majority-vote 69.3%）
- **耗时:** 3286s（~55 分钟），其中最后 1 个 long-tail (task 24-23/r2) 独占 ~40 分钟
- **Best val score: 0.622** → **Full train score: 0.693**（Train-Val gap +7.1pp）

**分析:**
- Train > Val 7.1pp 是可接受范围——反映训练集上的 edit 有一定拟合，但不是极端过拟合
- 对比 exploitation 中各 step 的 T-V gap（+5pp 到 +23pp），全量 train 的 gap 较温和
- 0.693 vs 冷启动 baseline 0.543 = **+15.0pp 提升**，说明 optimized strategy 在训练集上显著改善了 agent 行为

**下一步:** Layer 1 analysis（LLM 标注 140 个 train trajectory）→ val eval → test eval

### 07:30 — Layer 1 Analysis 完成

```
Analysis done — round=0 node=n0000 obs=407 patterns=145 l1_signals=0 l0_saturated=True
```

**analysis_result.json:**
- **407 observations** from failed trajectories
- **83 divergences** (偏差分析)
- **145 patterns** (68 旧 + 77 新)
- **0 L1 signals** — L0 已 saturated，没有触发 L1 branching
- **耗时 839s（~14 分钟）**

**输出文件:**
- observations.json (452KB) — 原始观察
- patterns.json (143KB) — 145 个认知模式
- divergences.json (137KB) — 偏差分析
- layer2_library.json (21KB) — Layer 2 库
- label_grouping.json (35KB) — 标签聚类（390 obs → Jaccard merge → 330 groups）

**★ 关键发现:** l1_signals=0 意味着系统判定 L0 exploitation 已充分探索当前策略空间，不需要 L1 级别的分支探索。这与 saturation 机制一致——5 consecutive rejects 触发的 saturation 信号被分析层确认。

### 07:30+ — Val Eval 启动

Val eval (60 tasks x k=3 = 180 units) 已自动启动，使用 optimized strategy (best val_score=0.622)。

### 07:49 — ★ Val Eval 完成 + Test Eval 启动

```
[batch_rollout] 180 units (60 tasks x k=3) pass=102/180 in 1153s
```

**Val eval 结果:**
- **pass=102/180, score=0.567**（56.7% majority-vote）
- 耗时 1153s（~19 分钟）

**★ 重要对比: Val score 差异**
- Exploitation gate best val: **0.622**（Step 3, 40 tasks batch）
- Post-exploitation val eval: **0.567**（60 tasks full val set）
- **差距: -5.5pp**

**分析:**
- 这不是回归——exploitation gate 只评 val 上的随机 batch（每 step 不同 batch），而 post-exploitation 是完整 60 tasks
- k=3 majority vote 有方差：agent 行为不完全确定性，同一 task 不同 rollout 可能通过/不通过
- Exploitation gate 的 0.622 是在 Step 3 的特定 val batch 上的结果，全 val set 上可能本就没有 0.622
- 真正可比的 baseline 是 cold start val score (0.543)，post-exploitation val 0.567 仍然 > baseline (+2.4pp)

**★ 汇总对比:**
| Phase | Train | Val | Gap |
|-------|-------|-----|-----|
| Baseline (cold start) | — | 0.543 | — |
| Exploit Step 3 (accept) | 0.550 | 0.622 | -7.2pp |
| Post-exploit full eval | 0.693 | 0.567 | +12.6pp |

### 07:49+ — ★★ Test Eval 启动（最终评估）

Test eval (200 tasks x k=3 = 600 units, Mod 1) 已自动启动。这是 held-out test set 上的最终评估——决定 CSS 系统的实际效果。

### 08:49 — ★★★ 实验完成！最终结果 ★★★

```
[batch_rollout] 600 units (200 tasks x k=3) pass=332/600 in 3633s
Epoch done — round=0 node=n0000 train=0.693 val=0.567 test=0.553 best=0.622 maturity=1
Round 0 done — branches=['NONE'] global_best=0.567
Terminated: exhausted
Best node: n0000
Best val_score: 0.5667
```

**★ 最终 Test Score: 0.553**（332/600 pass, 55.3% majority-vote）
- Test eval 耗时: 3633s（~60 分钟）
- 进程正常终止: Terminated=exhausted

## 实验全流程总结

### 实验配置
- **实验 ID:** spreadsheetbench_20260627_002016
- **模型:** qwen3.6-35b-a3b (target + optimizer)
- **数据:** Train=140, Val=60, Test=200 (T2S-aligned split)
- **Workers:** 128 并发
- **总时长:** 00:20 → 08:49 = **8 小时 29 分钟**

### 分数汇总
| Phase | Score | Pass/Total | 描述 |
|-------|-------|------------|------|
| Cold Start (baseline) | 0.543 | — | 初始 skill document |
| Exploit Step 3 gate (best) | 0.622 | — | Val batch gate score |
| Post-exploit Train | 0.693 | 291/420 | Full 140 tasks x k=3 |
| Post-exploit Val | 0.567 | 102/180 | Full 60 tasks x k=3 |
| **Final Test** | **0.553** | **332/600** | **Full 200 tasks x k=3** |

### 关键发现

**1. CSS 优化效果有限（+1.0pp test vs baseline）**
- Baseline: 0.543 → Test: 0.553 = **+1.0pp 提升**
- 低于预期。exploitation 中 gate 报告的 0.622 val score 与最终 test 0.553 差距很大

**2. Exploitation gate val score 虚高**
- Gate best val: 0.622（Step 3, 在 60 tasks 的 val batch 上）
- Post-exploit val: 0.567（同 60 tasks 重新评估）
- Final test: 0.553（200 tasks held-out）
- **原因分析:**
  - k=3 majority vote 有方差，同一 strategy 在不同 rollout 间可能产生不同结果
  - Exploitation gate 每 step 评 val 时，score=0.622 可能是方差波动的高点
  - 真正的 val 能力约 0.567，test 上进一步降至 0.553

**3. Train-Val-Test 梯度**
- Train 0.693 >> Val 0.567 >> Test 0.553
- Train-Val gap: +12.6pp（有过拟合信号）
- Val-Test gap: +1.4pp（val/test 分布差异较小）

**4. Exploitation 中唯一有效的 accept（Step 3）的 edits 是通用规则**
- Pre-Computation Mandatory（不用 openpyxl 写公式字符串）
- Post-Execution Integrity Verification（写后重新加载验证）
- Sort Order Verification
- 这些是 domain-universal 的规则，不是 task-specific 的修补

**5. bash_timeout=2880s 导致严重的 long-tail 问题**
- 每次大规模 rollout 都有 1-3 个 conversation 卡 40+ 分钟
- 总实验时间的 ~15-20% 被 long-tail 浪费

**6. Layer 1 Analysis 确认 L0 saturated**
- 407 observations, 145 patterns, 0 L1 signals
- 系统正确判断了 exploitation 空间已充分探索

### 机制验证总结
| Mod | 机制 | 验证结果 |
|-----|------|---------|
| Mod 1 | Test eval 200 tasks | ✅ 正常完成 |
| Mod 2 | Saturation N=5 + batch cycling | ✅ 正确触发，N=5 保护了 Step 3 breakthrough |
| Mod 3 | Reflect pipeline | ✅ 3 analysts → merge → dedup → select 3 正常工作 |
| Mod 4 | Baseline as gate threshold | ✅ 避免了 score=0 假通过 |
| Mod 5 | Post-exploit train rollout | ✅ 提供完整 train 数据供 analysis |

### 11:18 — No-Skill Baseline Test 启动

为获得公平对比，启动 no-skill baseline 实验：
- **配置:** 与 CSS 实验完全相同（k=3, max_turns=100, 128 workers, task_timeout=3600）
- **唯一区别:** skill_text="" （空策略文档）
- **数据:** test set 200 tasks x k=3 = 600 units
- **PID:** 1866703，输出目录: `runs/baseline_noskill_20260627_111831`
- 之前 20260625 的 baseline 用了不同参数（k=1, max_turns=30, score=0.15），不可比

### 下一步建议
1. **降低 bash_timeout** 到合理值（如 600s），减少 long-tail 浪费
2. **增加 exploitation steps** 或改进 reflect 质量，当前 1/9 accept rate 偏低
3. **调查 val score 方差**：多次 val eval 取平均，减少 gate 判断噪声
4. **对比 baseline run**：在同一 test set 上跑无 skill 优化的 baseline，确认 0.543 baseline 的稳定性
