# CSS：认知策略搜索——在文本空间中搜索冻结权重 Agent 的最优 Skill Document

## 完整方案机制设计文档（最终版）

---

## 1. 系统概述

### 1.1 核心贡献

提出**认知策略搜索（Cognitive Strategy Search, CSS）**框架：一个双层 skill document 优化系统，外层在认知策略空间（HOW to think）中搜索质的不同的思维方法，内层在每个认知策略下做 SkillOpt 式的战术规则优化（WHAT to do）。突破现有系统（如 SkillOpt）被困在单一认知策略下做局部优化的 scaling 天花板。

### 1.2 LLM-Code 分工原则

贯穿所有设计的基本原则：

```
LLM 负责（语义综合——不可替代）：
  - 从轨迹中观察和描述认知过程特征
  - 识别跨轨迹的共性认知模式
  - 根因归因（为什么 agent 这样思考）
  - 策略推导（根因 → 策略改变的逻辑推论）

Code 负责（约束验证——LLM 不可靠）：
  - 统计验证（模式出现率、趋势检测、显著性检验）
  - 物理隔离（L0/L1 文件分离）
  - 输出格式 gate（结构合规检查）
  - 操作调度（EXPLOITATION/REFINE/PROPOSAL 切换）
  - 适应度 gate（best-score accept/reject）
  - 评估公平性（同期比较、held-out 管理）
```

来源：v5 调研「第一定律」——措辞强硬 ≠ load-bearing，真正 load-bearing 的引导只有代码架构。

---

## 2. Skill Document 结构

### 2.1 物理分离的文件组织

```
skill_document/
  strategy.md   ← L1 认知策略（HOW to think）
  rules.md      ← L0 战术规则（WHAT to do）
  metadata.json  ← 学习曲线、模式库、负档案等元数据
```

### 2.2 strategy.md 格式

```markdown
## 策略名称
[简洁命名，如"假设验证驱动的迭代执行"]

## 策略正文

### 1. [认知过程维度名称]
[Level 2-3 粒度的具体认知过程描述]
[描述 METHOD 而非 GOAL]
[包含 WHEN 切换判据]

### 2. [认知过程维度名称]
...

### 3. [认知过程维度名称]
...
```

策略正文要求：
- 组织为多个 `###` 编号子段，每个描述一个认知过程维度
- Level 2-3 粒度：具体认知过程（非口号级 Level 4，非战术规则级 Level 1）
- 描述方法而非目标
- 包含决策逻辑：什么条件下切换到哪种行为

### 2.3 rules.md 格式

自由格式 markdown 文本。不强制编号结构——LLM 自由决定规则的表达形式（段落、列表、条件判断均可），以最大化利用轨迹经验。

### 2.4 多消费者视图

```
Task Agent 看到的：
  strategy.md（只读参考）+ rules.md（只读参考）

L0 Optimizer（EXPLOITATION）看到的：
  strategy.md（只读背景，不可修改）
  rules.md（可编辑：增/改/删）
  轨迹数据 + step_buffer

L1 Optimizer（REFINE）看到的：
  strategy.md（可修改 1-2 个 ### 子段）
  rules.md（可改/删冲突规则，不可新增）
  Layer 1-4 分析产出 + 元数据

L1 Optimizer（PROPOSAL）看到的：
  strategy.md（全部重写）
  rules.md（语义判断：兼容的保留，不兼容的删除）
  Layer 1-5 分析产出 + 元数据 + 负档案
```

---

## 3. 搜索拓扑：树状分支

### 3.1 树节点定义

```
TreeNode = {
  strategy:        L1 认知策略文本（strategy.md）
  rules:           当前积累的 L0 战术规则（rules.md）
  step_buffer:     L0 优化历史（accept/reject + 失败模式 + 被拒 edit）
  val_score:       最近验证集评估分数
  pattern_records: 该策略下发现的认知模式库
  parent:          父节点引用
  branch_type:     "PROPOSAL" | "REFINE" | "ROOT"
  refine_count:    从该节点尝试 REFINE 的次数
  status:          "active" | "pruned" | "saturated"
}
```

