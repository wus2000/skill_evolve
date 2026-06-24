# 训练机制 v2：基于代码库实证分析的完整设计

## 一、代码库分析核心发现摘要

### 1.1 SkillOpt —— 局部优化的精细工程

**训练循环** (`trainer.py`): 6阶段流水线 Rollout→Reflect→Aggregate→Select→Update→Evaluate
- **双速更新架构**: Fast update (step级, 小手术式edits) + Slow update (epoch级, 纵向对比同一批20个任务)
- **三层记忆系统**: (1) skill document本身, (2) slow_update的protected region (epoch级指导), (3) meta_skill (优化器侧跨epoch记忆)
- **梯度计算** (`reflect.py`): minibatch式——将失败/成功轨迹分组为minibatch, 每组独立产生patch, 类似minibatch SGD
- **梯度聚合** (`aggregate.py`): 层次化merge——failure patches优先, 成功patches低优先级, 并行层次merge后合并
- **评估门控** (`evaluate_gate`): candidate skill必须在selection set上得分超过current score才accept, 否则reject回滚
- **step buffer**: 同epoch内accumulate失败模式+被拒绝edits的历史, 避免重复无效尝试
- **关键局限**: 所有优化始终在同一skill document上做增量修改, 无法跳出初始策略空间

### 1.2 Arbor —— 树搜索的结构化探索

**IdeaTree** (`idea_tree.py`): Node(hypothesis, insight, score, status, related_work)
- **层次化搜索**: Depth 1=广策略方向(abstract), Depth 2+=具体可实现方案
- **OBSERVE→IDEATE→SELECT→DISPATCH→DECIDE循环**: 持久ReAct loop, IdeaTree作为跨context compression的持久记忆
- **Executor隔离**: 每个idea在独立git worktree中执行, 互不干扰
- **决策机制**: merge(超阈值合入trunk), prune(方向失败), retry(执行未完成)
- **关键insight**: Depth 1的idea是"framing a direction"而非implementation, 真正的实现在子节点展开
- **关键启发**: **树搜索是管理qualitatively different approaches的天然结构**

### 1.3 MLEvolve —— MCTS驱动的代码进化搜索

**搜索节点** (`search_node.py`): UCT value = exploitation + exploration, Bayesian alpha/beta tracking
- **动作类型**: draft(初始方案), improve(改进), debug(修bug), evolution(进化), fusion(融合), aggregation(跨分支聚合)
- **选择策略** (`node_selection.py`): 
  - `select_with_soft_switch`: 时间加权从exploration→exploitation
  - 早期: 高exploration weight, UCT选择
  - 后期: Top-K exploitation, 加权随机选高分节点
  - Branch diversity constraint: 限制同branch选择数量
- **停滞检测**: `is_branch_stagnant` → 触发evolution/fusion而非继续improve
- **关键启发**: **exploration/exploitation balance的时间自适应; 分支停滞时触发结构变异(evolution/fusion)**

### 1.4 POLCA/Trace —— 计算图上的文本梯度

**Optimizer** (`optimizer.py`): 抽象接口: propose → update, backward propagation through trace graph
- **OptoPrime** (`optoprime.py`): 将优化问题结构化为ProblemInstance (instruction, code, variables, inputs, outputs, feedback), LLM通过reasoning产生suggestion
- **OptoPrimeMulti** (`optoprimemulti.py`): 多候选生成 + best-of-N选择
- **Guide/Suggest** (`guide.py`, `suggest.py`): 分离评估(metric)和反馈生成(verbal feedback)
- **TraceGraph**: 自动追踪变量间的依赖关系, 将"为什么错"的信号精确归因到具体variables
- **关键启发**: **将反馈信号精确归因到可优化变量; 分离score判定和verbal feedback**

---

## 二、核心设计哲学：我们的方法与相关工作的关系

### 2.1 全景对比

