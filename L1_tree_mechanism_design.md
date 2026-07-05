# L1 策略搜索机制 · 完整设计(终审稿)

> 状态:设计定稿待终审(2026-07-05,六轮讨论收敛)。
> 取代:`L1_mechanism_design_v2.md`(假设-测试-验证循环)与代码中现行 L1 cycle v3
> (proposal.py 八轮考试);`L1_paradigm_redesign_plan.md` 草稿并入本文后废弃。
> 语言约定:本文档为设计评审文档(中文);全部部署级 prompt 与协议字段为英文。

---

## 0. 设计目标与证据背景

旧机制的三个实测失败,是本设计的出发点:

1. **ALFWorld 八轮考试**(runs/alfworld_20260703_2050):29 残差 + 32 回归,八轮
   1464 rollouts;7/8 轮 regression(−10~−21)压倒 lift(+4~+10),唯一 EFFECTIVE
   的 round 3 仅 net=0——"空 rules 一考定终身"测量的是"策略差 − 全部战术知识",
   信噪比被结构性摧毁。
2. **AppWorld 0 残差死局**(runs/appworld_20260704_171155):train 94% → lift≡0 →
   判据不可满足 → 八轮全灭后 `fallback_best_overall` 强行部署 net−1 候选并报
   success;暴露"PROPOSAL 必须产子"与"新节点 inf UCB 必选"的双重硬编码。
3. **Spreadsheet 冷启动**:单发 design→critique 整体重写,4547→2398 字符不成比例
   截除,零迭代零验证。

设计原则(六轮讨论收敛):

- **树自身是策略的评估器**:策略价值 = 其盆地在有限预算 burst 中的实际生长,
  不设树外考场;
- **材料详实、叙述为体**:一切分析以"每轨迹策略行为解读"为原子证据,段落叙述
  为主体,辅助字段为流转;拒绝卡片式标签精炼;
- **活文档累积综合**:跨 burst 融合而非覆盖,任何内容不得无声消失;
- **自由交给 agent,纪律交给结构**:探索无配额、生成不设立场模板;高度防火墙
  (策略层/战术层)以独立裁判 LLM 贯穿每一级产物;
- **中间产物全落盘**:步级断点恢复 + 人工可审。

---

## 1. 搜索机制(类 MCTS-UCB)

### 1.1 与经典 MCTS 的映射与偏差

| MCTS 概念 | 本设计对应物 |
|---|---|
| 节点 | 一份 strategy + 该节点自有的 rules 状态(持久,续跑)+ 档案 |
| 边(动作) | 产子:root→NEW 子;策略节点→REFINE 子 |
| Simulation | 一次 B=5 步的 L0 exploitation burst |
| 回报 | burst 内经 gate 接受的净 Δval |
| 最终答案 | 全树历史最优 (strategy, rules) 快照 |

四处对经典假设的偏差及处理:

1. **节点价值非平稳(随投入上升)**:不用终身平均 Q;选择分 =
   `val(当前高度) + α·slope_W(近期趋势) + β·√(ln T / n)`(select.py 现公式,
   n 改记 burst 数)。
2. **无终局回报**:以 burst 的 gate 裁决净位移为回报;gate(配对符号检验+升级)
   是现成的已校准降噪器,零新增测量机器。
3. **动作空间无穷(策略是生成的)**:产子由"饱和 + 度数配额"仲裁(取代
   progressive widening 公式);生成质量由材料体系(§2)与生成管线(§4)保证。
4. **模拟昂贵(每 burst 数小时)**:全 run 仅 20–40 次树决策;树注定浅
   (深度≤3),选择采用**全体活跃节点上的扁平 argmax**(现实现语义),不做
   根到叶分层下行——树的作用是谱系组织(证据继承/产子语义),不是分层 bandit。

### 1.2 节点三态机

| 状态 | 被选中时的行为 | 进入条件 |
|---|---|---|
| ACTIVE | 跑 B=5 步 L0 burst(从自身 rules checkpoint 续跑) | 出生即 ACTIVE |
| SATURATED | 产一个子节点并立即为其跑首个 burst(root→NEW;策略节点→REFINE) | 饱和判据(§1.3) |
| TERMINAL | 退出选择池 | 策略节点度数用尽;或 REFINE decline(无可论证 A/B 靶,§4.2 步 1)。root 永不 TERMINAL |

