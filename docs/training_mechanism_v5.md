# 训练机制 v5：引导流水线 + 轨迹经验处理 + PROPOSAL/REFINE 完整设计

> v4 建立了双层两生成器框架（Level 1 认知框架 + Level 0 战术规则）。
> v5 聚焦 v4 未解决的三个核心设计问题：
> 1. G1 的引导流水线怎么设计？（v4 只有"四个 Move"的骨架）
> 2. 轨迹经验如何处理为 L1 可用的信号？（v4 留白）
> 3. PROPOSAL/REFINE/EXPLOITATION 三操作的完整机制（v4 缺 REFINE）
>
> 基于 8 库代码实证调研（Arbor / MLEvolve / POLCA / SkillGrad / textgrad / SkillOpt / Trace / Trace2Skill）。

---

## 0. v4 → v5 的关键修正

### 0.1 因果链纠正

v4 将经验、LLM 推理、引导机制作为三个并列输入。**用户纠正**：它们是一条因果链：

```
[3] 引导机制（我们设计的）
    → 驱动 [2] LLM 的通用推理能力（frozen engine）
    → 去挖掘 [1] 累积的轨迹经验（迭代增长的数据）
    → 产出认知策略
```

引导机制是核心设计对象，不是三个并列输入之一。

### 0.2 REFINE 操作补全

v4 只有 PROPOSAL（新帧）和 EXPLOITATION（内层优化），缺少 **REFINE**——对现有认知策略做局部调整。REFINE 的意义：
- 让认知策略空间从离散的 O(10) 个帧变为**可做局部搜索的连续流形**
- 填补 PROPOSAL（质变跳转）和 EXPLOITATION（规则级微调）之间的真空
- 是策略改进的主力操作——大多数时候不需要换整个帧，只需调整某个环节

### 0.3 引导流水线从骨架到完整设计

v4 的 G1 只有"四个 Move"的抽象描述。v5 基于代码实证调研，给出完整的多阶段引导流水线。

---

## 1. 引导机制调研结论

### 1.1 调研范围与方法

逐行读取 8 个代码库的实际 prompt 文本和代码逻辑（非 README），提取引导机制的设计模式。

### 1.2 各系统的引导机制核心

**Arbor（idea_drafting.md）— 最成熟的认知层引导**：

三层强制结构：
- §1 Mindset 层级锁定："HOW not HOW MUCH" / "10× not 10%" / "Mechanism is a noun"
- §2 First-Principles Probe：4 问必答（瓶颈 CLASS / 隐藏假设 / Elephant in Room / Hamming），每问要证据
- §3 Four Moves：假设反转 / 成功倒推 / 类比迁移 / 失败反向工程，Diversity Rule 强制正交
- §5 Per-Candidate Declaration：5 字段（assumption / mechanism class / hypothesis chain / orthogonality / conflicts）
- §6 Self-Check Kill：能用数字/旋钮表达？只是 prompt 改写？"more X"？描述目标非机制？→ kill
- §8 Anti-Pattern：Probe 指向 X 但方案做了 Y（因 Y 更容易）→ probe-disconnected，kill

**MLEvolve（draft/evolution/improve agents）— 实用的 Magnitude Tier 升级**：

- Tier 1→2→3 系统，由停滞检测触发（success_patience≥2 OR total_patience≥5）
- 停滞时强制 Tier 2/3（"You MUST propose a Tier 2 or Tier 3 change"）— 真正 load-bearing 的是 C3 状态计数器路由
- 结构化推理：WHY（root cause + evidence）→ HOW（mechanism）→ WHAT（keep unchanged）
- Branch Evolution History 注入：完整进化轨迹供学习
- Memory 作为去重："distinctly different from existing attempts"
- ⚠️ **高估修正**：module-enum 表面上限制 module 选择，实际越界只 warning 照用（`base_planner.py:184-187`），非真强制

**SkillGrad（diagnoser/momentum/patcher）— 角色分离 + 模式驱动**：

- 诊断器/修补器严格分离：诊断器禁止开药方，修补器禁止重新诊断
- 泛化标签："Do not use task-specific values in the LABEL"
- "Iterate by pattern, not by task"——聚类后统一修补
- 成功/失败证据激活不同推理风格（workflow 用频率驱动，operation 用因果追踪）
- textgrad 补充：backward engine "only strategies, not new versions"；动量检测触发步长放大

**POLCA/OptoPrime — 最简引导**：

- 零方法论引导："change variables to improve output"
- 多候选差异化弥补引导不足：temperature / self-refinement / iterative alternatives / multi-experts
- 四种生成技术提供结构性多样性

### 1.3 提炼的 7 个引导设计模式

