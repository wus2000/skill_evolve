# L1 战略机制改进方案 (v2)

## 一、问题背景

首次实验中 CSS 系统在一轮后终止：L0 饱和后 l1_signals=0 → decide_branch 返回 NONE → 搜索树耗尽。根本原因：L1 信号的三重统计门槛过严（max occurrence_rate=5.7%，远低于 15% 阈值），且旧的策略推导机制是一次性 prompt 生成，缺乏假设-验证循环。

## 二、核心改进：L1 假设-测试-验证循环

### 2.1 触发条件

**L0 饱和即触发 L1**。不再依赖 l1_signals 统计门槛。

`decide_branch` 简化为：
- L0 未饱和 → EXPLOITATION
- L0 饱和 → 进入 L1 循环

### 2.2 L1 循环与 MCTS 树的关系

L1 循环是节点创建前的「策略锻造」过程。内部的聚焦测试是草稿性质的临时实验。**只有最终通过验证的策略才创建正式 MCTS 子节点**，进入 L0 exploitation。

### 2.3 五步循环流程

```
触发: L0 饱和
    ↓
Step 1: 多维分析（1a/1b/1c 并行 → 1d 综合）
    ↓
Step 2: 策略提案 + 预判（1 次 LLM 调用）
    ↓
Step 3: 聚焦测试（新策略 + 空 rules 在诊断子集上执行）
    ↓
Step 4: 验证（逐轨迹 Judge 并行 → 整体诊断聚合）
    ↓
Step 5: 迭代控制
  → strategy_effective=true → 创建 MCTS 子节点
  → adherence_failure → IterationContext → 回到 Step 2
  → hypothesis_failure → IterationContext → 回到 Step 1
  → partial_success → IterationContext → 回到 Step 2
  → 超过 max_iterations → negative archive + saturated
```

---

## 三、Step 1：多维分析

### 3.1 Step 1a — L0 天花板分析

**输入：**
- Score 轨迹摘要（每步 accept/reject、score 变化）
- 当前 rules.md（L0 的最佳成果）
- 最后 N 步被拒绝的 merged_edits（天花板证据）
- 当前 strategy.md

**分析目标：** 当前策略框架的什么特性构成了 L0 无法突破的天花板？

### 3.2 Step 1b — 失败轨迹深度分析

**输入：**
- 3-5 条代表性失败轨迹（按 pattern 覆盖度选取：取 top-K 失败 pattern，每个选一条覆盖它的轨迹，优先选被多 pattern 共同覆盖的轨迹）
- 对应的任务描述

**分析目标：** agent 在这些轨迹中从什么节点开始走向失败？行为模式的共性是什么？

### 3.3 Step 1c — L0 对比分析局限性审视

**输入：**
- L0 的 contrastive analyst 已有诊断
- L0 尝试但被拒绝的相关规则
- mixed 任务组的成功-失败轨迹对

**分析目标：** L0 的对比分析发现的差异为什么用规则修复没效？差异是规则能覆盖的，还是需要策略层面的改变？

### 3.4 Step 1d — 假设综合

**输入：** 1a + 1b + 1c 的分析结果

**输出（结构化）：**
1. **当前策略的核心假设及局限** — 不是细节问题清单，而是策略层面的判断
2. **建议的策略变化方向**（2-3 条）— 每条包含变化维度 + 方向 + 预期影响的任务类型
3. **约束和不变量** — 当前策略中有效的部分，不应被破坏

**关键要求：** 必须跳出细节层面的反思，为后续策略产出提供策略层面的有价值信息。

**并行化：** 1a/1b/1c 无依赖关系，并行执行。1d 等待三者完成后执行。

---

## 四、Step 2：策略提案 + 预判

### 4.1 输入

- Step 1d 的假设文档
- 当前 strategy.md（参考）
- 当前 rules.md（参考）
- Step 1a 的 L0 天花板分析摘要
- IterationContext（如有，包含之前所有轮次的诊断结果）

### 4.2 输出 JSON Schema

```json
{
  "strategy_text": "完整的 strategy.md 正文（两段式格式）",
  "design_reasoning": "策略设计者的推理论述",
  "adherence_criteria": [
    {
      "id": "AC-1",
      "expected_behavior_pattern": "在这个策略下 agent 应该呈现的行为逻辑/模式",
      "current_behavior_contrast": "当前策略下 agent 在同类情况中的典型行为（对比基线）"
    }
  ],
  "improvement_expectations": [
    {
      "id": "IE-1",
      "target_problem": "这个策略要解决的具体失败模式/弱点",
      "improvement_mechanism": "策略通过什么行为变化来改善这个问题",
      "trajectory_evidence": "如果改善发生了，在 agent 执行轨迹中应该能观察到什么"
    }
  ]
}
```

### 4.3 核心设计原则

