# FlowEdge 调车关系边结构建模设计

这份文档是在 `ShuntingStructure` 之后继续收敛。

`ShuntingStructure` 把现场拆成：

```text
MainChain
Port
Receiver
Blocker
Debt
Lock
TailState
```

这个方向比 workflow 更接近本质，但仍有一个风险：

**对象太多，每个对象都需要角色、健康度、债务、生命周期，最后会变成一堆结构互相解释。**

后续实测证明，单靠一条 `FlowEdge` 不能承接全站冲突。

更准确的承接方式是：

```text
先用 FlowEdge 识别车流。
再用 EdgeContract 定义履约。
最后用 StationResourceGraph 仲裁共享资源。
```

也就是：

```text
FlowEdge + EdgeContract + StationResourceGraph
```

说人话：

```text
哪一批车，从哪里来，经过哪个关键口，要去哪个接收端，现在推进到哪一步，被什么挡住，还欠什么没完成。
```

---

## 1. 核心结论

调车不是一堆对象状态管理，而是一批车沿着关键口流向接收端的关系推进。

最终主干：

```text
FlowGraph
  -> TargetContract
  -> StructuralIntent
  -> WorkPatternTemplate
  -> ResourceRequest
  -> ContractDelta
  -> ResourceDelta
  -> Accept
```

含义：

```text
FlowGraph:
  当前有哪些调车关系边。

TargetContract:
  当前最该履约的边合同。

StructuralIntent:
  当前合同下一步要形成的结构意图，例如开入口、清资源口、消化大库、恢复交接。

WorkPatternTemplate:
  能服务这个结构意图的人工作业形态模板，只提供候选动作边界，不做主控。

ContractDelta:
  候选动作让这条边合同怎么变化。

ResourceDelta:
  候选动作申请、占用、释放了哪些共享资源。

Accept:
  是否接受这个合同变化和资源变化。
```

这不是复刻人工动作。

人工提供的是结构价值标准：

```text
存4释放口更好
主流更连续
机接更完整
修库摘解更彻底
轮线不再夹主流
尾项更纯
```

算法要做的是：

```text
在这些结构价值标准下，选择更少勾、更稳定、更少返工的 ContractDelta。
```

这不是把 `FlowEdge` 推翻，但必须修正早期说法。

更准确的关系是：

```text
FlowEdge 是车流识别对象。
EdgeContract 是车流履约对象。
StationResourceGraph 是全站资源仲裁对象。
StructuralIntent 是合同下一步的结构目标。
WorkPattern 是服务结构目标的动作模板库，不是新的控制中心。
ContractDelta 是动作对合同的影响。
ResourceDelta 是动作对资源的影响。
```

说人话：

```text
不要只问“这条边有没有变化”。
要问“这条边当前承诺了什么，这个动作是在履约，还是在违约”。
```

### 1.0A Work Pattern 的重新定位

已有人工方案里总结的 8 类 `Work Pattern` 是有价值的。

但它们不能成为新的主控层。

更准确的位置是：

```text
TargetContract 先决定当前服务哪条业务流。
StructuralIntent 再决定这条业务流下一步要形成什么结构。
WorkPatternTemplate 只选择能服务这个 intent 的人工套路模板。
LocalTieBreakSearch 只在同一 intent / 同一模板边界内比较勾数。
```

也就是说：

```text
不是：
  选择 pattern -> 生成动作 -> 搜索能不能过

而是：
  选择 contract -> 生成 intent -> 套用可服务 intent 的 pattern template -> 验证 delta
```

这个定位专门防止两个偏差：

- 把 `WorkPatternSelector` 写成另一个 workflow 阶段机。
- 把 `_tier()` / recent / cooldown / post-action patch 写成事实上的控制中心。

`Work Pattern` 仍然保留人工经验，但只能作为模板库进入候选生成。

它不能：

- 改写 `TargetContractSelector` 的合同选择。
- 绕过 `StationResourceGraph` 的资源仲裁。
- 绕过 `AcceptRejectGate` 的硬违反。
- 用人工动作序列替代 `ContractDelta + ResourceDelta` 验收。

### 1.1 覆盖范围修正：不是修库主链合同，而是全站流向合同

前面的修库主链研究只能解释一部分车辆。

如果真实数据确认：

```text
修库主链约 22%~31%，平均约 23%
非修库主链约 69%~78%
```

那么 `EdgeContract` 不能只覆盖修库/大库/机接。

否则会出现：

```text
FlowEdge 很懂 23% 的修库车。
但 77% 左右的出库、预修、调棚、存车整理、功能线车辆没有合同。
这些车只能掉进 residuals。
```

这不是实现细节问题，而是架构覆盖问题。

所以本文档的目标必须从：

```text
修库主链合同体系
```

升级为：

```text
全站流向合同体系
```

修库主链只是其中一个合同族：

```text
StationFlowContract:
  REPAIR_INBOUND        # 入修库主链
  DEPOT_OUTBOUND        # 修1-4 出库到存4北，腾大库
  PRE_REPAIR_STAGING    # 预修暂存编组
  DISPATCH_SHED_QUEUE   # 调棚作业排队
  YARD_REBALANCE        # 存1/2/3/5 存车整理、腾位
  FUNCTION_LINE_SERVICE # 油/洗/抛等功能线
  LOCO_AREA_STAGING     # 机库/机北/机棚缓冲
  SPECIAL_REPAIR_PROCESS # 拉走/称重/留/轮对架等特殊修程
  TAIL_CLOSEOUT         # 真尾项收束
```

核心原则：

```text
residuals 只能接少量证据不足的边角车。
不能承接全站 70% 以上的主体车流。
```

---

## 2. 为什么 FlowEdge 比 ShuntingStructure 更显式

`ShuntingStructure` 的多个对象可以收敛到 `FlowEdge` 字段里：

| ShuntingStructure 对象 | FlowEdge 中的位置 |
| --- | --- |
| `MainChain` | `subject` |
| `Port` | `via_port` |
| `Receiver` | `to_receiver` |
| `Blocker` | `blockers` |
| `Debt` | `obligations` 未完成部分 |
| `Lock` | `protections` 和 `status >= ACCEPTED` |
| `TailState` | 没有 active FlowEdge 后的剩余状态 |

也就是说：

```text
不是不要 MainChain/Port/Receiver。
而是不让它们成为互相竞争的顶层对象。
它们只是 FlowEdge 的组成字段。
```

这样每次判断只围绕一个问题：

```text
这条边有没有向前推进？
```

---

## 3. FlowGraph

```text
FlowGraph:
  edges: list[FlowEdge]
  residuals: list[ResidualItem]
  evidence:
    explicit_signals
    inferred_signals
    contradictions
    confidence
```

`FlowGraph` 只做三件事：

- 保存当前所有活跃调车关系边。
- 保存暂时无法归入边的残余项。
- 保存证据和矛盾，防止缺信号 case 被强行分类。

它不直接决定下一步动作。

### 3.1 FlowGraph 的边界

`FlowGraph` 不是新的 workflow。

它只回答一个问题：

```text
当前现场里，有哪些“车流关系”正在推进，哪些还没法归边。
```

它不做这些事：

- 不按固定阶段驱动求解。
- 不替代 move generator。
- 不保存一堆独立控制器。
- 不把人工动作序列写死。

它允许派生 view，但 view 只能服务一条边：

```text
PortView       = 某条 edge 的 via_port 健康度
ReceiverView   = 某条 edge 的 to_receiver 消化能力
SubjectView    = 某条 edge 的 subject 连续性
ObligationView = 某条 edge 的未完成事项
ProtectionView = 某条 edge 的不可破坏结构
```

所有 view 都不能反过来成为主裁决中心。

### 3.2 FlowGraph 的最小不变量

为了避免 `FlowGraph` 变成另一个“什么都装”的大结构，必须守住 6 个不变量：

| 不变量 | 含义 | 失败后果 |
| --- | --- | --- |
| `vehicle_ownership` | 一辆车同一时刻最多只能属于一条 active edge 的 `subject` | 多条边抢同一批车，后续评分失真 |
| `active_contract_set` | active contract 可以并发，必须受资源仲裁约束；实测常见约 6 个并发族 | 写成 1 条主边会漏掉全站并发，写成无限并发会退化成全局搜索 |
| `status_monotonicity` | 高置信 edge 原则上只能前进，不能随便倒退 | 机接后又回外场重编 |
| `protection_hardness` | `status >= ACCEPTED` 后 protection 是硬约束 | 已接大列被拆散 |
| `residual_expiry` | residual 必须有归边、废弃或尾项确认条件 | residual 变垃圾桶 |
| `evidence_traceability` | 每个 status 和 obligation 必须能追到现场信号或人工案例模式 | 结构判断变成拍脑袋 |

这几个不变量比“多加一层状态机”更重要。

`active_contract_set` 特别说明：

```text
旧口径“默认 1 条主边 + 少量从属边”已经不成立。

实测每案常见多个合同族并发：
  REPAIR_INBOUND
  DEPOT_OUTBOUND
  PRE_REPAIR_STAGING
  DISPATCH_SHED_QUEUE
  YARD_REBALANCE
  FUNCTION_LINE_SERVICE

所以新不变量不是 active edge 少。
而是 active contract 必须有边界、有资源占用、有仲裁顺序。
```

正确约束是：

```text
active_contract_count 可以是 5~8。
但每个 active contract 必须满足：
  有 owner vehicles
  有 resource request
  有 must_progress
  有 must_not_break
  有 expiry_condition
```

说人话：

```text
不是让 FlowGraph 更聪明。
而是让它更克制。
它只保存能影响下一钩好坏的结构事实。
```

---

## 4. FlowEdge

### 4.1 核心字段

```text
FlowEdge:
  edge_id
  edge_key
  subject
  from_area
  via_port
  to_receiver
  status
  contract
  blockers
  obligations
  protections
  evidence
  confidence
```

字段解释：

| 字段 | 含义 | 例子 |
| --- | --- | --- |
| `subject` | 这条边服务哪批车 | 去修库/轮的主目标群 |
| `edge_key` | 这条边的业务身份 | `REPAIR_MAIN_VIA_CUN4` |
| `from_area` | 从哪里来 | 存2、存5、存3、修库侧 |
| `via_port` | 经过哪个关键口 | 存4、机、修库入口 |
| `to_receiver` | 要去哪 | 修库、轮、油、库 |
| `status` | 这条边推进到哪一步 | `PORT_READY`、`DIGESTING` |
| `contract` | 这条边当前必须履行的业务合同 | `PORT_READY_REPAIR_CONTRACT` |
| `blockers` | 谁挡着这条边 | 占住存4口的车、轮线 blocker |
| `obligations` | 这条边还欠什么 | 必须大释放、必须机接、必须摘解 |
| `protections` | 哪些结构不能破坏 | 机接后主列、已成形释放口 |
| `evidence` | 证据来源 | 显式信号、后续反推、矛盾信号 |
| `confidence` | 置信度 | 高、中、低 |

### 4.0.1 FlowEdge 的三面

为了避免字段越堆越多，`FlowEdge` 应该按三面理解：

```text
FlowEdge:
  identity   # 这是谁
  state      # 到哪一步
  contract   # 当前必须守什么
```

对应关系：

| 面 | 字段 | 回答的问题 |
| --- | --- | --- |
| `identity` | `edge_key / subject / from_area / via_port / to_receiver` | 这条边是谁，服务哪批车 |
| `state` | `status / confidence / evidence` | 这条边推进到哪一步，证据够不够 |
| `contract` | `must_progress / must_not_break / must_finish_before_done / allowed_shortcuts / forbidden_moves` | 下一步什么算履约，什么算违约 |

这样就不会把 `obligations`、`protections`、`blockers` 当成一堆散字段。

它们最终都服务一个问题：

```text
候选动作是否履行当前 EdgeContract？
```

### 4.1.1 edge_key：防止 subject 漂移

只用 `subject.vehicles` 不够。

原因很简单：

```text
人工的主流经常不是一开始就完整出现。
车会从存2、存5、存3逐步组织，也会在轮、修库、油之间穿插。
```

如果 edge 只靠当前车辆集合识别，就会出现两个问题：

- 前面还没集齐时，算法认为主边不存在。
- 中间摘解后车辆集合变化，算法误以为换了一条边。

所以每条边必须有一个稳定业务身份：

```text
EdgeKey:
  intent_family
  primary_receiver
  preferred_port
  source_scope
```

典型取值：

```text
REPAIR_MAIN_VIA_CUN4:
  intent_family = REPAIR_MAIN
  primary_receiver = DEPOT
  preferred_port = CUN4
  source_scope = OUTER_YARD

REPAIR_DIRECT_ENTRY:
  intent_family = REPAIR_MAIN
  primary_receiver = DEPOT
  preferred_port = NO_EXPLICIT_PORT
  source_scope = REPAIR_SIDE

DEPOT_DIGEST_ONLY:
  intent_family = REPAIR_DIGEST
  primary_receiver = DEPOT
  preferred_port = ALREADY_CROSSED
  source_scope = DEPOT_SIDE

WHEEL_INTERLEAVED_SUPPORT:
  intent_family = WHEEL_SUPPORT
  primary_receiver = WHEEL
  preferred_port = CUN4_OR_REPAIR_SIDE
  source_scope = MIXED
```

`edge_key` 的作用不是增加一层分类，而是解决一个核心问题：

```text
同一条业务边在不同阶段，车辆集合可以变化，但身份不能随便变化。
```

这能解释人工案例：

- `0117Z` 从存2/存5/存3组织，到存4释放，到机接，再到修库摘解，都是 `REPAIR_MAIN_VIA_CUN4`。
- `0310W` 前段信号弱，但后面存4摘和机接完整，所以不是新开外场边，而是 `REPAIR_MAIN_VIA_CUN4` 已经来到 `PORT_READY`。
- `0103W / 0223W` 没有标准机接，但修库摘解和库回明确，所以是 `DEPOT_DIGEST_ONLY`，不能当尾项。

### 4.1.2 FlowEdge 的最小 JSON 形态

研究阶段可以先落成这个结构：

```json
{
  "edge_id": "edge_001",
  "edge_key": "REPAIR_MAIN_VIA_CUN4",
  "subject": {
    "vehicles": ["v1", "v2"],
    "role": "PRIMARY",
    "continuity": 0.72,
    "dispersion": 0.28
  },
  "from_area": ["存2", "存5", "存3"],
  "via_port": {
    "name": "存4",
    "role": "RELEASE_PORT",
    "health": "IMPROVING"
  },
  "to_receiver": {
    "name": "修库",
    "receiver_type": "DEPOT",
    "readiness": "ACCEPTING",
    "digest_state": "NOT_STARTED"
  },
  "status": "APPROACHING_PORT",
  "contract": {
    "contract_key": "APPROACHING_CUN4_REPAIR_CONTRACT",
    "must_progress": ["FORM_RELEASE_GROUP"],
    "must_not_break": ["KEEP_CUN4_PORT_USABLE"],
    "must_finish_before_done": ["MACHINE_ACCEPT", "DEPOT_DETACH", "TAIL_PURIFY"],
    "allowed_shortcuts": [],
    "forbidden_moves": ["DIRTY_RELEASE_PORT"],
    "evidence_level": "HIGH"
  },
  "blockers": [],
  "obligations": ["FORM_RELEASE_GROUP", "MACHINE_ACCEPT", "DEPOT_DETACH"],
  "protections": [],
  "evidence": {
    "explicit_signals": ["存4 - 北头"],
    "inferred_signals": [],
    "contradictions": []
  },
  "confidence": "HIGH"
}
```

这里故意不放复杂对象引用。

实现上可以是 dataclass，但输出必须能压成这种扁平 JSON，方便调试和压测。

### 4.2 subject

```text
FlowSubject:
  vehicles
  role
  continuity
  dispersion
  confidence
  membership_reason
```

`role`：

```text
PRIMARY          # 当前主边
SECONDARY        # 次要边
TAIL_CANDIDATE   # 可能尾项，但未确认
```

注意：

```text
subject 不要求一开始就是连续车组。
人工里的主流经常是逐步组织出来的。
```

subject 的识别不要追求一步精确到全车辆。

更稳的做法是分三档：

| 档位 | 含义 | 可以做什么 | 不能做什么 |
| --- | --- | --- | --- |
| `SEED` | 只看到主流种子 | 低风险靠位、清 blocker | 不能主动机接 |
| `FORMING` | 主流正在成形 | 集货、靠存4、准备释放 | 不能锁死保护 |
| `BOUND` | 主流已被释放或机接绑定 | 修库摘解、轮/油处理 | 不能回外场重编 |

人工案例里的意义：

- `0117Z` 早期是 `SEED -> FORMING`，到 `存4 - 16 北头摘 / 机 + 18 接` 以后变成 `BOUND`。
- `0310W` 进入求解时可以已经是 `FORMING` 或 `BOUND`，所以不需要补前段。
- `0130Z / 0201W / 0306W` 有后续信号但前段缺口，最多先给 `FORMING + medium confidence`，不能直接造高置信机接。

### 4.3 via_port

```text
FlowPort:
  name
  role
  health
```

`role`：

```text
APPROACH_PORT    # 正在靠近的关键口
RELEASE_PORT     # 已具备释放作用
ACCEPT_PORT      # 承接口
NO_EXPLICIT_PORT # 后段露出，口已跨过或未显式记录
```

`health`：

```text
CLEAR
BLOCKED
DIRTY
IMPROVING
WORSENING
```

### 4.4 to_receiver

```text
FlowReceiver:
  name
  receiver_type
  readiness
  digest_state
```

`receiver_type`：

```text
DEPOT
WHEEL
JI
OIL
YARD
TAIL
```

`digest_state`：

```text
NOT_STARTED
ACCEPTING
DIGESTING
DIGESTED
```

---

## 5. FlowEdge status

`status` 是 FlowEdge 的最重要字段。

```text
FlowEdgeStatus:
  DISCOVERED
  APPROACHING_PORT
  PORT_READY
  ACCEPTED
  DIGESTING
  DONE
```

含义：

| status | 说人话 | 允许的主要推进 |
| --- | --- | --- |
| `DISCOVERED` | 发现这条边存在，但主流还不清 | 补证据、低风险组织 |
| `APPROACHING_PORT` | 主流正在靠近关键口 | 靠位、集货、清 blocker |
| `PORT_READY` | 关键口已经具备释放/承接条件 | 大释放、机接、入修库 |
| `ACCEPTED` | 已被机/修库等承接 | 锁定主列，禁止大回退 |
| `DIGESTING` | 正在修库/轮摘解消化 | 连续摘解、减少债务 |
| `DONE` | 主边完成 | 允许进入尾项 |

状态推进的基本顺序：

```text
DISCOVERED
  -> APPROACHING_PORT
  -> PORT_READY
  -> ACCEPTED
  -> DIGESTING
  -> DONE
```

但不是所有 case 都从头开始。

例如：

- `0310W` 可以直接从 `PORT_READY` 开始。
- `0104W / 0213W` 可以从 `APPROACHING_PORT` 或 `DIGESTING` 附近开始。
- `0103W / 0223W` 可以直接从 `DIGESTING` 开始。

### 5.1 状态进入条件

状态不能只靠“看起来像”。

更不能依赖人工工单里的未来动作。

在线求解时只能使用：

```text
StartStatus:
  track ordered vehicles
  vehicle position
  target track / target area
  repair process
  flags
  locomotive position
```

不能使用：

```text
人工动作序列里的 存4-北头 / 存4摘 / 机+接 / 库回
```

这些人工动作只能用于：

```text
offline labeling
truth validation
error diagnosis
```

不能用于在线 status 判定。

每个状态都必须有在线进入条件：

| 目标状态 | 在线几何/资源证据 | 人工动作信号用途 | 允许的置信度 |
| --- | --- | --- | --- |
| `DISCOVERED` | 车辆目标/修程显示存在某个合同族，但还未形成可推进结构 | 离线验证是否漏识别 | low/medium/high |
| `APPROACHING_PORT` | subject 与目标 port/receiver 的距离、方向、可达路径、前后阻挡关系显示正在靠近 | 离线验证靠口动作是否合理 | medium/high |
| `PORT_READY` | port 资源可申请，subject 连续性达标，接收端/释放端容量可用，关键 blocker 可清除或已清除 | 离线验证存4摘是否发生 | medium/high |
| `ACCEPT_READY` | 不是已接，而是具备承接候选条件：机车位置、端别、路径、接收端容量、合同条款均允许 | 离线验证机接前一刻是否正确 | medium/high |
| `ACCEPTED` | 在线状态里已经存在被承接后的物理事实；不能用未来机接动作推断 | 离线验证机接动作结果 | high |
| `DIGESTING` | 车辆已经位于接收域内，且仍有未完成 contract clauses | 离线验证修库摘解动作 | medium/high |
| `DONE` | 所有 must_finish_before_done 已满足，且资源/尾项检查通过 | 离线验证库回/收束动作 | high |

两个关键限制：

```text
low confidence 不能进入 ACCEPTED。
DONE 必须检查 obligations，不允许只看库回。
人工动作信号不能作为在线进入条件。
```

这就是为了防止：

- 半成品机接。
- 修库还在摘解时提前收尾。

### 5.2 状态跃迁守卫

允许跳过前置状态，但不允许无证据乱跳。

```text
DISCOVERED -> APPROACHING_PORT:
  subject 有合同归属
  且几何方向 / target receiver / route resource 变清楚

APPROACHING_PORT -> PORT_READY:
  target port resource 可申请
  subject continuity 达标
  receiver/buffer capacity 可用
  blockers 不构成硬阻断

PORT_READY -> ACCEPT_READY:
  locomotive position 可达
  accessible_end 正确
  gate/resource slot 可申请
  MACHINE_ACCEPT 不触发 HALF_READY_MACHINE_ACCEPT

ACCEPT_READY -> ACCEPTED:
  move 已执行后，现场物理状态确认被承接

PORT_READY -> ACCEPTED:
  只允许在 move 已执行后的 state update 中发生
  receiver.readiness >= ACCEPTING
  且 MACHINE_ACCEPT 不触发 HALF_READY_MACHINE_ACCEPT

ACCEPTED -> DIGESTING:
  KEEP_ACCEPTED_TRAIN_INTACT 已生效
  且 DEPOT_DETACH 或 WHEEL_INTERLEAVE_RESOLVE 开始减少

DIGESTING -> DONE:
  DEPOT_DETACH 完成
  WHEEL_INTERLEAVE_RESOLVE 完成或被转成独立 support edge
  TAIL_PURIFY 完成
```

允许的直接初始化：

```text
late_cun4_chain:
  init status = PORT_READY

direct_repair_entry:
  init status = APPROACHING_PORT / DIGESTING

depot_ops_without_machine_accept:
  init status = DIGESTING
```

注意：

```text
直接初始化只能来自 StartStatus 的物理状态。
不能来自人工后续动作。
```

禁止的状态倒退：

```text
ACCEPTED -> PORT_READY:
  除非证明前一个 ACCEPTED 是误识别。

DIGESTING -> APPROACHING_PORT:
  除非当前 edge 被判定为错误归边。
```

### 5.3 状态不是阶段

这里要特别说清楚。

`FlowEdgeStatus` 不是传统 workflow 阶段。

区别是：

| workflow 阶段 | FlowEdge status |
| --- | --- |
| 全局只能按阶段走 | 每条 edge 自己有 status |
| 先决定现在属于哪个阶段 | 先识别哪条边正在推进 |
| 阶段内再找动作 | 候选动作先解释为 ContractDelta |
| 异常 case 容易掉到 fallback | 异常 case 可以从中间状态初始化 |

所以它不会把问题绕回原来的 workflow。

原来的 workflow 问的是：

```text
现在该执行哪个阶段？
```

这里问的是：

```text
当前最有价值的合同是哪条？
这个动作是在履约还是违约？
```

---

## 6. blockers / obligations / protections

### 6.1 blockers

```text
BlockerOnEdge:
  blocker
  blocks_what
  severity
```

`blocks_what`：

```text
SUBJECT_CONTINUITY
PORT_HEALTH
RECEIVER_READINESS
DIGEST_PROGRESS
TAIL_PURITY
```

例子：

```text
无关车占住存4口:
  blocks_what = PORT_HEALTH

轮线夹在修库主流里:
  blocks_what = DIGEST_PROGRESS

半成品主列挡机接:
  blocks_what = RECEIVER_READINESS
```

### 6.2 obligations

`obligations` 是这条边必须完成的事项。

```text
EdgeObligation:
  obligation_id
  obligation_type
  must_finish_before_status
  severity
  owner_edge_id
  state
  evidence
```

类型：

```text
CLEAR_PORT
FORM_RELEASE_GROUP
MACHINE_ACCEPT
DEPOT_DETACH
WHEEL_INTERLEAVE_RESOLVE
TAIL_PURIFY
```

例子：

```text
status = PORT_READY
obligation = MACHINE_ACCEPT

status = DIGESTING
obligation = DEPOT_DETACH
```

obligation 必须有生命周期：

```text
OPEN
  -> REDUCING
  -> SATISFIED
  -> TRANSFERRED
  -> CANCELLED_AS_NOISE
```

生命周期规则：

- `OPEN`：已经确认欠这个动作，但还没处理。
- `REDUCING`：候选动作正在减少它。
- `SATISFIED`：完成，不再参与评分。
- `TRANSFERRED`：从主边转成 support edge，例如轮线交织从主修库边剥离出来。
- `CANCELLED_AS_NOISE`：证明之前是误识别，必须保留 evidence。

不允许出现没有 owner 的 obligation。

否则它会变成全局债务池，最后又绕回一堆补丁。

### 6.3 protections

`protections` 是不能破坏的结构。

```text
EdgeProtection:
  protection_id
  protection_type
  active_from_status
  break_violation
  owner_edge_id
  state
```

类型：

```text
KEEP_RELEASE_PORT_CLEAN
KEEP_ACCEPTED_TRAIN_INTACT
KEEP_DEPOT_SEQUENCE
KEEP_TAIL_PURE
```

例子：

```text
status >= ACCEPTED:
  KEEP_ACCEPTED_TRAIN_INTACT
```

protection 也必须有生命周期：

```text
PENDING
  -> ACTIVE
  -> RELEASED
```

规则：

- `PENDING`：结构还没成形，不能提前锁死。
- `ACTIVE`：结构已成形，破坏就是硬违反。
- `RELEASED`：对应 edge 已 DONE，保护解除或转入 tail purity。

人工意义：

- `存4` 还没大释放时，不能把所有存4相关动作都锁死。
- `机 + 接` 后，主列不能为了局部少一钩被拆散。
- `库 回` 前，不能把仍在修库摘解的车当尾项处理。

### 6.4 blockers / obligations / protections 的关系

三者不要混用：

| 类型 | 问题 | 例子 | 动作目标 |
| --- | --- | --- | --- |
| `blocker` | 谁挡住了推进 | 无关车占存4口 | 移开或绕开 |
| `obligation` | 这条边欠什么 | 还没机接、还没摘解 | 完成或转移 |
| `protection` | 什么不能破坏 | 机接后的大列 | 禁止破坏 |

一条候选动作必须能说清楚：

```text
减少了哪个 blocker？
减少了哪个 obligation？
有没有破坏 protection？
```

如果说不清楚，只能算普通动作，不能作为主边推进动作。

---

## 7. EdgeDelta

`EdgeDelta` 是候选动作造成的边变化。

```text
EdgeDelta:
  edge_id
  move_or_planlet
  status_change
  subject_change
  port_change
  receiver_change
  blocker_change
  obligation_change
  protection_change
  hook_count
  violations
  score
```

### 7.1 好变化

```text
status 往前推进
subject continuity 上升
subject dispersion 下降
port health 变好
receiver readiness 上升
blocker 减少
obligation 减少
protection 没被破坏
```

### 7.2 坏变化

```text
status 倒退
subject 被打散
port health 变差
receiver readiness 下降
blocker 增加
obligation 增加
protection 被破坏
```

### 7.3 硬违反

```text
EdgeViolation:
  HALF_READY_MACHINE_ACCEPT
  CLOSEOUT_BEFORE_EDGE_DONE
  BREAK_ACCEPTED_TRAIN
  DIRTY_RELEASE_PORT
  MISCLASSIFY_DIGESTING_EDGE_AS_TAIL
  FORCE_OUTER_PICKUP_ON_PORT_READY_EDGE
```

硬违反不能被勾数抵消。

### 7.4 EdgeDelta 的解释粒度

`EdgeDelta` 不应该只评价单钩，也不应该直接膨胀成大搜索计划。

推荐粒度是：

```text
1 个 move
或
1 个短 planlet
```

`planlet` 的边界：

- 能在 1 到 3 勾内完成一个明确结构变化。
- 必须只服务一个主目标 edge。
- 可以顺带改善 support edge，但不能牺牲主 edge。
- 不能跨过 `ACCEPTED` / `DONE` 这类硬边界。