按因果链位置排列：[3]引导 → [2]LLM → [1]经验 → 策略

| # | 模式 | 核心机制 | 出处 | 有效性判据 |
|---|------|---------|------|-----------|
| P1 | **层级锁定 + 排斥过滤** | 明确判据界定合格产出层级；kill 不合格候选 | Arbor §1+§6, SkillGrad L2 约束 | SkillGrad rewrite-from-scratch 仍产 L0 = 无锁定则无 L1 |
| P2 | **强制诊断流水线** | 结构化分析步骤 + 证据要求，防跳过分析 | Arbor §2 (4问Probe), SkillGrad diagnoser | vague=fail; 禁具体值促泛化 |
| P3 | **角色分离** | 诊断/生成由不同角色完成，禁止跨层 | SkillGrad diagnoser/patcher, textgrad backward/optimizer | 信息被迫在更高抽象层流动 |
| P4 | **多 Move 发散 + 正交性强制** | 多种生成方法 + Diversity Rule | Arbor §3, OptoPrime 4 techniques | 候选必须在≥1维不同 |
| P5 | **负档案 + 显式 Counter** | 历史废弃记录 + 新候选必须显式克服废弃原因 | Arbor constraints block | 防止 O(10) 策略空间循环 |
| P6 | **停滞→强制升级** | 停滞检测触发更大步长的改变 | MLEvolve Tier 1→2→3 | success_patience≥2 时禁 Tier 1 |
| P7 | **模式驱动迭代** | 按模式聚类再处理，非逐任务处理 | SkillGrad patcher §3, textgrad momentum | 避免碎片化；同类重复→加大步长 |

### 1.4 第一定律：措辞强硬 ≠ load-bearing（claude-worker 交叉验证）

claude-worker 对 8 库做了「装饰猎手」红队逐处源码复核，核心结论：

**所有华丽强措辞引导均为装饰**——NOVELTY/DIVERSITY、WHAT-WHY-HOW、KEEP-UNCHANGED、kill 过滤器、LoadSkill 收据、Forbidden actions、L2/L3 判据——**零代码强制**。铁证：SkillGrad `pipeline/patcher.py` docstring 作者自陈 `"every patch is accepted"`。

**真正 load-bearing 的引导只有 6 类代码架构**：

| # | 类型 | 机制 | 典型出处 |
|---|------|------|---------|
| C1 | 结构化切分 | 可变/不可变区域物理隔离 | SkillOpt protected region |
| C2 | 输出契约+解析 gate | 格式错误→crash/retry，不是建议 | SkillGrad tag parsing |
| C3 | 状态计数器路由 | patience 等计数器决定走哪条 prompt 路径 | MLEvolve Tier 升级 |
| C4 | 代码构造输入视图 | 预分桶/预聚类，LLM 只看处理后视图 | SkillOpt 四象限构造 |
| C5 | 数值/选择 clamp | 硬上下界、枚举白名单 | MLEvolve branch limit |
| C6 | 外部可证伪 gate + retry | rollout 验证→不通过→回退 | 所有带 held-out 的系统 |

**修正两处常见高估**：
- MLEvolve module-enum：越界只 warning 照用（`base_planner.py:184-187`），非真强制
- SkillOpt 保护区：用脆弱 `skill.find()` substring matching（`skill.py:18-28`），可被格式变化击穿

**借鉴原则 = 借架构不借文案**。我们设计的引导流水线（§3-§4）中每一步都必须回答：「这一步是 C1-C6 中哪类代码架构在执行？」如果答不出，该步就是装饰。

### 1.5 遗漏的 3 个引导模式

| # | 模式 | 核心机制 | 何时 load-bearing |
|---|------|---------|------------------|
| P8 | **Persona / 多专家 fan-out** | 多角色独立生成+投票 | 仅 OptoPrime multi-expert 形态（真并行）为 load-bearing；单 prompt 说「你是专家」是装饰 |
| P9 | **Few-shot worked example + 锚 tag** | 示例锚定输出格式+思维路径 | 仅当耦合 C2 解析器时 load-bearing |
| P10 | **输入侧 token 预算 / 轨迹截断** | 硬截断/head-tail 窗口控制输入 | 本身就是 C4 的实例，always load-bearing |

### 1.6 迁移风险清单（从 code 领域到 text skill document 领域）

我们的载体是自由自然语言 skill document，而 8 库调研的所有 load-bearing 机制都作用于可确定性解析执行的代码对象。迁移面临的根本风险：