**策略遵守标准（adherence_criteria）**：LLM 提出策略后，基于自己的推理自然知道 agent 应该呈现怎样的行为逻辑。验证就是看 agent 是否按这个预期在行动。每条标准附带 current_behavior_contrast 作为 Judge 的对比锚点。

**改善预期（improvement_expectations）**：策略设计者认为应该改善的具体方面。trajectory_evidence 必须描述在轨迹文本（[role]/content 消息序列）中可观察的内容。

**质量要求：** 所有标准和预期必须是行为范式层面的（不是 L0 规则层面的），且必须有利于 LLM-as-Judge 高质量判断。adherence_criteria 2-3 条，improvement_expectations 2-3 条。

### 4.4 System Prompt 要点

- 角色：L1 策略设计师
- 策略文档格式规范：两段式（见第七节）
- 明确区分策略 vs 规则（策略是认知范式，规则是操作指南）
- 验证标准的质量要求：范式层面、可观察、有对比基线
- 不引用评估分数，基于轨迹行为判断

---

## 五、Step 3：聚焦测试

### 5.1 任务选取

- **诊断任务**：从持续失败任务中按 improvement_expectations 的 target_problem 匹配选取
- **回归检查**：补充少量当前能通过的代表性任务
- **总量**：15-25 个

### 5.2 执行配置

- **策略**：Step 2 产出的新 strategy_text
- **规则**：空 rules（纯粹验证策略本身的行为影响）
- **rollout**：标准 agent 执行流程

### 5.3 产出

完整轨迹（TaskResult 含 messages 列表 + 任务描述 + outcome），全量落盘不截断。

---

## 六、Step 4：验证

### 6.1 两层验证结构

**第一层：逐轨迹 Judge（并行）**

每条轨迹独立处理。输入：一条完整轨迹 + Step 2 的 adherence_criteria 和 improvement_expectations。

工作流程：
1. **定向证据提取**：对照每条标准，从轨迹中提取 agent 在相关方面的实际行为表现
2. **基于证据做判断**：对每条标准做 verdict

输出：
```json
{
  "task_id": "task_042",
  "task_type": "...",
  "outcome": "pass/fail",
  "criteria_assessments": [
    {
      "criterion_id": "AC-1",
      "behavioral_evidence": "轨迹中 agent 的实际行为描述",
      "verdict": "adhered | not_adhered | partial",
      "analysis": "判断理由"
    }
  ],
  "expectation_assessments": [
    {
      "expectation_id": "IE-1",
      "behavioral_evidence": "轨迹中的改善相关证据",
      "verdict": "improved | not_improved | inconclusive",
      "analysis": "判断理由"
    }
  ]
}
```

**第二层：整体诊断聚合（单次调用）**

输入：所有轨迹的 per-trajectory verdict + Step 2 完整产出。

输出：
```json
{
  "adherence_verdicts": [
    {
      "criterion_id": "AC-1",
      "verdict": "adhered | not_adhered | partial",
      "evidence": "跨轨迹的模式总结",
      "analysis": "整体判断理由"
    }
  ],
  "improvement_verdicts": [
    {
      "expectation_id": "IE-1",
      "verdict": "improved | not_improved | inconclusive",
      "evidence": "跨轨迹的改善模式",
      "analysis": "整体判断理由"
    }
  ],
  "overall_diagnosis": {
    "strategy_effective": true/false,
    "primary_issue": "none | adherence_failure | hypothesis_failure | partial_success",
    "diagnosis_detail": "问题具体出在哪里",
    "iteration_suggestion": "对下一轮迭代的建议方向"
  }
}
```

---

## 七、策略文档格式规范

### 7.1 两段式结构

```markdown
## {策略名称}

{一段话概述策略的整体内容——核心认知范式是什么，agent 应该以怎样的思维方式处理任务}

### 详述

{具体展开描述策略——这个思维范式在实际任务中意味着什么，它的各个方面如何协同工作。
保持连贯叙事，根据需要多段展开，不变成条目列表或操作指南。}
```

### 7.2 规范要求

- 策略描述认知范式（HOW to think），不是操作规则（WHAT to do）
- 概述段落精炼，让 agent 读完就理解核心思维方式
- 详述保持连贯叙事，不变成条目列表
- 策略名称简短，概括核心思想

### 7.3 适用范围

- L1 PROPOSAL/REFINE 的策略产出
- Cold start 的初始策略推导
- 两者使用相同的格式规范

---

## 八、L0 战术层改进：Section 级编辑

### 8.1 rules.md 结构

rules.md 由 `###` section 组织（因为在最终 skill document 中，rules.md 整体作为 `## Rules` 二级标题下的内容）。每个 section 有一个主题标题，内部是自由形式 markdown。

### 8.2 edit 操作

