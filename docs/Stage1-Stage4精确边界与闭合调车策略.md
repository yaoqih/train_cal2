# Stage1-Stage4 精确边界与闭合调车策略

## 1. 文档定位

这份规格回答的不是“哪条线平均出现多少次”，而是运行时面对一个具体状态时：

- 哪些车现在必须动；
- 哪些车虽然能动，但现在不应动；
- 目标线什么时候算干净，什么时候必须联合重建；
- `机南/洗油北/机走棚/调梁线北/存5线北/存4线` 什么时候能成为资源；
- 如何在一个最终空挂的 session 内实现多摘、多挂、部分摘后继续挂；
- 第一批和第二批去大库车怎样形成，分别由哪个阶段拥有；
- 无法生成动作时应输出哪个缺失前提，而不是切换 fallback。

本文按当前四阶段代码的真实责任划分：

| 代码阶段 | 当前硬责任 |
|---|---|
| Stage1 | 去卸轮车编入 `存4线`；去大库车编入显式 assembly contract；同时完成会阻碍 assembly/后续阶段的前场服务和 gate 释放 |
| Stage2 | 卸轮翻库、大库出库和 `存4线` 出段主列，维护 `OFF* C4*` 边界 |
| Stage3 | 从 assembly line 取去大库车，完成修1-4库内/库外最终位置 |
| Stage4 | Stage3 完成后的存车、洗油抛、调梁、预修、机区和强台位残债；不重新接管修库目标和卸轮目标 |

业务 P1-P5 与代码 Stage1-Stage4 不是同一套编号。业务 P3/P4 分别主要落在代码 Stage2/Stage3；不能再把修库出库、存4释放和大库入库写成 `stage4_simple` 的责任。

## 2. 不能违反的物理骨架

关键串联系统是：

```text
抛丸线 - 渡10 - 机南 - 机走棚 - 机走北 - 渡5

洗罐站 - 洗罐线北 - 洗油北 - 机走棚
油漆线 ------------ 洗油北 - 机走棚

调梁棚 - 调梁线北 - 渡4 - 机北2
                         \- 机库线

存5线南 - 存5线北 - 渡1

存4线 - 存4南 - 存3线
存4南 - 渡8

预修线 - 存2线
预修线 - 渡7
```

这些边不是“线路相邻即可通过”。每次 Get/Put 还必须同时满足：

1. 只能从规定操作端取得北端连续前缀；
2. Put 只能摘机后尾段，不能从车列中间抽车；
3. 占用线路不能被当作普通通路穿越；
4. 折返时要检查机车 15m、机后车辆长度和既有阻塞车长度；
5. 机后折算辆始终不超过 20；
6. 线路实际长度和强制台位必须满足；
7. `存5线南` 只能经 `存5线北` 操作；
8. 新车不能从北端物理插入目标线既有车内部，除非先取出冲突窗口再重放。

策略层只决定“应尝试什么意图”；每一条操作仍必须由 `physical.validate_candidate` 逐步验证。

## 3. 精确定义线路状态

### 3.1 目标线不是简单的空或有车

对目标线 `T` 和待放批次 `B`，定义四种状态。

#### `direct_ready(T, B)`

必须同时满足：

1. `B` 是某源线当前可取连续块，或当前机后尾段；
2. 源到 `T` 的动态路径可达；
3. `B` 加入后不超容量；
4. `planned_positions_for_batch` 能生成完整位置；
5. 不移动 `T` 的既有车，也能从北端 Put 得到最终合法顺序；
6. `T` 上既有车全部满足且没有待迁出车；
7. 当前已知的后续 inbound block 不需要插入 `B` 的南侧；
8. Put 后不会新增其他残债的 route/access/gate lock。

因此：空目标通常是 `direct_ready`，但路径被 gate 挡住时仍不是；有车目标如果可以合法北端追加，也可以是 `direct_ready`。

#### `local_rebuild_ready(T, B)`

`T` 不能直接接收，但冲突窗口能在一个闭合 session 内完成：

- 只需取出一个连续目标窗口；
- 所有临时移出的既有车能在同一 session 恢复；
- `B` 与该窗口能一次形成最终顺序；
- session 结束时 `T` 不需要因当前已可见来源再次打开。

