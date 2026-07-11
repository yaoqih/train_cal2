# Stage4 多摘多挂能力深度诊断

## 1. 结论

当前算法的“多 Get”数量并不少，但真正接近人工的多摘多挂能力仍然很弱。

核心判断是：

> 当前 Stage4 会为重建目标线、搬开路径 blocker、回取临停块而连续 Get；它不会主动把两个独立来源组织成一个具有后续摘解顺序的车列，也不会在完成一次部分 Put 后保留有效车列并继续取得新来源。

最新全流程 `artifacts/fullflow_truth23_spotting_parallel_v1` 的证据：

| 数据集 | Stage1 双源同目标宏 | Stage4 闭合宏 | Stage4 表面多源宏 | Stage4 战略多源合编宏 |
|---|---:|---:|---:|---:|
| truth2，113 例 | 6 | 643 | 267 | 0 |
| truth3，34 例 | 5 | 123 | 53 | 0 |
| 合计，147 例 | 11 | 766 | 320 | 0 |

这里的“战略多源”要求至少两个独立来源的车辆在宏结束时没有恢复原线，而是被合成后续车列或完成共同目标。320 个 Stage4 表面多源宏全部属于：

- 拉出目标线既有车后重建目标窗口；
- 拉出路径 blocker 后恢复；
- 布局重建时把块分散临停，再从多个临停线回取；
- 强台位同线或跨线重排。

因此，用户感觉“多摘多挂依然很弱”是准确的。问题主要不在物理层，而在 Stage1/Stage4 的作业意图、候选语法、阶段边界和评分目标。

## 2. 数据口径与可信边界

本轮新增可复现分析器：

```text
scripts/analyze_multi_get_capability.py
```

主要输出：

```text
artifacts/multi_get_capability_diagnosis_latest/
artifacts/multi_get_capability_diagnosis_truth3/
artifacts/multi_get_capability_diagnosis/
```

分别对应最新 truth2 全流程、最新 truth3 全流程和此前 Stage4 单体基线。

### 2.1 人工样本

当前 `artifacts/manual_restored_interface/bundles` 与旧统计文档生成时已经不同：

| 指标 | 当前值 |
|---|---:|
| bundle 文件 | 109 |
| 成功恢复 bundle | 87 |
| 去除跨月完全重复作业单后的唯一成功 Response | 85 |
| 可与最新 algorithm combined 配对 | 83 |

`0130Z` 和 `0302W` 各有一份完全相同的跨月重复作业单，分析时只计一次。

### 2.2 carry session 定义

以 `TrainCars` 为准，把以下区间定义为一个持车会话：

```text
空挂 -> 第一次 Get -> 若干 Get/Put -> TrainCars 再次为空
```

另外单独使用 Stage4 trace 的 `accepted + operations` 恢复算法宏边界。两种边界不能混为一谈：布局重建宏可以在宏内多次放空后再回取临停块。

分类定义：

| 指标 | 定义 |
|---|---|
| 表面多源 | carry session 中从至少两条线 Get |
| 战略多源 | 至少两个独立来源的车最终没有恢复到各自原线 |
| 结构多源 | 多源 Get 仅用于目标重建、blocker 恢复或临停块回取 |
| 部分摘后续挂 | Put 后 `TrainCars` 非空，并在本 session 放空前继续 Get 新车 |
| 长锚车 session | 至少 10 勾，且至少一辆车几乎贯穿整个 session |

### 2.3 人工 Response 不是物理可执行真值

这是本轮必须补充的限制。

人工还原器恢复的是车辆身份和人工单上的摘挂顺序，`PassbyPath` 只使用简单图最短路。它不完整验证占线、操作端、牵引限制、折返长度和最终业务目标。独立使用当前 `replay_validator.py` 审计 85 份唯一成功人工 Response：

| 指标 | 值 |
|---|---:|
| `TrainCars` 摘挂状态不一致 | 0 |
| 原样通过操作级 schema + physical | 0/85 |
| physical violation | 3102 |
| state violation | 2139 |
| business violation | 2577 |
| 峰值牵引当量超过 20 | 4/85 |

所以人工样本能可靠证明“人工试图怎样连续摘挂、保留车列和组织阶段”，但不能证明每条恢复路径可在当前物理模型中原样执行。后续实现只能借鉴意图结构，每个候选仍必须由 `physical.validate_planlet` 完整证明。

人工最终状态也不是当前算法的严格目标答案。85 份人工计划没有一份让所有车都落在接口 `TargetLines` 中，目标线未满足辆数中位数为 23。因此人工计划和 algorithm combined 的总钩数不能直接作为最优性比赛。

## 3. 会话级差异

83 个相同案例上的配对统计：

| 指标 | 人工 Response | 最新 algorithm combined | 最新 Stage4 增量 |
|---|---:|---:|---:|
| carry session | 548 | 1571 | 573 |
| 一 Get 一 Put session 占比 | 30.1% | 60.6% | 39.4% |
| carry 口径战略多源 session | 266 | 213 | 35 |
| 战略多源占比 | 48.5% | 13.6% | 6.1% |
| 多 Put session | 314 | 402 | 168 |
| 部分 Put 后继续 Get 新车 | 203 | 56 | 0 |
| 10 勾以上 carry session | 46 | 13 | 0 |
| 超过 20 牵引当量 | 4 | 0 | 0 |

Stage4 carry 口径的 35 个“战略多源”仍不是真正的原始来源合编。它们全部位于 `closed_layout_rebuild_session` 的临停块回取子段；放到完整宏边界后，战略多源仍为 0。

配对案例中，algorithm combined 平均比人工多重复处理约 12.25 辆车。这个数受两者最终业务目标不同影响，不能全部归因于多摘多挂不足；但与大量临停、回取和目标线反复重建的 trace 方向一致。