例子：

```text
靠存4:
  planlet = 把主流从存2/存5/存3组织到存4北头附近
  目标 = DISCOVERED -> APPROACHING_PORT

存4大释放:
  planlet = 存4 - 14/15/16 北头摘
  目标 = APPROACHING_PORT -> PORT_READY

机接:
  planlet = 机 + n 接
  目标 = PORT_READY -> ACCEPTED

库内连续摘解:
  planlet = 修x - n / 库外n / 摘
  目标 = ACCEPTED -> DIGESTING 或 DIGESTING obligation reduction
```

这样做的原因：

```text
单钩太短，看不出结构价值。
长计划太大，又回到搜索。
短 planlet 正好承接人工的“一个结构意图”。
```

### 7.5 EdgeDelta 必须输出反事实

每个候选不只输出分数，还要输出：

```text
why_accept
why_reject
counterfactual_risk
```

例子：

```text
候选 A: 存4大释放
why_accept:
  status: APPROACHING_PORT -> PORT_READY
  obligation: FORM_RELEASE_GROUP 减少
  blocker: PORT_HEALTH 改善

候选 B: 回外场补车
why_reject:
  status 无推进
  new_obligation = OUTER_PICKUP
  subject dispersion 上升
counterfactual_risk:
  对 0310W 这类 PORT_READY case 会明显返工
```

如果一个动作只是“评分高”，但说不清它让哪条边前进，就不能进入主链。

---

## 8. EdgeContract

`EdgeContract` 是这份设计最重要的升级。

它不是新增一个顶层系统，而是 `FlowEdge` 的裁决核心。

说人话：

```text
FlowEdge 说明“这条边是谁”。
EdgeContract 说明“这条边现在承诺了什么”。
ContractDelta 说明“这个动作是在履约还是违约”。
```

### 8.1 EdgeContract 结构

```text
EdgeContract:
  contract_id
  contract_key
  owner_edge_id
  status_scope
  must_progress
  must_not_break
  must_finish_before_done
  allowed_shortcuts
  forbidden_moves
  evidence_level
  contradiction_policy
  expiry_condition
```

字段解释：

| 字段 | 含义 | 例子 |
| --- | --- | --- |
| `contract_key` | 合同模板 | `PORT_READY_REPAIR_CONTRACT` |
| `status_scope` | 合同在哪些 status 生效 | `PORT_READY -> ACCEPTED` |
| `must_progress` | 当前优先推进什么 | `MACHINE_ACCEPT` |
| `must_not_break` | 当前不能破坏什么 | `KEEP_RELEASE_PORT_CLEAN` |
| `must_finish_before_done` | 到 `DONE` 前必须完成什么 | `DEPOT_DETACH` |
| `allowed_shortcuts` | 允许跳过什么前置 | late cun4 可跳过外场补链 |
| `forbidden_moves` | 当前硬禁止动作族 | `OUTER_MAIN_PICKUP` |
| `evidence_level` | 合同证据等级 | high/medium/low |
| `contradiction_policy` | 有矛盾时怎么保守处理 | 只允许低风险组织 |
| `expiry_condition` | 合同什么时候结束或切换 | `MACHINE_ACCEPT satisfied` |

### 8.2 合同和 obligation/protection 的关系

`obligations` 和 `protections` 不再是散字段。

它们是合同条款的两种形态：

```text
obligation = 必须履行的条款
protection = 不能破坏的条款
```

对应关系：

| 原字段 | 合同字段 | 含义 |
| --- | --- | --- |
| `obligations` | `must_progress / must_finish_before_done` | 欠什么 |
| `protections` | `must_not_break` | 不能破坏什么 |
| `blockers` | 阻碍合同履行的对象 | 谁挡住合同履约 |
| `violations` | `forbidden_moves` 命中的结果 | 什么动作违约 |

这样能避免一个问题：

```text
字段都在，但不知道谁说了算。
```

合同就是裁决中心。

### 8.3 全站合同模板

合同模板不能只来自修库主链 family。

真实现场里，修库主链只是全站流向之一。

所以合同模板分两层：

```text
第一层：StationFlowContract
  覆盖所有主体车流。

第二层：RepairInboundVariant
  只覆盖入修库主链内部变体。
```

这能解决一个关键问题：

```text
不是所有车都要进修库。
但所有主体车都必须有合同。
```

#### 8.3.1 第一层：全站流向合同族

```text
ContractTemplate:
  REPAIR_INBOUND
  DEPOT_OUTBOUND
  PRE_REPAIR_STAGING
  DISPATCH_SHED_QUEUE
  YARD_REBALANCE
  FUNCTION_LINE_SERVICE
  LOCO_AREA_STAGING
  SPECIAL_REPAIR_PROCESS
  TAIL_CLOSEOUT
```

| 合同族 | 服务对象 | 典型目的地 | 核心任务 | 不应该掉 residual |
| --- | --- | --- | --- | --- |
| `REPAIR_INBOUND` | 入修库主链 | 修1-4、轮、机 | 组织、释放、机接、摘解 | 是 |
| `DEPOT_OUTBOUND` | 修库出库减压 | 存4北 | 从修1-4拉出，腾大库 | 是 |
| `PRE_REPAIR_STAGING` | 段修前暂存 | 预修 | 预修编组、等待后续入库 | 是 |
| `DISPATCH_SHED_QUEUE` | 调棚作业队列 | 调棚 | 排队、避免堵主线 | 是 |
| `YARD_REBALANCE` | 存车线整理 | 存1/2/3/5 | 腾位、归并、重排 | 是 |
| `FUNCTION_LINE_SERVICE` | 功能线作业 | 油/洗/抛 | 功能线进出和服务完成 | 是 |
| `LOCO_AREA_STAGING` | 机区缓冲 | 机库/机北3/机棚 | 入库骨架到机区缓冲线 | 是 |
| `SPECIAL_REPAIR_PROCESS` | 特殊修程 | 拉走/称重/留/轮对架/修x库外 | 保留特殊工艺语义 | 是 |
| `TAIL_CLOSEOUT` | 真尾项 | 库/机/边角线 | 主合同完成后的收束 | 只有确认后才是 |

#### REPAIR_INBOUND

这是原文档已经重点设计的修库主链合同族。

它下面再分变体：

```text
RepairInboundVariant:
  FULL_CHAIN_REPAIR
  LATE_CUN4_REPAIR
  DIRECT_REPAIR_ENTRY
  MIXED_SIGNAL_REPAIR
  DEPOT_DIGEST_ONLY
  WHEEL_INTERLEAVED_SUPPORT
```

#### DEPOT_OUTBOUND

适用：

```text
修1-4 出库拉到存4北
腾出修库能力
每案可能 12~20 辆
```

合同起点：

```text
DEPOT_OUTBOUND_CONTRACT
```

关键条款：

```text
must_progress:
  FREE_DEPOT_TRACK
  MOVE_TO_CUN4_NORTH
  KEEP_OUTBOUND_GROUP_CONTIGUOUS

must_not_break:
  DO_NOT_BLOCK_REPAIR_INBOUND_PORT
  DO_NOT_MIX_WITH_ACTIVE_REPAIR_INBOUND
  KEEP_CUN4_NORTH_RETRIEVABLE

forbidden_moves:
  TREAT_AS_REPAIR_INBOUND
  PARK_RANDOMLY_IN_CUN4
  BREAK_DEPOT_OUTBOUND_GROUP
```

人工含义：

```text
这批车不是要进大库。
它们经常是从修库出来，去存4北，目的是腾大库。
方向与 REPAIR_INBOUND 相反，不能套入库释放合同。
```

#### PRE_REPAIR_STAGING

适用：

```text
预修暂存编组
每案可能约 11 辆
```

合同起点：

```text
PRE_REPAIR_STAGING_CONTRACT
```

关键条款：

```text
must_progress:
  GROUP_PRE_REPAIR_CARS
  PLACE_ON_PRE_REPAIR_BUFFER
  KEEP_NEXT_REPAIR_ENTRY_AVAILABLE

must_not_break:
  DO_NOT_SCATTER_PRE_REPAIR_GROUP
  DO_NOT_OCCUPY_RELEASE_PORT_AS_STORAGE
  DO_NOT_BLOCK_DEPOT_OUTBOUND

forbidden_moves:
  FORCE_MACHINE_ACCEPT
  MIX_WITH_TAIL_CLOSEOUT
  RECLASSIFY_AS_NOISE
```

人工含义：

```text
预修车不是尾项，也不是当前修库主链。
它们是下一轮入修库的候选库存，需要稳定暂存。
```

#### DISPATCH_SHED_QUEUE

适用：

```text
调棚作业排队
每案可能 8~11 辆
```

合同起点：

```text
DISPATCH_SHED_QUEUE_CONTRACT
```

关键条款：

```text
must_progress:
  FORM_DISPATCH_QUEUE
  KEEP_SHED_ACCESS_CLEAR
  AVOID_CONFLICT_WITH_MAIN_FLOW

must_not_break:
  DO_NOT_PULL_BACK_INTO_REPAIR_MAIN
  DO_NOT_BLOCK_CUN4_RELEASE
  KEEP_QUEUE_ORDER_IF_REQUIRED

forbidden_moves:
  TREAT_AS_REPAIR_DETACH
  CLOSEOUT_BEFORE_SHED_READY
```

人工含义：

```text
调棚是独立作业队列。
它不是修库主链的 blocker，也不是 residual。
```

#### YARD_REBALANCE

适用：

```text
存1/存2/存3/存5 的整理、腾位、归并
各线每案可能 6~10 辆
```

合同起点：

```text
YARD_REBALANCE_CONTRACT
```

关键条款：

```text
must_progress:
  FREE_REQUIRED_YARD_CAPACITY
  CONSOLIDATE_COMPATIBLE_GROUPS
  PRESERVE_RETRIEVAL_ORDER

must_not_break:
  DO_NOT_OCCUPY_CUN4_AS_RANDOM_STORAGE
  DO_NOT_SPLIT_BOUND_GROUP
  DO_NOT_CREATE_PORT_BLOCKER

forbidden_moves:
  RANDOM_YARD_SHUFFLE
  STEAL_CAPACITY_FROM_ACTIVE_CONTRACT
  HIDE_ACTIVE_VEHICLE_BEHIND_HOLD
```

人工含义：

```text
存车整理不是低级杂活。
它决定后续有没有空间、有没有顺序、会不会挡住主合同。
```

#### FUNCTION_LINE_SERVICE

适用：

```text
油 / 洗 / 抛 等功能线作业
```

合同起点：

```text
FUNCTION_LINE_SERVICE_CONTRACT
```

关键条款：

```text
must_progress:
  MOVE_TO_FUNCTION_LINE
  COMPLETE_SERVICE_OR_EXIT_SERVICE_LINE
  RETURN_TO_VALID_RECEIVER

must_not_break:
  DO_NOT_MIX_FUNCTION_DONE_WITH_PENDING
  DO_NOT_BLOCK_REPAIR_OR_OUTBOUND_PORT
  KEEP_FUNCTION_LINE_ACCESSIBLE

forbidden_moves:
  TREAT_AS_TAIL_BEFORE_SERVICE_DONE
  PULL_INTO_REPAIR_MAIN_WITHOUT_REASON
```

人工含义：

```text
油/洗/抛 不是修库主链尾巴的一句话。
它们有自己的服务完成条件。
```

#### LOCO_AREA_STAGING

适用：

```text
机库 / 机北3 / 机棚 等机区缓冲
```

合同起点：

```text
LOCO_AREA_STAGING_CONTRACT
```

关键条款：

```text
must_progress:
  STAGE_TO_LOCO_AREA
  KEEP_LOCO_ACCESS_CLEAR
  PRESERVE_ENTRY_SKELETON

must_not_break:
  DO_NOT_MIX_WITH_RANDOM_YARD
  DO_NOT_BLOCK_LINK6_LINK7_GATE
  DO_NOT_CREATE_DIRTY_LOCO_CARRY

forbidden_moves:
  TREAT_AS_TAIL_CLOSEOUT
  FORCE_REPAIR_INBOUND
  HIDE_IN_RESIDUAL
```

人工含义：

```text
机区缓冲不是普通存车整理。
它经常承接入库骨架到机区缓冲线的中间状态。
```

#### SPECIAL_REPAIR_PROCESS

适用：

```text
拉走 / 称重 / 留 / 轮对架 / 修x库外
```

合同起点：

```text
SPECIAL_REPAIR_PROCESS_CONTRACT
```

关键条款：

```text
must_progress:
  SATISFY_REPAIR_PROCESS
  PRESERVE_PROCESS_CONSTRAINT

must_not_break:
  DO_NOT_TREAT_AS_GENERIC_YARD
  DO_NOT_HIDE_SPECIAL_PROCESS_IN_RESIDUAL
  DO_NOT_OVERRIDE_PROCESS_BY_TARGET_TRACK_ONLY

forbidden_moves:
  GENERIC_REBALANCE_FOR_SPECIAL_PROCESS
  CLOSEOUT_BEFORE_PROCESS_SATISFIED
```

人工含义：

```text
特殊修程的语义强于目标线粗分类。
例如“拉走”不是普通归并，“留”可能根本不该动。
```

#### TAIL_CLOSEOUT

适用：

```text
所有主合同完成后，剩余少量尾项收束
```

合同起点：

```text
TAIL_CLOSEOUT_CONTRACT
```

关键条款：

```text
must_progress:
  PURIFY_TAIL
  RETURN_LOCOMOTIVE_OR_CLEAR_ENGINE
  CLOSE_REMAINING_MINOR_ITEMS

must_not_break:
  DO_NOT_CLOSE_ACTIVE_CONTRACT
  DO_NOT_HIDE_UNFINISHED_SERVICE
  DO_NOT_MISCLASSIFY_DIGESTING_AS_TAIL

forbidden_moves:
  CLOSEOUT_BEFORE_ALL_PRIMARY_CONTRACTS_DONE
  SWALLOW_RESIDUAL_AS_TAIL_WITHOUT_EVIDENCE
```

人工含义：

```text
尾项只能是主合同完成后的少量剩余。
不能把未建模的 70% 车辆叫尾项。
```

#### 8.3.2 第二层：REPAIR_INBOUND 内部变体

以下模板只用于 `REPAIR_INBOUND` 合同族内部。

它们不能覆盖全站车辆。

#### FULL_CHAIN_REPAIR

适用：

```text
0117Z, 0115W, 0128W, 0303W
```

合同序列：

```text
APPROACHING_CUN4_REPAIR_CONTRACT
  -> PORT_READY_REPAIR_CONTRACT
  -> MACHINE_ACCEPTED_REPAIR_CONTRACT
  -> DEPOT_DIGEST_CONTRACT
  -> CLOSEOUT_CONTRACT
```

关键条款：

```text
must_progress:
  FORM_RELEASE_GROUP
  MACHINE_ACCEPT
  DEPOT_DETACH
  TAIL_PURIFY

must_not_break:
  KEEP_CUN4_PORT_USABLE
  KEEP_RELEASE_PORT_CLEAN
  KEEP_ACCEPTED_TRAIN_INTACT

forbidden_moves:
  DIRTY_RELEASE_PORT
  BREAK_ACCEPTED_TRAIN
  CLOSEOUT_BEFORE_EDGE_DONE
```

人工含义：

```text
0117Z 不是一堆动作，而是一串合同切换：
先把存4口做成可释放口，再机接锁定，再修库摘解，再收束。
```

#### LATE_CUN4_REPAIR

适用：

```text
0310W, 0302W, 0331W, 0105W
```

合同起点：

```text
PORT_READY_REPAIR_CONTRACT
```

关键条款：

```text
allowed_shortcuts:
  SKIP_OUTER_MAIN_PICKUP
  SKIP_APPROACHING_CUN4_IF_RELEASE_SIGNAL_EXISTS

must_progress:
  MACHINE_ACCEPT
  DEPOT_DETACH

forbidden_moves:
  OUTER_MAIN_PICKUP
  REBUILD_DISCOVERED_CHAIN
```

人工含义：

```text
0310W 已经露出存4大摘和机接窗口。
算法再回外场补前段，就是违约，不是保守。
```

#### DIRECT_REPAIR_ENTRY

适用：

```text
0104W, 0213W, 0123W, 0313W
```

合同起点：

```text
DIRECT_DEPOT_ENTRY_CONTRACT
```

关键条款：

```text
must_progress:
  DEPOT_ENTRY_STABILIZE
  DEPOT_DETACH
  TAIL_PURIFY

allowed_shortcuts:
  NO_CUN4_RELEASE_REQUIRED
  NO_OUTER_CHAIN_REQUIRED

forbidden_moves:
  FORCE_CUN4_MAIN_CHAIN
  ADD_OUTER_PICKUP_OBLIGATION
```

人工含义：

```text
0213W 这类 case 不是标准主链失败。
它本来就是近库侧直接处理。
```

#### MIXED_SIGNAL_REPAIR

适用：

```text
0130Z, 0201W, 0306W, 0311W
```

合同起点：

```text
LOW_CONFIDENCE_REPAIR_CONTRACT
```

关键条款：

```text
must_progress:
  EVIDENCE_STABILIZE
  SAFE_POSITION_IMPROVEMENT

must_not_break:
  DO_NOT_FORCE_ACCEPT
  DO_NOT_DESTROY_POSSIBLE_MAIN

forbidden_moves:
  HALF_READY_MACHINE_ACCEPT
  LOW_CONFIDENCE_ACCEPTED_CROSSING
```

人工含义：

```text
信号缺口不是让算法乱猜。
可以承认后续反推，但不能低置信主动跨机接硬边界。
```

#### DEPOT_DIGEST_ONLY

适用：

```text
0103W, 0223W, 0308W, 0329W
```

合同起点：

```text
DEPOT_DIGEST_CONTRACT
```

关键条款：

```text
must_progress:
  DEPOT_DETACH
  WHEEL_INTERLEAVE_RESOLVE
  TAIL_PURIFY

allowed_shortcuts:
  NO_MACHINE_ACCEPT_REQUIRED
  ALREADY_CROSSED_PORT

forbidden_moves:
  MISCLASSIFY_DIGESTING_EDGE_AS_TAIL
  CLOSEOUT_BEFORE_EDGE_DONE
```

人工含义：

```text
没有机接，不等于没有主任务。
只要修库摘解层存在，就必须先履行消化合同。
```

#### WHEEL_INTERLEAVED_SUPPORT

适用：

```text
0117Z, 0115Z, 0113Z
```

合同起点：

```text
WHEEL_SUPPORT_CONTRACT
```

关键条款：

```text
must_progress:
  REDUCE_WHEEL_INTERLEAVE
  PROTECT_REPAIR_MAIN

must_not_break:
  PRIMARY_REPAIR_CONTRACT

forbidden_moves:
  PROMOTE_SUPPORT_OVER_PRIMARY
  BREAK_REPAIR_MAIN_FOR_WHEEL_ONLY
```

人工含义：

```text
轮是高频交织项，但通常不是压过修库主边的主合同。
```

### 8.4 合同冲突优先级

当多个合同同时存在，不能靠大评分黑箱。

使用固定优先级：

```text
1. 已经 ACTIVE 的 protection 合同
2. 已经进入 DIGESTING 的消化合同
3. PORT_READY 后即将跨 ACCEPTED 的合同
4. 高置信 PRIMARY 合同
5. support 合同
6. residual/tail 合同
```

解释：

```text
越晚形成的硬结构，越不能被前段动作破坏。
```

但有一个例外：

```text
如果 DIGESTING 是低置信误识别，不能压过高置信 PORT_READY 主合同。
```

所以排序时必须先检查：

```text
contract.evidence_level
contract.contradiction_policy
```

### 8.5 合同切换

合同切换必须由条款完成触发，而不是由阶段名称触发。

```text
APPROACHING_CUN4_REPAIR_CONTRACT
  -- FORM_RELEASE_GROUP satisfied -->
PORT_READY_REPAIR_CONTRACT

PORT_READY_REPAIR_CONTRACT
  -- MACHINE_ACCEPT satisfied -->
MACHINE_ACCEPTED_REPAIR_CONTRACT

MACHINE_ACCEPTED_REPAIR_CONTRACT
  -- DEPOT_DETACH started -->
DEPOT_DIGEST_CONTRACT

DEPOT_DIGEST_CONTRACT
  -- DEPOT_DETACH + WHEEL_INTERLEAVE_RESOLVE satisfied -->
CLOSEOUT_CONTRACT

CLOSEOUT_CONTRACT
  -- TAIL_PURIFY satisfied -->
DONE
```

这样能防止：

- 看见 `库 回` 就直接 `DONE`。
- 看见修库动作就过早 `DIGESTING`。
- 看见存4靠口就过早 `PORT_READY`。

### 8.6 合同生成规则

```text
build_contract(edge, evidence):
  if depot_outbound intent visible:
    template = DEPOT_OUTBOUND

  elif pre_repair staging intent visible:
    template = PRE_REPAIR_STAGING

  elif dispatch shed queue intent visible:
    template = DISPATCH_SHED_QUEUE

  elif yard capacity / storage rebalance intent visible:
    template = YARD_REBALANCE

  elif function line service intent visible:
    template = FUNCTION_LINE_SERVICE

  elif repair inbound intent visible:
    template = build_repair_inbound_variant(edge, evidence)

  elif all primary contracts done and only minor tail remains:
    template = TAIL_CLOSEOUT

  else:
    put into residual with strict expiry

  attach status-specific clauses
  attach evidence_level
  attach contradiction_policy
```

`build_repair_inbound_variant`：

```text
build_repair_inbound_variant(edge, evidence):
  if full_chain signals strong:
    return FULL_CHAIN_REPAIR

  elif release + machine_accept visible but pre_cun4 missing:
    return LATE_CUN4_REPAIR

  elif repair-side entry visible and outer chain weak:
    return DIRECT_REPAIR_ENTRY

  elif depot detach visible but machine accept absent:
    return DEPOT_DIGEST_ONLY

  elif main repair signal exists but contradictions exist:
    return MIXED_SIGNAL_REPAIR
```

关键不是把 case family 写死。

而是把人工 family 转成合同模板：

```text
family 只是证据来源。
contract 才是在线求解使用的对象。
```

### 8.7 FlowClassify：先全站归约，再选 TargetContract

在合同生成前，必须先做 `FlowClassify`。

它不是 workflow 阶段，而是车辆归属：

```text
FlowClassify:
  input: all vehicles
  output:
    no_move_vehicles
    contract_groups
    residuals
    conflicts
```

分类顺序：

```text
0. 先过滤不动车
   如果 source track 已经属于 target track set，且不阻塞关键资源：
     标记为 NO_MOVE_SATISFIED。
   不生成 active contract。

1. 读取 RepairProcess / 作业语义
   拉走、称重、留、轮对架等特殊语义优先于目标线粗分类。

2. 显式目标优先
   targetTrack / targetArea / vehicle plan 指向哪里，就先归到对应合同族。

3. 来源方向校验
   从修1-4出来去存4北，更像 DEPOT_OUTBOUND。
   从外场靠存4再机接，更像 REPAIR_INBOUND。

4. 功能线优先独立
   油/洗/抛 有服务语义，不能默认当尾项。

5. 存车整理独立成合同
   存1/2/3/5 的腾位和归并不能掉 residual。

6. 只有证据不足才 residual
```

### 8.7.1 NO_MOVE_SATISFIED

实测里大量车辆已经在目标线集内。

这类车不能为了“覆盖率好看”强行生成合同。

```text
NoMoveVehicle:
  vehicle_id
  source_track
  target_track_set
  satisfied_reason
  blocks_resource
```

规则：

```text
if source_track in target_track_set
and not blocks critical resource
and not required by RepairProcess to move:
  classify as NO_MOVE_SATISFIED
```

不动车不进入：

```text
effective_contract_coverage
active_contract_count
TargetContractSelector
```

但必须进入审计输出：

```text
no_move_vehicle_count
no_move_vehicle_ratio
effective_movable_vehicle_count
```

覆盖率必须分两种：

```text
gross_contract_coverage:
  包含不动车，仅说明车辆都有解释。

effective_contract_coverage:
  只统计需要调动的车，说明求解器真正覆盖了多少工作量。
```

### 8.7.2 RepairProcess 特殊语义

只看目标线会压平特殊修程。

必须读取 `RepairProcess` 或等价作业语义：

| 语义 | 合同解释 | 不能误归类为 |
| --- | --- | --- |
| `拉走` | 离场/移出当前作业域，通常不是保留可取性 | `YARD_REBALANCE` |
| `称重` | 功能/检测服务，需要服务完成条件 | 普通存车整理 |
| `留` | 明确保持不动或延后处理 | active movement contract |
| `轮对架` | 特殊设备/工装相关流向 | 普通修库主链 |
| `修x库外` | 修库内外边界状态，归入入库/出库合同 | residual |

新增合同族：

```text
SPECIAL_REPAIR_PROCESS
```

使用条件：

```text
当 RepairProcess 语义强于目标线语义时，
优先生成 SPECIAL_REPAIR_PROCESS_CONTRACT。
```

关键条款：

```text
must_progress:
  SATISFY_REPAIR_PROCESS
  PRESERVE_PROCESS_CONSTRAINT

must_not_break:
  DO_NOT_TREAT_AS_GENERIC_YARD
  DO_NOT_HIDE_SPECIAL_PROCESS_IN_RESIDUAL

forbidden_moves:
  GENERIC_REBALANCE_FOR_SPECIAL_PROCESS
  CLOSEOUT_BEFORE_PROCESS_SATISFIED
```

输出要求：

```text
每个求解 step:
  effective_contract_coverage >= 90%
  residual_vehicle_ratio <= 10%
```

研究阶段可以更严格：

```text
目标 effective_contract_coverage >= 95%
目标 residual_vehicle_ratio <= 5%
```

如果低于这个覆盖率，不能进入求解优化。

因为那说明：

```text
合同体系还没覆盖现场。
后面的 TargetContract / ContractDelta 都是在局部聪明。
```

---

## 9. ContractDelta

`ContractDelta` 是 `EdgeDelta` 的更硬版本。

`EdgeDelta` 关心边字段怎么变化。

`ContractDelta` 关心合同条款怎么变化：

```text
ContractDelta:
  contract_id
  move_or_planlet
  satisfied_clauses
  reduced_clauses
  added_clauses
  broken_clauses
  transferred_clauses
  next_contract_key
  hook_count
  violation_level
  accept_reason
  reject_reason
```

### 9.1 好的 ContractDelta

```text
satisfied_clauses 非空
或 reduced_clauses 明确减少高优先级合同债务
且 broken_clauses 为空
且没有生成更高优先级合同债务
```

例子：

```text
0310W:
  move = 机 + 14 接
  satisfied_clauses = MACHINE_ACCEPT
  next_contract_key = MACHINE_ACCEPTED_REPAIR_CONTRACT
```

### 9.2 坏的 ContractDelta

```text
broken_clauses 非空
或 added_clauses 增加更大主合同债务
或把 support 合同放到 primary 合同之前
```

例子：

```text
0310W:
  move = 回外场补车
  broken_clauses = SKIP_OUTER_MAIN_PICKUP
  added_clauses = OUTER_PICKUP_OBLIGATION
  violation = FORCE_OUTER_PICKUP_ON_PORT_READY_EDGE
```

### 9.3 合同接受准则

```text
accept(contract_delta):
  reject if broken_clauses contains hard clause
  reject if low confidence crosses ACCEPTED
  reject if support contract damages primary contract
  reject if closeout before must_finish_before_done is empty

  accept if:
    satisfied_clauses contains current must_progress
    or reduced_clauses reduces high severity debt
    or move improves evidence without increasing hard debt

  tie break:
    same contract rank -> fewer hooks
```

这比纯评分更稳。

因为它先判断：

```text
是不是履约。
```

再判断：

```text
履约方式是不是少钩。
```

---

## 10. ContractOptimizer

评分只在无硬违反的候选里进行。

```text
score(contract_delta) =
  + satisfied_clause_gain
  + reduced_clause_gain
  + evidence_stabilize_gain
  + next_contract_gain
  - broken_clause_penalty
  - added_clause_penalty
  - cross_contract_damage_penalty
  - hook_count_penalty
```

关键约束：

```text
不能为了 hook_count 破坏合同。
不能为了局部 blocker 减少增加更大合同债务。
不能为了 tail purity 把 DEPOT_DIGEST_CONTRACT 提前 closeout。
```

### 10.1 TargetContract 选择

`TargetContract` 是这个设计里最危险的隐藏点。

如果它不清楚，整个系统会重新变成：

```text
先猜当前阶段，再靠搜索补救。
```

所以目标合同选择必须用明确规则，而不是一个大评分黑箱。

推荐顺序：

