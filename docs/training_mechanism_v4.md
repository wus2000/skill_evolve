# 训练机制 v4：双层搜索统一方案

> 基于 claude-lead + claude-worker 两轮对抗式审计（共 15+ agent）+ 8 库代码实证。
> v3 被用户正确识别为「丢失了 Level 1 认知策略搜索」——v4 修复这个根本缺陷。
> v3 的全部机制作为 Level 0 内层保留；v4 新增 Level 1 外层作为论文核心贡献。

---

## 0. 为什么需要 v4：v3 的根本缺陷

v3 设计了优秀的 Level 0 机制（四源梯度、K-island、显著性门控、评估前沿），但**完全丢失了 Level 1 认知策略搜索**——本应是论文的核心贡献。

v3 本质上是「更好的 SkillOpt × K 份拷贝」：每个 island 都在做同一类事——从轨迹中提取战术规则。K-island 提供的是并行度，不是认知多样性。

**代码实证（最硬的发现）**：SkillGrad 的 `patcher.py` L120-123 早已开放 "rewrite from scratch in place"（最高幅度档），但输出仍是 Level 0（WORKFLOW-THEME 落地为一个 H2 checklist 条目）。**钳住 Level 0 的不是幅度上限，是编辑的语义定义**（"HOW not HOW MUCH"）。→ 换帧需要独立机制，光放大编辑幅度产不出来。

---

## 1. 核心框架：双层两生成器

### 1.1 两个层级

| 层级 | 搜索什么 | 类比 | SkillOpt 能做？ |
|---|---|---|---|
| **Level 1（认知框架）** | agent 的思维方式——HOW to think | 换模型架构 | **不能**——始终在同一框架内修补 |
| **Level 0（战术知识）** | 框架内的具体规则——WHAT rules | 调超参/加数据 | **能**——这正是它擅长的 |

**具体例子**：

```
Level 1 帧 A「先规划后执行」：
  面对任务 → 完整阅读 → 构建心智模型 → 制定分步计划 → 按计划执行 → 验证
  
Level 1 帧 B「假设驱动」：
  面对任务 → 对答案做假设 → 设计实验验证/否证 → 迭代假设 → 收敛
  
Level 1 帧 C「从输出倒推」：
  面对任务 → 明确期望输出格式 → 倒推每步应产生什么 → 自底向上构建

Level 0 规则（可在任何帧下工作）：
  「遇到 pandas DataFrame 先 df.info()」
  「数值比较前检查数据类型」
  「修改文件前先备份」
```

帧 A、B、C 是质的不同——不是增删规则的区别，而是整个推理链路的重组。

### 1.2 两个生成器

共享同一个 frozen-LLM 引擎，但信号源/输出语义/去重坐标完全不同：

```
            生成器（产新内容）                   分配器（分配 compute，不产新内容）
Level 0:    G0 = 轨迹诊断 → 战术规则补丁         K-island/branch-quota
Level 1:    G1 = 帧提议器 → 认知框架              帧间 compute 调度
```

| 维度 | G0（Level 0 生成器） | G1（Level 1 生成器） |
|---|---|---|
| **输入** | 单条/几条具体 rollout 轨迹 | 跨轨迹失败模式 + 当前帧假设分析 + 先验知识 |
| **问题** | 「这次为什么失败？加什么规则？」 | 「这个帧的结构性盲区是什么？什么质的不同的思维方式能覆盖它？」 |
| **输出** | 战术 patch（一条规则、一个修改） | 全新认知框架（推理模式 + 注意力分配 + 错误恢复 + 元认知） |
| **去重坐标** | anchor/pattern-slug（规则级） | {assumption, mechanism-class, analogy}（帧级） |
| **信号源** | 外部任务世界（每轮新失败） | 帧假设取反 + 跨域类比 + 目标态反推 + 失败签名 |

### 1.3 为什么 G1 不是「大号 G0」

