# 训练机制 v3：统一方案设计

> 基于 claude-lead 的 MCTS 分析 + claude-worker 的 7-agent 对抗式审计 + 8 库代码实证。
> 两份分析的交叉综合：3 个独立提案全判 mixed，收敛到同一组一阶机制。

---

## 0. 核心洞察：分配器 vs 生成器

**任何方案都由两层构成**：

| 层级 | 角色 | 实例 | 产出 |
|---|---|---|---|
| **生成器（一阶）** | 轨迹诊断 → 文本梯度 → patch | SkillOpt reflect, SkillGrad diagnoser, Trace2Skill map-reduce | **唯一产生新 skill 内容的组件** |
| **分配器（二阶）** | 决定「下一份 rollout 投哪个候选」 | MCTS UCT, QD-Archive, 种群/island, 进化 | 一行新策略都不产生 |

**推论**：
- 生成器未枯竭时，分配器间差异只是 compute 效率（sublinear 系数）
- 生成器枯竭后，任何分配器都只是在天花板下烧 compute
- **设计重心必须放在「如何让生成器不枯竭」上**——这是 8 个系统全都没解决的问题

**论文 claim 的诚实边界**：
> 「单调不退化（硬保证）+ 在任务/模型天花板前持续提升 + 该天花板由梯度多样性与评估前沿移动推高」

不是「随 compute 单调上升不止」——后者没有任何机制支撑。

---

## 1. 为什么抛弃 QD-Archive 和 MCTS 叙事

### 1.1 QD-Archive 的 4 个致命问题（已确认）

1. behavior descriptor 在 skill 文本空间无自然定义
2. 网格离散化粒度难以 justify
3. 低质量 niche 浪费 compute
4. 本质是存储结构，不是搜索策略

### 1.2 MCTS 为什么不是核心叙事

UCT 在 skill 文本空间的 3 个理论根基断裂：

1. **分支因子非平稳**：若 action=任意文本编辑 → 分支因子近无限，sqrt(lnN/n) 的 n 永不被均摊，UCT 退化为「无限宽零纵深」的广度优先资源黑洞
2. **expansion 是观测依赖的**：同一节点第 1 次和第 5 次 rollout 因采样到不同失败轨迹会聚出不同 action 集 → 破坏 UCT 回访统计的平稳性假设
3. **评估高方差**：agent rollout 本身有随机性，单次评估不可信 → best-child 近随机，树退化为昂贵的随机搜索

**关键推论**（claude-worker）：为让 UCT 成立，被迫做 (a) 丢 UCT 统计量改用 held-out 过门率，(b) action 从动态诊断改回固定算子枚举，(c) 退火换成正反馈——做完这三步，它**已经不是 MCTS，就是一个带 niche 配额的种群方案**。

MCTS 可以作为每个 niche 内部扩展调度器的实现细节，但叙事上不声称它提供 anti-collapse 或 scaling。

---

## 2. 统一架构总览

```
┌─────────────────────────────────────────────────────────┐
│                   Evaluation Frontier                    │
│        （随 skill 成熟移动的任务课程——真正的 scaling 杠杆）    │
└─────────────┬───────────────────────────┬───────────────┘
              │ 提供任务                    │ 反馈适应度
              ▼                            ▲
┌─────────────────────────────────────────────────────────┐
│              K-Island Portfolio（分配器层）                │
│                                                          │
│  Island 1    Island 2    Island 3    ...   Island K      │
│  (niche A)   (niche B)   (niche C)        (niche K)     │
│     │           │           │                │           │
│  [skill₁]   [skill₂]   [skill₃]         [skillₖ]      │
│     │           │           │                │           │
│  内部树形      内部树形      内部树形         内部树形     │
│  扩展路径      扩展路径      扩展路径         扩展路径     │
│                                                          │
│  确定性 niche 配额：max_compute_per_island / round        │
│  无改进→增探索 正反馈律 + 停滞→算子升级                     │
└──────────────────────┬──────────────────────────────────┘
                       │ 分配 rollout 到哪个 island/节点
                       ▼
┌─────────────────────────────────────────────────────────┐
│            四源轨迹梯度（生成器层——心脏）                    │
│                                                          │
│  ① 失败诊断    ② 对比诊断    ③ 纵向对照    ④ 成功反推    │
│  (SkillGrad)   (contrastive) (slow_update) (Trace2Skill)│
│                                                          │
│  诊断-编辑解耦（textgrad 模式）                             │
│  rejected/negative buffer（梯度记忆）                       │
└──────────────────────┬──────────────────────────────────┘
                       │ 产生 patch 候选
                       ▼
┌─────────────────────────────────────────────────────────┐
│          显著性门控 Held-out 晋升（质量保证层）              │
│                                                          │
│  多 rollout 显著性检验（非单次）                            │
│  当前解作为候选之一（POLCA trick，零成本单调性）              │
│  held-out 轮换/刷新（防 multiple-comparisons 过拟合）       │
│  单调精英 = 全局最佳 skill（硬保证绝不退化）                 │
└─────────────────────────────────────────────────────────┘
```