```text
1. 先处理已经跨过硬边界的 edge
   ACCEPTED / DIGESTING 优先于 DISCOVERED。

2. 再处理即将跨硬边界的 edge
   PORT_READY 优先于 APPROACHING_PORT。

3. 再处理高置信主边
   PRIMARY + high confidence 优先于 low confidence。

4. 再处理会减少大 obligation 的 edge
   MACHINE_ACCEPT / DEPOT_DETACH 优先于局部 blocker。

5. 最后才看 hook_count。
```

伪代码：

```text
select_target_contract(flow_graph):
  candidates = active contracts excluding DONE

  reject contract if:
    confidence = low and next step would cross ACCEPTED
    contract has unresolved contradiction and no safe delta

  sort by:
    contract_rank
    hard_clause_urgency
    primary_role
    must_progress_severity
    evidence_level
    estimated_hook_efficiency

  return first contract
```

`status_criticality`：

```text
DEPOT_DIGEST_CONTRACT
  > MACHINE_ACCEPTED_REPAIR_CONTRACT
  > PORT_READY_REPAIR_CONTRACT
  > APPROACHING_CUN4_REPAIR_CONTRACT
  > LOW_CONFIDENCE_REPAIR_CONTRACT
```

这和普通人工阶段顺序不一样。

原因：

```text
越往后的边，越可能已经形成保护和债务。
先乱动前面的事，反而可能破坏后面已经成形的结构。
```

人工案例映射：

- `0117Z` 前段履行 `APPROACHING_CUN4_REPAIR_CONTRACT`，机接后切到更高优先级的消化合同。
- `0310W` 初始就是 `PORT_READY_REPAIR_CONTRACT`，不能退回去补外场合同。
- `0103W / 0223W` 是 `DEPOT_DIGEST_CONTRACT`，所以必须先消化库内债务，不能 closeout。

### 10.2 多合同冲突规则

大多数时候只应该有一条主边。

但实际会出现 support contract，比如轮、油、尾项。

冲突时遵循 4 条规则：

| 规则 | 含义 |
| --- | --- |
| 主合同优先 | support contract 不能破坏 primary contract |
| 后边界优先 | `MACHINE_ACCEPTED / DEPOT_DIGEST` 合同优先于早期组织合同 |
| 同车唯一 | 同一车辆不能同时作为两条 active contract 的 subject 主成员 |
| support 可转移 | 轮/油问题可以从合同条款转成 support contract，但必须有 owner |

典型冲突：

```text
轮线夹在修库主流里
```

不要一开始就新开一条完全平级的轮线边。

先表达为：

```text
primary edge:
  obligation = WHEEL_INTERLEAVE_RESOLVE
```

只有当轮线动作有独立连续推进价值时，才转成：

```text
support contract:
  edge_key = WHEEL_INTERLEAVED_SUPPORT
  owner_edge_id = primary edge
```

这样既承认人工里 `轮` 的高频交织，又不把系统拆成一堆平级流程。

### 10.3 接受准则

候选 contract delta 被接受必须满足：

```text
no hard violation
and target_contract has positive contract gain
and no higher-rank contract is damaged
and hook_count is not obviously dominated
```

`obviously dominated` 的意思：

```text
存在另一个候选：
  satisfied_clauses 不少
  reduced_clauses 不少
  broken_clauses 不多
  hook_count 更少
```

如果候选之间结构价值相同，才用勾数做决定。

---

## 10A. StationResourceGraph：合同之上的资源仲裁

全站合同覆盖解决的是：

```text
每批车有没有合同。
```

但真实失败经常来自另一个问题：

```text
多个合同同时抢同一个资源。
```

所以必须增加一个轻量的资源仲裁层：

```text
StationResourceGraph
```

它不是新的 workflow。

它只回答：

```text
哪些资源是独占的？
哪些合同正在申请它？
谁先用，谁等待？
```

### 10A.1 资源类型

```text
StationResource:
  resource_id
  resource_type
  capacity
  direction_mode
  occupied_by
  requested_by
  release_condition
```

资源类型：

```text
CUN4_NORTH_BUFFER       # 存4北，双向争抢核心
LINK6_GATE              # 联6 全局门
LINK7_GATE              # 联7 全局门
DEPOT_TRACK_CAPACITY    # 修1-4 容量
DEPOT_SLOT_RESOURCE     # 修1-4 库位级占用/换位
YARD_TRACK_CAPACITY     # 存1/2/3/5 容量
FUNCTION_LINE_CAPACITY  # 油/洗/抛 容量
LOCO_POSITION           # 机车位置、端别、空驶距离
LOCO_CARRY_STATE        # 机车携带/牵引状态
```

### 10A.1A DepotSlotGraph：大库是 swap，不是 load

实测后的关键修正：

```text
大库不是单纯“装更多车进去”。
大库更像“把要出的车换出来，把要进的车换进去”。
```

也就是：

```text
库内常驻车辆很多。
每天想进一批，也想出一批。
净流入接近 0，但周转很高。
```

这会推翻一个过粗假设：

```text
DEPOT_TRACK_CAPACITY 只看修1-4 总容量够不够。
```

如果只看线级容量，会误判：

```text
修1 还有总容量，REPAIR_INBOUND 可以进。
```

但真实约束可能是：

```text
目标库位正被 DEPOT_OUTBOUND 车占着。
不先出库，就没有可接的具体位。
```

所以资源图必须下沉一层：

```text
DepotSlotGraph:
  depot_track
  slot_id
  occupied_vehicle_no
  occupied_contract
  requested_by_contracts
  target_inbound_vehicle_no
  outbound_release_required
```

库位资源状态：

```text
DepotSlotResource:
  FREE
  OCCUPIED_STAY        # 已在库内且目标仍是该库位/该库线
  OCCUPIED_OUTBOUND    # 占位车要出库
  RESERVED_INBOUND     # 已为入库车预留
  SWAP_LOCKED          # 入库车等待占位出库车释放
```

入库合同不能只申请：

```text
DEPOT_TRACK_CAPACITY
```

还必须申请：

```text
DEPOT_SLOT_RESOURCE(track, slot or slot_band)
```

如果目标只给到修1-4 线、没有明确库位，就至少要建 `slot_band`：

```text
slot_band:
  track
  available_free_slots
  occupied_outbound_slots
  stayer_slots
```

判断逻辑：

```text
if inbound_target_slot is occupied by outbound vehicle:
  REPAIR_INBOUND waits DEPOT_SLOT_RESOURCE
  DEPOT_OUTBOUND gets release priority

if inbound only has target track, not exact slot:
  find free slot first
  if no free slot but outbound slots exist:
    create SWAP_LOCKED dependency
```

这不是新增复杂度炫技，而是把真实的大库时序说清楚：

```text
先出后进，不是阶段偏好。
而是库位资源依赖。
```

### 10A.1B DepotSwapDelta

大库 swap 必须有自己的资源变化摘要：

```text
DepotSwapDelta:
  inbound_vehicle_no
  target_depot_track
  target_slot_or_band
  blocking_outbound_vehicle_no
  released_slot
  reserved_slot
  swap_dependency_status
```

状态：

```text
NO_SWAP_NEEDED:
  有空位，可直接入库。

SWAP_REQUIRED:
  入库目标位/目标带被出库车占用。

SWAP_RELEASED:
  出库车已经离开，入库车可接。

SWAP_VIOLATION:
  未释放库位就强行入库，或占用了 stayer 位。
```

`ResourceDelta` 必须能输出：

```text
DEPOT_SLOT_BLOCKED_BY_OUTBOUND
DEPOT_SLOT_RELEASED
DEPOT_SLOT_RESERVED_FOR_INBOUND
DEPOT_SWAP_VIOLATION
```

否则 `StationResourceGraph` 只能管“库线容量”，管不了“具体谁给谁让位”。

### 10A.2 存4北双向资源

存4北不是一个普通 `via_port`。

它同时服务两种相反方向：

```text
REPAIR_INBOUND:
  把存4北当释放口，准备入修库/机接。

DEPOT_OUTBOUND:
  把修1-4 出库车送到存4北，腾大库。
```

因此必须建资源锁：

```text
CUN4_NORTH_BUFFER:
  direction_mode:
    INBOUND_RELEASE
    OUTBOUND_HOLD
    MIXED_DIRTY
    FREE
```

硬规则：

```text
INBOUND_RELEASE 与 OUTBOUND_HOLD 不能无仲裁并发。

如果 CUN4_NORTH_BUFFER = INBOUND_RELEASE:
  DEPOT_OUTBOUND 可以申请，但不能污染释放口。

如果 CUN4_NORTH_BUFFER = OUTBOUND_HOLD:
  REPAIR_INBOUND 可以申请，但必须先生成 CLEAR_CUN4_NORTH 或 USE_ALTERNATIVE_BUFFER。

如果 CUN4_NORTH_BUFFER = MIXED_DIRTY:
  任何机接/大释放都不能直接跨过。
```

这比在每条合同里写 `DO_NOT_BLOCK_REPAIR_INBOUND_PORT` 更强。

因为它能决定：

```text
同一时刻谁先用存4北。
```

### 10A.3 联6 / 联7 全局门

联6/联7 是全站门控，不属于任何单条 FlowEdge。

所以不能只放在某条边的 `must_not_break` 里。

```text
GlobalGate:
  gate_id: LINK6_GATE / LINK7_GATE
  current_mode
  requested_mode
  occupied_by_contract
  waiting_contracts
  switch_cost
  release_condition
```

门控规则：

```text
同一时刻只能服务兼容方向的合同。
跨门动作必须先申请 gate slot。
gate slot 未获批，候选动作不能进入 ContractDelta scoring。
```

典型影响：

```text
REPAIR_INBOUND 想过联6/联7 入库。
DEPOT_OUTBOUND 想从修库出来腾库。
YARD_REBALANCE 想借同一通道调位。
```

如果没有全局门，局部合同都觉得自己合理，合起来就互相堵死。

### 10A.4 loco_carry 状态压缩

在讨论 `loco_carry` 之前，必须先建 `LOCO_POSITION`。

`LOCO_POSITION` 是少钩的一阶因素：

```text
LocoPosition:
  track
  side
  accessible_end
  distance_to_target_contract
  route_to_target
  gate_requirements
```

规则：

```text
TargetContractSelector 不能只看合同收益。
还必须看机车从当前位置到目标合同的空驶距离、端别是否可接、是否需要跨联6/联7。
```

如果缺 `LOCO_POSITION`：

```text
合同选择可能结构正确，但勾数很差。
```

所以资源图里必须同时建：

```text
LOCO_POSITION
LOCO_CARRY_STATE
```

`loco_carry` 不应该作为搜索状态无限膨胀，但也不能把顺序信息直接丢掉。

4 值枚举只能作为粗资源标签：

```text
LocoCarryState:
  EMPTY
  CARRYING_CONTRACT_GROUP
  CARRYING_MIXED_GROUP
  DIRTY_CARRY
```

硬规则：

```text
CARRYING_CONTRACT_GROUP:
  只能继续履行该 contract，或执行明确 transfer。

CARRYING_MIXED_GROUP:
  必须先拆分成合法 contract group，不能直接机接/closeout。

DIRTY_CARRY:
  只能生成清理/拆分候选。
```

这样可以避免：

```text
机车带着一串混合车，搜索还在自由组合，状态爆炸。
```

但真实可摘性仍要保留：

```text
ordered_carry_segments:
  segment_id
  vehicle_nos_in_tail_order
  contract_family
  target_track_or_resource
  detachable_from_tail
```

也就是说：

```text
LocoCarryState 用来裁剪“能不能继续乱跑”。
ordered_carry_segments 用来判断“能不能合法少钩摘下”。
```

### 10A.5 ResourceDelta

候选动作除了产生 `ContractDelta`，还必须产生 `ResourceDelta`。

```text
ResourceDelta:
  requested_resources
  acquired_resources
  released_resources
  blocked_resources
  resource_violations
```

接受准则变成：

```text
Accept = ContractDelta 合法
      and ResourceDelta 合法
      and hook_count 不被同等级候选支配
```

资源硬违反：

```text
CUN4_DIRECTION_CONFLICT
LINK6_GATE_CONFLICT
LINK7_GATE_CONFLICT
BUFFER_CAPACITY_OVERFLOW
DEPOT_SLOT_SWAP_VIOLATION
LOCO_DIRTY_CARRY_CROSS_ACCEPT
```

### 10A.6 跨族 TargetContract 仲裁

全站覆盖后，`TargetContract` 不能再用纯修库优先级。

新的选择顺序：

```text
select_target_contract(flow_graph, resource_graph):
  1. 过滤 no-move vehicles
  2. 过滤资源不可申请的合同
  3. 找出正在占用关键资源的合同
  4. 找出阻塞最多其他合同的合同
  5. 找出硬时序即将违约的合同
  6. 在同资源等级内按合同收益和 hook_count 排序
```

跨族优先级不是固定写死：

```text
REPAIR_INBOUND 永远优先
```

而是看资源图：

```text
谁占着关键资源？
谁释放后能让更多合同推进？
谁如果不做会导致硬违约？
```

例子：

```text
如果 DEPOT_OUTBOUND 占住修库库位，导致 REPAIR_INBOUND 无法入库：
  DEPOT_OUTBOUND 优先，因为它释放 DEPOT_SLOT_RESOURCE。

如果 DEPOT_OUTBOUND 想占存4北，但 REPAIR_INBOUND 已经 PORT_READY 等机接：
  REPAIR_INBOUND 优先，因为存4北处于 INBOUND_RELEASE。
```

### 10A.7 Resource Deadlock Detection

多合同共享资源后，必须显式处理死锁。

典型死锁：

```text
REPAIR_INBOUND 等 CUN4_NORTH_BUFFER 释放。
DEPOT_OUTBOUND 等 CUN4_NORTH_BUFFER 接出库车。
REPAIR_INBOUND 等 DEPOT_SLOT_RESOURCE 释放。
DEPOT_SLOT_RESOURCE 又被 DEPOT_OUTBOUND 的占位车占着。
```

或者：

```text
Contract A 等 LINK6_GATE。
Contract B 占 LINK6_GATE 但等 CUN4_NORTH_BUFFER。
Contract C 占 CUN4_NORTH_BUFFER 但等 A 释放库容。
```

资源图必须维护等待关系：

```text
WaitForGraph:
  nodes:
    contracts
    resources
  edges:
    contract -> resource   # contract 正在等待资源
    resource -> contract   # resource 当前被 contract 占用
```

每次生成候选前检查：

```text
detect_deadlock(wait_for_graph):
  cycles = find_cycles(contract/resource graph)
  if cycles:
    emit RESOURCE_DEADLOCK
    choose_break_contract(cycle)
```

死锁打破策略：

```text
1. 优先让占用关键资源但没有立即履约收益的合同让步。
2. 优先释放 CUN4_NORTH_BUFFER / LINK6_GATE / LINK7_GATE。
3. 如果两个合同收益相近，选择 hook_cost 最低的让步动作。
4. 让步动作只能是合法 ContractDelta + ResourceDelta，不能为了破死锁破坏硬合同。
```

输出 trace：

```json
{
  "resource_deadlock": {
    "cycle": [
      "REPAIR_INBOUND -> CUN4_NORTH_BUFFER",
      "CUN4_NORTH_BUFFER -> DEPOT_OUTBOUND",
      "REPAIR_INBOUND -> DEPOT_SLOT_RESOURCE",
      "DEPOT_SLOT_RESOURCE -> DEPOT_OUTBOUND"
    ],
    "break_contract": "DEPOT_OUTBOUND",
    "break_action_family": "CLEAR_CUN4_NORTH",
    "reason": "DEPOT_OUTBOUND holds CUN4_NORTH_BUFFER and blocks higher-gain inbound release"
  }
}
```

没有死锁检测，`StationResourceGraph` 只会告诉你“大家都在等”，不会告诉你“谁必须先让”。

### 10A.8 LocoCarry Compression Validation

`LocoCarryState` 压缩是设计假设，不是已验证结论。

压缩成立的前提：

```text
同一 contract group 内部顺序不影响工艺合法性和少钩最优。
```

但调车里经常不是这样。

例如：

```text
挂车端别不同，决定哪辆能先摘。
同组内部顺序不同，可能导致修库摘解多一钩。
混合携带时，前后顺序可能决定是否能直接入线。
```

所以 `loco_carry` 不能只靠 4 值枚举上线。

当前静态审计已经给出强反证：

```text
脚本:
  scripts/validate_flow_edge_foundation_experiments.py

数据:
  truth2_force 113 案例

结果:
  risk_case_count = 112 / 113
  risk_case_ratio = 99.12%
  adjacent_target_switch_count = 1040
  static_audit_conclusion = ORDER_SENSITIVE_CASES_EXIST
```

这说明：

```text
同一来源线上，需动车经常按不同目标交错排列。
如果机车一把挂走，能不能少钩摘下，取决于携带序列的尾端顺序。
```

所以当前结论不是“压缩可能成立”，而是：

```text
不能把 loco_carry 压成只有 4 值枚举。
4 值枚举只能做资源粗状态。
主求解仍必须保留 ordered carry sequence 或 ordered_contract_segments。
```

必须做压缩实验：

```text
LocoCarryCompressionExperiment:
  baseline:
    keep_ordered_carry_sequence = true

compressed:
  use LocoCarryState enum only

  compare:
    solvability
    hook_count
    invalid_detach_count
    wrong_end_access_count
    contract_violation_count
    resource_deadlock_count
```

通过条件：

```text
compressed.solvability >= baseline.solvability
compressed.hook_count <= baseline.hook_count + accepted_tolerance
compressed.invalid_detach_count = 0
compressed.wrong_end_access_count = 0
compressed.contract_violation_count = 0
```

在严格 A/B 实验实现前，工程默认值应是：

```text
keep_ordered_carry_sequence = true
use_loco_carry_state_as_label = true
disable_enum_only_carry_compression = true
```

如果不通过，改成分层 carry 表达：

```text
LocoCarry:
  carry_state
  ordered_contract_segments
  accessible_end
  detach_constraints
```

也就是说：

```text
全局仍用 LocoCarryState 降维。
但在需要摘解/端别/顺序判断时，保留局部 ordered segments。
```

这条必须先实验，不能靠文档断言。

这就是“双优硬约束”的落地方式：

```text
先保证结构方向对。
方向对了，勾数优化才有意义。
方向不对，少一钩也是错。
```

### 10.4 hook_count 的位置

虽然 `hook_count` 不能压过结构硬约束，但也不能完全后置。

正确位置是：

```text
在同一结构等级内尽早比较。
```

也就是：

```text
先分结构等级：
  硬违反
  合同违约
  无履约
  blocker 改善
  合同债务减少
  合同条款完成
  合同切换到下一段

再在同等级里比较 hook_count。
```

这样既不会为了少钩走错方向，也不会因为“结构正确”而接受很笨的多钩方案。

### 10.5 候选生成要被 EdgeContract 反向约束

这份设计不是只改评分。

更重要的是前移到候选生成：

```text
TargetContract = PORT_READY_REPAIR_CONTRACT:
  优先生成大释放、机接、入修库候选。
  弱化或禁用外场补车候选。

TargetContract = DEPOT_DIGEST_CONTRACT:
  优先生成修库摘解、轮/油消化、库回前清债候选。
  禁止尾项 closeout 候选抢主链。

TargetContract.evidence_level = low:
  生成补证据和低风险组织候选。
  禁止主动跨 ACCEPTED。
```

如果只在最后评分，搜索空间已经被大量错误候选污染。

真正应该做的是：

```text
用 EdgeContract 决定“应该生成哪类可行动作”。
再用 ContractDelta 比较这些动作。
最后才用搜索在局部 tie-break。
```

这才是“立足人工又高于人工”：

- 人工给出结构意图。
- 算法用结构意图裁剪动作空间。
- 算法在剩余空间里找更少钩。

---

## 11. 识别器设计：从现场到 FlowGraph

这一节是落地关键。

如果识别不稳，后面的合同和 delta 都会漂。

### 11.1 输入

识别器只依赖当前求解现场和历史动作轨迹：

```text
current_state:
  track -> ordered vehicles
  vehicle -> attributes
  target requirements
  locomotive position

history:
  executed moves
  previous solver moves
  previous FlowGraph snapshot
```

如果是离线人工案例研究，还可以额外使用：

```text
manual_order_signals:
  存4前
  存4摘
  机接
  摘解
  库回
```

但在线求解不能依赖未来人工答案。

在线识别器必须把这两类证据分开：

```text
OnlineObservable:
  current track order
  vehicle target
  repair process
  resource occupancy
  locomotive position
  executed solver moves

OfflineLabelOnly:
  manual_order_signals
  human hook sequence
  future machine accept / depot detach / closeout actions
```

`OfflineLabelOnly` 只能用于验证识别器准不准，不能作为识别器输入。

### 11.2 识别步骤

```text
build_flow_graph(state, history):
  1. extract online geometry/resource features
  2. infer candidate edge_key
  3. bind subject seed
  4. assign status with guards
  5. build EdgeContract from edge_key + status + evidence
  6. attach blockers / obligations / protections as contract clauses
  7. put unresolved items into residuals with expiry
  8. compare with previous FlowGraph and stabilize edge identity
```

### 11.3 在线可观测特征

在线高价值特征：

| 特征 | 含义 |
| --- | --- |
| `track_order` | 车组连续性、端别、阻挡关系 |
| `target_track_set` | 是否已就位、是否需要移动 |
| `distance_to_port` | 是否接近存4北、联6/联7、修库入口 |
| `resource_occupancy` | 存4北/联6/联7/库容是否可申请 |
| `receiver_capacity` | 修库、预修、调棚、功能线是否可接 |
| `locomotive_position` | 机车所在线、端别、空驶距离、可接近方向 |
| `repair_process` | 拉走、称重、留、轮对架、修x库外等特殊语义 |
| `executed_solver_moves` | 已执行动作造成的真实状态变化 |

人工动作信号不在这张表里。

原因：

```text
在线求解时，人工动作还没有发生。
求解器不能靠未来答案判断当前 status。
```

人工动作信号只作为离线标签：

| 离线标签 | 验证什么 |
| --- | --- |
| `存4 - 北头` | 在线是否提前识别出靠口趋势 |
| `存4 - n 北头摘` | 在线是否在动作前识别出 release ready |
| `机 + n 接` | 在线是否在动作前识别出 accept ready |
| `修x - n / 库外n / 摘` | 在线是否识别出 digesting debt |
| `库 回` | 在线是否识别出 closeout ready |

### 11.4 反推信号

人工单里有些 case 缺前段信号。

所以允许反推：

```text
后面出现 机 + n 接 + 修库摘解:
  可以反推存在 REPAIR_MAIN edge。

后面出现 修库摘解 + 库回:
  可以反推存在 DEPOT_DIGEST_ONLY edge。

后面出现 轮交织 + 修库摘解:
  可以把轮作为主边 obligation 或 support edge。
```

但反推有硬限制：

```text
反推可以创建 edge。
反推可以设置 PORT_READY / DIGESTING。
反推不能低置信主动创建 ACCEPTED 动作。
```

也就是说：

```text
可以承认“它已经发生过/已经跨过”。
不能凭空决定“现在就该机接”。
```

### 11.5 residuals 封口规则

`residuals` 是临时区，不是垃圾桶。

更严格地说：

```text
residuals 只能存放“暂时证据不足”的少量车辆。
不能存放“没有合同模板”的主体车流。
```

如果出现：

```text
residual_vehicle_ratio > 10%
```

这不是 residual 需要更聪明。

而是合同体系漏了流向。

```text
ResidualItem:
  vehicles
  suspected_role
  reason_unbound
  expiry_condition
  max_age
```

允许的 `suspected_role`：

```text
POSSIBLE_SUPPORT
POSSIBLE_TAIL
POSSIBLE_BLOCKER
NOISE
```

必须有 `expiry_condition`：

| 条件 | 处理 |
| --- | --- |
| 后续动作证明它服务主边 | 归入 primary edge |
| 后续动作证明它是轮/油支持项 | 建 support edge |
| 主边 DONE 且它不挡路 | 转 tail |
| 长时间无结构影响 | 降为 noise |
| 挡住 port/receiver | 转 blocker |

不允许：

```text
residual 永久存在但不影响任何决策。
```

这会让设计失控。

### 11.6 residual 覆盖率硬门槛

每一步都必须输出：

```text
contracted_vehicle_count
residual_vehicle_count
gross_contract_coverage
effective_contract_coverage
no_move_vehicle_ratio
residual_vehicle_ratio
residual_reason_breakdown
```

硬门槛：

```text
residual_vehicle_ratio <= 10%
```

研究目标：

```text
residual_vehicle_ratio <= 5%
```

超过门槛时，不允许继续调 `TargetContract` 或评分权重。

必须先回答：

```text
哪类车没有合同？
是 DEPOT_OUTBOUND 漏了？
是 PRE_REPAIR_STAGING 漏了？
是 YARD_REBALANCE 漏了？
还是 FUNCTION_LINE_SERVICE 漏了？
```

这条门槛专门防止：

```text
用一个很优雅的修库主链模型，处理 23% 的车；
再把 77% 的车塞进 residual。
```

---

## 12. 求解闭环

完整求解链路应该是：

```text
Input
  -> FlowGraphBuilder
  -> FlowClassify
  -> TargetContractSelector
  -> StructuralIntentBuilder
  -> WorkPatternTemplateSelector
  -> EdgeBoundedCandidateGenerator
  -> ContractDeltaSimulator
  -> AcceptRejectGate
  -> LocalTieBreakSearch
  -> Move
  -> State Update
  -> FlowGraph Rebuild
  -> Output
```

说人话：

```text
先看现场形成了哪些车流关系。
再决定现在最该履行哪个边合同。
再把合同下一步翻译成结构意图。
然后只生成能服务这个合同和结构意图的动作。
再看这些动作让合同条款被履行还是被破坏。
结构都对时，再用小搜索比勾数。
每走一步，重新识别现场。
```

### 12.1 模块职责

| 模块 | 职责 | 不能做什么 |
| --- | --- | --- |
| `FlowGraphBuilder` | 从现场识别边 | 不能选动作 |
| `FlowClassify` | 把全站车辆归入合同族 | 不能把大批车辆丢 residual |
| `TargetContractSelector` | 选当前最重要的合同 | 不能生成动作 |
| `StructuralIntentBuilder` | 把合同下一步翻译成可执行结构目标 | 不能自己改合同优先级 |
| `WorkPatternTemplateSelector` | 为 intent 选择可用人工模板 | 不能成为 workflow 主控 |
| `EdgeBoundedCandidateGenerator` | 按 contract/status/intent/template 生成候选 | 不能跨硬违反 |
| `ContractDeltaSimulator` | 模拟候选导致的合同变化 | 不能直接调分作弊 |
| `AcceptRejectGate` | 执行硬违反和结构准入 | 不能为了勾数放行硬违反 |
| `LocalTieBreakSearch` | 同结构等级内少钩比较 | 不能扩大成全局搜索 |

### 12.2 搜索的位置

搜索不是主体。

搜索只做两件事：

```text
1. 在同一个 TargetContract 的同一个结构意图内比较少钩。
2. 在短 horizon 内避免显然返工。
```

禁止搜索做这些事：

- 自己发明主链。
- 自己决定是否 closeout。
- 自己绕过机接保护。
- 自己把 `DIGESTING` 当尾项。
- 自己在 `PORT_READY` 时回外场大补车。

这就是和过去 workflow / search 主体的根本区别。

过去更像：

```text
生成很多动作 -> 搜索试 -> 分数挑一个 -> 后面修补
```

这里应该是：

```text
结构先裁剪动作 -> delta 守门 -> 小搜索只做同类优选
```

### 12.3 输出

每一步必须输出可审查 trace：

```json
{
  "step": 17,
  "flow_graph_summary": {
    "gross_contract_coverage": 0.97,
    "effective_contract_coverage": 0.94,
    "no_move_vehicle_ratio": 0.39,
    "residual_vehicle_ratio": 0.06,
    "active_edges": [
      {
        "edge_id": "edge_001",
        "edge_key": "REPAIR_MAIN_VIA_CUN4",
        "status": "PORT_READY",
        "confidence": "HIGH",
        "obligations": ["MACHINE_ACCEPT", "DEPOT_DETACH"],
        "protections": ["KEEP_RELEASE_PORT_CLEAN"]
      }
    ],
    "contract_groups": {
      "REPAIR_INBOUND": 14,
      "DEPOT_OUTBOUND": 16,
      "PRE_REPAIR_STAGING": 11,
      "DISPATCH_SHED_QUEUE": 9,
      "YARD_REBALANCE": 18,
      "FUNCTION_LINE_SERVICE": 4,
      "LOCO_AREA_STAGING": 3,
      "SPECIAL_REPAIR_PROCESS": 2,
      "TAIL_CLOSEOUT": 2
    },
    "no_move_vehicle_count": 31,
    "residual_count": 2,
    "residual_reason_breakdown": {
      "POSSIBLE_BLOCKER": 1,
      "POSSIBLE_SUPPORT": 1
    }
  },
  "target_contract": "PORT_READY_REPAIR_CONTRACT",
  "generated_candidate_families": ["MACHINE_ACCEPT", "DEPOT_ENTRY"],
  "accepted_contract_delta": {
    "move": "机 + 14 接",
    "satisfied_clauses": ["MACHINE_ACCEPT"],
    "next_contract_key": "MACHINE_ACCEPTED_REPAIR_CONTRACT",
    "activated_clauses": ["KEEP_ACCEPTED_TRAIN_INTACT"],
    "hook_count": 1
  },
  "rejected_deltas": [
    {
      "move": "回外场补车",
      "reason": "FORCE_OUTER_PICKUP_ON_PORT_READY_EDGE"
    }
  ]
}
```

