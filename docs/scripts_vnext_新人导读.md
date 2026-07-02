# scripts / solver_vnext 新人导读

这份文档专门讲 `/scripts` 目录。目标不是复述代码，而是帮刚接触项目的人先建立一张脑内地图：这些脚本在解决什么问题、数据怎么进来、每一步怎么判断、最后输出什么、出问题时该看哪里。

一句话先说结论：

```text
scripts 里这套 vNext 求解器，是一个“调车计划生成器 + 诊断器”。

它读入一份取送车计划 JSON，
判断每辆车应该去哪里，
不断生成候选调车动作，
用物理规则、资源规则、业务阶段规则筛掉不合法动作，
每轮选一个最划算的动作执行，
直到所有车都到目标位置，或者明确阻塞。
```

---

## 1. 这个目录里有什么

当前 `/scripts` 主要分两层：

```text
scripts/
  generate_vnext_runtime_trace.py       # 命令行入口：批量跑案例，写 CSV/JSON 诊断产物

  solver_vnext/
    engine.py                           # 主循环：每一勾怎么生成、过滤、选择、落地
    physical.py                         # 站场物理模型：线路、道岔、路径、容量、挂车顺序、验证门面和高频缓存

    domain.py                           # 数据结构：合同、资源请求、候选、阶段、trace、case result
    contracts.py                        # 把“车辆目标”翻译成“业务合同”
    flow.py                             # 识别当前车流结构：前场债、远端债、存4口债等
    phase.py                            # H1-H5 人工阶段门控
    policy.py                           # 选择策略：多个合法候选里选哪个

    episodes.py                         # 候选动作模板库：直达、远端会话、修库摘解、多点投放等
    access.py                           # 前缀让路计划：先带走挡车，再放目标车，再把挡车还回去
    planlets.py                         # 多步小计划：尾部消化、临时暂存、回放
    placement.py                        # 计算放车位置

    frontier.py                         # 当前哪些线路可达、哪些前缀车能取出来
    resources.py                        # 资源仲裁：线路容量、修库槽位、存4口、远端会话等
    resource_structures.py              # 资源结构诊断：存4、修库槽位、串联门、机车携带顺序
    serial.py                           # 串联阻挡关系：谁挡住谁

    delta.py                            # 候选执行前后，合同债务和全局未完成数变化了多少
    gate.py                             # 最终硬门：资源违规、合同倒退、无收益就拒绝

    diagnostics.py                      # 结构诊断行：每轮生成多少、拒绝多少、为什么阻塞
    connection.py                       # 连接链路诊断：Flow -> Contract -> Intent -> Candidate -> Delta
    staging.py                          # 临时暂存意图诊断
    plan_facts.py                       # 小工具：判断是否碰远端、put 数、hook 数等
```

如果只看主流程，先读这几个文件就够：

```text
generate_vnext_runtime_trace.py
engine.py
contracts.py
episodes.py
physical.py
resources.py
delta.py
phase.py
policy.py
```

---

## 2. 怎么运行

项目文档里推荐的命令是：

```bash
rtk python3 scripts/generate_vnext_runtime_trace.py --root . --output-dir artifacts/current_truth2_eval --max-hooks 300 --check
```

常用参数：

| 参数 | 含义 |
| --- | --- |
| `--root .` | 项目根目录 |
| `--truth-dir data/truth2` | 输入案例目录，默认就是 `data/truth2` |
| `--output-dir ...` | 输出诊断结果的目录 |
| `--case-id 0104W 0104Z` | 只跑指定案例，不写就是跑全部 |
| `--max-hooks 300` | 最多允许多少轮内部移动批次，防止死循环 |
| `--check` | 做基础验收：至少跑了案例，且不能接受硬物理违规 |
| `--trace-all-candidates` | 把被拒绝的候选也写进 trace，排查问题时有用，但全量跑会慢 |
| `--trace-frontier` | 每轮记录可达性快照，排查“为什么生成不出候选”时有用 |

只跑一个案例可以这样：

```bash
rtk python3 scripts/generate_vnext_runtime_trace.py --root . --case-id 0104W --output-dir artifacts/tmp_0104W --max-hooks 300 --check
```

排查疑难阻塞时可以这样：

```bash
rtk python3 scripts/generate_vnext_runtime_trace.py --root . --case-id 0104W --output-dir artifacts/debug_0104W --max-hooks 300 --trace-all-candidates --trace-frontier --check
```

