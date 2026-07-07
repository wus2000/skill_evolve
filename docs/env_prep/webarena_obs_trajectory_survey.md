# WebArena 系 web-agent 的观测/轨迹压缩综述

> 调研执行:后台 survey agent,2026-07-06。触发背景:smoke #2 发现 8 条 WebArena
> 轨迹(单条最大 159k tokens,实测 vLLM /tokenize)塞进一个融合式 reflect prompt
> 撑爆 262k context(HTTP 400 被静默吞)。本文回答两个问题:
> (A) 各工作 acting 时如何控观测尺寸;(B) 从轨迹学习(memory/skill induction)时
> 如何表示长轨迹。置信度标注见文末——引用数字前先看第五部分。

## 抬头结论

- **全场没有一个工作是"把多条完整轨迹塞进一个 reflection prompt"的**。所有工作都在
  acting time 就规避了 context 爆炸:(1) prompt 里永远只放一条当前观测;(2) 历史一律以
  action list 而非堆叠 observation 的形式携带。针对"minibatch 多轨迹反思做 rule 编辑"
  的场景没有现成配方,但有两条可拼装主线(见第四部分)。
- **官方 WebArena 观测截断默认 `max_obs_length=1920` token(前缀截断)**,非流传的
  3840(源码亲验:run.py argparse default=1920;3840 仓库查无;VisualWebArena 的
  15360 是图像模态)。`observation_type` 默认 accessibility_tree,
  `current_viewport_only` 默认 False(整页),viewport 1280×720,max_steps=30。
- **(B) 学习期最反复出现的一招**:反思/归纳时绝不喂 AXTree,用 agent 自己的 thinking
  或一句话 NL state 描述替代每一步观测。ReasoningBank 脚注 1 明确归功于 AWM/ASI 一脉;
  ASI 量化为每步 87.9→13.4 token。

---

## 第一部分 —— (A) acting-time 观测处理

**1. WebArena 原版(社区标准 baseline)** — Zhou et al., arXiv:2307.13854, NeurIPS'23 D&B
- 观测=accessibility tree,每节点一行 `[id] role 'name'`。Token cap 1920,前缀截断
  (`tokenizer.decode(encode(obs)[:1920])`)。默认整页;viewport-only 开启时丢弃可见比例
  <60% 的节点。**历史 = 仅当前观测 + 上一个 action,不含任何历史观测**。
- 不从轨迹学习(无状态逐步 prompt + 固定手写 few-shot)。
- Takeaway:标准 baseline 根本不堆观测,唯一杠杆是 per-obs 前缀截断 + 可选可见性剪枝。

**2. AgentOccam** — Yang et al., arXiv:2410.13825, ICLR'25
- AXTree 剪枝规则:(i) static-text 节点并入同标签 interactive 节点;(ii) 表/列表→Markdown;
  (iii) 删布局样板;(iv) max_browser_rows=500 + 整页;删 scroll 动作改整页载入。
  均值 ~2930 token/step(范围 Shopping 1634→Reddit 3698)。SR 43.1% vs baseline 16.5%。
- 不跨 episode 学习;轨迹内用 planning-tree branch/prune 把旧子计划的观测 dismiss,
  只 replay pivotal 节点邻域。
- Takeaway:存前剪枝(static→interactive 合并、表→Markdown、行数封顶)最可移植。

**3. SteP** — Sodhi et al., arXiv:2310.03720, COLM'24
- WebArena 原生 AXTree;行数封顶 500(代码默认 300);current_viewport_only=True,
  viewport 1920×1080;per-policy 历史只有 action、零历史观测。~4k token/policy,
  ~22.7k token/轨迹(flat agent 52k)。不从轨迹学习。

**4. WebPilot(MCTS)** — Zhang et al., arXiv:2408.15978, AAAI'25(无官方 repo 可定位)
- actree 不压缩、无 token cap,靠历史设计控量:Explorer 只收当前观测,历史只有 action。
- 任务内 RENE 把搜索历史蒸馏为短 NL reflection;兄弟分支概括成 NL 非拼接原文。
- Takeaway:压缩"广度"的模板——其他分支概括成短 NL 反思,不堆观测。