三个 G0 没有的机制环节（缺任一个，输出就坍回 Level 0）：

1. **瓶颈类别分类器**：把 persistent_fail 映射到 7 轴 bottleneck CLASS
   - wrong-retrieval / wrong-reasoning / wrong-stopping / wrong-representation / wrong-objective / wrong-action-space / wrong-credit-assignment
   - 把失败从「哪条规则该补」抬到「认知框架的哪个维度坏了」

2. **Level-0 排斥过滤器**（来自 Arbor §1/§6 的设计）：
   - 候选若是「单个数字/knob/改 prompt 措辞/more X」→ kill
   - **这正是 SkillGrad 缺失的**——它幅度拧满仍产 Level 0，因为没有过滤器强制产出帧级变化

3. **帧坐标去重**：
   - 按 {assumption, mechanism-class, analogy} 去重（帧坐标）
   - 不是 SkillGrad 的 anchor/pattern-slug（规则坐标）
   - 使用开放可增长码本，非固定预设

### 1.4 Level 1 解决了什么问题

**Level 1 搜索解决了 v3 的核心 scaling 难题——「生成器枯竭」**：

```
帧 F1: G0 优化 [████████████▓▓░░ 边际信息→0，Level 0 饱和]
                                  ↓ G0 饱和触发 G1
帧 F2:                   G0 优化 [████████████▓▓░░ 新帧下 G0 重获信息]
                                                  ↓ 再次触发 G1
帧 F3:                                   G0 优化 [████████████...

全局精英：max(F1_best, F2_best, F3_best, ...)  ← 单调不退化
```

总可改进量 ≈ M（有效帧数）× 单帧 Level-0 余量。各帧锯齿之和 >> 单帧单调衰减曲线。

---

## 2. 外层机制：G1 帧提议器

### 2.1 G1 的四个信号源

| Move | 信号源 | 与轨迹的关系 | 描述 |
|---|---|---|---|
| **A 假设反转** | 当前帧的承重假设 | **解耦** | 识别当前帧隐含依赖的假设（如「任务可分解」），取反产出候选帧（如「全局优化，不分解」） |
| **B 目标态反推** | benchmark 理想终态 | **解耦** | 想象任务已被完美解决，倒推「解法管线中缺失的是哪个认知阶段」 |
| **C 类比迁移** | 跨域机制库 | **完全解耦** | 从 {SAT/CSP, beam search, debate, program synthesis, control theory, ...} 中迁移问题解决范式 |
| **D 失败聚类** | persistent_fail 簇心 | **唯一耦合** | 轨迹模式的触发与定向——「哪类失败说明当前帧错了」 |

**关键洞察**：G1 的主力信号（A/B/C）与单条轨迹完全解耦。轨迹模式（D）只负责**触发与定向**——「何时该换帧、往哪个方向换」。**帧的内容值来自 frozen-LLM 的范式组合能力**，不来自轨迹。

### 2.2 G1 的完整工作流程

```
Step 1: 触发判定（机制化，非 prompt 注入）
  ├── persistent_fail 簇是否存在？（SkillOpt slow_update 的分类）
  ├── 最近 N 次 Level-0 patch 后 held-out 通过率有无显著提升？
  │   （多 rollout 显著性检验，防高方差误触发）
  └── 若 persistent_fail 存在 AND Level-0 无显著提升 → 触发 G1

Step 2: 瓶颈分类
  ├── persistent_fail 簇 → 7 轴 bottleneck CLASS
  └── 识别「当前帧的哪个认知维度是瓶颈」

Step 3: 帧生成（四个 Move 并行/轮转）
  ├── Move A：当前帧对瓶颈轴的假设是什么？取反。
  ├── Move B：如果 benchmark 已完美解决，需要什么认知管线？
  ├── Move C：哪个跨域范式能处理这类瓶颈？
  └── Move D：失败簇心指向什么样的替代推理模式？

Step 4: Level-0 排斥过滤
  ├── 候选是「单条规则/参数调整/prompt 措辞变化」？→ kill
  └── 只保留结构性改变推理方式的候选

Step 5: 帧坐标去重
  ├── 用 {assumption, mechanism-class, analogy} 坐标
  ├── 开放可增长码本（不预设固定帧空间）
  └── 与 negative archive 中已失败的帧坐标对比

Step 6: 新帧初始化 → 开 island → 进入 Level 0 内层优化
```