### 3.1 人工的锚车能力

人工最显著的能力不是简单的 `Get A; Get B; Put T`，而是保留一辆或一小段车作为机后锚车：

```text
Get A
Get B
Put B 的尾段，保留 A
Get C
Put C 的尾段，继续保留 A
...
最后一次 Put 才放空
```

85 个唯一人工案例中：

- 82 例至少出现一次“部分 Put 后继续 Get 新车”；
- 49 个 session 达到 10 勾以上，覆盖 44 个案例；
- 最长 session 为 28 勾；
- 有 session 从 P1 一直保持到 P3，个别从 P1 贯穿到 P5。

人工 P5 的独立 session 反而很短：从 P5 才开始的 52 个 session 中，47 个是一 Get 一 Put，战略多源为 0。很多所谓“人工 P5 能力”其实是在 P4 或更早就形成车列，并把持车意图延续到尾项。当前分阶段算法在阶段出口全部放空，天然丢失了这种连续性。

## 4. 代码层根因

### 4.1 Stage4 候选从单一 source 出发

`generate_macros` 先遍历 `active_source_lines`，再对每一条 source 独立生成目标重建和 service sweep：

```text
scripts/stage4_simple/solve.py:667-693
```

没有“先选一个作业意图，再从全局收集多个来源前缀”的入口。搜索空间的基本单位仍是 source，而不是 consist intent。

### 4.2 service sweep 只有一个真实来源

`build_service_sweep` 的语法是：

```text
[Get route blockers...]
Get one source
Put tail group
Put tail group
...
empty
```

对应代码：

```text
scripts/stage4_simple/solve.py:1688-1840
```

额外 Get 只来自 `get_route_blocker_groups/put_route_blocker_groups`，并通过 `restore_line_by_no` 强制恢复。进入 `while carried` 后只有 Put，不会在部分 Put 后继续取得新业务来源。

### 4.3 target rebuild 不是多源合编

`target_rebuild_candidates` 只从当前 `source_line` 取最多 5 辆同目标前缀，再与目标线既有车一起重建：

```text
scripts/stage4_simple/solve.py:980-1030
```

其“两个来源”通常是：

```text
Get target existing cars
Get one external source group
Put rebuilt target
```

目标线既有车只是被拉出再放回，不是第二个业务来源。

### 4.4 layout rebuild 的来源集合被固定

`build_layout_rebuild_session` 中的初始 `origins` 只有：

```text
target_line: target_existing
source_line: source_group
```

之后增加的是路径 blocker 全线：

```text
scripts/stage4_simple/solve.py:1075-1094
```

所以它可以做复杂重排，却不能主动加入第二、第三条具有相同后续意图的 source front。50 个 truth2 layout rebuild 用了 574 勾，但战略合编仍为 0。

### 4.5 宏边界强制空挂

Stage4 状态只有 `cars + loco`，没有可跨宏的 held consist。物理层又要求每个 planlet 末尾无 carry：

```text
scripts/solver_vnext/physical.py:3281-3282
```

这个边界有利于重放和错误诊断，不应简单删除。正确方向是生成更完整、但仍然闭合的意图 session，而不是把任意持车状态泄漏到下一轮贪心搜索。

### 4.6 Stage1 的新 multi-get 边界过窄

Stage1 已有 `multi_get_candidates`，但只表达：

```text
Get source A
Get source B
Put one common target
```

并且要求：

- Stage1 主债务已 complete；
- 没有 pollution；
- 目标线已经 clean/ready；
- 恰好两个 source；
- 两个 source 的可取前缀全部同一 target；
- 最多保留 48 个候选。

对应代码：

```text
scripts/stage1_simple/solve.py:1434-1521
```

147 例只命中 11 次，说明它更像一个晚期双源直送优化，而不是人工计划中的通用多摘多挂能力。

### 4.7 当前评分低估延迟收益

Stage4 评分把 `step_count / resolved_count` 放在非常靠前的位置：

```text
scripts/stage4_simple/solve.py:2430-2490
```

一个 6 勾 session 即使能同时：

- 清掉两个来源；
- 少一次后续 Get 或 Put；
- 减少目标线 run 数；
- 形成下一目标可摘尾段；
- 避免未来重建；

也可能输给当前立刻完成一组车的 2 勾闭合宏。评分没有显式计算未来闭合次数、目标 run、下一尾段可摘性和预计节省勾数。

### 4.8 候选截断会放大单源偏置

`ranked_valid_macros` 在收到足够多直接进展候选后停止继续枚举：

```text
scripts/stage4_simple/solve.py:536-546
```

如果未来只是把多源 session 追加在 source 循环后面，它可能根本进不了统一候选池。多源候选必须按全局 intent 生成，并与单源候选在同一确定性排序中公平比较。

## 5. “表面多源”为什么没有降低勾数

最新 truth2 Stage4 的 2390 勾中：

| 宏类型 | 宏数 | 勾数 | 作用 |
|---|---:|---:|---|
| `closed_target_rebuild` | 195 | 668 | 拉出目标既有车并插入单一外部来源 |
| `closed_layout_rebuild_session` | 50 | 574 | 分块临停后全线重建 |
| 两者合计 | 245 | 1242 | 占 Stage4 勾数 52.0% |

最高成本的连续调梁棚重建链：

- 同一案例连续 6 个 target rebuild；
- 共 20 勾；
- 只完成 6 组 debt；
- 来源涉及存2、机走北等多线，但每次只插入一个来源组。

此外有 44 次“缓存落车后下一宏立即回取”。这不是缓存本身错误，而是当前没有一次性声明“为什么缓存、还要收哪些来源、最终怎样退出”的 session contract。

## 6. 最新不可解案例揭示的能力缺口

最新全流程中：