**5. Agent-E(DOM 蒸馏)** — arXiv:2407.13032(注意:评测在 WebVoyager,非 WebArena)
- DOM 蒸馏 3 模式:text_only / input_fields(剪到 interactive-only) / all_fields;元素注入
  mmid 序号。蒸馏前量级:YouTube 首页 DOM ≈ 80 万 token(蒸馏后比值论文未报)。
- change-observation:MutationObserver 只采集新增节点+变更文本的增量,替代整页重发。
- Takeaway:(1) 反思前把 AXTree 剪到 interactive-only;(2) 存步间 diff 而非整份快照。

**6. BrowserGym / WorkArena(field 标准旋钮)** — arXiv:2403.07718 / 2412.05467
- 标准 agent FLAGS_GPT_4o(源码亲验):use_ax_tree=True、use_html=False("太大")、
  use_screenshot=False、use_som=False、use_action_history=True、use_think_history=False
  → 标准模态 = AXTree-only 文本 + action 历史。
- Prompt cap max_prompt_tokens=40_000,fit_tokens() 递归收缩:AXTree 从第 10 轮起从底部截、
  每轮删剩余 30%、最多 20 轮。GenericAgent context 里永远只有当前一份观测。

**2025–26 观测压缩新作**
- **LineRetriever** — arXiv:2507.00210:LM 给 AXTree 每行打"与未来动作相关性"分取 top 行
  (planning-aware 非 embedding 相似度)。降幅 61% WorkArena-L1 / 72% WebLinx /
  73% WebArena(数字为子代理全文/snippet 源)。
- **FocusAgent** — arXiv:2510.03204:轻量 LLM retriever 抽最相关行,观测缩 >50%,
  顺带降低 prompt-injection 成功率(abstract 亲验)。
- **Read More, Think More** — arXiv:2604.01535:反调——最优观测表示取决于模型能力+thinking
  预算:弱模型紧凑 AXTree、强模型原始 HTML;加观测历史普遍有益,diff-based 表示省 token。
- **SDO** — arXiv:2606.06708(position):观测频率≠动作频率;信号触发(URL 变/新元素/动作失败)
  才重读全 DOM,只回相关元素+selector。无实验。
- **MFS** — arXiv:2605.29397:Minimal Failure Set coverage 作剪枝器的廉价离线代理指标
  (>100× 省评测算力;snippet 源)。

---

## 第二部分 —— (B) learning-time 轨迹表示(核心)

四大必查工作没有一个把 raw AXTree 喂进归纳 LLM。主导压缩:(i) thought/短 NL state 替代观测;
(ii) LM 每步概括成一句;(iii) 存蒸馏 item 而非轨迹;(iv) 切短子轨迹+一句合成指令。

**1. AWM(Agent Workflow Memory)** — Wang/Neubig, arXiv:2409.07429(全文精读)
- 归纳 prompt 喂"自然语言指令 + 一串 action",不是 raw obs(Appendix A.1 逐字)。
  Workflow 每步 = 短 NL state("Order {id} is shown")+ reasoning + program action,
  字面量抽象成 {变量},示例里零 AXTree。
- **Offline 批处理:把一个网站的全部训练轨迹拼进单个 prompt 做一次多轨迹归纳**;
  Online 变体(WebArena)每完成一个测试任务就 induce→integrate→utilize(自判对的轨迹)。
  WebArena 相对 SR +51.1%。
- **Takeaway:这是"minibatch 多轨迹归纳"最贴近的原型**——指令+抽象 action 序列、
  字面量→变量、整批一个 prompt。

**2. ASI(Agent Skill Induction)** — Wang/Neubig, arXiv:2504.06821(全文精读)
- 先过滤:LLM evaluator 判对的 episode 才归纳,一次一条。episode 清洗 = 删执行错误步 +
  每段冗长 thought 用 LM 概括成一句 → **每步 thought 87.9→13.4 token**。