| 维度 | SkillOpt | SkillGrad | Trace2Skill | Arbor | MLEvolve | POLCA/Trace | **我们** |
|------|---------|-----------|-------------|-------|----------|-------------|---------|
| 搜索范式 | 单链贪心爬山 | 单链无回退 | 离线单代 | 树搜索 | MCTS+进化 | 图优化 | **QD population search** |
| 优化对象 | 单skill doc | 三层skill包 | SKILL.md+refs | 算法/方案 | 代码解 | 可优化变量 | **框架空间×战术知识** |
| 多样性维护 | 无 | 无 | 无(多seed) | 深度层次 | 分支UCT | best-of-N | **QD-Archive行为覆盖** |
| 反退化 | held-out gate✓ | 无gate | 人工选优 | score阈值 | metric比较 | FIFO buffer | **双层gate+跨框架仲裁** |
| 诊断/编辑分离 | 耦合 | **彻底分离** | 分离 | N/A | N/A | 分离 | **分离(借鉴SkillGrad)** |
| 经验利用 | 只看失败/成功 | +对比诊断 | +Lean Path | insight传播 | memory层 | 归因反馈 | **失败/成功/对比/纵向四源** |

### 2.2 核心差异化定位

所有现有 skill document 优化工作 (SkillOpt/SkillGrad/Trace2Skill) 共享一个根本局限：**搜索退化为单链贪心**。它们在固定策略子空间内做局部优化，无法发现质变不同的认知框架。我们的贡献是在此之上增加一层：**在认知框架空间上做 population-based search**，发现并维护多样的高层策略，每个框架内部再复用 SkillOpt-style 的局部优化。

---

## 三、完整训练机制设计

### 3.0 核心数据结构

```
CognitiveFramework:
  id: str
  domain_theory: str          # 对任务域的根本认识 (e.g., "这是一个需要分步验证的数学推理任务")
  attention_pattern: str       # 决定agent关注什么
  work_structure: str          # 决定agent怎样组织工作
  correctness_criteria: str    # 决定agent怎样判断对错
  tactical_skill: str          # 该框架下SkillOpt-style积累的战术知识
  
  # 评估记录
  score_history: List[float]
  behavior_descriptor: np.ndarray  # 行为特征向量(用于QD-Archive)
  generation: int               # 认知进化代数
  parent_id: Optional[str]      # 衍生自哪个框架
  
FrameworkArchive:              # MAP-Elites Archive
  cells: Dict[Tuple, CognitiveFramework]  # behavior_descriptor离散化后的格子
  elite_pool: List[CognitiveFramework]    # 按score排序的精英池
  
TrainingState:
  archive: FrameworkArchive
  current_framework: CognitiveFramework
  task_bank: TaskBank           # 带分层采样的任务集
  epoch: int
  step: int
```

### 3.1 Phase 0: 冷启动 —— 初始框架种群生成

**灵感来源**: MLEvolve的coldstart + Arbor的Depth-1 diverse strategies + SkillOpt的initial skill

**机制**:
1. **经验采集**: 在任务子集上用vanilla agent (无skill) 执行rollout, 收集k组轨迹
2. **四轮渐进分析** (从之前设计保留):
   - Round 1: 每条轨迹 → 原子观察 (agent做了什么, 什么奏效/失败)
   - Round 2: 每batch轨迹 → 行为主题聚类 (什么类型的行为pattern)
   - Round 3: 跨batch → 环境特征×行为缺陷×成功模式的交叉分析
   - Round 4: 抽象为domain theory candidates (对任务域的根本认识是什么)
3. **框架生成**: 每个domain theory → derive (attention_pattern, work_structure, correctness_criteria)
4. **多样性保障**: 
   - 显式要求不同domain theories必须是"对任务域本质的不同理解"
   - 类似Arbor的约束: "each should explore a fundamentally different axis"
   - 生成N个初始框架 (N=3~8, 取决于compute budget)
5. **初始tactical skill**: 每个框架附带空的tactical_skill (或minimal seed)

**关键设计决策**: 不从一个框架开始然后分裂, 而是从经验中**同时涌现**多个不同框架。这避免了所有框架共享同一个初始bias。

### 3.2 Phase 1: 内层优化 —— 框架内战术知识积累

**灵感来源**: SkillOpt的6-stage pipeline骨架 + SkillGrad的诊断/编辑分离 + Trace2Skill的三段式应用

对每个active框架, 执行一个**mini-SkillOpt训练回合**, 但吸收了三个系统的精巧设计:

