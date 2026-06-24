# 训练机制 v6：完整方案设计（Grill-Me 协商确定）

> v5 = 8 库调研结论（引导机制 + 轨迹处理 + 第一定律 + 迁移风险）。
> v6 = 基于 v5 调研，通过逐点协商确定的完整方案机制设计。
> 每个设计决策附协商过程的关键推理和否决理由。

---

## D0. 论文定位

**核心 contribution**：

提出一个**双层 skill document 搜索框架**：外层在认知策略空间（HOW to think）中搜索质的不同的思维方法，内层在每个认知策略下做 SkillOpt 式的战术规则优化（WHAT to do）。突破 SkillOpt 等系统被困在单一认知策略下做局部优化的 scaling 天花板。

**LLM-Code 分工原则**（贯穿所有设计）：

v5 调研的「第一定律」（措辞强硬 ≠ load-bearing）被进一步修正——第一定律说的是**约束/控制** LLM 时 prompt 不可靠需要 code gate，但**不意味着**应该用 code 替代 LLM 做语义理解和模式综合。

```
LLM 负责（语义综合——不可替代）：
  - 从轨迹中观察和描述认知过程特征
  - 识别跨轨迹的共性认知模式
  - 根因归因（为什么 agent 这样思考）
  - 策略推导（根因 → 策略改变的逻辑推论）

Code 负责（约束验证——LLM 不可靠）：
  - 统计验证（模式出现率、趋势检测、显著性检验）
  - 物理隔离（L0/L1 PROTECTED 区）
  - 输出格式 gate（结构合规检查）
  - 操作调度（PROPOSAL/REFINE/EXPLOITATION 切换）
  - 适应度 gate（best-score accept/reject）
  - 评估公平性（同期比较、held-out 管理）
```

**否决记录**：曾提出「code-enforced 跨轨迹模式聚合器」作为核心 contribution，被修正——code 聚类（embedding/DBSCAN）抓不住深层认知共性，LLM 的语义综合能力在此处不可替代。Code 的角色是验证 LLM 声称的模式是否有统计支撑，不是替代 LLM 做模式发现。

---

## D1. 搜索拓扑：树状分支

**选择**：C. 树状分支搜索

**否决 A（种群并行）**：K × 内层成本，研究实验预算难承受。

**否决 B（单线顺序）**：不支持回溯；如果 REFINE 走错方向无法返回父节点换方向尝试；论文表述也弱于树结构。

**选择 C 的条件**：必须让树状搜索**真正有效**而非只引入术语。有效性的核心设计见 D2（L0 学习曲线驱动树管理）。

---

## D2. 树节点定义 + 管理机制

### 节点定义

```
TreeNode = {
  strategy: L1 认知策略文本,
  l0_rules: 当前积累的 L0 战术规则集,
  maturity: 已投资的 epoch 数,
  learning_curve: [(epoch, train_score, val_score, pattern_stats)],
  pattern_records: 该策略下发现的认知模式库,
  parent: 父节点引用,
  branch_type: "PROPOSAL" | "REFINE" | "ROOT",
  status: "active" | "pruned" | "saturated"
}
```

### 分支触发

```
EXPLOITATION（不分支）：在当前节点内做 L0 优化
  → 默认操作，每个 epoch 都做

REFINE 分支：创建相邻子节点（策略局部修改）
  触发：L0 学习曲线趋平 + 存在 L1 信号
        + 该节点此前未做过 REFINE（或 REFINE 仍有改善空间）

PROPOSAL 分支：创建质变子节点（新认知策略）
  触发：L0 学习曲线趋平 + 存在 L1 信号
        + REFINE 已尝试 ≥K 次无显著改善（remedy_resistance）
  可从当前节点或任意祖先节点分支
```

### 树管理核心信号：L0 学习曲线

**关键 insight**：认知策略的真实质量通过 L0 内层优化的学习曲线间接暴露。

```
好策略：L0 学习曲线持续上升（规则持续有效积累）
差策略：L0 学习曲线快速趋平（persistent_fail 不消退）
```

**此信号同时驱动三个决策**：