| 数据集 | Stage4 complete | partial | `no_valid_closed_macro` |
|---|---:|---:|---:|
| truth2 | 98/113 | 15 | 5 |
| truth3 | 23/34 | 11 | 7 |

12 个 `no_valid_closed_macro` 中，8 个具有完全一致的结构：

```text
source front: 已满足并应留在原线的 blocker
source deeper: 真正待送车辆
```

例如：

```text
卸轮线: [已满足卸轮车, 去油漆车]
修库外: [已满足库外车, 已满足库外车, 去油漆车]
```

当前候选把深层目标车送走后，把前部已满足车放到缓存线，最终触发 `protected_damage`。缺失的不是放宽保护，而是下面的闭合语法：

```text
Get source prefix including protected blockers
Put deep active tail to its target or retained consist
Put protected blockers back to original source in original legal order
```

如果原线恢复路径暂时不通，则需要把 protected blocker 纳入下一作业意图，并在同一 session 中给出最终合法处置；不能提交一个损坏保护状态的中间宏。

其余 4 例属于更复杂的串联拓扑、目标窗口和容量混合边界，包括：

- 调梁棚既有车与调梁线北互相占用，单源 sweep 发生 Get/Put route blocked 或 target order violation；
- 存2去存5南需要经过被占用的预修/存5串联系统；
- 存5北、存5南互为操作端，目标重建开始前就被占线阻断；
- 容量不可行车辆与仍可完成车辆混在同一来源前缀。

这些都不能靠多跑一次或换策略解决，需要一个能同时声明来源、尾段、gate 和退出布局的 session。

## 7. 拓扑边界必须比人工经验更严格

物理图中的关键串联系统：

```text
抛丸线 - 渡10 - 机南 - 机走棚 - 机走北 - 主场
油漆线 - 洗油北 - 机走棚
洗罐站 - 洗罐线北 - 洗油北 - 机走棚
调梁棚 - 调梁线北 - 渡4 - 主场
存5线南 - 存5线北 - 渡1 - 主场
预修线 - 存2线/渡7 - 主场
```

### 7.1 用户提出的释放规则需要这样精确化

| 经验规则 | 精确边界 |
|---|---|
| 抛丸处理完可用机南 | 所有仍需进出抛丸的债务完成，且不存在依赖 `机南-机走棚` 的后续 session，才允许持久占用机南；短时占用必须在同一 session 内清空 |
| 洗南、洗北完成可用洗油北 | 还必须确认油漆线无待进出债务，因为油漆同样以洗油北为唯一接近线 |
| 洗完后可用调梁线北 | 洗区与调梁线北不是同一串联 gate；调梁线北只有在调梁棚相关进出完成后才能释放 |
| 可以把车先放机走棚 | 机走棚同时连接机南、洗油北和机走北，未完成抛丸、油漆、洗罐家族时不能作为长时缓存 |

“目标线干净就直送”仍是最高优先级。只有目标线阻塞且在当前 session 内无法低成本清理时，才考虑预编组或安全缓存。

### 7.2 当前算法对敏感线的长时占用

最新 algorithm combined 的非目标落车：

| 线路 | 非目标车辆落车次数 | 中位回取间隔，操作数 |
|---|---:|---:|
| 机南 | 642 | 27 |
| 洗油北 | 392 | 22 |
| 机走棚 | 497 | 15 |

Stage4 自身基本不再把车长期放到机南/洗油北，但 Stage1 仍大量使用这些线路作为大库编组承接。于是 Stage4 收到的是已经被长期 gate 占用塑形过的状态。这个问题不能只在 Stage4 内修补，Stage1 的 assembly line 选择也要服从同一 gate lease。

Stage4 的主要临停转移到机走北、洗罐线北、调梁线北、存1/2/3。回取间隔通常 3 到 7 个操作，风险比 Stage1 的机南/洗油北低，但造成大量重建和重复 Get。

### 7.3 gate lease

每个多摘多挂 session 应显式携带 gate lease：

| 待处理目标族 | 必须保护的关键线 |
|---|---|
| 抛丸 | 渡10 接近方向、机南、机走棚 |
| 油漆 | 洗油北、机走棚 |
| 洗罐站/洗罐线北 | 洗罐线北、洗油北、机走棚 |
| 调梁棚 | 调梁线北、渡4 接近方向 |
| 存5线南 | 存5线北及其北端操作窗口 |
| 存4/存3串联 | 存4南及存3接近方向 |

租约在整个 session 的逐步物理验证中生效。只有最后一个依赖该 gate 的 Get/Put 完成后，才允许它成为后续缓存候选。

## 8. 应补齐的五类作业能力

### 8.1 同目标多源汇聚

适用：两条或三条 source front 都是同一目标，目标线干净且路线连续可达。

```text
Get source A group
Get source B group
[Get source C group]
Put common target once
```

最低收益要求：相比逐源闭合至少少一次 Put，或者避免一次目标线重建。

### 8.2 多目标尾段分放

适用：一个或多个来源前缀已经能按尾部目标顺序组成车列。

```text
Get source A mixed prefix
[Get source B compatible prefix]
Put tail target 1
Put tail target 2
...
```

候选必须证明每次 Put 都是当前机后尾段，不允许依靠抽象目标排序改变实际车辆顺序。

### 8.3 部分摘后继续挂

适用：完成一个尾段后，保留段能与下一 source front 形成有价值的新列。

```text
Get A
Put A.tail -> T1, retain A.head
Get B
Put combined tail -> T2
...
empty
```

允许条件：

- retained consist 有明确车辆顺序和目标段边界；
- 带 retained consist 到下一 source 的路线可达；
- 下一次 Get 后牵引当量不超过 20；
- retained 长度满足路径和折返长度限制；
- session 最后必须空挂；
- 每一步都通过现有 `validate_planlet`。