没有这种 trace，就无法判断失败到底是：

- 边识别错。
- 目标合同选错。
- 候选生成错。
- contract delta 模拟错。
- gate 放错。
- 小搜索 tie-break 错。

---

## 13. 人工案例到合同规则

这一节把人工案例压成合同规则，不停留在“像人工”。

| Family | 代表 case | 合同模板 | 求解偏好 | 硬禁止 |
| --- | --- | --- | --- | --- |
| `full_chain` | `0117Z`, `0115W`, `0128W`, `0303W` | `FULL_CHAIN_REPAIR` | 前段集货，后段保护大列并消化 | 机接后拆主列 |
| `late_cun4_chain` | `0310W`, `0302W`, `0331W`, `0105W` | `LATE_CUN4_REPAIR` | 不补外场长链，直接承接/消化 | `FORCE_OUTER_PICKUP_ON_PORT_READY_EDGE` |
| `direct_repair_entry` | `0104W`, `0213W`, `0123W`, `0313W` | `DIRECT_REPAIR_ENTRY` | 尊重近库结构，不强制存4长链 | 新增 `OUTER_PICKUP` 债务 |
| `mixed_or_exception` | `0130Z`, `0201W`, `0306W`, `0311W` | `MIXED_SIGNAL_REPAIR` | 低风险补证据，不主动跨机接 | `HALF_READY_MACHINE_ACCEPT` |
| `depot_ops_without_machine_accept` | `0103W`, `0223W`, `0308W`, `0329W` | `DEPOT_DIGEST_ONLY` | 先修库消化再收尾 | `CLOSEOUT_BEFORE_EDGE_DONE` |

---

## 14. 重新定位：不是更简单，而是显式化全局编排

到这里必须重新定义这套方案的价值。

它不是把复杂度消灭了。

它是把复杂度从：

```text
隐式 workflow 阶段
隐式搜索评分
隐式补丁规则
```

迁移到：

```text
显式合同
显式资源
显式冲突
显式死锁
显式验证门槛
```

这件事更诚实，也更适合工程实现。

### 14.1 和 workflow 的本质区别

原 workflow 的真实价值不是“阶段名字”。

它其实在隐式管理：

```text
联6/联7 什么时候开门
存4北当前服务哪个方向
修库容量什么时候释放
机车带车状态是否干净
哪些尾项可以收束
```

新结构不是否认这些复杂度。

新结构只是把它们换成显式对象：

| workflow 隐式职责 | 新结构显式承接 |
| --- | --- |
| 阶段推进 | `EdgeContract` 合同切换 |
| 阶段资源边界 | `StationResourceGraph` |
| 联6/联7 门控 | `LINK6_GATE / LINK7_GATE` |
| 存4方向独占 | `CUN4_NORTH_BUFFER.direction_mode` |
| 搜索兜底 | `ContractDelta + ResourceDelta` |
| 异常尾项 | `residual_vehicle_ratio` 门槛 |

所以更准确的说法是：

```text
不是更简单。
而是更显式、更可测、更能定位失败。
```

### 14.2 为什么仍然值得做

虽然对象变多了，但每个对象有明确职责：

```text
FlowClassify:
  解决哪些车需要动、属于哪个流向。

EdgeContract:
  解决这条边当前必须做什么、不能做什么。

StationResourceGraph:
  解决多个合同抢共享资源时谁先谁后。

ContractDelta:
  解决候选动作是否履约。

ResourceDelta:
  解决候选动作是否拿得到资源。
```

这比 workflow 的优势是：

```text
失败时能定位：
  是分类错
  是合同错
  是资源仲裁错
  是 carry 压缩错
  是候选缺失
  是局部少钩 tie-break 错
```

而不是只得到：

```text
这个阶段没解出来。
```

### 14.3 复杂度边界

为了防止重新变成一堆层，必须守住边界：

```text
FlowClassify 只分类，不选动作。
EdgeContract 只定义合同，不抢资源。
StationResourceGraph 只仲裁资源，不替合同评分。
ContractDelta 只评价履约，不处理资源。
ResourceDelta 只评价资源，不处理业务价值。
LocalTieBreakSearch 只在同等级候选里少钩比较。
```

这套结构的口径应该是：

```text
显式全局编排，不是极简模型。
```

---

## 15. 实施路径

### 15.1 第一阶段：只建图，不控求解

目标：

```text
每一步输出 FlowGraph + FlowClassify。
```

第一优先验收不是修库主链是否漂亮，而是：

```text
全站车辆是否都有合同。
```

必须输出：

```text
gross_contract_coverage
effective_contract_coverage
no_move_vehicle_ratio
residual_vehicle_ratio
contract_groups
residual_reason_breakdown
```

硬门槛：

```text
12 个真实案例:
  effective_contract_coverage >= 90%
  residual_vehicle_ratio <= 10%

研究目标:
  effective_contract_coverage >= 95%
  residual_vehicle_ratio <= 5%
```

### 15.1A 地基验收：遮答案在线识别测试

目标：

```text
证明不看人工动作序列，只看 StartStatus，也能识别出可用的初始 status / contract / resource。
```

输入只允许：

```text
StartStatus:
  track ordered vehicles
  vehicle positions
  target track sets
  repair process
  flags
  locomotive position
```

禁止输入：

```text
manual_order_signals
human hook sequence
future 存4摘 / 机接 / 修库摘解 / 库回
```

每个 case 输出：

```text
online_initial_flow_graph
online_status_reason
online_contract_reason
online_resource_state
hidden_manual_label_comparison
```

验收：

```text
online status 能把 REPAIR_INBOUND / DEPOT_OUTBOUND / YARD_REBALANCE / FUNCTION_LINE_SERVICE 等主合同分清。
online PORT_READY / ACCEPT_READY 不能依赖未来 存4摘 / 机接。
online DONE 不能依赖未来 库回。
```

如果遮答案测试不过：

```text
后面的 ContractDelta / StationResourceGraph / 搜索优化都不能算在线成立。
```

当前已跑的静态验收：

```text
脚本:
  scripts/validate_flow_edge_foundation_experiments.py

输入:
  data/validation_inputs/truth2_force

输出:
  artifacts/flow_edge_foundation_experiments.json
```

结果：

```text
case_count = 113
vehicle_count = 9506
movable_vehicle_count = 5757
manual_signal_keys_present = []
effective_contract_coverage = 98.3%
online_status_coverage = 100.0%
```

这说明：

```text
只看 StartStatus，合同族和初始粗 status 可以建起来。
```

但也要诚实说明边界：

```text
这只是初始静态识别验收。
还没有证明动态执行中每一步 status 都能正确更新。
```

验收 case：

```text
0117Z:
  识别 REPAIR_MAIN_VIA_CUN4

0310W:
  初始或中段识别 PORT_READY，不补外场

0103W / 0223W:
  识别 DEPOT_DIGEST_ONLY + DIGESTING

0130Z / 0201W / 0306W:
  识别 contradiction + medium/low confidence
```

但这些只验证 `REPAIR_INBOUND`。

还必须覆盖：

```text
DEPOT_OUTBOUND:
  修1-4 -> 存4北，腾大库

PRE_REPAIR_STAGING:
  预修暂存编组

DISPATCH_SHED_QUEUE:
  调棚作业排队

YARD_REBALANCE:
  存1/2/3/5 整理、腾位、归并

FUNCTION_LINE_SERVICE:
  油/洗/抛 功能线进出

LOCO_AREA_STAGING:
  机库/机北3/机棚缓冲

SPECIAL_REPAIR_PROCESS:
  拉走/称重/留/轮对架/修x库外
```

### 15.1B 大库 slot-level swap 诊断

目标：

```text
验证 DEPOT_TRACK_CAPACITY 是否过粗。
量化大库是否真的是 slot/band 级 swap 问题。
```

已跑结果：

```text
脚本:
  scripts/validate_flow_edge_foundation_experiments.py

数据:
  truth2_force 113 案例

结果:
  cases_with_same_track_swap_pressure = 110 / 113
  case_ratio = 97.35%
  same_track_blocked_inbound_lower_bound = 1448
  strict_slot_block_count_order_approximation = 911
  avg_net_depot_flow = -0.177
```

说人话：

```text
几乎每个真实 case 都不是“库里还能不能塞”。
而是“要进来的车，需要等要出去的车先让位”。
```

典型高压案例：

```text
20260117Z:
  depot_inbound = 16
  depot_outbound = 16
  net_depot_flow = 0
  same_track_blocked_lower_bound = 16

20260105W / 20260113W / 20260120W / 20260121W:
  depot_inbound = 15
  depot_outbound = 15
  net_depot_flow = 0
  same_track_blocked_lower_bound = 15
```

结论：

```text
DEPOT_TRACK_CAPACITY 只能当粗筛。
真正仲裁必须靠 DEPOT_SLOT_RESOURCE / DepotSlotGraph。
```

限制：

```text
当前输入多为目标库线，不一定给精确目标库位。
strict_slot_block_count_order_approximation 使用 order 近似 slot。
所以 911 不是最终库位阻塞真值，而是“slot 级互锁一定不能忽略”的下界证据。
```

### 15.2 第二阶段：ContractDelta 旁路评估

目标：

```text
不改变 solver 决策，只评价当前 solver 的动作。
```

输出：

```text
selected_contract_delta
manual_like_better_contract_delta_if_any
hard_violation_if_any
```

这样能先定位当前 solver 为什么可解性差：

- 是没生成对的动作。
- 是生成了但没选。
- 是选了但破坏结构。
- 是过早 closeout。

### 15.2A 第二阶段补充：ResourceDelta 旁路评估

目标：

```text
不改变 solver 决策，只评价当前动作的资源影响。
```

输出：

```text
selected_resource_delta
wait_for_graph
resource_deadlock_if_any
gate_slot_rejection_if_any
cun4_direction_conflict_if_any
depot_slot_swap_delta_if_any
loco_carry_state
```

必须覆盖：

```text
CUN4_NORTH_BUFFER:
  入库释放 vs 出库暂存

LINK6_GATE / LINK7_GATE:
  跨门 slot 申请、占用、释放

DEPOT_SLOT_RESOURCE:
  库位/slot_band 占用、释放、预留、swap 依赖

LOCO_CARRY_STATE:
  EMPTY / CARRYING_CONTRACT_GROUP / CARRYING_MIXED_GROUP / DIRTY_CARRY
```

这一阶段只诊断，不裁剪候选。

如果发现资源死锁或 gate 冲突，先输出原因，不急着改求解器。

### 15.3 第三阶段：候选生成前置裁剪

目标：

```text
用 TargetContract 控制候选动作族。
```

先接全站合同裁剪：

```text
REPAIR_INBOUND / PORT_READY:
  禁止外场大补车抢主链。

REPAIR_INBOUND / ACCEPTED:
  禁止拆已承接主列。

REPAIR_INBOUND / DIGESTING:
  禁止 closeout 抢修库摘解。

DEPOT_OUTBOUND:
  优先释放 DEPOT_SLOT_RESOURCE，并移动到存4北。
  禁止当作入库主链。

PRE_REPAIR_STAGING:
  优先稳定预修暂存。
  禁止强制机接。

DISPATCH_SHED_QUEUE:
  优先保持调棚通路和队列。
  禁止拉回修库主链。

YARD_REBALANCE:
  优先腾位、归并、保持可取性。
  禁止随机打散和占死存4。

FUNCTION_LINE_SERVICE:
  优先完成功能线服务和退出。
  禁止服务未完就当尾项。

LOCO_AREA_STAGING:
  优先保持机区通路和缓冲线可用。
  禁止产生 DIRTY_LOCO_CARRY。

SPECIAL_REPAIR_PROCESS:
  优先满足 RepairProcess 语义。
  禁止按普通存车整理处理。
```

### 15.4 第四阶段：loco_carry 压缩实验

目标：

```text
验证 enum-only LocoCarryState 压缩是否安全。
```

对比两组：

```text
baseline:
  保留 ordered carry sequence

compressed:
  使用 LocoCarryState + ordered_contract_segments fallback
```

注意：

```text
静态审计已经发现 112/113 案例存在顺序敏感风险。
所以默认方案不是 enum-only 压缩。
默认方案是 LocoCarryState 标签 + ordered_contract_segments。
```

必须比较：

```text
solvability
hook_count
invalid_detach_count
wrong_end_access_count
contract_violation_count
resource_deadlock_count
```

通过后才能把压缩 carry 接入主求解。

如果不通过：

```text
保留 ordered_contract_segments。
只在全局资源层使用 4 值 carry_state。
```

### 15.5 第五阶段：局部 tie-break 少钩优化

目标：

```text
在结构同等级候选里比较 hook_count。
```

只允许小 horizon。

不要一上来做全局最优搜索。

### 15.6 第六阶段：全量案例压测

至少输出：

```text
case_id
family
initial_flow_graph
gross_contract_coverage
effective_contract_coverage
no_move_vehicle_ratio
residual_vehicle_ratio
contract_group_counts
resource_conflicts
critical_contract_transitions
violations
final_solvability
hook_count
failure_bucket
```

failure_bucket：

```text
EDGE_MISIDENTIFIED
TARGET_EDGE_WRONG
TARGET_CONTRACT_WRONG
CANDIDATE_MISSING
CONTRACT_DELTA_SIM_WRONG
GATE_TOO_STRICT
GATE_TOO_LOOSE
LOCAL_SEARCH_DOMINATED
REAL_INFRA_CONSTRAINT
```

这比简单说“搜索失败”更有用。

---

## 16. 风险和控制

| 风险 | 表现 | 控制 |
| --- | --- | --- |
| 合同覆盖不足 | 只覆盖修库主链 23%，大批车辆掉 residual | 先做 FlowClassify，`effective_contract_coverage >= 90%` 才允许优化 |
| 不动车虚增覆盖率 | 已在目标线的 39% 车辆被算作合同覆盖 | 先输出 `NO_MOVE_SATISFIED`，覆盖率分 gross/effective 两套 |
| active contract 并发失控 | 现实平均约 6 个并发族，但旧口径只允许 1 条主边 | 使用 `active_contract_set + StationResourceGraph` 仲裁 |
| 存4北双向争抢 | `REPAIR_INBOUND` 要释放口，`DEPOT_OUTBOUND` 要存4北暂存 | 用 `CUN4_NORTH_BUFFER.direction_mode` 做资源锁 |
| 大库 swap 被线级容量掩盖 | 净流入约 0，但入库车目标位被出库车占着；线级容量看不出先出后进 | 建 `DepotSlotGraph / DEPOT_SLOT_RESOURCE`，输出 `DepotSwapDelta` |
| 联6/联7 全局门缺失 | 多合同都觉得局部合理，合起来抢全局通道 | 用 `LINK6_GATE / LINK7_GATE` 申请 gate slot |
| 特殊修程被压平 | 拉走/称重/留/轮对架被当普通存车整理 | 读取 `RepairProcess`，优先生成 `SPECIAL_REPAIR_PROCESS` |
| loco_carry enum-only 压缩不安全 | 静态审计 112/113 案例存在目标交错和顺序敏感 | 默认保留 `ordered_contract_segments`，4 值枚举只做资源标签 |
| 资源死锁 | 多合同环形等待存4北、库容、联6/联7 | 建 `WaitForGraph`，检测 cycle，并生成合法让步动作 |
| `FlowEdge` 太粗 | 一条边塞进修库、轮、油、尾项所有问题 | 只有独立连续推进价值才转 support edge |
| `TargetContract` 黑箱化 | 调分过 case，但不知道为什么 | 先规则排序，再 tie-break，输出 reason |
| `WorkPattern` 主控化 | pattern 直接决定下一步，重新退化成阶段机 | pattern 只能作为 `StructuralIntent` 的候选模板 |
| `_tier()` 控制中心化 | 候选先乱生成，再靠尾部 promote/demote 补救 | 把控制前移到 contract / intent / resource request |
| `residual` 垃圾桶化 | 解释不了的车永久残留，甚至承接主体车流 | 必须有 expiry_condition、max_age、`residual_vehicle_ratio <= 10%` |
| 硬约束被可解性冲掉 | 低置信机接、未消化 closeout | 硬违反不能被 hook_count 抵消 |
| 少钩破坏结构 | 短期少一钩，后面返工 | 先按结构等级分级，同级再比勾数 |
| 复刻人工动作 | 只会模仿 `存4 - 14 / 机 + 14` | 人工动作只当结构信号，不当动作模板 |

---

## 17. 最终判断

这个设计能不能解决问题，关键不在名字叫 `FlowEdge`。

关键在五点：

```text
1. 用 FlowClassify 先过滤不动车，并把需动车归入全站合同族。
2. 用 EdgeContract 稳住“这条边当前承诺了什么，什么必须完成，什么不能破坏”。
3. 用 StationResourceGraph 仲裁存4北、联6/联7、DEPOT_SLOT_RESOURCE、缓冲容量和 loco_carry。
4. 用 StructuralIntent 把合同下一步变成可执行结构目标，再用 WorkPatternTemplate 提供候选模板。
5. 用 ContractDelta + ResourceDelta 把动作承接成合同变化和资源变化，而不是让搜索自己猜。
```

如果这五点做到，求解思路就不是“搜索为主体”。

它会变成：

```text
结构识别为主体。
结构意图做前置动作边界。
资源仲裁做边界。
候选生成受结构约束。
搜索只做局部优选。
```

这才有机会同时提高可解性和勾数。

因为：

```text
可解性来自不走错主结构。
勾数来自在正确主结构内做局部最优。
```

---

## 18. 人工案例映射

### 18.1 `0117Z`：标准完整链

人工链条：

```text
存2/存5/存3 组织
  -> 存4 - 北头
  -> 轮/修库目标群展开
  -> 存4 - 16 北头摘
  -> 机 + 18 接
  -> 修库内摘解
  -> 轮/油/库回
```

FlowEdge：

```text
subject = 去修库/轮主目标群
from_area = 存2/存5/存3
via_port = 存4
to_receiver = 修库/轮
status:
  DISCOVERED
  -> APPROACHING_PORT
  -> PORT_READY
  -> ACCEPTED
  -> DIGESTING
  -> DONE
```

关键 ContractDelta：

```text
APPROACHING_CUN4_REPAIR_CONTRACT -> PORT_READY_REPAIR_CONTRACT:
  satisfied_clauses = FORM_RELEASE_GROUP
  reduced_clauses = KEEP_CUN4_PORT_USABLE

PORT_READY_REPAIR_CONTRACT -> MACHINE_ACCEPTED_REPAIR_CONTRACT:
  satisfied_clauses = MACHINE_ACCEPT
  activated_clauses = KEEP_ACCEPTED_TRAIN_INTACT

MACHINE_ACCEPTED_REPAIR_CONTRACT -> DEPOT_DIGEST_CONTRACT:
  reduced_clauses = DEPOT_DETACH
```

算法可以不复刻人工钩序，但必须实现这些边状态推进。

### 18.2 `0310W`：late cun4

人工后半段：

```text
修库展开
  -> 存4 - 14 北头摘
  -> 机 + 14 接
  -> 修库摘解
  -> 库回
```

FlowEdge：

```text
subject = 已展开的修库目标群
from_area = 修库侧/存4侧
via_port = 存4
to_receiver = 机/修库
status = PORT_READY
```

关键判断：

```text
因为 status 已经是 PORT_READY，
所以 OUTER_MAIN_PICKUP 不会带来 status_progress_gain。
它反而触发 FORCE_OUTER_PICKUP_ON_PORT_READY_EDGE。
```

### 18.3 `0104W / 0213W`：direct repair entry

FlowEdge：

```text
subject = 近修库侧主目标群
from_area = repair_entry
via_port = 存4 或 NO_EXPLICIT_PORT
to_receiver = 修库
status = APPROACHING_PORT 或 DIGESTING
```

关键判断：

```text
不要求从 DISCOVERED 重新开始。
不强制补外场。
允许就地形成 DIGESTING 或 PORT_READY。
```

错误动作：

```text
把近库结构拉回外场。
```

对应坏 delta：

```text
status 倒退
new_obligation = OUTER_PICKUP
subject dispersion 上升
```

### 18.4 `0130Z / 0201W / 0306W`：信号缺口

FlowEdge：

```text
subject = 由后续机接/摘解反推的主流
via_port = 存4 或 NO_EXPLICIT_PORT
to_receiver = 机/修库
status = DISCOVERED / PORT_READY / ACCEPTED 之一
confidence = medium 或 low
evidence.contradictions = true
```

关键判断：

```text
允许通过后续摘解承认 ALREADY_CROSSED。
不允许低置信主动生成 MACHINE_ACCEPT。
```

硬违反：

```text
HALF_READY_MACHINE_ACCEPT
```

### 18.5 `0103W / 0223W`：无标准机接但后段明确

FlowEdge：

```text
subject = 已进入修库摘解层的主目标群
via_port = NO_EXPLICIT_PORT 或 已跨过
to_receiver = 修库
status = DIGESTING
obligations = DEPOT_DETACH, WHEEL_INTERLEAVE_RESOLVE
```

关键判断：

```text
status != DONE
所以不能 closeout。
```

硬违反：

```text
CLOSEOUT_BEFORE_EDGE_DONE
MISCLASSIFY_DIGESTING_EDGE_AS_TAIL
```

---

## 19. 和 ShuntingStructure 的关系

`FlowGraph / FlowEdge` 不是否定 `ShuntingStructure`，而是收敛它。

如果实现时发现某些字段需要展开，可以从 `FlowEdge` 内局部派生：

```text
edge.subject -> MainChainView
edge.via_port -> PortView
edge.to_receiver -> ReceiverView
edge.obligations -> DebtView
edge.protections -> LockView
```

但这些 view 不应该成为新的主裁决中心。

主裁决仍然是：

```text
ContractDelta 是否履行目标 FlowEdge 的 EdgeContract。
```

---

## 20. 最小实现顺序

### 第一步：构建 FlowGraph，不改求解

输出：

```text
FlowGraph:
  edges
  residuals
  evidence
  online_status_reason
  online_contract_reason
  online_resource_state
```

每条 edge 输出：

```text
subject
from_area
via_port
to_receiver
status
blockers
obligations
protections
confidence
contract
status_source = ONLINE_OBSERVABLE
```

验收：

- 输入只允许 StartStatus，不允许 manual_order_signals。
- 每条 active edge 的 status 必须给出在线几何/资源理由。
- `PORT_READY / ACCEPT_READY / DIGESTING / DONE` 不能由未来人工动作推出。
- 人工标签只能作为 hidden_manual_label_comparison。

### 第二步：实现 ContractDelta

对候选动作模拟：

```text
before_edge
after_edge
contract_delta
```

先只观察：

- 当前 solver 选中的动作是否履行目标 contract。
- 人工认为错误的动作是否破坏 contract 或新增合同债务。

### 第三步：接入硬违反

先接：

```text
HALF_READY_MACHINE_ACCEPT
CLOSEOUT_BEFORE_EDGE_DONE
BREAK_ACCEPTED_TRAIN
DIRTY_RELEASE_PORT
MISCLASSIFY_DIGESTING_EDGE_AS_TAIL
FORCE_OUTER_PICKUP_ON_PORT_READY_EDGE
```

### 第四步：接入 ContractDelta scoring

只在无硬违反的候选里评分。

优先级：

```text
satisfied_clause > reduced_clause > blocker_reduction > hook_count
```

### 第五步：人工案例压测

必须覆盖：

```text
0117Z
0310W
0104W
0213W
0130Z
0201W
0306W
0103W
0223W
```

每个 case 输出：

```text
flow_graph_summary
target_edge
target_contract
candidate_contract_delta_summary
accepted_contract_delta
rejected_contract_delta_with_reason
```

---

## 20A. 结构级审计标准：每个结构如何判定达标

本章是本方案的文档级验收口径。它不是结构名词解释，而是用来判断本方案是否真的具备达到甚至超越人工的量化标准。

方案是否可行，不能主要靠最终案例是否跑出类似人工计划的序列来证明。最终案例对照只能证明端到端效果，不能替代结构审计。

真正的可行性应先看本方案提出的每个关键结构在每个求解过程里是否达标：

```text
输入足够明确
输出足够明确
职责边界清楚
局部决策逻辑成立
硬约束可判定
失败条件可识别
性能上不会把求解拖垮
能解释人工案例中的结构价值
指标可量化、可复现、可失败定位
```

只有这些结构逐项达标，端到端案例对照才有意义。

人工案例研究文档只提供人工结构基准，例如存4释放、机接、修库摘解、库回、大库 swap、功能线和调棚服务。本文档负责把这些人工基准转成方案结构标准。

### 20A.1 审计结论分级

| 等级 | 名称 | 含义 | 是否可声称达到人工 | 是否可声称超越人工 |
|---|---|---|---|---|
| L0 | 概念设想 | 有方向，但缺业务闭环和证据链 | 否 | 否 |
| L1 | 结构方案 | 识别了主要业务结构，能解释为什么这样建模 | 否 | 否 |
| L2 | 结构可审计方案 | 覆盖主业务流、硬约束、异常族，并列出关键结构输入输出和失败边界 | 否，只能说具备实现基础 | 否 |
| L3 | 结构可行方案 | 每个关键结构都有可实现逻辑、性能边界和局部验收标准 | 可以说具备达到人工的结构条件 | 否，只能说存在超越空间 |
| L4 | 集成可验证方案 | 关键结构通过局部验证，并有端到端案例对照 | 可以 | 在多指标支配人工时才可以 |

本文档的目标是达到 L3，并为 L4 的集成验证提供审计标准。

### 20A.2 业务覆盖标准

方案必须能解释完整链路：

```text
存5/外场接车
  -> 信息判定
  -> 预修/调棚/功能线/存车整理
  -> 大库入库前组织
  -> 存4北释放
  -> 机接
  -> 修1-修4库内摘解/对位
  -> 大库出库到存4北
  -> 尾项收束
```

必须明确这些对象在结构中的角色：

| 对象 | 文档必须说明 |
|---|---|
| 存4北 | 既是出段集结线，也是大库入库释放口，存在双向争抢 |
| 存4南 | 只能临停，不能当正式出段终点 |
| 预修/机棚 | 是预修能力，不是普通尾项 |
| 调棚 | 同时有调梁工位和预修尽头位，是复合线路 |
| 洗/油/抛/轮 | 是功能线/运营线，有服务完成语义 |
| 修1-修4库内 | 是大库作业线，涉及台位、出入库 swap 和摘解顺序 |
| 修1-修4库外 | 是进出库衔接，不等同库内检修 |
| 机库/机区 | 涉及机车位置、称重、缓冲、承接端别 |
| 联6/联7 | 是全局门控，不属于某一个局部流程 |
| 存1/存2/存3/存5 | 是存车整理和组流空间，不是低级 residual |

如果方案只解释“预修到大库”或“存4到大库”，但不能解释功能线、调棚、存车整理、库内出库、尾项收束，则不能通过文档级审核。

### 20A.2A 量化验收口径

本章所有标准分三层：

| 层级 | 含义 | 结论 |
|---|---|---|
| 硬门槛 | 触发即失败，不能被少钩或可解性抵消 | 不通过 |
| 达到人工线 | 结构能力不弱于人工基准，允许钩数不一定更优 | 可声称具备达到人工的结构条件 |
| 超越人工线 | 在达到人工线之上，多指标支配人工或显著减少搜索/返工 | 可声称具备超越人工空间 |

量化口径必须按三种粒度输出：

| 粒度 | 必须输出 |
|---|---|
| step 级 | 当前 FlowGraph、target_contract、intent、candidate_count、ContractDelta、ResourceDelta、AcceptReject 结果 |
| case 级 | 覆盖率、residual 比例、硬违规数量、人工关键结构识别结果、钩数、运行时间、失败桶 |
| batch 级 | 通过案例数、失败分布、P50/P90/P95 运行时间、平均 residual、平均候选数量、结构漏识别率 |

最低 trace 字段：