### 2.3 帧无关知识的跨帧继承

换帧时不应丢弃所有 Level 0 知识：

```python
class FrameAgnosticPool:
    """存储帧无关的战术知识，跨帧继承"""
    
    # 每条 Level-0 规则标注其依赖的假设
    # 示例：
    rules = [
        {
            "rule": "openpyxl 修改后必须手动 recalculate formulas",
            "assumption_dependency": None,  # 帧无关 → 继承
        },
        {
            "rule": "面对复杂任务先分解为 3-5 个子任务",
            "assumption_dependency": "task-decomposability",  # 帧相关 → 不继承到取反了这个假设的新帧
        },
    ]
    
    def inherit_to_new_frame(self, new_frame_assumptions):
        """只继承与新帧假设不冲突的规则"""
        return [r for r in self.rules 
                if r["assumption_dependency"] is None 
                or r["assumption_dependency"] not in new_frame_assumptions.negated]
```

---

## 3. 内层机制：Level 0（v3 原样复用）

**每个帧独立运行一套完整的 Level 0 优化**：

### 3.1 四源轨迹梯度 G0

（与 v3 §3.1 完全相同）
- 失败诊断（SkillGrad 模式）
- 对比诊断（同任务基线 vs 当前）
- 纵向对照（SkillOpt slow_update 模式）
- 成功路径反推（Trace2Skill 模式）
- 诊断-编辑解耦（textgrad 模式）

### 3.2 显著性门控 Held-out 晋升

（与 v3 §3.3 完全相同）
- 多 rollout 显著性检验
- POLCA trick（当前精英作为候选之一）
- Held-out 池轮换防过拟合

### 3.3 单调精英

全局精英 = max(所有帧所有 island 的最佳 skill)。绝不退化。

### 3.4 Negative Archive + 梯度记忆

（与 v3 §3.1.3 相同，加软过期防误封）

### 3.5 确定性 Niche 配额

（与 v3 §3.2 相同，血统制 + 硬限额）

---

## 4. 两层的连接：触发、晋升、正反馈

### 4.1 G0 饱和 → 触发 G1（正反馈律）

这是与 MLEvolve/Arbor 最关键的设计分歧——替换退火/停机的负反馈为正反馈：

```python
def check_frame_switch(current_frame):
    # 1. persistent_fail 簇是否存在
    pf = get_persistent_fail_cluster(current_frame.held_out_results)
    if not pf:
        return NO_SWITCH  # 没有系统性失败，继续 Level 0
    
    # 2. Level 0 是否仍在改进（显著性检验，非单步比较）
    recent_promotions = current_frame.recent_promote_attempts[-WINDOW:]
    if any(p.result == PROMOTE for p in recent_promotions):
        return NO_SWITCH  # Level 0 仍在产有效 patch，继续
    
    # 3. 触发 G1
    return TRIGGER_G1(bottleneck=classify_bottleneck(pf))
```

**机制化要求**（同 v3，但现在触发目标是 G1 而非 Tier 2/3 升级）：
- 必须基于多步窗口判定（防高方差误触发）
- 必须有显著性检验（不是「最近一步没改进」）
- 触发后的动作是确定性的（开 G1 流程，不是注入 prompt 字符串）

### 4.2 帧级晋升门

比 Level 0 晋升更严格——需要两个条件同时满足：