项目的 Streamlit 页面 `app.py` 也会直接加载 `solver_vnext.engine.VNextSolver`，所以 `/scripts` 不只是离线评估工具，也是页面演示求解器的核心来源。

---

## 3. 输入和输出是什么

### 3.1 输入

输入是一份接口形态的 JSON，通常在：

```text
data/truth2/*.json
```

核心字段：

| 字段 | 说人话 |
| --- | --- |
| `StartStatus` | 当前每辆车在哪里、车号、车型、长度、目标线、是否称重、是否关门车等 |
| `TerminalLines` | 修1-修4库内的可用台位配置 |
| `locoNode` | 调车机当前在哪条线、哪一端 |

### 3.2 输出

运行后输出目录大概长这样：

```text
artifacts/current_truth2_eval/
  case_summary.csv
  step_trace.csv
  phase_gate_records.csv
  access_frontier_records.csv
  staging_intent_records.csv
  flow_edge_records.csv
  connection_metrics.csv
  structure_node_metrics.csv
  resource_structure_records.csv
  generation_gap_records.csv
  structure_acceptance.csv
  vnext_summary.json
  responses/
    0104W.json
    ...
```

最重要的几个：

| 文件 | 先看它干什么 |
| --- | --- |
| `responses/*.json` | 真正的接口响应，里面有 `Operations` 和 `GeneratedEndStatus` |
| `case_summary.csv` | 每个案例是否完成、用了多少勾、最终还剩多少未满足、阻塞原因 |
| `step_trace.csv` | 每一轮最终选中的候选，或者开启 `--trace-all-candidates` 后所有候选的生死记录 |
| `structure_node_metrics.csv` | 每轮生成了多少候选、拒绝多少、主要拒绝原因、最后选了谁 |
| `phase_gate_records.csv` | 当前处于 H1-H5 哪个阶段，候选是否被阶段规则允许 |
| `resource_structure_records.csv` | 存4、修库槽位、串联门、机车携带顺序等资源结构有没有问题 |
| `structure_acceptance.csv` | 汇总验收表：每类结构 pass / warn / fail / unknown |
| `generation_gap_records.csv` | 一轮连候选都生成不出来时，看这里解释为什么 |

---

## 4. 先理解几个核心概念

### 4.1 车不是直接被移动，先被翻译成“债务”

代码不会一上来就说“把 A 车从存5搬到修1”。它先问：

```text
这辆车现在的位置，和它最终应该去的位置相比，差在哪里？
```

如果车还没到位，就产生一个未完成项。多个未完成项会被归到不同业务族，也就是 `ContractFamily`：

| 合同族 | 说人话 |
| --- | --- |
| `REPAIR_INBOUND` | 要进修库 |
| `DEPOT_SLOT` | 要去修库外/库内指定槽位 |
| `DEPOT_OUTBOUND` | 从修库区域出来，释放修库资源 |
| `CUN4_PORT_STAGING` | 存4线作为关键口/缓冲口的组织 |
| `PRE_REPAIR_STAGING` | 去预修线 |
| `DISPATCH_SHED_QUEUE` | 去调梁棚/调梁线北 |
| `FUNCTION_LINE_SERVICE` | 去洗罐、油漆、抛丸、卸轮等功能线 |
| `LOCO_AREA_STAGING` | 去机库、机走棚、机走北等机车区域 |
| `YARD_REBALANCE` | 存车线之间整理、腾位 |
| `SPECIAL_REPAIR_PROCESS` | 称重、特殊修程 |
| `REMOTE_SESSION` | 聚合的远端会话合同，专门处理修库/卸轮/存4这类远端交互 |
| `TAIL_CLOSEOUT` / `RESIDUAL` | 尾项或兜底 |

这一步主要在 `contracts.py`：

```text
车辆状态 -> CarRef -> FlowContract
```

### 4.2 Contract 是“我要完成什么”，Episode 是“我可以怎么做”

`FlowContract` 只描述目标，不描述动作。

比如合同说：

```text
这批车现在在存4线，目标是修1库内/修2库内，需要完成远端入库。
```

真正怎么动，要靠 `episodes.py` 里的模板生成候选动作。

可以把它理解成：

```text
合同：我要把这些车送到对的位置。
模板：我尝试用某种人工套路生成一套 Get / Put 步骤。
候选：某个具体动作方案。
```