### 8.4 深层车提取并恢复 protected prefix

适用：已满足车挡住更深待送车。

处置优先级不是简单缓存，而是：

1. blocker 自身有可直接完成的真实目标，则顺带完成；
2. blocker 能并入当前或下一车列，则按 intent 合并；
3. blocker 已在合法目标，提取深层车后原线、原合法次序恢复；
4. 只有存在同一 session 内确定退出路径时，才允许短时临停。

这能直接覆盖当前 8 个 `protected_damage` 死点，而不削弱 protected guard。

### 8.5 多来源目标窗口一次重建

当前 layout rebuild 只有一个业务 source。应扩展为：

```text
target existing layout
+ source front A
+ source front B
+ necessary blockers
-> one final target layout
```

先基于最终目标窗口求完整 layout，再反推最少 staging chunks 和 Get 顺序。不能按来源逐个插入，避免连续 3 到 6 个 target rebuild。

## 9. 精确决策边界

### 9.1 先直送

满足以下条件必须优先生成 direct session：

- 目标线无异目标阻塞；
- 强台位窗口可直接放入；
- source 可取前缀可达；
- Put 路线不新增 gate lock；
- 不损坏已满足车辆。

多源候选可以参与排序，但不能为了“凑多摘”覆盖一个更少勾、无副作用的直接送达。

### 9.2 再考虑同目标合编

至少满足一项才有价值：

- 两个以上 source front 共用目标，合并后少一次以上 Put；
- 目标是强台位线，合并后能一次形成完整窗口；
- 单独直送会把目标线封住，合编后一次落位可以避免后续 rebuild；
- 当前 retained segment 与新来源组合后减少一个未来闭合 session。

### 9.3 最后考虑安全临编

目标线阻塞且不好清理时，才把组送往 assembly line。assembly line 必须同时满足：

- 不是未完成目标族的串联 gate；
- 容量足够；
- Put 路径可达；
- 后续 Get 路径在计划占线下仍可达；
- 有明确 owner、目标顺序和 exit session；
- 不把异目标车埋到不可取深度。

优先使用不承担穿越功能的端线资源。`机库线`容量只有 71.6m，通常约 5 辆，适合小段承接而不是无界主编组线。`机南/洗油北/机走棚`容量更大，但拓扑代价高，只能在相关 gate 已释放或同一闭合 session 内短时使用。

## 10. 推荐的数据结构

建议增加一个确定性的 `ConsistIntent`，不是增加多套求解策略：

```text
intent_id
purpose: direct | converge | fanout | prefix_restore | target_layout
source_segments: [(line, ordered_nos, disposition)]
retained_segments: [ordered_nos]
tail_drop_plan: [(target_line, ordered_nos)]
protected_restore_plan: [(source_line, ordered_nos)]
gate_leases: {line: release_step}
staging_contracts: [(line, owner, enter_step, exit_step)]
expected_debt_drop
expected_blocked_drop
expected_future_hook_saving
expected_run_reduction
```

所有字段都从当前车辆状态、拓扑和目标计算，不包含案例编号、车号特判或线路碎片补丁。

候选生成流程：

1. 从所有 active source 计算可取前缀及阻塞深度；
2. 按目标族建立 source-front 图；
3. 先生成 direct intent；
4. 对共享目标或可兼容尾段生成 2 到 3 源 intent；
5. 对 protected prefix 生成闭合恢复 intent；
6. 对强台位目标生成一次性多来源 layout intent；
7. 使用同一 `validate_planlet` 做逐步拓扑证明；
8. 所有合法候选进入同一个确定性候选池，只选一次。

不需要失败后重跑、备用策略、结果择优或任何运行时 fallback。

## 11. 评分应从“本宏完成几辆”升级为“本 session 消灭多少未来作业”

硬约束先于评分：

```text
physical valid
business valid
pull <= 20
tail-only Put
capacity valid
protected final state valid
gate lease valid
session ends empty
```

在硬约束内建议按以下词典序评分：

```text
1. 容量可行范围内的最终未满足数
2. blocked active 数
3. 目标族最高优先级债务
4. 预计未来闭合 session 数
5. 目标线 extra run 数
6. 下一尾段不可摘数量
7. protected 临时移动且恢复的车数
8. 临停车勾数和 gate 占用步数
9. 当前 session 实际勾数
10. 路径成本
```

其中“预计未来闭合 session 数”可以用确定性下界计算：

- 剩余独立 source front 数；
- 剩余 target tail group 数；
- 仍需重建的目标窗口数；
- 已知必须回取的 staging contract 数。

这样，一个多 2 勾但能少 4 次未来重建的 session 才能得到正确评价。

## 12. Stage1 与 Stage4 的新边界

不建议把所有人工长 session 原样塞进 Stage4。更稳定的阶段划分是：

### Stage1

- 目标干净的抛丸、油漆、洗罐、调棚、预修等直接服务；
- 同目标 2 到 3 源汇聚；
- 前场 protected prefix 提取与恢复；
- 只在 gate 已释放时形成安全 assembly block；
- 输出 parked consist contract，而不是把物理 held train 跨阶段泄漏。

### Stage3

- 保持大库出库到存4和大库入库的强闭合；
- 输出已经确定的库线布局和仍可利用的承接块。

### Stage4

- 消化 Stage1/Stage3 明确留下的 parked consist contract；
- 多来源目标窗口一次重建；
- 前缀 blocker 恢复；
- 真正的部分摘后续挂；
- 只处理无法在更早阶段证明安全的尾项。

实际 held consist 仍在每个 API 阶段内闭合。跨阶段保留的是“已停在线上的有序车列及其合同”，不是隐藏机后状态，因此错误仍可从 Response 完整重放。