#### `joint_rebuild_ready(T)`

存在多个来源、保护前缀或 route blocker，但它们的完整依赖闭包当前都可取得，并且已经找到一个最终空挂、保护车恢复、gate 释放的联合 session。

#### `not_ready(T)`

出现任一情况即为 `not_ready`：

- 仍有必须进入 `T` 的来源块尚不可取；
- 目标最终台位窗口尚不能确定；
- 需要的 staging line 没有 owner 或退出路径；
- 某个 blocker 只能被临时放下，却无法在同一 session 恢复/完成；
- 当前重建后已知还必须再次打开 `T`；
- 任何一步会占住未释放 gate。

`not_ready` 目标不能靠“先放一部分再说”推进。

### 3.2 阻塞深度用结构向量，不用车辆数量阈值

对一个意图计算：

```text
source_prefix_equivalent   源线为取得目标块必须挂出的折算辆
source_blocker_runs        前缀中非目标连续段数量
protected_blocker_runs     必须原线恢复的已满足连续段数量
route_blocker_lines        Get/Put 动态路径上的占线 blocker 数量
target_replay_equivalent   目标线必须取出重放的折算辆
future_inbound_blocks      本次之后仍需进入同一目标的已知块数
temporary_put_get_pairs    必须临停后回取的块数
gate_leases                session 中必须保持开放的 gate 集合
```

按结构分级：

| 等级 | 精确含义 | Stage1 | Stage4 |
|---|---|---|---|
| D0 直送 | `direct_ready`，不移动 blocker，不新增 target reopen | 必须优先 | 可处理 Stage1 未覆盖的残余 |
| D1 局部清理 | 单一来源闭包；blocker 全部真目标落位或原线恢复；一次 session 关闭 | 当它关闭服务 gate、暴露 Stage1 车或降低全流程下界时处理 | 可处理 |
| D2 联合闭合 | 多来源/多目标/目标既有车，但完整依赖闭包当前可解 | 只处理 mandatory gate-close 或形成 assembly contract 的 session | 主体责任 |
| D3 前提未齐 | 缺来源、缺 staging exit、缺 gate release 或必然再次开目标 | 不搬半成品 | 先完成明确 prerequisite；仍不齐则诊断停止 |

“浅阻塞”不再定义成 3 辆或 45m；只要无法证明一次闭合，它就是 D3。反过来，即使涉及 8 辆，只要逐步牵引合法且一个 session 能完全退出，也可以是 D1/D2。

### 3.3 线路释放条件

线路 `L` 只有同时满足以下条件才能承接非真实目标车：

```text
service_dependents(L) 已关闭
L 自身没有待进、待出或待重建债务
projected Put 不让任何 pending car 的 service_available/access_available 从 true 变 false
承接批次有 assembly/staging owner
已证明从 L 到 owner 最终目标的退出 session
退出前不需要穿过被该批车占住的 L
容量、端别和车序合法
```

允许在一个 session 内短时占用尚未释放的线，但该线必须在 session 结束前恢复到初始状态或清空；这不等同于允许阶段间持久占用。

## 4. 逐线 gate 与资源边界

### 4.1 抛丸、机南、机走棚

`机南` 的持久使用条件不是“抛丸线上当前没车”，而是抛丸服务合同关闭：

```text
没有外部车仍需进抛丸线
抛丸线上没有车仍需迁出
抛丸强制位置/容量合法
没有后续已声明 session 仍需通过机南侧操作抛丸
模拟占用机南后，所有其他 pending route/access 仍可达
```

规则：

- 外部仍有去抛丸车时，禁止把抛丸去大库车持久放到 `机南`；这会把抛丸后续路径关在本轮之外。
- 如果先 Get 抛丸去大库车后，抛丸合同已经关闭，可以在同一个 session 的末尾 Put `机南`。
- 如果抛丸合同尚未关闭，优先保留该去大库段在机后，继续 Get 外部抛丸车，先 Put 抛丸目标车，再 Put 去大库段到刚释放的 `机南`。
- 若带车去第二来源的路线不合法，使用有 owner 的中性 staging；不能把 `机南` 当默认 staging。
- `机走棚` 同时是 `机南` 和 `洗油北` 两支的共同前置线。只关闭抛丸不代表 `机走棚` 可以承接，仍要检查洗罐/油漆和机走棚自身债务。