### 4.3 Candidate 只是候选，不代表能执行

候选生成出来之后，还要过很多关：

```text
物理能不能走？
线路够不够长？
能不能从这一端取到车？
放车位置会不会撞？
修库槽位对不对？
存4口会不会被污染？
串联线路有没有先清门？
这个动作让合同变好还是变坏？
当前人工阶段允许不允许？
```

只有都通过，才可能被选中。

### 4.4 Delta 是“动作带来的变化”

`delta.py` 做的是试算：

```text
如果执行这个候选，车辆状态会变成什么样？
未完成车辆数减少了吗？
当前合同债务减少了吗？
有没有把已经做好的东西破坏掉？
有没有释放重要阻挡线？
```

所以 vNext 不是“生成一个动作就做”，而是：

```text
先模拟执行，再看收益和副作用。
```

---

## 5. 一次完整求解过程

入口在 `generate_vnext_runtime_trace.py`，真正的主循环在 `engine.py` 的 `VNextSolver.solve_case()`。

完整过程可以按下面理解。

### 第 0 步：读取案例

`physical.read_case(truth_path)` 会做这些事：

```text
读取 JSON
校验输入字段
标准化线路名和车辆字段
计算修库台位分配 depot_assignment
读取调车机初始位置 loco_location
生成 case_id，例如 0104W
```

`physical.py` 直接给一些高频函数加缓存，比如：

```text
车辆编号 car_no
线路名 normalize_line
未满足车辆 unsatisfied_cars
线路车辆分组 cars_by_line
按可接近顺序排列某条线上的车 line_cars_in_access_order
```

### 第 1 步：初始化状态

`SolverState` 里放的是求解器当前记忆：

| 字段 | 含义 |
| --- | --- |
| `cars` | 当前所有车的位置 |
| `depot_assignment` | 修库目标槽位分配 |
| `loco_location` | 调车机当前位置 |
| `hook_index` | 当前第几轮内部移动批次 |
| `visited_signatures` | 已经出现过的状态，防止循环 |
| `remote_session_open` | 远端会话是否打开 |
| `last_business_remote` | 上一次业务挂/摘是否在远端 |
| `serial_gate_leases` | 临时打开的串联门租约 |

### 第 2 步：判断是否已经完成

每轮开头都会检查：

```text
physical.unsatisfied_cars(state.cars, state.depot_assignment)
```

如果没有未满足车辆，说明所有车都到目标，循环结束，案例完成。

### 第 3 步：识别当前阶段 H1-H5

`flow.py` 先统计当前还有哪类债务：

| 债务 | 含义 |
| --- | --- |
| `front_debt` | 前场/普通功能线/整理类未完成 |
| `cun4_port_debt` | 存4口相关未完成 |
| `remote_debt` | 修库、库外、卸轮等远端交互未完成 |
| `closeout_debt` | 尾项收束未完成 |

然后 `phase.py` 把状态分成 H1-H5：

| 阶段 | 说人话 |
| --- | --- |
| `H1_FRONT_SERVICE` | 前场服务优先，先处理功能线、预修、调棚、普通整理 |
| `H2_CUN4_PORT` | 组织存4口，为后续远端会话准备 |
| `H3_RELEASE_ACCEPT` | 远端会话开启前的释放/接车准备 |
| `H4_REMOTE_DEPOT` | 修库、库外、卸轮等远端主会话 |
| `H5_CLOSEOUT` | 主债务结束后的尾项收束 |

这里有一个重要原则：

```text
阶段通常只能往后走，不能随便倒退。
远端会话打开后，只要远端债还没清，就继续 H4。
```

### 第 4 步：记录当前结构快照

每轮会记录几类诊断：

```text
flow_edge_records        当前有哪些车流边
resource_structure       存4、修库槽位、串联门等资源状态
access_frontier          如果开启 --trace-frontier，记录哪些线可达、哪些被挡
```

这些不是为了求解本身，而是为了事后回答：

```text
当时求解器“看见”的结构是什么？
它为什么做这个选择？
它为什么没有候选？
```

### 第 5 步：生成合同

`build_contracts(state.cars, state.depot_assignment)` 会把所有没到位的车按业务族、来源、目标分组，生成一批 `FlowContract`。

例子：