```
for framework in active_frameworks:
  combined_skill = framework.render()  # domain_theory + derived aspects + tactical_skill
  rejected_buffer = []                 # 被拒绝edits历史 (借鉴SkillOpt step_buffer)
  stagnation_counter = 0              # 停滞计数器 (借鉴SkillGrad remedy_log升级)
  
  for step in range(steps_per_inner_round):
    # 1. ROLLOUT: 在任务子集上用combined_skill执行
    results = rollout(tasks, combined_skill)
    
    # 2. DIAGNOSE (非REFLECT): 诊断与编辑彻底分离 (借鉴SkillGrad)
    #    Diagnoser只产出"发生了什么/为什么/应该怎样", 禁止处方文档改动
    #    失败轨迹: failure diagnosis (因果机制)
    #    对比诊断: 框架A失败但框架B成功的任务 → contrastive analysis
    diagnoses = diagnose_trajectories(results, combined_skill, rejected_buffer)
    
    # 3. PATCH: 独立的patcher角色, 基于diagnoses决定编辑策略
    #    (借鉴SkillGrad的patcher职责分离)
    patches = generate_patches_from_diagnoses(diagnoses, combined_skill)
    
    # 4. AGGREGATE + SELECT: 层次merge + 排序裁剪
    #    (借鉴SkillOpt的hierarchical_merge + rank_and_select)
    merged = hierarchical_merge(patches, failure_priority=True)
    selected = rank_and_select(merged, budget=edit_budget)
    
    # 5. APPLY: 三段式应用 (借鉴Trace2Skill)
    #    Step a: LLM产语义patch (改什么/为什么)  — 已在上步完成
    #    Step b: TRANSLATION精确化 (模糊target → 文件中精确字符串)
    #    Step c: 程序化APPLY (零LLM, 确定性代码执行)
    precise_edits = translate_to_precise(selected, combined_skill)
    candidate = apply_programmatic(framework, precise_edits)  # 只改tactical_skill
    
    # 6. GATE: held-out验证门控 (借鉴SkillOpt, 强制执行)
    score = evaluate(candidate, selection_tasks)
    if score > current_score:
      framework.tactical_skill = candidate.tactical_skill
      framework.score_history.append(score)
      stagnation_counter = 0
    else:
      rejected_buffer.append(selected)  # 失败edits回灌下步prompt
      stagnation_counter += 1
    
    # 7. 停滞升级 (借鉴SkillGrad remedy_log escalation)
    if stagnation_counter >= 3:
      # 局部patch无效 → 触发框架层面mutation信号
      signal_framework_mutation(framework)
      break  # 退出inner loop, 交由outer loop处理
```

**与SkillOpt的关键区别**:
- Protected region不是slow_update guidance, 而是**整个认知框架定义** (domain_theory + 三个derived aspects)
- **诊断与编辑分离**: diagnose阶段禁止处方文档改动, patch阶段才决定结构动作
- **三段式应用**: LLM只负责语义, 精确定位和执行交给确定性代码
- 不需要SkillOpt的slow_update (我们的外层优化承担了这个角色)
- **停滞检测→升级**: 连续reject触发框架层面mutation而非继续微调

**行为描述符提取**: 每次rollout后, 从轨迹中提取行为描述符(behavior descriptor):
- 维度包括: action分布, 工具使用pattern, reasoning深度, 验证频率, 错误类型分布等
- 这些描述符用于QD-Archive的cell定位

### 3.3 Phase 2: 外层进化 —— 认知框架的搜索与进化

**灵感来源**: Arbor的OBSERVE→IDEATE→DECIDE + MLEvolve的evolution/fusion + QD/MAP-Elites

在完成一轮内层优化后, 进入外层进化:

#### Step 2a: OBSERVE —— 跨框架纵向分析 + 对比诊断

借鉴SkillOpt的slow_update + SkillGrad的contrastive diagnosis, 但在框架层面:

```
# 收集所有active框架在SAME任务集上的performance
cross_framework_comparison = []
for fw in active_frameworks:
  results = rollout(longitudinal_tasks, fw.render())
  cross_framework_comparison.append({
    'framework_id': fw.id,
    'score': compute_score(results),
    'behavior_descriptor': extract_behavior_descriptor(results),
    'per_task_results': results,
    'trajectory_patterns': extract_patterns(results),
  })

# 三维分析:
# 1. 框架互补性: 哪些任务在框架A下成功但框架B下失败?
#    → 对比诊断: "框架A的哪个认知特性导致了成功? 这个成功是robust还是fragile?"
#    (直接借鉴SkillGrad contrastive diagnoser的思想, 升级到框架层面)
contrastive_analysis = cross_framework_contrastive(cross_framework_comparison)

# 2. 集体盲区: 哪些任务在所有框架下都失败?
blind_spots = identify_universal_failures(cross_framework_comparison)

# 3. 行为多样性检验: 框架间的行为差异是否真的qualitatively different?
#    如果两个框架的behavior_descriptor过于相近, 标记为"冗余框架"
diversity_check = verify_behavioral_diversity(cross_framework_comparison)
```

#### Step 2b: IDEATE —— 新框架提议

基于观察结果, 通过多种机制产生新框架候选:

**机制1: Gap-driven generation (针对盲区)**
```
# 识别所有框架都失败的任务cluster
blind_spot_tasks = identify_universal_failures(cross_framework_comparison)
# 分析这些任务的共性 → 推断需要什么样的domain theory
# "这些任务的失败模式是什么? 什么样的根本认识能让agent避免这些失败?"
new_theory = llm_reason_about_blind_spots(blind_spot_tasks, existing_frameworks)
new_framework = derive_framework_from_theory(new_theory)
```

**机制2: Evolution (变异现有框架)**
```
# 选择高分框架, 对其domain_theory进行局部变异
# 类似MLEvolve的evolution_agent: 保留核心insight, 改变一个关键aspect
parent_fw = select_parent(archive, method='quality_weighted')
mutated_theory = mutate_domain_theory(parent_fw.domain_theory, 
                                       mutation_focus=identify_weakest_aspect(parent_fw))
new_framework = derive_framework_from_theory(mutated_theory)
new_framework.parent_id = parent_fw.id
```

**机制3: Fusion (融合互补框架)**
```
# 类似MLEvolve的fusion_agent: 取两个框架的优势组合
# 识别在不同任务子集上互补的框架对
fw_a, fw_b = find_complementary_pair(cross_framework_comparison)
fused_theory = synthesize_theories(fw_a.domain_theory, fw_b.domain_theory,
                                    fw_a_strengths, fw_b_strengths)
new_framework = derive_framework_from_theory(fused_theory)
```

**机制4: Adversarial challenge (理论挑战)**
```
# 对每个现有框架的domain_theory提出"为什么这个认识可能是错的"
# 从反面推导新的domain_theory
challenge = adversarial_critique(existing_fw.domain_theory, failure_evidence)
alternative_theory = construct_from_critique(challenge)
```

#### Step 2c: SELECT + ARCHIVE —— 框架入库与淘汰

借鉴MAP-Elites的archive机制:

```
for candidate_fw in new_framework_candidates:
  # 1. 快速评估: 在small task sample上rollout
  quick_results = rollout(quick_eval_tasks, candidate_fw.render())
  score = compute_score(quick_results)
  behavior_desc = extract_behavior_descriptor(quick_results)
  
  # 2. 行为conformance检验 (来自Faithfulness paper的insight)
  # 检查agent的实际行为是否符合框架描述的行为模式
  conformance = check_behavior_conformance(candidate_fw, quick_results)
  if conformance < threshold:
    # 框架被忽略——agent没有真正按照框架描述的方式行事
    log("Framework ignored by agent, discarding")
    continue
  
  # 3. Archive更新: MAP-Elites style
  cell = discretize(behavior_desc)
  if cell not in archive or score > archive[cell].score:
    archive[cell] = candidate_fw  # 该行为niche的新冠军
  
  # 4. Elite pool更新
  update_elite_pool(candidate_fw, score)
```

#### Step 2d: DECIDE —— 下一轮active框架选择

```
# 选择下一轮内层优化的active frameworks
# 策略: 类似MLEvolve的soft switch
if early_phase:
  # 探索: 从archive中选择diverse cells的框架
  active = select_diverse_from_archive(archive, n=active_budget)
elif mid_phase:
  # 混合: elite pool + 新框架候选
  active = elite_pool[:n//2] + select_diverse_from_archive(archive, n=n//2)
else:
  # 利用: 主要集中在top performers
  active = elite_pool[:active_budget]
```

