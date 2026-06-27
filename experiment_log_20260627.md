# CSS 实验运行日志 — 20260627

> **当前活跃运行**: `runs/spreadsheetbench_20260627_183630`(PID 3357602)—— 带齐全部修复的全新跑。
> 见文末「## 第二次全新重启」。下面先记录第一次运行(162001)及其暴露的问题。

运行 ID: `runs/spreadsheetbench_20260627_162001`(服务器 10.77.110.127,PID 2708255)
配置: target/optimizer=qwen3.6-35b-a3b,**max_api_workers=256**,max_turns=100,reflect_mode=plan_a,k_rollouts=3
数据: Train=140 / Val=60 / Test=200(Trace2Skill 对齐)
代码: L1 机制 v2(本次已修复 review 发现的崩溃类 bug)+ FD 上限修复

---

## 阶段 0:Cold Start Bare Rollout(140 任务 × 3 = 420 rollouts)

**状态: ✅ 完成,420/420 全部正确产出**

| 项 | 结果 |
|---|---|
| rollout 目录 | 420/420 全部创建 |
| 有效 conversation.json | 420/420 |
| EMFILE 错误 | 0 |
| no-test-cases / 目录创建失败 | 0 |
| 耗时 | ~30 分钟(16:20:01 → 16:50:15) |

### 🔧 已修复问题 #1:256 并发 FD 上限耗尽(根因 + 解决)

**症状**(用户先前多次观察到):256 并发跑 full train rollout 时,部分任务"不产生 task dir"。本次日志确证错误:
`RuntimeError: OpenAI-compat API request failed: <urlopen error [Errno 24] Too many open files>`

**根因**:服务器 `ulimit -n` 软上限仅 **1024**(硬上限 1048576)。256 并发时每个 ReAct agent 峰值持有多个 FD(LLM socket + xlsx 句柄 + bash 子进程 3 管道 + 输出文件),撑爆 1024 → `urlopen`/`glob`/`makedirs`/`shutil.copy` 抛 EMFILE。若失败发生在 `task_interface.py` 建 prediction_dir 之前,该任务永不产生目录。client 用 `with urlopen() as resp` **无 socket 泄漏**,纯上限问题。

**修复**(两层):
1. 根因:`css/rollout/batch.py:_ensure_fd_limit()` —— batch_rollout 启动时把软上限抬到硬上限(幂等/POSIX-only/失败降级)。库层修复,所有 rollout 入口受益。
2. 防御:`task_interface.py` 把 `os.makedirs(prediction_dir)` 提前到 `_find_test_cases` 之前,确保任何早期失败也留可审计目录。

**验证**:`/proc/PID/limits` Max open files = 1048576;全程 EMFILE=0;420/420 目录全部创建。

### ⚠️ 观察到的问题 #2:长尾任务阻塞整批(效率,未修)

420 个 rollout 的完成呈长尾:~94% 在 17 分钟内完成,但最后 1 个任务(`262-17/r1`,涉及 `.xlsm` 宏格式)艰难硬扛了 **~25 分钟**才完成,期间 255/256 个 worker 空转等它。

- 速率曲线:爬坡 ~18/min → 峰值 ~27/min → 长尾骤降 ~4/min
- 根因:bare LLM(无 skill)在极难任务上反复换文件格式尝试,跑向 max_turns=100(每轮 LLM ~8-31s)
- 影响:批次并发的固有低效,会在每个节点的 train rollout 重复出现
- 待定决策:是否调低 max_turns(100 对 bare LLM 偏高)或 task_timeout。**用户指示:接受长尾,耐心等待,暂不改。**

---

## 阶段 1:Cold Start Analysis → strategy_0

**状态: ✅ 完成,~11 分钟(16:50 → 17:01)**

分析阶段细分(可用 fds 并发度区分):
- **Layer 1 标注**:高并发(fds≈260)~5min,优化器 LLM 标注 420 轨迹的认知观察
- **Layer 2 标签聚类 + cluster_refine**:串行(fds≈5)~5min,`label_grouping.json`(35KB),iterative cluster 精炼(每次 LLM ~1.4s)
- **根因归因 + strategy_0 派生**:~1min

### ✅ 关键验证:strategy_0 质量高且格式正确

派生的 `strategy_0` = **"Evaluation-First Spreadsheet Verification"**:
- **格式正确**:严格两段式(`## 名称` + 概述段 + `### 详述` + `####` 子节)—— 验证 v2 策略格式改造正确生效
- **诊断精准**:命中 SpreadsheetBench 最经典失败模式 —— agent 把公式字符串(`=SUM(A1:B1)`)当完成,而非写计算值(`5`)。核心认知边界:Formula Definition(语法)vs Formula Evaluation(计算值),"Evaluate-Before-Commit" 协议
- 内容含:openpyxl headless 无自动计算的处理、列映射验证、多层完整性检查、数据转换前诊断分析等

这是对整条 cold start 流水线(bare rollout → Layer1-3 分析 → 根因 → 派生)端到端正确性的有力验证。

---

## 阶段 2:Round 0 — ROOT 节点 L0 Exploitation

**状态: 进行中**(17:01 启动,fds≈127)