```text
REPAIR_INBOUND: 存4线 -> 修1库内 : 车 A/B/C
FUNCTION_LINE_SERVICE: 存5线北 -> 洗罐站 : 车 D/E
DEPOT_OUTBOUND: 修2库内 -> 存4线 : 车 F/G
REMOTE_SESSION: 聚合所有远端相关未完成车
```

然后 `policy.order_contracts()` 会根据当前阶段排序。

比如 H4 远端阶段会优先看：

```text
REMOTE_SESSION
DEPOT_OUTBOUND
REPAIR_INBOUND
DEPOT_SLOT
```

### 第 6 步：每个 Episode 生成候选

`EPISODES` 是候选模板列表，顺序在 `episodes.py` 末尾：

```text
DepotOutboundSessionEpisode
RemoteSessionEpisode
DirectMoveEpisode
SerialGateClearEpisode
TailBlockerPeelDigestEpisode
PrefixDigestEpisode
DepotRepackWithInboundTailEpisode
DepotMultiDropEpisode
DepotSlotFillEpisode
DepotSlotSwapEpisode
RemoteDepotEpisode
TailCloseoutEpisode
```

每个模板都先判断：

```python
episode.applies(contract)
```

如果这个模板适合当前合同，就尝试生成候选。

下面是主要模板的人话解释：

| 模板 | 做什么 |
| --- | --- |
| `DirectMoveEpisode` | 最简单的直达：从源线取前缀车，直接放到目标线 |
| `RemoteDepotEpisode` | 远端修库类直达，逻辑继承自直达，但只服务修库/库外/出库 |
| `RemoteSessionEpisode` | 一次远端会话里尽量成批处理远端相关车，减少来回切换 |
| `DepotOutboundSessionEpisode` | 从修库/库外/卸轮等远端区域成批取出，通常放到存4等目标 |
| `DepotMultiDropEpisode` | 一次取一串车，按尾部顺序投放到多个修库目标 |
| `DepotSlotFillEpisode` | 修库里前面槽位空着、后面有锁定车时，补齐前置槽位 |
| `DepotSlotSwapEpisode` | 修库目标槽位被别的车占了，尝试交换/摘解 |
| `PrefixDigestEpisode` | 一条线前缀里有多辆车，按尾部目标逐段消化，剩余的还回源线 |
| `TailBlockerPeelDigestEpisode` | 尾部车挡住后续消化时，先剥离到暂存线，或剥离后继续消化 |
| `DepotRepackWithInboundTailEpisode` | 修库已有车和待入库尾部车需要重新组合入库 |
| `SerialGateClearEpisode` | 某条串联阻挡线挡住下游目标，先把挡车搬走，打开“门” |
| `TailCloseoutEpisode` | 尾项收束阶段的直达 |

### 第 7 步：候选先过物理校验

候选会进入：

```text
physical.validate_candidate(...)
```

底层是 `physical.py` 里的直接物理校验函数。

它会检查很多硬约束，常见包括：

| 校验 | 解释 |
| --- | --- |
| 路径是否存在 | 调车机从当前位置能不能到源线/目标线 |
| 路径是否被占用 | 有些线/边被别的车占着，不能直接通过 |
| 是否能从可接近端取到车 | 不是想取哪辆就能取哪辆，前面有车挡着就不行 |
| Put 顺序是否合法 | 机车携带车辆是有顺序的，放车通常是尾部先放 |
| 线路长度是否够 | 目标线放下这些车后是否超长 |
| 调车等效辆数限制 | `PULL_LIMIT_EQUIVALENT = 20` |
| 修库槽位规则 | 修1-修4库内/库外的位置、锁定位、修程限制 |
| 强制对位规则 | `ForceTargetPosition` 相关位置必须满足 |
| 称重规则 | 称重线、称重车完成方式 |
| 关门车规则 | 最后还会回放检查关门车顺序 |
| 串联阻挡规则 | 比如某些线要先清外侧/上游，里面才能进 |

如果这里失败，候选直接拒绝，原因写进 trace。

### 第 8 步：候选申请资源

物理通过后，`resources.py` 会根据候选涉及的线路和动作生成 `ResourceRequest`。

常见资源：