| 决策 | 信号使用方式 |
|------|-------------|
| SELECT（投资哪个节点）| 优先投资学习曲线仍在上升的节点 |
| BRANCH（何时分支）| 学习曲线趋平 + persistent_fail 不消退 = L0 饱和 |
| PRUNE（何时剪枝）| 兄弟节点在**同 epoch 数**下比较，显著更差且已趋平 → 剪枝 |

**选择策略**：

```
节点优先级 = f(当前分数, 学习曲线斜率, 探索奖励)
  当前分数    = 最近 epoch 的 held-out 通过率
  学习曲线斜率 = 近 W epoch 的线性回归斜率（反映潜力）
  探索奖励    = 已投资 epoch 数的递减函数
```

**此信号天然是 code-enforceable**——数值序列的趋势检测、兄弟比较、饱和判定都是确定性计算。

---

## D3. 冷启动机制

**原则**：初始化不是特殊逻辑，而是 PROPOSAL 在「空白状态」下的自然运行。

```
阶段 0（裸跑暖身）：
  Agent 在无认知策略下执行一批种子任务
  目的：(a) 建立基线通过率
       (b) 暴露 LLM 裸跑时的系统性认知弱点
       (c) 产生第一批轨迹供 PROPOSAL 消费

阶段 1（首次 PROPOSAL）：
  输入 = 阶段 0 的轨迹（无先前策略、无负档案）
  走标准 PROPOSAL 流程
  产出 = 第一个认知策略 → 树的根节点

阶段 2（正常主循环）：
  根节点开始 EXPLOITATION → 触发 REFINE/PROPOSAL → 树生长
```

---

## D4. PROPOSAL 机制：五层轨迹驱动递进分析

**核心理念**：PROPOSAL 不是「LLM 创造性生成」，而是「轨迹数据层层分析、L1 信号自然浮现、策略改变是分析的逻辑推论」。

### Layer 1：开放式认知过程分析（Per-Trajectory）

**关键设计决策**：**不预设认知分析维度**。

**否决预设维度方案**：曾设计 5 个预设维度（分解/信息处理/验证/执行策略/元认知），被否决。理由：
- 系统的整个意义是发现未知的认知策略，预设维度把搜索空间钳死在已知框架内
- 和批评 SkillGrad 的 `operation|workflow` 标签是同一个问题——人工先验限制发现
- 正确做法：**结构化输出格式，但不预设分析内容**

**机制**：

```
分析 LLM 的引导：
  聚焦在「agent 如何思考」而非「做了什么」
  用 LLM 自己的语言命名观察到的认知方面
  每个观察必须引用轨迹原文作证据
  观察数量不限——如实报告，不多不少
  区分 critical（对结果有显著影响）vs notable（值得注意但不确定）

产出格式：
  observations: [
    { what, cognitive_aspect（LLM 自命名）, evidence, consequence, significance }
    ...（数量不固定）
  ]

contrastive pairs（同任务成功/失败配对）单独分析：
  聚焦「导致不同结果的认知过程差异」
  产出：divergence_point + cognitive_difference + is_systematic 判断
```

### Layer 2：跨轨迹认知模式聚类

```
Step 2a（Code）：Per-observation embedding + DBSCAN 预分组
  单位是 observation 不是 trajectory（一条轨迹贡献多个 observation）
  
Step 2b（LLM）：逐簇精炼——这些观察描述的是同一种认知行为吗？
  统一命名 / 拆分不同模式 / 合并相似模式
  
Step 2c（LLM）：跨簇操作
  - 失败模式 vs 成功模式配对 → 「对偶模式」
    （同一认知方面的两端：失败时缺少 vs 成功时具备）
  - 对偶模式对 Layer 5 策略推导极有价值：
    成功模式直接提示了「应该怎么做」

增量更新：新 epoch 轨迹通过 embedding 检索匹配已有模式
         匹配不上的 → 创建新模式 / 合并旧模式
         模式库增量成长，不每次推倒重来
```

### Layer 3：纵向追踪 + L0/L1 信号分离

**核心机制**：用 L0 优化历史客观分离 L1 信号——现有系统都没做过。