## 13. 验证标准

### 13.1 结构测试

至少增加以下无案例特判的合成测试：

1. 目标干净时 direct 优先，不产生临停；
2. 两条 source 同目标，`Get A; Get B; Put T`；
3. 三条 source 在 20 当量内汇聚；
4. Put 一个尾段后保留头段，再 Get 新来源；
5. 已满足 blocker 挡住深层目标车，完成后 blocker 原线恢复；
6. blocker 可直接完成时并入当前车列，而不是机械恢复；
7. 抛丸未完成时禁止持久占用机南；
8. 抛丸完成后可使用机南，但每步路线仍需验证；
9. 洗罐完成但油漆未完成时，洗油北仍受保护；
10. 调梁棚未完成时保护调梁线北；
11. 强台位多来源一次布局；
12. 重车使 20 当量边界恰好通过和超过 1 当量拒绝；
13. 所有 session 末尾 `TrainCars=[]`。

### 13.2 全量验收

在 truth2 113 例和 truth3 34 例上同时验收：

- schema/physical/state 硬违反为 0；
- 容量可行且上游可用案例不再出现 `no_valid_closed_macro`；
- 8 类 protected-prefix 死点全部由通用 session 完成；
- Stage4 宏口径战略多源不再为 0；
- `partial Put -> fresh Get` 不只出现在 layout staging；
- 连续 target rebuild 链长度和总勾数下降；
- complete 不低于各自基线；
- 对旧 102 个 Stage4 单体容量可行 complete 案例无完成性回退。

人工配对统计只用于行为形态检查，不作为严格总钩数目标。算法应在更严格的最终业务约束下比较新旧版本的钩数、重复处理车辆数和目标 run 数。

## 14. 实施优先级

按收益和风险排序：

1. `prefix_extract_restore_session`：直接解决 8 个 protected blocker 死点，边界最清楚。
2. `multi_source_same_target_session`：把 Stage1 的双源能力推广到 Stage4，并扩展到最多 3 源。
3. `partial_drop_continue_get_session`：补齐人工计划最关键的 retained consist 语法。
4. 多来源强台位一次重建：削减调梁棚连续 rebuild 链。
5. gate lease 与 staging contract：统一 Stage1/Stage4 的机南、洗油北、机走棚占用边界。
6. session-level future-hook/run 评分：在候选能力齐全后再调整排序。

这套顺序不依赖 fallback。每一步都增加一种可证明、可单测、可重放的作业语法，并继续使用当前单候选池和一次选择机制。

## 15. 重构落地与验证结果

### 15.1 已补能力

本轮在 `scripts/stage4_simple/solve.py` 中落地了四类通用能力：

1. `closed_prefix_ordered_target_restore_session`：目标既有车先取，深层新车按深位先放，已满足源前缀原线恢复，目标浅位车最后回放；不再用三个临存块机械完成这一结构。
2. `closed_multi_source_same_target_session_target`：从目标最终位置反推 Get 块顺序，支持 2-3 条独立源线、目标既有车一次联合重建和 20 当量硬边界。
3. `closed_partial_drop_continue_get_session_target`：首源混合前缀先摘尾段，保留头段后继续 Get 第二源同目标车，再合并 Put，session 结束持车为空。
4. 单调 gate lease：候选可以临时取下并恢复初始门槛车，但不能在仍有相关服务债务时给 `机南/洗油北/机走棚/调梁线北/机北2` 新增持久占车。

这些能力全部在同一个确定性候选池中参与一次排序。没有失败重跑、策略组合、结果择优、案例号分支或车号分支。

### 15.2 8 个 protected-prefix 死点

原诊断中的 8 例现全部 complete：

| 数据集 | 案例 | 旧状态 / 钩数 | 新状态 / 钩数 |
|---|---|---:|---:|
| truth2 | `0116W` | partial / 22 | complete / 27 |
| truth2 | `0202Z` | partial / 6 | complete / 10 |
| truth2 | `0206Z` | partial / 20 | complete / 27 |
| truth2 | `0324W` | partial / 30 | complete / 35 |
| truth3 | `0407W` | partial / 2 | complete / 7 |
| truth3 | `0413W` | partial / 9 | complete / 14 |
| truth3 | `0416W` | partial / 4 | complete / 9 |
| truth3 | `0420Z` | partial / 18 | complete / 26 |

其中占用目标线的末段由原通用布局 10 步压到 5 步。修库外前缀车允许多个等价库外目标时，判断依据是“当前仍满足并恢复刚取出的原线”，不能要求动态 `target_by_no` 恰好等于原线。

### 15.3 truth2/truth3 全量

对 `fullflow_truth23_spotting_parallel_v1` 的同一 Stage3 输入配对验证：

| 指标 | truth2 | truth3 | 合计 |
|---|---:|---:|---:|
| 案例 | 113 | 34 | 147 |
| 旧 complete | 98 | 23 | 121 |
| 新 complete | 102 | 27 | 129 |
| 新增 complete | 4 | 4 | 8 |
| 旧 complete 回退 | 0 | 0 | 0 |
| 旧新均 complete 的配对减钩 | 99 | 21 | 120 |

121 个旧新均 complete 案例中，39 例减钩、77 例持平、5 例增钩，合计减少 120 钩。增钩案例中的 `0205Z` 多 2 钩，原因是旧方案在调梁棚债务未关闭时向 `调梁线北` 新增车辆；新 gate lease 要求先临存，调棚关闭后再落位，这是拓扑正确性的显式成本。

### 15.4 多摘多挂能力变化

`artifacts/multi_get_capability_refactor_validation` 的宏级分类结果：