```text
case_id
step_index
vehicle_count
movable_vehicle_count
active_contract_count
effective_contract_coverage
residual_vehicle_ratio
target_contract
target_contract_reason
structural_intent
candidate_family_count
candidate_count
resource_request_count
contract_delta_summary
resource_delta_summary
accepted
reject_reason
failure_bucket
hook_count_so_far
runtime_millis
```

禁止用定性话术替代指标。例如：

```text
不能只写：能识别大部分主体流。
必须写：effective_contract_coverage >= 95%，residual_vehicle_ratio <= 5%。

不能只写：候选被结构约束。
必须写：candidate_family_count <= 8，forbidden_candidate_to_scoring_count = 0。

不能只写：资源冲突可解释。
必须写：resource_delta_coverage = 100%，hard_resource_violation_accepted_count = 0。
```

### 20A.2B 分过程量化验收矩阵

这个矩阵是本章的核心。它要求每个结构不仅“存在”，而且在每个求解过程里达到可测门槛。

| 过程 | 参与结构 | 硬门槛 | 达到人工线 | 超越人工线 | 失败桶 |
|---|---|---|---|---|---|
| P0 在线证据边界 | `OnlineObservable / OfflineLabelOnly`, `FlowGraphBuilder` | `offline_label_used_online_count = 0`; `unknown_status_source_count = 0`; `online_status_reason_coverage = 100%` | 12 个代表案例 status reason 覆盖 100%；低信号案例必须输出 confidence 和 contradiction | 在人工动作发生前识别存4、库位、联7、端别冲突，`pre_action_conflict_detection_rate >= 90%` | `EVIDENCE_LEAKAGE`, `STATUS_REASON_MISSING` |
| P1 车辆分类 | `FlowClassify`, `NoMoveVehicle`, `ResidualItem` | 12 个代表案例 `effective_contract_coverage >= 90%`; `residual_vehicle_ratio <= 10%`; `no_move_blocks_key_resource_count = 0` | `truth2_force` 研究目标 `effective_contract_coverage >= 95%`; `residual_vehicle_ratio <= 5%`; residual reason 覆盖 100% | `residual_vehicle_ratio <= 3%`; 非修库主体族覆盖率 `>= 95%`; no-move 避免无效动车有可量化收益 | `FLOW_CLASSIFY_LOW_COVERAGE`, `RESIDUAL_OVERFLOW`, `NO_MOVE_FALSE_SATISFIED` |
| P2 建边与身份稳定 | `FlowGraph`, `FlowEdge`, `EdgeKey`, `FlowSubject`, `FlowPort`, `FlowReceiver` | `subject_overlap_count = 0`; support edge `owner_edge_id_coverage = 100%`; edge identity drift 无理由漂移 `= 0` | 主体业务流都能输出 `subject / via_port / to_receiver / status / evidence`; edge identity 稳定率 `>= 95%` | 能提前识别 SEED/FORMING 主流并保护关键口；高价值主流漏识别率 `<= 2%` | `EDGE_MISIDENTIFIED`, `EDGE_ID_DRIFT`, `SUBJECT_OVERLAP` |
| P3 状态与合同生成 | `FlowEdgeStatus`, `EdgeContract`, `ContractTemplate`, `StationFlowContract`, `RepairInboundVariant` | `illegal_status_transition_count = 0`; 低置信主动跨 `ACCEPTED` 次数 `= 0`; `ACCEPTED` 后 protection 激活率 `= 100%` | `0117Z / 0310W / 0103W / 0213W / 0306W` 对应 variant 识别正确；每个 active edge 合同条款完整率 100% | late 存4、直接入库、库内消化均不被强套长链；异常变体误判率 `<= 5%` | `STATUS_GUARD_BROKEN`, `CONTRACT_TEMPLATE_MISSING`, `VARIANT_MISCLASSIFIED` |
| P4 目标合同与结构意图 | `TargetContractSelector`, `StructuralIntentBuilder`, `Blocker`, `Obligation`, `Protection` | `target_contract_reason_coverage = 100%`; 后边界合同被低优先级合同破坏次数 `= 0`; hard obligation 被跳过次数 `= 0` | `PORT_READY` 优先机接/入库，`DIGESTING` 优先摘解/消化，`DEPOT_SLOT` 被占优先释放出库 | target 选择能量化释放资源收益；被选合同的下游解锁合同数不低于人工同阶段动作 | `TARGET_CONTRACT_WRONG`, `INTENT_WRONG`, `HARD_OBLIGATION_SKIPPED` |
| P5 候选生成 | `WorkPatternTemplateSelector`, `EdgeBoundedCandidateGenerator`, `ResourceRequest` | `forbidden_candidate_to_scoring_count = 0`; `candidate_family_count <= 8`; `planlet_horizon <= 3`; `resource_request_coverage = 100%` | 必须候选族存在率 100%，例如 `PORT_READY` 有 `MACHINE_ACCEPT / DEPOT_ENTRY`，`DIGESTING` 有 `DEPOT_DETACH` | 相比无边界候选，候选数量下降 `>= 50%`，且必需候选不丢失 | `CANDIDATE_MISSING`, `CANDIDATE_UNBOUNDED`, `RESOURCE_REQUEST_MISSING` |
| P6 资源仲裁 | `StationResourceGraph`, `StationResource`, `CUN4_NORTH_BUFFER`, `DepotSlotGraph`, `GlobalGate`, `LocoPosition`, `LocoCarryState`, `WaitForGraph` | `hard_resource_violation_accepted_count = 0`; `depot_slot_request_coverage = 100%`; `gate_conflict_accepted_count = 0`; `enum_only_carry_enabled = false` | 存4北四态识别 100%；大库入库必须输出 slot/band 或 swap status；联6/联7 必须输出 gate slot 申请结果 | `deadlock_cycle_detected_coverage = 100%`; 合法 break action 输出率 100%；swap 早识别率 `>= 95%` | `RESOURCE_CONFLICT_ACCEPTED`, `DEPOT_SWAP_MISSING`, `GATE_CONFLICT`, `DIRTY_CARRY` |
| P7 Delta 与硬门 | `EdgeDelta`, `ContractDelta`, `ResourceDelta`, `AcceptRejectGate` | `hard_clause_accepted_count = 0`; `resource_violation_accepted_count = 0`; `delta_required_field_coverage = 100%`; `why_reject_coverage = 100%` | 人工关键动作能被解释为正 delta；人工错误模式能被解释为 reject delta | 算法动作可不同于人工，但 `contract_gain >= human_same_phase_gain`，且 dominated accepted rate `= 0` | `CONTRACT_DELTA_SIM_WRONG`, `GATE_TOO_LOOSE`, `GATE_TOO_STRICT` |
| P8 同结构少钩 | `ContractOptimizer`, `LocalTieBreakSearch`, `hook_count` | 只允许同 contract、同 intent、同结构等级比较；`search_scope_violation_count = 0`; `local_branch_count <= 64` | 标准链钩数 `<= manual_hook_count * 1.05`; 短链/直接入库 `<= manual_hook_count + 1` | 同结构价值下钩数低于人工，或钩数相同但道岔/空驶/存4污染/库位冲突更少 | `LOCAL_SEARCH_DOMINATED`, `HOOK_OVERRIDES_STRUCTURE`, `SEARCH_SCOPE_EXPANDED` |
| P9 状态更新与 trace | `State Update`, `FlowGraph Rebuild`, `Trace`, `failure_bucket` | 每步后 rebuild 覆盖 100%；车辆守恒 `= 100%`; `failure_bucket_coverage = 100%`; `trace_missing_count = 0` | 每个失败能定位到一个主失败桶和最多两个辅助失败桶 | 失败定位可直接指向可修结构，重复未知失败率 `<= 2%` | `TRACE_MISSING`, `STATE_INCONSISTENT`, `FAILURE_UNCLASSIFIED` |
| P10 全量压测 | 全部结构 | `hard_violation_count = 0`; `final_target_satisfied = true`; 单案 `runtime <= 300s` | 代表案例族全部通过；`truth2_force` 覆盖率和 residual 达到 P1 研究目标 | 多指标支配人工：少钩、少空驶、少污染、少 swap 冲突中至少 2 项优于人工 | `CASE_FAILED`, `RUNTIME_EXCEEDED`, `END_TO_END_REGRESSION` |

注意：P10 不能替代 P0-P9。一个案例最终跑通，但中间结构指标失败，仍然不能证明方案可行。

### 20A.2C 指标公式

为避免“覆盖率”“稳定”“可解释”变成定性说法，本文档统一使用以下公式。

车辆口径：

```text
vehicle_count = 当前案例车辆总数
movable_vehicle_count = 目标未满足或虽已在目标线但阻塞关键资源的车辆数
no_move_vehicle_count = 已满足目标且不阻塞关键资源的车辆数
contracted_movable_vehicle_count = 被非 RESIDUAL 主体合同覆盖的需动车数
residual_movable_vehicle_count = 被 RESIDUAL 覆盖的需动车数
```

覆盖率：

```text
gross_contract_coverage =
  contracted_vehicle_count / vehicle_count

effective_contract_coverage =
  contracted_movable_vehicle_count / max(1, movable_vehicle_count)

residual_vehicle_ratio =
  residual_movable_vehicle_count / max(1, movable_vehicle_count)

no_move_false_satisfied_rate =
  no_move_blocks_key_resource_count / max(1, no_move_vehicle_count)
```

识别与状态：

```text
online_status_reason_coverage =
  status_with_online_reason_count / max(1, active_status_count)

edge_identity_stability =
  1 - unexplained_edge_id_change_count / max(1, edge_identity_observation_count)

contract_clause_completeness =
  contracts_with_required_clauses_count / max(1, active_contract_count)

variant_accuracy_on_representative_cases =
  correctly_classified_variant_case_count / representative_variant_case_count
```

候选与搜索：

```text
necessary_candidate_recall =
  generated_required_candidate_family_count / required_candidate_family_count

candidate_prune_rate =
  1 - bounded_candidate_count / max(1, raw_candidate_count)

forbidden_candidate_leak_rate =
  forbidden_candidate_to_scoring_count / max(1, bounded_candidate_count)

dominated_accepted_rate =
  dominated_accepted_candidate_count / max(1, accepted_candidate_count)
```

资源与 delta：

```text
resource_request_coverage =
  candidates_with_resource_request_count / max(1, generated_candidate_count)

resource_delta_coverage =
  candidates_with_resource_delta_count / max(1, simulated_candidate_count)

delta_required_field_coverage =
  deltas_with_required_fields_count / max(1, simulated_candidate_count)

why_reject_coverage =
  rejected_candidates_with_reason_count / max(1, rejected_candidate_count)
```

trace 与失败：

```text
trace_field_coverage =
  present_required_trace_field_count / required_trace_field_count

failure_bucket_coverage =
  classified_failure_count / max(1, failure_count)

unknown_failure_repeat_rate =
  repeated_unknown_failure_count / max(1, failure_count)
```

硬门槛统一要求：

```text
offline_label_used_online_count = 0
hard_clause_accepted_count = 0
hard_resource_violation_accepted_count = 0
illegal_status_transition_count = 0
subject_overlap_count = 0
search_scope_violation_count = 0
trace_missing_count = 0
```

### 20A.2D 结构 x 过程量化验收表

这张表把每个结构放回它参与的求解过程。审核时不能只看某个结构“有没有定义”，必须看它在对应过程里是否输出了可测指标。

| 结构 | 过程 | 必须输出 | 硬门槛 | 达到人工线 | 超越人工线 |
|---|---|---|---|---|---|
| `OnlineObservable / OfflineLabelOnly` | P0 | online feature list、offline label list、status_source | `offline_label_used_online_count = 0` | `online_status_reason_coverage = 100%` | 低信号案例仍能输出 confidence/contradiction，缺失原因覆盖 100% |
| `FlowGraphBuilder` | P0/P9 | feature extraction summary、build reason、rebuild diff | `unknown_status_source_count = 0` | 代表案例集 rebuild 成功率 100% | 单步 rebuild 后可直接定位新增/消失 edge 原因，unknown diff `<= 2%` |
| `FlowClassify` | P1 | contract family per movable vehicle、reason、priority conflict | 代表案例 `effective_contract_coverage >= 90%` | `truth2_force effective_contract_coverage >= 95%` | `effective_contract_coverage >= 97%` 且非修库主体族覆盖率 `>= 95%` |
| `NoMoveVehicle` | P1 | no-move reason、blocked resource check | `no_move_blocks_key_resource_count = 0` | `no_move_false_satisfied_rate = 0` | 避免无效动车有 case 级 hook saving 或候选减少证据 |
| `ResidualItem` | P1/P9 | suspected_role、reason_unbound、expiry_condition、max_age | `residual_vehicle_ratio <= 10%` | 研究目标 `residual_vehicle_ratio <= 5%`; residual reason 覆盖 100% | `residual_vehicle_ratio <= 3%`; residual 平均生命周期不超过 3 次 rebuild |
| `FlowGraph` | P2/P9 | active_edges、contract_groups、residuals、contradictions | `subject_overlap_count = 0` | 主体业务流输出完整率 100% | 能提前暴露并发冲突，pre-action conflict detection `>= 90%` |
| `FlowEdge` | P2/P3 | edge_key、subject、from_area、via_port、to_receiver、status、evidence | active edge 必填字段覆盖 100% | 人工关键结构 edge 识别率 100% | 高价值主流漏识别率 `<= 2%` |
| `EdgeKey` | P2/P9 | edge identity history、change reason | 无理由 identity drift `= 0` | `edge_identity_stability >= 95%` | `edge_identity_stability >= 98%` |
| `FlowSubject` | P2/P5 | subject role、vehicle order、seed/forming/bound | 同车 active primary subject 重叠 `= 0` | subject role 覆盖 100% | 在 FORMING 阶段提前生成保护或清口 intent 的比例 `>= 90%` |
| `FlowPort` | P2/P6 | port role、health、blocking vehicles | dirty/blocked port 直接跨越 `= 0` | 存4北、联线、修库入口 health 覆盖 100% | 关键口污染提前一步识别率 `>= 90%` |
| `FlowReceiver` | P2/P6 | receiver capacity、digest_state、pending obligations | receiver capacity unknown 下强行 accept `= 0` | 修库、预修、调棚、功能线 receiver state 覆盖 100% | 入接收端后新增返工 obligation 比人工少 |
| `FlowEdgeStatus` | P3/P9 | status、entry_reason、guard_result、confidence | `illegal_status_transition_count = 0`; 低置信跨 `ACCEPTED` `= 0` | 代表案例 status path 可解释率 100% | late/short/direct case 不倒退补长链，误判率 `<= 5%` |
| `EdgeContract` | P3/P7 | must_progress、must_not_break、must_finish_before_done、forbidden_moves | `contract_clause_completeness = 100%` | 人工核心承诺都能表达为条款 | 短期少钩破坏后续结构的候选拒绝率 100% |
| `ContractTemplate` | P3 | template family、required clauses、scope | 主体合同族 template 缺失 `= 0` | 9 个合同族都有模板 | 非修库主体族同样输出 delta 和 resource request |
| `StationFlowContract` | P1/P3/P4 | active contract set、family count、priority hints | `REPAIR_INBOUND` 之外主体流全部 residual 失败 | 主体合同族覆盖率 `>= 95%` | 多合同并发能输出资源等待和解锁收益 |
| `RepairInboundVariant` | P3/P4 | variant、allowed_shortcuts、forbidden_moves | 标准链/短链/消化链混淆导致硬边界错误 `= 0` | `0117Z / 0310W / 0103W / 0213W / 0306W` variant 正确 | 代表变体准确率 100%，全量异常变体误判率 `<= 5%` |
| `Blocker` | P4/P5 | blocker type、blocked clause、clear intent | hard blocker ignored `= 0` | blocker reason 覆盖 100% | blocker 清理优先级能解释解锁资源数或减少债务数 |
| `Obligation` | P4/P7/P9 | owner、type、lifecycle、completion condition | hard obligation skipped `= 0` | obligation lifecycle 覆盖 100% | 清债顺序不弱于人工同阶段动作 |
| `Protection` | P3/P7 | active_from_status、break_violation、release_condition | active protection broken accepted `= 0` | `ACCEPTED` 后 protection 激活率 100% | 对短期少钩破坏保护的候选拒绝率 100% |
| `TargetContractSelector` | P4 | selected contract、rank reason、suppressed contracts | `target_contract_reason_coverage = 100%`; hard priority inversion `= 0` | `PORT_READY`、`DIGESTING`、slot release 等高优先合同选择正确 | 被选合同下游解锁合同数不低于人工同阶段动作 |
| `StructuralIntentBuilder` | P4/P5 | intent、source clauses、forbidden intents | intent 无合同来源 `= 0` | intent 与 target contract 匹配率 100% | intent 使候选族减少且不丢必要候选 |
| `WorkPatternTemplateSelector` | P5 | template list、template reason、applicability | pattern 绕过 contract/resource gate `= 0` | 每个 intent 可用模板数 `1..6` | 同 intent 下模板覆盖人工动作族，且不复刻固定钩序 |
| `EdgeBoundedCandidateGenerator` | P5 | candidate families、candidate count、forbidden filter | `forbidden_candidate_to_scoring_count = 0`; `planlet_horizon <= 3` | `necessary_candidate_recall = 100%`; `candidate_family_count <= 8` | 相比 raw candidate `candidate_prune_rate >= 50%` |
| `ResourceRequest` | P5/P6 | requested_resources、blocked_resources、request reason | `resource_request_coverage = 100%` | 关键资源申请覆盖 100% | 资源不可得候选进入 delta 前拦截率 100% |
| `StationResourceGraph` | P6 | occupied_by、requested_by、waiting contracts、release_condition | `hard_resource_violation_accepted_count = 0` | 所有关键资源占用/等待/释放字段覆盖 100% | 能输出资源解锁收益，支持 target contract 排序 |
| `CUN4_NORTH_BUFFER` | P6/P7 | direction_mode、dirty_reason、clear_intent | `MIXED_DIRTY` 直接机接/释放 `= 0` | 四态识别覆盖 100% | 存4污染提前一步识别率 `>= 90%`，清口钩数不高于人工 |
| `DepotSlotGraph / DepotSwapDelta` | P6/P7 | slot/band state、blocked_by、released_slot、reserved_slot、swap status | `DEPOT_SLOT_SWAP_VIOLATION accepted = 0`; slot request 覆盖 100% | 入库动作 swap status 覆盖 100% | slot blocked 早识别率 `>= 95%`，swap 冲突少于人工 |
| `GlobalGate` | P6 | gate slot request、occupied_by、waiting contracts | gate conflict accepted `= 0` | 联6/联7 跨门动作 request 覆盖 100% | gate 等待和切换次数不高于人工 |
| `LocoPosition` | P4/P6/P8 | track、side、accessible_end、distance_to_target_contract | unknown loco position 下执行需端别动作 `= 0` | 目标合同选择距离/端别字段覆盖 100% | 同结构下空驶距离不高于人工 |
| `LocoCarryState / ordered carry segments` | P6/P8 | carry_state、ordered segments、detachable_from_tail | `enum_only_carry_enabled = false`; invalid detach accepted `= 0` | carry action ordered segment 覆盖 100% | 错误端别/脏携带导致返工 `= 0`，同结构少钩不弱于人工 |
| `WaitForGraph / DeadlockDetection` | P6/P9 | cycle、break_contract、break_action_family | cycle ignored `= 0` | synthetic/resource cycle 检测覆盖 100% | 合法 break action 输出率 100%，break hook cost 不高于人工经验动作 |
| `EdgeDelta` | P7 | before/after edge、good/bad changes、violations | edge hard violation accepted `= 0` | 边状态变化解释字段覆盖 100% | 明显破坏边结构动作在合同评分前拦截率 100% |
| `ContractDelta` | P7 | satisfied/reduced/broken/added clauses、why_accept/why_reject | hard clause accepted `= 0`; required fields 覆盖 100% | 人工关键动作正 delta 识别率 100% | `contract_gain >= human_same_phase_gain`，dominated accepted `= 0` |
| `ResourceDelta` | P7 | requested/acquired/released/blocked/violations | resource violation accepted `= 0`; coverage 100% | 关键资源变化解释覆盖 100% | 提前发现等待和让步动作，死锁前置暴露率 `>= 95%` |
| `AcceptRejectGate` | P7 | accept/reject、hard reasons、dominance reason | hard violation accepted `= 0`; why_reject 覆盖 100% | 人工安全边界候选放行/拒绝一致 | 短期少钩长期返工候选拒绝率 100% |
| `ContractOptimizer / hook_count` | P8 | structure rank、hook_count、dominance comparison | hook 覆盖结构硬约束 `= 0` | 标准链 `<= manual_hook_count * 1.05`; 短链 `<= manual_hook_count + 1` | 同结构价值下钩数低于人工或其他成本至少 2 项更优 |
| `LocalTieBreakSearch` | P8 | horizon、branch count、tie-break reason | `search_scope_violation_count = 0`; `local_branch_count <= 64` | 只在同 contract、同 intent、同结构等级内比较 | P50 局部搜索候选数比无界搜索减少 `>= 50%` |
| `Trace / failure_bucket` | P9/P10 | required fields、primary failure bucket、secondary buckets | `trace_missing_count = 0`; `failure_bucket_coverage = 100%` | 每个失败能定位到一个主失败桶和最多两个辅助桶 | 重复 unknown failure rate `<= 2%` |

### 20A.2E 过程通过条件

每个过程必须同时满足“字段完整、硬门槛、人工线、性能线”四类条件。

| 过程 | 字段完整 | 硬门槛 | 人工线 | 性能线 |
|---|---|---|---|---|
| P0 | status_source、online_reason 覆盖 100% | offline label 使用 0 | 代表案例 status 可解释 100% | 单次识别不触发候选搜索 |
| P1 | 每辆需动车有 class reason | residual <= 10% | residual <= 5%，coverage >= 95% | 车辆级线性分类 |
| P2 | active edge 必填字段覆盖 100% | subject overlap 0 | edge stability >= 95% | 构图近线性 |
| P3 | 合同条款完整率 100% | illegal transition 0 | 代表变体识别 100% | 合同模板数量有限 |
| P4 | target reason 覆盖 100% | hard obligation skip 0 | 高优先合同选择正确 | active contract 排序 |
| P5 | candidate/resource request 覆盖 100% | forbidden leak 0 | necessary candidate recall 100% | family <= 8，horizon <= 3 |
| P6 | resource state 覆盖 100% | resource violation accepted 0 | slot/gate/carry 全覆盖 | resource graph 局部判断 |
| P7 | delta/reject reason 覆盖 100% | hard accepted 0 | 人工关键动作 delta 可解释 | 单候选或短 planlet 模拟 |
| P8 | tie-break reason 覆盖 100% | scope violation 0 | 钩数不弱于人工容忍线 | local_branch_count <= 64 |
| P9 | trace/failure bucket 覆盖 100% | state inconsistent 0 | 失败可定位 | 不回扫全局计划 |
| P10 | case summary 覆盖 100% | hard violation 0，runtime <= 300s | 代表案例族全部通过 | batch P95 runtime <= 300s |

### 20A.3 结构标准总表

这张表是结构索引，不是完整验收标准。完整验收必须同时看 20A.2B 的过程矩阵。

任一关键结构不达标，都不能用最终案例偶然跑通来替代。

| 结构 | 必须解决的问题 | 达到人工的最低标准 | 超越人工的结构标准 | 否决条件 |
|---|---|---|---|---|
| `FlowGraphBuilder` | 从当前现场稳定识别全站业务流 | 只用当前状态、目标、修程、资源、机车位置和已执行动作生成 `FlowGraph` | 能比人工更早暴露并发流之间的冲突 | 用未来人工动作反推在线 status |
| `OnlineObservable / OfflineLabelOnly` | 区分在线输入和离线标签 | 在线只用当前现场和已执行动作，人工未来动作只作评估标签 | 防止信息泄漏，使方案可真实上线 | 用人工计划未来动作决定当前状态 |
| `FlowGraph` | 保存全站 active edge、residual、证据和矛盾 | 同时表达入库、出库、预修、调棚、功能线、存车整理 | 提前发现多业务流互相阻塞 | 只保存修库主链 |
| `FlowClassify` | 把车辆归入主体合同族 | 主体车流不掉入 residual，研究目标 `effective_contract_coverage >= 95%` | 稳定区分不动车、当前主流、下一轮候选和功能未完成车 | 用 `NO_MOVE` 虚增覆盖率，或 residual 承接主体车流 |
| `NoMoveVehicle` | 区分真正不动车和被忽略动车 | 已达目标且不阻塞资源才可归入 no-move | 避免无意义动车，减少钩数 | 用 no-move 掩盖未建模主体车流 |
| `ResidualItem` | 暂存低证据边角车 | 有 suspected_role、reason、expiry_condition、max_age | 能快速归边、转 support、转 blocker 或降噪 | residual 永久存在且不影响决策 |
| `FlowEdge` | 表达一条业务流的身份、状态和接收端 | 能解释组织、存4释放、机接、修库摘解、库回 | subject 未完全成形时提前保护关键口 | edge 身份漂移或多条边抢同一 subject |
| `EdgeKey` | 稳定表达业务边身份 | 同一业务流在车辆集合变化时身份不漂移 | 支撑跨步追踪和失败复盘 | 每步重新命名导致合同断裂 |
| `FlowSubject` | 表达 subject 从 seed 到 forming 到 bound 的生命周期 | 能区分主流种子、成形主流、已绑定主流 | 提前保护未完全成形但高价值的主流 | 一开始就要求 subject 完全确定 |
| `FlowPort` | 表达关键口角色和健康度 | 存4、联线、修库入口等口能判断 usable、dirty、blocked | 提前清口或选择替代口 | port 只是线路名，无健康度 |
| `FlowReceiver` | 表达接收端能力和消化状态 | 修库、预修、调棚、功能线能判断能否接收和是否欠债 | 减少进入接收端后的返工 | 接收端只看目标线容量 |
| `FlowEdgeStatus` | 表达业务流推进位置 | `PORT_READY / ACCEPTED / DIGESTING / DONE` 有在线进入条件 | 避免人工隐式阶段误判 | 低置信主动跨 `ACCEPTED` |
| `EdgeContract` | 定义当前必须履约和不能破坏的条款 | 能表达存4口、机接保护、修库债务 | 把人工经验固化为可判定硬边界 | 合同条款由阶段名或评分黑箱替代 |
| `ContractTemplate` | 把合同族转成可复用条款 | 每个合同族有 must_progress、must_not_break、forbidden_moves | 让人工经验可复用、可比较 | 只有自然语言描述，没有可审计条款 |
| `StationFlowContract` | 覆盖全站合同族 | 覆盖入库、出库、预修、调棚、功能线、存车整理、机区、特殊工艺、尾项 | 多合同并发时能进入资源仲裁 | 只覆盖 `REPAIR_INBOUND` |
| `RepairInboundVariant` | 区分入库结构变体 | 支撑完整链、late 存4、直接入库、低信号、库内消化 | 不把短链强拉成长链，不把无机接消化误收尾 | 所有入库都套标准长链 |
| `Blocker / Obligation / Protection` | 表达挡点、债务和保护 | 能解释清存4、清机区、修库摘解未完 | 能选择最值得先清的 blocker | 机接后主列、释放口、修库债务无硬保护 |
| `EdgeDelta / EdgeViolation` | 判断候选动作对边状态的影响 | 能识别 status progress、subject split/merge、硬违反 | 在合同前就剔除明显破坏边结构的动作 | 只看目标线变化，不看边结构破坏 |
| `TargetContractSelector` | 选择当前最该履约的合同 | 后边界、硬债务、关键资源优先于早期组织 | 按资源释放价值和硬时序选择，比人工计划更少返工 | 黑箱评分决定主合同 |
| `StructuralIntentBuilder` | 把合同下一步转成结构目标 | 先决定开入口、清资源口、机接、消化、收束等意图 | 用结构意图裁剪候选空间 | 直接从 pattern 或搜索决定动作 |
| `WorkPatternTemplateSelector` | 调用人工经验模板 | 只在 contract 和 intent 之后生成候选边界 | 保留人工套路但不复刻钩序 | pattern 成为事实主控 |
| `EdgeBoundedCandidateGenerator` | 生成受边、合同、资源约束的候选 | 不生成跨硬边界或明显违约候选 | 前置缩小搜索空间，减少错误候选污染 | 先生成全量动作再靠评分补救 |
| `ResourceRequest` | 候选动作先申请资源 | 申请存4北、gate、库位、机车位置、携带状态后才进入 delta | 在候选生成阶段过滤资源不可能动作 | 资源到执行后才发现冲突 |
| `ContractDeltaSimulator / ContractDelta` | 判断候选是否履约 | 输出 satisfied、reduced、broken、added、why_accept、why_reject | 允许不同于人工计划，但合同增益不低于人工动作 | hard clause 被 hook_count 抵消 |
| `StationResourceGraph` | 仲裁共享资源 | 存4北、联6/联7、大库 slot、功能线、机车位置都显式申请释放 | 发现人工未显式记录的等待、抢占和让步顺序 | 资源冲突只靠合同评分或搜索处理 |
| `StationResource` | 定义资源容量、方向、占用者、等待者和释放条件 | 每类共享资源都能说明谁占用、谁等待、何时释放 | 让资源等待可排序、可让步 | 资源只是散规则 |
| `CUN4_NORTH_BUFFER` | 处理存4北双向争抢 | 区分 `INBOUND_RELEASE / OUTBOUND_HOLD / MIXED_DIRTY / FREE` | 提前发现存4污染并少清口 | 存4北只当普通 via_port |
| `DepotSlotGraph / DepotSwapDelta` | 处理大库 slot/band swap | 能判断入库目标位、出库释放位、stayer 锁定 | 明确先出哪辆、预留哪个 slot/band | 大库只用线级容量 |
| `GlobalGate` | 处理联6/联7全局门控 | 跨门动作先申请 gate slot | 精确安排 gate slot，减少等待 | gate 冲突进入普通评分 |
| `LocoPosition` | 表达机车所在线、端别和可达性 | 合同选择考虑空驶、端别、跨门要求 | 同结构下少空驶、少道岔 | 合同选择不看机车位置 |
| `LocoCarryState / ordered carry segments` | 表达携带车列是否干净和可摘顺序 | 4 值枚举只作粗标签，保留有序段 | 稳定避免脏携带和错误端别 | enum-only 压缩上线 |
| `ResourceDelta` | 判断候选动作对资源的影响 | 输出 requested、acquired、released、blocked、violations | 提前发现资源等待和合法让步动作 | 不输出资源变化或资源硬违反 |
| `WaitForGraph / DeadlockDetection` | 发现合同和资源环形等待 | 输出 cycle、break_contract、合法让步动作族 | 系统化打破人工计划隐含死锁 | 只说“大家都在等” |
| `AcceptRejectGate` | 最终硬门 | ContractDelta、ResourceDelta、硬约束同时合法才放行 | 稳定拒绝短期少钩但长期返工的动作 | 为可解性或少钩放行硬违反 |
| `LocalTieBreakSearch` | 同结构等级内少钩 | 只在同一 contract、intent、模板边界内短 horizon 搜索 | 同结构价值下少钩、少空驶、少道岔 | 搜索自己发明主链或绕过合同 |
| `Trace / failure_bucket` | 输出可审计证据链 | 失败能定位到分类、合同、候选、delta、资源、gate 或搜索 | 比人工计划更可复盘、更可改进 | 只输出成功/失败或总钩数 |