---

## 3. 五大核心机制详细设计

### 3.1 四源轨迹梯度（生成器——一阶主体）

这是优化器的心脏，不是分配器的插件。

#### 3.1.1 四个梯度源

| 源 | 输入 | 输出 | 来自 | 关键约束 |
|---|---|---|---|---|
| **失败诊断** | 失败轨迹 | 泛化的失败模式标签（不含具体处方） | SkillGrad `FAILURE_DIAGNOSER` | **禁止开处方**——诊断只说「问题是什么」，不说「怎么改」 |
| **对比诊断** | 同任务：基线失败轨迹 vs 当前成功轨迹 | robust/fragile 判断 + 成功因子归因 | SkillGrad contrastive | 追问「成功是因 skill 还是运气」 |
| **纵向对照** | 同组任务：上一版 skill 的结果 vs 当前版结果 | regression/drift/improvement 分类 + 方向性指导 | SkillOpt `slow_update` | Markov：只看相邻版本 |
| **成功路径反推** | 成功轨迹 | Lean Solution Path → 可提取的策略规则 | Trace2Skill map-reduce | 只从成功中提取，不从失败中猜 |

#### 3.1.2 诊断-编辑解耦（textgrad 模式）

```
[诊断器] → 只产生批评/分析，禁止产生新版本
    "DO NOT propose a new version ... that will be the job of the optimizer"
      │
      ▼ 批评文本（文本梯度）
[编辑器] → 根据批评修改 skill document
    输入：当前 skill + 批评 + rejected buffer
    输出：patch（edits 列表）
```

为什么解耦比单 prompt 好：
- 诊断器不受「我能不能改」的约束，可以给出更激进的批评
- 编辑器有明确的目标（这些批评）和约束（结构帽、protected region）
- 两步可以独立调参（诊断器用高 reasoning effort，编辑器用中等）

#### 3.1.3 梯度记忆（rejected + negative buffer）

```python
rejected_buffer:    # SkillOpt 的 step buffer
  - 记录被 held-out 门拒绝的 patch + 拒绝原因
  - 作为编辑器的上下文：「不要再试这些」
  - 按 recency 衰减（最近 N 条）

negative_archive:   # Arbor 的 constraints_block
  - 记录被验证为无效的策略方向（按机制类去重）
  - 作为诊断器的硬约束：「这些方向已试过且失败」
  - ⚠️ 暗面防护：设置置信度阈值
    - 只有「多次验证均失败」才写入（≥3 次）
    - 带软过期：超过 M 步后从「硬禁止」降级为「不推荐」
    - 防止早期噪声永久封禁正确方向
```

### 3.2 K-Island Portfolio（分配器——二阶）

#### 3.2.1 Island 结构

每个 island 是一条独立的 skill 进化路径：

```python
class Island:
    id: str                  # 确定性标识（不靠 LLM 语义）
    origin: str              # 血统：从哪个 seed/parent 派生
    skill_history: [SkillVersion]  # 版本链（树形内部扩展）
    current_best: SkillVersion     # 该 island 的局部精英
    stagnation_count: int    # 连续无改进步数
    compute_spent: int       # 已消耗 rollout 数
    niche_key: str           # 确定性 niche 标识（见 3.2.2）
```

#### 3.2.2 确定性 Niche-Key（替代 QD 行为描述子）