要点:

- **产子与首 burst 原子化**:孩子出生即带 n=1 的真实证据,选择池中**永不存在
  n=0 节点**——旧机制"新节点 inf UCB 必选"问题从结构上消灭(select.py 的
  `n<=0 → inf` 分支随之删除;run 起点只有 root 一个节点,首 burst 无需选择)。
- **饱和不杀节点**:val 冻结、slope→0,UCB 自然衰减;其成果已锁入 global_best。
  饱和节点的 UCB 分实为"它的下一个孩子值不值得生"的定价(高 val 盆地的孩子
  有好先验),语义自洽。
- **root = 空策略 bare agent**:root 自身也跑 L0(= 无策略纯战术优化,天然
  ablation 臂;其 val 序列即裸基线锚)。root 的"REFINE"就是 NEW(从零创生),
  全树只有一条统一产子规则。**冷启动不再是特例机制**:run 天然以 root 的
  bare burst 开局,root 饱和后的历次被选中逐个产出 NEW 子。css/coldstart.py
  的单发 design+critique 机制退役。

### 1.3 饱和判据(用户终裁 2026-07-05:连续干涸 burst,取代方案 A)

**判据:节点最近 `saturation_dry_bursts`(=2)个 burst 的 gate accept 数全为 0
→ 饱和**。实现记账在 `TreeNode.burst_accepts`(每 burst 一个 accept 计数,
对因硬错误提前结束的短 burst 也稳健);任何 accept(无论是否新 best)都算
盆地仍有产出,重置干涸计数。

- 单个 5 步零 accept burst 在 p(accept)=0.3 下有 0.7⁵≈17% 假饱和率——不足为凭;
  连续两个干涸 burst 假饱和率 ≈0.7¹⁰≈3%;
- (设计史:方案 A"跨 burst stall 计数器 ≥8"曾短暂采用,2026-07-05 用户终裁
  统一为本判据——更直观,且判定单位与决策单位[burst]对齐;`l0_stall_steps`
  自此为 legacy-inert,退出 resume 指纹,`saturation_dry_bursts` 入指纹。)
- 假饱和代价温和(不杀节点,只是提前产子,战术余量随 REFINE 继承迁移),无需
  更保守。

### 1.4 产子规则

- **NEW**:仅 root 可产。子节点 **rules 从空开始,零继承**(bounded-burst 评估
  看生长曲线而非一考定终身,空起步不再是致命伤;各根范式一律从零,可比性最干净)。
- **REFINE**:策略节点产。子节点 rules 由**继承裁决**流程决定(§4.2 步 5):
  LLM 依据新策略逐条三判(相容保留 / 冲突丢弃 / 最小改写后保留)。
- **度数**(直接子节点上限,超参):策略节点 3;**root 不限;深度不限**
  (用户拍板 2026-07-05:先实施跑起来观察)。终止语义随之变化:TERMINAL 仅
  两途(策略节点度尽 / REFINE decline),root 永不 TERMINAL,run 的实际终止
  = 总预算耗尽。观察点:root 无限配额下 NEW 的产出节奏由 β 探索项主导,
  深 REFINE 链是否出现,首环境实测后复核。
- burst 长度 B=5 固定(不中途早停,语义干净;留 knob)。

### 1.5 回报、选择与部署解耦

- burst 回报 r = burst 内 gate 接受步的净 val 位移(被拒候选不产生位移);
- **选择看"还能长多少"**(val+slope+不确定性),**部署看"长到过多高"**:
  global_best = 全树历史最优 (strategy, best_rules) 快照;
- 终止 = 全节点 TERMINAL 或总预算耗尽;
- 并发:初期 concurrency_limit=1(信号干净);多节点并行 burst(virtual loss)
  留作后续选项。

### 1.6 一次 run 的自然生命周期(示意)