```python
def try_promote_frame(candidate_frame, global_elite):
    # 条件 1：Held-out 多 rollout 显著优于当前全局精英
    score_test = significance_test(
        candidate_frame.best_skill_scores, 
        global_elite.scores
    )
    
    # 条件 2：行为级验证——agent 的 HOW 真的换了
    # 防止「换了顶层文字但行为没变」
    behavior_test = detect_control_flow_change(
        candidate_frame.trajectories,
        global_elite.trajectories
    )
    
    return score_test.significant AND behavior_test.novel_structure_detected
```

### 4.3 评估前沿移动（同时驱动两层）

（v3 §3.5 的设计保留，但现在角色更清晰）

评估前沿移动**同时喂两个生成器**：
- **对 G0**：新任务 → 新失败 → 新规则信号（延缓 Level 0 饱和）
- **对 G1**：新任务 → 新 bottleneck 类 → 可能逼出新帧（延缓 Level 1 饱和）

---

## 5. 完整架构图

```
┌─────────────────────────────────────────────────────────────────┐
│                    评估前沿移动（真正的 scaling 杠杆）              │
│                同时喂 G0（新规则信号）和 G1（新帧信号）              │
└───────────────┬─────────────────────────────────┬───────────────┘
                │                                  │
    ┌───────────▼────────────────┐     ┌──────────▼──────────────┐
    │ G1 帧提议器（Level 1 生成器）│     │  帧间 compute 调度       │
    │                             │     │  （Level 1 分配器）       │
    │ persistent_fail 簇           │     │                         │
    │   → 瓶颈 CLASS 分类         │     │  帧级晋升门              │
    │   → 假设反转 / 类比 / 反推   │     │  （score + behavior）   │
    │   → Level-0 排斥过滤        │     │                         │
    │   → 帧坐标去重              │     │  全局单调精英             │
    └───────────┬────────────────┘     └──────────┬──────────────┘
                │ 产出新帧                          │ 选择投哪个帧
                ▼                                  ▼
    ┌─────────────────────────────────────────────────────────────┐
    │              帧 F1              帧 F2           帧 F3       │
    │           ┌────────┐        ┌────────┐      ┌────────┐     │
    │           │ Level 0│        │ Level 0│      │ Level 0│     │
    │           │ 内层   │        │ 内层   │      │ 内层   │     │
    │           │        │        │        │      │        │     │
    │           │ G0 四源 │        │ G0 四源│      │ G0 四源│     │
    │           │ 梯度   │        │ 梯度   │      │ 梯度   │     │
    │           │        │        │        │      │        │     │
    │           │ K-island│       │ K-island│     │ K-island│    │
    │           │ 显著性门│        │ 显著性门│      │ 显著性门│    │
    │           │ neg arch│       │ neg arch│     │ neg arch│    │
    │           └────────┘        └────────┘      └────────┘     │
    │                                                             │
    │           FrameAgnosticPool（帧无关知识跨帧继承）              │
    └─────────────────────────────────────────────────────────────┘
```

---

## 6. Skill Document 结构

```markdown
# === 认知框架（Level 1 — G1 搜索的对象）===

## 核心推理模式
[当前帧的推理模板——如何分解问题、何时验证、如何恢复错误]

## 注意力分配策略
[面对任务时的优先级——先全局还是先细节、先输入还是先输出]

## 元认知规则
[何时判断当前路径不通、何时决定换策略、何时停止]

# === 战术知识（Level 0 — G0 搜索的对象）===

## 数据分析任务
[具体规则、heuristics、patterns]

## 文件操作任务
[具体规则、heuristics、patterns]

## 工具使用
[具体的 API 知识、常见陷阱]

# === 帧无关知识池 ===
<!-- 跨帧继承的环境/工具知识 -->
[openpyxl 公式重算、pandas 类型检查、...]

<!-- SLOW_UPDATE_START -->
[epoch 级纵向指导]
<!-- SLOW_UPDATE_END -->
```

