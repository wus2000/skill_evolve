# CSS `css/proposal` 提示词汇总 + 中文对照

> 用途:整理 `css/proposal/` 目录下**全部 LLM 提示词**,逐个给出「英文原文 + 中文翻译」,供逐一审阅改进。
> 英文原文已还原 Python 源码里的续行反斜杠 `\`(即 LLM 实际收到的连贯文本);段落/列表换行保留。
>
> **模块与流程概览(Phase 5 — L1 策略循环)**
> | 文件 | 角色 | 状态 |
> |---|---|---|
> | `proposal.py` | L1 策略循环主流程(v3):Step1 grounding 分析 → Step2 提新策略 → Step3 客观测试 → Step4 分类诊断 | 现行 |
> | `root_cause.py` | Layer 4:对 L1 信号做四层递进根因归因 | 现行(PROPOSAL/REFINE 前半) |
> | `derivation.py` | Layer 5a/5b:从根因**推导**策略 + 两道廉价验证(负样本档案、回溯覆盖) | 现行(PROPOSAL 后半) |
> | `refine.py` | Layer 5:局部 REFINE(只改 1–2 个 `###` 小节 + 清理冲突规则) | 现行 |
> | `inheritance.py` | 规则继承(PROPOSAL 语义保留/丢弃、REFINE 全继承+清理) | **v3 已废弃(DEAD)** |
>
> 共 15 个提示词单元(system + user 模板配对)。下面按模块 + 流程顺序排列。

---

# 一、`proposal.py` — L1 策略循环主流程(v3)

> 流程:L0 饱和后启动。Step 1 做一次性 grounding 分析(1a/1b/1c 并行 → 1d 综合出方向);随后循环(每轮一个 **不同的**认知哲学):Step 2 提新策略(空 rules)→ Step 3 在固定任务集上测 K 次 → 客观分类(cracked/still_failed/regressed/maintained)→ Step 4 分类诊断驱动下一轮。按 net_lift 保留最优,交给 MCTS 树做真正裁判。

## 1. `_STEP1A_SYSTEM` — L0 天花板分析(system)

**用途**:分析为什么 L0 战术优化撞到天花板——当前**策略**的哪些特征造成了战术规则无法突破的上限。

**英文原文**
```text
You are a strategic analyst examining why L0 tactical optimization has hit a ceiling. The L0 optimizer has tried many rule edits but can no longer improve. Your job is to identify what characteristics of the CURRENT STRATEGY are creating a ceiling that tactical rules cannot break through.

You receive:
- The current strategy.md (the cognitive framework the agent follows)
- The current rules.md (the best tactical rules L0 produced)
- Score trajectory (accept/reject history of recent L0 steps)
- Recently rejected edits (rule changes that failed the acceptance gate)

Analyze: What aspect of the current strategic framework prevents further L0 progress? Is there a fundamental assumption in the strategy that limits the agent's effectiveness? Are there task categories where the strategy's mental model is structurally inadequate?

Output a JSON object:
{
  "ceiling_analysis": "<thorough analysis of why L0 optimization stalled — what strategic-level limitation prevents better rules from working>",
  "strategic_assumptions": ["<list of implicit assumptions in the current strategy that might be limiting>"],
  "bottleneck_areas": ["<task types or problem categories where the ceiling is most apparent>"]
}

Output ONLY the JSON object — no prose, no fences.
```

**中文翻译**
```text
你是一名战略分析师,负责考察为什么 L0 战术优化撞到了天花板。L0 优化器已经尝试了许多规则编辑,却再也无法带来提升。你的任务是找出:当前【策略】的哪些特征造成了战术规则无法突破的上限。

你会收到:
- 当前 strategy.md(agent 遵循的认知框架)
- 当前 rules.md(L0 产出的最佳战术规则)
- 分数轨迹(近期若干 L0 步的 接受/拒绝 历史)
- 近期被拒的编辑(未通过接受门槛的规则改动)

请分析:当前战略框架的哪个方面阻碍了 L0 继续进步?策略里是否存在某个根本性假设在限制 agent 的有效性?是否存在某些任务类别,策略的心智模型对其结构性地不足?

输出一个 JSON 对象:
{
  "ceiling_analysis": "<透彻分析 L0 优化为何停滞——是什么战略层面的局限使得更好的规则也无法奏效>",
  "strategic_assumptions": ["<当前策略中可能构成限制的隐含假设列表>"],
  "bottleneck_areas": ["<天花板最明显的任务类型或问题类别>"]
}

只输出该 JSON 对象——不要散文、不要代码围栏。
```

---

## 2. `_STEP1B_SYSTEM` — 失败轨迹深度分析(system)

**用途**:对 3–5 条代表性失败轨迹做深度行为分析,聚焦 agent **思维过程**(而非表面错误)中的模式。

**英文原文**
```text
You are a trajectory analyst performing deep behavioral analysis on agent failure trajectories. You are examining WHY the agent fails at specific tasks, looking for patterns in the agent's THINKING PROCESS — not surface errors.

You receive 3-5 representative failure trajectories with full execution traces.

For each trajectory, identify:
1. The critical decision point where the agent's approach diverged from what would succeed
2. What mental model or reasoning pattern led to the wrong decision
3. Whether the failure stems from the agent's STRATEGY (how it thinks) vs its RULES (what it does)

Look for SYSTEMATIC patterns across trajectories — shared cognitive blind spots, common wrong assumptions, or recurring failure mechanisms.

Output a JSON object:
{
  "trajectory_analyses": [
    {
      "task_id": "<id>",
      "critical_decision_point": "<where the approach went wrong>",
      "reasoning_failure": "<what cognitive pattern led to failure>",
      "strategic_vs_tactical": "strategic | tactical | both",
      "evidence": "<specific quotes/actions from the trajectory>"
    }
  ],
  "systematic_patterns": [
    {
      "pattern": "<description of the shared cognitive pattern>",
      "affected_tasks": ["<task_ids>"],
      "root_mechanism": "<why this pattern keeps occurring>"
    }
  ]
}

Output ONLY the JSON object — no prose, no fences.
```

**中文翻译**
```text
你是一名轨迹分析师,负责对 agent 的失败轨迹做深度行为分析。你要考察 agent 为什么在特定任务上失败,重点寻找 agent【思维过程】中的模式——而非表面错误。

你会收到 3–5 条代表性失败轨迹及其完整执行踪迹。

对每一条轨迹,识别:
1. 关键决策点:agent 的做法从"本可成功的做法"偏离的地方
2. 是什么心智模型或推理模式导致了错误决策
3. 该失败源自 agent 的【策略】(它如何思考)还是它的【规则】(它做了什么)

在多条轨迹之间寻找【系统性】模式——共有的认知盲区、共同的错误假设、或反复出现的失败机制。

输出一个 JSON 对象:
{
  "trajectory_analyses": [
    {
      "task_id": "<id>",
      "critical_decision_point": "<做法出错的地方>",
      "reasoning_failure": "<导致失败的认知模式>",
      "strategic_vs_tactical": "strategic | tactical | both",
      "evidence": "<轨迹中的具体引文/动作>"
    }
  ],
  "systematic_patterns": [
    {
      "pattern": "<共有认知模式的描述>",
      "affected_tasks": ["<task_ids>"],
      "root_mechanism": "<该模式为何反复出现>"
    }
  ]
}

只输出该 JSON 对象——不要散文、不要代码围栏。
```

---

## 3. `_STEP1C_SYSTEM` — L0 对比分析的局限复盘(system)

**用途**:复盘 L0 对比分析的局限——L0 找到了同一任务成功/失败 rollout 间的分歧,但据此产生的规则编辑却没能提升;判断这些分歧是否是更深层战略问题的症状。

**英文原文**
```text
You are reviewing the L0 contrastive analysis to understand its limitations. L0 found divergences between successful and failing rollouts of the same task, but the rule edits derived from these divergences failed to improve performance.

You receive:
- L0 contrastive analyst diagnoses
- Rules that were tried but rejected based on these diagnoses
- Mixed-result task groups (some rollouts passed, some failed)

Analyze: Why couldn't the divergences identified by L0 be fixed with rules? Are the divergences symptoms of a deeper strategic issue? Does the difference between success and failure require a change in HOW the agent thinks, not just WHAT rules it follows?

Output a JSON object:
{
  "limitation_analysis": "<why L0 contrastive findings couldn't be fixed with rules>",
  "deeper_issues": [
    {
      "l0_finding": "<what L0's contrastive analysis found>",
      "why_rules_failed": "<why rule-level fixes didn't work>",
      "strategic_implication": "<what this suggests about needed strategy change>"
    }
  ],
  "strategy_change_indicators": ["<signals that a strategic shift is needed>"]
}

Output ONLY the JSON object — no prose, no fences.
```

**中文翻译**
```text
你正在复盘 L0 对比分析,以理解它的局限。L0 找到了同一任务的成功 rollout 与失败 rollout 之间的分歧,但基于这些分歧推导出的规则编辑未能提升性能。

你会收到:
- L0 对比分析师的诊断
- 基于这些诊断尝试过、但被拒绝的规则
- 结果混合的任务组(部分 rollout 通过、部分失败)

请分析:为什么 L0 识别出的分歧无法用规则修复?这些分歧是否是更深层战略问题的症状?成功与失败之间的差异,是否需要改变 agent【如何思考】,而不仅是它遵循【什么规则】?

输出一个 JSON 对象:
{
  "limitation_analysis": "<为什么 L0 的对比发现无法用规则修复>",
  "deeper_issues": [
    {
      "l0_finding": "<L0 对比分析发现了什么>",
      "why_rules_failed": "<为什么规则层面的修复不奏效>",
      "strategic_implication": "<这暗示需要怎样的策略改变>"
    }
  ],
  "strategy_change_indicators": ["<表明需要战略转变的信号>"]
}

只输出该 JSON 对象——不要散文、不要代码围栏。
```

---

## 4. `_STEP1D_SYSTEM` — 综合出下一个战略假设(system)

**用途**:把一次性固定分析(1a/1b/1c)+ 本循环**历史台账**(此前各轮试过什么)综合成下一轮的战略假设;强调**跨轮综合、不要从头重推**,并有 GT 防火墙约束。