### 20A.4 量化审核包

进入方案审核时，必须提交以下 5 个数据包。缺任一数据包，只能评为 L2，不能评为 L3。

| 数据包 | 内容 | 最低通过标准 |
|---|---|---|
| `process_metrics.csv` | 每个 case、每个 step 的 P0-P10 指标 | 必须覆盖 100% executed steps |
| `structure_metrics.csv` | 每个结构在 20A.2D 中的输出字段、硬门槛、人工线、超越线 | 20A.2D 全部结构必须有记录 |
| `hard_violation_report.csv` | 所有硬约束检查结果 | 所有 accepted action 的 hard violation 均为 0 |
| `case_family_report.csv` | 标准链、late 存4、直接入库、库内消化、低信号、功能线/调棚重案例的结果 | 每类至少有代表案例；代表案例必须全部通过硬门槛 |
| `failure_bucket_report.csv` | 失败桶分布、重复未知失败、对应结构 | `failure_bucket_coverage = 100%`，`unknown_failure_repeat_rate <= 2%` |

每个数据包必须能追溯到原始 case、step、structure。不能只提供汇总均值。

### 20A.5 硬约束量化表

硬约束和优化目标必须分开。硬约束不能被少钩、路径短、评分高抵消。

| 硬约束 | 结构承接 | step 级指标 | case 级通过线 |
|---|---|---|---|
| 最终目标满足 | `AcceptRejectGate`, `Trace` | `target_violation_after_move = 0` | `final_target_satisfied = true` |
| 大库台位合法 | `DepotSlotGraph`, `DepotSwapDelta`, `ResourceDelta` | `depot_slot_violation_accepted = 0` | `depot_slot_valid = true` |
| 牵引能力 | `ResourceRequest`, `ResourceDelta`, `AcceptRejectGate` | `traction_over_limit_accepted = 0` | `traction_valid = true` |
| 关门车顺位 | `ContractTemplate`, `ResourceDelta`, `AcceptRejectGate` | `close_door_order_violation_accepted = 0` | `close_door_rule_valid = true` |
| 称重规则 | `SPECIAL_REPAIR_PROCESS`, `ResourceDelta`, `AcceptRejectGate` | `weighing_rule_violation_accepted = 0` | `weighing_valid = true` |
| 临停容量 | `StationResourceGraph`, `ResourceDelta` | `buffer_capacity_overflow_accepted = 0` | `temporary_buffer_valid = true` |
| 运行干涉 | `LocoPosition`, `ResourceRequest`, `ResourceDelta` | `route_interference_accepted = 0` | `route_interference_valid = true` |
| 走行线禁停 | `StationResourceGraph`, `GlobalGate`, `ResourceDelta` | `running_line_storage_accepted = 0` | `running_line_valid = true` |
| 存4南非正式终点 | `CUN4_NORTH_BUFFER`, `StationResourceGraph`, `AcceptRejectGate` | `cun4_south_final_destination_accepted = 0` | `cun4_south_valid = true` |
| 联6/联7门控 | `GlobalGate`, `ResourceRequest`, `ResourceDelta` | `gate_conflict_accepted = 0` | `global_gate_valid = true` |

即使当前 `truth2` 中没有重车、关门车、称重车，也必须保留这些指标字段。没有样本时字段值为 `not_applicable`，不能删除字段。

### 20A.6 性能预算

业务文档要求求解时间限制为 5 分钟。结构审计必须证明复杂度没有从局部判断退化成全局排列。

| 过程 | 核心结构 | 单 step 预算 | case 级预算 | 失效判定 |
|---|---|---:|---:|---|
| P0 | `FlowGraphBuilder` | 近线性特征提取 | P95 不超过总耗时 10% | 构图阶段做候选搜索 |
| P1 | `FlowClassify` | `O(vehicle_count)` | P95 不超过总耗时 10% | 分类依赖车辆组合枚举 |
| P2 | `FlowGraph / FlowEdge` | `O(vehicle_count + active_edge_count)` | active edge 无理由爆炸失败 | 每步全排列匹配 subject |
| P3 | `EdgeContract` | `O(active_contract_count)` | 合同模板数量固定 | 合同生成依赖长计划搜索 |
| P4 | `TargetContractSelector` | `O(active_contract_count log active_contract_count)` 或规则筛选 | P95 不超过总耗时 10% | 黑箱全局评分反复调参 |
| P5 | `EdgeBoundedCandidateGenerator` | `candidate_family_count <= 8`, `planlet_horizon <= 3` | `necessary_candidate_recall = 100%` | raw candidate 未裁剪进入评分 |
| P6 | `StationResourceGraph` | 资源申请/释放局部判断 | P95 不超过总耗时 15% | 资源仲裁变成路径全排列 |
| P7 | `ContractDelta / ResourceDelta` | 单候选或短 planlet 模拟 | `delta_required_field_coverage = 100%` | delta 依赖全局 rollout |
| P8 | `LocalTieBreakSearch` | `local_branch_count <= 64` | P95 不超过总耗时 20% | 搜索跨 contract 或跨 intent |
| P9 | `Trace / Rebuild` | 只记录摘要和 diff | `trace_missing_count = 0` | trace 需要回扫全局计划才能解释 |
| P10 | 全部结构 | 单案 `runtime <= 300s` | batch P95 `<= 300s` | 任一案例超 5 分钟且无降级策略 |

超过预算时，不能用“后续优化”通过审核，必须指出是哪一个结构制造了搜索空间。

### 20A.7 达到人工与超越人工的量化判定

达到人工不是一句定性结论，而是以下布尔表达式：

```text
reached_human =
  P0_passed
  and P1_passed
  and P2_passed
  and P3_passed
  and P4_passed
  and P5_passed
  and P6_passed
  and P7_passed
  and P8_reached_human
  and P9_passed
  and hard_violation_count = 0
  and final_target_satisfied = true
```

其中：

```text
P8_reached_human =
  standard_chain_hook_count <= manual_hook_count * 1.05
  and short_or_direct_hook_count <= manual_hook_count + 1
```

超越人工必须先达到人工，再满足多指标支配：

```text
exceeded_human =
  reached_human
  and runtime <= 300s
  and dominated_accepted_rate = 0
  and (
    hook_count < manual_hook_count
    or (
      hook_count <= manual_hook_count
      and improved_secondary_metric_count >= 2
    )
  )
```

`improved_secondary_metric_count` 只从以下指标计数：

| 指标 | 计入改进的条件 |
|---|---|
| 道岔次数 | `< manual_switch_count` |
| 路径代价 | `< manual_path_cost` |
| 机车空驶 | `< manual_loco_empty_distance` |
| 存4污染 | `< manual_cun4_dirty_count` |
| 大库进出次数 | `<= manual_depot_entry_exit_count` 且 swap 冲突更少 |
| 大库 swap 冲突 | `< manual_depot_swap_conflict_count` |
| 叉线/临停使用 | `< manual_temp_buffer_use_count` |
| trace 可解释性 | `trace_field_coverage = 100%` 且 `failure_bucket_coverage = 100%` |

没有对应人工基准的二级指标不能计入超越，只能作为辅助说明。

### 20A.8 否决项

出现以下任一问题，方案文档不能通过审核：

| 否决项 | 原因 |
|---|---|
| 只覆盖修库主链，不覆盖全站主体流 | 业务覆盖不足 |
| 把预修、调棚、功能线、存车整理大量放进 residual | 主体车流漏建模 |
| 用未来人工计划动作作为在线判断条件 | 信息泄漏 |
| 硬约束可被评分抵消 | 安全边界错误 |
| 大库只用线级容量，不处理 slot/band swap | 大库模型不足 |
| 存4北没有双向资源仲裁 | 主冲突点漏建模 |
| 机车携带顺序被完全压缩成枚举 | 摘解合法性不足 |
| 未定义关键结构输入输出 | 方案不可落地 |
| 未定义结构性能边界 | 5 分钟求解不可判断 |
| 候选生成无边界 | 搜索空间不可控 |
| 没有低信号/异常案例处理 | 泛化能力不足 |
| 没有失败分类 | 审核无法定位问题 |

一句话标准：

```text
方案文档只有在同时说明“业务全覆盖、关键结构可实现、结构性能可控、硬约束不可破、资源冲突可解释”时，
才有资格进入“方案可行性”的审核；
只有在关键结构验证通过后，端到端结果再显示无硬违规且多指标支配人工时，
才有资格声称“超越人工”。
```

---

## 20B. 人工调车阶段模型审计

业务文档中的大库要求不能直接固定成四个机械阶段。人工钩序显示，现场真正稳定的阶段边界不是“全部编组、全部拉出、全部放置、剩余到位”，而是围绕存4释放口、机接硬边界和修库消化形成的阶段链。

基于 119 个人工调车作业单、4086 个有效钩序的复核，严格信号定义如下：

```text
存4口成形 = 存4/注意存4出现北头、代位、靠口等信号，但不是带辆数的北头摘。
存4大释放 = 存4 - N 北头摘。
标准机接 = 机 + N 接。
修库消化 = 修1-修4 - / 摘 / 库外。
库回收束 = 库回或库侧收束信号。
```

关键统计：

| 信号 | 覆盖案例数 | 首次出现中位钩 | 首次相对位置中位数 | 说明 |
|---|---:|---:|---:|---|
| 存4口成形 | 106 | 18.5 | 0.55 | 中段开始靠存4，不等同最终释放 |
| 存4大释放 | 104 | 25.0 | 0.77 | 标准链关键转折点 |
| 标准机接 | 102 | 27.0 | 0.80 | 通常紧跟存4大释放 |
| 修库接入/送入 | 111 | 20.0 | 0.62 | 常在释放前后穿插形成大库目标群 |
| 修库消化 | 117 | 25.0 | 0.71 | 大库摘解不是尾项 |
| 库回收束 | 76 | 36.0 | 1.00 | 多数为后段收束 |

严格分类后：

| 人工阶段族 | 案例数 | 中位钩数 | 含义 |
|---|---:|---:|---|
| 标准存4释放-机接-修库消化 | 97 | 36 | 稳定主链，适合完整阶段链 |
| 修库消化但无标准存4释放 | 15 | 28 | 库内消化、直接承接、缺前段释放信号 |
| 有存4释放但无标准机接 | 7 | 21 | 短链、低信号或直接处理 |

因此，阶段模型应按人工逻辑定义为 `H1-H5`。`H` 表示 human-derived phase，不是新的求解过程；每个阶段内部仍然执行 P0-P9。

```text
H1 前段全站组织与服务处理
H2 存4释放口成形与保护
H3 存4大释放-机接硬边界
H4 修库消化、库位和出入库冲突处理
H5 尾项收束与库回
```

阶段数不是按业务文档文字拆出来的，而是按人工计划中的稳定边界拆出来的：

| 判定依据 | 人工信号 | 为什么形成独立阶段 |
|---|---|---|
| 前段服务动作大量早于大库释放 | 功能线服务中位第 8 钩，机区第 12 钩，调棚/叉线第 14 钩，存车区第 17 钩 | 这些动作不是“大库前杂项”，而是在给主流、存4口、机车端别和缓冲资源塑形，因此需要 H1 |
| 存4口成形早于存4大释放 | 存4口成形中位第 18.5 钩，存4大释放中位第 25 钩 | “靠口/代位/北头整理”和“带辆数北头摘”之间存在保护与清口过程，因此需要 H2 |
| 存4大释放与机接是硬边界 | 存4大释放覆盖 104 案，标准机接覆盖 102 案，二者在标准链中连续出现 | 释放后主列状态改变，机接后保护条款生效，不能和普通编组混在同一阶段，因此需要 H3 |
| 修库摘解不是尾项 | 修库消化覆盖 117 案，首次中位第 25 钩，常与机接前后穿插 | 大库 slot/band、stayer、swap、摘解顺序会决定是否可解，因此需要 H4 |
| 库回只在主债务完成后出现 | 库回覆盖 76 案，首次相对位置中位数 1.00 | 库回是收束信号，不应提前触发，因此需要 H5 |

所以，业务不是固定分成原先的 S1-S4。更准确的表达是：

```text
P0-P9 = 求解器内部过程轴，用来说明一次候选如何从状态、合同、资源、delta、gate、少钩搜索推进。
H1-H5 = 人工调车业务阶段轴，用来说明当前人工计划结构处在前段组织、存4成形、释放机接、修库消化还是尾项收束。
```

两条轴相互正交：一个案例在 H3 阶段内仍然会执行 P0-P9；短链、库内消化或低信号案例可以合法跳过或压缩部分 H 阶段，但不能跳过对应的业务硬门和 trace 证明。

### 20B.1 必须新增的阶段合同

建议增加 `HumanPhaseContract / PhaseGate`，不替代 `FlowEdge`，只负责表达人工计划中的阶段边界、允许跳过和失败定位：

```text
HumanPhaseContract:
  phase:
    H1_FRONT_SERVICE_ORGANIZATION
    H2_CUN4_PORT_SHAPING
    H3_RELEASE_ACCEPT_BOUNDARY
    H4_DEPOT_DIGEST_SLOT_RESOLUTION
    H5_TAIL_CLOSEOUT
  active_variant:
    FULL_CHAIN_REPAIR
    LATE_CUN4_REPAIR
    DIRECT_REPAIR_ENTRY
    MIXED_SIGNAL_REPAIR
    DEPOT_DIGEST_ONLY
  required_contracts
  phase_entry_condition
  phase_exit_condition
  allowed_skip_condition
  forbidden_moves
  metrics
```

它不是新的 workflow 主控，而是阶段验收合同：

```text
FlowEdge 识别每条业务流。
EdgeContract 定义每条流的履约。
StationResourceGraph 仲裁资源。
HumanPhaseContract 定义当前人工阶段、可跳过阶段和阶段硬门。
```

没有这个阶段合同，`TargetContractSelector` 可能会动态选择局部收益最高的合同，但无法证明：

```text
前段组织是否已经足够；
存4口是否已经成形且受保护；
存4大释放和机接是否作为硬边界被连续处理；
修库消化是否完成；
库回和尾项是否没有提前。
```

### 20B.2 人工阶段量化标准

| 阶段 | 目标 | 参与结构 | 硬门槛 | 达到人工线 | 超越人工线 |
|---|---|---|---|---|---|
| H1 前段全站组织与服务处理 | 完成外场、预修、调棚、功能线、存车整理中的主体债务，并让大库主流具备 owner | `FlowClassify`, `StationFlowContract`, `YARD_REBALANCE`, `FUNCTION_LINE_SERVICE`, `PRE_REPAIR_STAGING`, `DISPATCH_SHED_QUEUE`, `LocoCarryState` | `effective_contract_coverage < 95%` 失败；`residual_vehicle_ratio > 5%` 失败；预修/调棚/功能线主体流 residual 失败 | `non_depot_progress_ratio >= 0.8`，目标 `>= 0.9`; 前段动作都有合同 owner | 前段钩数不高于人工计划同阶段；提前识别存4/联7/端别冲突率 `>= 90%` |
| H2 存4释放口成形与保护 | 主流逐步靠存4，清理或保护存4北，准备最终大释放 | `CUN4_NORTH_BUFFER`, `FlowPort`, `Blocker`, `Protection`, `TargetContractSelector`, `StationResourceGraph` | `cun4_direction_conflict_accepted = 0`; dirty port 强行释放 `= 0`; `port_shape_reason_coverage = 100%` | 存4口成形识别率 `>= 95%`; 标准链中位靠口位置可解释；late 存4不回外场补链 | 存4污染次数少于人工，或同钩数下清口动作更少 |
| H3 存4大释放-机接硬边界 | 执行 `存4 - N 北头摘 -> 机 + N 接` 或合法跳过/保守处理 | `RepairInboundVariant`, `FlowEdgeStatus`, `EdgeContract`, `Protection`, `ResourceRequest`, `AcceptRejectGate` | 标准链 `strict_release_candidate_recall = 100%`; `machine_accept_candidate_recall = 100%`; 低置信主动跨 `ACCEPTED = 0`; 机接后 protection broken accepted `= 0` | 标准链中 `release_accept_gap <= 2`，无资源原因时目标 `<= 1`; `0117Z/0310W` 不断链 | 在不破坏保护下减少释放前返工；late 存4和短链不强套标准长链 |
| H4 修库消化、库位和出入库冲突处理 | 机接后或直接入库后完成修1-修4摘解、slot/band 合法性、必要出库腾位 | `DepotSlotGraph`, `DepotSwapDelta`, `DEPOT_OUTBOUND`, `REPAIR_INBOUND`, `Obligation`, `ResourceDelta`, `ordered carry segments` | `tail_before_primary_done_count = 0`; `depot_slot_violation_accepted = 0`; `swap_required_resolved_ratio < 100%` 失败；非法摘解 `= 0` | 修库消化债务完成率 `100%`; slot/band 或 area 满足率 `100%`; digest-only 不补存4长链 | 大库进出次数 `<= manual_depot_entry_exit_count`; swap 冲突少于人工 |
| H5 尾项收束与库回 | 主合同完成后处理库回、机区、功能线、存车整理和剩余目标 | `TAIL_CLOSEOUT`, `LOCO_AREA_STAGING`, `FUNCTION_LINE_SERVICE`, `YARD_REBALANCE`, `SPECIAL_REPAIR_PROCESS`, `Trace` | `final_target_satisfied = true`; `hard_violation_count = 0`; 主体债务未完 closeout `= 0`; 关键 residual owner 不明失败 | `remaining_target_completion = 100%`; 所有尾项动作有 owner 和 why_accept | 总钩数不高于人工；若钩数相同，空驶/临停/叉线/存4污染至少 2 项更优 |

### 20B.3 阶段进入和退出门

阶段门必须是硬门，不能被 `hook_count` 或评分抵消。阶段不是全局固定 workflow；它是当前主合同族的业务位置。

阶段边界不能写成固定钩号，因为人工计划中前段服务、修库接入、功能线处理会穿插出现。可执行边界必须写成 `PhaseGate` 谓词：

```text
entry_predicate    = 允许进入该阶段的最小条件
active_predicate   = 当前动作仍属于该阶段的判定条件
exit_predicate     = 允许离开该阶段的完成条件
skip_predicate     = 允许合法跳过该阶段的变体条件
fail_predicate     = 该阶段无法继续推进时的失败定位条件
veto_predicate     = 任何情况下都不能接受的动作
```

每次阶段变化必须生成一条 `PhaseGateRecord`：

```text
PhaseGateRecord:
  case_id
  step_index
  from_phase
  to_phase
  transition_type: enter | exit | skip | fail | stay
  active_variant
  predicate_values
  consumed_contract_ids
  created_contract_ids
  carried_obligation_ids
  blocked_contract_ids
  evidence_ids
  hook_count_in_phase
  manual_phase_hook_count
  reject_reason
```

最低硬门：

```text
phase_transition_trace_coverage = 100%
accepted_without_phase_permission_count = 0
phase_gate_bypass_count = 0
phase_obligation_carryover_error_count = 0
phase_regression_without_reason_count = 0
```

#### 20B.3A 五阶段明确边界

| 阶段 | 进入边界 `entry_predicate` | 活跃边界 `active_predicate` | 退出边界 `exit_predicate` | 失败边界 `fail_predicate` | 一票否决 `veto_predicate` |
|---|---|---|---|---|---|
| H1 前段全站组织与服务处理 | `FlowClassify` 完成；`effective_contract_coverage >= 95%`；`residual_vehicle_ratio <= 5%`；存在非大库服务债务，或大库主流尚未达到 H2/H3/H4 高置信边界 | 当前 accepted 动作服务于 `YARD_REBALANCE / FUNCTION_LINE_SERVICE / PRE_REPAIR_STAGING / DISPATCH_SHED_QUEUE / LOCO_AREA_STAGING`，或为大库主流创建 owner、端别、缓冲、机车位置条件 | `non_depot_progress_ratio >= 0.8`，目标 `>= 0.9`；预修/调棚/功能线 hard obligation 未完成数 `= 0`；大库主流 owner 覆盖 `100%`；存4口资源状态可解释 | 非大库主体车无 owner；服务债务无法生成候选；机车端别/缓冲阻塞无 clear action；超过 `pre_depot_hook_count > 40` 且无 hard reason | 把预修/调棚/功能线未完成主体车直接塞入大库主组；把无 owner 车辆当尾项；为少钩破坏后续存4口 |
| H2 存4释放口成形与保护 | H1 退出，或初始已高置信接近存4口；存在 `REPAIR_INBOUND` 主流；`CUN4_NORTH_BUFFER` 可观测；存4北 owner/blocker/direction 有 reason | 当前 accepted 动作清理、靠口、代位、保护或组织存4北释放口；动作必须减少 blocker、改善 port health 或保护 release subject | `cun4_port_shape_ready = true`；`port_shape_reason_coverage = 100%`；存4口 dirty blocker 已清理或有合法释放顺序；方向冲突 accepted `= 0`；release subject、车辆数、端别、保护合同已绑定 | 存4北状态未知；dirty blocker 无清理动作；方向冲突无法解除；release subject 无法稳定识别；低优先合同持续污染存4口 | 把存4北当普通存车线；`MIXED_DIRTY` 状态下强行释放/机接；`PORT_READY` 后回外场补长链，除非有 hard blocker reason |
| H3 存4大释放-机接硬边界 | H2 退出且标准链/late 存4链成立；或已高置信处于 `PORT_READY`；必须能申请释放、机接、机车端别和 gate 资源 | 当前 accepted 动作只能服务 `strict_release`、`machine_accept`、必要短距离保护/等待；如果低信号，只能走保守候选，不能主动跨 `ACCEPTED` | 标准链：`strict_release_done = true` 且 `machine_accept_done = true`；`release_accept_gap <= 2`，无资源原因时目标 `<= 1`；`accepted_subject_id` 与 release subject 一致；机接后 protection 激活 `100%`；生成 H4 修库消化债务 | 释放候选或机接候选 recall 不足；机车端别未知；gate 不可用；接收端容量未知；低置信证据不足以跨 `ACCEPTED` | 低置信主动机接；机接后拆散保护主列；用少钩评分跳过 `ContractDelta/ResourceDelta`；释放后无 reason 长时间返回 H1/H2 |
| H4 修库消化、库位和出入库冲突处理 | H3 退出；或 `DIRECT_REPAIR_ENTRY / DEPOT_DIGEST_ONLY` 成立；存在修库消化、入库、出库腾位、slot/band、stayer 或特殊工艺债务 | 当前 accepted 动作服务于 `REPAIR_INBOUND / DEPOT_OUTBOUND / DEPOT_SLOT / DEPOT_DIGEST / SPECIAL_REPAIR_PROCESS`，并能解释 slot、swap、stayer、端别、摘解顺序变化 | `depot_digest_complete = true`；`slot_or_area_satisfaction = 100%`；`swap_required_resolved_ratio = 100%`；`depot_slot_violation_accepted = 0`；`invalid_detach_count = 0`；主修库债务未完成数 `= 0` | slot/band 不满足且无 swap；stayer 锁定冲突；出库腾位无合法 break action；携带顺序不可摘；称重/顶送/对位缺硬约束解释 | 修库摘解未完成就库回；slot 未释放强行入库；移动 stayer；只按线容量满足而忽略台位、长度、厂修/段修规则 |
| H5 尾项收束与库回 | H4 退出；或本案无大库/修库主债务且剩余动作只有 support contracts；所有 primary obligation 要么完成，要么有合法 no-op reason | 当前 accepted 动作只服务 `TAIL_CLOSEOUT / LOCO_AREA_STAGING / FUNCTION_LINE_SERVICE / YARD_REBALANCE` 中的剩余目标，不得新建无来源主合同 | `final_target_satisfied = true`；`remaining_target_completion = 100%`；`hard_violation_count = 0`；critical residual `= 0`；trace/failure bucket 覆盖 `100%` | 剩余目标不满足；residual owner 不明；库回后仍有主合同未完；尾项动作无法解释 owner | 主体债务未完 closeout；把 H4 失败伪装成尾项；库回后再无 reason 重开 H2/H3/H4 |

#### 20B.3B 跳过和压缩边界

跳过不是自由裁剪，而是 `skip_predicate` 通过后的阶段事件。每次跳过必须写入 `PhaseGateRecord.transition_type = skip`。

| 跳过/压缩 | 明确条件 | 反例 |
|---|---|---|
| 跳过 H1 | 初始已经高置信处于 `PORT_READY / ACCEPTED / DIGESTING`，或 `DIRECT_REPAIR_ENTRY / DEPOT_DIGEST_ONLY` 成立；且 `non_depot_open_hard_obligation_count = 0` | 仍有预修、调棚、功能线主体债务未完成，却被塞进尾项 |
| 跳过 H2 | `DEPOT_DIGEST_ONLY`、直接入库，或已处于 `PORT_READY` 之后；且不存在需要存4口成形的标准释放债务 | 标准链仍未形成存4释放口，却直接生成释放/机接 |
| 跳过 H3 | `DEPOT_DIGEST_ONLY` 或 `DIRECT_REPAIR_ENTRY` 明确不需要标准机接；低信号案例只能跳过为保守路径，不能伪造 `ACCEPTED` | 标准链已经 `PORT_READY` 且机接资源可申请，却为少钩绕过机接保护 |
| 压缩 H3/H4 | 近库侧直接入库案例中，机接/入库/消化在少量钩内连续发生；必须同时满足 H3 出口和 H4 入口对象创建 | 只有最终位置接近，但没有 slot、端别、摘解顺序证明 |
| 跳过 H4 | 本轮无修库/大库主债务，或车辆初始已合法满足 slot/area 且无出入库冲突 | 只满足线路容量，不满足台位、长度、厂修/段修、stayer |
| 跳过 H5 | 初始或 H4 退出时已 `final_target_satisfied = true` | 还有 residual、功能线服务或库回债务未解释 |

#### 20B.3C 阶段重叠时的主阶段判定

人工计划存在穿插动作，所以同一步可能同时有 support contract 和 primary contract。文档级标准采用主阶段判定：

```text
primary_phase =
  最高优先级未完成硬边界阶段
```

优先级按业务硬边界而不是钩号：

```text
H3 存4大释放-机接硬边界
  > H4 修库消化、库位和出入库冲突
  > H2 存4释放口成形与保护
  > H1 前段全站组织与服务处理
  > H5 尾项收束与库回
```

