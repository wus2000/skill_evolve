# Agent Skills 自进化相关工作的 Benchmark 调研（2026-07-03）

## 一、五条相关工作线及其常用 benchmark

### 1. Skill/Playbook 文档进化线（与 CSS 最直接同类）
| 工作 | Benchmark | 备注 |
|---|---|---|
| ACE (Agentic Context Engineering, 2510.04618) | **AppWorld** + FiNER/Formula(金融XBRL) | playbook 进化，AppWorld leaderboard 上以小模型追平生产级 agent；对比基线含 ICL/MIPROv2/GEPA/Dynamic Cheatsheet |
| Dynamic Cheatsheet | AIME、GPQA、Game of 24 等推理任务 | test-time 自维护备忘单 |
| AWM (Agent Workflow Memory) | **WebArena、Mind2Web** | 从轨迹归纳 workflow 注入 |
| Voyager | Minecraft (MineDojo) | 代码技能库开山作 |
| SkillWeaver | WebArena | 网页 API 技能合成 |
| ExpeL | **ALFWorld、WebShop、HotpotQA**、FEVER | 经验规则抽取，经典配置 |
| Reflexion | ALFWorld、HotpotQA、HumanEval | 反思记忆 |
| Socratic-SWE (2606.07412) | SWE-bench Verified/Lite/Pro、Terminal-Bench 2.0 | 轨迹蒸馏成 agent skills |
| MMG2Skill (2606.01993) | 自建 MMG2Skill-Bench（GUI 控制+开放游戏+卡牌） | 网络攻略→可执行 skill |
| Parthenon (2606.04602) | Harvey LAB（法律，12,510 轨迹） | skills/tools/knowledge 三面进化 |
| WorldEvolver (2606.30639) | **ALFWorld、ScienceWorld**（AgentBoard 协议） | 世界模型上下文进化 |
| EmbodiSkill / SkillMAS / MUSE-Autoskill / SkillRL（2026 一批） | 具身/多智能体/RL 场景各异 | skill 进化已成体裁 |

### 2. 经验记忆线（memory-augmented，与 skill 线互通）
| 工作 | Benchmark |
|---|---|
| Agent-KB | **GAIA**、SWE-bench |
| Memento / AgentFly | GAIA、DeepResearcher 系、SimpleQA |
| Alita-G (2510.23601) | GAIA (83% pass@1 SOTA)、PathVQA、HLE |
| Evo-Memory (2511.20857, 基准套件) | 流式化 10 数据集：MMLU-Pro、GPQA-Diamond、AIME-24/25、**ToolBench、ALFWorld、BabyAI、ScienceWorld、PDDL**、LangChain-Memory；统一评测 Dynamic Cheatsheet/AWM/Mem0/MemOS 等 13 法 |
| EXG 经验图 (2605.17721) | 代码生成 + 推理基准 |

### 3. Prompt/程序优化线（optimizer 同型，粒度不同）
| 工作 | Benchmark |
|---|---|
| GEPA | HotpotQA、HoVer、IFBench、PUPA |
| DSPy/MIPROv2 | HotpotQA 多跳系 |
| TextGrad | Math/代码/分子等 |
| Trace/OptoPrime | AIME、BigBench |
| POLCA | **τ-bench**、HotpotQA |

### 4. Scaffold/代码自进化线
Live-SWE-agent（SWE-bench Verified 77.4%/Pro 45.8%）、DGM、ADAS/InfiAgent、SEVerA（**τ²-bench** 政策遵循+形式验证）、AgentDevel（execution-heavy 基准，flip-centered gating——与我们 V3.3 净翻转 gate 撞思路，值得引用）。

### 5. 本仓库基线库实际使用
Arbor/MLEvolve→**MLE-bench/Kaggle+Math**；POLCA→**τ-bench+HotpotQA**；SkillGrad→Spreadsheet；SkillOpt→**ALFWorld+Math+Spreadsheet**；Trace→AIME+BigBench；Trace2Skill→Math+Spreadsheet。