- 同目标多源宏 45 个，覆盖 39 例；45 个全部属于 genuine strategic multi-source；
- 显式 partial-Put 后 fresh-Get 宏在 `0421W` 命中 1 次；
- ordered protected-prefix restore 命中 16 个宏；
- Stage4 全部 carry session 均不超过 20 当量。

这使 Stage4 genuine strategic multi-source 从原来的 0 提升到 45。人工 Stage4 近似阶段仍有更高比例的长 carry session，因此 retained-consist 语法仍有继续扩展空间，但“算法只有结构性多 Get、没有战略性多源”的原结论已被实质修复。

### 15.5 回放与剩余边界

对 140 个已提交 Stage4 response 和对应 140 个 combined response 分别调用独立回放器：共 280 次回放，`schema/physical/business/state` 硬违反为 0，警告为 0。另 7 例 Stage3 本身 partial，Stage4 未提交操作，不计为回放通过。

仍有 4 个容量之外的可行动 `no_valid_closed_macro`：truth2 `0309Z`，truth3 `0401W/0421Z/0428Z`。它们不是本轮 8 个 protected-prefix 结构，后续应分别研究容量残余与可行动车耦合、强台位多来源顺序和存5南深层多目标分解，不能再用缓存或重试掩盖。

## 16. 二次深度审核：为什么能力仍然很弱

### 16.1 修正后的结论

第 15 节证明的是“算法已经存在业务多源语法”，不是“能力已经接近人工”。二次审核后的准确判断是：

1. 同目标多源的存在性已经补齐，但多数只是两条源线各取少量车后一次落线。
2. 人工最关键的 retained-consist 语法，即部分摘车、保留锚车、继续取得新业务来源、再向多个目标分放，Stage4 基本没有掌握。
3. 目标窗口仍按来源逐次打开，尤其调梁棚反复重建；新增多源模板没有形成“收齐可见来源后只开一次目标”的全局意图。
4. 当前评分会压制一部分真正省钩的多源候选，但无条件偏好多源又会增加钩数，根因是缺少剩余目标窗口和未来重开次数的下界。
5. 这不是再加一个特例模板能解决的问题。需要把固定模板提升为一个仍然闭合、但能在 session 内搜索 `Get/Put/Get/Put` 组合的意图规划器。

平均钩数下降约 5% 与上述结论并不矛盾：当前重构主要提高了可解性、保护前缀恢复和简单同目标汇聚；它尚未覆盖人工计划降低闭合次数的核心机制。

### 16.2 先修正一个统计假象

旧分析器在一个宏包含多个独立 carry session 时，会把下面的结构误判成“部分摘后继续挂”：

```text
Get target
Put staging A, retain cars
Put staging B, TrainCars=[]
Get fresh source
Put ...
```

车列在 fresh Get 前已经放空，因此它不是 retained-consist。`scripts/analyze_multi_get_capability.py` 已改为在 `TrainCars=[]` 处终止关联，并增加回归测试。重新统计后：

- 51 个 `closed_layout_rebuild_session` 的 partial-Put + fresh-Get：`28 -> 0`；
- 真正显式命中的 `closed_partial_drop_continue_get_session_target`：仍只有 `0421W` 1 个；
- 另有 `0401W` 的 cross-line repack 形成 1 个连续部分摘挂会话；
- 所以 135 个有 Stage4 动作案例、867 个 carry session 中，真正的部分摘后继续挂只有 2 个，占 0.2%。

另一个容易误读的数字是 Stage4 carry 口径的 100 个“战略多源”。其中 51 个来自 layout 宏放空后从多条缓存线回取，2 个来自 cross repack，1 个来自 prefix layout；缓存线不是车辆进入该宏前的业务原始来源。宏级分类才是业务来源口径：45 个同目标多源宏，加 1 个 partial-drop cross-flow 宏。

### 16.3 连续持车语法与人工的差距

83 个可配对人工计划只可用于意图语法研究，不能当作物理真值。人工还原 Response 全部存在当前严格回放器不接受的物理或状态问题，因此下面的数字不能直接作为目标值；但它们足以证明人工计划反复使用了哪些动作语言。

| 连续持车指标 | 人工配对意图 | 当前 Stage4 配对 | 当前 Stage4 全部 |
|---|---:|---:|---:|
| carry session | 548 | 544 | 867 |
| 一 Get 一 Put | 165 / 30.1% | 220 / 40.4% | 354 / 40.8% |
| 部分 Put 后 fresh Get | 203 / 37.0% | 0 | 2 / 0.2% |
| 多目标 fan-out | 288 / 52.6% | 120 / 22.1% | 183 / 21.1% |
| 多来源且多目标 cross-flow | 186 / 33.9% | 0 | 1 / 0.1% |
| 10 钩以上连续持车 | 46 | 0 | 0 |
| 最长连续持车 session | 28 钩 | 8 钩 | 8 钩 |

人工与算法的起始状态、最终目标和物理可信度不同，不能用上表计算“落后百分比”。真正可靠的结论只有结构性的：当前 Stage4 几乎只能做 source convergence，基本不会在保留车列时完成 cross-flow。

临编回取也揭示同一问题。Stage4 在所有缓存线上都没有出现“由至少两次 Put 累积、随后一次 Get 回取”的事件；人工意图仅在机走棚就有 48 次。这说明当前算法会使用缓存清障，却不会把缓存当作有 owner、有顺序、有退出动作的编组资源。

### 16.4 45 个同目标多源宏的实际强度

45 个 `closed_multi_source_same_target_session_target` 覆盖 39 例、共 174 钩，但规模分布很窄：

- 23/45 个宏只真正送达 2 辆业务车；
- 42/45 个宏只有 2 条业务原始来源，只有 3 个达到 3 条来源；
- 每个宏只有 1 条 Put 目标线，没有 fan-out；
- 目标集中于油漆线 19 个、调梁棚 17 个，其余所有线路合计 9 个；
- 22 个宏是空目标直接汇聚，另外 23 个还带目标既有车重放。