```
burst(root)×k → root 饱和(裸基线锚定)
→ 选 root → NEW#1 出生+首burst → UCB:NEW#1 slope 高,连续获 burst
→ NEW#1 趋饱和,root 探索券回升 → 选 root → NEW#2 出生+首burst
→ NEW#1 饱和后被选中 → REFINE 子出生(继承裁决过的 rules)+首burst
→ …… → 预算尽/全 TERMINAL → 交付 global_best(strategy, rules)
```

---

## 2. 材料体系

### 2.1 目录总览

```
nodes/n0003/
  strategy.md                      部署原文(干净,无 meta)
  rules_best.md                    最佳 rules 快照(checkpoint 同步)
  dossier/
    rationale.md                   生成动机档案(含"预期行为变化清单"→事后对质)
    behavior_profile.md            策略层行为画像(活文档,跨 burst 累积)
    frontier_analysis.md           未解任务策略层局限分析(活文档,跨 burst 累积)
    bursts.jsonl                   机械账:每 burst 步数/accepts/Δval/gate 统计
  analysis/burst_03/               ← 每 burst 的管线中间产物(步级断点恢复)
    interpretations/traj_<id>.json    每轨迹解读(全量)
    interpretations/screen_verdicts.json
    grouping.json                     行为归组(含成段理由)
    group_analyses/<group>.json       组内深析
    burst_summary.md
    profile_update_diff.json          活文档融合对账(无声丢失核验)
    frontier_update_diff.json
  gen/                             ← 出生时生成管线中间产物
    exploration_ref.json / target_selection.json / concept_draft.md /
    novelty_verdict.json / altitude_check.json / inherit_decisions.json(REFINE)
global/
  unsolved/                        全局未解任务群跨策略综合(叙述体)
    grouping.json / group_<k>.md
  exploration/
    <group>/findings.md            按任务群缓存的探索发现(叙述体)
    sessions/session_<t>/          会话全档:plan/briefing/probes/*/report/session_meta.json
```

已删除(讨论中裁决):维度×立场坐标网格(index.md)、全局通用规则池
(universals.md)、立场组合生成模板、观察卡/诊断卡式标签精炼、解读抽样。

### 2.2 活文档累积语义(核心纪律)

behavior_profile.md 与 frontier_analysis.md 是**节点全生命期的认知综合体**:

- 每 burst 的更新 = 把新证据**融入**既有全部认识:仍成立的保留;被新证据修正的
  **显式改写并陈述因由**;确实过时的**显式退役并记录原因**——任何内容不得无声消失;
- 文档尾部维护**演化史**节(每 burst 一段:新认识/修正了什么旧判断)。时间维度
  本身是信号(行为随 rules 成熟的变化 = 策略-战术互动的一手证据);
- **无声丢失核验**(独立批判者,1 调用):新旧对照,旧版每条实质结论必须有下落
  (kept / revised / retired,后两者附理由),逐条对账协议
  `{old_claim, disposition, reason, new_claim?}` 落盘;
- 体量影响使用时做**有审计的 consolidation**(仍受无声丢失核验约束)。

### 2.3 解读-挖掘管线(每 burst)

协议设计总原则:**每个字段都必须说得出下游消费者是谁**;段落叙述为体,专用字段
为流转。长叙述内嵌 JSON 由统一 json_repair 兜底。

**第一层:每轨迹解读**(burst 内 on-policy train minibatch 轨迹**全量**,
5 步 × minibatch16 × K3 ≈ 240 条;val eval 轨迹只出分不解读):

```jsonc
{
  // 主字段(质量标准不降)
  "narrative": "(多段)本回合 agent 以怎样的整体方式对待任务;策略哪些指导在哪些
                关头可见地塑造了选择、哪些被无视/无从执行;该行为方式如何一步步
                导向结局(行为层因果)",              // → 组内深析全文精读
  "outcome_causality": "(一段)行为模式与成败的机理链", // → 优势/短板分析
  // 辅助字段(专设下游)
  "behavior_signature": "一句话行为模式刻画",           // → 归组索引
  "adherence": [{"section","verdict:followed|partial|ignored|inapplicable",
                 "evidence_steps","note"}],             // → adherence 台账
  "strategy_signals": [{"claim(完整句)","evidence_steps","confidence"}], // → profile 证据指针
  "anomalies": "意外事件/环境反馈",                     // → frontier 深析+探索线索
  "task_group_hint": "任务表面类型",                    // → 归组对照维度
  "key_steps": [ ... ]                                  // → 人工审阅定位
}
```