---

## 7. 与 SkillOpt 的核心对比（论文叙事）

| 维度 | SkillOpt | 本方案 |
|---|---|---|
| **搜索层级** | 仅 Level 0（战术规则积累） | **Level 1（认知框架搜索）+ Level 0（框架内优化）** |
| **坍缩根因** | 单路径 + gate→0 + 梯度枯竭 | Level 0 饱和 → 触发 Level 1 换帧（正反馈） |
| **生成器** | 单一（轨迹诊断 → 规则） | **双生成器（G0 规则 + G1 帧提议）** |
| **G1 信号** | 不存在 | 假设反转 + 类比迁移 + 目标态反推 + 失败签名 |
| **Compute scaling** | 单帧内 Level 0 饱和即停 | 帧切换重置信息曲线，M×延长有效优化区间 |
| **天花板** | 初始帧的规则空间 | M 帧的联合覆盖 + 评估前沿推高 |

---

## 8. 论文 Contribution 的诚实边界

### 8.1 能站住的 claim

> **"Frame-level search escapes the Level-0 plateau"**
> 
> 帧级搜索使 agent 逃出 Level 0 的局部最优。
> 
> 证据：帧跳变处的 held-out 分数阶跃 + 被访问的不同帧坐标数 + 行为级 control-flow 结构变化

### 8.2 不能声称的

- ~~「Level 1 随 compute 无限提升」~~ — 帧空间可能 O(10)，Level 1 也会饱和
- ~~「超越 frozen 模型的认知能力上限」~~ — G1 的帧内容来自同一个 frozen LLM
- ~~「MCTS/QD 带来 anti-collapse」~~ — 已证伪

### 8.3 诚实报告的开放问题

> 「帧能否随 compute/课程持续新增」是开放的经验问题。
> 论文用帧坐标码本的增长曲线如实报告——该测而非该断言。

---

## 9. 可证伪化与判决实验

### 9.1 帧的外延定义

一个 skill 改动算「帧级」iff：
1. 它在 agent 执行轨迹上产生**新的、可机器检测的 control-flow 结构特征**
   - 例：先前从不出现的 verify-then-accept 回路
   - 例：belief-state 多轮条件检索
   - 例：DAG 子任务分解
2. 且引起 held-out persistent_fail 簇分布的**显著迁移**

使用**开放可增长码本**（每发现一个真新结构特征就扩充），非固定预设。

### 9.2 判决 ablation

| 实验 | 量化指标 | 预期 |
|---|---|---|
| `amplitude_cap ∈ {1,2,3,4} × frame-search on/off` | 帧坐标数 + held-out 阶跃 | frame-search=on 时出现 Level 0 够不到的分数跳变 |
| Level 0 only vs Level 0+1 | 固定 compute 下的最终分数 | 双层 > 单层，差距在 Level 0 plateau 后拉开 |
| 帧继承 on/off | 新帧起步分数 | 继承 > 不继承（帧无关知识的价值） |
| 评估前沿 fixed vs moving | 帧坐标增长曲线 + 最终分数 | moving 同时延缓 G0 和 G1 饱和 |

### 9.3 Compute Scaling 曲线的预期形态

```
Score
  │         ┌── Level 0+1 + 移动前沿
  │        ╱    （锯齿上升，每次帧切换有阶跃）
  │      ╱╱
  │    ╱╱       ┌── Level 0+1 + 固定前沿
  │  ╱╱        ╱    （锯齿但最终饱和）
  │╱╱        ╱╱
  │╱       ╱╱  ┌── Level 0 only (SkillOpt)
  │      ╱╱  ╱     （单调递减边际收益，早停）
  │    ╱╱ ╱╱
  │──╱╱╱╱───────────────────────
  └──────────────────────────── Compute →
```

---

## 10. 红队识别的风险 + 对策

### 10.1 Level 1 自身饱和