| 资源 | 解释 |
| --- | --- |
| `LOCO_POSITION` | 调车机位置 |
| `LOCO_CARRY` | 机车当前挂着哪些车 |
| `ROUTE_GET` / `ROUTE_PUT` | 取车/放车路径 |
| `LINE_CAPACITY` | 普通线路容量 |
| `DEPOT_SLOT` | 修库槽位 |
| `CUN4_NORTH_BUFFER` | 存4北缓冲口 |
| `REMOTE_SESSION` | 远端会话 |
| `GLOBAL_GATE` | 全局口门控制 |
| `WEIGH_STAND` | 称重点 |
| `SERIAL_LINE_GATE` | 串联线路门 |

资源层不管“业务收益高不高”，它只回答：

```text
这个候选有没有资源硬违规？
```

比如：

```text
不能把普通车随便放到修库槽位。
不能把运行线当存车线。
不能污染存4口。
不能在下游债务没清时又把串联门堵回去。
```

### 第 9 步：试算执行后的 Delta

`delta.py` 会复制一份车辆状态，然后模拟执行候选。

它会得到：

| 字段 | 含义 |
| --- | --- |
| `before_unsatisfied` | 执行前全局未满足车辆数 |
| `after_unsatisfied` | 执行后全局未满足车辆数 |
| `before_contract_debt` | 执行前当前合同还欠多少 |
| `after_contract_debt` | 执行后当前合同还欠多少 |
| `contract_reduction` | 当前合同减少了多少债 |
| `support_gain` | 虽然不直接完成合同，但释放了串联门等结构收益 |
| `effective_gain` | 合同收益 + 支撑收益 |
| `broken` | 是否让合同或全局状态变坏 |

这里最关键的是：

```text
候选必须让事情变好，或者至少产生明确结构收益。
```

### 第 10 步：AcceptRejectGate 最终硬门

`gate.py` 的规则很短，但很硬：

```text
有资源违规 -> 拒绝
合同被破坏 -> 拒绝
合同和全局都没有正收益 -> 拒绝
否则接受
```

另外 `engine.py` 还会检查：

```text
如果执行后状态签名已经出现过 -> 拒绝，防止循环
```

### 第 11 步：阶段门控

即使候选通过了物理、资源、delta，也还要问 `phase.py`：

```text
当前 H1/H2/H3/H4/H5 阶段，允许做这个合同族吗？
```

比如 H1 阶段主要允许前场服务，H4 阶段主要允许远端修库。

也有“支持动作”例外，比如：

```text
清串联门虽然不是当前主合同，但能释放后续债务，可以作为 support 通过。
```

阶段门控的意义是避免求解器东一下西一下，像无头苍蝇一样在全站乱跳。

### 第 12 步：多个可接受候选里选一个

如果一轮里有多个候选都合法，`policy.py` 负责排序。

排序思路大概是：

```text
当前阶段最需要的合同优先。
能一次减少更多合同债务的优先。
远端会话打开后，尽量连续处理远端，减少远端/非远端来回切换。
能成批消化的优先。
碎片化、小收益、容易返工的候选靠后。
```

最终选中的候选就是这一轮真实执行的动作。

### 第 13 步：落地执行并更新状态

选中后，`engine.py` 会做这些事：

```text
写 StepTrace
写 PhaseGateRecord
写 ConnectionMetricRecord
写 ResourceStructureRecord
写 StagingIntentRecord
把候选转成接口 Operations
更新串联门租约 serial_gate_leases
更新 cars
更新 loco_location
记录 visited_signatures
更新 remote_session_open / last_business_remote
hook_index += 1
```

然后进入下一轮。

### 第 14 步：结束

循环结束有几种情况：

| 结束原因 | 状态 |
| --- | --- |
| 所有车都满足目标 | `completed` |
| 没有合同 | `blocked: no_active_contract` |
| 没有生成候选 | `blocked: no_episode_candidate_generated` |
| 候选都被拒绝 | `blocked: all_episode_candidates_rejected:数量` |
| 超过最大轮数 | `blocked: max_hook_limit_reached` |
| 关门车回放违规 | `blocked: closed_door_replay...` |

如果有操作，响应写到：

```text
output_dir/responses/{case_id}.json
```

---

## 6. 一个动作在系统里怎么流转

可以把单轮流程记成这条链：

```text
车辆现状
  -> build_car_refs
  -> build_contracts
  -> policy.order_contracts
  -> episode.generate
  -> physical.validate
  -> resources.request_for / acquire
  -> delta.simulate_candidate / build_contract_delta
  -> gate.decide
  -> phase.permission
  -> policy.better
  -> apply selected candidate
```