禁令入合同:不得输出"下次应在第 X 步做 Y"式战术处方(L0 反思管线的职权;
两条管线读同一批轨迹、各守各的高度)。

**Altitude Screen(可复用裁判部件)**:批量审(~10 份/调用),四判据——
①论述对象是行为方式与取径而非具体动作;②结论可泛化而非绑定单任务细节;
③叙述充分性(解释机理而非贴标签);④无战术处方。判定协议
`{verdict: pass|revise|reject, violated_criteria, feedback(一段,具体到句),
quoted_offense}`;revise 重写一次,仍不过则剔除(留盘标记)。
**复用点:解读层、组分析、活文档更新、生成产物、探索报告/findings。**

**第二层:挖掘**:
1. 归组(两遍制):第一遍读 240 条 behavior_signature 提分组方案(成段理由+
   代表轨迹);第二遍对边界样本读 narrative 复核;
2. 组内深析(每组 1 调用,组大则分批 map→merge):通读成员 narrative 全文 →
   `{analysis_narrative(多段主体), distilled_claims[{claim,evidence}](供融合对账),
   open_questions(→探索选点)}`;
3. adherence 台账:先机械聚合(策略逐节计数+证据索引),再 1 调用综合情境 note
   成段落判读;
4. burst 综合 → burst_summary.md;
5. 融入活文档(§2.2 语义+核验)。

**frontier_analysis(失败侧)**:基础 = 残差任务(0/K)失败轨迹解读全文 +
跨 burst 持续失败记录。流程:失败方式归组(理由成段)→ 每组深析(组内解读+
该组既有叙述 → 更新版组叙事:此策略在这类任务上诱导出怎样的行为、为何结构上
到不了终点、跨 burst 失败形态演变)→ **叙述充分展开后以归因段收尾**:

- **A 策略机制缺失**(策略不提供所需行为机制,须从行为叙事中论证)→ REFINE 正当靶;
- **B 策略表达不达**(写了但 agent 不执行,对照 adherence 台账)→ 表达重构靶;
- **U 未能确立策略层病因**(行为层读法显示取径本身无恙、失败出自局部执行,
  或现有证据不足以指认 A/B)。**U 是证据裁决,不是"留给 L0"的预测承诺**;
  U 群不作 REFINE 靶,但绝不弃置,三条出路:①随任意子节点的 L0 自然获得
  再尝试(REFINE 子还携继承规则);②**跨 burst 持续 U 且 L0 已停滞的群自动
  升格为探索靶**(派 probe 查明它为何抵抗)——"无法解释的持续失败,本身就是
  策略层读法漏了东西的证据",此升格规则写入组深读 prompt;③探索 findings
  可将 U 群改判为 A/B。

(设计裁决 2026-07-05:原"C 战术未熟,留给 L0"删除。理由:其归因是对未来的
预测而非对证据的裁决,不可证伪、易成舒适倾倒场;且在唯一消费点——饱和时选
REFINE 靶——上操作性已死:L0 已停滞,"留给 L0"没有执行者。但不可强制 A/B
二分:硬要给战术性失败编一个策略层病因,会把执行细节乔装进 REFINE 靶。U 类
承接这部分现象并给出建设性出路。防"战术乔装成策略问题"由三处承接:解读层
战术处方禁令、Altitude Screen、REFINE 对质步。)

组粒度融合:持续失败加深叙事、失败方式改变显式改写、被攻克移入"已解"节并
记录攻克方式。

**global/unsolved(跨策略综合,NEW 主靶来源)**:机械交集(全树 0/K =
全局硬残差;仅个别策略解出 = 范式敏感任务)→ 每群跨策略对读(各节点组叙事并排:
同方式失败=共同失败机理假设,NEW 最佳靶)→ 优先级排序(群大小×交互空间可及)。