### 3.4 整体训练循环

```
# === COLD START ===
frameworks = cold_start_generate(task_bank.sample(k=cold_start_size))
archive = FrameworkArchive()
for fw in frameworks:
  archive.insert(fw)

# === MAIN TRAINING LOOP ===
for outer_epoch in range(max_outer_epochs):
  
  # 选择本轮active框架
  active_frameworks = decide_active_frameworks(archive, outer_epoch)
  
  # --- INNER LOOP: 每个框架独立的战术优化 ---
  for fw in active_frameworks:
    for inner_step in range(steps_per_inner_round):
      # SkillOpt-style 6-stage pipeline (只修改tactical_skill)
      fw = inner_optimize_step(fw, task_bank)
    
    # 更新archive中的记录
    archive.update(fw)
  
  # --- OUTER LOOP: 框架层面的搜索与进化 ---
  # 2a. Observe: 跨框架纵向对比
  comparison = cross_framework_observe(active_frameworks, longitudinal_tasks)
  
  # 2b. Ideate: 生成新框架候选
  candidates = []
  candidates += gap_driven_generation(comparison)
  candidates += evolution_mutation(archive)
  candidates += fusion_synthesis(comparison)
  candidates += adversarial_challenge(archive)
  
  # 2c. Select + Archive: 快速评估, conformance检验, archive更新
  for cand in candidates:
    evaluate_and_archive(cand, archive)
  
  # 2d. Decide: 停止/继续判断
  if convergence_detected(archive):
    break
  
  # --- EPOCH-LEVEL MEMORY (SkillOpt meta_skill的框架层类比) ---
  if outer_epoch >= 2:
    # 跨epoch的框架优化记忆
    framework_meta_insight = generate_framework_meta_insight(
      prev_epoch_archive, current_archive, comparison
    )
    # 这个insight指导下一轮的IDEATE (类似SkillOpt meta_skill指导optimizer)

# === OUTPUT ===
best_framework = archive.get_best()
# or: ensemble of top-K diverse frameworks
```

### 3.5 关键anti-collapse机制

**问题**: 框架多样性可能在训练过程中坍缩到少数几个

**机制** (基于8个代码库中观察到的实践):

1. **QD-Archive强制覆盖** (核心): 行为特征空间被离散化为cells, 每个cell只保留最佳框架。自动维护行为多样性——即使某区域得分不是全局最高也被保留。

2. **Negative archive当硬约束** (借鉴Arbor `get_constraints_block`): 被retire的框架的domain_theory+失败原因作为"不得重提共享同一隐藏假设的框架"的prompt硬约束, 在prompt层做niche去重。

3. **三级停滞升级** (借鉴Arbor收敛侦测 + MLEvolve Magnitude Tier + SkillGrad remedy_log):
   - **Tier 1** (stagnation_counter < 3): 继续原子tactical edits
   - **Tier 2** (stagnation_counter ∈ [3,5)): tactical_skill整段重写 (对应SkillGrad的结构性重写)
   - **Tier 3** (stagnation_counter ≥ 5): 触发框架层面mutation, 退出inner loop交由outer loop
   - 借鉴MLEvolve原文: "you MUST propose a Tier 2 or Tier 3 change to break the plateau"

4. **框架retirement** (借鉴Arbor的prune + convergence三级stop): 连续多个outer epoch无改进则retire并释放compute。retirement后该框架进入negative archive。

5. **Domain theory diversification enforcement** (借鉴Arbor的Diversity rule): 新框架必须在 `{domain theory核心假设, 关注焦点, 工作结构方式}` 至少一轴与现有所有框架不同。原文: "If two candidates differ only on phrasing or numerical choice, drop one."

6. **Task rotation** (借鉴SkillOpt的epoch-based task ordering): 不同outer epoch使用不同任务子集评估, 防止overfitting。

7. **"当前解作为候选"精英保留** (借鉴POLCA): 内外层每步更新都把"不更新"作为候选之一参与选择, 零成本保证不退化。