ROOT 节点开始 L0 战术优化:reflect(plan_a 三路分析→编辑生成)→ apply edits → evaluate_candidate(val 集 rollout)→ accept/reject gate,循环到饱和(N=5 连续 reject 或 max_l0_steps=20)。

⏳ **重点观察点**:本节点 L0 饱和后将首次触发**新重写的 L1 假设-测试-验证循环**(本次 review 重点修复区)。这是 v2 L1 机制的首次实跑,需密切观察是否有未捕获的运行时问题。

### 🔧 已修复问题 #3:策略产出中英文混杂(prompt 硬编码中文)

**症状**(用户发现):cold start 派生的 strategy_0 出现中英文混杂——section 标题是中文 `### 详述`,正文英文。

**根因**:两段式策略格式模板里硬编码了中文 token `### 详述`(源自设计文档)。全仓库扫描确认只有这一个中文 token,分布在 8 行 / 3 个 prompt 文件:`proposal.py` _STEP2_SYSTEM、`derivation.py` _DERIVE_SYSTEM、`coldstart.py` 回退策略。LLM 严格按模板产出,故每个派生/回退策略都带中文标题。

**修复**:`详述` → `Details`(8 处);测试里的 Unicode 往返用例改用非中文字符串。全仓库 `grep CJK` 现为 0。

**用户规范**(已记 memory [[feedback-english-only-code]]):所有代码+prompt 必须全英文,禁中英混杂。

**影响范围**:当前运行(`162001`)的代码已加载旧 prompt,strategy_0 及后续 L1 派生策略仍会带 `### 详述`。需重启才能产出纯英文策略。

**决策(用户)**:**让当前运行继续**(不重启)——这次重点观察 L1 循环机制是否正常,不在意本次产出的语言;代码修复留待下次运行生效。

### 🔴 已修复问题 #4(最严重):服务器 edit.py 陈旧 → L0 完全失效

**症状**(用户发现):L0 step 0 的 reflect=3 raw 但 **merge=0**,即三个 edit generator 共产出 **0 个 edit**。

**逐层排查**:
1. 看 trace:generator response 有 3871-8203 字符(模型明明产出了内容),却解析成 0 edit。
2. 复现 generator 调用:qwen 正确产出了 `{"edits":[{"op":"add_section",...}]}` 的高质量 edit。
3. 服务器直测 `_edit_from_dict(add_section)` → **返回 None(丢弃)**;`grep EDIT_OPS` 发现服务器 `css/data/edit.py` 是 **2 天前的陈旧版**,`EDIT_OPS` 只有旧的 4 个 op,**缺 add_section/rewrite_section/delete_section**。
4. 根因:`reflect.py`(新,prompt 让模型优先用 section op)与 `edit.py`(旧,EDIT_OPS 缺 section op)**不匹配** → `_edit_from_dict` 把每个 section edit 静默丢弃 → **每个 L0 step 产出 0 edit,rules.md 永远空,L0 战术优化完全失效**。

**为什么没同步上去**:rsync 默认"大小+mtime"快速判断**静默跳过了内容已变的 edit.py**(本地 mtime 与服务器巧合接近)。这是基础设施隐患——见 memory [[server-experiment-setup]]。

**修复**:scp 强制覆盖 edit.py + 之后全部用 `rsync -c`(checksum)同步。这次 L0 step 0 的 noise-accept(0.519→0.533)也印证:0 edit 时 candidate=空 rules,分数变化纯属 temperature=0.7 噪声。

---

## 第二次全新重启 — `runs/spreadsheetbench_20260627_183630`

**重启时间**:2026-06-27 18:36(PID 3357602)。停掉 162001,用最新代码从头跑。

**本次带齐的修复 + 新功能**:
1. **L0 修复**(edit.py 7-op)—— section edit 不再被吃掉,L0 真正能优化 rules
2. **英文策略**(`详述→Details`)—— 无中英混杂
3. **FD 修复**(`_ensure_fd_limit`)—— 256 并发无 EMFILE(已验证 soft_nofile=1048576)
4. **🆕 阶段级 checkpoint/resume**(`css/checkpoint.py`):cold start 后 + 每轮后写 checkpoint(tree+archive+baseline+round_index+rng+config 指纹);`run_experiment_server.py --resume [out_dir]` 从最新 checkpoint 续跑,config 指纹不匹配则拒绝。**170 测试通过(含 2 个新 resume 集成测试)**
5. **🆕 rollout 磁盘缓存**(`task_interface.load_cached_result` + `batch._load_cached`):每个 rollout 写 `result.json`(含 skill_hash);resume 时同 skill 的已完成 rollout 直接读盘跳过。是阶段内中断的细粒度恢复。
6. **审计**:`llm_calls.jsonl` 早已记录每次调用的完整 system/user/response/usage(本会话确认已实现)。

**启动健康验证**:soft_nofile=1048576、open_fds=1029、EMFILE=0、Tracebacks=0,cold start bare rollout 正常 ramp up。

**⚠️ 一个 3.8 兼容教训**:`run_experiment_server.py` 缺 `from __future__ import annotations`,`str | None` 注解在服务器 Python 3.8 运行时求值报错。已加。本地 Python 3.11 测不出——**服务器是 3.8,新写脚本必须带 future import 且避免运行时 `X|Y` 联合**。

(持续记录中……)
