# P10 runtime 结构深度审核

日期：2026-06-30

本文审核当前实现是否存在“结构过于冗杂、靠互相打补丁推进、后续诊断和升级低效”的问题。审核对象不是目标设计文档，而是当前实际代码、当前评估产物和 UI 调用方式。

结论先说清楚：当前复杂度有大量业务必要性，不能简单删减；但当前实现确实已经形成“领域结构、候选枚举、阶段门、补救策略、评分选择、状态推进、诊断产物”混在同一个 runtime 主循环里的形态。它能跑出阶段性成果，但逻辑层次不干净，导致后续升级越来越依赖新增候选、拒绝原因和排序项来修局部问题。现在最重要的工作不是压缩代码行数，而是把这些已经有效的经验沉淀成稳定边界。

## 1. 审核范围

主要文件：

| 文件 | 当前角色 | 规模 |
|---|---|---:|
| `scripts/generate_physical_runtime_trace.py` | P10 物理 runtime、候选生成、求解循环、物理校验、结构门控、诊断输出 | 12618 行 |
| `app.py` | Streamlit 演示 UI，加载 runtime，运行单案，展示回放、接口响应、诊断和评估 dashboard | 1858 行 |
| `scripts/validate_phase_gates.py` | 人工阶段解析、H1-H5/P0-P9 审计基线、若干公共解析工具 | 2540 行 |
| `scripts/generate_p0_p4_trace.py` 到 `generate_p9_state_update_trace.py` | 离线结构 trace 生成链路 | 150-400 行级别 |
| `docs/flow_edge_structure_model_design.md` | 目标结构设计和验收口径 | 5772 行 |
| `docs/P10_人工差距结构诊断.md` | 当前人工差距和下一步结构工作看板 | 约 260 行 |

重点产物：

```text
artifacts/roundFW_current_structural_full/
```

该产物是当前诊断文档使用的基线。

## 2. 一句话结构判断

当前实现不是“纯粹冗余”，而是“单体 runtime 承载了过多层次”。很多结构是长期优化后留下的有效经验，尤其是物理约束、远端 session、人工阶段、短链、库位、对位窗口、关门车、route clear 等；问题在于这些经验还没有被抽象成稳定的领域对象和门控接口，而是散落在候选生成、排序和拒绝函数中。

因此后续如果继续按当前方式迭代，会出现三个趋势：

1. 新业务问题通常通过新增 planlet、排序项或 `p*_reject_*` 原因修复。
2. 每个修复都可能影响候选池、阶段门、远端 session、状态推进和评估口径多个位置。
3. 诊断产物越来越丰富，但诊断本身也越来越依赖运行结果反推结构，而不是结构天然可解释。

## 3. 当前真实分层

### 3.1 目标分层

从 `flow_edge_structure_model_design.md` 看，目标结构大致是：

```text
输入标准化
  -> FlowClassify / FlowEdge / EdgeContract
  -> HumanPhaseContract / PhaseGate
  -> WorkPatternTemplateSelector
  -> EdgeBoundedCandidateGenerator
  -> ResourceRequest / ResourceDelta / ContractDelta
  -> AcceptRejectGate
  -> ContractOptimizer / LocalTieBreakSearch
  -> State Update / FlowGraph Rebuild
  -> Trace / failure_bucket
```

这个分层是合理的：先把车辆归属和业务合同定清楚，再生成有限候选，再由资源/合同硬门过滤，最后只在同结构合法候选中优化。

### 3.2 当前实际分层

当前 `generate_physical_runtime_trace.py` 的实际主路径更接近：

```text
读取 truth2 / manual baseline
  -> 构造 base/cluster depot assignment
  -> 生成多组 StrategyConfig 组合
  -> 每组策略运行 _run_case_once
      -> 每轮计算 phase_state / remote_session_plan
      -> build_candidates 一次性生成 tail/direct/contract/remote/route_clear/staging 等所有候选
      -> PhysicalValidator 物理校验
      -> phase_reject_reason 阶段过滤
      -> structural_reject_reason 结构过滤
      -> selection_key / remote_session_selection_key 排序选择
      -> apply_candidate 修改车辆状态
      -> 写 phase/contract/resource/dominance/pool/session 诊断行
  -> better_case_result 在策略结果中选最好
  -> 写 case summary、response、CSV 审计产物
```

也就是说，目标设计里的多层结构，在当前实现中主要表现为一个大函数调用链和若干大型函数的局部分支，而不是稳定的模块边界。