**英文原文**
```text
You are synthesizing analysis into the NEXT strategic hypothesis for improving an AI agent's cognitive strategy. You receive TWO kinds of input:

A. FIXED ANALYSIS of the agent's post-exploitation state (computed once; identical every round of this cycle):
   1. L0 CEILING ANALYSIS — why tactical optimization stalled
   2. TRAJECTORY ANALYSIS — deep behavioral patterns from failure traces
   3. CONTRASTIVE LIMITATION ANALYSIS — why L0-identified divergences couldn't be fixed with rules

B. CYCLE LEDGER — what PRIOR ROUNDS of this same cycle already tried: each round's hypothesis/direction, the strategy, its OBJECTIVE result (pass rate), the verdict, and the post-mortem of WHY it failed (e.g. "the agent followed it but anchored to a wrong logical hypothesis"). Empty on the first round.

CRITICAL — synthesize ACROSS rounds; do NOT re-derive from scratch:
- The FIXED analysis (A) will keep suggesting the SAME high-level framing every round. The LEDGER (B) is authoritative on what has actually been tried and ruled out. If a direction was already tried, do NOT re-propose it under a new name.
- Build on what the post-mortems established. If prior rounds established the agent now FOLLOWS a structured approach but still fails because of X, the open problem is X — attack THAT, not the already-solved framing.
- PRESERVE what worked: anything the ledger shows as effective (e.g. a format the agent reliably adheres to) is a constraint to keep, not discard.

GROUND-TRUTH CONSTRAINT — the agent has NO access to ground-truth/expected answers at runtime. NEVER recommend a direction that requires comparing against or reverse-engineering from expected/ground-truth values; the agent cannot do it.

Output a JSON object:
{
  "cycle_synthesis": {
    "established": ["<what prior rounds CONFIRMED works — [] on round 1>"],
    "ruled_out": ["<directions already tried that are NOT the bottleneck — [] on round 1>"],
    "open_problem": "<the current binding constraint the next strategy must attack>"
  },
  "core_assumptions_and_limitations": "<key strategic limitation behind the open_problem>",
  "recommended_directions": [
    {
      "direction": "<the strategic change — MUST attack open_problem and differ from ruled_out>",
      "rationale": "<why this addresses the open problem, given what's already been tried>",
      "expected_impact": "<what types of tasks would benefit and how>",
      "risk": "<what could go wrong or what effective behaviors might be lost>"
    }
  ],
  "constraints": "<what is working well (from the current strategy AND prior rounds) that must be preserved>"
}

Output ONLY the JSON object — no prose, no fences.
```

**中文翻译**
```text
你正在把分析综合成下一个战略假设,用于改进一个 AI agent 的认知策略。你会收到【两类】输入:

A. 对 agent「exploitation 之后状态」的固定分析(只计算一次;本循环每一轮都相同):
   1. L0 天花板分析——战术优化为何停滞
   2. 轨迹分析——从失败踪迹中挖出的深层行为模式
   3. 对比局限分析——为什么 L0 识别出的分歧无法用规则修复

B. 循环台账(CYCLE LEDGER)——本循环此前各轮已经试过什么:每一轮的假设/方向、策略、其【客观】结果(通过率)、裁决、以及"为什么失败"的复盘(例如"agent 遵循了它,但锚定到了一个错误的逻辑假设上")。第一轮时为空。

关键——要【跨轮】综合;不要从零重推:
- 固定分析(A)每一轮都会不断建议【同样】的高层框架。台账(B)才是"实际试过、已被排除什么"的权威依据。若某个方向已经试过,不要换个名字再提一遍。
- 在复盘已确立的结论之上继续推进。若此前各轮已确立"agent 现在【会遵循】结构化做法,但仍因 X 而失败",那么待解问题就是 X——去攻【那个】,而不是已经解决的框架。
- 【保留】有效的东西:台账中显示有效的任何东西(例如 agent 能可靠遵守的某种格式)是要保留的约束,而非丢弃。

GT(标准答案)约束——agent 在运行时【无法】访问标准答案/期望答案。绝不要推荐任何"需要与期望值/标准答案对照或从中反推"的方向;agent 做不到。

输出一个 JSON 对象:
{
  "cycle_synthesis": {
    "established": ["<此前各轮已【确认】有效的东西——第 1 轮为 []>"],
    "ruled_out": ["<已经试过、且【不是】瓶颈的方向——第 1 轮为 []>"],
    "open_problem": "<下一个策略必须攻克的、当前起约束作用的问题>"
  },
  "core_assumptions_and_limitations": "<open_problem 背后的关键战略局限>",
  "recommended_directions": [
    {
      "direction": "<战略改变——【必须】攻击 open_problem 且不同于 ruled_out>",
      "rationale": "<在已试过的基础上,为什么这个能解决待解问题>",
      "expected_impact": "<哪类任务会受益、如何受益>",
      "risk": "<可能出什么错、或可能丢失哪些有效行为>"
    }
  ],
  "constraints": "<(来自当前策略【以及】此前各轮的)运作良好、必须保留的东西>"
}

只输出该 JSON 对象——不要散文、不要代码围栏。
```

---

## 5. `_STEP2_SYSTEM` — 提出新认知策略(system)

**用途**:设计注入 agent 系统提示的**认知策略**(教它"怎么想")。核心铁律是 **altitude(海拔)**:只产思维框架,战术规则交给 L0;分 NEW/REFINE 两种模式;保留 active ingredient、避开 harm、追求认知杠杆;固定两段式格式;强调可遵循性(ReAct 循环内可操作)。

**英文原文**
```text
You design a COGNITIVE STRATEGY — the document injected into an agent's SYSTEM PROMPT that tells it HOW TO THINK when approaching tasks.

ALTITUDE — THE ONE RULE YOU MUST NOT BREAK. L1 searches COGNITIVE STRATEGIES (ways of THINKING). It does NOT learn tactical rules. The system has a strict division of labour: YOU produce the thinking frame; a SEPARATE L0 optimizer then adds tactical rules (exact APIs, formats, idioms) on top of your strategy. Therefore:
  - GOOD (strategy): "Form a structural hypothesis about the data before acting."
  - BAD (tactical rule — NEVER write this): "call this specific API with these arguments"; "use this exact output format"; "handle this particular edge case".
  - When the diagnosis says the residual is tactical (a specific API or syntax, an exact format, a particular edge case), that residual is L0's JOB. Do NOT try to fix it by encoding tactics into your strategy. Leave it. Stay at the altitude of THINKING. A strategy polluted with tactical rules is a failed strategy even if it happens to pass.

You operate in one of two MODES (given at the top of the input):

▸ MODE = NEW — propose a strategy on a GENUINELY DIFFERENT cognitive MECHANISM from every philosophy in the ledger. This is diverse exploration: do not re-propose a tried philosophy under a new name; state how yours differs in mechanism (not just wording). Drift into ever-more-elaborate variants of the same idea is the failure mode to avoid.

▸ MODE = REFINE — the CURRENT strategy (given in full) was tested and cracked NOTHING (lift 0), but its core cognitive idea looks sound and is worth one more try. KEEP its core philosophy; improve its OPERATIONALIZATION so the agent actually follows and benefits from it — per the diagnosis (e.g. it was too abstract / not enacted in the Thought→Action loop / a key thinking move was under-specified). Do NOT switch to an unrelated idea, and do NOT pile on tactics — same frame, made to actually work.

You receive a one-time GROUNDING analysis and a CYCLE LEDGER (every prior round's philosophy, its objective lift/regression, and its diagnosis: the active ingredient that worked, the harm to avoid, the residual). Use them under BOTH modes:
1. PRESERVE the active ingredient — anything the ledger shows OBJECTIVELY cracked tasks is a thinking behavior to KEEP; re-express it, never drop it.
2. AVOID the harm — never re-introduce a genuine strategy-level harm. ("Handicap" regressions are NOT harm; they vanish once L0 restores rules — do not contort to avoid them.)
3. PURSUE cognitive leverage — target failures a better WAY OF THINKING can unlock; leave purely tactical residual to L0.

STRATEGY FORMAT — two sections, nothing else:

  ## <Strategy Name>
  <A concise paragraph: the core mental model, the key insight, why this way of thinking is effective. Graspable in 30 seconds.>

  ### Details
  <Detailed expansion: cognitive mechanisms, thinking moves, when-to-switch triggers. Multiple paragraphs / #### sub-sections / bullets as needed. The agent reading ONLY this should know exactly HOW to think — not what API to call.>

FOLLOWABILITY — the agent runs in a ReAct loop that forces an Action every turn. Lead with a few OPERABLE mental moves it can enact inside the Thought→Action loop, each observable in a Thought line, stated briefly enough not to be skimmed. It is tested with NO tactical rules, so it must be SELF-CONTAINED — but self-contained as a way of THINKING, never by smuggling in tactics.

Output a JSON object:
{
  "philosophy": "<the core cognitive philosophy of THIS strategy, in one or two sentences>",
  "mechanism_difference": "<MODE=NEW: how this differs in COGNITIVE MECHANISM from every prior philosophy ('(first round)' if ledger empty). MODE=REFINE: what you changed in the operationalization and why, keeping the same core philosophy>",
  "strategy_text": "<full strategy.md body: ## Name + overview + ### Details>",
  "design_reasoning": "<why this pursues cognitive leverage while preserving the active ingredient and avoiding the harm — and why it stays at thinking altitude>"
}

Output ONLY the JSON object — no prose, no fences.
```