### 4.2 洗罐、洗北、油漆、洗油北

`洗油北` 的 dependent family 是一个联合合同：

```text
洗罐站 inbound/outbound
洗罐线北 inbound/outbound
油漆线 inbound/outbound
这些目标的强制位置重建
```

规则：

- 洗罐站完成但油漆仍需进出时，`洗油北` 仍然锁定。
- 油漆完成但洗罐站或洗北仍需进出时，`洗油北` 仍然锁定。
- `洗罐线北` 只有在洗罐站不再进出后才能承接无关车。
- 洗罐/油漆去大库车应在关闭对应服务窗口时顺手抽出；若 `洗油北` 尚未释放，保留在机后或放到显式 staging，不能提前压入洗油北。
- 只有洗罐站、洗北、油漆三个窗口都关闭，并且动态 guard 通过后，才允许把 `洗油北` 纳入第一批 assembly line。

### 4.3 机走棚、机走北

`机走棚` 既是作业目标，又是两条西侧支路的共同 gate；`机走北` 又是机走棚的操作口。

规则：

- 仍有车需进出 `机走棚` 时，`机走北` 不能成为持久 assembly line。
- 仍有抛丸/机南或洗罐油漆/洗油北操作时，`机走棚` 不能成为持久 assembly line。
- `机走棚` 自身有预修目标车时，应先确定其最终窗口；不能先塞去大库车，再由 Stage4 整线拉开。
- 只有两支前场 family 和机走棚自身合同都关闭后，`机走棚` 才能成为第二批承接线。
- `机走北` 只有在机走棚不再进出，或其占车会被下一个同 session Get 立即清掉时可用。

### 4.4 调梁棚、调梁线北、机北2、机库线

规则：

- `调梁线北` 在调梁棚仍有任何 inbound/outbound/rebuild 债务时是硬 gate，不是缓存线。
- `机北2` 在调梁棚或机库线仍需经渡4进出时保持 gate 角色。
- `机库线` 只有 71.6m，适合小段、称重和明确机库目标，不适合无界第一批主编组。
- 有称重车时，称重顺序是 assembly contract 的一部分；不能用普通 Put 机库线代替称重完成。
- 调梁棚强台位必须先收集当前可取得的全部 inbound block，再决定最终窗口。逐来源插入会产生连续 target rebuild。
- 调梁棚目标窗口未关闭前，不把去大库车永久甩到调北或机北2；可在联合 session 中短时取下并恢复。

### 4.5 存5北、存5南

规则：

- `存5线北` 是 `存5线南` 唯一操作口，原则上只取不新增无关车。
- 操作存5南前先计算存5北完整前缀，不允许假设可从南侧绕入。
- 去存5南车辆、容量 holdout、前缀保护车必须在同一个 `cun5_segment_session` 中联合规划。
- 容量不足时，holdout 留在有 owner 的非 gate 线；不能先把所有车送南、失败后再挑一辆拉回。
- 为了深挖存5南而移动的存5北非目标车，必须在同 session 真目标落位或恢复，不留下“以后再收”的普通缓存。

### 4.6 存1、存2、存3、预修、存4南

这些线容量大于机区短线，但不是无条件中性：

- `存3线` 与 `存4南` 共用操作关系，Stage2/存4释放前不能形成无法清理的北端占车。
- `存2线` 与 `预修线` 有折返长度耦合；预修仍有进出债务时，必须由动态路径验证决定能否承接。
- `存1线` 受 `机北1` 操作口影响，机北1/机北2仍有任务时不盲目压车。
- 只有真实存车目标可无 owner 长期保留；临编车辆必须声明 `staging_owner` 和 `exit_session`。
- `存4南` 是走廊，不作为阶段间资源线。

### 4.7 存4线

`存4线` 在当前流水线中首先属于 Stage2：Stage1 结束时只允许去卸轮阶段车留在存4，Stage2 还要维护出段主列 `OFF* C4*` 顺序。

因此，去大库车可以使用存4北的唯一合法方式是原子 `store4_marshal_session`，而不是阶段出口半成品：