- `add_section`：添加一个新 `###` section（标题 + 自由形式内容）
- `rewrite_section`：重写一个已有 section（通过 `###` 标题定位，产出新的完整 section 内容）
- `delete_section`：删除一个 `###` section

### 8.3 reflect 步骤调整

- **不再限制** edit 为「一条 bullet point 规则」
- 引导 LLM 以最自然有效的形式组织经验：段落、指南、决策流程、对比说明等
- 每个 edit 是一个 section 级的内容块，有明确的主题范围
- 已有 section 主题重叠时用 `rewrite_section` 改进扩充，不新增重复 section

### 8.4 aggregate 步骤调整

- 聚合粒度从「单条规则去重」变为「section 级合并」
- 多个分析师提出的同主题内容合并为一个 section
- 检查与已有 section 的主题重叠

---

## 九、MCTS 节点创建

### 9.1 L1 成功 — 创建子节点

- **新策略**：L1 循环验证通过的 strategy_text
- **规则继承**：
  - PROPOSAL：不继承规则，空 rules 启动
  - REFINE：语义判断继承不冲突的有效规则
- **初始状态**：空 pattern 库、空 step_buffer（不继承 Step 3 的聚焦测试数据）
- 新节点进入 L0 exploitation

### 9.2 L1 失败 — 终止处理

- 超过 max_iterations（2-3 轮）仍未验证通过
- 失败方向写入 negative archive
- 当前节点标记为 saturated
- 搜索树尝试其他活跃节点；无活跃节点 → 终止

---

## 十、迭代反馈机制

### 10.1 IterationContext 结构

```json
{
  "iteration_round": 2,
  "previous_attempts": [
    {
      "round": 1,
      "strategy_summary": "上一轮策略的核心思路",
      "diagnosis": "adherence_failure | hypothesis_failure | partial_success",
      "adherence_results": [
        {
          "criterion_id": "AC-1",
          "verdict": "not_adhered",
          "cross_trajectory_pattern": "跨轨迹的行为模式总结",
          "key_evidence": "代表性证据摘要"
        }
      ],
      "improvement_results": [
        {
          "expectation_id": "IE-1",
          "verdict": "not_improved",
          "cross_trajectory_pattern": "...",
          "key_evidence": "..."
        }
      ],
      "judge_diagnosis": "问题定位",
      "judge_suggestion": "建议方向",
      "excluded_direction": "排除的方向描述（如有）"
    }
  ]
}
```

### 10.2 不同诊断的迭代路径

| 诊断 | 回到 | 新增输入 | 目标 |
|------|------|---------|------|
| adherence_failure | Step 2 | IterationContext（含 agent 实际行为证据） | 重写策略措辞使 agent 理解 |
| hypothesis_failure | Step 1 | IterationContext（含失败方向排除） | 重新分析，换策略方向 |
| partial_success | Step 2 | IterationContext（含有效/无效部分分析） | 在当前方向上微调策略 |

### 10.3 质量保证

- 保留所有历史轮次的诊断，避免重复错误
- cross_trajectory_pattern 从聚合 Judge 产出，比单条 verdict 更有信息量
- judge_diagnosis + judge_suggestion 提供具体可操作的迭代方向
- key_evidence 从 per-trajectory 评判中提取最具代表性的证据

---

## 十一、持久化和审计

所有中间产物完整落盘，目录结构：

```
runs/{run_id}/{node_id}/l1_cycle/
  round_{N}/
    step1/
      1a_ceiling_analysis.json
      1b_trajectory_analysis.json
      1c_contrastive_review.json
      1d_hypothesis.json
    step2/
      strategy_proposal.json       # 含 strategy_text, adherence_criteria, improvement_expectations
    step3/
      rollout/                     # 完整轨迹，不截断
        task_{id}_rollout_{k}.json
    step4/
      per_trajectory/              # 每条轨迹的 Judge verdict
        task_{id}.json
      aggregated_verdict.json      # 整体诊断
    iteration_context.json         # 如有迭代，记录反馈信息
  final_outcome.json               # 最终结果（成功/失败 + 原因）
```

不截断任何轨迹内容，支持人工审计全流程。

---

## 十二、需要同步修改的现有代码

1. **css/tree/branching.py** — `decide_branch()` 简化触发逻辑
2. **css/proposal/proposal.py** — `run_proposal()` 和 `run_refine()` 重写为 L1 循环
3. **css/proposal/derivation.py** — 策略推导 prompt 更新为两段式格式
4. **css/coldstart.py** — 初始策略推导使用新格式规范
5. **css/optimizer/reflect.py** — reflect prompt 放开格式约束，支持 section 级产出
6. **css/optimizer/edit_engine.py** — 新增 section 级 edit 操作
7. **css/optimizer/aggregate.py** — section 级合并逻辑
8. **css/orchestrator.py** — 集成 L1 循环，落盘逻辑
9. **css/config.py** — 新增 L1 循环相关配置参数