```
对每个认知模式，跨 epoch 追踪出现率：
  模式在 L0 优化后消退 → L0 问题（加规则能解决）
  模式在多轮 L0 优化后仍顽固存在 → L1 信号（需要改思维方式）

L1 判定三信号（Code 计算）：
  (a) 近 W epoch 出现率无显著下降趋势
  (b) remedy_resistance ≥ K（已尝试 K 次不同 L0 规则均未持续改善）
  (c) 仍影响较大比例的任务

模式跨 epoch 匹配：
  给每个模式分配稳定 pattern_id
  新 epoch 的 observations 优先 embedding 检索 + LLM 判断匹配已有模式
  定期全局审查：独立创建的模式是否其实相同 → 合并
```

### Layer 4：根因归因

```
四层逐级追问（LLM，每层有 Code gate 要求引用证据）：

  第一层（行为归因）：L1 模式中 agent 具体做了什么？纯事实汇总
  第二层（过程归因）：为什么 agent 这样做？从 THOUGHT 原文中找原因
  第三层（策略归因）：策略的什么设计导致了这个过程？指向策略具体段落
  第四层（假设归因）：策略背后的隐藏假设是什么？打破后什么被解锁？

跨模式归因：多个 L1 模式是否指向同一策略层面根因 → 高杠杆点
L0 失败解释：引用 remedy_history 说明为什么 L0 规则修不好这个问题
```

### Layer 5：策略推导 + 回溯验证

```
5a 策略推导（LLM）：
  关键词是「推导」非「生成」——策略改变是根因的逻辑推论
  根因 → 需要改变的策略组件 → 改变方向 → 具体机制
  与对偶成功模式配合：成功轨迹中 agent 偶然展现的正确行为
    = 新策略应该系统化的行为
  与负档案对比：如类似方向已废弃 → 说明差异

5b 回溯验证（rollout 前的廉价验证）：
  检查 1：成功轨迹中有没有正面证据（agent 偶然使用类似方法且成功）
  检查 2：失败轨迹的反事实推演（关键决策点换方法会怎样）
  检查 3：覆盖率估计（能解决多大比例的 persistent_fail）
  覆盖率 < 20% → 重新考虑根因 / 覆盖率 > 30% → 进入 rollout

5c 快速 rollout 验证（Code 主导）：
  在 persistent_fail 任务子集上针对性验证
  核心指标：目标 L1 模式出现率是否显著下降（不是总通过率）
  通过 → 创建新树节点 / 不通过 → 进负档案，试下一个根因
```

---

## D5. 数据使用机制

```
数据划分：
  训练集 (80 tasks)：主循环——收集轨迹、L0 优化、模式分析
  验证集 (40 tasks)：树节点评估——公平比较不同策略
  [可选] 测试集 (20 tasks)：论文报告，从不参与优化

每任务 K=3 次 rollout：
  (a) 减少 LLM 随机性噪声（多数投票）
  (b) 产生天然 contrastive pairs（同任务不同结果 = 最有价值的分析素材）
  (c) 提供模式出现率的可信度

一个完整 epoch 的数据流：
  Rollout → Layer 1 标注 → Layer 2 模式库更新 → Layer 3 纵向追踪
  → EXPLOITATION(L0 优化) → 验证集评估(每 M epoch)
  → 饱和检测 → 如饱和: Layer 4-5 → REFINE/PROPOSAL

节点间公平比较：同期比较（同 epoch 数下的验证集表现）
Held-out 轮换：每 T epoch 重新随机划分训练/验证集，防过拟合
```

---

## D6. 认知策略文本格式

### 策略文档结构

```markdown
## 策略名称
[简洁命名，如"假设验证驱动的迭代执行"]

## 策略正文

[Level 2-3 粒度的具体认知过程描述]
[描述 METHOD 而非 GOAL]
[包含 WHEN 切换判据——什么条件下切换到哪种行为]

示例片段：

面对一个新任务时：

1. 理解阶段——先搞清楚在做什么
   不要急于动手。花 20% 的时间分析任务结构：
   - 输入输出的格式约束是什么
   - 有没有隐含的边界条件
   - 这个任务和之前做过的哪些任务结构相似

   如果任务描述模糊：先执行一个最小探测（只用最简单的输入跑一遍），
   从输出推断实际要求，再规划后续步骤。

2. 执行阶段——每一步都是一个可验证的假设
   ...
```