```text
session 开始前：存4无 Stage2-owned/卸轮/出段保护车，或这些车完全不参与且顺序可原样恢复
session 内：多个来源按最终回取顺序的逆序 Put 存4
session 内：一次 Get 存4形成主列
session 内：Put 已释放且已预约的 assembly line
session 结束：存4恢复初始状态，去大库车为 0，TrainCars=[]
```

额外硬条件：

1. `存4南` 路径和 `存3线` 相关折返合法；
2. 存4容量、关门车位置和北端 Put 顺序合法；
3. 最终 assembly line 已释放且容量足够；
4. 从 assembly line 到 Stage3 的退出路径已证明；
5. 该 session 相比各来源直接送 assembly 至少减少一次未来闭合/目标重开，或它是形成正确主列顺序的必要条件；
6. Stage2 尚未开始，中间不能插入其他宏。

不满足任一条件时，不使用存4预编组。

若 Stage1 还要把去卸轮车最终放入存4，`store4_marshal_session` 必须发生在第一辆去卸轮车最终落入存4之前。去卸轮合同一旦占用存4，存4立即转为 Stage2 reserved，本案不再生成去大库预编组。

### 4.8 快速释放矩阵

| 拟承接线路 | 持久承接前必须关闭 | 仅满足下面条件仍不够 | 有效长度 |
|---|---|---|---:|
| `机南` | 抛丸 inbound、outbound、台位和后续路径合同 | 只看抛丸线当前为空 | 90.1m |
| `洗油北` | 洗罐站、洗罐线北、油漆线联合合同 | 只完成洗南/洗北，油漆仍开放 | 62.9m |
| `机走棚` | 抛丸/机南支、洗罐油漆/洗油支、机走棚自身合同 | 只关闭其中一个支路 | 111.1m |
| `机走北` | 机走棚不再进出，且渡5/机北2方向无新增锁 | 机走棚当前为空但仍有 inbound | 69.1m |
| `调梁线北` | 调梁棚 inbound、outbound、强台位重建 | 调梁棚当前没有可取车 | 70.1m |
| `机北2` | 调梁棚、机库线经渡4的当前合同 | 只关闭调梁棚但机库仍需进出 | 55.7m |
| `机北1` | 存1、机北2相关进出合同 | 当前线路为空 | 81.4m |
| `存5线北` | 存5线南本轮进出合同 | 存5南当前没有可取前缀 | 367.0m |
| `机库线` | 称重、机库真实目标和渡4退出合同 | 仅有剩余容量 | 71.6m |
| `存4线` | 原子 marshal 条件；阶段出口只允许 Stage2-owned 车 | 仅有剩余容量 | 317.8m |

容量按车辆实际长度和 0.5m 容差判断，不按“约几辆”判断。表中语义关闭只是最低条件，最终仍以 projected route/access/gate guard 为准。

## 5. Stage1 与 Stage4 的精确边界

### 5.1 Stage1 必须完成的工作

Stage1 completion debt 不应只包含“所有去大库车在四条 assembly line”。它还必须包含 `mandatory_service_debt`：

1. 挡住 Stage1 去大库/去卸轮车源前缀的服务车；
2. 占住所选 assembly line 或其必要路径的服务车；
3. 未关闭就会使 Stage2/Stage3/Stage4 新增 route/access lock 的 gate family；
4. 当前 `direct_ready` 且完成后不会破坏 assembly 的前场真实目标；
5. 为形成 Stage3 `required_access_order` 必须处理的混合前缀。

这些服务动作必须与 assembly 主合同在同一个候选池中交替选择，不能等 assembly 全部结束后才进入一个固定 4 planlet/16 钩的 service tail。

### 5.2 Stage1 可以做但不是完成条件的工作

- 独立、干净、当前可直送的调梁/预修/存车目标；
- 能在一个 D1 session 内关闭且明确减少全流程剩余闭合数的目标；
- 第一批/第二批 assembly 的排序塑形；
- 原子存4预编组。

### 5.3 Stage1 必须延期的工作

- 来源尚未齐全的调梁棚/洗罐站最终窗口；
- 不能在同 session 恢复的 protected prefix；
- 需要占用未释放 `机南/洗油北/机走棚/机走北/调北/机北2` 的临编；
- 无 assembly owner 的缓存；
- Stage2 出段、Stage3 入库本体；
- 只减少当前 Stage1 debt、但会增加 Stage4 target reopen 或 route lock 的动作。