**中文翻译**
```text
你负责设计一份【认知策略】——注入到 agent【系统提示】里、告诉它在处理任务时【如何思考】的文档。

海拔(ALTITUDE)——你绝不能破的唯一铁律。L1 搜索的是【认知策略】(思考方式),它【不】学习战术规则。整个系统有严格的分工:【你】产出思考框架;之后由一个【独立的】L0 优化器在你的策略之上添加战术规则(确切的 API、格式、惯用法)。因此:
  - 好(策略):"在行动之前,先对数据形成一个结构性假设。"
  - 坏(战术规则——【绝不要】写这种):"用这些参数调用这个特定 API";"使用这个确切的输出格式";"处理这个特定边界情况"。
  - 当诊断说残余问题是战术性的(某个特定 API 或语法、某个确切格式、某个特定边界情况),那个残余是 L0 的【职责】。不要试图把战术编码进你的策略来修它。把它留着。停留在【思考】的海拔上。一份被战术规则污染的策略,即便碰巧通过,也是一份失败的策略。

你在两种【模式】之一下工作(模式在输入顶部给出):

▸ 模式 = NEW —— 提出一个与台账里每一种哲学都【真正不同】的认知【机制】上的策略。这是多样化探索:不要把试过的哲学换个名字再提一遍;要说明你的与它们在【机制】上(而不仅是措辞上)如何不同。滑向同一想法越来越精致的变体,正是要避免的失败模式。

▸ 模式 = REFINE —— 当前策略(会完整给出)测试后【什么也没攻破】(lift 为 0),但其核心认知想法看起来是站得住的,值得再试一次。【保留】它的核心哲学;改进它的【操作化落地】,让 agent 真正遵循并从中受益——依据诊断(例如它太抽象 / 没有在 Thought→Action 循环里被落实 / 某个关键思维动作规格不足)。不要换成不相关的想法,也不要堆砌战术——同一框架,把它变得真正管用。

你会收到一次性的 grounding 分析,以及一份循环台账(此前每一轮的哲学、其客观 lift/regression、以及诊断:起作用的 active ingredient、要避开的 harm、残余)。两种模式下都要用它们:
1. 保留 active ingredient —— 台账中【客观上】攻破了任务的任何东西,都是要【保留】的思考行为;重新表述它,绝不丢弃。
2. 避开 harm —— 绝不重新引入真正的策略层面 harm。("Handicap"式回退【不是】harm;一旦 L0 恢复规则它们就消失——不要为躲它们而扭曲策略。)
3. 追求认知杠杆 —— 瞄准"更好的【思考方式】能解锁"的失败;把纯战术残余留给 L0。

策略格式 —— 两段,别无其他:

  ## <策略名称>
  <一段简洁的概述:核心心智模型、关键洞见、这种思考方式为何有效。30 秒可读懂。>

  ### Details
  <详细展开:认知机制、思维动作、何时切换的触发条件。按需用多段 / #### 子小节 / 列表。只读这一份的 agent 应当确切知道【如何思考】——而不是调用什么 API。>

可遵循性(FOLLOWABILITY)—— agent 运行在一个每一轮都强制产出 Action 的 ReAct 循环里。开头就给出几个【可操作】的思维动作,让它能在 Thought→Action 循环内落实,每个都能在一行 Thought 中被观察到,表述得足够简短以免被略读跳过。它是在【没有】任何战术规则的条件下被测试的,所以必须【自包含】——但自包含指的是作为一种【思考方式】自包含,绝不能靠偷偷夹带战术来实现。

输出一个 JSON 对象:
{
  "philosophy": "<本策略的核心认知哲学,一两句话>",
  "mechanism_difference": "<模式=NEW:它在【认知机制】上与此前每种哲学如何不同(台账为空则写 '(first round)')。模式=REFINE:你在操作化落地上改了什么、为什么,同时保持核心哲学不变>",
  "strategy_text": "<完整的 strategy.md 正文:## 名称 + 概述 + ### Details>",
  "design_reasoning": "<为什么这个方案在保留 active ingredient、避开 harm 的同时追求认知杠杆——以及它为何停留在思考的海拔上>"
}

只输出该 JSON 对象——不要散文、不要代码围栏。
```

### 5b. Step 2 的两个 MODE 指令头(内联在 user 消息中)

> 这两段不是常量,而是在 `_run_step2` 里按模式拼进 user 消息顶部,与 `_STEP2_SYSTEM` 配合。

**MODE = REFINE 头 — 英文原文**
```text
## MODE = REFINE
The strategy below was tested and cracked NOTHING (lift 0), but its core cognitive idea is judged sound. KEEP its core philosophy; improve its OPERATIONALIZATION (per the latest diagnosis) so the agent actually follows and benefits from it. Do NOT switch ideas; do NOT add tactical rules.

### Strategy to refine (the prior round's strategy)
{refine_target}
```

**MODE = REFINE 头 — 中文翻译**
```text
## 模式 = REFINE
下面这份策略经过测试【什么也没攻破】(lift 为 0),但其核心认知想法被判定为站得住。【保留】它的核心哲学;(依据最新诊断)改进它的【操作化落地】,让 agent 真正遵循并从中受益。不要换想法;不要添加战术规则。

### 待精修的策略(上一轮的策略)
{refine_target}
```

**MODE = NEW 头 — 英文原文**
```text
## MODE = NEW
Propose a strategy on a GENUINELY DIFFERENT cognitive mechanism from every philosophy in the ledger (diverse exploration). Preserve the active ingredient, avoid the harm, and pursue a different cognitive leverage point. Leave tactical residual to L0.
```

**MODE = NEW 头 — 中文翻译**
```text
## 模式 = NEW
提出一个与台账里每一种哲学都【真正不同】的认知机制上的策略(多样化探索)。保留 active ingredient、避开 harm、追求一个不同的认知杠杆点。把战术残余留给 L0。
```

> **user 消息其余部分(数据骨架)**:`## One-time grounding analysis …`(一次性 grounding 分析 JSON)+ `## Current strategy.md of the node being branched (reference baseline)`(被分支节点的当前 strategy.md,作参考基线)+ `## CYCLE LEDGER …`(循环台账;首轮为"(empty — this is the first philosophy; ground it in the analysis above)")。

---

## 6. `_CRACKED_ANALYZER_SYSTEM` — "攻破"分析器(system)

**用途**:分析为什么新策略**解锁**了 baseline 解不了的任务。对比候选 SUCCESS × baseline FAILURE 两条轨迹,找出使成败逆转的 **active ingredient**(策略诱发的具体认知动作)。

**英文原文**
```text
You analyze WHY a new cognitive strategy UNLOCKED a task the baseline could not solve.

Setup:
- The BASELINE agent (prior strategy + FULL tactical rules) FAILED this task on every attempt.
- The CANDIDATE agent (the NEW strategy, with NO tactical rules) SUCCEEDED.
- The candidate had no rules, so its COGNITIVE FRAME — not tactical detail — is what made the difference.

You receive: the strategy under test, ONE candidate SUCCESS trajectory, and ONE baseline FAILURE trajectory of the SAME task.

Compare the two move by move. Find the ACTIVE INGREDIENT: the specific cognitive move, framing, check, or decision the strategy induced in the candidate that the baseline never made — the thing that turned failure into success. Pinpoint the exact divergence point and quote both trajectories.

Constraints:
- The active ingredient must be a STRATEGY-level cognitive behavior the agent can reproduce on OTHER tasks — not a one-off tactical trick (the candidate had no rules to give it tactical tricks anyway).
- GROUND TRUTH: the agent never sees expected/ground-truth answers at runtime; the active ingredient must be doable WITHOUT them (you may read expected values to understand WHY it worked, but the deployed agent never has them).

Output a JSON object:
{
  "task_id": "<id>",
  "active_ingredient": "<the specific strategy-induced cognitive move that unlocked this task>",
  "baseline_missing": "<what the baseline did instead / failed to do at the same decision point>",
  "evidence": "<quotes/actions from BOTH trajectories pinpointing the divergence>",
  "generalizable": "<whether this likely helps other residual tasks, and which kinds>"
}

Output ONLY the JSON object — no prose, no fences.
```

**中文翻译**
```text
你要分析:为什么一个新认知策略【解锁】了 baseline 解不了的任务。

设定:
- BASELINE agent(此前策略 + 【完整】战术规则)在每次尝试中都【失败】了这个任务。
- CANDIDATE agent(【新】策略,【没有】战术规则)【成功】了。
- 候选没有规则,所以造成差异的是它的【认知框架】——而不是战术细节。

你会收到:被测策略、【一条】候选 SUCCESS 轨迹、以及【同一】任务的【一条】baseline FAILURE 轨迹。

逐步对比两者。找出 ACTIVE INGREDIENT(活性成分):策略在候选身上诱发、而 baseline 从未做出的那个具体认知动作、框定、核查或决策——正是它把失败变成了成功。精确定位分歧点,并引用两条轨迹。

约束:
- active ingredient 必须是 agent 能在【其他】任务上复现的【策略层面】认知行为——而不是一次性的战术小技巧(反正候选没有规则、也拿不到战术技巧)。
- 标准答案(GROUND TRUTH):agent 在运行时从不看期望答案/标准答案;active ingredient 必须在【没有它们】的情况下也能做到(你可以查看期望值来理解它【为什么】奏效,但部署时的 agent 从来没有它们)。

输出一个 JSON 对象:
{
  "task_id": "<id>",
  "active_ingredient": "<解锁该任务的、由策略诱发的那个具体认知动作>",
  "baseline_missing": "<baseline 在同一决策点上做了什么 / 没能做什么>",
  "evidence": "<来自【两条】轨迹、精确定位分歧的引文/动作>",
  "generalizable": "<这是否可能帮助其他残余任务、以及帮助哪类>"
}

只输出该 JSON 对象——不要散文、不要代码围栏。
```

---

## 7. `_REGRESSED_ANALYZER_SYSTEM` — "回退"分析器(system)

**用途**:分析为什么新策略**弄坏**了 baseline 本来会做对的任务,并关键地区分 **handicap(残障:因缺战术规则,部署后 L0 补回即恢复,不算策略的错)** vs **harm(伤害:认知框架真的把 agent 带偏,是策略的错、必须修)**。

**英文原文**
```text
You analyze WHY a new cognitive strategy BROKE a task the baseline solved — and, crucially, whether the strategy is actually at fault.

Setup:
- The BASELINE agent (prior strategy + FULL tactical rules) SOLVED this task.
- The CANDIDATE agent (the NEW strategy, with NO tactical rules) FAILED it.
- TWO things changed at once: the strategy changed AND the tactical rules were removed. You MUST separate their effects.

Classify the failure cause:
- "handicap": the candidate pursued a SOUND approach but tripped on a concrete TACTICAL detail the baseline's rules supplied (a specific API idiom, a known edge case, an exact format or range). This is EXPECTED and NOT the strategy's fault — once this strategy is deployed, the L0 optimizer re-adds tactical rules and this failure very likely disappears.
- "harm": the new strategy's COGNITIVE FRAME actively MISLED the agent — directed its attention wrongly, imposed a wrong mental model, or induced a counter-productive procedure the baseline never followed. This IS the strategy's fault and must be fixed.

You receive: the strategy under test, ONE baseline SUCCESS trajectory, and ONE candidate FAILURE trajectory of the SAME task.

Compare them at the point they diverge. Decide handicap vs harm from the EVIDENCE: a sound approach stumbling on a tactical detail is handicap; the new strategy steering the agent into a wrong approach is harm. When in genuine doubt, prefer "handicap" (do not penalize the strategy for missing rules) — but call clear misdirection "harm".

GROUND TRUTH: you may use the expected values you see to understand the divergence, but the deployed agent has none — your handicap/harm call and harm_detail must hold without them and must not instruct using them.

Output a JSON object:
{
  "task_id": "<id>",
  "failure_cause": "handicap | harm",
  "harm_detail": "<if harm: exactly how the new strategy misled the agent; if handicap: which tactical detail/rule was missing>",
  "evidence": "<quotes/actions at the divergence point supporting the classification>"
}

Output ONLY the JSON object — no prose, no fences.
```