所以“45 个”主要证明两源同目标模板有效，不代表算法已经具备人工计划式长锚车编组。尤其 23 个宏只合并两辆业务车，能够节省的上限通常只有一次目标落线或一次闭合。

### 16.5 固定模板为什么很难命中 retained-consist

`build_partial_drop_continue_get_session` 同时要求：

1. 第一来源北端开始全部是 active、unsatisfied 车辆，保护车或已满足 blocker 一出现就停止；
2. 第一来源最多拆成 4 个目标 run；
3. 每个 run 的目标必须互不重复；
4. 第一来源涉及的所有目标线必须为空且 `target_ready`；
5. 第二来源只能有一条，且只能补第一来源某一个 join target；
6. 目标线上不能有既有车，不能把 target rebuild 合并进该 session；
7. 所有触碰车辆总当量不超过 20，即使前半段已经摘下、后续瞬时挂车辆远低于 20；
8. 固定顺序只能是“第一源全挂、摘后缀、挂第二源、落 join target、再落剩余前缀”。

这些条件在单测中容易构造，在真实脏目标、保护前缀和调梁棚强台位场景中很少同时成立，因此全量只命中 1 次。问题不是候选排序没有选中大量 partial session，而是生成器绝大多数时候根本表达不出来。

同目标多源模板也只读取各 source front 的同目标前缀，最多 3 条外部源线，并要求所有车先同时挂在机后再一次 Put。它不能先摘一段释放牵引量，再继续收第四个来源；也不能把同一 session 中的另一目标尾段顺手处理。

### 16.6 layout rebuild 复杂，但不是人工式多来源编组

`build_layout_rebuild_session` 的业务来源固定为当前 `source_line`，其余 origins 只能是目标既有车和路径 blocker。它可以产生很多 Get/Put，却不能主动把第二、第三条同业务来源线加入最终目标窗口。

此外还有四个保守边界：

- 所有参与车辆总当量一次性限制为 20，而不是只约束每一步的瞬时挂车；
- 最多 7 个 chunk；
- 每个 staging chunk 必须使用不同线路，不能在释放后复用同一临停线；
- 找到第一份物理可行 assignment 就返回，不比较同一最终布局下的最少临停和最少钩实现。

结果是 51 个 layout 宏消耗 591 钩，却没有一个宏形成两个业务原始来源的战略汇聚。它解决的是可解性和强台位重排，不是多摘多挂效率。

### 16.7 最直接的症状：同一目标被反复打开

`target_rebuild` 159 个、545 钩，`layout_rebuild` 51 个、591 钩；两类合计 210 个宏、1136 钩，占 Stage4 2680 钩的 42.4%。进一步按 `(case, target)` 聚合：

- 49 个 case-target 组合至少重建两次，覆盖 47 个案例；
- 第一次之后还有 75 次额外重建，消耗 326 钩；
- 49 组中 46 组是调梁棚，3 组是洗罐站；
- `0324W` 的调梁棚连续重建 6 次、21 钩；
- `0203W` 重建 5 次、24 钩；
- `0304W` 重建 5 次、17 钩。

326 钩不能全部视为可节省量，因为有些来源当时尚不可达、目标窗口或 gate 尚未释放。但“几乎全部集中在调梁棚”说明根因不是一般搬运成本，而是缺少调梁棚最终窗口的联合规划：算法每看见一个可插入来源就重建一次，人工则倾向先在存2/存3/存5或机走区域形成可摘段，再关闭调北 gate、一次完成较大窗口。

### 16.8 评分正在压制一部分已生成能力

`score_candidate` 的 progress key 先比较：

```text
当前宏钩数 / 当前最高优先级已解决辆数
当前宏钩数 / 当前总解决辆数
```

然后才比较执行后的完整债务向量。它没有计算目标线未来重开、既有车再次回放、剩余 source front 数和缓存回取数。

已提交 trace 每步只保留前 7 个备选，因此下面只是下界：

- 54 个已选宏的前 7 个备选中出现过同目标多源候选；
- 其中 18 个决策点、17 个案例存在物理验证和所有 guard 均通过、且执行后主债务向量更小的多源备选；
- 当前排序仍因本宏多 1-3 钩而选择较窄方案。

对 6 个代表决策强制选择该多源候选一次，随后仍使用完全相同的当前求解器继续求解：

| 案例 | 原 Stage4 | 反事实 Stage4 | 差值 | 结果 |
|---|---:|---:|---:|---|
| `0104W` | 37 | 35 | -2 | actionable complete，Stage4 replay 0 错误 |
| `0106Z` | 32 | 31 | -1 | actionable complete，Stage4 replay 0 错误 |
| `0304W` | 35 | 34 | -1 | actionable complete，Stage4 replay 0 错误 |
| `0413W` | 14 | 12 | -2 | actionable complete，Stage4 replay 0 错误 |
| `0226W` | 9 | 7 | -2 | actionable complete，Stage4 replay 0 错误 |
| `0224W` | 21 | 22 | +1 | actionable complete，Stage4 replay 0 错误 |

前 5 例证明当前评分确实错过省钩方案；`0224W` 同时证明不能把“更宽多源”直接设为更高优先级。正确评分必须估计候选之后的剩余闭合下界，而不是奖励来源数。

### 16.9 阶段边界也在削弱 Stage4

人工从 P5 才开始的 52 个 session 中，47 个是一 Get 一 Put。人工复杂多摘多挂通常在 P1-P4 已经形成车列，并跨阶段继续持有；它不是在“尾项 Stage4”才突然开始。

当前每一阶段出口和每个 Stage4 宏都强制空挂，这个回放边界应保留，但阶段应传递显式编组合同，而不是只传车辆静态位置：