- 归纳出 Python skill(2–5 步、≤10 行、docstring);**重执行验证**:轨迹改写成调用新 skill、
  截掉最后一次 skill 调用后的原语动作、重跑任务仍过才入库。
  skill 由 7.6% 任务产生、42.5% 复用、SR +23.5%、步数 −10.7~15.3%。
- Takeaway:删错误步 + 每步概括到 ~1 句 + 重执行 gate 编辑(对应我们的 merger/gate)。

**3. ReasoningBank** — Ouyang et al.(Google), arXiv:2509.25140(全文精读+HTML 核)
- **脚注 1(关键)**:"We use the thinking process of π_L as the approximation of o_0:t due
  to lengthy observation representations"——连 acting 历史都用 thinking 换掉 AXTree。
- memory-item schema = title/description/content(存 item 不存轨迹);成功与失败都学
  (LLM-as-a-Judge 自判,无 GT);检索 top-k 默认 k=1。
- MaTTS:parallel = 同一 query 生成 k 条轨迹自对比筛可靠 memory;sequential = 迭代自精化。
  WebArena-Shopping(Gemini-2.5):parallel 49.7→55.1(k=1→5)、sequential 49.7→54.5。
- Takeaway:轨迹蒸成 titled item、连失败一起学;**MaTTS parallel self-contrast 是全场
  最接近我们 contrastive minibatch 反思的机制**。

**4. Learn-by-Interact(backward construction)** — Su et al., arXiv:2501.10893(全文精读)
- 滚出完整轨迹 T,对每段连续子轨迹 T[i:j] 让 LLM 写一条概括该片段的新指令 I';
  存(短指令, 子轨迹)。粒度消融:short(<5 步)最优,short+med+long 混合总体最佳
  (WebArena Claude-3.5:35.8→39.4→42.0)。检索 = model-based + observation-based。
- Takeaway:30 步轨迹切成 <5 步子片段、各概括一句、指令+观测双索引。

**2025–26 memory/context 新作**
- **HMT** — arXiv:2603.07024(WebArena+Mind2Web):raw 轨迹自动抽象成 3 层
  (Intent→Stage→Action),Stage 带可观测 pre/post-condition。context −72.7%、
  成本 −71%/任务(数字 snippet 源未逐字核)。想要结构化压缩,这是最具体 schema。
- **M²** — arXiv:2603.00503:Dynamic Trajectory Summarization(历史压成 state updates)
  + insight 检索;token −58.7% / SR +19.6%(abstract 亲验;WebVoyager 系非 WebArena)。
- **AgentFold** — arXiv:2510.24699(阿里):每步执行学到的 folding(granular condensation
  或 deep consolidation);30B-A3B 达 36.2% BrowseComp。关键洞见:~20% 长任务被强制终止时
  context 才 ~7K token → **失败是分心不是长度**——语义压缩可能提准,不只是塞得下。
- **ACON** — arXiv:2510.00615(Microsoft,代码开源):压缩准则在 NL 空间靠 failure analysis
  迭代优化(压缩→失败→分析→改压缩 prompt);peak token −26~54%,小模型 SR +46%
  (AppWorld/OfficeBench)。**failure-driven 学"什么能安全丢"**。
- **ReAP** — arXiv:2506.02158:存成功+失败 self-reflection,只留 key insight(未逐字核)。
- **OCR-Memory** — arXiv:2604.26622:历史轨迹渲染成图、locate-and-transcribe 检索(未逐字核)。
- **SkillWeaver** — arXiv:2504.07079:自主发现→练习→蒸馏 API 库;+31.8% WebArena(未逐字核)。
- **WebXSkill** — arXiv:2604.13318:可执行 skill + 步级 NL guidance;+9.8 WebArena(未逐字核)。
- **MemSkill** — arXiv:2602.02474:memory 操作(extract/consolidate/prune)本身做成可学习
  skill(未逐字核)。