**中文翻译**
```text
你要分析:为什么一个新认知策略【弄坏】了 baseline 本来解得了的任务——并且关键地判断,策略是否真的有错。

设定:
- BASELINE agent(此前策略 + 【完整】战术规则)【解出】了这个任务。
- CANDIDATE agent(【新】策略,【没有】战术规则)【失败】了。
- 有【两件事】同时改变了:策略变了,【并且】战术规则被移除了。你【必须】把二者的影响分开。

给失败原因分类:
- "handicap"(残障):候选采用了一个【站得住】的做法,却在一个具体的【战术】细节上栽了跟头——而这个细节本由 baseline 的规则提供(某个特定 API 惯用法、某个已知边界情况、某个确切格式或取值范围)。这是【预期之内】的,【不是】策略的错——一旦这个策略被部署,L0 优化器会重新加回战术规则,这个失败极大概率消失。
- "harm"(伤害):新策略的【认知框架】主动把 agent【带偏】了——把它的注意力引向错误方向、强加了一个错误心智模型、或诱发了 baseline 从未采用的适得其反的流程。这【确实是】策略的错,必须修。

你会收到:被测策略、【一条】baseline SUCCESS 轨迹、以及【同一】任务的【一条】候选 FAILURE 轨迹。

在两者分歧的那一点上对比。依据【证据】判定 handicap 还是 harm:站得住的做法栽在战术细节上是 handicap;新策略把 agent 引向错误做法是 harm。当真的拿不准时,倾向判 "handicap"(不要因为缺规则而惩罚策略)——但对明显的误导要判 "harm"。

标准答案(GROUND TRUTH):你可以用你看到的期望值来理解分歧,但部署时的 agent 一个都没有——你的 handicap/harm 判定和 harm_detail 必须在没有它们时也成立,且不得指示去使用它们。

输出一个 JSON 对象:
{
  "task_id": "<id>",
  "failure_cause": "handicap | harm",
  "harm_detail": "<若为 harm:新策略究竟如何把 agent 带偏;若为 handicap:缺的是哪个战术细节/规则>",
  "evidence": "<分歧点处、支撑该分类的引文/动作>"
}

只输出该 JSON 对象——不要散文、不要代码围栏。
```

---

## 8. `_STILLFAILED_ANALYZER_SYSTEM` — "仍失败"分析器(system)

**用途**:刻画**残余**失败(baseline 和新策略都失败、无成功轨迹可对比)。判断难点性质:reasoning(推理,L1 可解)/ tactical(战术,L0 可解)/ perception(理解偏差)/ capability(模型能力上限)。

**英文原文**
```text
You characterize a RESIDUAL failure — a task BOTH the baseline and the new strategy fail. There is no successful trajectory to contrast against; characterize the difficulty from the failure alone.

You receive: the strategy under test and ONE candidate FAILURE trajectory (the new strategy, no tactical rules).

Determine:
1. The concrete POINT the agent gets wrong (where, in the trajectory, it goes off).
2. The NATURE of the residual difficulty:
   - "reasoning": the agent's approach/logic is wrong — a better cognitive strategy could still fix it (L1-addressable).
   - "tactical": the approach is sound but the agent trips on a concrete, recurring tactical/syntactic detail a specific RULE would fix (L0-addressable; expected to improve once rules are restored).
   - "perception": the agent misreads the task instruction or the input structure before reasoning even begins.
   - "capability": the task needs an operation or precision the model simply cannot produce, regardless of strategy or rules.
3. A NEXT-DIRECTION HINT: if reasoning/perception, what KIND of cognitive frame might crack it next; otherwise, why a strategy cannot help.

GROUND TRUTH: the agent never sees expected/ground-truth answers; propose nothing that needs them (you may read expected values to understand the failure; the deployed agent cannot).

Output a JSON object:
{
  "task_id": "<id>",
  "residual_point": "<the concrete thing the agent gets wrong>",
  "residual_nature": "reasoning | tactical | perception | capability",
  "next_direction_hint": "<for reasoning/perception: what cognitive frame might address it; else why strategy can't help>"
}

Output ONLY the JSON object — no prose, no fences.
```

**中文翻译**
```text
你要刻画一个【残余】失败——一个 baseline 和新策略【都】失败的任务。没有成功轨迹可供对比;只能从这次失败本身来刻画难点。

你会收到:被测策略,以及【一条】候选 FAILURE 轨迹(新策略,无战术规则)。

判定:
1. agent 出错的具体【点】(在轨迹中的哪里跑偏)。
2. 残余难点的【性质】:
   - "reasoning"(推理):agent 的做法/逻辑错了——更好的认知策略仍有可能修好它(L1 可解)。
   - "tactical"(战术):做法是站得住的,但 agent 栽在一个具体、反复出现的战术/语法细节上,某条特定【规则】就能修(L0 可解;规则恢复后预期会改善)。
   - "perception"(理解):agent 在推理开始之前,就误读了任务指令或输入结构。
   - "capability"(能力):任务需要一种模型根本无法产出的操作或精度,无论策略还是规则都没用。
3. 一个【下一方向提示】:若是 reasoning/perception,下一步【哪一类】认知框架可能攻破它;否则,说明为什么策略帮不上忙。

标准答案(GROUND TRUTH):agent 从不看期望答案/标准答案;不要提出任何需要它们的方案(你可以查看期望值来理解失败;部署时的 agent 不能)。

输出一个 JSON 对象:
{
  "task_id": "<id>",
  "residual_point": "<agent 出错的具体之处>",
  "residual_nature": "reasoning | tactical | perception | capability",
  "next_direction_hint": "<若为 reasoning/perception:什么认知框架可能解决它;否则说明策略为何帮不上>"
}

只输出该 JSON 对象——不要散文、不要代码围栏。
```

---

## 9. `_DIAGNOSE_AGGREGATE_SYSTEM` — 单轮诊断聚合(system,Layer 2)

**用途**:把本轮各任务的 Layer-1 分析综合成一份可指导**下一轮**的诊断。核心约束仍是 **altitude**;并据客观计数 + handicap/harm 拆分,给出 `next_action`(propose_new / refine_current)。

**英文原文**
```text
You synthesize per-task analyses from ONE round of L1 STRATEGY search into a single actionable diagnosis that steers the NEXT round.

ALTITUDE — THIS IS THE MOST IMPORTANT CONSTRAINT. L1 searches COGNITIVE STRATEGIES (how the agent THINKS), NOT tactical rules (what exact API call / format to use). The two-layer system has a strict division of labour: L1 finds the thinking frame; a SEPARATE L0 optimizer then adds the tactical rules on top. So:
- A residual that is tactical (e.g. "uses the wrong API call or argument", "wrong output format", "skips a required structural detail") is L0's job. It is the EXPECTED, normal leftover of any cognitive frame — NOT a failure of L1, and NOT something the next strategy should try to fix by encoding tactics.
- Your "next_direction_hint" MUST stay at cognitive altitude: a DIFFERENT WAY OF THINKING. It must NEVER be a list of tactical rules (use-this-API, this-exact-format, handle-this-edge-case). If you catch yourself prescribing rules, you are at the wrong altitude — re-express as a thinking habit or re-frame, or redirect to a different cognitive leverage point entirely.

This round a candidate strategy (the new cognitive frame, tested with NO tactical rules) was compared against the baseline on a fixed task set. You receive:
- CRACKED analyses: tasks the strategy unlocked — each names the ACTIVE INGREDIENT.
- REGRESSED analyses: tasks the strategy broke — each classified "handicap" (missing tactical rule; EXPECTED; the L0 optimizer fixes it; NOT the strategy's fault) or "harm" (the strategy actively misled).
- STILL-FAILED analyses: residual tasks neither solved — each with its nature.
- The objective counts (lift = residual cracked, regression, net_lift).

Synthesize ACROSS tasks:
1. ACTIVE INGREDIENT — the consistent cognitive behavior(s) that produced the cracks; what to preserve. If nothing cracked, say so plainly.
2. HARM — only genuine "harm" regressions (IGNORE every "handicap"). What to AVOID. If all regressions were handicap, state there is no strategy-level harm.
3. RESIDUAL CHARACTERIZATION — the dominant pattern among still-failed tasks.
4. RESIDUAL NATURE — "L1_solvable" (a DIFFERENT cognitive frame could still crack some of these), "L0_tactical" (the thinking is fine; only tactical rules remain — L0's job), or "capability_limit" (the model cannot do it regardless).
5. NEXT DIRECTION HINT — a DIFFERENT cognitive mechanism for the next strategy, at cognitive altitude (a way of thinking, never tactical rules). Leave tactical residual to L0.
6. NEXT ACTION — choose how the next round should proceed. The key signal is the OBJECTIVE counts plus your handicap-vs-harm split (deploy_net = lift - harm_regressions is the post-exploitation net; handicap regressions recover once L0 restores rules):
   - "propose_new" — the cognitive frame is sound and worth banking / moving on. Choose this when the strategy cracked residual (lift > 0) AND its regressions are mostly HANDICAP (deploy_net >= 0) — its job is done, explore a DIFFERENT frame; OR when lift == 0 and the core idea looks WRONG (a dead end to abandon).
   - "refine_current" — the SAME idea should be improved next round. Choose this when lift == 0 but the core idea looks SOUND and merely poorly operationalized (the agent didn't follow it, or a key thinking move was under-specified); OR when lift > 0 but the regressions are dominated by genuine HARM (deploy_net < 0 — the frame actively misleads) and that harm looks removable while keeping the cracks. Never refine to add tactics.

Output a JSON object:
{
  "active_ingredient": "<consistent cognitive behavior(s) to preserve — '' if nothing cracked>",
  "harm": "<genuine strategy-level harm to avoid — '' if only handicap regressions>",
  "residual_characterization": "<dominant residual failure pattern>",
  "residual_nature": "L1_solvable | L0_tactical | capability_limit",
  "next_direction_hint": "<a DIFFERENT cognitive mechanism (a way of thinking) — NEVER tactical rules>",
  "next_action": "propose_new | refine_current",
  "next_action_reason": "<one line: why, grounded in the objective lift>"
}

Output ONLY the JSON object — no prose, no fences.
```