### 两消费者视图（物理分离文件）

```
task agent 看到的（strategy.md + rules.md）：
  strategy.md → 策略正文（只读，agent 执行时参考）
  rules.md    → L0 战术规则（只读，agent 执行时参考）

L0 optimizer（EXPLOITATION）看到的：
  strategy.md → 策略正文（只读 context，不可修改）
  rules.md    → L0 规则（可编辑：增/改/删）
  trajectories → 完整轨迹数据
  step_buffer → 前序 step 的 accept/reject 记录 + 失败模式 + 被拒 edit

L1 optimizer（REFINE）看到的：
  strategy.md → 策略正文（可修改 1-2 个 ### 子段）
  rules.md    → L0 规则（可改/删与策略修改冲突的规则，不可新增）
  metadata.json → 学习曲线、模式库、负档案
  Layer 1-4 分析产出

L1 optimizer（PROPOSAL）看到的：
  strategy.md → 全新策略（重写）
  rules.md    → 语义判断继承（兼容的保留，不兼容的删除）
  metadata.json → 全量元数据 + 负档案
  Layer 1-5 分析产出
```

---

## D7. EXPLOITATION 机制（L0 优化）

### 数据消费原则：不做有损截断

```
完整轨迹直接给 L0 优化器。
唯一截断处理：单个工具调用返回结果 >= 8K tokens → 截断（保留首尾摘要）
全局 context 阈值 = 可配参数（默认 256K）× 80%
超过阈值时：从最老的轨迹开始裁减（优先保留当前 epoch 轨迹）
无 Epoch-Level Review（Layer 3 + L1 已覆盖跨 epoch 视角）
```

### rules.md 格式：自由文本 + SkillOpt 式 edit

```
rules.md = 自由格式 markdown 文本（不强制编号结构）
  LLM 自由决定规则的表达形式——段落、列表、条件判断均可
  最大化利用轨迹经验，不做不必要的格式限制

L0 optimizer 输出 edit 列表（借鉴 SkillOpt skill.py:48-108）：
  [{op: "append", content: "..."},
   {op: "insert_after", target: "...", content: "..."},
   {op: "replace", target: "...", content: "..."},
   {op: "delete", target: "..."},
   ...]
  Code 逐条顺序应用 → candidate rules.md

与 SkillOpt 的适配差异：
  SkillOpt = 一个文件 + SLOW_UPDATE 标记保护区
  我们 = strategy.md（物理隔离只读）+ rules.md（L0 可编辑）
  → 更干净，不需要标记解析
```

### 每个 L0 Step 的完整流程

```
(1) 构造 optimizer prompt：
    [只读背景] strategy.md
    [编辑目标] rules.md
    [轨迹] 完整轨迹（仅截工具结果 >=8K）
    [历史] step_buffer（前序 step 的 accept/reject + 失败模式 + 被拒 edit）

(2) LLM 产出 edit 列表
    不限格式、不限数量——LLM 根据轨迹分析自由决定

(3) Code 应用 edit → candidate rules.md

(4) Rollout 评估：selection set（训练集子集）上跑 candidate skill → candidate_score

(5) Accept/reject gate（借鉴 SkillOpt gate.py:31-73）：
    candidate_score > current_score → accept，更新 rules.md
    candidate_score > best_score → accept_new_best
    否则 → reject，保留原 rules.md

(6) 更新 step_buffer：
    {step, action, score_before, score_after, failure_patterns, rejected_edits}
    → 传入下一个 step 的 optimizer prompt，避免重复尝试已失败的 edit
```

### 饱和检测：Step 粒度（非 Epoch）

```
step_buffer 跟踪每个 step 的 accept/reject
连续 N 个 step 被 reject（N 初始值 5，可调）→ L0 饱和

饱和 + Layer 3 报告 persistent_fail 模式存在 → 触发 REFINE
REFINE exhausted → 触发 PROPOSAL

两层评估粒度并存：
  Step 级（高频）：每个 edit 的 accept/reject → 驱动饱和检测
  Epoch 级（低频）：周期性全验证集评估 → 驱动节点间比较（SELECT/PRUNE）
```