### 3.2 三操作与分支关系

```
EXPLOITATION（不分支）：在当前节点内做 L0 优化
  → 默认操作，每个 epoch 都做

REFINE 分支：创建子节点（策略局部修改）
  触发：L0 饱和 + persistent_fail 存在 + refine_count < K

PROPOSAL 分支：创建子节点（全新策略）
  触发：L0 饱和 + persistent_fail 存在 + REFINE exhausted（refine_count ≥ K）
```

### 3.3 树管理核心信号

认知策略的真实质量通过 L0 内层优化的学习曲线间接暴露：

```
好策略：L0 accept rate 持续较高（规则持续有效积累）
差策略：L0 accept rate 快速降低（edit 持续被 reject）
```

此信号同时驱动：SELECT（投资哪个节点）、BRANCH（何时分支）、PRUNE（何时剪枝）。

---

## 4. 三操作详细设计

### 4.1 EXPLOITATION（L0 优化）

每个 epoch 的默认操作。借鉴 SkillOpt 核心机制，适配双层架构。

**数据消费原则**：
- 完整轨迹直接给 L0 优化器，不做有损截断
- 唯一截断：单个工具调用返回结果 ≥ 8K tokens → 截断（保留首尾摘要）
- 全局 context 阈值 = 可配参数（默认 256K）× 80%
- 无 Epoch-Level Review（Layer 3 + L1 已覆盖跨 epoch 视角）

**Epoch 内的 L0 Step 流程**：

```
Epoch 开始：rollout 全部训练任务 → 收集轨迹
将轨迹划分为 minibatch（M 条/batch，失败/成功分开）

每个 L0 Step：
  (1) Per-minibatch LLM 分析 → raw patches（edit 提议）
  (2) 层级聚合（hierarchical merge）→ merged edit set
  (3) 选择/排序 → top edits
  (4) 应用 edit 到 rules.md → candidate rules
      edit 操作类型（借鉴 SkillOpt skill.py:48-108）：
        append / insert_after / replace / delete
  (5) Selection set rollout → candidate_score
  (6) Accept/reject gate（借鉴 SkillOpt gate.py:31-73）：
      candidate_score > current_score → accept
      candidate_score > best_score → accept_new_best
      否则 → reject
  (7) 更新 step_buffer：
      {step, action, score_before, score_after, failure_patterns, rejected_edits}

跑到饱和（连续 N reject）或 epoch 轨迹充分利用。
即使 mid-epoch 触发饱和也跑完本 epoch（保证 Layer 1-3 拿到完整数据）。
```

**饱和检测**（Step 粒度）：
```
连续 N 个 step 被 reject（N 初始值 5）→ L0 饱和
饱和 + Layer 3 报告 persistent_fail 存在 → 触发 REFINE 或 PROPOSAL
```

### 4.2 REFINE（策略局部修改）⚠️ 需实验重点观察

REFINE 在三操作中扮演中间层角色：比 EXPLOITATION 大（改策略组件），比 PROPOSAL 小（不换整个策略）。

> **实验观察标记**：REFINE 的修改幅度控制、与 PROPOSAL 的边界、升级阈值 K 是当前最不确定的机制点，必须通过运行实验深入观察分析。

**与 PROPOSAL 的管道共享**：
```
Layer 1-4：完全共享（持续运行的分析管道）
Layer 5：分叉——同一份诊断结果的两种响应幅度
```

**REFINE Layer 5**：
```
输入：Layer 4 归因 → 指认的问题策略组件（1-2 个 ### 子段）
操作：
  strategy.md：只重写被指认的子段，其余保持不变
  rules.md：清理冲突规则（可修改/删除，不可新增）
验证：5b 回溯验证 + 负档案检查 + 5c rollout

Code gate（diff 检查）：
  (a) 必须至少修改 1 个 ### 子段（否则拒绝空操作）
  (b) 未修改子段必须与父节点完全相同（否则升级为 PROPOSAL）
```