| # | 风险 | 具体问题 | 对策方向 |
|---|------|---------|---------|
| R1 | **载体退化** | NL 可解析结构但不可解析语义；C2 只保格式不保内容 | 语法代理硬 gate（结构合规）+ LLM-judge 软复核（语义合规）双层 |
| R2 | **frozen LLM 天花板** | 所有引导效果受底座元认知能力封顶 | mask 消融实验：分离「编排效果」vs「底座效果」|
| R3 | **「改了 HOW」无法确定性 gate** | L1/L0 分界是语义判断，不存在 substring-find 式硬判 | 语法代理（结构差异度量）+ LLM-judge（认知层级判断）组合 |
| R4 | **agent 任务高方差** | 同一 skill 多次 rollout 分数剧烈波动 | 多 seed rollout + 显著性检验（POLCA trick）|
| R5 | **validation-overfitting** | 固定 held-out 上显式搜索→过拟合 | held-out pool 轮换 + 独立 test set |
| R6 | **level collapse 检测依赖 LLM** | 「这是 L0 还是 L1」本身是主观语义判断 | frequency×cognitive-level×remedy_resistance 多信号投票，不依赖单次 LLM 判断 |

### 1.7 v4 的缺失与 v5 的补全

| 模式 | v4 状态 | v5 设计 |
|------|---------|---------|
| P1 层级锁定 | **缺失**（最致命） | 入口锁定 + 出口 kill 规则 + REFINE 版锁定 |
| P2 强制诊断 | 仅瓶颈分类器（简单） | 完整 4 问 Probe + 角色分离 |
| P3 角色分离 | **缺失** | 诊断器 vs 策略生成器分离 |
| P4 多 Move 发散 | 有（四个 Move） | 保留，加正交性强制 + 证据追溯 |
| P5 负档案 | 帧坐标去重（概念级） | 废弃策略 + 原因 + 显式 counter 要求 |
| P6 停滞升级 | **缺失** | REFINE 停滞→自动切 PROPOSAL |
| P7 模式驱动 | **缺失** | 经验按模式聚类后输入 |

---

## 2. 轨迹经验处理调研结论（含 claude-worker 交叉验证）

### 2.0 核心发现

**真·跨轨迹聚合在 5 库里几乎不存在——这是全语料最大的空白，也是我们外层 L1 认知策略搜索必须补的承重层。** 没有任何一个系统做过完整的「跨轨迹聚类 → 复发计数 → 泛化 gate」链。聚合原语已散落各系统（MLEvolve RRF+FAISS / OptoPrime AgglomerativeClustering+medoid / SkillGrad default-to-merge 判据 / SkillOpt 四象限+PROTECTED 隔离），缺的只是组装。

三个层级化事实：
1. **L1 容器 ≠ L1 搜索**：SkillOpt `meta_skill` 是唯一 code-real 的 L1 容器，但它是单线 last-write-wins（只读 epoch-1、每轮 LLM 整体改写、无种群/搜索/并行评估）
2. **物理隔离 > token 标签**：SkillOpt 用独立文件 + PROTECTED fenced 区 + 内层 prompt 显式禁改外层 = code-real；SkillGrad 仅用 `operation|workflow` token 标签、无代码 gate = decoration
3. **L1 浮现是待验证假设**：「L1 信号从跨轨迹模式浮现」本身是 5 库都没实现、未经实证的假设——必须当头号风险设计+实测

### 2.1 5 种轨迹处理范式（代码实证 + 交叉验证修正）

**范式 1：散装喂轨迹（POLCA/OptoPrime）**

`optoprime.py:407-426` — FIFOBuffer 直接拼历史 (variables, feedback) 对到 prompt。零结构化，默认 `memory_size=0` 关闭。多候选共识用 AgglomerativeClustering+medoid（即用即弃，不跨步累积）。对 L1 无效。

**范式 2：四象限纵向对比（SkillOpt slow_update）**

`slow_update.py:152-215` — `build_comparison_pairs()` 将同一任务在前后 epoch 的结果分四类：improved / regressed（HIGHEST PRIORITY）/ persistent_fail / stable_success。

⚠️ **交叉验证修正**：四象限**不是「L1 提取器」**——它只是 per-task 1:1 跨版本结果分类器。按 task_id 1:1 join，象限内**不做任何跨任务聚类/复发统计**（persistent_fail 只是一堆独立 task 标签的集合，代码从不计「有几条轨迹共享同一失败模式」），且产物在 `slow_update.md:43-49` 被明文降维成 target 规则。

仍有价值的设计点：
- persistent_fail 作为 L1 信号**入口**（但需补跨任务聚类才能升为 L1 信号）
- regressed 暴露策略改变的副作用 → REFINE/回滚信号
- ⚠️ **漏项补充**：`trainer.py:770-829` 的 **best-score accept/reject gate** + `trainer.py:441-480` 的 **step_buffer 负向记忆**（带真实 `score_before→after` 的 rejected_edits）——这是语料中**唯一 code-real 的适应度 gate + 优化轨迹自反馈**，比四象限更适合做 L1 适应度评估模板