---

## D8. Embedding 技术选型

### 使用场景

```
场景 1（Layer 2a）：对认知 observations 做 DBSCAN 聚类
场景 2（Layer 2 增量）：新 epoch observations 匹配已有模式库
场景 3（Layer 5b）：回溯检索——找展现特定认知行为的轨迹
```

### 选型决策

```
首选模型：Qwen3-Embedding-0.6B
  参数量：0.6B | 维度：1024（MRL 支持 32~1024 灵活调整）
  C-MTEB 聚类：68.74（远超 BGE-M3 的 ~55 和 gte-Qwen2-1.5B 的 54.61）
  多语言均值：64.33（MMTEB）
  context：32K tokens
  显存：fp16 ~1.2 GB，CPU 可运行
  指令感知：支持 query/document 双 prompt
  sentence-transformers 兼容

备选升级：Qwen3-Embedding-4B（如 0.6B 聚类质量不足时）
  C-MTEB 聚类：77.89 | 维度：2560 | 需 GPU ~8GB

否决 BGE-M3：
  Dense+Sparse+ColBERT 三模优势在短文本聚类场景价值不大
  聚类分数（~55）显著低于 Qwen3 系列
  我们不需要大规模文档检索的 sparse retrieval

否决 API 嵌入（OpenAI/Cohere）：
  研究系统需可复现性
  embedding 调用频繁（每 epoch 数百次），成本累积
  不必要的外部依赖
```

### 向量索引选型

```
选择：faiss-cpu (IndexFlatIP)
  规模 ~300-5000 向量 → 精确搜索足够
  L2 normalize + 内积 = cosine similarity
  MLEvolve 已实证，API 稳定

否决 ChromaDB：嵌入式数据库对我们规模是 overkill
否决 BM25 混合：认知描述的语义相似不在词汇层面
  "缺少中间验证" ≈ "没有检查执行中的输出"，词汇重叠低但语义高度相关
```

### DBSCAN 参数策略

```
eps：不预设固定值，根据实际 embedding 分布自适应
  方法：k-distance plot 找拐点（k=min_samples）
  或用 HDBSCAN（自动选择密度层级）
min_samples=2：允许低频模式被捕获，让 LLM 在 Step 2b 做精炼过滤
```

---

## D10. REFINE 机制 ⚠️ 需实验重点观察

> **实验观察标记**：REFINE 的具体设计效果（修改幅度控制、与 PROPOSAL 的边界、升级阈值 K）
> 是当前最不确定的机制点，必须通过运行实验深入观察分析，根据实验数据迭代调整。

### 与 PROPOSAL 的管道共享

```
Layer 1-4：完全共享（持续运行的分析管道，不区分操作类型）
Layer 5：分叉——REFINE 和 PROPOSAL 是同一份诊断结果的两种响应幅度

REFINE-5（局部修改）：
  输入：Layer 4 四层归因 → 指认的问题策略组件（1-2 个 ### 子段）
  操作：只重写被指认的子段，其余子段保持不变
  产出：修改后的策略文本（结构相同，局部不同）

PROPOSAL-5（全局重写）：
  （已有设计 D4，5a推导 + 5b验证 + 5c rollout）
```

### 修改幅度控制：基于策略文本 ### 子段结构

```
策略正文要求组织为多个 ### 编号子段，每个描述一个认知过程维度：

## 策略正文
### 1. 任务理解方法
  ...
### 2. 执行过程中的验证机制
  ...
### 3. 错误发现后的应对
  ...

REFINE 的修改单元 = 1-2 个 ### 子段
PROPOSAL 的修改单元 = 整个策略正文

Code gate（diff 检查）：
  (a) REFINE 必须至少修改 1 个 ### 子段 → 否则拒绝（空操作）
  (b) 未修改的子段内容必须与父节点完全相同 → 否则要求修正或升级为 PROPOSAL
  → 结构化 markdown diff，确定性可执行，不依赖 LLM 判断幅度
```