解释：

- 一旦进入 H3，释放和机接保护优先于继续做前段整理；前段动作只能作为 support contract，且不得破坏 `PORT_READY / ACCEPTED`。
- 一旦进入 H4，修库 slot/swap/stayer/摘解债务优先于尾项；库回不能提前。
- H5 只能在主债务完成后成为 primary phase。
- 如果高优先级阶段因资源硬阻塞暂不能推进，可以接受 lower phase support action，但必须记录 `blocked_contract_ids` 和 `support_action_reason`。

#### 20B.3D 代表案例边界落点

| 案例 | 入口判定 | 边界路径 | 关键证明 |
|---|---|---|---|
| `0117Z` 标准完整链 | 从 H1 进入 | H1 -> H2 -> H3 -> H4 -> H5 | 第 32 钩存4大摘触发 H3，33 钩机接后生成 H4，34-37 钩修库摘解未完前不能 H5 |
| `0310W` late 存4链 | 可从 H2/H3 附近进入 | H2/H3 -> H4 -> H5 | `PORT_READY` 后禁止回 H1 补长链；36-37 钩释放机接是硬边界 |
| `0103W` 库内消化 | 跳过 H2/H3，从 H4 进入 | H4 -> H5 | 无标准存4释放/机接，但修库摘解和特殊工艺债务必须完成后才能库回 |
| `0213W` 短链直接入库 | 跳过 H1/H2，压缩 H3/H4 | H3/H4 -> H5 | 不能强套标准长链；短链必须同时证明直接入库和修库消化合法 |
| `0306W` 低信号 | H2 保守推进 | H2 -> 保守路径 -> H5 | 存4释放存在，但机接信号不标准；低置信禁止主动跨 `ACCEPTED` |

### 20B.4 代表阶段路径摘要

H1-H5 是人工计划阶段模型，不是所有案例都必须机械执行完整链。详细的跳过和压缩边界以 20B.3B 的 `skip_predicate` 为准；这里仅保留代表案例的阶段路径摘要。

| 案例族 | 阶段路径 |
|---|---|
| `0117Z / 0310W / 0128W` 标准链 | H1 -> H2 -> H3 -> H4 -> H5 |
| `0310W` late 存4 | 可从 H2/H3 附近开始，禁止回 H1 补长链 |
| `0103W / 0223W` 库内消化 | H4 -> H5，跳过 H2/H3 |
| `0213W` 短链直接入库 | H3/H4 压缩或跳过，不能强套 H1/H2 |
| `0306W` 低信号 | H2 保守推进，H3 不允许低置信主动跨 `ACCEPTED` |

### 20B.5 钩数不高于人工的可证明条件

“钩数不高于人工”不能由阶段名称保证，只能由以下审计条件证明。

case 级判定：

```text
hook_not_higher_than_human =
  hard_violation_count = 0
  and final_target_satisfied = true
  and solver_hook_count <= manual_hook_count
```

阶段级判定：

```text
phase_hook_not_higher_than_human =
  H1_hook_count <= manual_H1_hook_count
  and H2_hook_count <= manual_H2_hook_count
  and H3_hook_count <= manual_H3_hook_count
  and H4_hook_count <= manual_H4_hook_count
  and H5_hook_count <= manual_H5_hook_count
```

如果没有人工计划阶段切分，只能使用 case 级钩数，不得声称每个阶段都不高于人工。

为了提高“不高于人工”的可实现性，方案必须满足：

| 条件 | 量化门槛 |
|---|---|
| 候选生成不漏人工结构动作 | `necessary_candidate_recall = 100%` |
| 同结构候选不接受劣解 | `dominated_accepted_rate = 0` |
| 少钩搜索不跨结构边界 | `search_scope_violation_count = 0` |
| 局部搜索规模可控 | `local_branch_count <= 64` |
| 人工动作可作为结构模板覆盖 | 人工关键动作族 recall `= 100%` |

如果这些条件不满足，即使 H1-H5 都能完成，也不能承诺钩数不高于人工。

### 20B.6 高可解性条件

高可解性不是“能跑出几个案例”，而是每个可进入的阶段都有合法 fallback 和失败定位。

建议门槛：

| 指标 | 达到人工线 | 高可解性目标 |
|---|---:|---:|
| 代表案例阶段硬门通过率 | 100% | 100% |
| `truth2_force` 可解率 | `>= 95%` | `>= 98%` |
| 单案 P95 runtime | `<= 300s` | `<= 180s` |
| hard violation accepted | 0 | 0 |
| `failure_bucket_coverage` | 100% | 100% |
| `unknown_failure_repeat_rate` | `<= 2%` | `= 0` |
| 必需候选 recall | 100% | 100% |
| resource deadlock 有合法 break action | 100% | 100% |

每个阶段必须有 fallback：

| 阶段 | fallback |
|---|---|
| H1 | 前段组织失败时输出缺失 owner、服务债务、阻塞资源、不可混编原因 |
| H2 | 存4口成形失败时输出口污染、方向冲突、阻塞合同和合法清口动作 |
| H3 | 释放/机接失败时输出端别、机车位置、gate、接收端容量或低置信原因 |
| H4 | 修库消化失败时输出 slot/band 缺口、stayer 锁定、端别不可达、携带顺序不可摘 |
| H5 | 剩余到位失败时按合同族分类失败，不允许 residual 垃圾桶化 |

### 20B.7 当前结论

对“业务到底分成几个阶段比较合适”的当前结论是：

```text
不应固定为原先的 S1-S4。
更合适的是人工计划反推的 H1-H5 阶段模型。
```

必须补齐：

1. `HumanPhaseContract / PhaseGate`，把 H1-H5 的进入、退出、跳过和失败条件变成硬门。
2. 阶段级指标：`front_service_progress`、`cun4_port_shape_ready`、`release_accept_boundary_done`、`depot_digest_complete`、`tail_closeout_complete`。
3. 阶段级钩数对照：如果要声称每阶段不高于人工，必须先把人工计划切成 `manual_H1..H5_hook_count`。
4. 可解性压测：代表案例 100% 通过，`truth2_force` 可解率至少 95%，目标 98%。

如果只保留当前 `FlowEdge / EdgeContract / StationResourceGraph`，而不增加人工计划阶段门，则方案只能说“有能力表达人工关键结构”，不能说“能稳定遵守人工阶段逻辑并保证不高于人工”。

## 20C. 独立复审：小结构有效性与连接闭环

本节是对 20A/20B 的独立复审。它不再只问“结构是否定义了”，而是逐一审查：

```text
每个小结构是否有可量化有效标准；
每个小结构的输入输出是否能被上下游消费；
结构之间的连接是否 100% 可追踪、可拒绝、可定位；
人工案例中的结构价值是否能被这些结构共同表达；
业务文档中的硬约束是否能落到明确结构和明确 gate；
当所有结构和连接都达标时，整体是否具备达到甚至超越人工的充分结构条件。
```

### 20C.1 独立复审结论

当前方案的结构方向是正确的：`FlowGraph -> FlowEdge -> EdgeContract -> ResourceGraph -> Delta -> Gate -> LocalSearch -> Trace` 能把人工调车中的“先组织、再释放、再机接、再库内消化、再收束”转成可审计的结构链。

但“结构方向正确”不等于已经可以声称必然达到或超越人工。复审结论分两层：

| 问题 | 结论 |
|---|---|
| 如果 20A/20B/20C 的结构指标、连接指标、人工阶段门指标全部达标，整体能否达到人工？ | 可以。因为人工关键结构、业务硬约束、候选生成、资源仲裁、少钩比较和 trace 已经形成闭环。 |
| 如果这些指标全部达标，整体能否具备超越人工的条件？ | 可以，但必须在硬约束为 0、可解率达标、钩数不高于人工之后，再证明至少 2 个二级指标支配人工。 |
| 当前文档中的原始结构是否已经足够直接保证？ | 不足。必须补齐 `HumanPhaseContract / PhaseGate`、连接审计指标和人工阶段级钩数切分，才能把“有基础”提升为“可证明”。 |

因此，方案可行性的判断不能靠最终跑几个案例像人工，而要靠以下充分链：

```text
all_small_structures_passed
  and all_structure_connections_passed
  and all_phase_gates_passed
  and hard_violation_count = 0
  and necessary_candidate_recall = 100%
  and dominated_accepted_rate = 0
  and final_target_satisfied = true
  and runtime <= 300s
  and solver_hook_count <= manual_hook_count
=> reached_human
```

进一步：

```text
exceeded_human =
  reached_human
  and (
    solver_hook_count < manual_hook_count
    or (
      solver_hook_count = manual_hook_count
      and improved_secondary_metric_count >= 2
    )
  )
```

本章不再新增 `20D` 式审计层。后续应直接把 20A/20B/20C 的标准转成 4 类可执行标准产物：

| 标准产物 | 来源章节 | 作用 | 完成后可进入 |
|---|---|---|---|
| 结构标准卡 | 20C.2 | 规定每个小结构本身什么算有效 | 小结构实现设计 |
| 连接标准卡 | 20C.3 | 规定上下游结构之间如何无损接力 | runtime 集成设计 |
| 场景验收卡 | 20C.4/20C.6 | 规定人工案例和人工计划阶段如何验收结构 | case replay 和压力测试 |
| 指标数据字典 | 20A.2C/20C.5 | 规定指标字段、公式、来源、粒度 | 自动化验收报表 |

进入下一步“小结构标准设计”的准入条件是：

```text
small_structure_standard_design_ready =
  structure_inventory_frozen
  and connection_inventory_frozen
  and phase_inventory_frozen
  and metric_dictionary_defined
  and representative_case_set_defined
  and human_case_phase_split_started
  and business_rule_mapping_complete
```

| 准入项 | 通过标准 |
|---|---|
| `structure_inventory_frozen` | 20C.2 中的小结构清单固定；新增结构必须说明替代或合并关系 |
| `connection_inventory_frozen` | 20C.3 中的连接链固定；新增连接必须说明上下游对象 |
| `phase_inventory_frozen` | H1-H5 阶段、进入/退出/跳过条件固定 |
| `metric_dictionary_defined` | 每个指标有公式、粒度、来源字段和失败桶 |
| `representative_case_set_defined` | 至少包含 `0117Z/0310W/0103W/0213W/0306W/0128W/0223W/0130Z/0201W` |
| `human_case_phase_split_started` | 至少 9 个金标案例开始切分 `manual_H1..H5_hook_count`；未切分完时只能做 case 级钩数对照 |
| `business_rule_mapping_complete` | 业务文档中的硬约束均能映射到结构、连接和 gate |

### 20C.2 小结构有效性总表

下表是独立复审标准。审核时每一行都必须有 step/case/batch 证据。缺证据的结构即使名称存在，也只能算 L1/L2，不能算 L3。

| 小结构 | 上游输入 | 下游输出 | 有效标准 | 连接标准 | 人工案例验证 | 业务约束映射 | 失败影响 |
|---|---|---|---|---|---|---|---|
| `OnlineObservable` | `StartStatus`、车辆属性、线路几何、目标 | 在线可用 feature、status source | `online_status_reason_coverage = 100%`; `offline_label_used_online_count = 0` | 所有 status 必须带 `source = geometry/resource/target/vehicle/executed_move` | `0306W` 低信号仍能给 confidence | 禁止用人工计划动作泄漏未来 | 信息泄漏会让可行性结论失真 |
| `OfflineLabelOnly` | 人工钩序、人工结构标签 | hidden label、对照指标 | 只能用于评估，不能进入 runtime | `runtime_feature_join_count = 0` | 118 个人工文件只作为基准 | 审计可比性 | 泄漏后钩数和可解性无效 |
| `FlowGraphBuilder` | 在线 feature、上一步状态 | `FlowGraph`、rebuild diff | 每步 rebuild 成功率 `100%`; unknown diff `<= 2%` | `graph_build_trace_coverage = 100%` | `0117Z` 能看到边逐步推进 | 全站业务覆盖 | 构图错误会传染所有结构 |
| `FlowClassify` | 车辆目标、属性、当前位置、service state | 每辆需动车的合同族和 reason | `effective_contract_coverage >= 95%`; `residual_vehicle_ratio <= 5%` | `vehicle_to_contract_owner_coverage = 100%` | 标准链 97 文件主流不掉 residual | 预修/调棚/功能线/大库/存车整理全覆盖 | 主体流漏分类会导致无解或假少钩 |
| `NoMoveVehicle` | 已满足目标车辆、阻塞检查 | no-move 集合和 reason | `no_move_false_satisfied_rate = 0` | no-move 仍要输出是否阻塞资源 | `0213W` 不强行多动车 | 避免无效动车 | 错判不动车会堵关键口 |
| `ResidualItem` | 未绑定车辆、低信号车辆 | residual reason、expiry、suspected role | `residual_vehicle_ratio <= 5%`; reason 覆盖 `100%`; 平均生命周期 `<= 3` 次 rebuild | residual 必须有升级/失败路径，不能永久垃圾桶化 | `0306W` 可低信号保守 residual | 异常样本可解释 | residual 过多会降低可解性 |
| `FlowGraph` | 分类结果、边识别结果 | active edges、residuals、contradictions | `subject_overlap_count = 0`; 主体流输出完整率 `100%` | 每个 active edge 必须绑定 contract 或 residual reason | `0117Z` 标准主边不断推进 | 全站并发合同视图 | 图层不稳会让目标选择乱跳 |
| `FlowEdge` | subject、port、receiver、evidence | 边对象、状态、证据 | active edge 必填字段覆盖 `100%`; 高价值主流漏识别率 `<= 2%` | `edge_id`、subject、contract owner 连续 | `0310W` late 存4边不能被重建成长链 | 存4释放、机接、大库出入 | 边错会让合同错 |
| `EdgeKey` | 车辆集合、方向、via、receiver | 稳定 edge identity | `edge_identity_stability >= 95%`; 无理由漂移 `= 0` | rebuild 后 `edge_key` 变化必须有原因 | `0117Z` 23-37 钩不能频繁换主边 | trace 可复盘 | identity drift 会让合同债务丢失 |
| `FlowSubject` | 车辆集合、有序段、role | seed/forming/bound subject | 同车 primary subject 重叠 `= 0`; role 覆盖 `100%` | subject 到 candidate 的车辆集合守恒 `100%` | `0117Z` 前段组流逐渐成主列 | 编组与摘解顺序 | subject 错会导致脏携带 |
| `FlowPort` | via line、端别、口状态 | port role、health、blocking vehicles | 存4北、联线、修库入口 health 覆盖 `100%` | port 被 candidate 使用前必须有 resource request | `0310W` 存4北释放口 | 存4北双向争抢、联6/联7 | port 当普通线会污染关键口 |
| `FlowReceiver` | 目标区域、作业线、容量、digest state | receiver state、pending obligations | 修库/预修/调棚/功能线 receiver 覆盖 `100%` | receiver capacity unknown 不允许 accept | `0103W` 修库消化不是尾项 | 大库、功能线、预修台位 | 接收端错会提前 closeout |
| `FlowEdgeStatus` | edge evidence、resource state、合同履约 | `SEED/FORMING/PORT_READY/ACCEPTED/DIGESTING/DONE` | `illegal_status_transition_count = 0`; 低置信跨 `ACCEPTED = 0` | status change 必须生成 status delta trace | `0306W` 不低置信主动机接 | 低信号保守边界 | 状态越级会制造非法少钩 |
| `EdgeContract` | edge、status、template | must/protection/forbidden/finish clauses | `contract_clause_completeness = 100%` | 每个 accepted action 必须有 `ContractDelta` | `0117Z` 机接后保护主列 | 人工结构价值硬化 | 合同缺项会让评分破坏结构 |
| `ContractTemplate` | 合同族、业务规则 | 标准条款模板 | 9 个合同族 template 缺失 `= 0` | template 到 contract 实例字段覆盖 `100%` | 非修库案例不掉 residual | 全站主体流覆盖 | 只会修库主链，泛化不足 |
| `StationFlowContract` | 所有 active contract | active contract set、priority hint | 主体合同族覆盖率 `>= 95%` | active contract 必须进入 target selector 或 suppressed reason | `0103W` `DEPOT_DIGEST_ONLY` 优先 | 多合同并发 | 合同集缺失会让资源仲裁无 owner |
| `RepairInboundVariant` | repair edge、证据强弱、当前位置 | variant、shortcut、forbidden move | 代表变体准确率 `100%`; 全量异常误判率 `<= 5%` | variant 必须改变 allowed/forbidden candidates | `0117Z/0310W/0103W/0213W/0306W` 分型正确 | 大库主链异常处理 | 强套标准链会劣于人工 |
| `Blocker` | contract clause、resource conflict | blocker type、clear intent | hard blocker ignored `= 0`; reason 覆盖 `100%` | blocker 必须能生成 clear candidate 或 failure bucket | `0310W` 存4口阻塞先清理 | 存4/大库/gate 冲突 | blocker 不显式会导致死等 |
| `Obligation` | 未完成条款、阶段债务 | owner、completion condition | hard obligation skipped `= 0`; lifecycle 覆盖 `100%` | obligation 完成必须反写 contract/status | `0103W` 修库摘解完成前不能库回 | 主合同未完禁止尾项 | 债务丢失会提前收尾 |
| `Protection` | `ACCEPTED` 后合同、主列状态 | active protection、release condition | active protection broken accepted `= 0` | protection 违反必须进入 reject reason | `0117Z` 机接后不拆主列 | 机接硬边界 | 短期少钩会破坏后续 |
| `TargetContractSelector` | active contracts、blockers、resource state | target contract、rank reason | `target_contract_reason_coverage = 100%`; hard priority inversion `= 0` | suppressed contract 必须有 reason | `0310W` 优先存4释放/机接，不补外场 | 阶段主次顺序 | 目标选错导致钩数膨胀 |
| `StructuralIntentBuilder` | target contract、clauses、blockers | intent、forbidden intents | intent 无合同来源 `= 0`; intent 匹配率 `100%` | intent 到模板选择必须带 clause source | `0213W` 直接入库 intent | 不强套长链 | 意图错会生成错误候选族 |
| `WorkPatternTemplateSelector` | intent、edge status、资源状态 | template list | 每个 intent 可用模板数 `1..6`; pattern 绕 gate `= 0` | template 必须声明 resource request schema | 人工动作族 recall `= 100%` | 人工经验动作族 | 漏模板会无解，过多会爆搜索 |
| `EdgeBoundedCandidateGenerator` | template、subject、port、receiver | bounded candidates | `necessary_candidate_recall = 100%`; `candidate_family_count <= 8`; `planlet_horizon <= 3` | forbidden candidate 进入 scoring `= 0` | `0117Z` 标准动作族存在 | 5 分钟性能 | 候选漏召回会低可解，候选失控会超时 |
| `ResourceRequest` | candidate、template schema | requested/blocked resources | `resource_request_coverage = 100%` | 无 request 的 candidate 不允许进入 delta | `0310W` 存4北、机接资源申请 | 存4/联线/大库/机车 | 无资源申请会放行冲突 |
| `StationResourceGraph` | resource requests、占用、等待 | acquired/released/blocked/wait-for | `hard_resource_violation_accepted_count = 0`; 资源字段覆盖 `100%` | 每个 resource delta 必须回写 graph | 标准链存4和大库争抢 | 全站共享资源仲裁 | 资源冲突靠评分会非法 |
| `CUN4_NORTH_BUFFER` | 存4北占用、方向、等待合同 | `FREE/INBOUND_RELEASE/OUTBOUND_HOLD/MIXED_DIRTY` | 四态识别 `100%`; direction conflict accepted `= 0` | 被占用/释放必须有 owner contract | `0117Z/0310W` 存4北释放 | 存4北不是普通终点 | 污染存4会增加钩数 |
| `DepotSlotGraph` | 大库现车、目标台位、stayer、长度修程 | slot/band availability、locked tail | slot request 覆盖 `100%`; `depot_slot_valid = true` | 入库/出库 candidate 必须带 slot/band delta | `0103W` 库内消化、出入库摘解 | 17.6 米、厂修 4/5、stayer 锁定 | 只看线容量会产生非法入库 |
| `DepotSwapDelta` | slot graph、入库/出库候选 | swap required/resolved/released | `swap_required_resolved_ratio = 100%` | swap 未解决不允许 H4 强行消化/入库 | 大库先出后进经验 | 出库腾位再入库 | swap 漏识别会卡死或非法 |
| `GlobalGate` | 联6/联7开放、路径申请 | gate slot、reject reason | `gate_conflict_accepted = 0`; gate request 覆盖 `100%` | 跨 gate 动作必须有 request/result | 大库集中期避开联7前 60 分钟 | 联7前 60 分钟关闭、走行线禁停 | gate 冲突会违反业务时序 |
| `LocoPosition` | 机车线位、端别、可达性 | track、side、distance、accessible end | `loco_position_unknown_accept_count = 0` | candidate 评分必须引用 loco position | 人工北头/南头备注 | 倒车折返、端别、空驶 | 忽略机车会低估钩数 |
| `LocoCarryState / ordered carry segments` | 当前挂车顺序、机车端别 | ordered segments、dirty/counted flags | `enum_only_carry_enabled = false`; 车辆守恒 `100%` | `ContractDelta/ResourceDelta` 使用同一 ordered list | `0117Z` 主列保护，`0103W` 称重/顶送 | 摘解顺序、称重最后一辆 | 枚举压缩会产生非法摘挂 |
| `WaitForGraph / DeadlockDetection` | contract/resource 等待边 | cycle、break action | cycle coverage `100%`; legal break action 输出 `100%` | break action 必须回到 target/candidate | 大库入库等出库腾位 | 存4、大库、gate 环形等待 | 无死锁解释会可解率低 |
| `EdgeDelta` | candidate、edge before/after | status/subject/progress change | required fields 覆盖 `100%` | delta 后 edge rebuild 必须一致 | `0117Z` 每步推进可解释 | 状态变化审计 | delta 错会误判履约 |
| `ContractDelta` | candidate、contract clauses | fulfilled/broken/new obligations | `contract_delta_required_field_coverage = 100%`; hard broken accepted `= 0` | accepted 必须有正向或合法让步 delta | 人工关键动作是正 delta | 合同硬门 | 无 delta 则少钩不可审计 |
| `ResourceDelta` | candidate、resource graph | acquired/released/violations | `resource_delta_coverage = 100%`; resource violation accepted `= 0` | accepted 后 resource graph 更新一致 | 存4释放、大库腾位 | 临停容量、走行线禁停、gate | 资源漏更新会连锁错误 |
| `AcceptRejectGate` | contract delta、resource delta、hard rules | accept/reject、why | `why_reject_coverage = 100%`; 硬违反 accepted `= 0` | gate bypass `= 0` | `0306W` 低置信机接被拒 | 所有业务硬约束 | gate 太松非法，太严无解 |
| `ContractOptimizer / hook_count` | 合法候选、局部成本 | 同结构排序结果 | `search_scope_violation_count = 0`; `dominated_accepted_rate = 0` | 只能在同 contract/intent/模板边界比较 | `0128W` 标准链高钩数可优化 | 少钩目标 | 跨结构少钩会破坏人工价值 |
| `LocalTieBreakSearch` | 同结构候选、短 horizon | best local planlet | `local_branch_count <= 64`; `planlet_horizon <= 3` | 搜索结果仍需 gate | 标准链同结构减少往返 | 5 分钟求解 | 搜索失控会超时 |
| `State Update / FlowGraph Rebuild` | accepted move、delta | 新状态、新图、新 trace | 车辆守恒 `100%`; rebuild 覆盖 `100%` | state change 必须能由 accepted delta 解释 | 所有案例 step 级复盘 | 结果一致性 | 状态漂移会产生假可解 |
| `Trace / failure_bucket` | 所有结构输出 | step/case/batch trace、失败桶 | `trace_field_coverage = 100%`; `failure_bucket_coverage = 100%`; unknown repeat `<= 2%` | 每个失败绑定一个主结构和最多两个辅助结构 | 代表案例全量回放 | 审核和迭代闭环 | 无法定位就无法证明方案 |
| `HumanPhaseContract / PhaseGate` | active contracts、phase metrics、resource state | H1-H5 phase、entry/exit/skip/fail | `phase_gate_bypass_count = 0`; H1-H5 指标按 20B 达标 | phase change 必须消耗/生成对应合同债务 | `0117Z` 标准全链，`0213W` 合法跳过 | 人工阶段顺序 | 没有人工阶段门就无法证明 H1-H5 完成 |

下一步把这张总表转成每个小结构的标准卡。标准卡必须使用统一模板：

```text
StructureStandardCard:
  structure_name:
  business_purpose:
  process_scope:
  upstream_inputs:
  downstream_outputs:
  online_offline_boundary:
  required_fields:
  hard_thresholds:
  reached_human_thresholds:
  exceeded_human_thresholds:
  connection_requirements:
  phase_requirements:
  human_case_checks:
  business_rule_mapping:
  performance_budget:
  failure_buckets:
  trace_fields:
  veto_conditions:
  open_questions:
```

字段要求：

| 字段 | 必填内容 | 不合格写法 |
|---|---|---|
| `business_purpose` | 这个结构解决哪个业务问题，不能只写技术职责 | “用于建模” |
| `process_scope` | 属于 P0-P10 哪些过程，是否参与 H1-H5 | “全流程都相关” |
| `upstream_inputs` | 输入对象、字段、来源、在线/离线边界 | “读取状态” |
| `downstream_outputs` | 输出对象、字段、谁消费 | “输出结果” |
| `hard_thresholds` | 触发即失败的量化指标 | “尽量避免” |
| `reached_human_thresholds` | 达到人工计划结构价值的指标 | “效果接近人工” |
| `exceeded_human_thresholds` | 超越人工所需的多指标支配条件 | “比人工更优” |
| `connection_requirements` | 上下游 id、owner、trace、delta 连续性 | “连接正常” |
| `phase_requirements` | 是否受 `HumanPhaseContract / PhaseGate` 约束 | “按阶段处理” |
| `human_case_checks` | 需要通过哪些人工代表案例 | “用案例验证” |
| `business_rule_mapping` | 对应业务文档哪条硬约束 | “符合业务” |
| `performance_budget` | step/case/batch 预算 | “不能太慢” |
| `failure_buckets` | 失败归因名称和触发条件 | “失败时记录原因” |
| `trace_fields` | 必须写入 trace 的字段 | “输出日志” |
| `veto_conditions` | 一票否决项 | “严重错误失败” |

标准卡设计应按求解链路拆成 7 组，不按代码文件拆：

| 标准包 | 包含小结构 | 先设计原因 | 完成标志 |
|---|---|---|---|
| A. 在线证据与车辆归属 | `OnlineObservable`, `OfflineLabelOnly`, `FlowGraphBuilder`, `FlowClassify`, `NoMoveVehicle`, `ResidualItem` | 决定后续所有结构是否有干净输入 | 每辆需动车都有 owner 或 residual reason |
| B. 边、状态与合同 | `FlowGraph`, `FlowEdge`, `EdgeKey`, `FlowSubject`, `FlowPort`, `FlowReceiver`, `FlowEdgeStatus`, `EdgeContract`, `ContractTemplate`, `StationFlowContract`, `RepairInboundVariant` | 决定人工计划结构能否被表达 | active edge 绑定合同率 100%，variant 代表案例准确率 100% |
| C. 目标、债务与保护 | `Blocker`, `Obligation`, `Protection`, `TargetContractSelector`, `StructuralIntentBuilder` | 决定下一步做什么，以及什么不能破坏 | target reason 覆盖 100%，hard obligation skipped = 0 |
| D. 候选、模板与 delta | `WorkPatternTemplateSelector`, `EdgeBoundedCandidateGenerator`, `ResourceRequest`, `EdgeDelta`, `ContractDelta` | 决定必要候选不漏且搜索受控 | `necessary_candidate_recall = 100%`; `candidate_family_count <= 8` |
| E. 资源、库位与机车携带 | `StationResourceGraph`, `CUN4_NORTH_BUFFER`, `DepotSlotGraph`, `DepotSwapDelta`, `GlobalGate`, `LocoPosition`, `LocoCarryState`, `WaitForGraph` | 决定大库、存4、联线、机车端别是否合法 | resource violation accepted = 0，slot/gate/carry request 覆盖 100% |
| F. 人工阶段门 | `HumanPhaseContract`, `PhaseGate` | 决定 H1-H5 是否完成或合法跳过 | `phase_gate_bypass_count = 0`; H1-H5 exit condition 可审计 |
| G. 硬门、少钩与 trace | `ResourceDelta`, `AcceptRejectGate`, `ContractOptimizer`, `LocalTieBreakSearch`, `State Update`, `FlowGraph Rebuild`, `Trace / failure_bucket` | 决定合法候选如何少钩、失败如何定位 | dominated accepted = 0，trace/failure 覆盖 100% |