**风险**：有效帧数可能 O(10)，Level 1 也会枯竭。
**对策**：
- 评估前沿移动同时喂 G1（新 bottleneck → 可能逼出新帧）
- 帧间 fusion（两个部分成功的帧的交叉组合 → 新帧候选）
- 诚实报告码本增长曲线，不 overclaim

### 10.2 「帧级变化」可能是假的

**风险**：G1 产出看似新帧但 agent 行为没变（换了文字没换思维）。
**对策**：帧级晋升门要求行为级 control-flow 结构检测（§4.2）

### 10.3 帧无关知识标注的准确性

**风险**：assumption-dependency 标注依赖 LLM，可能误标。
**对策**：
- 保守策略：只对高置信度判断做继承，不确定的规则不继承
- 最终由 held-out 门控决定继承质量

### 10.4 v3 已识别的风险（仍然适用）

- negative archive 误封（→ 软过期 + 置信度阈值）
- held-out 过拟合（→ 池轮换 + 保留池）
- 评估高方差（→ 多 rollout 显著性检验）
- niche-key 确定性（→ 血统制）

---

## 11. 实现优先级

### Phase 0：验证 Level 1 概念（判决实验）
1. 手工构造 2-3 个质的不同的认知帧种子
2. 对比：固定帧 A + Level 0 优化 vs 固定帧 B + Level 0 优化 vs 帧搜索
3. 验证「不同帧确实在不同任务类型上表现不同」这个前提

### Phase 1：Level 0 内层（v3 的全部机制）
4. 四源梯度 + 诊断-编辑解耦
5. 多 rollout 显著性门控 + 单调精英
6. Negative archive + 梯度记忆
7. 对比实验 vs SkillOpt

### Phase 2：Level 1 外层
8. G1 帧提议器（瓶颈分类 + 四 Move + 排斥过滤 + 帧坐标去重）
9. G0 饱和→触发 G1 的正反馈机制
10. 帧无关知识继承
11. 帧级晋升门（score + behavior）
12. 判决 ablation：amplitude_cap × frame-search on/off

### Phase 3：Scaling
13. 评估前沿移动
14. Compute scaling 曲线
15. 帧坐标码本增长曲线
16. 消融实验

---

## 12. 关键设计决策待定清单

| 决策 | 选项 | 倾向 | 需实验 |
|---|---|---|---|
| 初始帧种子数 | 2 / 3 / 5 | 3 起步 | 是 |
| G1 触发窗口大小 | 3 / 5 / 8 步 | 5 步 | 是 |
| Level-0 排斥过滤的判据 | LLM 判断 / 规则匹配 / 混合 | LLM 判断 + 帧坐标变化检测 | 是 |
| 帧间 fusion 策略 | 自由交叉 / 按 bottleneck 轴对齐 | 按 bottleneck 轴对齐 | 是 |
| 行为级结构检测方法 | 轨迹 pattern mining / LLM 判断 | pattern mining（确定性） | 是 |
| 帧无关知识的 assumption-tag 方法 | LLM 标注 / 规则系统 / 两步验证 | LLM 标注 + 保守继承 | 是 |
| 每帧内的 K-island 数 | 1 / 2 / 4 | 每帧 K=2 | 是 |
| G1 Move 轮转策略 | round-robin / 按 bottleneck 匹配 | 按 bottleneck 匹配 | 是 |

---

## 附录：从 v2 到 v4 的演化轨迹

| 版本 | 核心 | 被什么推翻 |
|---|---|---|
| **v2** | QD-Archive + 两层搜索（草案） | QD 的 4 个致命问题；机制缺乏代码实证 |
| **v3** | 四源梯度 + K-island + 显著性门控 + 评估前沿 | **丢失了 Level 1 认知策略搜索**（用户正确识别） |
| **v4** | 双层两生成器：G1 帧搜索（外层）+ G0 战术优化（内层，=v3） | 当前版本 |