**知识继承**：
```
策略：父节点局部修改版
L0 规则：全部继承 + REFINE 清理冲突部分
模式库：继承（追踪序列延续）
学习曲线：从 0 重新开始
```

**终止条件**：
```
从同一父节点尝试 REFINE ≥ K 次（K=3），且所有 REFINE 子节点学习曲线
未显著优于父节点 → "REFINE-exhausted" → 升级触发 PROPOSAL
```

### 4.3 PROPOSAL（全新策略推导）——五层轨迹驱动递进分析

PROPOSAL 不是「LLM 创造性生成」，而是「轨迹数据层层分析、L1 信号自然浮现、策略改变是分析的逻辑推论」。

#### Layer 1：开放式认知过程分析（Per-Trajectory）

**关键设计**：不预设认知分析维度。结构化输出格式，但不预设分析内容。

```
LLM 引导：
  聚焦「agent 如何思考」而非「做了什么」
  用 LLM 自己的语言命名观察到的认知方面（cognitive_aspect）
  每个观察必须引用轨迹原文作证据
  区分 critical（对结果有显著影响）vs notable（值得注意）

产出格式：
  observations: [
    { what, cognitive_aspect（LLM 自命名）, evidence, consequence, significance }
    ...（数量不固定）
  ]

Contrastive pairs（同任务成功/失败配对）单独分析：
  聚焦「导致不同结果的认知过程差异」
  产出：divergence_point + cognitive_difference + is_systematic
```

#### Layer 2：跨轨迹认知模式聚类

```
Step 2a（Code）：embedding 预分组
  对每个 observation 的文本做 embedding（Qwen3-Embedding-0.6B）
  DBSCAN 聚类（eps 自适应，min_samples=2）
  单位是 observation 不是 trajectory

Step 2b（LLM）：逐簇精炼
  统一命名 / 拆分 / 合并

Step 2c（LLM）：跨簇操作
  失败模式 vs 成功模式配对 → 「对偶模式」
  成功模式直接提示「应该怎么做」→ Layer 5 策略推导的关键输入

增量更新：
  新 epoch 的 observations 通过 embedding 检索匹配已有模式
  匹配不上 → 创建新模式 / 合并旧模式
  模式库增量成长，不每次推倒重来
```

#### Layer 3：纵向追踪 + L0/L1 信号分离

**核心创新**：用 L0 优化历史客观分离 L1 信号。

```
对每个认知模式，跨 epoch 追踪出现率：
  模式在 L0 优化后消退 → L0 问题（规则层面可解决）
  模式在多轮 L0 后仍顽固存在 → L1 信号（需要改思维方式）

L1 信号判定（Code 计算）：
  (a) 近 W 个 step 出现率无显著下降趋势
  (b) L0 已饱和（连续 N step reject = remedy_resistance 证据）
  (c) 仍影响较大比例的任务

模式跨 epoch 匹配：
  给每个模式分配稳定 pattern_id
  新 epoch observations 优先 embedding 检索 + LLM 判断匹配已有模式
  定期全局审查：独立创建的模式是否相同 → 合并
```

#### Layer 4：根因归因

```
四层逐级追问（LLM，每层 Code gate 要求引用证据）：

  第一层（行为归因）：L1 模式中 agent 具体做了什么？纯事实汇总
  第二层（过程归因）：为什么这样做？从 THOUGHT 原文中找原因
  第三层（策略归因）：策略的什么设计导致了这个过程？指向策略具体段落
  第四层（假设归因）：策略背后的隐藏假设是什么？打破后什么被解锁？

跨模式归因：多个 L1 模式指向同一策略层面根因 → 高杠杆点
L0 失败解释：引用 remedy_history 说明为什么 L0 规则修不好这个问题
```

#### Layer 5：策略推导 + 回溯验证