**范式 3：模式记录 + 递归生长 + 补丁日志（SkillGrad momentum）**

`prompts/momentum.py` — Pattern Record Schema：
```
### <pattern-id-slug> | <kind: operation|workflow|mixed> | <one-line>
- anchor: <kebab-case slug>
- appeared_in: iter_2, iter_3, iter_5
- description: <随证据增长的散文>
- latest_executor_action: <当前最佳处方>
- remedy_log:  (append-only)
  - iter_N | diagnosis: ... | patch: ...
```

⚠️ **交叉验证修正**：整套 momentum 复发链（≥2/≥3/appeared_in/remedy_log/default-to-merge/WORKFLOW-THEMES）**全是 prompt，100% decoration**——输出仅按路径传给 patcher 从不被解析，无代码统计支持轨迹数，无 gate 拒不达标。WORKFLOW-THEMES 是语料里「L1 从跨轨迹浮现」最字面实现，但 LLM 可从单条轨迹幻觉一个 theme 无人拦截。

仍有价值的**设计思想**（需 code-enforce 才能借鉴）：
- "A pattern is a class of mistake or success, not an instance" — 强制抽象化
- "Default to merging" — 按 decision-rule+action 匹配而非措辞匹配
- remedy_log append-only + appeared_in ≥3 触发结构性改变
- 真正 code-real 的部分：确定性 trace 归一化层（`trajectory_logger.py`）、`<diagnosis>` 正则提取、classify_batch is_correct 路由

**范式 4：树状进化轨迹 + 嵌入检索（MLEvolve）**

- `search_node.py:395-421` — `get_root_to_current_trajectory()` 从根到当前的完整进化路径
- `global_memory.py` + `retriever.py` — BM25 + FAISS 混合检索（RRF 融合），按 label 过滤（success/fail），支持 dissimilar 检索
- ⚠️ 检索后**不做聚类/模式抽取**——只是单邻居 top-2 召回

**范式 5：角色分离 + 对比配对（SkillGrad diagnoser + textgrad）**

- `prompts/diagnoser.py` — 泛化标签（3-6 词，禁任务特定值）+ 禁止开药方
- ⚠️ **交叉验证修正**：Contrastive diagnoser 被低估——它是同任务 **base→evolved 两版本**配对（`diagnoser.py:140-150`），跨轨迹成分比之前承认的强，与四象限同类
- ⚠️ **交叉验证修正**：textgrad `TextualGradientDescentwithMomentum`（`optimizer.py:233-`）是独立且 code-real 的工作类（非死件），真正的死件是 `gradient_memory` 默认关 + `Aggregate` 丢 context

### 2.2 对 L1 认知策略的适配分析

现有系统的轨迹处理全部面向 L0（战术规则提取）。L1 需要的信号与 L0 根本不同：

| 维度 | L0 信号特征 | L1 信号特征 |
|------|-----------|-----------|
| 粒度 | 单条轨迹内可诊断 | 跨轨迹模式才能浮现 |
| 内容 | "遇到 X 做 Y"的具体规则 | "改变思考过程的某个环节" |
| 一致性 | 不同任务的修补不同 | 不同任务的表现一致 |
| 修补效果 | 一次修补通常有效 | 多次规则修补无效（信号） |

L0/L1 信号分离的自然判据（非硬编码规则）：
- **frequency**：appeared_in ≥3 个不同任务 → 大概率 L1
- **cognitive-level**：wrong-decomposition → L1；wrong-formula-syntax → L0
- **remedy resistance**：多次规则修补仍不解决 → L1 信号

可借鉴的原语分类（按 real/decoration × 服务层级）：

| 原语 | 出处 | real/decoration | 服务层 | 适配方式 |
|------|------|----------------|--------|---------|
| 四象限纵向配对 | SkillOpt | REAL | L1 入口 | 借鉴，但别降维成 L0 规则，补跨任务聚类 |
| best-score gate + step_buffer | SkillOpt | REAL | L1 | **直接借鉴**（比四象限更该搬的件）|
| PROTECTED 区隔离 | SkillOpt | REAL | Both | **直接借鉴** |
| meta_skill L1 容器 | SkillOpt | REAL | L1 | 从单线覆盖升级为策略种群 |
| WORKFLOW-THEMES ≥N | SkillGrad | DECORATION | L1 | 思想对，必须 code-enforce |
| default-to-merge | SkillGrad | DECORATION | L1 前置 | 加真实相似度/聚类后端 |
| remedy_log + appeared_in≥3 | SkillGrad | DECORATION | L1 | code-enforce 计数 = PROPOSE/REFINE 切换信号 |
| contrastive 对比 | SkillGrad | MIXED | L1 来源 | 从单任务扩展为跨任务对比聚类 |
| RRF+FAISS 检索 | MLEvolve | REAL | L0 | 召回单位从「单邻居」改为「模式簇」|
| 聚类+medoid 共识 | OptoPrime | REAL | L0 | 从同 step 候选移植到跨轨迹经验簇 |
| patience→Tier 升级 | MLEvolve | REAL | L1 触发 | 借鉴信号，改造：产出须持久化进 L1 种群 |