### 5.4 Stage4 接收边界

Stage4 输入必须满足：

```text
Stage2 complete
Stage3 complete
没有修库/卸轮可行动债务需要 Stage4 猜测
所有阶段间 staging 都有 owner，或已经由 Stage3 消费
没有由 Stage1 新增的 active gate lock
每个 Stage4 residual 至少有 direct/local/joint intent，或明确 prerequisite
```

Stage4 负责 D0-D2 的前场残余。D3 不能用缓存试探；应输出缺少的 source、gate、capacity holdout 或 exit contract。

## 6. 第一批与第二批 assembly 策略

### 6.1 assembly contract

每个去大库批次不是“某条线上的一堆车”，而是：

```text
contract_id
owner = Stage3
assembly_line
ordered_blocks = [(depot target/slot family, ordered car nos)]
required_access_order
weigh_sequence
capacity_used
gate_release_certificate
stage3_exit_path_certificate
```

Stage3 应把 `required_access_order` 作为上游合同输出，Stage1 按这个顺序编组；不能只用目标 run 数做友好度猜测。

### 6.2 第一批：前场服务释放批

来源范围：

- 抛丸、油漆、洗罐站、洗罐线北中需要去大库的车；
- 为关闭上述服务窗口，从存5北/南、存2、存3等源线顺手暴露的去大库连续块；
- 同一闭合 session 中已完成称重且属于该批的车。

执行顺序不是固定“抛丸永远第一”，而是：

```text
先执行 direct_ready 真实目标
再选 unlock_gain 最大的前场 family-close session
抛丸关闭后，机南才进入 assembly 候选
洗罐站+洗北+油漆联合关闭后，洗油北才进入 assembly 候选
若两支都未关闭，去大库块保留在机后或进入有退出合同的中性 staging
```

第一批 assembly line 从“已释放集合”动态选择，不再按静态 `机南 -> 洗油北 -> 机走棚 -> 机走北` 排名：

```text
eligible(line) = released(line)
              and no own service debt
              and capacity fits
              and required_access_order can be built
              and Stage3 exit remains reachable
```

### 6.3 第二批：调梁/机库/预修释放批

第一批关闭西侧前场 family 后，再处理：

- 调梁棚/调梁线北中的去大库车；
- 机库线已称重或机库目标完成后的去大库车；
- 预修线/机走棚中的去大库车；
- 存车线剩余、需要与上述块合并的连续段。

规则：

1. 调梁棚先求完整 target window，再抽出去大库 blocker；不能每出现一个来源就重建一次。
2. 调棚仍需进出时，第二批不能落调北/机北2。
3. 机库线仍有称重/目标债务时，不能把第二批塞满机库。
4. 机走棚只有在抛丸支、洗油支和机走棚自身合同都关闭后可承接。
5. 机走北只有在机走棚不再进出后可承接。

### 6.4 Stage1 退出检查

```text
所有 depot_inbound car 属于一个 assembly contract
存4线只保留 Stage1 允许的卸轮阶段车
机南/洗油北/机走棚/机走北上的车都有 Stage3 owner
每条 assembly line 的 access order 满足 Stage3 contract
不存在未关闭 family 被 assembly car 占住 gate
不存在无 owner 的存1/2/3/调北/洗北临编车
所有 submitted session TrainCars=[]
```

## 7. 多摘多挂的具体闭合语法

### 7.1 直送

```text
Get S: block B
Put T: block B
```

只在 `direct_ready(T, B)` 时生成。目标干净时它永远优先于为了凑多摘而绕行。

### 7.2 抛丸 gate-close retained session

状态：抛丸线上有去大库块 `A`，外部源 `S` 有去抛丸块 `B`。

优先尝试：

```text
Get 抛丸线: A
Get S: B                    # 保留 A；逐步牵引和路径合法
Put 抛丸线: B              # B 必须位于机后尾端
Put 机南/assembly: A        # 此时抛丸合同已关闭，重新计算 released
```

若第二个 Get 带车不可达：