推荐设计顺序：

```text
A -> B -> C -> E -> F -> D -> G
```

第一批应优先设计这些结构标准卡：

| 优先级 | 结构标准卡 | 为什么先写 | 必须包含的量化指标 |
|---|---|---|---|
| P0 | `FlowClassify` | 车辆 owner 错了，后面全部失真 | `effective_contract_coverage`, `residual_vehicle_ratio`, `no_move_false_satisfied_rate` |
| P0 | `FlowEdge / EdgeKey / FlowEdgeStatus` | 人工结构必须先被稳定表达 | `edge_identity_stability`, `active_edge_required_field_coverage`, `illegal_status_transition_count` |
| P0 | `EdgeContract / RepairInboundVariant` | 决定能否识别标准链、late 存4、短链、库内消化 | `contract_clause_completeness`, `variant_accuracy_on_representative_cases` |
| P0 | `HumanPhaseContract / PhaseGate` | 决定 H1-H5 是否可证明完成 | `phase_gate_bypass_count`, `phase_exit_condition_coverage`, `phase_transition_trace_coverage` |
| P0 | `DepotSlotGraph / DepotSwapDelta` | 决定大库能否合法先出后进 | `depot_slot_request_coverage`, `swap_required_resolved_ratio`, `depot_slot_violation_accepted` |
| P0 | `CUN4_NORTH_BUFFER / GlobalGate` | 决定存4北和联线主冲突点 | `cun4_direction_conflict_accepted`, `gate_conflict_accepted`, `resource_request_coverage` |
| P0 | `LocoCarryState / ordered carry segments` | 决定摘解、称重、关门车、端别是否合法 | `enum_only_carry_enabled`, `vehicle_conservation`, `invalid_detach_count` |
| P0 | `ContractDelta / ResourceDelta / AcceptRejectGate` | 决定硬约束是否真正生效 | `hard_clause_accepted_count`, `resource_violation_accepted_count`, `why_reject_coverage` |
| P1 | `TargetContractSelector / StructuralIntentBuilder` | 决定局部动作是否服务正确主合同 | `target_contract_reason_coverage`, `intent_contract_source_coverage`, `hard_priority_inversion_count` |
| P1 | `EdgeBoundedCandidateGenerator / WorkPatternTemplateSelector` | 决定可解性和 5 分钟性能 | `necessary_candidate_recall`, `candidate_family_count`, `planlet_horizon` |
| P1 | `ContractOptimizer / LocalTieBreakSearch` | 决定能否低于人工钩数 | `dominated_accepted_rate`, `search_scope_violation_count`, `local_branch_count` |
| P1 | `Trace / failure_bucket` | 决定失败能否定位并迭代 | `trace_field_coverage`, `failure_bucket_coverage`, `unknown_failure_repeat_rate` |

### 20C.3 结构连接审计标准

单个结构达标仍不足以推出端到端可行。必须证明结构之间没有断链、孤儿对象和绕门行为。

连接通用硬门槛：

```text
connection_trace_coverage = 100%
orphan_edge_count = 0
orphan_contract_count = 0
orphan_resource_request_count = 0
orphan_delta_count = 0
unexplained_state_change_count = 0
accepted_without_contract_delta_count = 0
accepted_without_resource_delta_count = 0
accepted_without_phase_permission_count = 0
phase_gate_bypass_count = 0
```

核心连接链必须逐段审计：

| 连接 | 需要传递的对象 | 硬门槛 | 达到人工线 | 失败后果 |
|---|---|---|---|---|
| `OnlineObservable -> FlowGraphBuilder` | online feature、source、confidence | `offline_label_used_online_count = 0`; `unknown_source_count = 0` | 低信号案例能保守建图 | 信息泄漏或无源状态 |
| `FlowGraphBuilder -> FlowClassify` | vehicle state、target、service attribute | `vehicle_id_continuity = 100%` | 每辆需动车进入分类 | 车辆遗漏 |
| `FlowClassify -> FlowGraph / FlowEdge` | contract family、role、reason | `unbound_movable_vehicle_count / movable_vehicle_count <= 5%` | 主体车流不掉 residual | 主边缺失 |
| `FlowEdge -> EdgeContract` | edge key、status、evidence、variant | `active_edge_contract_binding = 100%` | 每条主边有合同债务 | 有边无合同 |
| `EdgeContract -> TargetContractSelector` | clauses、priority、blocker、protection | `target_contract_reason_coverage = 100%` | 能解释为什么先做这条合同 | 目标乱跳 |
| `TargetContractSelector -> StructuralIntentBuilder` | target contract、suppressed reason | `intent_contract_source_coverage = 100%` | intent 不脱离合同条款 | 生成无主意图 |
| `StructuralIntentBuilder -> WorkPatternTemplateSelector` | intent、allowed/forbidden families | `template_applicability_checked = 100%` | 不复刻固定钩序，覆盖人工动作族 | 漏候选或候选泛滥 |
| `WorkPatternTemplateSelector -> EdgeBoundedCandidateGenerator` | template、horizon、subject boundary | `necessary_candidate_recall = 100%`; `candidate_family_count <= 8` | 标准动作族都有候选 | 无解或超时 |
| `CandidateGenerator -> ResourceRequest` | candidate、resource schema | `resource_request_coverage = 100%` | 每个候选先申请资源 | 资源冲突绕过 |
| `ResourceRequest -> StationResourceGraph` | requested resources、owner contract | `orphan_resource_request_count = 0` | 存4、大库、gate、机车资源都有 owner | 等待关系不可解释 |
| `StationResourceGraph -> ResourceDelta` | acquired/released/blocked | `resource_delta_coverage = 100%` | 资源收益/冲突可比较 | 放行非法占用 |
| `Candidate -> ContractDelta` | before/after contract state | `contract_delta_required_field_coverage = 100%` | 人工关键动作可解释为正 delta | 少钩无法审计 |
| `ContractDelta + ResourceDelta -> AcceptRejectGate` | hard clause、resource violation、why | `hard_violation_accepted_count = 0` | 拒绝人工错误模式和非法少钩 | 硬约束被评分抵消 |
| `AcceptRejectGate -> LocalTieBreakSearch` | legal candidates only | `illegal_candidate_in_search_count = 0` | 只在同结构合法候选中少钩 | 搜索绕过结构 |
| `LocalTieBreakSearch -> State Update` | selected move、delta | `selected_move_has_delta = 100%` | 状态变化可复盘 | 状态漂移 |
| `State Update -> FlowGraph Rebuild` | updated vehicle positions、resources | `unexplained_state_change_count = 0` | 新图继承旧债务或解释消失 | 合同断代 |
| `FlowGraph Rebuild -> Trace` | diff、metrics、failure bucket | `trace_field_coverage = 100%` | case 失败能定位结构 | 无法迭代 |
| `HumanPhaseContract / PhaseGate -> TargetContractSelector` | phase、entry/exit/skip、forbidden moves | `accepted_without_phase_permission_count = 0` | 人工计划阶段有硬门 | 局部最优破坏阶段 |
| `PhaseGate -> Trace` | phase metric、phase transition reason | `phase_transition_trace_coverage = 100%` | 能证明 H1-H5 完成或合法跳过 | 不能证明 H1-H5 业务 |

连接审计必须新增一个数据包：

| 数据包 | 内容 | 最低通过标准 |
|---|---|---|
| `connection_metrics.csv` | 每个 case、step、连接段的输入 id、输出 id、owner、trace、hard gate 结果 | 上表所有连接硬门槛 100% 达标 |

`connection_metrics.csv` 至少包含：

```text
case_id
step_index
connection_name
upstream_object_id
downstream_object_id
owner_contract_id
owner_edge_id
owner_phase
input_count
output_count
missing_output_count
orphan_output_count
trace_id
accepted
reject_reason
hard_gate_passed
failure_bucket
```

### 20C.4 人工案例对结构和连接的验收作用

人工案例不是用来要求算法复刻人工钩序，而是用来验证结构和连接有没有表达人工的关键判断。

| 案例 | 人工结构信号 | 必过结构 | 必过连接 | 量化验收 |
|---|---|---|---|---|
| `0117Z` 标准完整链 | 前段组流、存4北释放、机接、修1-修4摘解、库回 | `FlowEdge`, `FlowEdgeStatus`, `EdgeContract`, `Protection`, `DepotSlotGraph`, `ContractDelta` | `FlowEdge -> EdgeContract -> ContractDelta -> Gate -> Rebuild` | variant 正确；状态路径可解释 `100%`; 大库摘解完成前 `tail_closeout_accepted = 0`; 钩数 `<= manual * 1.05`，目标 `<= manual` |
| `0310W` late 存4链 | 前段已接近存4释放，不能回外场补长链 | `RepairInboundVariant`, `TargetContractSelector`, `CUN4_NORTH_BUFFER`, `StructuralIntentBuilder` | `Status -> Variant -> Target -> Candidate` | `FORCE_OUTER_PICKUP_ON_PORT_READY_EDGE = 0`; `PORT_READY` 后目标选择正确率 `100%` |
| `0103W` 库内消化 | 无标准机接但修库摘解明确，有称重/顶送/尾部组织 | `DEPOT_DIGEST_ONLY`, `SPECIAL_REPAIR_PROCESS`, `Obligation`, `DepotSlotGraph`, `Trace` | `Receiver -> EdgeStatus -> EdgeContract -> Obligation -> Gate` | 修库消化债务完成前 `CLOSEOUT_BEFORE_EDGE_DONE = 0`; 称重/顶送候选 hard rule 覆盖 `100%` |
| `0213W` 短链直接入库 | 人工 5 钩，已接近库侧，不应套标准长链 | `DIRECT_REPAIR_ENTRY`, `PhaseGate`, `WorkPatternTemplateSelector`, `LocalTieBreakSearch` | `Variant -> PhaseSkip -> Intent -> Candidate` | H1/H2 合法跳过；`new_outer_pickup_obligation_count = 0`; 钩数 `<= manual + 1`，目标 `<= manual` |
| `0306W` 短链低信号 | 有存4释放但机接信号不标准 | `OnlineObservable`, `FlowEdgeStatus`, `MIXED_SIGNAL_REPAIR`, `AcceptRejectGate` | `Evidence -> Confidence -> StatusGuard -> Gate` | 低置信主动跨 `ACCEPTED = 0`; contradiction reason 覆盖 `100%`; 保守候选存在率 `100%` |
| `0128W` 标准链高钩数 | 标准结构成立但有少钩优化空间 | `ContractOptimizer`, `LocalTieBreakSearch`, `LocoPosition`, `StationResourceGraph` | `LegalCandidates -> Optimizer -> Gate -> Trace` | 搜索不跨结构；`dominated_accepted_rate = 0`; 同结构少钩或二级指标至少 2 项优于人工 |
| `0223W / 0308W / 0329W` 库内消化族 | 缺标准机接但后段明确 | `DEPOT_DIGEST_ONLY`, `TAIL_CLOSEOUT`, `FailureBucket` | `Contract -> Obligation -> Trace` | 不把 `DIGESTING` 边 closeout；失败桶覆盖 `100%` |
| `0130Z / 0201W` 信号缺口族 | 前段或机接信号不完整 | `OnlineObservable`, `ResidualItem`, `RepairInboundVariant` | `Evidence -> Residual/Variant -> ConservativeCandidate` | 低信号不硬造高置信主边；residual reason 覆盖 `100%` |

人工案例族的最低通过线：

```text
representative_case_hard_gate_pass_rate = 100%
representative_variant_accuracy = 100%
representative_connection_pass_rate = 100%
representative_failure_bucket_coverage = 100%
standard_chain_hook_count <= manual_hook_count * 1.05
short_or_direct_hook_count <= manual_hook_count + 1
```

若要声称超越人工，代表案例还必须满足：

```text
hard_violation_count = 0
final_target_satisfied = true
solver_hook_count <= manual_hook_count
improved_secondary_metric_count >= 2
```

### 20C.5 业务需求到结构的闭环映射

业务文档中的要求必须落到结构、连接和指标，不能停留在描述层。

| 业务要求 | 承接结构 | 必过连接 | 量化标准 |
|---|---|---|---|
| 求解时间限制 5 分钟 | `EdgeBoundedCandidateGenerator`, `LocalTieBreakSearch`, `Trace` | `Candidate -> Optimizer -> Gate` | 单案 `runtime <= 300s`; P95 `<= 300s`; `local_branch_count <= 64`; `planlet_horizon <= 3` |
| 联7 前 60 分钟不开放 | `GlobalGate`, `HumanPhaseContract`, `PhaseGate` | `PhaseGate -> TargetSelector -> ResourceRequest -> GlobalGate` | `gate_conflict_accepted = 0`; 跨联7候选 gate request 覆盖 `100%` |
| 大库前先完成非大库 80%-90% | `HumanPhaseContract`, `StationFlowContract`, `TargetContractSelector` | `PhaseGate -> TargetSelector` | H1 退出前 `non_depot_progress_ratio >= 0.8`，目标 `>= 0.9`；若低于必须有 gate reason |
| 最大预算 40 钩完成大库前组织 | `PhaseGate`, `ContractOptimizer`, `Trace` | `PhaseGate -> HookCounter -> Trace` | `pre_depot_hook_count <= 40`，除非 hard constraint 证明必须超出 |
| 单机车去大库清理线路和集合 | `LocoPosition`, `DepotSlotGraph`, `DepotSwapDelta`, `StationResourceGraph` | `Target -> ResourceRequest -> ResourceDelta` | 大库清理动作 owner 覆盖 `100%`; `blocking_outbound_vehicle_remaining_count = 0` |
| 将大库车辆调到存4 | `DEPOT_OUTBOUND`, `CUN4_NORTH_BUFFER`, `GlobalGate` | `DEPOT_OUTBOUND -> CUN4_NORTH_BUFFER -> ResourceDelta` | `outbound_pull_completeness = 100%`; 存4方向冲突 accepted `= 0` |
| 机走编组车辆分散到大库对应位置 | `REPAIR_INBOUND`, `DepotSlotGraph`, `ordered carry segments` | `Candidate -> DepotSlotGraph -> ResourceDelta -> Gate` | `inbound_placement_completeness = 100%`; `spot_or_area_satisfaction = 100%`; `invalid_detach_count = 0` |
| 最好大库一进一出 | `HumanPhaseContract`, `LocoPosition`, `ContractOptimizer` | `PhaseGate -> Optimizer -> Trace` | `depot_entry_exit_count <= manual_depot_entry_exit_count`; 目标 `<= 1` 个完整周期 |
| 大库台位长度规则 | `DepotSlotGraph`, `AcceptRejectGate` | `DepotSlotGraph -> ResourceDelta -> Gate` | `length_slot_violation_accepted = 0`; 长度 `>= 17.6` 仅 `301-305/401-405` |
| 厂修只能 4/5 台位 | `DepotSlotGraph` | `SlotAssignment -> ResourceDelta -> Gate` | `factory_repair_slot_violation_accepted = 0` |
| 段修台位序不能超过同线厂修台位数 | `DepotSlotGraph` | `SlotAssignment -> Gate` | `section_repair_order_violation_accepted = 0` |
| 原地不动车后方不能动、空位不能占 | `DepotSlotGraph`, `NoMoveVehicle` | `NoMove -> DepotSlotGraph -> Gate` | `stayer_moved_count = 0`; `stayer_tail_occupied_violation = 0` |
| 倒车/折返需校验物理长度并加 15m 机车 | `ResourceRequest`, `ResourceDelta`, `LocoPosition` | `Candidate -> ResourceDelta -> Gate` | `reverse_length_violation_accepted = 0`; 机车 15m 必计入 |
| 警冲标/岔区可通过不可停车 | `StationResourceGraph`, `GlobalGate` | `ResourceRequest -> ResourceDelta -> Gate` | `fouling_point_storage_accepted = 0` |
| 走行线禁止停放 | `StationResourceGraph`, `ResourceDelta` | `Candidate -> ResourceDelta -> Gate` | `running_line_storage_accepted = 0` |
| 称重只在机库称重位 | `SPECIAL_REPAIR_PROCESS`, `LocoCarryState`, `ResourceDelta` | `SpecialProcess -> Candidate -> Gate` | `weighing_location_violation_accepted = 0` |
| 单钩可挂多辆称重车，但只完成尾部最后一辆 | `SPECIAL_REPAIR_PROCESS`, `AcceptRejectGate` | `ContractDelta -> Gate` | `weighing_only_tail_vehicle_marked_complete = 0` |
| 称重车必须在编组最后 | `ordered carry segments`, `ContractDelta` | `CarryState -> ContractDelta -> Gate` | `weighing_tail_order_violation_accepted = 0` |
| 关门车顺位 | `LocoCarryState`, `ResourceDelta`, `AcceptRejectGate` | `CarryState -> ResourceDelta -> Gate` | `close_door_order_violation_accepted = 0` |
| 重车牵引折算 | `ResourceRequest`, `ResourceDelta` | `Candidate -> ResourceDelta -> Gate` | `traction_over_limit_accepted = 0`; 重车按 4 辆折算 |
| 预修/调棚/功能线不是普通存车 | `StationFlowContract`, `FlowReceiver`, `ContractTemplate` | `FlowClassify -> ContractTemplate -> Receiver` | 相关合同族覆盖 `>= 95%`; residual 比例 `<= 5%` |

### 20C.6 人工阶段的结构充分性复审

对用户关心的“到底分成几个阶段比较合适”，独立复审结论是：阶段应采用 H1-H5，而不是原先的 S1-S4。

| 阶段 | 若哪些结构达标 | 是否能完成该阶段 | 钩数不高于人工的必要条件 | 可解性条件 |
|---|---|---|---|---|
| H1 前段全站组织与服务处理 | `FlowClassify`, `StationFlowContract`, `YARD_REBALANCE`, `FUNCTION_LINE_SERVICE`, `PRE_REPAIR_STAGING`, `DISPATCH_SHED_QUEUE`, `LocoCarryState` | 可以。非大库主体债务和大库主流 owner 能被区分，前段动作不再被当作杂活。 | `front_service_progress >= 0.8`，目标 `>= 0.9`; `manual_H1_hook_count` 可比；同结构搜索不接受劣解。 | `effective_contract_coverage >= 95%`; residual `<= 5%`; 失败有 owner/blocker/fallback。 |
| H2 存4释放口成形与保护 | `CUN4_NORTH_BUFFER`, `FlowPort`, `Blocker`, `Protection`, `TargetContractSelector`, `StationResourceGraph` | 可以。存4北能从普通线路提升为释放口资源，并识别污染、方向和阻塞。 | `cun4_port_shape_ready = true`; 存4污染次数 `<= manual_cun4_dirty_count`。 | `port_shape_reason_coverage = 100%`; `cun4_direction_conflict_accepted = 0`。 |
| H3 存4大释放-机接硬边界 | `RepairInboundVariant`, `FlowEdgeStatus`, `EdgeContract`, `Protection`, `ResourceRequest`, `AcceptRejectGate` | 可以。标准链能连续处理 `存4 - N 北头摘 -> 机 + N 接`，短链/低信号可合法跳过或保守。 | 标准链 `release_accept_gap <= 2`，目标 `<= 1`; `manual_H3_hook_count` 可比。 | `strict_release_candidate_recall = 100%`; `machine_accept_candidate_recall = 100%`; 低置信跨 `ACCEPTED = 0`。 |
| H4 修库消化、库位和出入库冲突处理 | `DepotSlotGraph`, `DepotSwapDelta`, `DEPOT_OUTBOUND`, `REPAIR_INBOUND`, `Obligation`, `ResourceDelta`, `ordered carry segments` | 可以。修库摘解、slot/band、stayer、swap 和必要出库腾位能被统一处理。 | `depot_digest_complete = true`; `swap_required_resolved_ratio = 100%`; 大库进出次数 `<= manual_depot_entry_exit_count`。 | `depot_slot_violation_accepted = 0`; `invalid_detach_count = 0`; H4 失败必须给 slot/swap/端别原因。 |
| H5 尾项收束与库回 | `TAIL_CLOSEOUT`, `YARD_REBALANCE`, `FUNCTION_LINE_SERVICE`, `LOCO_AREA_STAGING`, `SPECIAL_REPAIR_PROCESS`, `Trace` | 可以。主合同完成后再处理库回、机区、功能线、存车整理和剩余目标。 | `tail_closeout_complete = true`; 总钩数 `<= manual_hook_count`。 | `final_target_satisfied = true`; `hard_violation_count = 0`; 主体债务未完 closeout `= 0`。 |

因此：

```text
H1_passed
  and H2_passed
  and H3_passed
  and H4_passed
  and H5_passed
  and all_phase_connections_passed
  and hook_not_higher_than_human
=> 人工阶段达到人工
```

其中：

```text
all_phase_connections_passed =
  phase_gate_bypass_count = 0
  and accepted_without_phase_permission_count = 0
  and phase_transition_trace_coverage = 100%
  and phase_obligation_carryover_error_count = 0
```

如果没有人工阶段级钩数切分，只能证明：

```text
case_hook_not_higher_than_human =
  solver_hook_count <= manual_hook_count
```

不能证明：

```text
H1_hook_count <= manual_H1_hook_count
H2_hook_count <= manual_H2_hook_count
H3_hook_count <= manual_H3_hook_count
H4_hook_count <= manual_H4_hook_count
H5_hook_count <= manual_H5_hook_count
```

### 20C.7 为什么这些局部指标同时达标后足以达到或超越人工

人工调车的优势不是枚举能力，而是结构判断：

```text
什么时候先整理外场；
什么时候保护存4北；
什么时候释放并机接；
什么时候先出库腾位；
什么时候入库摘解；
什么时候才可以尾项收束；
什么时候短链不应强套长链。
```

本方案要达到人工，必须把这些判断拆成可验证结构：

| 人工判断 | 方案结构 | 充分条件 |
|---|---|---|
| 先把主体流分清 | `FlowClassify + StationFlowContract` | 主体合同覆盖 `>= 95%`，residual `<= 5%` |
| 保护存4释放口 | `CUN4_NORTH_BUFFER + ResourceDelta` | 存4四态识别 `100%`，方向冲突 accepted `= 0` |
| 机接后不乱拆 | `FlowEdgeStatus + Protection + ContractDelta` | protection broken accepted `= 0` |
| 先出库腾位再入库 | `DepotSlotGraph + DepotSwapDelta + PhaseGate` | swap required resolved `= 100%` |
| 库内摘解完成前不收尾 | `Obligation + TAIL_CLOSEOUT Gate` | `tail_before_primary_done_count = 0` |
| 短链直接入库不套长链 | `RepairInboundVariant + PhaseSkip` | 代表变体准确 `100%` |
| 低信号不冒进 | `OnlineObservable + Confidence + Gate` | 低置信跨 `ACCEPTED = 0` |
| 少钩不破坏结构 | `ContractOptimizer + LocalTieBreakSearch` | search scope violation `= 0`; dominated accepted `= 0` |

这些条件同时成立时，算法不是在模仿人工计划，而是在复现人工计划的结构判断，并用候选裁剪、资源显式仲裁和局部少钩搜索减少人工计划中的经验性往返。因此它具备达到人工的充分结构条件。

要进一步超越人工，还必须满足：

```text
human_structural_value_preserved = true
hard_violation_count = 0
final_target_satisfied = true
solver_hook_count <= manual_hook_count
runtime <= 300s
trace_field_coverage = 100%
failure_bucket_coverage = 100%
improved_secondary_metric_count >= 2
```

二级指标只允许从以下已定义指标计数：

```text
manual_switch_count
manual_path_cost
manual_loco_empty_distance
manual_cun4_dirty_count
manual_depot_entry_exit_count
manual_depot_swap_conflict_count
manual_temp_buffer_use_count
```

## 21. 成功标准

第一层：全站合同覆盖可解释

- 每个状态都能输出当前 `FlowGraph + FlowClassify`。
- 每条主体车流都能解释 `subject / via_port / to_receiver / status / contract`。
- 必须通过遮答案在线识别测试：只输入 StartStatus，不输入人工动作序列。
- 每个 `status` 必须输出 `online_status_reason`，且来源只能是几何、资源、目标、修程、机车位置或已执行 solver move。
- `manual_order_signals` 只能用于 hidden label comparison，不能进入识别器输入。
- 12 个真实案例中，`effective_contract_coverage >= 90%`。
- 研究目标中，`effective_contract_coverage >= 95%`。
- 必须单独输出 `gross_contract_coverage / no_move_vehicle_ratio / effective_contract_coverage`，不能用不动车虚增有效覆盖率。
- `residual_vehicle_ratio <= 10%`，研究目标 `<= 5%`。
- 缺信号 case 能输出 `confidence`、`contradictions` 和 `residual_reason_breakdown`。

第二层：合同变化可比较

- 每个候选动作都能输出 `ContractDelta`。
- 能说明这个动作履行了哪些条款、破坏了哪些条款、还是没有履约。
- 能说明新增或减少了哪些合同债务/blocker。
- 每个候选动作都能输出 `ResourceDelta`。
- 能说明是否申请/占用/释放 `CUN4_NORTH_BUFFER / LINK6_GATE / LINK7_GATE / LOCO_POSITION / LOCO_CARRY_STATE`。
- 能说明是否申请/占用/释放 `DEPOT_SLOT_RESOURCE`，并输出 `DepotSwapDelta`。
- 少钩评价必须使用 `LOCO_POSITION` 的 track、side、accessible_end、distance_to_target_contract。
- 资源等待必须能输出 `WaitForGraph`。
- 出现环形等待时必须输出 `RESOURCE_DEADLOCK`、cycle 和 break action。

第三层：人工案例一致

- `0117Z` 能看到标准边逐步推进。
- `0310W` 不补外场。
- `0104W / 0213W` 不强制长链。
- `0130Z / 0201W / 0306W` 不低置信主动机接。
- `0103W / 0223W` 不把 `DIGESTING` 边 closeout。
- 非修库主体车流不能掉 residual：`DEPOT_OUTBOUND / PRE_REPAIR_STAGING / DISPATCH_SHED_QUEUE / YARD_REBALANCE / FUNCTION_LINE_SERVICE / LOCO_AREA_STAGING / SPECIAL_REPAIR_PROCESS` 都必须有合同。
- 存4北双向争抢必须能输出资源仲裁结果。
- 大库出入库必须能输出 slot/band 级 swap 依赖；不能只靠 `DEPOT_TRACK_CAPACITY` 判断可入库。
- 联6/联7 全局门必须能输出 gate slot 申请和拒绝理由。
- `loco_carry` 不允许 enum-only 压缩上线；如要压缩，必须通过 baseline 对比实验：不降低可解性，不产生非法摘挂/端别错误/合同违反。

第四层：高于人工

- 不要求复刻人工动作顺序。
- 只要求 ContractDelta 不破坏人工总结出的结构价值。
- 可以在同样结构价值下选更少钩的动作。

---

## 22. 一句话总结

**把调车建模成全站车辆沿不同业务流向推进的 `FlowEdge`，先用 `FlowClassify` 过滤不动车并归入合同族，再用 `EdgeContract` 定义每条边当前必须履行的合同，最后用 `StationResourceGraph` 仲裁存4北、联6/联7、大库 slot/band swap、缓冲容量和 `loco_carry` 有序携带段。求解器不应该只问“修库主链怎么走”，而应该先问“需动车是否都有合同”，再问“目标合同能否拿到资源”，最后才问“候选动作是否履约且少钩”。**

---

## 23. 实施和验证入口

本设计文档只保留目标结构、验收标准和结构之间的连接关系，不再记录多轮运行历史。当前 runtime 结果、人工计划差距、R1-R6 状态和下一步结构工作统一见：

```text
docs/P10_人工差距结构诊断.md
artifacts/current_truth2_eval/
```

当前验证入口：

```bash
rtk python3 scripts/generate_vnext_runtime_trace.py --root . --output-dir artifacts/current_truth2_eval --max-hooks 300 --check
```

用于判断方案是否真正接近人工的核心指标：

| 指标 | 来源 | 用途 |
|---|---|---|
| `business_get_put_hook_count` | `case_summary.csv` | 总业务勾数，和人工计划勾数对比 |
| `remote_business_transition_count` | `case_summary.csv` | 远端/非远端业务勾切换次数，衡量远端来回调车 |
| `manual_vs_solver_case_compare.csv` | 当前 artifact | 人工勾数、远端勾数、远端 session 对比 |
| `structural_repair_acceptance.csv` | 当前 artifact | R1-R6 当前是否通过 |
| `structure_work_audit.csv` | 当前 artifact | 六个结构工作的细粒度失败原因 |

严谨结论仍然是：

```text
P10 物理合法性通过，不等于 P0-P10 结构全部达标。
只有 R1-R6 通过、113 案完成、硬物理违规为 0、业务勾数和远端切换指标达到人工计划基准后，
才能说当前求解器达到甚至超过人工。
```