### 2.3 轨迹经验处理设计（整合交叉验证后的完整方案）

**核心新设计：code-enforced 跨轨迹模式聚合器**——5 库集体缺失的承重层。

**S0-S7 完整管道**（标注每步的 code-enforce 要求）：

```
S0 RAW JSONL — 原始轨迹存储
    [确定性，code-enforced]

S1 确定性 merge/render — 轨迹归一化
    [code-enforced] 借鉴 SkillGrad trajectory_logger 的 merge_trace_events
    THOUGHT→ACTION→OBSERVATION + ERROR 正则标记

S2 code-real 结果分类 — 四象限 + 适应度 gate
    [code-enforced] 借鉴 SkillOpt build_comparison_pairs（per-task 1:1 join）
    + best-score accept/reject gate（trainer.py:770-829）
    + step_buffer 负向记忆（score_before→after rejected_edits）
    输出：improved / regressed / persistent_fail / stable_success 标签
    ⚠️ 不在此步降维成规则——标签向下传递

S3 per-traj 诊断 — 泛化标签 + 因果链
    [LLM 可用] 借鉴 SkillGrad diagnoser（角色分离，禁止开方）
    + contrastive 对比扩展为跨任务版
    输出：task-agnostic LABEL（3-6 词）+ 因果链
    认知操作标注：分解方式/注意力分配/验证行为/纠错行为/元认知

S4 跨轨迹聚合 ← 【核心新增承重层】
    [code-enforced] 这是 5 库集体缺失、我们必须自建的
    (a) 聚类后端：AgglomerativeClustering（借 OptoPrime）+ RRF/FAISS（借 MLEvolve）
        匹配判据：按 decision-rule + corrective-action（借 SkillGrad default-to-merge 思想）
        但用真实相似度计算，不全交 LLM
    (b) 真实统计：每个模式簇的 轨迹支持数 / 跨象限胜率 / appeared_in 列表
        code 计数器，非 prompt 自述
    (c) 复发门控：≥N 复发阈值（code-enforce，借 SkillGrad ≥3 思想但真实实现）
        + 留出任务泛化验证（不达标的模式不得升级）
    (d) remedy_history：append-only 审计史，带真实 score_before→after
        与 SkillOpt step_buffer 合并 = PROPOSE/REFINE 切换的真实信号

S5 L0/L1 路由 + 物理隔离
    [code-enforced] 路由信号：frequency × cognitive-level × remedy_resistance
    物理隔离：PROTECTED fenced 区（借 SkillOpt _strip_all）
    内层 L0 优化器无法改写外层 L1 策略容器

S6 持久化 + 按模式簇召回呈现
    [LLM 可用] 写入可搜索、可并行比较、带衰减/升级的 L1 策略种群
    按操作类型适配呈现：
      PROPOSAL：persistent_fail top-3（按复发计数排序）
               + remedy_history 显示无效 + 负档案
      REFINE：瓶颈组件的 delta 排序 + contrastive 对比聚类
      EXPLOITATION：原始轨迹 + 标准 SkillOpt 管道

S7 真实 score gate + 策略级反馈
    [code-enforced] 借鉴 SkillOpt best-score accept/reject + revert gate
    复发≥3 且 REFINE delta≈0 → 触发 PROPOSE
    接受/回滚决策基于多 rollout 显著性检验
```

**S1/S2/S4/S5/S7 必须 code-enforced**——现有系统恰在 S4/S5 外包给 LLM 导致 L1 失真。S3/S6 可用 LLM。

### 2.4 头号风险：L1 浮现是待验证假设

**「L1 信号能否从跨轨迹稳定浮现」本身未经实证**——5 库没一个做出完整聚合链。必须在论文方法节显式声明并设计验证：