## 4. 量化结构画像

### 4.1 runtime 规模

`scripts/generate_physical_runtime_trace.py`：

| 指标 | 数值 |
|---|---:|
| 总行数 | 12618 |
| 顶层函数数 | 234 |
| 类数量 | 37 |
| dataclass 数量 | 35 |
| 顶层赋值/常量块 | 67 |
| 嵌套函数数 | 61 |

最长函数：

| 函数 | 行号 | 行数 | 主要职责 |
|---|---:|---:|---|
| `_run_case_once` | 10176-11182 | 1007 | 单策略完整求解循环、候选评估、选择、状态推进、诊断写入 |
| `build_candidates` | 7147-8082 | 936 | 所有候选族的入口和优先合并 |
| `structural_reject_reason` | 9080-9471 | 392 | P4/P7/P8/R 相关结构拒绝门混合 |
| `run_case_with_diagnostics` | 9844-10173 | 330 | 多策略组合、base/cluster/short-chain 策略选择 |
| `build_structure_work_audit_rows` | 11908-12187 | 280 | R1-R6/15.13.7 结构审计 |
| `short_chain_planlets` | 5969-6242 | 274 | 短链候选 |
| `remote_session_bundle_planlets` | 5606-5849 | 244 | 远端 bundle 候选 |
| `remote_session_contract_planlets` | 5379-5603 | 225 | 远端 session 合同候选 |
| `front_capacity_release_contract_planlet` | 3645-3856 | 212 | 前场容量释放合同候选 |

这些数字不是单独的坏味道。调车领域本身复杂，且当前 runtime 已覆盖多个真实约束。但这些数字说明核心变化点集中在少数大函数中，后续维护成本会随策略叠加非线性上升。

### 4.2 关键函数复杂度

| 函数 | 参数数 | `if` 数 | `for` 数 | 嵌套函数数 | 本地调用数 |
|---|---:|---:|---:|---:|---:|
| `build_candidates` | 15 | 86 | 15 | 6 | 44 |
| `_run_case_once` | 16 | 56 | 13 | 13 | 65 |
| `structural_reject_reason` | 14 | 52 | 4 | 6 | 26 |
| `run_case_with_diagnostics` | 10 | 17 | 4 | 2 | 15 |

`build_candidates` 和 `_run_case_once` 已经不只是“一个函数比较长”，而是承担了多个设计层：

| 函数 | 当前混合职责 |
|---|---|
| `build_candidates` | 目标识别、尾部收束、清障、临停选择、容量释放、合同候选、远端 session 候选、短链候选、route clear 包装、候选去重和截断 |
| `_run_case_once` | phase 状态、远端 session 状态、候选构建、物理校验、阶段门、结构门、lookahead、循环检测、排序、接受、状态变更、诊断行生成、response 写入 |
| `structural_reject_reason` | remote session 合同、leased corridor、front interruption、transition budget、mixed profile、ownerless recovery、aligned repair mode、同合同 dominated 判断 |

这就是后续升级低效的核心位置。

## 5. 当前评估表现

基线：`artifacts/roundFW_current_structural_full`。

### 5.1 全量表现

| 指标 | 当前值 |
|---|---:|
| truth2 案例数 | 113 |
| completed | 102 |
| blocked | 11 |
| final unsatisfied | 23 |
| business Get/Put 勾数 | 4074 |
| remote business transition | 504 |
| remote interaction session | 127 |
| hard physical violation | 0 |
| unknown route | 0 |
| depot slot failure | 0 |
| closed door replay violation | 0 |
| final capacity warning | 2 |

这说明当前 runtime 的物理安全底线已经比较强。不能因为结构混杂就否定现有实现；它确实把大量真实约束落到了可运行系统里。

### 5.2 R1-R6 状态

当前 `structural_repair_acceptance.csv`：

| 项 | 状态 | 当前值摘要 | 说明 |
|---|---|---|---|
| R1 HumanPhaseContract | failed | `phase_mismatch=84; forced_open=185` | 阶段门仍不是硬边界，存在大量强行放行 |
| R2 TargetContractSelector | failed | `zero_or_negative_delta=31; p4_rejects=7` | 合同选择和 delta 仍有“非正收益但被接受/需要拒绝”的问题 |
| R3 ResourceDeltaRejectGate | failed | `dominated=111; dominance_rate=0.1093; p7_rejects=185` | 同轮候选中仍有较多被更优候选支配的选择 |
| R4 RemoteSessionPlanlet | passed | `remote_transition_plus2_violations=0` | 远端 session 结构已有阶段性成果 |
| R5 ShortChainPlanlet | failed | `short_chain_failed=2` | 短链仍明显高于人工计划 |
| R6 BlockerCapacityFeasibility | failed | `completed=102/113; capacity_infeasible=9; all_runtime_candidates_rejected=1` | 可解性和容量一致性仍未闭环 |