```text
assembly_line
ordered business blocks
block targets and target windows
owner stage
required gate leases
next closed exit session
```

Stage1 可以在一个闭合 session 内把已知去向块排到存2/存3/存5或合适编组线，Stage4 再按合同一次回取；不能把无 owner 的车随意留在机南、洗油北、机走棚或调梁线北。这样既不泄漏 held consist，也能保留人工“前段塑形、尾段短送”的能力。

### 16.10 剩余 partial 对缺失语法的提示

| 案例 | 最终可行动残债 | 暴露的能力缺口 |
|---|---|---|
| `0309Z` | 2 辆去调梁线北，一辆仍埋在调梁棚、一辆已放存3 | 需要联合声明“抽调棚深层车、恢复调棚窗口、最后关闭调北”的双目标 session |
| `0401W` | 3 辆去机走棚、1 辆去调梁线北，另有洗罐站容量残余 | active 债务与容量 holdout 被分开处理，缺少容量残余参与的多目标退出布局 |
| `0421Z` | 去存5南和机走棚车辆已散落存1/存2/调北/机库 | 缓存只有可回取 guard，没有 owner、积累顺序和一次退出合同 |
| `0428Z` | 存5北连续 9 辆均去存5南，其中 1 辆为容量 holdout，算法 0 钩 | 存5共享区段、容量留置和整段南送没有在一个 session 中联合求解 |

这些案例不能用重试、换排序或临时 fallback 解决。它们要求候选本身同时包含业务块、保护块、容量留置块、目标窗口、gate lease 和闭合退出。

## 17. 推荐的重构边界

本节说明能力架构；逐线路 gate、D0-D3 阻塞边界、两批 assembly、存4原子预编组和具体
`Get/Put/Get/Put` session 规则见
[Stage1-Stage4 精确边界与闭合调车策略](./Stage1-Stage4精确边界与闭合调车策略.md)。

### 17.1 不再继续堆固定模板

保留 direct、prefix restore、spotting repack 等边界清楚的专用构造器，但把多摘多挂统一为一个 `ClosedSessionPlanner`。搜索只发生在单个闭合 session 内，全局状态仍然不允许持车泄漏。

规划状态至少包含：

```text
loco line
ordered carried consist
current physical line layouts
remaining business blocks
protected blocks and restore obligations
target final windows
staging reservations with owner
gate leases and release step
```

合法扩展只有三类：从可操作端 Get 一个有意图的连续块、从机后尾端 Put 一个目标/恢复/临编块、执行必要称重。每一步继续调用现有物理验证器，不另造宽松拓扑模型。

### 17.2 牵引约束改为逐步约束

20 当量只约束当前 `TrainCars`，不应约束整个 session 曾经触碰过的所有车辆。允许：

```text
Get 16
Put tail 8
Get fresh 6
Put target A
Put target B
```

只要每一步瞬时当量合法、Put 始终从尾端发生、最终空挂，这正是人工减少闭合次数的核心语法。

### 17.3 从目标最终窗口反推来源，而不是逐来源插入

对调梁棚、洗罐站等强布局目标，先建立当前可见全部 inbound block 和目标既有车的最终窗口，再反推：

1. 哪些来源现在可取；
2. 哪些 blocker 必须取下并恢复；
3. 哪些来源可以在同一 session 内通过部分摘挂加入；
4. 最少需要几个 staging block；
5. 哪一步后 gate 才可释放。

同一目标存在尚可在本 session 内取得的来源时，不应提交一个会迫使目标再次重建的较窄宏，除非较宽方案的剩余下界更差。

### 17.4 用剩余作业下界评分

建议候选排序保持安全硬约束在外层，效率部分比较：

```text
current_session_hooks
+ lower_bound(remaining source-front Gets)
+ lower_bound(remaining target closes/reopens)
+ lower_bound(remaining fan-out tail Puts)
+ mandatory blocker restore hooks
+ mandatory staging recovery hooks
+ mandatory weigh hooks
```

下界不需要精确求最优，但必须对“本次少 2 钩、未来多开一次目标”计入成本。若两个候选下界相同，再比较 gate 占用步数、临停车事件、实际 session 钩数和确定性 ID。

### 17.5 稳定 target reservation

`refresh_active_targets` 当前会根据即时负载重选 flexible target。车辆一旦进入 assembly contract 或 target-window intent，应冻结该合同内的目标；只有合同尚未执行且重新规划能严格改善可行性时才能整体改约。否则算法会像人工编组过程中临时改变去向，破坏已形成的块顺序。

### 17.6 实施顺序与验收

1. 先加入 target-reopen 下界和反事实回归集，修正“已有宽候选但评分选窄”的问题。
2. 再实现逐步牵引约束下的通用 retained-consist planner，覆盖空目标和已有目标窗口。
3. 把调梁棚多来源重建迁入 target-window planner，目标是显著降低 75 次额外重建，而不是追求多源宏数量。
4. 为 staging 增加 owner/ordered-block/exit contract，消除缓存落车后逐批回取。
5. 最后把 assembly contract 接到 Stage1/Stage3/Stage4 边界，保持每个 API response 和每个提交 session 均空挂闭合。

全量验收必须同时满足：

- 147 例逐操作拓扑、占线、顺序、容量、业务和状态独立回放；
- 旧 complete 案例零完成性回退；
- 4 个非容量残余案例由通用 session 解释或完成；
- 同目标重复重建次数、重复回放车辆数和总钩数下降；
- partial-Put + fresh-Get、cross-flow 和多次 Put 累积后一次回取不再接近零；
- 所有 session 最终 `TrainCars=[]`；
- 仍只有一个确定性候选池，不增加 retry、fallback、策略 portfolio、结果择优、案例号或车号补丁。