| # | 风险 | 具体问题 | 验证方案 |
|---|------|---------|---------|
| T1 | **载体无锚点** | L0 靠 task_id 1:1 join；L1「同一认知策略」的跨任务身份只能靠语义聚类认定 | ablation：开/关 S4 聚合层，比 L1 增益 |
| T2 | **gate 强度断崖** | 现存 code gate 都是数值的；L1 提升判据「反复奏效」退回 prompt 自述 | 自建代码计数器 + 留出集泛化验证 |
| T3 | **适应度方差** | L1 信号稀疏 + LLM 评判随机 | 多 seed rollout + 显著性检验 |
| T4 | **frozen 模型封顶** | persistent_fail 若源于模型能力而非策略缺失→外层无休止 PROPOSE | 「能力封顶 vs 策略缺失」判别机制 |

---

## 3. PROPOSAL 引导流水线

生成质的不同的新认知策略。

### Phase 0：经验聚合 [P7]

输入：累积执行轨迹（成功/失败/对比）

不喂散装轨迹。按失败模式聚类，每簇附代表性轨迹片段 + 成功对比。

输出：≤5 个失败模式簇 + 代表性证据

### Phase 1：强制诊断 [P2 + P3]

输入：失败模式簇 + 当前认知策略文本

**角色分离**：诊断器只产出分析，禁止提出新策略。

强制诊断 Probe（适配版 Arbor §2）：
- Q1 瓶颈 CLASS（6 轴）：wrong-decomposition / wrong-attention / wrong-verification / wrong-recovery / wrong-metacognition / wrong-representation。引用 ≥2 个具体失败轨迹片段。
- Q2 隐藏假设：当前策略隐含依赖什么假设？放弃后什么新方法解锁？
- Q3 系统性回避：什么系统性弱点被当前策略绕过而非真正解决？
- Q4 Hamming 检验：解决这个瓶颈后多数失败任务会实质改善吗？若否 → 重做。

输出：瓶颈 CLASS + 隐藏假设清单 + 因果链

### Phase 2：策略生成 [P1 入口 + P4 + P5]

输入：Phase 1 诊断结果 + 负档案

**层级锁定入口**：
- "改变 agent 如何组织思考过程，不是增加具体规则"
- "Mechanism is a noun——必须命名一个可实现的新流程/组件"
- "2-page paper test——能写成独立方法论文的才合格"

**四个 Move + 正交性强制**：
- Move A 假设反转：Phase 1 Q2 的每个假设取反
- Move B 从理想解倒推：任务全解时 agent 缺什么认知能力
- Move C 类比迁移：映射到已解领域的思维方法
- Move D 失败轨迹反向工程：具体失败 → 最小新认知能力 → 聚类

正交性检查：每个候选在 {攻击的假设, 机制类别, 类比来源} 中至少一维不同。

**负档案**：被废弃的认知策略及其废弃原因。候选不得重走相同的隐藏假设或机制类别，除非在 Conflicts 字段显式说明如何克服废弃原因。

输出：2-4 个候选认知策略，每个附 5 字段声明（assumption / mechanism class / hypothesis chain / orthogonality / conflicts）

### Phase 3：排斥过滤 [P1 出口]

逐条检查每个候选：
- 能表述为"遇到 X 做 Y"的具体规则？→ kill（这是 L0）
- 只是改写 prompt 措辞？→ kill
- 只是"more X"（更仔细/更多次/更详细）？→ kill
- 描述目标而非机制？→ rewrite
- 与被剪枝策略共享 assumption+mechanism 且无 counter？→ kill
- 无法追溯到 Phase 1 诊断的哪个瓶颈？→ kill（probe-disconnected）

存活候选 → 进入 rollout 评估 + 显著性门控晋升

---

## 4. REFINE 引导流水线

调整现有认知策略的某个组件。v4 和 Arbor 都缺少这个操作。

### Phase 0：策略解剖

将当前认知策略分解为可独立调整的组件：推理模式 / 注意力分配 / 验证方法 / 纠错流程 / 元认知控制。每个组件用 1 句话描述其当前实现。

### Phase 1：环节诊断 [P2 + P7]

借鉴 MLEvolve 的轨迹分析 + SkillGrad 的模式驱动。

根据最近 N 轮执行轨迹的模式：
- 哪个组件最频繁成为失败的直接原因？
- 成功轨迹与失败轨迹在哪个组件的执行上差异最大？
- 引用具体轨迹片段作为证据。

输出：瓶颈组件定位 + 证据

### Phase 2：局部重构 [P1 REFINE 版]

**层级锁定 REFINE 版**：
- 只修改被诊断的瓶颈组件，保持其他组件不变
- 修改必须是流程/方法层面的重构，不是"增加检查 X"的规则
  - ✓ "将线性执行改为带验证回路的执行"
  - ✓ "将全局注意力改为分层聚焦"
  - ✗ "执行前先检查数据格式"（规则，属 L0）
  - ✗ "更仔细地验证"（more X，无机制）