**成本**:全量解读 ≈240 调用 + 裁判 ~24 + 挖掘 ~12 + 融合核验 ~6 ≈ **280 单发
调用/burst**,约为 burst 本体 rollout 调用量(2400+)的 12%。留 K 内去同质化
knob,默认全量。

---

## 3. 自由探索机制(probe 式)

### 3.1 定位

探索 agent 是**实验设计者(旁观者)**而非当局者:它不亲自在环境行动,而是撰写
behavior prompt 派发真实 rollout,读忠实解说,迭代实验。三层价值:

1. **高度对齐**:实验单元 = "一个 agent 若以这种方式行事会怎样"——恰是 L1 要
   回答的问题;probe prompt 空间就是策略空间,探索 = 策略空间的**带真实反馈的
   前哨搜索**;破题 probe(未解任务首过)即**原型策略**,直供生成步泛化;
2. **受控对照**:同任务、单变量差异的 prompt 对 → 行为→结局的因果证据
   (被动日志永远给不了);
3. **保真与统一**:probe 走真实管线(同环境/同评估),发现直接可迁移;环境无关,
   基建全复用(rollout 执行器/轨迹序列化/评估),无需为探索者做任何环境适配。

**触发时机**:每次产子前(NEW→全局 unsolved 高优群;REFINE→本节点 A 类群);
findings 按任务群缓存复用,失效条件 = 群被攻克(归档)或失败形态改变(标 stale
重探)。

### 3.2 dispatch_probe 工具协议

```jsonc
dispatch_probe({
  "behavior_prompt": "完整行为指令(极简单规则/完整范式/历史策略原文复刻均可)",
  "task_id": "从简报任务单选:目标未解群成员 + 2-3 个近邻已解任务(对照用);仅 train",
  "k": 1,            // ≤2:温度采样下单次是单样本
  "purpose": "(一段)本次实验想观察什么(落盘;解说聚焦参考)"
})
```

**边界(已确认)**:behavior_prompt 只替换 agent system prompt 的**行为层**
(strategy+rules 槽位);环境脚手架(动作格式合同/回合协议)锁定不动——否则
探测死于格式错误,什么都学不到。同回合可并发派 2–3 个 probe。

### 3.3 解说者忠实合同

- **覆盖义务**:逐回合落入叙述(动作+环境反馈要旨);关键推理时刻重点还原;
  循环以计数概括不省略;**报错短则原文引用**;全文回合锚点 `[t7]`,锚点齐全性
  **机械核验**(全部回合号必须出现,零 LLM 成本);
- **忠实纪律**:只还原不评论(评论权归探索者);保持时序;实体标识符原样引用;
  长度与轨迹成比例(约每回合一句,上限千余词);
- **结构**:主体 = 完整客观还原;末尾两节 = 评估结果 + (有 gold 的环境)
  **正确思路要旨**——标注 "instance-specific reference",以"该实例参考路径"
  措辞;探测超时/挂死如实解说(本身是观察);
- GT 相容性:优化器侧可见 GT 是既定 firewall 原则;findings 过双筛(§3.6)
  保证不入部署产物。

### 3.4 探索 agent 提示词(部署终稿,英文)