### 3.6 收敛控制与预算治理

借鉴Arbor的三独立刹车 + finalization buffer:

```
# 三个独立的停止条件, 任一触发即停:
stop_conditions = [
  max_outer_epochs,                        # 迭代数硬上限
  total_llm_calls > budget,                # 计算预算上限
  consecutive_no_archive_improvement >= 8, # 无改进步数 (Arbor: stop_after=8)
]
# finalization buffer: 停止前留足预算做最终evaluation
if any(stop_conditions) or remaining_budget < finalization_reserve:
  run_final_evaluation(archive.get_best(), held_out_test_set)
```

### 3.7 Compute Scaling 维度

1. **框架覆盖宽度** (Width): archive中active cell的数量 → 更多不同行为策略的覆盖
2. **框架内优化深度** (Depth): 每个框架的inner loop步数 → 每个策略空间内更充分的优化
3. **认知迭代深度** (Iteration): outer epoch数量 → 框架空间搜索的深度
4. **并行度**: 不同框架的inner loop可以完全并行 (类似Arbor的RunExecutorParallel)

---

## 四、与论文叙事的对应

| 论文概念 | 训练机制中的具体实现 |
|---------|-----------------|
| "认知框架"空间搜索 | QD-Archive + 四种框架generation机制 |
| "两层优化" | Inner loop (SkillOpt-style) + Outer loop (框架进化) |
| "domain theory → cognitive framework" | cold_start的四轮分析 + derive_framework_from_theory |
| "三组件模型" (经验+推理+引导) | 经验=rollout trajectories, 推理=LLM synthesis, 引导=我们的framework generation mechanism |
| "行为conformance" | check_behavior_conformance (验证agent是否真的按框架行事) |
| "可投入算力搜索" | 三维scaling: width × depth × iteration |
| "与SkillOpt互补" | 我们的outer loop发现框架, SkillOpt的inner loop在框架内优化 |

---

## 五、来自 claude-worker 深度分析的补充 insight

### Task 1 报告 (SkillOpt + Trace2Skill + SkillGrad) — 引用校验: 34/34 confirmed

### 5.1 SkillGrad 的三个精巧设计 (我们应吸收)

**a) 诊断与patch职责彻底分离**: SkillGrad 的 diagnoser 被明确禁止处方文档改动 ("MUST NOT prescribe changes")，只描述因果机制；patcher 才做结构决策。对比 SkillOpt 中 analyst 同时产出诊断和 edits —— SkillGrad 更干净。
- **对我们的启发**: 内层优化中，"框架层面的观察分析"和"战术层面的skill编辑"应该由不同的角色/prompt完成，避免耦合。

**b) Contrastive diagnosis (对比诊断)**: 利用"基线失败→当前成功"的任务做正样本，不仅提取"什么知识带来成功"，还追问 robust vs fragile (是稳健的能力提升还是偶然成功)。
- **对我们的启发**: 框架评估时，除了看score，还应该做 contrastive analysis —— "在框架A下失败但框架B下成功的任务，是因为框架B的什么认知特性？" + "这个成功是 robust 还是 fragile？"

**c) Remedy_log + 复发升级**: 同一 anchor 反复失败 (appeared_in≥3) 就从原子编辑升级到结构性重写。这是文本空间版的 "局部更新失败则换更大步长/换方向"。
- **对我们的启发**: 如果一个框架在inner loop中连续多步无进展，不应继续微调tactical_skill，而应触发框架层面的mutation (对应SkillGrad的"升级到结构性重写")。

### 5.2 Trace2Skill 的工程精巧 (我们应借鉴)

**三段式应用**: "LLM提语义patch → TRANSLATION精确化 → 程序化APPLY(零LLM)"。在我们的inner loop中，应该考虑类似的分离 —— LLM 只负责"改什么/为什么"，精确定位和应用交给确定性代码。

### 5.3 三者共同局限的精准归纳 (我们的核心差异化)