**Remedy History 约束**（借鉴 SkillGrad §4.6 "differ from prior remedies"）：
- 读取该组件的修改历史
- 如果计划的修改与之前尝试过的近似 → 必须说明差异或换角度

### Phase 3：一致性检查 + 停滞升级 [P6]

- 修改后的组件与其他组件是否一致？
- 会不会破坏现有的成功模式？

**停滞→升级**：如果最近 K 轮 REFINE 都未产生显著改善（多 rollout 显著性检验），建议切换到 PROPOSAL 模式寻找质的不同方向。

---

## 5. EXPLOITATION：内层 Level 0 优化

完全复用 v4 §3（v3 原样复用）的设计：
- G0 四源轨迹梯度（失败/对比/纵向/成功路径）
- 显著性门控 held-out 晋升
- 单调精英
- Negative archive + 梯度记忆
- 确定性 niche 配额

唯一新增：EXPLOITATION 在当前认知策略框架下运行，其产出的战术规则自动标注对策略框架的 assumption 依赖，供跨帧继承判断。

---

## 6. 三操作的调度关系

```
开始 → PROPOSAL（初始认知策略）→ EXPLOITATION（框架内规则优化）
                                           │
                                     Level 0 饱和？
                                     （persistent_fail 存在
                                      AND 近期无显著提升）
                                           │
                                    ┌──────┴──────┐
                                    │              │
                              首次饱和         已 REFINE 多次
                              或 REFINE        仍无显著改善
                              有改善空间        （remedy_history ≥3
                                    │           且 delta ≈ 0）
                                    │              │
                                    ▼              ▼
                                 REFINE        PROPOSAL
                              （局部调整        （质变跳转
                               策略组件）       到新策略）
                                    │              │
                                    ▼              ▼
                              EXPLOITATION    EXPLOITATION
                              （继续框架        （新框架下
                               内优化）          从头优化）
```

调度判据（机制化，非 prompt 注入）：
1. persistent_fail 簇是否存在？否 → 继续 EXPLOITATION
2. 近期 Level 0 patch 后 held-out 有显著提升？是 → 继续 EXPLOITATION
3. 当前认知策略的 remedy_history 中同一 cognitive-level 被 REFINE 过 ≥3 次且 delta ≈ 0？
   - 否 → REFINE
   - 是 → PROPOSAL

---

## 7. 保留的 v4 设计（无需修改）

以下 v4 设计在 v5 中原样保留：

- **§1.1-1.4** 双层两生成器框架、Level 1 ≠ big Level 0 的代码实证、Level 1 解决的 scaling 问题
- **§3** 内层 Level 0 机制（四源梯度、显著性门控、单调精英、negative archive、niche 配额）
- **§4** 两层连接（触发、晋升、正反馈律、评估前沿移动）
- **§5** 完整架构图
- **§6** Skill Document 结构
- **§7** 与 SkillOpt 的核心对比
- **§8** 论文 claim 的诚实边界

---

## 8. 待完善项目（等 claude-worker 调研后更新）

### 8.1 轨迹经验处理的细化设计 ✅ 交叉验证完成，进入实现细化

claude-worker 完成了 5 系统 × 6 维度的独立调研 + 红队复核（校正了 4 处过度宣称）。§2 已整合交叉验证结果。

剩余细化项：
- [ ] S4 跨轨迹聚合器的具体实现：聚类算法参数、相似度阈值、增量更新策略
- [ ] S5 L0/L1 路由的 frequency×cognitive-level×remedy_resistance 三信号组合权重
- [ ] S7 best-score gate 的具体实现：多 rollout 样本量、显著性检验方法
- [ ] 「能力封顶 vs 策略缺失」判别机制设计（T4 风险）
- [ ] 策略种群的数据结构：存储格式、衰减机制、并行比较接口
- [ ] S4 聚合层的 ablation 实验设计（T1 风险验证）

### 8.2 引导机制调研的交叉验证 ✅ 已完成

claude-worker 独立完成了 8 库「装饰猎手」红队源码复核。交叉验证结果已整合至 §1.4-§1.6：
- [x] 对比两份调研的结论差异 → 核心发现一致，worker 补充了「第一定律」框架
- [x] 识别我可能遗漏的设计模式 → 补充 P8-P10（§1.5）
- [x] 修正可能的实证错误 → 修正 MLEvolve module-enum 和 SkillOpt 保护区两处高估

### 8.3 需要进一步设计的问题