**关键设计决策**：niche-key 不来自 LLM 的失败标签（会摧毁配额确定性），而绑定到确定性来源：

方案 A：**绑定到 held-out 任务簇**
```
niche-key = "擅长哪类任务"
  - 将 held-out 按任务类型分 K 簇（数据分析/文件操作/推理/...）
  - 每个 island 的 niche = 它在哪个任务簇上得分最高
  - 确定性：任务簇划分是预设的，不靠 LLM 判断
```

方案 B：**绑定到血统（MLEvolve 模式）**
```
niche-key = island_id（= 创建时的 seed 编号 / parent_id + 创建步数）
  - 完全确定性，不靠任何语义判断
  - 配额只按 island 计数，不按语义分类
  - 更简单，但不保证 island 间策略多样性
```

**推荐：方案 B（血统制）为主 + 方案 A 的多样性奖励为辅**
- 配额按血统确定性执行（`max_compute_per_island` 硬限）
- island 创建时的初始 seed 来自不同失败簇（一次性 LLM 分类，只在创建时用）
- island 间的 diversity 通过「cross-island fusion」算子间接维护

#### 3.2.3 Compute 分配策略

```python
def allocate_next_rollout(islands, global_elite):
    # 1. 确定性配额硬限：每轮每 island 最多 max_per_round 次 rollout
    eligible = [i for i in islands if i.round_count < max_per_round]
    
    # 2. 优先级排序（不用 UCT，用简单的策略）
    for island in eligible:
        if island.stagnation_count == 0:
            island.priority = island.current_best.score  # exploit
        else:
            # 无改进→增探索：停滞越久优先级越高
            island.priority = base_priority + stagnation_bonus(island)
    
    # 3. 选择 + 轮转保证
    return select_with_round_robin_floor(eligible)
```

### 3.3 显著性门控 Held-out 晋升（质量保证层）

#### 3.3.1 为什么单次评估不够

agent rollout 有内在随机性（LLM 温度、工具调用顺序、环境状态）。单次 rollout 的 pass/fail 是高方差的。

#### 3.3.2 晋升协议

```python
def try_promote(candidate_skill, current_elite, held_out_tasks):
    # Step 1: 多次 rollout 收集样本
    candidate_scores = [rollout(candidate_skill, tasks) for _ in range(N_ROLLOUTS)]
    elite_scores = [rollout(current_elite, tasks) for _ in range(N_ROLLOUTS)]
    # 可以复用 elite 的历史 rollout 结果（如果 held-out 没变）
    
    # Step 2: POLCA trick——当前精英作为候选之一
    # 即使 candidate 和 elite 打平，也不退化
    
    # Step 3: 显著性检验
    # 单边配对 t-test 或 Wilcoxon：candidate > elite?
    p_value = paired_test(candidate_scores, elite_scores)
    
    if p_value < SIGNIFICANCE_THRESHOLD:  # e.g., 0.05
        return PROMOTE  # candidate 成为新的全局精英
    else:
        return REJECT   # 记入 rejected_buffer，candidate 不替换精英
```

#### 3.3.3 Held-out 轮换防过拟合

```
问题：K-island × M 步 × 多重启 = 经典 multiple-comparisons 过拟合
  → 在固定 held-out 上反复 model-selection，最终「过拟合 held-out」

解法：held-out 分池轮换
  - 将全部验证任务分为 P 个池（P ≥ 3）
  - 每 R 步轮换使用的池
  - 任何单个 skill 至多在同一个池上被评估有限次
  - 全局精英的最终评估用一个从未在训练中用过的保留池
```

### 3.4 「无改进→增探索」正反馈律

**这是与 MLEvolve/Arbor 最关键的设计分歧**：它们内建退火/停机的负反馈（compute 越多越接近停机），我们需要正反馈（无改进时增加探索而不是收缩）。

#### 3.4.1 机制化要求

**这必须是带阈值 + 预算记账的真机制**，不能是注入 LLM 的字符串。否则：
- 和 Arbor 的 `"MANDATORY paradigm_shift"` 是同一类空话
- 高方差 fitness 下被假性「无改进」误触发 → 不断开低质量 niche 烧 rollout

#### 3.4.2 三级递进响应