更口语化一点：

```text
先看还欠什么活。
再决定现在优先干哪类活。
再用人工套路试着编几个动作方案。
每个方案先看能不能走、能不能放、会不会违规。
再看做完后是不是更接近目标。
再看当前阶段该不该做。
最后从合法方案里挑一个最值的。
```

---

## 7. physical.py 为什么这么大

`physical.py` 是这个目录里最大的文件，因为它把很多“现场硬规则”都放进来了。

它包含几类东西：

### 7.1 线路和站场图

比如：

```text
TRACK_SPECS          每条线长度、类型
LINE_ATTACHMENTS     每条线连到哪些道岔/节点
SWITCH_EDGES         道岔/节点之间怎么连
SERIAL_LINE_BLOCKERS 串联阻挡关系
```

`TrackGraph` 会基于这些信息算路径。

### 7.2 车辆和目标标准化

包括：

```text
normalize_line
normalized_car
target_lines
car_no
car_length
force_positions
```

这部分负责把输入里各种中文别名、空值、字段差异统一成求解器能处理的格式。

### 7.3 修库目标槽位分配

核心是：

```text
build_depot_assignment
planned_target_for_car
car_is_satisfied
```

它会判断一辆车最终应该在：

```text
哪条线
哪个位置
为什么这么分配
```

### 7.4 放车位置计算

比如：

```text
planned_positions_for_batch
first_free_south_positions_for_batch
candidate_positions_available
target_position_is_acceptable
```

这部分决定一批车放到目标线时应该占哪些位置。

### 7.5 取车/放车可达性

常见函数：

```text
line_access_order
line_cars_in_access_order
source_access_node
inaccessible_get_reason
inaccessible_put_reason
route_blocking_lines
```

这解决的问题是：

```text
这辆车看起来在这条线，但从调车机这一端真的拿得到吗？
如果要放这几辆，机车携带顺序允许吗？
```

### 7.6 候选动作和执行

核心数据结构：

```text
PlanStep       一个 Get / Put / Weigh 步骤
HookCandidate  一组步骤组成的候选
OperationTraceRow 最终输出操作行
```

核心函数：

```text
hook_candidate
planlet_candidate
operation_rows
response_operation
apply_candidate
state_signature
```

---

## 8. 重点诊断文件怎么读

### 8.1 `case_summary.csv`

先看它。

关键列：

| 列 | 意义 |
| --- | --- |
| `case_id` | 案例编号 |
| `status` | `completed` 或 `blocked` |
| `hook_count` | Get/Put 业务勾数口径 |
| `operation_count` | 输出 Operations 数量，包含 Weigh |
| `initial_unsatisfied` | 初始未满足车辆数 |
| `final_unsatisfied` | 最终未满足车辆数 |
| `blocked_reason` | 阻塞原因 |
| `hard_physical_violation_accepted_count` | 接受了硬物理违规的次数，应该是 0 |

如果 `status=blocked`，先看 `blocked_reason`。

### 8.2 `step_trace.csv`

这是看“每一轮为什么选这个”的主文件。

关键列：

| 列 | 意义 |
| --- | --- |
| `hook_index` | 第几轮 |
| `phase` / `phase_reason` | 当时处于什么阶段 |
| `candidate_id` | 候选动作唯一 ID |
| `contract_id` | 这个动作服务哪个合同 |
| `family` | 合同族 |
| `intent` | 结构意图 |
| `template_name` | 哪个 Episode 模板生成的 |
| `source_line` / `target_line` | 源线和目标线 |
| `move_nos` | 本候选涉及车辆 |
| `gate_accepted` | 是否通过硬门 |
| `selected` | 是否最终被选中 |
| `gate_reason` | 接受/拒绝原因 |
| `physical_reasons` | 物理拒绝原因 |
| `resource_violations` | 资源拒绝原因 |
| `contract_reduction` | 当前合同减少了多少债 |
| `effective_gain` | 有效收益 |
| `total_reduction` | 全局未满足减少了多少 |

默认只写选中的步骤。要看所有被拒绝候选，需要加：

```bash
--trace-all-candidates
```

### 8.3 `structure_node_metrics.csv`

这是每轮候选生成和拒绝统计。

适合回答：

```text
这一轮是没生成候选，还是生成了但都被拒了？
主要卡在物理、资源、合同、阶段，还是循环？
```

关键列：