### 知识继承 + REFINE 对 rules.md 的清理权限

```
REFINE 分支时：
  strategy.md：父节点的局部修改版（1-2 个 ### 子段不同）
  rules.md：继承父节点，但 REFINE 可以清理冲突规则
    → 可修改：与策略修改产生冲突的规则
    → 可删除：策略修改后不再适用或不恰当的规则
    → 不可新增：新规则只由后续 L0 EXPLOITATION 产出
    （原则：REFINE 负责「策略变了，旧规则要跟着调整」的清理工作，
     不负责「基于新策略发现新规则」的增长工作）
  模式库：继承（模式定义不变，追踪序列延续）
  学习曲线：从 0 重新开始

PROPOSAL 分支时：
  strategy.md：全新策略
  rules.md：L1 optimizer 做语义判断——兼容的保留，不兼容的删除
  模式库：不继承（新策略下可能产生完全不同的认知模式）
  学习曲线：从 0 开始
```

### 验证与终止

```
验证：同 PROPOSAL 的 5b-5c（回溯验证 + persistent_fail 子集 rollout）
  核心指标：被修改子段所关联的 L1 模式出现率是否下降

终止条件（Code 判定）：
  从同一父节点尝试 REFINE ≥K 次（K 待实验确定，初始值 3）
  且所有 REFINE 子节点学习曲线未显著优于父节点
  → 标记 "REFINE-exhausted" → 升级触发 PROPOSAL
```

---

## D11. 树管理：SELECT / PRUNE

### Epoch 边界规则

```
Epoch 是最小操作单元：
  (1) rollout 全部训练任务 → 收集完整轨迹
  (2) 在本 epoch 轨迹上跑多个 L0 step（edit → 评估 → accept/reject）
  (3) 即使 mid-epoch 连续 N reject 触发饱和条件，也跑完本 epoch
  (4) epoch 结束时：
      - 运行 Layer 1-3 分析（完整 epoch 数据）
      - 检查饱和状态 → 决定继续 EXPLOITATION 还是触发 REFINE/PROPOSAL

好处：
  Layer 1-3 总拿到完整 epoch 数据（不出现半 epoch 脏数据）
  饱和后跑完的 step 成本可控（edit 被 reject = rules.md 不变）
  REFINE/PROPOSAL 在 epoch 边界启动，数据充分
```

### SELECT 优先级函数：UCB1 变体

```
SELECT(node) = val_score + α × accept_slope + β × sqrt(ln(T) / n_i)

val_score     = 最近一次验证集评估分数（归一化到 [0,1]）
accept_slope  = 近 W 个 step 的 accept rate 线性趋势
                （正 = 还在学习，0 = 趋平，负 = 退化）
T             = 全局已投资的总 step 数
n_i           = 该节点已投资的 step 数
α, β          = 超参数

选择 accept_rate 趋势而非 score 趋势的理由：
  score 变化在早/晚期量级差别大，跨阶段不可比
  accept_rate 天然归一化（0~1），直接复用 step_buffer 数据
  0.8 = 活跃学习中，0.1 = 接近饱和，语义直观

探索项 sqrt(ln(T)/n_i) 的意义：
  投资少的节点获得探索加成，避免过早放弃
  经典 UCB1 形式，理论性质好（论文可引用 bandit 文献）
```

### PRUNE 显著性检验：Paired Bootstrap

```
PRUNE 条件（全部满足）：
  (a) 该节点已投资 ≥ min_steps（初始值 10，避免过早判死）
  (b) 该节点 L0 已饱和（连续 N step reject）
  (c) 该节点 val_score 显著低于同父兄弟中的最佳节点

显著性检验方法：paired bootstrap
  在验证集上，对同一批任务比较两个节点的 pass/fail
  resample 1000 次，计算 score 差值的 95% CI
  CI 下界 > 0（最佳兄弟显著更好）→ 剪枝

选择 paired bootstrap 的理由：
  vs McNemar：McNemar 只看 pass/fail 翻转，丢失分数幅度信息
  vs paired-t：假设正态分布，task pass/fail 是二值的不满足
  bootstrap：非参数，对小样本稳健，直接在实际分布上估计 CI

剪枝后：
  节点状态 → "pruned"，不再投资
  轨迹和模式库保留（供其他节点 Layer 5b 回溯检索参考）
  不占 SELECT 名额
```