```
=== YOUR MISSION ===

A new behavioral strategy is about to be designed for this task domain.
It must succeed where every existing strategy has failed — specifically,
on the task group in front of you, which no strategy so far can solve.

You are the exploration agent sent ahead of that design. Your one task:
through real experiments in the environment, dig out the information the
strategy designer will need, and produce the key supporting content that
makes a better new strategy possible. Everything you do in this session
exists for that purpose. The measure of your success is simple: with
your report in hand, the designer can make design decisions they could
not have made without it.

What counts as such information? For example (not a checklist — your
judgment governs):
  * why this task group actually resists every known strategy — verified
    against the environment, not merely inferred from old logs;
  * behavioral elements that visibly change outcomes here — things a new
    strategy should build on, avoid, or combine;
  * possibilities the environment allows that no existing strategy has
    ever used;
  * and if you find a behavior prompt that actually cracks an unsolved
    task, that is a prototype of the new strategy itself — featuring it
    and isolating which behavioral element made the difference may be
    the single most valuable thing you deliver.

=== WHAT YOU KNOW ===

Attached is the complete history for this task group: every strategy
tried, what it said, how agents actually behaved under it, and the full
narrative of how each failed here. Read it as a designer reads prior
work: absorb it, locate what it leaves unknown, and go find that out.

=== HOW YOU WORK ===

You never act in the environment yourself. Your single instrument is
dispatch_probe: you author a behavior prompt (the behavioral
instructions an executor agent will follow), pick a task, and a real
rollout runs under the standard harness; a faithful narration of the
whole run comes back to you, with its evaluation.

Stay at strategy altitude. You are probing which WAYS OF BEHAVING are
effective here — not tuning execution. Do not spend probes, or your own
reasoning, on how to fix a specific action at a specific step; that
kind of low-level execution experience is learned by a different loop
after a strategy is deployed. If a probe's lesson can only be phrased
as a step-level correction, it is below your altitude: find the
behavioral principle behind it, or move on. Your experiments and your
findings must be about behavioral approaches and their effectiveness.

Design experiments, don't just collect runs. Before each dispatch, ask
yourself: could this probe's outcome change what the new strategy
should be? If not, it is not worth dispatching. State your purpose with
every probe.

Techniques that tend to pay off (yours to use or ignore):
  * contrastive pairs — two prompts differing in exactly one behavioral
    instruction, on the same task: the cleanest evidence that a
    behavior causes an outcome;
  * replication — rerunning a past strategy's own text, to witness its
    failure firsthand instead of trusting the archive;
  * minimal prompts — a single behavioral rule in isolation to see its
    pure effect; full paradigms to test integration;
  * solved-neighbor contrast — the same behavior on a nearby solvable
    task, to see what breaks specifically here.

One run is one sample: outcomes vary across runs, so repeat or contrast
before you lean on a conclusion. Read narrations closely; when the
environment surprises you, that is usually the thread worth pulling.

=== WHEN TO STOP ===

There is no quota of probes; depth and breadth are your call. Stop when,
in your judgment, you have learned enough to genuinely support the
design of the new strategy — then write your report. Do not stop merely
because progress is slow: one verified insight is worth more than a
quick guess. And do not keep probing what you already know.

=== YOUR REPORT ===

Write in full paragraphs, narrative first, synthesis last:
  1. What you set out to learn, the experiments you ran, and what
     actually happened — with probe references;
  2. What you now understand about why this group resists every known
     strategy — stated as verified understanding, citing the probes
     that verify it;
  3. The design-support core: the concrete behavioral possibilities a
     new strategy could build on, each tied to its probe evidence; if
     any probe achieved a first-ever pass, feature it and isolate the
     behavioral element that made the difference.

The report is not an environment travelogue. Every part of it must earn
its place by helping design the new strategy.
```