| 列 | 意义 |
| --- | --- |
| `generated_candidate_count` | 生成候选数 |
| `accepted_candidate_count` | 硬门接受数 |
| `rejected_candidate_count` | 拒绝数 |
| `physical_reject_count` | 物理拒绝 |
| `resource_violation_count` | 资源拒绝 |
| `contract_reject_count` | 合同无收益/倒退 |
| `phase_veto_count` | 阶段门控拒绝 |
| `loop_reject_count` | 状态循环 |
| `top_reject_reasons` | 最常见拒绝原因 |
| `selected_template_name` | 最终选中的模板 |
| `blocked_reason` | 如果这一轮阻塞，原因在这里 |

### 8.4 `phase_gate_records.csv`

看阶段是否合理。

重点列：

| 列 | 意义 |
| --- | --- |
| `from_phase` / `to_phase` | 阶段变化 |
| `transition_type` | enter / stay / exit / skip / fail |
| `predicate_values` | 阶段判断时的债务数 |
| `blocked_contract_ids` | 被阶段挡住的合同 |
| `reject_reason` | 阶段拒绝原因 |

如果 `transition_type=fail` 或 `phase_gate_bypass_count` 不为 0，就要重点看。

### 8.5 `resource_structure_records.csv`

看资源结构有没有被破坏。

主要结构：

| 结构 | 看什么 |
| --- | --- |
| `CUN4_NORTH_BUFFER` | 存4口是否混脏 |
| `DEPOT_SLOT_GRAPH` | 修库槽位快照 |
| `SERIAL_GATE_LEASE` | 串联门是否被堵、租约是否被污染 |
| `LOCO_CARRY_STATE` | 机车携带/放车顺序是否干净 |
| `DEPOT_SWAP_DELTA` | 修库交换/摘解后是否满足槽位 |

### 8.6 `generation_gap_records.csv`

当 `blocked_reason=no_episode_candidate_generated` 时看它。

它会告诉你：

```text
这个合同有没有适用模板？
源线可达吗？
目标线可达吗？
源线前缀是什么？
是不是串联阻挡？
```

### 8.7 `structure_acceptance.csv`

这是汇总验收表。

常见状态：

| 状态 | 意思 |
| --- | --- |
| `pass` | 通过 |
| `warn` | 有风险，需要人工看 |
| `fail` | 明确失败 |
| `unknown` | 没开对应 trace，或本次没有覆盖到 |

`unknown` 不一定是坏事。比如没加 `--trace-frontier`，`AccessFrontierTraceCoverage` 就可能是 `unknown`。

---

## 9. 遇到问题怎么查

### 9.1 案例 blocked

建议顺序：

```text
1. 看 case_summary.csv 的 blocked_reason
2. 看 structure_node_metrics.csv 最后一轮
3. 如果是 no_episode_candidate_generated，看 generation_gap_records.csv
4. 如果是 all_episode_candidates_rejected，加 --trace-all-candidates 重跑，看 step_trace.csv
5. 如果物理拒绝多，看 physical_reasons
6. 如果资源拒绝多，看 resource_violations 和 resource_structure_records.csv
7. 如果阶段拒绝多，看 phase_gate_records.csv
```

### 9.2 勾数太多

先看：

```text
step_trace.csv
```

关注：

```text
template_name
contract_reduction
effective_gain
total_reduction
remote_business_transition_count
```

常见原因：

```text
候选太碎，每次只消化 1 辆。
远端会话没有连续处理，来回切换。
多点投放模板没有命中。
修库槽位被挡，走了很多支撑动作。
阶段门控太保守，拒绝了本该提前做的支撑动作。
```

### 9.3 明明有路，为什么说不可达

看：

```text
access_frontier_records.csv
step_trace.csv.physical_reasons
```

注意这里的“不可达”不只是线路图没有路，还可能是：

```text
路线上有车占用。
某条串联阻挡线没清。
从当前调车机端取不到那辆车。
放车需要的携带顺序不满足。
预修折返长度不够。
目标线容量或位置不合法。
```

### 9.4 修库槽位不对

看：

```text
resource_structure_records.csv
step_trace.csv.resource_violations
step_trace.csv.physical_reasons
```

关键词：

```text
depot_slot_unsatisfied_put
depot_locked_slot_collision
depot_slot_rule_violation
depot_vehicle_not_satisfied
```

### 9.5 存4相关问题

