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

这和普通阶段顺序不一样。

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

## 23. 阶段1-5 实施记录

当前已经完成的是研究/旁路实现，不是生产求解器强制接管。

也就是说：

```text
已完成:
  能从 StartStatus 建 FlowGraph。
  能给动作打 ContractDelta。
  能用 AcceptRejectGate 判断硬违反。
  能构建 DepotSlotGraph / DepotSwapDelta。
  能构建 ordered carry segments 并证明 enum-only carry 不安全。

未完成:
  尚未把 hard gate 接入 runtime move generator。
  尚未把 DepotSlotGraph 接入 runtime TargetContract 排序。
  尚未做 baseline solver vs compressed solver 的真实 A/B。
```

实现文件：

```text
src/fzed_shunting/workflow/phase1_v2/flow_graph.py
src/fzed_shunting/workflow/phase1_v2/contract_delta.py
src/fzed_shunting/workflow/phase1_v2/depot_slot_graph.py
src/fzed_shunting/workflow/phase1_v2/carry_state.py
scripts/validate_flow_edge_foundation_experiments.py
```

验证文件：

```text
tests/workflow/test_phase1_v2_flow_graph.py
tests/workflow/test_phase1_v2_contract_resource_carry.py
```

当前 `truth2_force` gate：

```text
case_count = 113
vehicle_count = 9506
movable_vehicle_count = 5757

Stage 1:
  effective_contract_coverage = 98.3%
  residual_vehicle_ratio = 1.7%
  mixed_source_vehicle_count = 2589
  mixed_source_contract_coverage = 98.69%
  gate = passed

Stage 2:
  contract_delta_probe_count = 813
  fulfills_contract_count = 702
  mode = synthetic_probe_observation_only
  gate = passed

Stage 3:
  hard_violation_probe_count = 111
  mode = hard_gate_probe_only_not_runtime_filter
  gate = passed

Stage 4:
  cases_with_same_track_swap_pressure = 110 / 113
  strict_slot_block_count_order_approximation = 911
  requires_depot_slot_graph = true
  gate = passed

Stage 5:
  carry_order_risk_case_count = 112 / 113
  adjacent_target_switch_count = 1040
  enum_only_compression_allowed = false
  ordered_carry_segments_required = true
  gate = passed
```

执行命令：

```bash
rtk pytest tests/workflow/test_phase1_v2_flow_graph.py tests/workflow/test_phase1_v2_contract_resource_carry.py -q
rtk python scripts/validate_flow_edge_foundation_experiments.py --sets truth2_force --output artifacts/flow_edge_foundation_experiments.json
```

下一步接 runtime 的顺序：

```text
1. 在 move generator 后、候选排序前接 ContractDelta 旁路 trace。
2. 只记录 selected / rejected candidate 的 delta，不先过滤。
3. 当 0310W、0103W 等人工错误模式能稳定标 hard violation 后，再打开 hard gate。
4. DepotSlotGraph 先进入 TargetContractSelector 排序，不直接改 replay。
5. loco_carry 保留 ordered sequence，4 值状态只作为资源标签。
```