REFINE 变体只改使命段首句("An improved strategy is about to be designed on
the basis of the current one, which succeeds broadly but fails on the task
group in front of you")与背景包(本节点档案:现任策略/行为画像/该群失败叙事),
其余条款共用。

### 3.5 预算:放开 + 静默护栏 + 倾向遥测

- 提示词**零预算话术**(不设配额/不报剩余/不催收束)——观察自然倾向;
- **静默技术护栏**:48 probes/会话硬上限(纯防失控,可调不入指纹),上限内
  完全沉默,触顶才注入"资源已尽,请立即成文报告";**不做中途提醒**(中途干预
  污染观察);
- **倾向遥测** `session_meta.json`:probe 总数/对照对数/复刻数/k>1 数/任务
  覆盖数/会话轮数/收束原因(自主 vs 触顶)/报告字数——运行数次后据此定夺是否
  引入预算纪律。

### 3.6 归档与双筛

会话全档落盘(probes 逐条 {behavior_prompt, task, purpose, narration, verdict}
+ 对话 + 报告);破题 probe 单列高亮直供生成。报告→跨会话 findings.md(叙述体,
末段归纳)→ **双筛后归档**:content-purity 筛(可引用探测结局,**不得收录 gold
解法本身**)+ Altitude Screen(策略行为层,无战术处方)。

---

## 4. 生成管线

### 4.1 NEW(root 产子;gen/ 全程落盘,步级断点恢复)

0. 探索(或复用缓存 findings);
1. **研读与靶选定**(1 调用):全部节点档案**全文**(策略+rationale+画像+局限
   +结局曲线,≤8 节点 ≈ ≤45k tokens 直接全喂;膨胀才降级"最相关 2–3 全文+
   其余摘要")+ global unsolved + 相关 findings → 选定靶问题 + "现有范式为何
   在此集体失灵"综合陈述;
2. **构思**(1 调用):新的整体行为范式——核心行为承诺/与历史各策略的本质区别/
   预期解决机理;破题 probe 存在时优先作为原型种子泛化;
3. **novelty 对质**(1 调用,独立批判者):构思 vs 每个历史策略逐一比对
   **行为方式实质**,重复→带理由打回重构思(至多 2 次);
4. **成文**(1 调用):部署级 strategy.md(面向执行 agent 可遵循行文;
   **以命名行为机制节 `## <behavioral mechanism>` 组织**——这是后续 REFINE
   受控编辑的操作单元约定)+ rationale.md(靶问题/构思来源/**预期行为变化
   清单**——后续 burst 画像的"预期兑现度"节据此对质);
5. **altitude+purity 核验**(现有防火墙)。
   子节点 rules 空,零继承。

### 4.2 REFINE(策略节点产子)——策略级受控编辑

REFINE 采用 L0 同族的**受控编辑**方式,但编辑单元锁定在策略高度:**操作对象
= 命名行为机制节**(strategy.md 自 NEW 成文起即以 `## <behavioral mechanism>`
节组织),绝不落到语句/动作层。收益:保留清单从"语义上保持"升级为**字节级
不动**(未列入编辑计划的节原文保留)——全文重生成会连"保留"的部分一起换措辞,
行为漂移会污染归因;受控编辑下 **diff 即干预**,单机制干预的受控实验语义干净,
谱系分析(哪个机制变化导致盆地差异)直接可读。(文献支撑:MPO 节级替换证据,
见 refine-mechanism-research;这也是当初"REFINE 字节级 diff 脆弱"研究的正解:
脆弱的是语句级 diff,不是机制节级替换。)

0. 探索(本节点 A 类群,或缓存;无 A/B 靶时改探持续 U 群,见步 1);
1. **病因确认**(1 调用):本节点档案全量 + 画像"预期兑现度" + 兄弟节点档案
   (父的其他孩子命运,谱系内直接可得)→ 从 frontier_analysis 选 **A 类**
   (机制缺失→机制级编辑)或 **B 类**(表达不达→同机制表述重构)靶;输出:
   修改靶 + **保留清单**(画像确认正在起作用的行为机制节,明示不动)。
   **无可论证的 A/B 靶时**:先对高优 U 群跑探索,携 findings 重做病因确认;
   仍无 → **decline:节点直接 TERMINAL**——诚实承认"此策略在本高度无可论证的
   改进靶",不产垃圾子(AppWorld 94% 型高分节点的正确出路,取代旧机制的
   fallback 强制部署);
2. **编辑计划**(1 调用):op ∈ {REPLACE_SECTION / ADD_SECTION / REMOVE_SECTION /
   REWRITE_SECTION_FOR_ADHERENCE(B 靶:机制不变,表述重构)},每 op 携完整
   新节文本(段落叙述)+ rationale;未列入的节**字节保留**;**不设
   REWRITE_ALL**——需要整体换范式,说明该做的是 root 的 NEW,不是 REFINE;
3. **对质**(1 调用,批判者):编辑计划是否只落在 A/B 归因的机制上?保留清单
   零触碰(机械可验)?所指认 A 类病因是**真机制缺失还是执行细节乔装**?
   与已死兄弟重复吗?每 op 新节文本过 Altitude Screen;
4. **确定性 apply + 连贯性修缮**:apply 后一次受限调用**仅**修跨节引用/衔接,
   产出逐条 `{quoted_old, new, reason}` 的 coherence diff,由对质步复核未改动
   保留节语义;
5. **rationale.md**(含预期行为变化清单)+ altitude/purity 终验;
6. **规则继承裁决**(1–2 调用,`inherit_decisions.json` 逐条落盘):新策略文本 +
   父 rules_best.md,逐节逐条三判:**相容保留 / 冲突丢弃 / 最小改写后保留**
   (规则大体适用但措辞绑定旧策略时)。

生成侧全部开销(探索除外)≈ 每次产子 6–9 次调用,占一次 burst 成本 <1%;
探索会话(probe 为真实 rollouts)另计,量级由遥测观察后定。

---

## 5. 现有代码部件去向表

| 现部件 | 去向 |
|---|---|
| css/proposal/proposal.py 八轮假设-测试-验证循环 | **退役**(树的 bounded bursts 即评估) |
| 空 rules 客观验证(lift/net_lift 判据)、fallback_best_overall | **废除** |
| css/proposal/derivation.py(derive_strategy/负档案) | **退役**;负档案职能由档案对质(novelty/回归对质)取代 |
| css/tree/branching.py decide_branch、FORCE_SATURATE 旗标 | **退役**(三态机取代;无相位可切换) |
| css/coldstart.py 单发 design+critique | **退役**(root bare bursts + 首批 NEW 取代;val_baseline 播种等评估基建保留) |
| css/tree/select.py 三项式 | **保留**:n 改记 burst 数;删 `n<=0→inf` 分支(选择池无 n=0 节点) |
| L0 exploitation 循环 | **重构**:"跑到饱和"→"跑恰好 B 步";饱和判据移到 burst 边界(跨 burst stall 计数) |
| L0 gate / editpipe v2 / checkpoint / difficulty ledger | **原样保留**(gate 兼任树回报的降噪器) |
| 分析管线 l1_signals | 已不参与分支决策;实施时清理死依赖 |

实施注意:新结构超参(B/度数/深度/饱和阈值)入 `_FINGERPRINT_FIELDS`
(resume 语义);探索护栏、遥测不入指纹。

---

## 6. 默认参数表(全部可调;per-env 商定约定照旧)

| 参数 | 默认 | 依据 |
|---|---|---|
| B(burst 步数) | 5 | 各环境 L0 增益集中前 3–8 步;决策频率 vs 单次信号质量折中 |
| 饱和判据 | 连续 saturation_dry_bursts=2 个零 accept burst | §1.3 用户终裁;假饱和 ~3% |
| 度数 | 策略节点 3 / root 不限 | 用户拍板 2026-07-05:先实施观察 |
| 深度 | 不限 | 同上;run 终止=总预算 |
| UCB α/β/W | 沿用 config 现值(0.5/0.5/10) | 已 pilot-tuned;n 换 burst 单位后按首环境实测复核 |
| 解读覆盖 | 全量(≈240/burst) | 用户拍板;留 K 内去同质化 knob |
| 探索护栏 | 48 probes/会话,静默 | §3.5;观察倾向后定夺 |
| probe k | ≤2 | 单次是单样本 |
| 并发 | concurrency_limit=1 | 信号干净;并行 burst 留作后续 |

---

## 7. 论文定位(备忘)

- **Bilevel search**:外层树搜行为范式(策略),每个节点内层跑战术学习循环(L0)
  ——区别于 LATS/ToT(推理时搜树)与 GEPA/TextGrad(单层反思优化);
- **Rising-value bandit**:节点价值随投入上升的选择机制(val+slope 取代终身均值,
  选择/部署解耦);
- **Experiment-informed generation**:probe 式探索把"事后日志反思"升级为
  "生成前主动实验",probe 原型直通策略生成——机制级新颖点;
- **Evidence documents**:活文档(画像/局限分析/findings)本身是 LLM 维护的
  搜索基质,与论文"搜索最优 skill documents"主命题自洽。

## 8. 待观察后定夺(显式留白)

1. 探索 agent 的 probe 用量与实验风格(遥测数据 → 是否引入预算纪律/引导);
2. UCB β 在 burst 单位下的重标定(首环境实测);
3. 全量解读的 K 内去同质化 knob 是否开启(成本实测后);
4. 多节点并行 burst(virtual loss)引入时机;
5. root 不限配额、深度不限下的树形态(NEW 产出节奏由 β 主导;深 REFINE 链
   是否出现、是否有价值)——首环境实测后再议是否要约束。