看：

```text
flow_edge_records.csv
resource_structure_records.csv
phase_gate_records.csv
```

关键词：

```text
CUN4_NORTH_BUFFER
CUN4_NORTH_BUFFER_DELTA
cun4_buffer_requires_owner
H2_CUN4_PORT
```

---

## 10. 新人读代码的推荐路线

不要从 `physical.py` 第一行硬啃到最后一行。建议这样读：

### 第一遍：跑通和看结果

```bash
rtk python3 scripts/generate_vnext_runtime_trace.py --root . --case-id 0104W --output-dir artifacts/learn_0104W --max-hooks 300 --check
```

然后按顺序打开：

```text
artifacts/learn_0104W/case_summary.csv
artifacts/learn_0104W/step_trace.csv
artifacts/learn_0104W/responses/0104W.json
```

先搞清楚一轮动作长什么样。

### 第二遍：看主循环

读：

```text
scripts/generate_vnext_runtime_trace.py
scripts/solver_vnext/engine.py
```

只关心主链路：

```text
读案例
while 未完成:
  建合同
  生成候选
  校验
  算 delta
  选择
  应用
写结果
```

### 第三遍：看合同和阶段

读：

```text
contracts.py
flow.py
phase.py
policy.py
```

理解：

```text
车为什么被归成这个 family？
为什么当前是 H1/H2/H4？
为什么某类合同优先？
为什么这个候选比另一个候选更好？
```

### 第四遍：看模板

读：

```text
episodes.py
access.py
planlets.py
placement.py
```

重点不是记住所有细节，而是记住每个模板的意图：

```text
直达
远端成批
多点投放
前缀消化
尾部剥离
修库换槽
串联门清理
```

### 第五遍：带问题读 physical.py

只有当你遇到具体拒绝原因时，再去 `physical.py` 搜关键词。

比如 trace 里有：

```text
target_line_length_violation
```

就搜：

```bash
rtk rg -n "target_line_length_violation|line_has_length_capacity" scripts/solver_vnext/physical.py scripts/solver_vnext/frontier.py
```

这样读效率最高。

---

## 11. 常见名词对照

| 名词 | 通俗解释 |
| --- | --- |
| `case` | 一个取送车计划案例 |
| `car` | 一辆车 |
| `line` | 股道/线路 |
| `loco` | 调车机 |
| `hook` | 求解器的一轮内部移动批次，可能包含多个 Get/Put/Weigh 操作 |
| `Operation` | 最终接口输出的一条动作 |
| `Get` | 取车 |
| `Put` | 放车 |
| `Weigh` | 称重 |
| `candidate` | 候选动作方案，还没确定执行 |
| `planlet` | 多个 Get/Put 组成的小计划 |
| `contract` | 当前要履约的业务目标 |
| `debt` | 还没完成的目标数量 |
| `delta` | 执行动作前后变化 |
| `frontier` | 当前物理可达边界 |
| `resource` | 共享资源，例如修库槽位、存4口、远端会话 |
| `phase` | H1-H5 人工阶段 |
| `staging` | 临时暂存 |
| `serial gate` | 串联线路门，某条线堵着会影响下游 |
| `remote` | 修库/库外/卸轮等远端交互区域 |

---

## 12. 这套设计最核心的工程思想

这套代码最重要的不是某一个模板，而是它把“调车”拆成了几层：

```text
事实层：车在哪里、目标在哪里、线路怎么连。
合同层：哪些业务目标还没完成。
候选层：用哪些人工套路生成可能动作。
物理层：动作真实能不能走、能不能取、能不能放。
资源层：共享资源会不会被破坏。
变化层：动作做完到底有没有收益。
阶段层：当前人工节奏允许不允许。
策略层：多个合法动作里选哪个。
诊断层：把每一步为什么通过/拒绝写出来。
```

这也是为什么代码看起来模块很多。

它不是为了复杂而复杂，而是为了避免所有规则堆在一个巨大 if/else 里。每层只回答一个问题：

```text
contracts.py：现在欠什么？
episodes.py：能尝试怎么做？
physical.py：物理上能不能做？
resources.py：资源上能不能做？
delta.py：做完有没有变好？
phase.py：当前阶段该不该做？
policy.py：多个好方案选哪个？
engine.py：把这些串起来。
```

新同学只要抓住这条线，再回头看具体函数，就不会迷路。