**高频通货统计**：ALFWorld（≥6 个系统）、GAIA（memory 系标配）、AppWorld（playbook 系新标配）、SWE-bench（SWE 系标配）、HotpotQA（prompt 优化系）、WebArena/Mind2Web（web 系）、τ-bench（政策遵循）、AIME/GPQA（推理系）、Spreadsheet/MLE-bench（本仓库基线圈）。

## 二、2026 "Agent Skills" 原生 benchmark 生态（Claude Skills 之后爆发）

综述《Agent Skill Evaluation and Evolution》(2606.11435) 划了六类 skill-centric 基准：
1. **Skill Utility**：**SkillsBench** (2602.12670，Stanford/CMU/Berkeley，87 任务 × 8 域 × 确定性验证器 × 18 model-harness；curated skills 平均 +16.6pp，33.9%→50.5%；发现"≤3 模块的聚焦 skill 优于大而全")、SkillCraft
2. Skill Generation：SkillLearnBench、SkillGenBench (2605.18693)
3. Retrieval & Routing：SkillRouter、SRA-Bench、SkillResolve-Bench
4. Safety：SkillTester、SkillGuardBench、SKILL-INJECT、SkillSafetyBench
5. SWE：SWE-Skills-Bench
6. Real-world：WildClawBench、SkillForge

四类 skill 进化范式：execution feedback / **trajectory distillation（Trace2Skill 被归此类）** / compression / RL。

**对 CSS 的直接机会**：SkillsBench 是"人写 skill vs 无 skill"的配对评测——把 CSS **学出的** skill 放上去与 curated skill 对比，是现成的外部泛化实验（test-only，不用改训练循环），能回答"搜索出的 skill 能否达到人类专家 skill 水平"。

## 三、适配 CSS 机制的候选任务分析

判据（源自 Bird 双臂审计教训）：
- **A. 任务池规模**：可切 train/val/test（≥数百，最好上千）
- **B. 自动确定性评分**（execution/unit-test based；GT firewall 可实施）
- **C. 本地可部署 + 高并发 rollout**（320 线程；排除真实网络/VM/外部 API 依赖）
- **D. 单任务成本**（回合数×token）
- **E. 知识可修复面**★（Bird 教训：~90% 失败是能力/随机性不可修；要选"环境规约/流程/政策知识密集"而非"纯推理能力"型）
- **F. 社区可比性**

| 候选 | 规模 | 评分 | 部署 | 成本 | 知识面 | 可比性 | 判定 |
|---|---|---|---|---|---|---|---|
| **AppWorld** | 750 任务（train105/dev60/testN168/testC417）；9 app/457 API | 状态级 unit tests（含附带损害检查）| 纯 Python 本地 | 中 | ★★★（API 语义+跨 app 工作流） | ACE 直接对标+leaderboard | **首选** |
| **ALFWorld** | 3,553 train + 134 unseen | 目标状态检查 | 本地文本env，秒级 | 低 | ★★（常识流程） | ExpeL/Reflexion/SkillOpt/Evo-Memory 通货 | **强推** |
| **WebShop** | 12,087 指令/1.18M 商品 | reward 自动 | 本地部署 | 低 | ★★（购物规约） | ExpeL 系 | 推荐 |
| **τ²-bench** | 375 任务（airline50/retail114/telecom114/banking97）| DB 状态比对 | 本地+需 user-sim LLM | 中高 | ★★★（**政策文档遵循=skill doc 直接对应物**） | POLCA/SEVerA | 推荐（池小，做展示域） |
| **ScienceWorld** | 30 类×千级变体 | 目标检查 | 本地文本env | 低中 | ★★★（实验流程知识） | Evo-Memory/WorldEvolver | 推荐 |
| **TravelPlanner** | 1,225 | 约束满足自动验证 | 本地（沙盒工具） | 中 | ★★★（规划约束） | 中 | 备选 |
| InterCode-SQL/Bash | ~千级 | 执行比对 | docker 本地 | 低 | ★★ | 中 | 备选（与 Bird 部分重叠） |
| FiNER/Formula | 大池 | 精确匹配 | 本地 | 低 | ★★★（金融标注规约） | ACE | 备选（非 agentic 交互） |
| Spider 2.0 | 632 | 执行比对 | 本地/云DB | 高 | ★★★ | Text-to-SQL 进阶 | Bird 的续作候选 |
| SWE-bench Verified | 500 | unit tests | docker/任务，重 | 高 | ★★（能力占比高） | SWE 线 | 观望 |
| GAIA | 466 | 精确匹配 | **需真实网络+搜索** | 高 | ★★ | memory 线通货 | 不适合封闭训练循环 |
| WebArena | 812 | 程序检查 | docker 网站群，重 | 高 | ★★ | AWM/SkillWeaver | 不推荐（并发难） |
| OSWorld / WorkArena++ | 369/682 | 脚本检查 | VM/ServiceNow 实例 | 很高 | ★★ | GUI 线 | 不推荐 |
| MLE-bench | 75 竞赛 | Kaggle 指标 | GPU 训练，极重 | 极高 | ★★ | Arbor/MLEvolve | 不推荐（预算） |
| AIME/GPQA/MMLU-Pro | 数百 | 精确匹配 | 本地 | 低 | ★（纯能力） | Dynamic Cheatsheet | 不适合（知识可修复面小） |