```python
def handle_stagnation(island, global_state):
    s = island.stagnation_count
    
    if s >= TIER1_THRESHOLD:  # e.g., 3 连续无改进
        # Tier 1: 切换梯度视角
        # - 从失败诊断切到对比诊断或成功反推
        # - 换一批 rollout 任务（从评估前沿的不同区域采样）
        island.gradient_source = rotate_gradient_source(island)
        
    if s >= TIER2_THRESHOLD:  # e.g., 6 连续无改进
        # Tier 2: 结构性重写（MLEvolve Magnitude Tier 2/3）
        # - 不再做小 edit，改为全 skill 重写
        # - 保留 negative archive 约束，但允许大幅改变结构
        island.operator = STRUCTURAL_REWRITE
        
    if s >= TIER3_THRESHOLD:  # e.g., 10 连续无改进
        # Tier 3: 硬重启为新 island
        # - 当前 island 的精英存入 archive（不丢弃）
        # - 从当前全局精英 fork 一个新 island
        # - 新 island 带不同的初始梯度视角
        # - 旧 island 的 negative archive 继承（不重复探索）
        new_island = fork_island(global_elite, island.negative_archive)
        replace_island(island, new_island)
```

#### 3.4.3 防误触发

```python
# 关键：「无改进」的判定不能是单次比较
def is_stagnant(island) -> bool:
    recent_attempts = island.recent_promote_attempts[-WINDOW:]
    if len(recent_attempts) < MIN_ATTEMPTS:
        return False
    # 所有最近 WINDOW 次尝试都被 reject
    return all(attempt.result == REJECT for attempt in recent_attempts)
    
# 而不是：
#   if island.current_best.score == last_step_score:  # 单步比较，高方差
```

### 3.5 评估前沿移动（真正的 Scaling 杠杆）

**这是 8 个系统全都没做的、我们的核心创新之一。**

#### 3.5.1 为什么需要

生成器（诊断）的边际信息产出会递减：
- 随 skill 成熟，easy tasks 全部被解决，诊断只能从 hard tasks 中提取信号
- 但如果 held-out 固定不变，一旦 skill 在这批任务上饱和，梯度信号归零
- 这时任何分配器都无法产生新的改进——天花板被钉死

#### 3.5.2 课程化移动策略

```python
class EvaluationFrontier:
    def __init__(self, all_tasks, K_pools):
        # 按预估难度排序/分层
        self.difficulty_tiers = partition_by_difficulty(all_tasks)
        self.current_tier = 0
        self.within_tier_pools = split_into_pools(self.difficulty_tiers[0], K_pools)
    
    def should_advance(self, global_elite_scores):
        # 当前 tier 的平均得分超过阈值 → 推进到更难的 tier
        avg = mean(global_elite_scores[self.current_tier])
        return avg >= ADVANCE_THRESHOLD  # e.g., 0.8
    
    def advance(self):
        self.current_tier += 1
        # 保留部分旧 tier 任务（防 regression）
        # 加入新 tier 的更难任务
        self.within_tier_pools = merge_and_split(
            keep_fraction(old_pools, 0.3),  # 30% 旧任务防退化
            self.difficulty_tiers[self.current_tier]
        )
    
    def get_rollout_tasks(self):
        # 主动挖掘：优先采样当前 skill 仍失败的任务
        failed = [t for t in current_pool if global_elite.fails(t)]
        if len(failed) >= MIN_BATCH:
            return sample(failed, BATCH_SIZE)
        else:
            return sample(current_pool, BATCH_SIZE)  # 随机采样兜底
```

#### 3.5.3 与论文叙事的关系

这给了我们一个有层次的 claim：
1. **硬保证**：单调精英 + held-out 门 → 绝不退化
2. **在固定任务集上**：四源梯度 + K-island 多样性 → 持续提升直到该任务集的天花板
3. **天花板推高**：评估前沿移动 → 持续提供新颖梯度信号 → 天花板不是固定的

---

## 4. 与 SkillOpt 的核心对比（论文叙事）