- [ ] PROPOSAL 产出的认知策略的具体文本格式和结构规范
- [ ] REFINE 时策略组件的分解粒度如何确定（固定 5 维 vs 动态发现）
- [ ] 帧无关知识继承的自动化判定（v4 §2.3 的 assumption_dependency 如何标注）
- [ ] 评估前沿移动的具体课程化策略
- [ ] 完整的 ablation 实验设计（v5 新增的引导流水线各环节的消融）
- [ ] mask 消融实验设计：分离编排效果 vs 底座效果（R2）
- [ ] 多 seed + 显著性检验方案（R4）
- [ ] held-out pool 轮换 + 独立 test set 方案（R5）
- [ ] 「第一定律」审计：§3-§4 流水线中每步标注其 C1-C6 类型，无标注步骤需重设计或降级为 optional

---

## 附录 A：引导机制调研的关键代码引用

### A.1 Arbor idea_drafting.md 核心片段

**§1 Mindset（层级锁定）**：
> "HOW, not HOW MUCH — Change the algorithm, representation, control flow, or objective. Do not just change a number."
> "10×, not 10% — Affect a class of failures, not a few specific examples."
> "Mechanism is a noun — You must name a concrete new component or process. 'improve' is not a mechanism."

**§2 Probe Q1（瓶颈 CLASS）**：
> 7 axes: wrong retrieval / wrong reasoning / wrong stopping / wrong representation / wrong objective / wrong action-space / wrong credit-assignment

**§6 Self-Check Kill（排斥过滤器）**：
> - Can the idea be expressed as a single number or knob? → kill
> - Is it just a reworded prompt? → kill
> - Is it "more X"? → kill
> - Does it describe a goal, not a mechanism? → rewrite
> - Does it re-tread a pruned idea without countering the lesson? → kill
> - Is it disconnected from the Probe answers? → kill

**§8 Anti-Pattern（probe-disconnected 检测）**：
> Probe points to X, but the candidate does Y (because Y is easier) → kill

### A.2 SkillGrad momentum 核心片段

**模式记录设计规则**（`prompts/momentum.py`）：
> "A pattern is a class of mistake or success, not an instance. Two signals match the same pattern when they share the same decision rule AND corrective action, even if the objects, sections, or values differ across tasks."
> "Default to merging. When a new signal could plausibly fit an existing pattern or become a new one, merge into the existing pattern. Rare singletons absorb into the nearest broader pattern."
> Remedy log is "append-only history — never truncate prior rows."

**Patcher 消费模式记录**（`prompts/patcher.py`）：
> "§3 — Iterate by pattern, not by task"
> "§4.6 — Differ from prior remedies. Before patching at an anchor with a non-empty remedy_log, read the log. If your planned patch closely matches a previously-tried one, either explain how this attempt differs, or pick a different angle."
> "When appeared_in has ≥3 entries and prior atomic in-place edits have not resolved recurrence, treat in-place edits as suspect — consider structural moves."

### A.3 MLEvolve 停滞检测与 Tier 升级

**evolution_agent.py:69-111**：
```
success_patience >= 2 OR total_patience >= 5 → use_magnitude_prompt = True
→ "You MUST propose a Tier 2 or Tier 3 change to break the plateau."
→ Tier 1 (How/optimization) forbidden
→ Tier 2 (What/representation) or Tier 3 (Architecture/paradigm shift) required
```

**Branch Evolution History**：
> `search_node.py:395-421` — `get_root_to_current_trajectory(max_steps=10)` 从根到当前的完整路径，每步：Stage / Design / Results / Metric Change (↑↓→)

### A.4 SkillOpt 纵向对比

**slow_update.py:152-215** — `build_comparison_pairs()` 四象限：
> improved (wrong→right) / regressed (right→wrong) → HIGHEST PRIORITY / persistent_fail (wrong→wrong) / stable_success (right→right)

**slow_update.md**：
> "Your role is different from the per-step analyst. The per-step analyst sees individual trajectories and proposes local patches. YOU see how the skill has evolved across an entire epoch by comparing the SAME tasks under two consecutive skill versions."
> Priority: "(1) preventing regressions, (2) fixing persistent failures, (3) reinforcing successful patterns"

### A.5 SkillGrad diagnoser 泛化标签

**prompts/diagnoser.py**：
> "Write your analysis inside a <diagnosis> block. Start with a LABEL — a short phrase (3-6 words) naming the general type of error, not the specific task."
> "MUST NOT: Do not use task-specific values (exact column letters, cell references, domain terms) in the LABEL."
> "Do not prescribe specific changes to the agent's skill document. Focus on what happened and what should have happened."

Contrastive diagnoser 额外要求：
> "Whether the success seems robust or fragile — would the same approach work on similar tasks, or did the agent get lucky?"