```
5a 策略推导（LLM）：
  关键词「推导」非「生成」——策略改变是根因的逻辑推论
  根因 → 需要改变的策略组件 → 改变方向 → 具体机制
  与对偶成功模式配合：成功轨迹中 agent 偶然展现的正确行为
    = 新策略应该系统化的行为
  与负档案对比：embedding 召回 top-K 相似废弃方向 → LLM 说明差异

5b 回溯验证（rollout 前的廉价验证）：
  检查 1：成功轨迹中有没有正面证据
  检查 2：失败轨迹的反事实推演
  检查 3：覆盖率估计（能解决多大比例 persistent_fail）
  覆盖率 < 20% → 重新考虑根因 / 覆盖率 > 30% → 进入 rollout

5c 快速 rollout 验证（Code 主导）：
  在 persistent_fail 任务子集上针对性验证
  核心指标：目标 L1 模式出现率是否显著下降
  通过 → 创建新树节点
  不通过 → 进负档案，试下一个根因
```

**PROPOSAL 的知识继承**：
```
策略：全新
L0 规则：L1 optimizer 语义判断——兼容的保留，不兼容的删除
模式库：不继承
学习曲线：从 0 开始
```

---

## 5. 树管理

### 5.1 SELECT：UCB1 变体

```
SELECT(node) = val_score + α × accept_slope + β × sqrt(ln(T) / n_i)

val_score     = 最近验证集评估分数（归一化到 [0,1]）
accept_slope  = 近 W 个 step 的 accept rate 线性趋势
T             = 全局已投资总 step 数
n_i           = 该节点已投资 step 数
α, β          = 超参数
```

并发模式下，SELECT_BATCH 选 top-K 节点并行投资。

### 5.2 PRUNE：Paired Bootstrap

```
条件（全部满足）：
  (a) 已投资 ≥ min_steps（初始值 10）
  (b) L0 已饱和
  (c) val_score 显著低于同父兄弟最佳节点

显著性检验：
  在验证集上 paired bootstrap，resample 1000 次
  score 差值 95% CI 下界 > 0 → 剪枝

剪枝后：
  状态 → "pruned"，不再投资
  轨迹和模式库保留（供 Layer 5b 回溯检索）
```

### 5.3 负档案（Negative Archive）

策略搜索树的元辅助信息，防止盲目重复已证伪的方向。

```
条目 = {
  strategy_snapshot, origin, root_cause, failure_evidence, created_at
}

写入：PRUNE / PROPOSAL 失败 / REFINE 失败
读取：PROPOSAL/REFINE Layer 5 → embedding 召回 top-K → LLM 判断
原则：提醒不是禁止——能说清差异则继续，不能则阻止
```

---

## 6. 冷启动

```
Phase 0a：裸跑 rollout
  无 strategy/rules，80 tasks × K=3 → baseline_score + trajectories_0

Phase 0b：Layer 1-3 初始分析 → 初始模式库

Phase 0c：首次 PROPOSAL（Layer 4-5）
  归因到 LLM 裸跑的系统性认知弱点 → strategy_0.md
  rules_0.md = 空

Phase 0d：创建根节点
```

---

## 7. 完整系统流程（并发轮次制）

```
while not terminated:

  ── SCHEDULE ──
  SELECT_BATCH：选 top-K 个 active 节点并行投资
  K = min(active_node_count, concurrency_limit)

  ── PARALLEL EXECUTE（K 个节点各自独立）──
  每个节点并行执行一个完整 epoch：

    ① Epoch Rollout：80 tasks × K=3 → epoch_trajectories
    
    ② L0 EXPLOITATION：
       minibatch 分析 → 层级聚合 → edit → 评估 → accept/reject
       多 step，跑到饱和或 epoch 结束（即使 mid-epoch 饱和也跑完）
    
    ③ Layer 1-3 分析（完整 epoch 数据）
    
    ④ 验证集评估（周期性）

  ── SYNC POINT ──
  
    ⑤ 全局更新：global_best + PRUNE 检查
    
    ⑥ 饱和节点分支（可并行）：
       REFINE 或 PROPOSAL → 新节点 / 负档案
    
    ⑦ 回到 SCHEDULE

终止：所有 active 节点 saturated 且无新 PROPOSAL 可尝试 / 手动停止
无 max_total_steps 预算上限——充分运行以观察机制效果。
```