| 维度 | SkillOpt | 本方案 |
|---|---|---|
| **搜索路径** | 单路径增量修改 | K-island 并行探索 |
| **坍缩防护** | 无（gate acceptance→0 即停） | 4 层防护（配额+negative archive+门控+正反馈律） |
| **梯度来源** | 单源（失败分析） | 四源（失败/对比/纵向/成功反推） |
| **诊断-编辑** | 耦合（同一 prompt） | 解耦（textgrad 模式） |
| **评估可信度** | 单次 rollout | 多 rollout 显著性检验 |
| **Compute scaling** | 几步后 plateau/退化 | 正反馈律 + 评估前沿移动 |
| **天花板** | 被初始策略空间钉死 | 被评估前沿持续推高 |

---

## 5. 红队识别的风险 + 对策

### 5.1 negative archive 永久封禁正确方向

**风险**：在坏梯度/噪声评估下，正确的策略方向被早期否决后写入 negative archive，永久不可探索。

**对策**：
- 写入阈值：≥3 次独立验证均失败才写入
- 软过期：超过 M 步后从「硬禁止」降级为「低优先级」
- Tier 3 重启时可选择性忽略部分 negative archive

### 5.2 held-out 过拟合（multiple comparisons）

**风险**：K-island × M 步 × 多重启 = 大量 model selection → 即使每次检验 p<0.05，累计假阳率远超 5%。

**对策**：
- held-out 池轮换（§3.3.3）
- 最终评估用保留池（从未在训练中用过）
- 可选：Bonferroni 校正晋升阈值

### 5.3 「无改进→增探索」被假性触发

**风险**：评估高方差下，真正在改进的 island 被误判为「停滞」→ 被不必要地重启/升级算子 → 浪费 compute。

**对策**：
- stagnation 判定基于窗口内所有尝试（§3.4.3），不是单步比较
- Tier 1/2 是可逆的（只改梯度视角/算子，不杀 island）
- 只有 Tier 3 是破坏性的（重启），且阈值设高（≥10 连续失败）

### 5.4 LLM 语义 niche-key 摧毁配额

**风险**：如果 niche-key 靠 LLM 的失败标签，LLM 可以换标签绕过配额。

**对策**：已在 §3.2.2 解决——niche-key 绑定到血统（确定性），不靠 LLM 语义。

### 5.5 frozen 模型诊断能力天花板

**风险**：frozen 模型的诊断能力本身有限，无法发现某些类型的改进方向。

**对策**：
- 四源梯度提供多视角（一个源看不到的，另一个源可能看到）
- 评估前沿移动让模型面对新任务，可能激发新的诊断角度
- 诚实承认这是最终天花板，论文不 overclaim

---

## 6. 实现优先级

### Phase 1：最小可行系统（验证核心机制）
1. 单 island + 四源梯度 + 诊断-编辑解耦
2. 多 rollout 显著性门控 + 单调精英
3. 基础 negative archive（无软过期）
4. 对比实验：vs SkillOpt 原始流程

### Phase 2：多样性与 scaling
5. K-island portfolio + 确定性配额
6. 三级停滞响应
7. cross-island fusion 算子
8. 对比实验：K=1 vs K=4 vs K=8

### Phase 3：天花板推高
9. 评估前沿移动（课程化）
10. held-out 池轮换
11. 对比实验：固定 held-out vs 移动前沿

### Phase 4：消融实验
12. 逐一关闭各机制组件，量化贡献
13. compute scaling 曲线（固定任务 vs 移动前沿）
14. 与 MCTS baseline / QD baseline 的对比

---

## 7. 关键设计决策待定清单

| 决策 | 选项 | 倾向 | 需实验 |
|---|---|---|---|
| K 的值 | 4 / 8 / 动态 | 固定 K=4 起步 | 是 |
| 显著性检验方法 | paired t-test / Wilcoxon / bootstrap | Wilcoxon（不假设正态） | 是 |
| N_ROLLOUTS（每次晋升多少次 rollout） | 3 / 5 / 10 | 5 起步 | 是 |
| stagnation 窗口大小 | 3 / 5 / 8 | 5 | 是 |
| negative archive 软过期步数 M | 10 / 20 / 50 | 20 | 是 |
| 评估前沿推进阈值 | 0.7 / 0.8 / 0.9 | 0.8 | 是 |
| held-out 池数 P | 3 / 5 | 3 | 是 |
| 梯度源轮转策略 | round-robin / 按最近信息量 | round-robin（简单先） | 是 |