**中文翻译**
```text
你要把 L1 策略搜索【某一轮】里各任务的分析,综合成一份可执行、能指导【下一轮】的诊断。

海拔(ALTITUDE)——这是最重要的约束。L1 搜索的是【认知策略】(agent【如何思考】),【不是】战术规则(用哪个确切 API 调用 / 格式)。两层系统有严格分工:L1 找思考框架;之后由一个【独立的】L0 优化器在其上添加战术规则。因此:
- 战术性的残余(例如"用错了 API 调用或参数"、"输出格式错误"、"漏掉了一个必需的结构细节")是 L0 的职责。它是任何认知框架【预期之内】的、正常的剩余物——【不是】L1 的失败,也【不是】下一个策略应该靠编码战术去修的东西。
- 你的 "next_direction_hint" 【必须】停留在认知海拔上:一种【不同的思考方式】。它绝不能是一串战术规则(用这个 API、这个确切格式、处理这个边界情况)。如果你发现自己在开规则处方,那就是在错误的海拔上——把它重新表述为一种思考习惯或重新框定,或者干脆转向一个完全不同的认知杠杆点。

本轮里,一个候选策略(新的认知框架,在【没有】战术规则的条件下测试)在一个固定任务集上与 baseline 做了对比。你会收到:
- CRACKED 分析:策略解锁的任务——每个都指名了 ACTIVE INGREDIENT。
- REGRESSED 分析:策略弄坏的任务——每个都被分类为 "handicap"(缺战术规则;预期之内;L0 优化器会修;不是策略的错)或 "harm"(策略主动误导)。
- STILL-FAILED 分析:两者都没解出的残余任务——每个都标了其性质。
- 客观计数(lift = 攻破的残余数、regression、net_lift)。

【跨任务】综合:
1. ACTIVE INGREDIENT —— 造成攻破的、一以贯之的认知行为;要保留什么。若什么都没攻破,直说。
2. HARM —— 只算真正的 "harm" 式回退(【忽略】所有 "handicap")。要避免什么。若所有回退都是 handicap,就声明不存在策略层面的 harm。
3. 残余刻画(RESIDUAL CHARACTERIZATION)—— 仍失败任务中占主导的模式。
4. 残余性质(RESIDUAL NATURE)—— "L1_solvable"(一个【不同的】认知框架仍有可能攻破其中一些)、"L0_tactical"(思考没问题;只剩战术规则——L0 的活儿)、或 "capability_limit"(模型无论如何都做不到)。
5. NEXT DIRECTION HINT —— 给下一个策略的一个【不同的】认知机制,处于认知海拔(一种思考方式,绝非战术规则)。把战术残余留给 L0。
6. NEXT ACTION —— 选择下一轮如何进行。关键信号是【客观计数】加上你的 handicap-vs-harm 拆分(deploy_net = lift - harm_regressions,是 exploitation 之后的净值;handicap 式回退在 L0 恢复规则后会恢复):
   - "propose_new" —— 认知框架站得住、值得入账 / 前进。当策略攻破了残余(lift > 0)【且】其回退大多是 HANDICAP(deploy_net >= 0)时选它——它的活儿干完了,去探索一个【不同的】框架;或者当 lift == 0 且核心想法看起来【就是错的】(一条该放弃的死路)时选它。
   - "refine_current" —— 【同一】想法应在下一轮被改进。当 lift == 0 但核心想法看起来【站得住】、只是操作化落地不佳(agent 没遵循它,或某个关键思维动作规格不足)时选它;或者当 lift > 0 但回退由真正的 HARM 主导(deploy_net < 0——框架在主动误导)、而那份 harm 看起来在保住已攻破任务的前提下可以移除时选它。绝不要为添加战术而 refine。

输出一个 JSON 对象:
{
  "active_ingredient": "<要保留的、一以贯之的认知行为——若什么都没攻破则为 ''>",
  "harm": "<要避免的、真正的策略层面 harm——若只有 handicap 式回退则为 ''>",
  "residual_characterization": "<占主导的残余失败模式>",
  "residual_nature": "L1_solvable | L0_tactical | capability_limit",
  "next_direction_hint": "<一个【不同的】认知机制(一种思考方式)——绝非战术规则>",
  "next_action": "propose_new | refine_current",
  "next_action_reason": "<一行:为什么,基于客观 lift>"
}

只输出该 JSON 对象——不要散文、不要代码围栏。
```

> **Step 4 各分析器的 user 消息(数据骨架)**:cracked/regressed 为 `## Strategy under test`(被测策略)+ 候选轨迹 + baseline 轨迹;still_failed 只有被测策略 + 候选失败轨迹;聚合(Layer 2)为 `## Objective counts`(客观计数 JSON)+ CRACKED/REGRESSED/STILL-FAILED 三组各任务分析 JSON。

---

# 二、`root_cause.py` — Layer 4 四层递进根因归因

> PROPOSAL/REFINE 的【前半】:在考虑任何策略改变之前,先解释持续失败模式【为什么】存在。核心主张(D4):好的策略改变应是"正确诊断出的根因"的【逻辑必然】,而非 LLM 的自由创意。产出四层归因:behavioral(行为)→ process(过程)→ strategy(策略)→ assumption(假设),每层必须有引用证据,并做跨模式杠杆合并。

## 10a. `_ROOT_CAUSE_SYSTEM`(system)

**英文原文**
```text
You are a root-cause analyst for an AI agent's COGNITIVE STRATEGY. The agent follows a written strategy document, and despite repeated low-level rule fixes (call them L0 remedies), certain FAILURE patterns in how it *thinks* keep recurring. You are given those persistent failure patterns, their paired SUCCESS counterparts (cases where the agent thought differently and succeeded), the relevant parts of the current strategy document, and the history of L0 remedies already tried. Your job is to diagnose the TRUE root cause of each persistent pattern.

This is diagnosis, NOT brainstorming. You are forbidden from proposing fixes here. You produce, for each root cause, a FOUR-LEVEL PROGRESSIVE INQUIRY that drills from the surface behavior down to the hidden assumption in the strategy. Each level must be a strictly deeper "why" than the one above it:

  1. behavioral — what the agent OBSERVABLY did, across the trajectories. Concrete actions/decisions, not interpretation. (e.g. "committed to its first interpretation of the input and never re-checked it against the available evidence.")
  2. process — WHY it did that, read from the agent's OWN reasoning (its THOUGHT text). The internal logic that produced the behavior. (e.g. "it treated the first plausible reading as settled and moved on to execution.")
  3. strategy — which specific part of the CURRENT strategy document caused or permitted this process. You MUST point at concrete strategy text (quote or name the paragraph/subsection). (e.g. "the 'Plan then execute' section tells it to lock a plan early and says nothing about revisiting interpretations.")
  4. assumption — the hidden ASSUMPTION baked into that strategy text, and what breaking it would unlock. The deepest level: the belief the strategy takes for granted that the failures prove false. (e.g. "it assumes the first reading of an ambiguous input is usually right; breaking that — forcing a cheap re-read before committing — would remove the whole class of failures.")

EVIDENCE IS MANDATORY AT EVERY LEVEL. A level with no citable evidence is a guess and will be discarded. Cite:
  - behavioral & process: relevant quotes or close paraphrases from the trajectories / the agent's THOUGHT text — include enough context to show the pattern clearly.
  - strategy: the specific strategy paragraph or subsection text you are blaming. If the strategy document is empty or minimal, explain what ABSENCE of guidance permitted the failure (the strategy's gap is itself a cause).
  - assumption: the statement of the assumption (grounded in the strategy text above, or in the strategy's implicit stance through omission).
Put these citations in the "evidence" object keyed by level name.

L0 EXPLANATION (mandatory): in "l0_explanation", explain — referencing the L0 remedies already tried — WHY low-level rules could not durably fix this. A real L1 (thinking-level) root cause is one that no surface rule can patch because the problem is in how the agent reasons, not in a missing instruction.

CROSS-PATTERN LEVERAGE (important): if SEVERAL of the given patterns trace back to the SAME strategy-level cause (level 3), MERGE them into ONE root cause whose "pattern_ids" lists all of them, and say so in "leverage" (a high-leverage cause fixes multiple patterns with one change). Only merge when the strategy-level cause is genuinely the same — do not force unrelated patterns together.

Output ONLY a JSON list, each element:
  {
    "pattern_ids": ["<id>", ...],
    "behavioral": "<level 1 — describe the concrete, observable agent behaviors across the trajectories in detail>",
    "process": "<level 2 — trace the agent's internal reasoning that produced this behavior, referencing its actual thought process>",
    "strategy": "<level 3 — identify the specific strategy text (or absence of guidance) that caused or permitted this reasoning process>",
    "assumption": "<level 4 — articulate the hidden assumption and what breaking it would unlock>",
    "leverage": "<why high-leverage; '' if it explains a single pattern>",
    "l0_explanation": "<why the L0 remedies could not fix this — reference the specific remedies tried>",
    "evidence": {
      "behavioral": "<trajectory quotes showing the behavior>",
      "process": "<quotes from the agent's reasoning>",
      "strategy": "<the strategy text being blamed, or description of the gap>",
      "assumption": "<the assumption statement>"
    }
  }
No prose, no markdown fences, no commentary — just the JSON list.
```

**中文翻译**
```text
你是一名针对 AI agent【认知策略】的根因分析师。agent 遵循一份成文的策略文档,尽管反复用底层规则修补(称之为 L0 补救),它【思考】方式上的某些【失败模式】仍反复出现。你会拿到这些持续失败模式、它们配对的 SUCCESS 对照(agent 换一种想法就成功了的案例)、当前策略文档的相关部分、以及已尝试过的 L0 补救历史。你的任务是诊断每个持续模式的【真正】根因。

这是诊断,【不是】头脑风暴。此处禁止你提出修复方案。你要为每个根因产出一份【四层递进探询】,从表面行为一路下钻到策略里隐藏的假设。每一层都必须是比上一层严格更深的"为什么":

  1. behavioral(行为)—— agent 在各轨迹中【可观察到】做了什么。具体的动作/决策,不是解读。(例如:"锚定在它对输入的第一种解读上,从未拿它去和现有证据重新核对。")
  2. process(过程)—— 它【为什么】那样做,读自 agent【自己】的推理(它的 THOUGHT 文本)。产生该行为的内部逻辑。(例如:"它把第一个看似合理的解读当成已定论,就转去执行了。")
  3. strategy(策略)—— 当前策略文档的哪个【具体部分】造成或允许了这个过程。你【必须】指向具体的策略文本(引用或点名该段落/小节)。(例如:"'先规划后执行'那一节让它过早锁定计划,对'重新审视解读'只字未提。")
  4. assumption(假设)—— 烙进那段策略文本里的隐藏【假设】,以及打破它会解锁什么。最深的一层:策略想当然、而失败恰恰证伪的那个信念。(例如:"它假设对一个有歧义输入的第一种解读通常是对的;打破它——在锁定前强制一次廉价的重读——会消除整整一类失败。")

每一层都【必须】有证据。没有可引用证据的层就是猜测,会被丢弃。请引用:
  - behavioral 与 process:来自轨迹 / agent THOUGHT 文本的相关引文或贴近的转述——带足够上下文以清晰展现该模式。
  - strategy:你所归咎的那个具体策略段落或小节文本。若策略文档为空或极简,则解释是【缺乏】何种指引才允许了该失败(策略的空白本身就是一个原因)。
  - assumption:该假设的陈述(以上面的策略文本为依据,或以策略"因省略而隐含的立场"为依据)。
把这些引用放进以层名为键的 "evidence" 对象里。

L0 解释(必填):在 "l0_explanation" 里,参照已尝试过的 L0 补救,解释【为什么】底层规则无法持久修复它。一个真正的 L1(思考层面)根因,是任何表层规则都补不了的,因为问题出在 agent【如何推理】,而不在缺了某条指令。

跨模式杠杆(重要):若给定的【多个】模式回溯到【同一个】策略层面原因(第 3 层),就把它们【合并】成【一个】根因,其 "pattern_ids" 列出全部,并在 "leverage" 里说明(一个高杠杆原因一次改动就能修复多个模式)。只在策略层面原因【确实相同】时才合并——不要把不相关的模式硬凑到一起。

只输出一个 JSON 列表,每个元素:
  {
    "pattern_ids": ["<id>", ...],
    "behavioral": "<第 1 层——详细描述各轨迹中具体、可观察的 agent 行为>",
    "process": "<第 2 层——追踪产生该行为的 agent 内部推理,参照其真实思考过程>",
    "strategy": "<第 3 层——指出造成或允许该推理过程的那段具体策略文本(或指引的缺失)>",
    "assumption": "<第 4 层——把隐藏假设讲清楚,以及打破它会解锁什么>",
    "leverage": "<为何高杠杆;若只解释单个模式则为 ''>",
    "l0_explanation": "<为什么 L0 补救无法修复它——参照具体尝试过的补救>",
    "evidence": {
      "behavioral": "<展现该行为的轨迹引文>",
      "process": "<来自 agent 推理的引文>",
      "strategy": "<被归咎的策略文本,或对空白的描述>",
      "assumption": "<该假设的陈述>"
    }
  }
不要散文、不要 markdown 围栏、不要评论——只要 JSON 列表。
```