```text
Get 抛丸线: A
Put staging X: A            # X 有 owner 和本 session 退出
Get S: B
Put 抛丸线: B
Get X: A
Put released assembly: A
```

禁止的缩短版本：

```text
Get 抛丸线: A
Put 机南: A                 # 抛丸 inbound B 尚未完成
```

### 7.3 洗罐/油漆联合 gate-close session

可以在机后保留去大库块，连续完成洗罐或油漆尾段：

```text
Get 洗罐站/洗北: depot block A
Get source S1: wash block B
Put 洗罐站: B
Get source S2: paint block C      # 若 A 保留且路径/牵引合法
Put 油漆线: C
Put 洗油北/assembly: A            # 仅在 wash+north+paint 全关闭后
```

如果油漆仍有不可取来源，最后一步不能 Put 洗油北；要么继续完成来源，要么把 A 放到非 dependent staging 并在同 session 退出。

### 7.4 混合源线 fan-out

源线北端目标 run 为 `[T1:A, T2:B, T1:C]` 时，不能要求目标互不重复。规划器按机后尾段执行：

```text
Get S: A+B+C
Put T1: C                  # 若 T1 当前窗口允许先放深/浅段
Put T2: B
[Get S2: D for T1]
Put T1: A+D
```

每次 Put 后保留的 consist 必须仍有明确下一动作；如果 `T1` 最终窗口不能分两次关闭，则 A/C/D 应先联合排成一次最终 Put，而不是机械执行上式。

### 7.5 同目标多来源一次关闭

```text
Get S1: block A
Get S2: block B
[Put another tail target / retain target block]
Get S3: block C
[Get T: conflicting existing window E]
Put T: final_layout(E+A+B+C)
```

生成前先确定 `final_layout`，再反推 Get 顺序。不能先按 S1 重建一次、下一宏再按 S2 重建。

### 7.6 部分摘后继续挂的准入条件

保留车列 `R` 后允许 fresh Get `B`，必须同时满足：

1. 当前 `R+B` 折算辆不超过 20；
2. 从当前位置带 `R` 到新源线的路径可达；
3. `B` 是新源线可取前缀；
4. `B` 加入后至少消灭一个未来闭合，或关闭一个 gate family；
5. 新车列的尾端存在合法下一 Put；
6. session 已有完整退出计划，最终空挂；
7. 不要求“所有 session 曾触碰车辆总当量 <=20”，只检查每一步实际 `TrainCars`。

### 7.7 临编块的准入条件

任何非真实目标 Put 都必须记录：

```text
staging_owner
ordered_nos
why_staged
recover_after_step/family
recovery_source_end
exit_target
gate_lease_until
```

没有这些字段的临编候选不生成，而不是生成后靠评分惩罚。

## 8. 目标窗口只关闭一次

对调梁棚、洗罐站、油漆线等目标，建立 `TargetWindowIntent`：

```text
target
existing_layout
known_inbound_blocks
known_outbound_blocks
forced_position_groups
currently_accessible_blocks
prerequisite_blocks
final_layout
reopen_lower_bound
```

提交最终 Put 前满足：

- 当前可见且能在本 session 取得的 inbound block 已全部纳入；
- 未纳入来源有明确前提，且本次 Put 不会使未来重开成本增加；
- 出站 blocker 已真目标落位或恢复；
- 强制位置和容量完整；
- gate release step 已确定。

如果宽窗口候选当前多 2 钩，但可少一次未来 `Get target + Put target`，必须比较完整剩余代价，不能按本宏钩/解决辆数直接淘汰。

## 9. 运行时决策顺序

每个状态按以下顺序生成一个候选池：

1. **硬验证前置**：阶段 ownership、端别、容量、台位、牵引、protected 不变量。
2. **D0 直送**：所有 `direct_ready` 块。
3. **关键 family-close**：能释放最多 pending route/access 的闭合 session。
4. **D1 blocker digest**：清障车真目标落位或原线恢复，并立即产生直送/assembly。
5. **assembly contract session**：只落已释放、有 Stage3 owner 的线。
6. **D2 TargetWindowIntent**：多来源、部分摘挂、目标既有车联合闭合。
7. **storage/cun5 unit**：共享区段和容量 holdout 联合处理。
8. 无候选时输出缺失前提，不进入第二套策略。