这组指标很关键：当前不是“完全没结构”，而是某些结构已经有效，某些结构仍靠放行和后置诊断维持。

### 5.3 人工远端对比

`manual_vs_solver_case_compare.csv` 中可比 completed 案例 96 个：

| 指标 | 人工 | solver |
|---|---:|---:|
| 远端切换均值 | 3.88 | 4.38 |
| P50 | 4 | 4 |
| P75 | 4 | 5 |
| P90 | 4 | 6 |
| 最大值 | 4 | 6 |

达标情况：

| 口径 | 案例数 |
|---|---:|
| solver <= manual | 65 / 96 |
| solver <= manual + 1 | 70 / 96 |
| solver <= manual + 2 | 96 / 96 |

这说明 R4 的改动不是无意义补丁，它确实把远端切换压到了 `manual + 2` 内。但 P90 仍高于人工，说明 session 仍没有完全块状化。

### 5.4 候选和拒绝分布

`candidate_physical_audit.csv`：

| 状态 | 数量 |
|---|---:|
| accepted | 1016 |
| rejected | 2149 |
| blocked | 5 |

主要拒绝原因：

| 拒绝原因 | 次数 |
|---|---:|
| `get_route_blocked_by_occupied_line` | 1253 |
| `put_route_blocked_by_occupied_line` | 436 |
| `temporary_line_final_target_violation:存4南` | 135 |
| `same_line_reposition_requires_staging_search` | 55 |

这说明候选池中大量候选是在物理路径层被拒绝，而不是进入更清晰的合同/资源层前就被裁剪。候选生成阶段没有足够利用可达性和资源状态做前置约束，导致物理校验承担了候选裁剪器的角色。

### 5.5 已接受结构分布

`contract_trace.csv` 中 accepted structural intent：

| structural intent | 次数 |
|---|---:|
| `MOVE_TO_PLANNED_TARGET` | 208 |
| `FRONT_CAPACITY_DIGEST_PLANLET` | 200 |
| `CORRIDOR_CLOSEOUT_PLANLET` | 151 |
| `REMOTE_SESSION_BUNDLE_CONTRACT` | 104 |
| `SAME_LINE_REORDER_PLANLET` | 58 |
| `DEPOT_DIGEST_PLANLET` | 56 |
| `H1_CARRY_PLANLET` | 38 |
| `ROUTE_CLEAR_SESSION_PLANLET` | 31 |
| `REMOTE_SESSION_CONTRACT` | 30 |
| `REMOTE_EXCHANGE_PLANLET` | 23 |
| `REMOTE_TAIL_CLOSEOUT_CONTRACT` | 19 |

这里能看到当前 runtime 的真实工作方式：不是一个统一搜索器，而是很多有业务含义的 planlet/template 共同填补场景。这个方向本身符合“人工计划结构经验”的需求；问题是这些 planlet 还没有统一模板接口和合同 delta 语义。

## 6. 哪些复杂度是必要的

不能把当前结构简单理解成“冗杂所以删掉”。至少以下复杂度是领域必要复杂度。

### 6.1 物理拓扑和路径约束必要

当前 `TrackGraph`、`TRACK_SPECS`、`SWITCH_EDGES`、`REVERSAL_DISTANCE_M`、运行线不可停、路径避让、倒车长度等都属于硬物理底座。产物中硬物理违规为 0，说明这部分是当前系统的基本安全价值。

不能简化为“只看起终点股道”，否则会重新引入：

- 路径被占用但仍生成动作。
- 联线/渡线/运行线误停车。
- 倒车长度超限。
- 临停线被当终点。
- 机车位置和路径端点不连续。

### 6.2 大库 slot 和对位窗口必要

`DepotAssignment`、`slot_allowed_for_car`、`build_depot_assignment`、`build_short_direct_depot_assignment`、spotting window 相关函数都在处理真实业务差异。

这些不能被简单合并成普通容量约束，因为修库和对位线存在：