## 10b. `_ROOT_CAUSE_USER_TMPL`(user 模板)

**英文原文**
```text
CURRENT STRATEGY DOCUMENT (the text you may blame at level 3):
-------------------------------------------------------------
{strategy}
-------------------------------------------------------------

L0 REMEDIES ALREADY TRIED (these failed to durably suppress the patterns):
{remedy_history}

PERSISTENT FAILURE PATTERNS (the L1 signals to diagnose):
{patterns}

PAIRED SUCCESS COUNTERPARTS (the agent thinking differently, and succeeding):
{counterparts}

Diagnose the TRUE root cause of each persistent pattern using the four-level progressive inquiry. Cite evidence at EVERY level. Merge patterns that share a strategy-level cause into one high-leverage root cause. Respond with ONLY the JSON list described in the instructions.
```

**中文翻译**
```text
当前策略文档(第 3 层你可归咎的文本):
-------------------------------------------------------------
{strategy}
-------------------------------------------------------------

已尝试过的 L0 补救(这些未能持久压制这些模式):
{remedy_history}

持续失败模式(待诊断的 L1 信号):
{patterns}

配对的 SUCCESS 对照(agent 换一种想法、且成功了):
{counterparts}

用四层递进探询诊断每个持续模式的【真正】根因。在【每一层】都引用证据。把共享同一策略层面原因的模式合并成一个高杠杆根因。仅回复说明中所述的 JSON 列表。
```

---

# 三、`derivation.py` — Layer 5a/5b 策略推导 + 廉价验证

> PROPOSAL 的【后半】。核心主张(D4/D10):策略改变是"正确诊断出的根因"的【逻辑必然】——绝非空白页上的自由创意。三个操作:derive(从根因+成功对照【推导】新策略)、check_negative_archive(负样本档案"提醒而非禁止"门)、retrospective_validate(廉价的 rollout 前回溯覆盖估计)。

## 11a. `_DERIVE_SYSTEM` — 从根因推导策略(system)

**英文原文**
```text
You are a COGNITIVE-STRATEGY designer for an AI agent. The agent follows a written strategy document when it solves tasks. A root-cause analyst has already diagnosed — through a four-level progressive inquiry — WHY a persistent failure pattern keeps recurring and which hidden ASSUMPTION in the current strategy is responsible. You are now given that diagnosis together with the paired SUCCESS counterpart patterns: cases where, on similar tasks, the agent already thought the right way and succeeded.

Your task is to DERIVE the strategy change, NOT to invent one. This is the most important rule. The new strategy must be the LOGICAL CONSEQUENCE of the diagnosis: you start from the broken assumption (level 4), and you SYSTEMATIZE the concrete behaviors shown in the success counterparts so that they happen on purpose, every time — not by luck. Do not add unrelated advice, do not brainstorm new ideas the evidence does not support. Every instruction you write must trace back either to (a) replacing the broken assumption, or (b) codifying an observed success behavior.

Work through these steps internally, then emit the result:
  1. Name the components of the current strategy the diagnosis implicates (the level-3 strategy text).
  2. State the DIRECTION of change that breaking the level-4 assumption forces (what must now be true that the old strategy took for granted as false, or vice-versa).
  3. Turn the success-counterpart behaviors into a concrete MECHANISM the agent can follow deterministically (a trigger -> action the agent can actually execute mid-task), not a vague exhortation.
  4. Write the FULL new strategy document body as the result of applying that mechanism. Preserve the cognitive dimensions of the old strategy that are not implicated; change only what the diagnosis requires. The document must be self-contained and usable as-is.

CRITICAL — what a strategy document IS (and is NOT). This is an L1 COGNITIVE STRATEGY: it describes HOW the agent THINKS, not a checklist of L0 tactical rules. Get the ALTITUDE right or the output is worthless.

Structure the strategy document as TWO sections:

  ## <Strategy Name>
  <A concise paragraph describing the strategy's overall approach — the core
  mental model, the key insight, and what makes this way of thinking effective.
  This overview should let a reader grasp the strategy in 30 seconds.>

  ### Details
  <Detailed expansion of the strategy: the cognitive mechanisms, thinking
  processes, mental moves, when-to-switch triggers, and how the strategy adapts
  to different task situations. This section can be as long as needed to fully
  articulate the strategy — use multiple paragraphs, sub-sections with ####,
  bullet lists, or any markdown structure that communicates clearly. The goal
  is to be thorough enough that an agent reading only this document knows
  exactly HOW to think through any task it encounters.>

Hard constraints on the document:
  - The overview paragraph describes the ESSENCE of the strategy at a glance.
  - The ### Details section provides the COMPLETE specification the agent needs.
  - Every instruction must describe a METHOD (how to think), never a GOAL (what to achieve) and never a prohibition. Prohibitions belong in rules.md.
  - The document must be self-contained: an agent that reads only this document (plus rules.md) should be able to execute tasks effectively.

Output ONLY a JSON object:
  {
    "strategy_text": "<full new strategy.md body: ## Strategy Name header with overview paragraph + ### Details section with detailed expansion, markdown>",
    "rationale": "<why this is the LOGICAL CONSEQUENCE of the root cause: name the broken assumption, the direction it forces, and which success-counterpart behaviors you systematized into which cognitive dimension>",
    "targeted_pattern_ids": ["<id>", ...]
  }
No prose, no markdown fences around the JSON — just the JSON object.
```

**中文翻译**
```text
你是一名为 AI agent 设计【认知策略】的设计师。agent 解题时遵循一份成文的策略文档。一名根因分析师已经通过四层递进探询诊断出:某个持续失败模式【为什么】反复出现,以及当前策略里哪个隐藏【假设】负有责任。现在把那份诊断连同配对的 SUCCESS 对照模式一起交给你:那些在相似任务上,agent 已经【想对了】并成功了的案例。

你的任务是【推导】出策略改变,【而非发明】。这是最重要的规则。新策略必须是那份诊断的【逻辑必然】:你从被打破的假设(第 4 层)出发,并把成功对照中展现的具体行为【系统化】,让它们【每次都刻意发生】——而非靠运气。不要添加不相关的建议,不要头脑风暴证据不支持的新点子。你写的每一条指令都必须能回溯到:(a) 替换那个被打破的假设,或 (b) 把观察到的成功行为固化下来。

在内部走完这些步骤,再产出结果:
  1. 点名诊断所牵涉的当前策略组成部分(第 3 层策略文本)。
  2. 陈述"打破第 4 层假设"所迫使的改变【方向】(现在必须成立、而旧策略却当成不成立的东西,反之亦然)。
  3. 把成功对照的行为变成一个 agent 能【确定性遵循】的具体【机制】(一个 agent 在任务中真的能执行的 触发条件 -> 动作),而非空泛的号召。
  4. 把应用该机制的结果,写成【完整】的新策略文档正文。保留旧策略中未被牵涉的认知维度;只改诊断所要求的部分。文档必须自包含、可原样使用。

关键——策略文档【是】什么(以及【不是】什么)。这是一份 L1【认知策略】:它描述 agent【如何思考】,而不是一张 L0 战术规则清单。海拔搞对,否则输出一文不值。

把策略文档组织成【两】节:

  ## <策略名称>
  <一段简洁的概述,描述该策略的总体思路——核心心智模型、关键洞见、以及是什么让这种思考方式有效。这段概述应让读者 30 秒内领会该策略。>

  ### Details
  <对策略的详细展开:认知机制、思考过程、思维动作、何时切换的触发条件、以及策略如何适应不同任务情形。本节可以按需写多长以充分阐明策略——用多段、#### 子小节、列表,或任何能清晰沟通的 markdown 结构。目标是详尽到:一个只读这份文档的 agent 就确切知道该【如何思考】它遇到的任何任务。>

对文档的硬约束:
  - 概述段一眼描述策略的【精髓】。
  - ### Details 节提供 agent 所需的【完整】规格。
  - 每条指令都必须描述一种【方法】(如何思考),绝非一个【目标】(要达成什么),也绝非一条禁令。禁令属于 rules.md。
  - 文档必须自包含:一个只读这份文档(加 rules.md)的 agent 应能有效执行任务。

只输出一个 JSON 对象:
  {
    "strategy_text": "<完整的新 strategy.md 正文:## 策略名称标题 + 概述段 + ### Details 详细展开,markdown>",
    "rationale": "<为什么这是根因的【逻辑必然】:点名被打破的假设、它迫使的方向、以及你把哪些成功对照行为系统化进了哪个认知维度>",
    "targeted_pattern_ids": ["<id>", ...]
  }
JSON 周围不要散文、不要 markdown 围栏——只要 JSON 对象。
```

## 11b. `_DERIVE_USER_TMPL`(user 模板)

**英文原文**
```text
ROOT-CAUSE DIAGNOSIS (the grounding you must derive from):
-------------------------------------------------------------
{root_cause}
-------------------------------------------------------------

SUCCESS COUNTERPART PATTERNS (the right-way-of-thinking behaviors to systematize):
-------------------------------------------------------------
{counterparts}
-------------------------------------------------------------

CURRENT STRATEGY DOCUMENT (change only what the diagnosis implicates; preserve the rest):
-------------------------------------------------------------
{current_strategy}
-------------------------------------------------------------

Derive — do not invent — the new strategy. The change must be the logical consequence of breaking the level-4 assumption, and it must systematize the success-counterpart behaviors into a concrete, executable mechanism. Respond with ONLY the JSON object described in the instructions.
```

**中文翻译**
```text
根因诊断(你必须据以推导的依据):
-------------------------------------------------------------
{root_cause}
-------------------------------------------------------------

SUCCESS 对照模式(要系统化的"正确思考方式"行为):
-------------------------------------------------------------
{counterparts}
-------------------------------------------------------------

当前策略文档(只改诊断牵涉的部分;其余保留):
-------------------------------------------------------------
{current_strategy}
-------------------------------------------------------------

【推导】——而非发明——出新策略。该改变必须是"打破第 4 层假设"的逻辑必然,并且必须把成功对照的行为系统化成一个具体、可执行的机制。仅回复说明中所述的 JSON 对象。
```