“直送优先”不是固定绝对排序。如果一个直送会占住本轮关键 gate，或迫使强目标再次打开，它在硬边界阶段就被拒绝；剩下的直送才优先。

## 10. 候选比较

所有候选先通过同一物理终审，再比较：

```text
1. actionable/unserviceable residual count
2. 新增 gate lock 数和 lease 持续步数
3. target reopen lower bound
4. 无 owner temporary block 数
5. Stage3 assembly fragment/run 数
6. relaxed remaining Get/Put cost
7. 当前 session Get/Put 数
8. 路径长度和热点咽喉占用
9. deterministic candidate id
```

`relaxed remaining Get/Put cost` 应由一个忽略部分路径占线、但保留源线栈顺序、目标窗口、牵引和 staging 回取的块级 DP 计算。它不需要证明全局最优，但必须看见“当前少两钩、未来多开一次目标”的代价。

禁止把 `source_count` 或 `multi_get=true` 直接作为奖励项。多摘只有减少剩余闭合或关闭 gate 时才有价值。

## 11. 无 fallback 的停止诊断

没有合法候选时，输出最小缺失前提集合，例如：

```text
target_not_ready:调梁棚:missing_sources=存5线北,存3线
gate_not_released:洗油北:open_families=油漆线
assembly_exit_missing:机南:stage3_route_blocked_by=机走棚
store4_reserved_by_stage2:cars=...
protected_prefix_no_restore:source=调梁棚:nos=...
capacity_holdout_unassigned:target=存5线南:required_m=...
retained_get_route_blocked:from=抛丸线:to=存5线北:blockers=...
target_reopen_required:调梁棚:future_blocks=2
```

这些原因直接对应应补能力或应等待的业务前提。不能改用缓存、换 profile、重跑或从多个结果中择优。

## 12. 对当前代码的具体重构映射

### Stage1

1. 把 `mandatory_service_debt` 纳入 `stage1_debt()`，不再等主 assembly complete 后才运行 `try_service_finish_step()`。
2. 移除“服务只能 4 planlet/固定 Get/Put 预算”作为业务边界；搜索预算耗尽应明确诊断，不应改变阶段责任。
3. `ASSEMBLY_DEPOT` 仍可作为候选全集，但 `official_stage1_target` 必须附加动态 `released(line, projected_state)`。
4. 用 `AssemblyContract` 取代静态 assembly line rank。
5. 增加原子 `store4_marshal_session`，绝不允许去大库车留在 Stage1 最终存4状态。
6. service candidate 与 assembly candidate 进入同一确定性池，比较全流程剩余代价。

### Stage3

1. 在 Stage1 开始前输出 `required_access_order` 或可接受 block order 集合。
2. 消费并验证 assembly contract；若顺序与合同不一致，Stage3 fail-fast，不自行猜测另一种上游意图。

### Stage4

1. 用 `ClosedSessionPlanner` 统一 retained-consist、fan-out、同目标多源和 target-window rebuild。
2. planner 状态保留 ordered carry、每条线布局、restore obligations、gate leases 和 staging owner。
3. 牵引限制逐 operation 检查，不再以 session touched-car 总量代替。
4. 保持每个提交 session 最终 `TrainCars=[]`，不把任意 held 状态泄漏到下一宏。
5. dynamic target 一旦进入 assembly/target-window contract 即冻结，不能中途按即时负载单车改约。

## 13. 验收标准

策略落地后必须同时验证：

- 147 例逐 operation 的端别、路径、占线、牵引、长度、容量、顺序、台位和业务回放；
- Stage1 结束时 assembly contract、存4纯度和 gate release 全部通过；
- Stage2/Stage3 对合同消费无隐式重排；
- Stage4 旧 complete 零回退；
- `0309Z/0401W/0421Z/0428Z` 由通用 intent 完成或给出新的精确不可行证明；
- 调梁棚重复 target rebuild 明显下降；
- partial-Put + fresh-Get、cross-flow、multi-Put accumulation 不再接近零；
- 总钩数按同案例配对下降，不能只看多源宏数量增加；
- 所有 session 最终空挂；
- 没有 retry、fallback、策略 portfolio、结果择优、案例号或车号补丁。