- **Synapse** — arXiv:2306.07863(ICLR'24 奠基):state abstraction + trajectory-as-exemplar。

---

## 第三部分 —— WebArena-Verified + 综述

- **WebArena-Verified** — ServiceNow, NeurIPS'25:重审全部 812 任务、确定性 evaluator、
  258 任务 Hard 子集、离线 network-trace replay 评分。**不规定观测格式**——观测策略是
  自由设计变量,只有 scorer 固定。
- 综述:Context Engineering(arXiv:2507.13334)、Agent-Native Memory System
  (arXiv:2606.24775,"无架构通吃、局部维护比全局重排更省")、Agent Skills for LLMs
  (arXiv:2602.12430)。(三条 ID 未逐字核)

---

## 第四部分 —— 综合:主导模式 & 我们最像哪个

**(A) acting-time 四大主导模式**
1. AXTree-only 文本(丢 HTML+截图)——field 标准。
2. 每观测硬 token 预算 + 截断——WebArena 1920 前缀 / BrowserGym 40k 底部 / SteP 500 行。
3. **只放当前观测 + 历史存 action 不存 observation——全场铁律**。
4. LLM 相关性剪枝(2025–26 浪潮)——LineRetriever/FocusAgent/AgentOccam。

**(B) learning-time 四大主导模式**
1. 绝不喂 AXTree;用 thought 或 ≤1 句 NL state 替代观测。
2. 存蒸馏 item/workflow/strategy 而非轨迹。
3. 层级/多尺度抽象——HMT/AgentFold/M²。
4. 先过滤到正确 + 批处理 + 连失败一起学 + 重执行 gate 编辑。

**我们的场景(minibatch 多轨迹反思做 rule 编辑)最像 AWM offline induction 叠加
thought-as-observation-proxy**。落地配方:
- 入库时(每条轨迹):ASI 式每步一句概括 或 ReasoningBank 式 thinking 替代观测;
  删执行错误步;字面量→{变量};可选 diff 编码把 30 步压成 obs_0 + 29 个小 diff。
- 反思时(多轨迹):压缩后轨迹拼一个 minibatch prompt(AWM);产出 titled item;
  成功与失败都进料。
- 编辑 gate:ASI 式重执行验证(对应我们现有 merger/gate/significance)。
- 多轨迹对比信号:ReasoningBank-MaTTS parallel self-contrast(同任务 k 条 rollout 互对比)
  = 我们 contrastive 组的直接对应物。
- **警示**(Read More Think More + AgentFold):别过度压缩——强模型受益于更丰富观测;
  长任务失败常源于分心而非长度,语义压缩的收益可能是提准不只是塞得下。

**给 350k 字符/轨迹的最大单点收益**:入库端 [interactive-only 剪枝 或 每步一句概括] +
[diff 编码],反思层再叠 [NL 概括/titled item]——文献一致裁决:
**raw AXTree 从来不该活过产生它的那一步**。

---

## 第五部分 —— 置信度 / 存疑(引用前必读)

- **亲自一手源核实**:WebArena max_obs_length=1920 及各默认值(run.py);HMT/M²/AgentFold/
  LineRetriever/ACON/FocusAgent/Read More Think More/SDO 的 arXiv ID+标题+abstract。
- **子代理一手源(全文/repo 代码)核实**:AgentOccam 剪枝规则+Table 4;SteP 参数;
  WebPilot 机制;Agent-E 3 模式+MutationObserver(评测在 WebVoyager);BrowserGym
  FLAGS_GPT_4o+40k 收缩;AWM/ASI/ReasoningBank/Learn-by-Interact 的 (B) 机制与逐字引文。
- **仅 snippet 源(勿当定论)**:HMT −72.7%/−71%;LineRetriever 61/72/73%;ReAP +11/+29;
  SkillWeaver/WebXSkill/MemSkill/OCR-Memory/MFS 具体数字;三篇综述 ID;
  WebArena-Verified README 细节;AgentOccam −14.4%/−45% 为对 Table 4 的算术。