claude-worker 的横向对比确认了我们方案的核心突破方向：
1. **所有现有工作的搜索都退化为单链贪心爬山** — 我们的 QD-Archive + 四种框架generation机制是第一个在文本策略空间做真正population-based search的
2. **性能级反退化普遍薄弱** — 我们应在所有层面(inner fast update + inner gate + outer framework evaluation)都有held-out验证
3. **过拟合训练失败池** — 我们的task rotation + diverse task sampling + held-out泛化验证直接回应
4. **新旧冲突仲裁弱** — 我们的跨框架纵向对比提供了更丰富的仲裁信息

### 5.4 应吸收的10个可借鉴精巧设计清单

| # | 设计 | 来源 | 在我们方案中的位置 |
|---|------|------|-----------------|
| 1 | SGD完整概念映射 (可插拔各阶段) | SkillOpt | Inner loop的整体骨架 |
| 2 | 诊断与编辑职责分离 | SkillGrad | Inner loop的reflect阶段 |
| 3 | 三段式应用 (语义→精确化→程序化) | Trace2Skill | Inner loop的update阶段 |
| 4 | 分层并行merge (失败优先) | SkillOpt+Trace2Skill | Inner loop的aggregate阶段 |
| 5 | 双时间尺度+物理隔离 | SkillOpt | 框架定义(protected) vs tactical skill |
| 6 | Pattern memory + 复发升级 | SkillGrad | 框架停滞检测→触发mutation |
| 7 | Held-out gate + rejected buffer | SkillOpt | Inner loop gate + outer archive gate |
| 8 | 结构帽 + distill比 + 禁append-only | SkillGrad+Trace2Skill | Skill document的体量控制 |
| 9 | 对比诊断 + 纵向对照 | SkillGrad+SkillOpt | 跨框架comparison |
| 10 | 优化器隐喻隔离不进prompt | SkillGrad | 工程纪律 |

### Task 2 报告 (Arbor + MLEvolve + POLCA + textgrad + Trace) — 引用校验: 82/82 confirmed

#### 5.5 两条技术路线的清晰划分

Task 2 揭示了搜索/进化方向的两条主路线：
- **种群/树搜索路线 (Arbor/MLEvolve)**: 维护显式搜索树或分支种群，有真正的 explore-exploit 调度
- **文本梯度/单点搜索路线 (POLCA/textgrad/Trace)**: LLM 驱动的单点爬山，局部利用强、全局探索弱

我们的方案本质是 **"外层用种群路线做全局探索 + 内层用梯度路线做局部精修"**。

#### 5.6 从搜索视角补充的精巧设计 (我们应吸收)

**a) Negative archive 当硬约束 (Arbor `get_constraints_block`)**: 把已剪枝失败教训作为 "不得重提共享同一隐藏假设" 的 prompt 硬约束。在 prompt 层做 niche 去重，省大量重复评估。
- **对我们的启发**: 外层进化中，被 retire 的框架的 domain_theory + 失败原因应作为"负面archive"注入新框架生成的prompt，防止重蹈覆辙。

**b) "当前解作为候选之一"的零成本精英保留 (POLCA `basic_algorithm.py:363`)**: `candidates.append((current_score, backup))` 再 max —— 无需额外机制就保证搜索不退化。
- **对我们的启发**: 内层和外层的每一步更新，都应把"不更新"作为候选之一参与选择。已在 Phase 1 的 GATE 步骤中体现。

**c) Arbor 收敛侦测三级升级**: warn(3步无改进) → paradigm_shift(5步,强制换 approach family) → stop(8步)。`_find_exhausted_parents` 追踪每个父节点的子节点耗尽情况。
- **对我们的启发**: 外层进化应有类似的三级刹车: 轻度提醒(3轮) → 强制框架变异方向(5轮) → 终止并输出当前最优(8轮)。

**d) MLEvolve 的 Magnitude-Based Tier 自适应步长**: 平时强制 single atomic change 保可归因；停滞时切到 Tier2/3 强制大改 ("you MUST propose a Tier 2 or Tier 3 change to break the plateau")。
- **对我们的启发**: Phase 1 已有的停滞升级(stagnation_counter≥3 → signal mutation)可进一步细化为：Tier 1 = 继续原子tactical edits; Tier 2 = tactical_skill整段重写; Tier 3 = 触发框架mutation。