- 修1-修4库内不同台位能力。
- 厂/段/临修程限制。
- 强制对位窗口。
- 南端缓冲对窗口的影响。
- 库外/库内容量与顺序问题。

### 6.3 关门车和称重必要

`_validate_closed_door`、`closed_door_replay_violation_reasons`、`has_weigh`、`Weigh` operation 的存在是接口和安全要求的一部分。当前 closed door replay violation 为 0，应保留为硬门。

### 6.4 人工阶段 H1-H5 必要

人工计划不是单纯最短路径。`validate_phase_gates.py` 和文档中 H1-H5 的结构意义是合理的：

- H1 前场组织和功能线服务。
- H2 存4释放口 shaping。
- H3 严格释放/机接。
- H4 大库消化。
- H5 尾项收束。

当前 R1 失败，不说明阶段结构多余；恰恰说明阶段结构还没有作为一等硬边界沉入 runtime。

### 6.5 远端 session 必要

当前 R4 已通过，且所有可比 completed 案例达到 `manual + 2`，说明远端 session 抽象是有效的。`REMOTE_SESSION_BUNDLE_CONTRACT`、`REMOTE_TAIL_CLOSEOUT_CONTRACT`、`DEPOT_DIGEST_PLANLET` 等不是随便堆出来的，它们在压缩远端来回切换方面有明确贡献。

不能直接删除 route clear 或 remote tail closeout。现有诊断已经证明，直接删除 front-only route clear 会导致多个代表案 `all_runtime_candidates_rejected`。

## 7. 可疑冗杂和补丁式信号

### 7.1 策略组合像“外层试错器”

`run_case_with_diagnostics` 会构造多组策略：

- base
- recovery suppressed
- contract
- remote_session_only_contract
- remote_session_front_contract
- remote_session_contract
- remote_session_contract_transition_first
- remote_session_bundle_contract
- remote_session_bundle_contract_transition_first
- cluster 变体
- short direct 变体
- aligned 模式

最后通过 `better_case_result` 选择最好结果。

这说明当前还没有一个稳定的策略选择模型，而是用多策略竞跑弥补结构识别不确定性。短期有效，但长期问题是：

- 运行成本和诊断成本上升。
- 同一个案例为什么选中某策略，需要事后解释。
- 某个策略修复可能被另一个策略覆盖，回归定位困难。
- 策略配置成为隐形业务逻辑，难以写单元测试。

### 7.2 `StrategyConfig` 是功能开关，不是业务模型

`StrategyConfig` 里有：

- `enable_contract_planlets`
- `prefer_contract_planlets`
- `enable_remote_session_contracts`
- `enable_remote_session_bundle_contracts`
- `front_debt_first`
- `enable_front_capacity_contracts`
- `suppress_ownerless_recovery`
- `remote_transition_first`
- `allow_early_remote_entry_prep`

这些开关很多其实对应业务结构是否成立，而不是运行时调参。例如“是否启用远端 session 合同”不应是策略开关，而应由输入状态和合同识别结果自然决定。否则同一个业务结构在不同策略里含义不同。

### 7.3 候选生成器过于集中

`build_candidates` 同时处理：

- tail closeout。
- same-line reorder。
- source blocker relocation。
- target spot release。
- target capacity release。
- direct target move。
- front capacity release/digest。
- remote entry capacity prep。
- remote exchange。
- corridor closeout。
- remote tail closeout。
- remote session bundle。
- depot digest。
- depot slot swap。
- short chain。
- route clear wrapping。

这不是一个“候选生成器”，而是模板选择器、资源释放器、清障器、尾项收束器、短链识别器和 route clear 包装器的混合体。

直接后果：

- 新候选加入时难判断放在哪个段落。
- 候选优先级由列表拼接顺序、`candidate_sort_key`、phase sort、selection key 共同决定。
- route clear 可以在生成阶段被多次包装，容易形成“为了某候选访问而生成辅助候选”的隐式链。
- `MAX_CANDIDATES_PER_ROUND = 128` 在候选生成末尾截断，可能掩盖候选族膨胀。

### 7.4 物理校验承担了过多候选裁剪

拒绝原因中 `get_route_blocked_by_occupied_line` 和 `put_route_blocked_by_occupied_line` 合计 1689 次。说明很多候选在生成时并不知道自己大概率不可达。

理想上：

```text
WorkPatternTemplateSelector 只产生结构上必要的候选族
ResourceRequest 预判关键资源和通路
PhysicalValidator 做最终硬校验
```