---

## D12. 负档案（Negative Archive）

负档案是策略搜索树的元辅助信息，记录被证伪的策略尝试，防止盲目重复、提高探索效率。

### 数据结构

```
负档案条目 = {
  strategy_snapshot:  被废弃的策略文本（或 REFINE 修改的 diff）
  origin:            "pruned_node" | "proposal_failed_rollout" | "refine_failed_rollout"
  root_cause:        Layer 4 归因摘要（为什么尝试这个方向）
  failure_evidence:  为什么失败——rollout 数据 / 学习曲线 / 比较结果
  created_at:        epoch + step 时间戳
}
```

### 写入时机

```
PRUNE 剪枝时   → 被剪节点的策略进负档案
PROPOSAL 5c 失败 → 候选策略进负档案
REFINE 5c 失败  → 修改方案进负档案
```

### 读取方式：Embedding 召回 + LLM 判断

```
在 PROPOSAL/REFINE 的 Layer 5 阶段：
  (1) 对新策略文本做 embedding（复用 Qwen3-Embedding-0.6B）
  (2) 与负档案条目的 embedding 做 cosine similarity
  (3) 召回 top-K 最相似条目（K=3~5）
  (4) 注入 Layer 5 prompt，LLM 判断新方向与废弃方向的关系

关键约束：提醒不是禁止
  负档案相似 ≠ 新策略一定不行
  可能根因不同、时机不同、L0 规则基础不同
  LLM 必须解释「与废弃方向的具体差异在哪」
  无法说清差异 → 不通过 5b 验证
  能说清差异 → 继续进入 5c rollout
```

---

## D13. Layer 3 统计阈值 + 全局参数表

```
参数          含义                       初始值    取值依据
N             连续 reject 饱和阈值        5        经验值，SkillOpt 类系统收敛速度
W             accept_rate 趋势窗口        2N=10    覆盖两倍饱和长度
K             REFINE→PROPOSAL 升级        3        ≈ 策略子段数（3-5 段）
K_remedy      → 不单独设，L0 饱和本身即 remedy_resistance 证据
min_steps     PRUNE 最小投资              10       ≈ 2 epoch 的 step 数
α             SELECT 趋势权重             待调      pilot grid search
β             SELECT 探索权重             待调      pilot grid search
eps_dbscan    DBSCAN 距离阈值             自适应    k-distance plot 拐点
min_samples   DBSCAN 最小簇大小           2        捕获低频模式
context_cap   全局 context 上限           256K     × 80% 为实际阈值
tool_trunc    工具结果截断阈值            8K       仅截工具调用返回

所有初始值基于合理推断，正式值通过 pilot 实验校准。
论文报告：初始值 + 最终值 + 敏感性分析。
```

---

## D14. 完整系统流程（CSS: Cognitive Strategy Search）

### Phase 0：冷启动（D3）

```
(0a) 裸跑 rollout：无 strategy/rules，80 tasks × K=3 → baseline_score + trajectories_0
(0b) Layer 1-3 初始分析 → 初始模式库
(0c) 首次 PROPOSAL（Layer 4-5）→ strategy_0.md，rules_0.md = 空
(0d) 创建根节点 TreeNode(strategy_0, rules_0, "ROOT")
```

### Phase 1：主循环（并发轮次制）