**e) bypassing 标志解耦"提案"与"落盘" (POLCA)**: `step(bypassing=True)` 只返回提案不改参数，使"提 N 个候选 → 逐个临时评分 → 还原 → 选最优"的循环极其干净。
- **对我们的启发**: 框架候选评估时，应该先"虚拟套用"打分再决定是否提交，而非每个候选都真正落盘。

**f) Arbor 的 Hamming 探针 + 反 probe-disconnect**: 强制先判断"瓶颈消除后benchmark是否会动" → 再提案，且提案必须因果对齐已观测瓶颈。
- **对我们的启发**: 新框架生成时，应先验证"如果这个认知缺陷被修复，任务成功率预期提升多少？" 避免生成与实际瓶颈不相关的框架。

#### 5.7 10 个 repo 共同确认的核心空白 (我们的最终差异化论点)

claude-worker 对 **全部 8 个代码库** (Task 1 的 SkillOpt/SkillGrad/Trace2Skill + Task 2 的 Arbor/MLEvolve/POLCA/textgrad/Trace) 的横向对比，从两个正交视角（"局部优化"视角 + "全局搜索"视角）独立确认了同一个核心空白：

> **所有现有工作都缺乏真正的质量-多样性 archive 与新颖性度量。**

- 局部优化类 (SkillOpt/SkillGrad/Trace2Skill): 搜索退化为单链贪心
- 树搜索类 (Arbor/MLEvolve): 有分支/niche但无按行为描述子分桶
- 梯度类 (POLCA/textgrad/Trace): 本质单点，完全无种群多样性维护

**这是我们 QD-Archive + 行为描述子 + 新颖性驱动探索方案的最强差异化论据。**

#### 5.8 更新后的完整可借鉴设计清单 (16项)

| # | 设计 | 来源 | 在我们方案中的位置 |
|---|------|------|-----------------|
| 1 | SGD完整概念映射 | SkillOpt | Inner loop骨架 |
| 2 | 诊断与编辑职责分离 | SkillGrad+textgrad | Inner loop的diagnose→patch |
| 3 | 三段式应用 (语义→精确化→程序化) | Trace2Skill | Inner loop的update |
| 4 | 分层并行merge (失败优先) | SkillOpt+Trace2Skill | Inner loop的aggregate |
| 5 | 双时间尺度+物理隔离 | SkillOpt | 框架定义(protected) vs tactical skill |
| 6 | Pattern memory + 复发升级 | SkillGrad | 停滞→三级Tier升级 |
| 7 | Held-out gate + rejected buffer | SkillOpt | Inner gate + outer archive gate |
| 8 | 结构帽 + distill比 | SkillGrad+Trace2Skill | Skill document体量控制 |
| 9 | 对比诊断 + 纵向对照 | SkillGrad+SkillOpt | 跨框架comparison |
| 10 | Negative archive当硬约束 | Arbor | 外层新框架生成的constraints |
| 11 | "当前解作为候选"精英保留 | POLCA/Trace | 内外层每步更新 |
| 12 | 收敛侦测三级升级 | Arbor | 外层训练循环的收敛控制 |
| 13 | Magnitude-Based Tier自适应步长 | MLEvolve | 停滞时步长放大 |
| 14 | bypassing解耦提案与落盘 | POLCA | 框架候选虚拟评估 |
| 15 | 结构化generation moves + 多样性硬规则 | Arbor+MLEvolve | 框架generation的四种机制 |
| 16 | 三独立刹车 + finalization buffer | Arbor | 外层训练循环的预算治理 |

---

## 六、实现优先级建议

### P0 (核心MVP):
1. CognitiveFramework数据结构 + render()
2. Cold start: 四轮分析 → 初始框架种群
3. Inner loop: 简化版SkillOpt (rollout → reflect → update, 保护框架region)
4. Outer loop: gap-driven generation + evolution mutation
5. 简单的elite pool (不需要完整QD-Archive)

### P1 (完整系统):
6. QD-Archive with behavior descriptors
7. Fusion mechanism
8. Behavior conformance checking
9. 框架meta insight (跨epoch记忆)
10. Compute parallelism

### P2 (进阶):
11. Adversarial theory challenge
12. Adaptive compute allocation (给有前途的框架更多inner steps)
13. Framework ensemble/combination for final deployment