当前则更像：

```text
大量候选先生成
PhysicalValidator 再作为第一大过滤器
```

这会导致诊断重心偏向“路径被堵”，而不是“哪个结构应该先释放哪条访问通道”。

### 7.5 `structural_reject_reason` 是多代修复叠加层

`structural_reject_reason` 中混合了：

- remote session contract violation。
- mixed session low debt reduction。
- leased corridor refill。
- remote session front interruption。
- remote transition budget。
- internal remote transition 限制。
- owner-bound restore。
- low yield route clear/session。
- uncompressed remote session。
- ownerless recovery。
- aligned mode 下 zero/negative delta。
- same progress without remote cross。
- dominated same contract。

这些逻辑很多都是必要规则，但现在被放在一个函数里，且通过字符串返回拒绝原因。这会带来两个问题：

1. 规则之间没有显式优先级模型，只靠代码顺序。
2. 拒绝原因既是诊断文本，又被上层当控制语义使用。

当后续新增规则时，很难证明不会改变旧规则的覆盖顺序。

### 7.6 阶段门仍存在“强行放行”

R1 显示：

```text
phase_mismatch=84
forced_open=185
```

这说明阶段门还没有成为真正的结构边界。`allow_phase_forced_open` 当前默认仍允许当没有 phase-permitted candidate 时使用 deferred candidate，并记录 forced reason。

这在研发阶段可以提高可解性，但如果长期保留，会导致：

- H1-H5 的业务证明力下降。
- 后续排序可能选择阶段外候选，再由诊断解释。
- 人工阶段差距被“可解性兜底”稀释。

### 7.7 诊断产物侵入主循环

`_run_case_once` 在接受候选后直接构造：

- `RuntimePhaseTraceRow`
- `ContractTraceRow`
- `ResourceDeltaTraceRow`
- `CandidateDominanceAuditRow`
- `CandidatePoolAuditRow`
- `DepotSessionAuditRow`

这些产物很有价值，但它们的生成逻辑与状态推进纠缠在一起。问题不是“写 CSV 太多”，而是：

- 主循环为了诊断要知道过多字段。
- trace 字段变化会触碰求解核心。
- 诊断模型无法独立复用或单测。

更合理的是主循环产生少量结构化事件，审计层订阅事件生成这些 CSV。

### 7.8 状态仍以可变 dict 为主

车辆状态主要是 `list[dict[str, Any]]`，通过 `apply_candidate` 修改，很多函数依赖字段名字符串。

短期好处是兼容接口 JSON；长期风险是：

- 状态字段语义不集中。
- `_Weighed`、`Line`、`Position`、`TargetLines`、`ForceTargetPosition` 等混用接口字段和 runtime 字段。
- 很难保证状态转移只发生在一个地方。
- 单元测试必须构造完整 dict，而不是领域对象。

这也是“诊断不准确”的来源之一：如果状态语义分散，trace 只能记录结果，很难证明中间不变量。

### 7.9 候选 ID 承载了过多语义

候选 ID 形如：

```text
0103W:P10:5:remote_session_contract_planlet:Get:存1线:...;Put:修1库内:...
```

这个 ID 对人工调试有帮助，但它同时承担：

- 唯一标识。
- 候选族说明。
- 操作序列摘要。
- 车号集合。
- trace 关联 key。

当 ID 变成事实上的结构表达时，后续改候选内部步骤会影响诊断可比性。建议保留 display id，但另建稳定 `candidate_uid` 和结构化字段。

### 7.10 artifact 爆炸反映调试成本高

`artifacts/` 下存在大量 `_debug_*`、`_probe_*`、`tmp_*`、`round*` 目录。这是研发过程中正常现象，但也说明当前诊断主要靠多轮全量/小样本试跑比较。

如果结构边界清晰，应能更多通过：

- 单候选模板单测。
- 单资源门单测。
- 单阶段转换单测。
- 小规模 fixture 回放。

而不是每次都需要全量 artifacts 比较才能判断影响。

## 8. 当前 UI 结构诊断

`app.py` 不是主要复杂度来源。它做了三类事：

1. 选择输入来源，调用 runtime。
2. 展示接口响应、候选诊断、终态和回放。
3. 展示评估 artifact dashboard。

它的问题是展示逻辑也集中在单文件中，包含大量 HTML/SVG/table helper。但这属于 UI 维护问题，不是求解结构冗杂的根因。