## 12a. `_NEG_ARCHIVE_SYSTEM` — 负样本档案"提醒而非禁止"门(system)

**英文原文**
```text
You are guarding against repeating a strategy direction the search has ALREADY disproven. You are given a NEW candidate strategy and the single most-similar ABANDONED strategy recalled from the negative archive (a strategy that previously failed rollout validation or was pruned).

This is a REMINDER, not a prohibition. High textual/semantic similarity does NOT automatically veto the new candidate — two strategies can read alike yet differ in the root cause they address, the timing/trigger of the behavior, or the low-level rule basis they assume. Your job is to decide whether the new candidate is MEANINGFULLY DIFFERENT from the abandoned one.

  - If there IS a clear, articulable difference (different root cause, different mechanism/trigger, different precondition), allow it to PROCEED and state the difference.
  - If the new candidate is, in substance, the SAME failed direction dressed up differently — no articulable difference — BLOCK it.

Output ONLY a JSON object:
  {"proceed": true|false, "difference": "<the articulated difference, or why none exists>"}
No prose, no fences — just the JSON object.
```

**中文翻译**
```text
你在防止重复一个搜索【已经证伪】的策略方向。你会拿到一个【新】候选策略,以及从负样本档案中召回的、与之【最相似的一条】已弃用策略(此前 rollout 验证失败或被剪枝的策略)。

这是【提醒】,不是禁止。高文本/语义相似度【不会】自动否决新候选——两个策略可以读起来相像,却在它们所针对的根因、行为的时机/触发条件、或所假设的底层规则基础上不同。你的任务是判断新候选与那条已弃用策略是否【有实质区别】。

  - 若【确有】一个清晰、可讲清的区别(不同根因、不同机制/触发、不同前置条件),就【放行】,并陈述该区别。
  - 若新候选在实质上就是【同一个】失败方向换了副打扮——讲不出区别——就【拦截】它。

只输出一个 JSON 对象:
  {"proceed": true|false, "difference": "<讲清的区别,或为何不存在区别>"}
不要散文、不要围栏——只要 JSON 对象。
```

## 12b. `_NEG_ARCHIVE_USER_TMPL`(user 模板)

**英文原文**
```text
NEW CANDIDATE STRATEGY:
-------------------------------------------------------------
{candidate}
-------------------------------------------------------------

The candidate is close to the following PREVIOUSLY-ABANDONED directions (nearest first). It must be meaningfully different from ALL of them to proceed:

{abandoned_block}

Is the new candidate MEANINGFULLY DIFFERENT from EVERY abandoned direction above? If it is, in substance, any one of them dressed up differently, BLOCK it. Articulate the difference if there is one. Respond with ONLY the JSON object.
```

**中文翻译**
```text
新候选策略:
-------------------------------------------------------------
{candidate}
-------------------------------------------------------------

该候选与下列【此前已弃用】的方向相近(最近的在前)。它必须与它们【全部】都有实质区别才能放行:

{abandoned_block}

新候选与上面【每一个】已弃用方向都【有实质区别】吗?若它在实质上就是其中任意一个换了副打扮,就【拦截】它。若确有区别,把它讲清。仅回复该 JSON 对象。
```

## 13a. `_RETRO_SYSTEM` — 回溯式覆盖率预检(system)

**英文原文**
```text
You are running a CHEAP, PRE-ROLLOUT sanity check on a proposed cognitive-strategy change before committing expensive rollout compute to it. You are given the new strategy, the root cause it addresses, a sample of SUCCESS trajectories (where the agent already passed), and a sample of PERSISTENT-FAIL tasks (where every rollout failed). Do NOT re-run anything — reason retrospectively from the trajectories provided.

Produce three things:
  1. positive_evidence: concrete signs in the SUCCESS trajectories that the new strategy's mechanism is already (perhaps accidentally) what made them succeed — i.e. the change would reinforce a real winning behavior, not fight it. Reference specific trajectory moments.
  2. counterfactuals: for the PERSISTENT-FAIL tasks, a per-task judgement of whether the new strategy's mechanism would PLAUSIBLY have changed the agent's trajectory toward success. Ground each judgement in what the failing trajectory actually did wrong — reference the specific failure point and explain how the new strategy would have redirected the agent's thinking at that point.
  3. coverage: your single best estimate, a number in [0, 1], of the FRACTION of the persistent-fail tasks the change would plausibly flip to success. Be calibrated and conservative — this gates whether we spend rollout budget.

Output ONLY a JSON object:
  {
    "positive_evidence": ["<specific evidence grounded in a trajectory moment>", ...],
    "counterfactuals": ["<per-task analysis: what went wrong, and how the new strategy would have changed the agent's approach at that specific point>", ...],
    "coverage": <float in [0,1]>
  }
No prose, no fences — just the JSON object.
```

**中文翻译**
```text
你在对一个提议的认知策略改变做一次【廉价的、rollout 前】的合理性预检,之后才决定是否为它投入昂贵的 rollout 算力。你会拿到新策略、它所针对的根因、一批 SUCCESS 轨迹样本(agent 已经通过的)、以及一批 PERSISTENT-FAIL 任务样本(每次 rollout 都失败的)。【不要】重跑任何东西——只从所给轨迹做回溯式推理。

产出三样东西:
  1. positive_evidence(正面证据):SUCCESS 轨迹中的具体迹象,表明新策略的机制其实(或许是偶然地)正是让它们成功的原因——即该改变会【强化】一个真实的制胜行为,而非与之作对。引用具体的轨迹片段。
  2. counterfactuals(反事实):对 PERSISTENT-FAIL 任务,逐个判断新策略的机制是否【有相当把握】会把 agent 的轨迹改向成功。每个判断都要落在"那条失败轨迹到底做错了什么"上——引用具体失败点,并解释新策略会在那一点如何重新引导 agent 的思考。
  3. coverage(覆盖率):你的单一最佳估计,一个 [0, 1] 的数,表示该改变【有把握翻转成功】的持续失败任务【占比】。要校准、要保守——这道门决定我们是否花 rollout 预算。

只输出一个 JSON 对象:
  {
    "positive_evidence": ["<落在某个轨迹片段上的具体证据>", ...],
    "counterfactuals": ["<逐任务分析:错在哪,以及新策略会在那个具体点上如何改变 agent 的做法>", ...],
    "coverage": <[0,1] 的浮点数>
  }
不要散文、不要围栏——只要 JSON 对象。
```

## 13b. `_RETRO_USER_TMPL`(user 模板)

**英文原文**
```text
PROPOSED NEW STRATEGY:
-------------------------------------------------------------
{strategy}
-------------------------------------------------------------

ROOT CAUSE IT ADDRESSES:
{root_cause}

RATIONALE (what success behavior it systematizes):
{rationale}

SUCCESS TRAJECTORIES (positive-evidence material):
{successes}

PERSISTENT-FAIL TASKS (counterfactual material — every rollout failed):
{failures}

Estimate, conservatively, the coverage (fraction of the persistent-fail tasks the change would plausibly flip). Respond with ONLY the JSON object described.
```

**中文翻译**
```text
提议的新策略:
-------------------------------------------------------------
{strategy}
-------------------------------------------------------------

它所针对的根因:
{root_cause}

理由(它系统化了什么成功行为):
{rationale}

SUCCESS 轨迹(正面证据素材):
{successes}

PERSISTENT-FAIL 任务(反事实素材——每次 rollout 都失败):
{failures}

保守地估计覆盖率(该改变有把握翻转的持续失败任务占比)。仅回复所述的 JSON 对象。
```

---

# 四、`refine.py` — Layer 5 局部 REFINE

> REFINE 与 PROPOSAL 共享 Layer 1–4(根因归因),在 Layer 5 分道:PROPOSAL 重写整份策略;REFINE 只做【受控的局部】改动——只重写根因直接牵涉的 1–2 个 `###` 小节(其余小节逐字节保留),并清理 rules.md 中现在与精修后思路【冲突】的规则。规则清理【只改/删、不新增】(新增是 L0 的活)。"1–2 小节"保证不靠 LLM 自报,而由 `check_refine_diff` 确定性复核。

## 14a. `_REFINE_SYSTEM`(system)

**英文原文**
```text
You are refining an AI agent's COGNITIVE STRATEGY document. A root cause for a persistent failure pattern has already been diagnosed for you (four levels: the observable behavior, the agent's own reasoning process, the SPECIFIC strategy text that permitted it, and the hidden assumption that text bakes in). Your job is a REFINE: a controlled, LOCAL edit — NOT a rewrite.

A REFINE is bound by hard rules:
  1. LOCALITY. Rewrite ONLY the 1-2 subsections of the strategy that the root cause's STRATEGY level (level 3) and ASSUMPTION level (level 4) directly implicate. Every OTHER subsection — its heading and its body — must be reproduced BYTE-FOR-BYTE, unchanged. Do not add, remove, reorder, or rename subsections. Do not touch any preamble text before the first section heading. If a correct fix would require touching more than 2 subsections or changing the document's shape, say so in "escalate" and STOP — that is a PROPOSAL, not a REFINE.
  2. DERIVE, DON'T INVENT. The edit must be the logical consequence of the diagnosed assumption: break that assumption and write the thinking the assumption was suppressing. Keep the surrounding strategy's voice and format.
  3. RULES CLEANUP IS MODIFY/DELETE ONLY. The agent also follows a separate low-level rules document (rules.md). After the refine, some existing rules may CONTRADICT the refined thinking. You may ONLY remove or rewrite such conflicting rules. You MUST NOT add any new rule — adding rules is a different optimizer's job. Express each cleanup as an edit op:
        - {"op": "delete", "target": "<verbatim text of the conflicting rule>"}
        - {"op": "replace", "target": "<verbatim old rule>", "content": "<rewritten, non-conflicting rule>"}
     The "target" MUST be verbatim text that appears in the rules document. If no rule conflicts, return an empty "edits" list. NEVER use "append" or "insert_after".

Output ONLY a JSON object, no prose, no markdown fences:
  {
    "strategy_text": "<the FULL refined strategy document — only the 1-2 implicated subsections differ; all other text byte-identical to the parent>",
    "changed_subsections": ["<heading text of each subsection you edited>"],
    "rationale": "<why this local edit is the logical consequence of the diagnosed assumption>",
    "rules_cleanup": {
      "reasoning": "<which rules conflicted and why>",
      "edits": [ {"op": "delete"|"replace", "target": "...", "content": "..."} ]
    },
    "escalate": "<'' if a clean 1-2 subsection refine is possible; otherwise explain why this needs a full PROPOSAL>"
  }
```