```
while not terminated:

  ──── SCHEDULE ────
  ① SELECT_BATCH：选 top-K 个 active 节点并行投资
     K = min(active_node_count, concurrency_limit)
     concurrency_limit = API 并发度 / 单节点 epoch 所需并发

  ──── PARALLEL EXECUTE（K 个节点各自独立）────
  每个节点 n 并行执行一个完整 epoch：

    ② EPOCH ROLLOUT：80 tasks × K=3 rollout → epoch_trajectories
    
    ③ L0 EXPLOITATION（D7）：
       将 240 条轨迹划分为 minibatch（M 条/batch）
       ┌─ 每个 L0 step ─────────────────────────────┐
       │ per-minibatch LLM 分析（失败/成功分开）→ raw patches │
       │ 层级聚合 → merged edit set → 选择 top edits      │
       │ 应用 edit → candidate rules                     │
       │ selection set rollout → accept/reject            │
       │ 更新 step_buffer                                │
       └─────────────────────────────────────────────┘
       跑到饱和（连续 N reject）或 epoch 轨迹充分利用

    ④ LAYER 1-3 分析（完整 epoch 数据）

    ⑤ 验证集评估（周期性）：val 集 40 tasks → 更新 val_score

  ──── SYNC POINT（所有节点 epoch 完成后）────
  
  ⑥ 全局更新：
     更新 global_best
     PRUNE 检查（D11，兄弟节点 paired bootstrap 比较）
  
  ⑦ 饱和节点的分支决策（可并行，各自独立）：
     对每个 saturated + persistent_fail 的节点：
       refine_count < K → REFINE（D10）
       REFINE exhausted → PROPOSAL（D4）
       → 新节点加入树 / 失败进负档案
  
  ⑧ 回到 ①

终止条件：
  (a) 所有 active 节点均 saturated 且无新 PROPOSAL 可尝试
  (b) 研究者手动停止
  注意：无 max_total_steps 预算上限——充分运行以观察机制效果
```

### Phase 2：报告

```
best_skill_document 在 test 集（20 tasks）上的最终评估
搜索过程可视化：树结构 + 每节点学习曲线 + 并发时间线
ablation 对比
```

### 并发设计说明

```
完全独立（可并行）：
  不同节点的 epoch rollout / L0 step / Layer 1-3 / REFINE / PROPOSAL

共享状态（低冲突）：
  负档案 → 追加写，无冲突
  全局 best_score → sync point 更新
  验证集 → 只读，可并行 rollout

需要同步（sync point）：
  PRUNE 比较 → 需要兄弟节点 val_score 都已更新
  树结构变更 → 新节点创建/剪枝
  SELECT_BATCH → 需要全局节点状态

加速效果：K 个节点并行 ≈ 搜索速度 ~K 倍
```

---

## D15. Ablation & Baselines（方向已定，待实验阶段展开）

```
核心 Ablation：
  A1 去 L1 层（固定策略只做 L0）    → 验证双层优化价值
  A2 去树结构（线性序列替代）        → 验证搜索拓扑贡献
  A3 简化 PROPOSAL（单步生成替代五层）→ 验证分析管道价值
  A4 去信号分离（不追踪 remedy_resistance）→ 验证 L0/L1 分离
  A5 去负档案                       → 验证防退化机制
  A6 预设维度替代涌现式 Layer 1      → 验证开放式分析

Baselines：
  B1 SkillOpt 原版
  B2 SkillOpt 加时版（等量 API 调用预算）
  B3 裸跑 LLM（无 skill document）
  B4 人工/一次性生成的 skill document

优先级：必做 A1,A3,B1,B2,B3 | 应做 A2,A4,A6 | 可选 A5,B4
统计：3 random seeds × paired bootstrap 检验
```

---

## D16. 待实施阶段处理

- [ ] **Layer 1 prompt 精细设计**：实施时逐步打磨
- [ ] **评估前沿移动**：观察实验效果后决定是否需要
- [ ] **frozen LLM 天花板判别**：运行全流程后根据数据分析
- [ ] **多 benchmark 泛化**：spreadsheetbench 验证后再考虑

---

## 附录：v4→v5→v6 演进脉络

| 版本 | 核心内容 | 定位 |
|------|---------|------|
| v4 | 双层两生成器框架（G0+G1, L0+L1） | 概念框架 |
| v5 | 8 库调研结论（7+3 引导模式 / 5 范式 / 第一定律 / S0-S7 管道 / 迁移风险） | 调研与初步设计 |
| v6 | 协商确定的完整方案（树搜索 / 五层 PROPOSAL / 涌现式分析 / LLM-Code 分工 / 数据使用） | 方案设计 |

v5 的调研结论（§1 引导机制 / §2 轨迹处理 / 附录代码引用）仍然有效，v6 引用但不重复。