需要注意的是，`app.py` 通过 import/reload runtime 脚本，并以文件 mtime 做 cache fingerprint。这种方式适合 demo，不适合作为稳定服务层。真正上线时应把 runtime 变成可导入包，UI/API 只调用稳定接口。

## 9. 离线 P0-P9 trace 链路诊断

`generate_p0_p4_trace.py` 到 `generate_p9_state_update_trace.py` 不是当前 runtime 主路径。它们更多是“按目标架构生成轻量审计产物”的离线链路。

这有两个含义：

1. 它们证明团队已经意识到 P0-P9 分层的重要性。
2. 但这些分层还没有真正成为 `generate_physical_runtime_trace.py` 的内部边界。

目前存在“双轨结构”：

```text
设计/审计链路：P0-P9、H1-H5、R1-R6，分层清楚
实际求解链路：单体 P10 runtime，多函数和字符串原因承载结构
```

后续工作应把离线审计里的结构对象逐步回灌到 runtime，而不是继续让审计链路只做事后说明。

## 10. 当前实现为什么会显得“互相打补丁”

更准确地说，当前是“长期优化经验被追加进同一执行面”，不是简单乱写。

典型演化路径可能是：

1. 先实现物理 runtime 和 prefix-access generator，确保能生成接口动作。
2. 发现大库和远端案例无解，加入 depot assignment、depot digest、slot swap。
3. 发现人工计划远端来回切换更少，加入 remote session、bundle、tail closeout。
4. 发现 route clear 不能删，加入 leased corridor、front interruption、owner 判断。
5. 发现阶段不对，加入 manual baseline、phase gate、forced open 诊断。
6. 发现少钩会破坏结构，加入 structural reject、dominance audit、zero/negative delta 拒绝。

每一步都有现实问题和指标收益。但这些改动没有被重构进统一的“合同 -> 资源 -> 候选 -> delta -> gate”模型，所以越往后越像补丁层。

这也是为什么不能“想简化就简化”：许多补丁实际是某类业务异常的保护层，直接删除会立刻损害可解性或物理安全。

## 11. 主要风险

### 11.1 回归风险高

候选生成、结构拒绝和选择排序高度耦合。修改一个候选族可能影响：

- 是否进入前 128 个候选。
- 是否被 route clear 包装。
- 是否通过 phase sort。
- 是否通过 structural reject。
- 是否改变 remote session 状态。
- 是否改变 `better_case_result` 选择策略。

因此小改动可能带来非局部影响。

### 11.2 诊断口径可能被实现细节污染

例如 `phase_link7_forced_open_no_front_candidate` 既说明阶段候选不足，也可能说明候选排序/资源预判不足，还可能说明前场候选被物理路径过滤。单个 gap reason 很难稳定对应唯一结构问题。

### 11.3 优化目标容易互相抵消

当前同时追求：

- 完成率。
- 硬物理违规 0。
- 远端切换接近人工。
- 勾数低。
- 阶段不提前。
- zero/negative delta 受控。
- dominated accepted 降低。
- route clear 不伤可解性。

这些目标目前靠排序 tuple 和拒绝规则协调。没有统一的候选合法域和同结构优化域时，后续“压勾数”很容易破坏“远端 session 块状化”，反过来也一样。

### 11.4 新人或未来维护者难以定位

要理解一个 accepted candidate，通常需要同时看：

- `build_candidates` 中该候选如何生成。
- `PhysicalValidator` 是否放行。
- `phase_reject_reason` 是否 deferred。
- `structural_reject_reason` 是否拒绝。
- `selection_key` 为什么选择它。
- `apply_candidate` 如何改变状态。
- `contract_trace` 和 `runtime_phase_trace` 如何记录。

这条链路太长，不适合长期快速迭代。

## 12. 应保留的结构资产

以下内容应视为资产，而不是重构时的清理对象。

| 资产 | 保留原因 |
|---|---|
| 静态 track graph 和硬物理校验 | 当前硬物理违规为 0，是安全底座 |
| depot slot / spotting / closed-door 校验 | 覆盖真实作业硬约束 |
| H1-H5 人工阶段识别 | 是达到人工计划结构价值的必要表达 |
| RemoteSessionPlan / bundle / tail closeout | 已把远端切换压到 `manual + 2` |
| Candidate/contract/resource/phase/dominance audit CSV | 是当前定位差距的主要证据链 |
| manual baseline 解析 | 是比较人工阶段和远端分布的基础 |
| short-chain variant 识别 | 防止短链被强套标准长链 |