---

## 8. 技术选型

### 8.1 Embedding

```
模型：Qwen3-Embedding-0.6B
  参数量 0.6B | 维度 1024 | MRL 支持 32~1024
  C-MTEB 聚类 68.74（远超 BGE-M3 ~55）
  中英双语强 | CPU 可运行 | sentence-transformers 兼容
  
备选升级：Qwen3-Embedding-4B（C-MTEB 聚类 77.89）
```

### 8.2 向量索引

```
faiss-cpu (IndexFlatIP)
  规模 ~300-5000 向量 → 精确搜索足够
  L2 normalize + 内积 = cosine similarity
```

### 8.3 聚类

```
DBSCAN / HDBSCAN
  eps 自适应（k-distance plot 拐点）
  min_samples = 2（捕获低频模式）
```

---

## 9. 数据使用

```
训练集 80 tasks：主循环——收集轨迹、L0 优化、模式分析
验证集 40 tasks：树节点评估——公平比较不同策略
测试集 20 tasks：最终报告，从不参与优化

每任务 K=3 次 rollout：
  (a) 减少 LLM 随机性（多数投票）
  (b) 产生天然 contrastive pairs
  (c) 提供模式出现率可信度

节点间公平比较：同期比较（同 step 数下的验证集表现）
Held-out 轮换：每 T epoch 重新划分 train/val，防过拟合
```

---

## 10. 全局参数表

| 参数 | 含义 | 初始值 | 取值依据 |
|------|------|--------|---------|
| N | 连续 reject 饱和阈值 | 5 | SkillOpt 类系统收敛速度 |
| W | accept_rate 趋势窗口 | 2N=10 | 覆盖两倍饱和长度 |
| K | REFINE→PROPOSAL 升级 | 3 | ≈ 策略子段数 |
| min_steps | PRUNE 最小投资 | 10 | ≈ 2 epoch step 数 |
| α | SELECT 趋势权重 | 待调 | pilot grid search |
| β | SELECT 探索权重 | 待调 | pilot grid search |
| context_cap | 全局 context 上限 | 256K × 80% | LLM context window |
| tool_trunc | 工具结果截断阈值 | 8K | 仅截工具调用返回 |
| eps_dbscan | DBSCAN 距离阈值 | 自适应 | k-distance plot |
| min_samples | DBSCAN 最小簇大小 | 2 | 捕获低频模式 |

所有初始值基于合理推断，正式值通过 pilot 实验校准。

---

## 11. Ablation & Baselines（待展开）

```
核心 Ablation：
  A1 去 L1 层        A2 去树结构      A3 简化 PROPOSAL
  A4 去信号分离      A5 去负档案      A6 预设维度 Layer 1

Baselines：
  B1 SkillOpt 原版   B2 SkillOpt 加时  B3 裸跑 LLM   B4 人工 skill

优先级：必做 A1,A3,B1,B2,B3 | 应做 A2,A4,A6 | 可选 A5,B4
```

---

## 附录 A：操作权限矩阵

| | strategy.md | rules.md |
|---|---|---|
| EXPLOITATION | 只读背景 | 增/改/删（自由 edit） |
| REFINE | 改 1-2 个 ### 子段 | 改/删冲突规则（不可新增） |
| PROPOSAL | 全部重写 | 语义判断保留/删除 |

## 附录 B：设计文档演进

| 版本 | 核心内容 | 定位 |
|------|---------|------|
| v2 | 原始 8 库分析记录 | 调研 |
| v3 | 统一设计（被识别为"丢失 Level 1"） | 概念 |
| v4 | 双层两生成器框架 | 概念框架 |
| v5 | 8 库调研结论 + 引导机制 + 轨迹处理 + 第一定律 | 调研与初步设计 |
| v6 | 协商确定的完整方案（D0-D16） | 方案设计 |
| 本文档 | 面向实施的最终版整合 | 实施参考 |