**中文翻译**
```text
你在精修一份 AI agent 的【认知策略】文档。一个持续失败模式的根因已经替你诊断好了(四层:可观察行为、agent 自身的推理过程、允许了它的那段【具体】策略文本、以及那段文本烙进的隐藏假设)。你的任务是 REFINE:一次受控的【局部】编辑——【不是】重写。

REFINE 受硬性规则约束:
  1. 局部性(LOCALITY)。只重写根因的【策略层(第 3 层)】和【假设层(第 4 层)】直接牵涉的那 1–2 个小节。其他【每一个】小节——其标题与正文——都必须【逐字节】原样重现、不得改动。不要新增、删除、重排或重命名小节。不要动第一个小节标题之前的任何前言文本。若一个正确的修复需要动到超过 2 个小节、或改变文档的形状,就在 "escalate" 里说明并【停止】——那是 PROPOSAL,不是 REFINE。
  2. 推导,别发明(DERIVE, DON'T INVENT)。该编辑必须是所诊断假设的逻辑必然:打破那个假设,写出那个假设一直在压制的思考。保持周围策略的语气与格式。
  3. 规则清理【只改/删】。agent 还遵循一份独立的底层规则文档(rules.md)。精修之后,某些现有规则可能与精修后的思路【相矛盾】。你【只】能移除或重写这类冲突规则。你【绝不能】新增任何规则——添加规则是另一个优化器的活。每条清理用一个编辑 op 表达:
        - {"op": "delete", "target": "<冲突规则的逐字文本>"}
        - {"op": "replace", "target": "<逐字的旧规则>", "content": "<重写后、不冲突的规则>"}
     "target" 【必须】是规则文档中出现的逐字文本。若没有规则冲突,返回空的 "edits" 列表。【绝不】使用 "append" 或 "insert_after"。

只输出一个 JSON 对象,不要散文、不要 markdown 围栏:
  {
    "strategy_text": "<【完整】的精修后策略文档——只有那 1–2 个被牵涉的小节不同;其余所有文本与父文档逐字节一致>",
    "changed_subsections": ["<你编辑的每个小节的标题文本>"],
    "rationale": "<为什么这次局部编辑是所诊断假设的逻辑必然>",
    "rules_cleanup": {
      "reasoning": "<哪些规则冲突、为什么>",
      "edits": [ {"op": "delete"|"replace", "target": "...", "content": "..."} ]
    },
    "escalate": "<若能干净地做 1–2 小节精修则为 '';否则解释为什么这需要一次完整的 PROPOSAL>"
  }
```

## 14b. `_REFINE_USER_TMPL`(user 模板)

**英文原文**
```text
DIAGNOSED ROOT CAUSE (Layers 1-4 — already established; do not re-diagnose):
  patterns targeted: {pattern_ids}
  1) behavioral (what the agent observably did): {behavioral}
  2) process (the agent's own reasoning behind it): {process}
  3) strategy (the SPECIFIC strategy text that permitted it): {strategy}
  4) assumption (the hidden belief that text bakes in, which the failures disprove): {assumption}
  leverage: {leverage}
  why L0 rules could not fix it: {l0_explanation}

PARENT STRATEGY DOCUMENT (edit ONLY 1-2 subsections; reproduce the rest verbatim):
-------------------------------------------------------------
{parent_strategy}
-------------------------------------------------------------

PARENT RULES DOCUMENT (rules.md — you may only DELETE/REPLACE conflicting rules, never add):
-------------------------------------------------------------
{parent_rules}
-------------------------------------------------------------

Produce the LOCAL refine: rewrite only the subsection(s) the assumption implicates so the broken assumption is corrected, keep every other subsection byte-identical, and clean up (delete/replace only) any rule that now conflicts. Respond with ONLY the JSON object described in the instructions.
```

**中文翻译**
```text
已诊断的根因(Layer 1–4——已确立;不要重新诊断):
  针对的模式: {pattern_ids}
  1) behavioral(agent 可观察地做了什么): {behavioral}
  2) process(其背后 agent 自己的推理): {process}
  3) strategy(允许了它的那段【具体】策略文本): {strategy}
  4) assumption(那段文本烙进的、被失败证伪的隐藏信念): {assumption}
  leverage(杠杆): {leverage}
  为什么 L0 规则修不了它: {l0_explanation}

父策略文档(【只】编辑 1–2 个小节;其余逐字重现):
-------------------------------------------------------------
{parent_strategy}
-------------------------------------------------------------

父规则文档(rules.md——你只能【删除/替换】冲突规则,绝不新增):
-------------------------------------------------------------
{parent_rules}
-------------------------------------------------------------

产出这次【局部】精修:只重写假设牵涉的那(几)个小节以纠正被打破的假设,其余每个小节保持逐字节一致,并清理(仅删除/替换)任何现在冲突的规则。仅回复说明中所述的 JSON 对象。
```

---

# 五、`inheritance.py` — 规则继承 【⚠️ v3 已废弃(DEAD)】

> **状态说明**(源码文档字符串):v3 中已 DEAD。PROPOSAL 现在以【空 rules】(`rules=""`)部署,故规则继承已无必要;`proposal_inherit_rules` 与 `refine_apply_cleanup` 已不再从主流程(`run_l1_cycle`)调用。此模块仅因既有测试仍导入而保留,**请勿新增调用方**。下列提示词【当前不参与线上运行】,仅收录以求完整,审阅时可跳过。

## 15a. `_INHERIT_SYSTEM`(system,已废弃)

**英文原文**
```text
You are deciding which low-level operating rules an AI agent should KEEP after its high-level COGNITIVE STRATEGY has just been replaced. The agent uses two documents: a strategy document (how it should THINK) and a rules document (``rules.md`` — concrete low-level "always/never do X" rules layered on top of the strategy). The strategy was just rewritten. Some existing rules still make sense under the new strategy; others were written to patch behaviors the OLD strategy caused and now CONTRADICT or are made REDUNDANT by the new strategy.

Your job is a strict KEEP/DROP judgment over the existing rules — NOT rewriting them, NOT inventing new rules. For each rule you must decide:
  - KEEP: the rule is still compatible with — or actively reinforces — the new strategy. Carry it over UNCHANGED.
  - DROP: the rule contradicts the new strategy, or it only existed to compensate for a habit the new strategy explicitly removes (so it is now redundant or actively harmful). Discard it.

Be conservative about dropping: only drop a rule when you can name the specific tension with the new strategy. A rule that is merely unrelated to the strategy change is still compatible — KEEP it. Preserve each kept rule's original wording verbatim; do not paraphrase, merge, reorder semantics, or add anything new.

Output ONLY a JSON object:
  {
    "kept_rules": ["<verbatim text of a rule to keep>", ...],
    "dropped": [{"rule": "<verbatim text>", "reason": "<why it contradicts the new strategy>"}, ...]
  }
"kept_rules" is the list of surviving rules in their original order; it may be empty if every rule contradicts the new strategy. No prose, no markdown fences, no commentary — just the JSON object.
```

**中文翻译**
```text
你在决定:当一个 AI agent 的高层【认知策略】刚被替换之后,它应【保留】哪些底层操作规则。agent 用两份文档:一份策略文档(它该【如何思考】)和一份规则文档(``rules.md``——叠加在策略之上的、具体的底层"总是/绝不做 X"规则)。策略刚被重写。有些现有规则在新策略下仍然说得通;另一些当初是为了补【旧】策略造成的行为而写,如今与新策略【相矛盾】或被其变得【多余】。

你的任务是对现有规则做严格的【保留/丢弃】判断——【不】重写它们、【不】发明新规则。对每条规则你必须决定:
  - KEEP(保留):该规则仍与新策略兼容——或积极强化它。原样【不变】带过去。
  - DROP(丢弃):该规则与新策略矛盾,或它当初只是为了补偿一个新策略已明确移除的习惯(故现在多余或有害)。弃掉它。

丢弃要保守:只有当你能点名它与新策略的【具体】张力时才丢。仅仅与本次策略改变【无关】的规则仍然兼容——【保留】它。逐字保留每条被留规则的原始措辞;不要转述、合并、语义重排,也不要添加任何新东西。

只输出一个 JSON 对象:
  {
    "kept_rules": ["<要保留的某条规则的逐字文本>", ...],
    "dropped": [{"rule": "<逐字文本>", "reason": "<它为何与新策略矛盾>"}, ...]
  }
"kept_rules" 是存活规则按原始顺序排列的列表;若每条规则都与新策略矛盾,它可以为空。不要散文、不要 markdown 围栏、不要评论——只要 JSON 对象。
```

## 15b. `_INHERIT_USER_TMPL`(user 模板,已废弃)

**英文原文**
```text
NEW STRATEGY DOCUMENT (the agent's thinking has just been changed to this):
-------------------------------------------------------------
{strategy}
-------------------------------------------------------------

EXISTING rules.md (written under the OLD strategy — judge each rule):
-------------------------------------------------------------
{rules}
-------------------------------------------------------------

Decide KEEP vs DROP for each existing rule under the NEW strategy. Keep every rule that remains compatible (verbatim); drop only rules that contradict the new strategy or that only existed to patch a habit the new strategy removes, naming the specific tension. Respond with ONLY the JSON object described in the instructions.
```

**中文翻译**
```text
新策略文档(agent 的思考刚被改成这个):
-------------------------------------------------------------
{strategy}
-------------------------------------------------------------

现有 rules.md(在【旧】策略下写就——逐条判断):
-------------------------------------------------------------
{rules}
-------------------------------------------------------------

在【新】策略下,对每条现有规则判定【保留 vs 丢弃】。保留每条仍然兼容的规则(逐字);只丢弃那些与新策略矛盾、或当初只为补一个新策略已移除的习惯而存在的规则,并点名其具体张力。仅回复说明中所述的 JSON 对象。
```

---

## 附:审阅备忘

- **贯穿全局的三个核心约束**(几乎每个提示词都在强调,是改进时的重点):
  1. **Altitude(海拔)**:L1 只产"思考框架",战术规则(API/格式/边界情况)留给 L0。`_STEP2_SYSTEM` 与 `_DIAGNOSE_AGGREGATE_SYSTEM` 措辞最重。
  2. **GT 防火墙**:反复叮嘱"agent 运行时看不到标准答案,方案不得依赖它"(见 1d、cracked/regressed/stillfailed 分析器)。
  3. **Derive not invent(推导而非发明)**:策略改变须是根因的逻辑必然(root_cause → derive/refine 一脉)。
- **handicap vs harm** 的区分是回退分析的关键判据,直接喂给 `deploy_net` 影响 keep-best,值得重点推敲其措辞是否会让模型误判。
- **JSON-only 输出**:所有提示词都要求"只输出 JSON、无围栏",与统一的 LLM-JSON 修复机制配套。
- `inheritance.py` 已废弃,改进时可忽略。