重构应该围绕这些资产建立边界，而不是删除它们。

## 13. 建议的目标结构

建议不要立刻“大拆文件”，而是先建立 runtime 内部接口。目标结构可以是：

```text
p10/
  domain/
    car.py
    yard.py
    topology.py
    depot_slot.py
    state.py
  contracts/
    phase.py
    flow_contract.py
    remote_session.py
    short_chain.py
  candidates/
    template.py
    generator.py
    route_clear.py
    depot.py
    remote.py
    front_capacity.py
    tail.py
  gates/
    physical.py
    phase_gate.py
    resource_gate.py
    contract_delta.py
    dominance.py
  search/
    strategy.py
    selector.py
    local_tiebreak.py
  runtime/
    runner.py
    state_update.py
    response.py
  audit/
    events.py
    csv_writers.py
```

但第一阶段不必真的建这么多文件。更重要的是把接口先分出来：

| 接口 | 输入 | 输出 | 目的 |
|---|---|---|---|
| `RuntimeState` | cars、loco、phase、remote session、depot assignment | 不可变或受控可变状态 | 集中状态语义 |
| `CandidateTemplate` | state、contract context | candidate list | 每个模板自带适用条件和资源需求 |
| `CandidateEnvelope` | candidate、contract、resources、phase intent、display id | 结构化候选 | 替代候选 ID 承载语义 |
| `GateResult` | candidate envelope、state | accept/reject/defer + typed reason | 替代字符串原因做控制流 |
| `SelectionContext` | legal candidates、metrics | selected candidate + rank explanation | 排序和选择可单测 |
| `RuntimeEvent` | accepted/rejected/deferred/state_changed | 审计事件 | 让 CSV 从事件生成 |

## 14. 分阶段改造建议

### 阶段 A：先做边界，不改行为

目标：在不改变求解结果的前提下，把主循环里最容易失控的概念对象化。

建议动作：

1. 新增 `CandidateEnvelope` dataclass，包装现有 `HookCandidate`，包含 `candidate_kind`、`selected_contract`、`structural_intent`、`remote_profile`、`resources`、`phase_scope` 等结构化字段。
2. 新增 `GateResult` dataclass，字段包括 `decision`、`reason_code`、`reason_detail`、`source_gate`、`severity`。
3. 让 `structural_reject_reason` 先返回 `GateResult`，再兼容转成旧字符串。
4. 把 `_run_case_once` 中的诊断行构造移动到 `audit` helper，主循环只发 accepted/rejected event。
5. 固化 current baseline 的 golden CSV 摘要，保证阶段 A 不改变关键指标。

验收：

| 指标 | 要求 |
|---|---|
| completed | 不低于 102 |
| hard physical violation | 0 |
| remote transition | 不高于 504 |
| R4 | 继续 passed |
| accepted candidate count / main summaries | 与基线一致或有明确差异说明 |

### 阶段 B：拆 `build_candidates`

目标：把候选生成从“一个大函数拼列表”改为“模板注册 + 适用条件 + 资源预判”。

建议先拆成 6 组：

| 组 | 包含 |
|---|---|
| `TailCloseoutGenerator` | tail direct、tail same-line、tail force group、remote tail closeout |
| `FrontCapacityGenerator` | front capacity release/digest、H1 carry、corridor closeout |
| `DirectMoveGenerator` | direct target move、multi pick/drop、same-line reorder |
| `DepotGenerator` | depot digest、slot swap、same-line repack |
| `RemoteSessionGenerator` | remote session contract、bundle、exchange、corridor |
| `RecoveryGenerator` | blocker relocation、spot release、capacity release、route clear |

每个 generator 输出：

```text
CandidateEnvelope[]
blocked_candidates[]
resource_requests[]
```

关键是不先改变候选内容，只改变组织方式。

### 阶段 C：把资源预判提前

目标：减少 `PhysicalValidator` 承担第一大裁剪器的问题。

当前最大拒绝类是路径占用。应把 route occupancy/resource request 前移：

```text
CandidateTemplate
  -> ResourceRequest(route_access, target_capacity, staging_capacity, depot_slot, spotting_window)
  -> ResourceGate preliminary decision
  -> PhysicalValidator final check
```

预期收益：

- 候选池更小。
- 拒绝原因更靠近结构问题。
- `route_clear_session` 是否必要能被资源图解释，而不是靠事后删除实验。