## 四、落地建议

1. **AppWorld = 第三个环境的最优解**：知识可修复面大（API 语义、鉴权流程、跨 app 编排——正是文本规则能救的失败）、unit-test 评分严格、officially split、纯本地、ACE/GEPA/DC 全在此对标。唯一注意：train 只有 105 任务（×3 变体），train/val 池比 Bird 小得多——恰好逼出小样本模式，且与 SkillsBench 的"≤3 模块聚焦 skill"发现呼应。
2. **ALFWorld = 最便宜的社区通货**：加入后 ExpeL/Reflexion/AWM/Evo-Memory 的数字可直接引用对比；qwen3.6-35b 上预计 70-85% 起点，留有可修复空间；SkillOpt 基线也用它（同仓库可复用）。
3. **τ²-bench 作"政策遵循"展示域**：skill doc 进化在这里的语义最漂亮（学出的 rules ≈ 优化过的 policy playbook），但池子小、user-sim 引入额外噪声——适合作 case study 而非主战场。
4. **SkillsBench 作外部评测**（不进训练循环）：CSS 产出 skill vs 人类 curated skill 的配对对比，一条评审很买账的实验线。
5. 维持 BIRD + SpreadsheetBench 为主战场（社区里 spreadsheet 恰是 SkillGrad/SkillOpt/Trace2Skill 小圈子的共同语言，Text-to-SQL 是执行验证的黄金域）。

## Sources
- ACE: https://arxiv.org/abs/2510.04618
- SkillsBench: https://arxiv.org/abs/2602.12670 / https://www.skillsbench.ai/skillsbench.pdf
- Agent Skill Evaluation and Evolution survey: https://arxiv.org/html/2606.11435v1 / https://github.com/Cassie07/AgentSkill_Survey
- Evo-Memory: https://arxiv.org/html/2511.20857v2
- Self-Evolving AI Agents survey: https://arxiv.org/abs/2508.07407
- AppWorld: https://arxiv.org/abs/2407.18901 / https://appworld.dev/
- τ²-bench: https://github.com/sierra-research/tau2-bench / https://evalscope.readthedocs.io/en/latest/benchmarks/tau2_bench.html
- Alita-G: https://arxiv.org/abs/2510.23601 ；Socratic-SWE: https://arxiv.org/abs/2606.07412 ；Live-SWE-agent: https://arxiv.org/abs/2511.13646 ；WorldEvolver: https://arxiv.org/abs/2606.30639 ；MMG2Skill: https://arxiv.org/abs/2606.01993 ；SEVerA: https://arxiv.org/abs/2603.25111 ；AgentDevel: https://arxiv.org/abs/2601.04620 ；SkillGenBench: https://arxiv.org/pdf/2605.18693