### 阶段 D：阶段门硬化

目标：减少 `forced_open`，让 H1-H5 成为真实边界。

建议不要直接把 `allow_phase_forced_open=False` 全量打开。应先做：

1. 对 185 个 forced open 按原因聚类。
2. 对每类 forced open 查是否缺少 phase-permitted candidate，还是候选被物理/资源拒绝。
3. 为最大类补候选或资源释放结构。
4. 每次只把一类 forced open 从 fallback 变成合法 phase transition。

验收逐步从：

```text
forced_open=185
```

降到：

```text
forced_open <= 100
forced_open <= 50
forced_open = 0
```

不要为了 R1 立即牺牲 completed。

### 阶段 E：短链专项

当前短链：

| 案例 | 人工勾数 | solver 勾数 | 差值 |
|---|---:|---:|---:|
| `0306W` | 10 | 33 | +23 |
| `0327W` | 11 | 27 | +16 |

短链不应和标准远端 session 一起优化。建议单独建立：

- `ShortChainContract`
- `ShortChainPhaseSkip`
- `DirectRepairEntryTemplate`
- `DepotDigestOnlyTemplate`
- `MixedSignalRepairConservativeTemplate`

短链目标不是先压远端切换，而是证明“不强套长链、不新增外场 obligation、钩数 <= manual + 1”。

## 15. 不建议做的事

### 15.1 不建议直接删除 route clear

现有诊断已经证明直接删除 front-only route clear 会伤可解性。它现在低效，但承担访问桥接职责。正确做法是先设计 `FrontCorridorAccessContract` 替代它，再降级旧结构。

### 15.2 不建议只按文件拆分

如果只是把 12618 行拆成多个文件，但仍共享 `cars: list[dict]`、字符串 reason、策略开关和大排序 tuple，问题不会解决。文件拆分必须跟领域接口一起发生。

### 15.3 不建议立即重写搜索器

当前系统有大量真实边界条件，直接重写搜索器很容易丢掉长期积累的异常处理。应先把现有 planlet 变成模板接口，再逐步替换内部实现。

### 15.4 不建议把人工阶段当硬编码流程

H1-H5 是阶段合同，不是固定动作序列。目标是“合法跳过、压缩、合并但可证明”，不是强制复刻人工计划。

## 16. 优先级判断

最高优先级不是“少几百行代码”，而是降低未来改动的耦合面。

建议优先级：

| 优先级 | 工作 | 原因 |
|---|---|---|
| P0 | `GateResult` / `CandidateEnvelope` / `RuntimeEvent` | 让结构语义从字符串和候选 ID 中解放出来 |
| P0 | 把诊断生成从 `_run_case_once` 移出 | 主循环先恢复清晰 |
| P1 | 拆 `build_candidates` 为 generator registry | 新候选不再继续堆到 936 行函数 |
| P1 | forced open 聚类和逐类消除 | R1 是结构证明的最大缺口 |
| P1 | ResourceRequest 前置 | 降低物理校验作为候选裁剪器的压力 |
| P2 | short-chain 专项模板 | R5 明确失败，且不应污染标准链 |
| P2 | strategy variants 收敛 | 从多策略竞跑改成输入驱动的策略选择 |
| P3 | 包结构化和 UI/API 分离 | 在内部边界稳定后做 |

## 17. 诊断结论

当前实现确实存在用户担心的问题：结构层次不够清晰，许多长期优化通过候选族、拒绝原因、排序项、策略开关互相补位。`_run_case_once`、`build_candidates`、`structural_reject_reason` 是最明显的复杂度集中点。

但这不是“删掉一半逻辑就会更好”的情况。当前复杂度里有大量真实业务资产：硬物理校验、库位规则、对位窗口、关门车、人工阶段、远端 session、短链识别、route clear 桥接、诊断 trace。它们解释了为什么当前系统能做到 102/113 完成、硬物理违规 0、远端切换全部在 `manual + 2` 内。

真正的问题是：这些资产还没有被沉淀成清晰的逻辑分层。继续在同一个 runtime 面上追加规则，会让每次优化都变成“调一个结构、看全量 artifact、再补另一个拒绝条件”的循环。

下一步应采用保守重构：先不改行为，把候选、门控、状态事件、诊断产物的边界拉出来；再拆候选生成；再前置资源门；最后逐步硬化阶段门和短链模板。这样既保留长期优化成果，又能让后续诊断和升级变得可定位、可证明、可回归。
