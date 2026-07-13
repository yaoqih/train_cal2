# Stage4 多摘多挂与全阶段连贯性深度诊断

## 结论

诊断基线的 Stage4 已经具备统一物理转移、目标位次、owner 栈、资源租约和闭合 session，
所以 140 个冻结案例能够全部闭合并通过 replay。但“可解”与“接近人工计划”之间仍有
明显距离。本文后续重构已把开放 carry episode 接入 complete-label 比较；同起点全量
重放从 2464 降到 2225 勾，平均从 17.600 降到 15.893，85 例改善且 0 例回退。以下
“当前”统计仍指重构前基线，便于保留问题证据；最新验收结果单独列在“重构实现结果”。

主要问题不是连续 `Get` 太少，而是取到的列没有在取车前形成全局可甩顺序。算法倾向
一次取较大的混合前缀，再把尾端小段分散到多个临时线，之后逐段取回。结果表现为：

1. 多取数量不弱，但多取后的有效合并弱；
2. 多甩次数不少，但每次保留 carry 时甩下的块偏小；
3. 目标位置被过早全序化，搜索只能为一个任意 rank 顺序做高成本排序；
4. target-window 内部是确定性构造，真正的全局搜索看不到多数中间编组选择；
5. 拓扑在单步上严格校验，但缺少“某门区在相关业务关闭前必须持续可用”的阶段状态；
6. `sealed_target` 只是“发生过目标兼容 Put”的历史标记，不等于业务窗口已经关闭。

因此重构没有增加案例特判、排序权重或搜索重试，而是把 Stage4 收敛为同一图中的
“阶段合同 DAG + 拓扑资源包络 + 有序块流 episode”求解。

## 审计口径

诊断脚本为 `scripts/analyze_stage4_coherence.py`，结果为
`artifacts/stage4_block_flow_final/coherence_diagnosis.json`。

输入包括：

- 140 个冻结 Stage4 的 `stage4_request/response/trace/summary`；
- 109 个可读取的人工 Response bundle；
- 每辆车的 Stage4 active/protected 身份和求解后实际终点；
- 每一勾 `MoveCars/TrainCars/PassbyPath`。

核心定义：

| 指标 | 定义 |
|---|---|
| 重复 Get 车次 | 同一案例内同一辆车第二次及以后被 Get 的次数 |
| 临时 Put 车次 | 车辆 Put 到与本方案最终线路不同的线路 |
| 临时循环相关勾 | 至少放下一辆临时车的 Put，或随后回收临时车的 Get |
| 纯临时循环勾 | 本勾 MoveCars 全部用于临时放置或临时回收 |
| 零净迁移 | 当前 carry 中的车被 Put 回本次 Get 的来源线，且不是最终落位 |
| owner 分裂 | 同一最终目标被多个非连续最终 Put 批次关闭 |
| 软目标再访问 | 任意目标兼容 Put 后，后续又从该线路 Get，即当前 `SearchCost.target_reopens` |
| 债务清零后再取 | 该目标 active 车辆连同位置约束曾全部满足，之后又从该线路 Get |
| 语义目标重开 | 已无 target、restore、source 和 gate 义务并 SEALED 后再次 Get |
| 严格多取 | 某 Put 前紧邻至少两个连续 Get |
| 部分甩车 | Put 后 `TrainCars` 非空，机车仍保留主列 |

“临时循环相关勾”不直接等于可删除勾数。一勾即使 MoveCars 全是临时车，也可能承担
暴露深层车辆的必要排序。这个指标用于定位结构负担，最终能否删除必须由新方案 replay
证明。

## 全量结果

### Stage4 自身

| 指标 | 结果 |
|---|---:|
| 案例 | 140 |
| 总勾数 | 2464 |
| 平均 / 中位 / P90 | 17.600 / 17 / 29 |
| 平均当前 lower-bound gap | 10.779 |
| 重复 Get 车次（全部车辆） | 993 |
| 重复 Get 车次（active） | 722 |
| 临时 Put 车次（全部车辆） | 905 |
| 临时 Put 车次（active） | 665 |
| 临时循环相关勾 | 839，占总勾数 34.05% |
| active 临时循环相关勾 | 656 |
| 纯临时循环勾 | 837，占总勾数 33.97% |
| owner 最终 Put 分裂增量 | 111 |
| 软目标再访问 | 90 |
| 债务清零后再取 | 20 |
| 语义目标重开（新 SEALED 证书） | 0 |
| `same_target_join` | 26 |
| `cross_flow_join` | 6 |
| `continue_owner_stack` | 3 |
| 标签预算耗尽案例 | 18 |

软目标再访问线路分布：

| 线路 | 重开次数 |
|---|---:|
| 存4线 | 42 |
| 油漆线 | 27 |
| 存2线 | 7 |
| 存3线 | 5 |
| 存1线 | 3 |
| 调梁棚 | 3 |
| 调梁线北 / 预修线 / 存5线南 | 各 1 |

这 90 次不能全部解释为策略反复。存4的 42 次和油漆的 27 次多数发生在目标窗口仍然
OPEN 时，只表示先放入一部分车辆后又需要取线。用原始 depot assignment、完整位置和
`Stage4Problem.unsatisfied_active()` 逐勾回放后，“全部 active 债务曾满足再 Get”为 20 次：
存2线 8 次、存3线 5 次、存1线 4 次、存4线 2 次、存5线南 1 次。它仍可能承担 protected
恢复或未来 source/gate 通行，不能称为 SEALED。按新完整证书回放，语义重开为 0。

### 与高勾数的关系

| 相关关系 | Pearson r |
|---|---:|
| 总勾数 vs 临时循环相关勾 | 0.8961 |
| 总勾数 vs 重复 Get 车次 | 0.8522 |
| 总勾数 vs stage 勾 | 0.8794 |
| 当前 gap vs 临时循环相关勾 | 0.9480 |
| 当前 gap vs stage 勾 | 0.9338 |

勾数至少 25 的 30 个案例，平均 29.233 勾、11.767 个临时循环相关勾、13.067
个重复 Get 车次。其余 110 个案例平均 14.427 勾、4.418 个临时循环相关勾、
5.464 个重复 Get 车次。高勾数和反复搬运不是偶然共现，而是当前 gap 的主要结构来源。

### 人工结构对照

人工 Response 是全流程，Stage4 是 Stage3 后残余问题，所以不能直接比较总勾数、重复
Get 或临时落位。可以直接比较的是“一个已形成 carry 如何继续取和继续甩”的结构。

| 严格结构指标 | Stage4 | 人工 Response |
|---|---:|---:|
| Put 前紧邻至少两个 Get | 20.40% | 19.76% |
| 平均 Get 批量 | 4.925 | 4.126 |
| 平均 Put 批量 | 4.000 | 4.018 |
| Put 后仍保留 carry | 52.50% | 61.18% |
| 保留 carry 时平均 Put 批量 | 2.158 | 3.258 |
| 单车 Put 占比 | 30.93% | 21.16% |

这组数据否定了“只要增加连续 Get 次数就会变好”。算法的平均 Get 批量反而更大，
问题出在取到混合前缀后只能小块剥离。人工计划更常保留有用主列，同时每次甩下更大的
同意图块。

### 人工目标语义兼容性

去除重复 case id 后，可将 102 个 Stage4 案例与人工终态一一匹配。1426 辆重叠 active
车中有 549 辆最终线路不同，占 38.50%。主要差异包括人工存5北而算法存5南、人工
存5北而算法调梁棚/预修、人工调梁棚而算法油漆/调北等。

进一步核对 `stage4_request.TargetLines` 后，549 辆全部落在人工终点不属于当前 Stage4
目标域的情形；1426 辆可比 active 车的 Stage4 目标域大小也全部为 1。也就是说，这组
差异反映人工计划与当前阶段目标语义不兼容，不能作为“联合改目标可以降勾”的证据。

对当前 140 案，目标线路是静态硬约束；需要联合优化的是目标**位置偏序与分批关闭顺序**。
若未来输入重新出现多目标车辆，可以在同一模型中增加目标域变量，但它不是当前高勾数
案例的主要根因。

## 高勾数案例逐勾诊断

### 0225W：有多取，但先把同 owner 拆散

结果：31 勾，lower-bound gap 20，14 个重复 Get 车次，14 个临时 Put 车次，17 个纯
临时循环相关勾；只评估 5 个全局标签就前沿耗尽。

关键链路：

1. 第 4 勾从油漆取 3 辆，其中 2 辆最终去洗罐站；第 5 勾先把这 2 辆放到存3。
2. 第 6 勾从机库取 2 辆洗罐车，第 7 勾又把其中 1 辆放到存1。
3. 第 8、9、10 勾分别从存3、洗罐站、存1取回，直到第 11 勾才一次 Put 洗罐站。
4. 第 20 勾从存5北取 9 辆混合车；第 21 勾把一辆抛丸车放回存5北，第 23 勾再取回。
5. 调梁 owner 又被拆到存4和调梁线北，第 28 至 30 勾逐线取回，第 31 勾关闭调梁棚。

人工同案开局是 `Get 洗罐站 -> Get 油漆线 -> Get 机库线 -> Put 洗罐站`，先形成共同
意图列再落位。两者目标分配和阶段起点不完全相同，不能直接相减勾数，但算法中“先拆
同 owner、再逐线取回”的结构是明确的。

反事实物理回放证明：第 20 勾只取前 8 辆，把第 9 辆抛丸车留在存5北；甩开调梁尾段
后再 Get 这辆车，后续所有动作仍合法，最终 active/protected 均满足。方案从 31 降到
30 勾，重复 Get 从 14 降到 13。这里应优化的是 pull depth，不是增加暂存线。

### 0309W：大混合前缀后，把主要 owner 分到多条临时线

结果：33 勾，22 个 active 重复 Get 车次，22 个 active 临时 Put 车次，17 个纯临时
循环相关勾；64 标签耗尽。

第 22 勾从存5北取 13 辆，其 owner word 为：

```text
调梁棚(2) | 调梁线北(1) | 预修线(2) | 调梁棚(8)
```

随后算法把 10 辆调梁车分别放到存5南、存3，再把调北车放到机走北，完成预修后才从
存3和存5南取回 10 辆调梁车。反事实验证表明，直接带 13 辆去调梁棚不合法：通过联6
需要 207.5m，超过 192m，且目标路径有占线阻塞。调梁 rank 顺序又是
`1,2 | 6,7 | 3,4,5 | 8,9,10`，至少需要两条有序链完成重排。因此多数 staging 是硬结构，
不能把 17 个循环相关勾都视为可删除。

当前方案仍多了一步：第 27 勾后机车上的 rank `1,2` 已是最终正确前缀，却先 Put 存3，
再与 `3..5` 一起 Get。保留 `1,2` 在 carry，直接依次 Get `3..5`、`6..10` 后 Put 调梁，
完整物理回放得到 32 勾方案，较当前少 1 勾，重复 Get 从 22 降到 20。

### 0311W：出现明确的零净迁移

结果：34 勾，14 个 active 重复 Get，14 个 active 临时 Put，14 个纯临时循环相关勾，
3 辆车零净迁移；64 标签耗尽。

第 12 勾从存5北取 12 辆后，第 13、15 勾把 3 辆最终去存3的车放回存5北。第 21 勾又
从存5北取出这 3 辆。它们的临时动作没有改变线路，只改变了本次 carry 中的位置关系。
人工计划会把这种动作视为排序决策，而不是普通 owner 暂存；当前模型没有“在源线保留
未选前缀”和“混合 assembly stack”的统一比较，因此只能先全取再放回。

反事实方案把第 12 勾的取车深度从 12 改成 10，末尾两辆存3车留在存5北，只把已经
取出的另一辆存3车放回；后续仍一次 Get 三辆。物理回放从 34 降到 33 勾，重复 Get 从
14 降到 12。该边在现有 `pull_prefix` 候选中理论上存在，但 64 标签与 6 次快速 source
attempt 没有把它扩展成最终 incumbent，属于搜索边界问题。

### 0128W：多甩存在，但 owner run 过碎

结果：35 勾，13 个重复 Get，13 个临时 Put，10 个纯临时循环相关勾，6 个 owner 最终
Put 分裂增量；64 标签耗尽。

第 27 勾从存5北取 7 辆，最终 owner 在预修与存2之间交替。第 28 至 33 勾产生 6 个
连续 Put，多个 Put 只有 1 辆。`sweep_source()` 当前以 `(-active_count, -runs, ...)`
排序，同等 active 数下反而偏向 owner run 更多的源线，然后按 carry 尾段逐 run fanout。
这个局部动作能完成目标，却没有比较“直接 6 次 fanout”与“较少临时排序后批量关闭”
的 episode 总成本。

在固定 Stage4 目标下，这一局部段实际上已经达到下界。owner word 是
`预修 | 存2 | 预修 | 存2 | 预修 | 存2`，一次 Get 后必须从尾端处理 6 个 owner run；
把某一 owner 临时归并至少增加一次临时 Put 和一次 Get，不能少于直接 6 次最终 Put。
所以这 7 勾不能靠 Stage4 多摘多挂继续压缩。要降低它，只能让前一阶段交接状态减少
存5北上的 owner 交替，而不是让 Stage4 更复杂地翻线。

### 0120W：source-window 强制清空 carry

结果：35 勾，gap 26，18 个重复 Get，14 个临时 Put，16 个纯临时循环相关勾，3 次
目标重开；只评估 6 个标签即前沿耗尽。

强制 rehook 在第 6 勾 Put 一辆油漆车，后续为重建位置再次 Get 油漆线；这属于 OPEN
paint window 的软再访问，不是语义关闭后重开。更大的浪费出现在第 17 勾后：机车只剩
一辆去存5南的车，当前第 18 勾把它暂存存1，第二轮取完存5北后第 27、28 勾再取回并
单独 Put。

保留这辆车作为 carry anchor，直接续 Get 存5北剩余 13 辆，分流结束时把两辆存5南车
一起 Put，完整物理回放从 35 降到 32 勾，重复 Get 从 18 降到 17。该例直接证明：全局
checkpoint 强制空 carry 排除了合法且更优的阶段连续方案。

### 0413Z：动作图缺边，不是搜索预算不足

结果：33 勾，只有 13 辆 active，却有 19 个重复 Get、18 个临时 Put、19 个纯临时循环
相关勾；只评估 6 个标签就前沿耗尽。

调梁、油漆、洗罐 owner 被分散到存2、存3、存5南，同时 protected blocker 在调北、
机走北等线清出后恢复。尝试把存5北末尾洗罐车留在线上、只取前 8 辆调梁车时，下一步
Put 存5南被物理拒绝，因为存5北仍占线。该例证明 source depth 不能只按 owner/run 优化，
还必须满足 `清空存5北 -> 才能使用存5南` 的拓扑资源前置关系。当前 6 标签前沿不足，
但不是所有临时动作都可通过保留源线尾车删除。

## 反事实回放结论

以下方案均从当前方案的真实中间状态分叉，逐步调用统一 `OperationTransitions`，每一步
经过线路、容量、整列长度、取放顺序和目标位置校验；终态 active pending、protected
damage、carry 均为空。

| 案例 | 当前 | 反事实 | 改动 | 数学含义 |
|---|---:|---:|---|---|
| 0225W | 31 | 30 | 存5北取 8 而不是 9 | source 尾车可作为未取免费栈 |
| 0309W | 33 | 32 | carry 保留 rank 1,2，续取两个 rank chain | carry 是零暂存成本的开放链 |
| 0311W | 34 | 33 | 存5北取 10 而不是 12 | pull depth 必须进入搜索状态 |
| 0120W | 35 | 32 | 存5南车跨 source-window 留在 carry | 空 carry checkpoint 不是安全分解边界 |
| 0128W 局部段 | 7 | 7 | 1 Get + 6 owner-run Put | 固定交接状态下已达到局部下界 |
| 0413Z 尝试 | 失败 | 失败 | 存5北留洗罐尾车 | 未清空存5北导致存5南不可用 |

四个可行反事实合计少 6 勾。如果只替换这四案，140 案总勾数理论上从 2464 降到 2458。
这不是新求解器结果，只是证明当前图至少漏掉这些合法路径；新算法仍必须自动生成并在
全量 replay 中选出它们。

## 重构实现结果

已实现 `OpenCarryEpisodeOptimizer`。它不识别案例编号或业务线路名称，而是把 incumbent
的每一勾视为一个线路访问槽。槽可以跳过，也可以在同一线路执行任意合法
`Get(prefix)`、`Put(carry suffix)` 或 `Weigh(tail)`；因此 source depth、跨来源保留 carry、
相邻同线动作合并都在一个 skeleton shortest-path 图中比较，不再由五套形状规则依次尝试。

同一图还包含严格成对事件边：取消临时 `Put -> Get` 租约、延迟 source suffix 到后续
同线 Get，以及“单调目标后缀提交”。最后一类只在车辆块是当前目标尚未完成车辆中的
最高连续 final-rank 区间、目标既有车辆已按最终位次排列、最终位置无冲突时成立；车辆
直接放入 spotting 目标的最终位置，后续恢复 Get 和最终大批 Put 中的该后缀同时删除。

每条候选都从 Stage4 初态重新调用 `OperationTransitions`；只有完整终态、物理 planlet、
独立 response replay 和 combined replay 全部合法且 `SearchCost` 严格下降才可成为
incumbent。每个 complete 标签只做 1-label 廉价筛选，主 frontier 结束后仅对最佳
incumbent 执行一次 8192-label 完整投影，避免投影抢占主搜索预算。

同时完成了以下替换：

- 删除失败驱动的 6/64/128 尝试和固定 4 successor 截断。普通 source-window 固定 16
  标签；终局债务全部进入显式栈，或初态为单来源且 owner 至少 7 段时，才使用外层预算；
- 全局优先级改为 `g + admissible lower bound`，再以 `g + ordered owner-run estimate`
  排序。后者只决定扩展顺序，绝不参与剪枝；
- admissible lower bound 保持为来源线数、目标线数和待过磅数之和，修复了“来源 owner
  word 可以拆分”导致旧 owner-run 下界高估的问题；
- target window 改为 `OPEN/SEALED` 证书，完整语义下 0 次 SEALED 重开；
- soft reaccess 90、债务清零后再取 20 继续作为诊断，不再混入 SEALED 定义。

对冻结 truth2/truth3 的 140 个同起点方案执行独立优化和重新验证：

| 指标 | 原始 | 重构后 |
|---|---:|---:|
| 总勾数 | 2464 | 2225 |
| 平均勾数 | 17.600 | 15.893 |
| 减少勾数 | - | 239 |
| 改善案例 | - | 85 |
| 回退案例 | - | 0 |
| planlet / replay 合法 | 140 | 140 |
| SEALED 重开 | 0 | 0 |

完整投影共产生 107 次严格减勾 contraction，只有 3 案耗尽 8192 标签。重点案例结果为：

| 案例 | 原始 | 重构后 | 结构 |
|---|---:|---:|---|
| 0120W | 35 | 25 | carry anchor + source/target 访问槽重排 |
| 0225W | 31 | 27 | source suffix 延迟 + 单调目标后缀 |
| 0309W | 33 | 27 | 正确 carry 持续保留 + 单调目标后缀 |
| 0311W | 34 | 31 | source suffix 延迟 |
| 0413Z | 33 | 26 | gate car 跨窗口保留 + 单调目标后缀 |
| 0128W | 35 | 34 | 固定交替末段仍为 7 勾，其他租约减少 1 勾 |
| 0127W | 26 | 25 | rank 10 直接提交调梁棚最终位置 |
| 0209W | 31 | 31 | 剩余暂存承担 rank 换序和位置重建 |

冻结审计不会改变 Stage3 起点；在当前 Stage3 产物上重新完整运行 `0120W`，全局标签先
选择可收缩性更好的 complete 方案。全量投影结果文件为
`artifacts/stage4_block_flow_final/episode_projection_audit_target_suffix.json`；真实重求解
使用 `scripts/audit_stage4_frozen_resolve.py`，不会把冻结响应作为 fallback。

另取 `0105W/0128W/0206Z/0209W/0212W/0213Z/0225W/0305Z/0309Z/0310W/0310Z/0311W`
这 12 个高难冻结起点，以 60 秒、128 外层标签真实重求解：336 降到 308 勾，12/12
complete、0 回退，独立 replay 和 combined replay 全部通过。重点结果包括
`0225W 31 -> 27`、`0305Z 33 -> 26`、`0311W 34 -> 30`；`0209W` 仍为 31。

## 代码层根因

### 1. pull depth 没有成为稳定的全局决策

`digest_line_body()` 虽生成 owner/rank boundary 对应的多个取车深度，但 source edge 找到
一个闭合结果后只保留很少的 limited-discrepancy 尝试。0225W 的 `8/9`、0311W 的
`10/12` 都是物理合法且更优的深度，却没有进入最终 incumbent。取多并不总是更强：
未取尾车相当于保留在源线上的免费栈，取满后再放回会平白增加一勾。

### 2. 柔性位置被转成了任意全序

`search.py:88-100` 把 inbound 按来源线、位置、车号排序，再计算唯一 `final_rank`。
没有强制位置的同类车本来只需要满足容量和相对约束，却被赋予了严格先后关系。
`same_target_join` 又要求 rank 完全连续，所以合法的人工作业块会因为任意 rank 不邻接
而不能合并。

### 3. 暂存模型只允许单 owner

`OwnedStack.prepend()` 拒绝不同 owner；两套 staging candidate 都要求已有 stack 的 owner
相同。这个不变量能防止恢复语义混乱，但也禁止人工计划常用的“保存一个有明确甩车
顺序的 mixed consist”。算法只能把 mixed carry 拆到多条线，每条线再单独回收。

### 4. target assembler 是确定性排序器

`ConsistAssembler.assemble()` 固定一个 `expected` 全序。carry 与 expected 前缀不一致时，
只取最后一个 owner segment 并调用 `place_tail()`；`PlanBuilder.stage()` 永远选择排序后
第一个 staging candidate。全局 `_target_edge()` 只返回这个唯一结果，没有 target episode
的 Pareto 分支。

### 5. 全局搜索只在空 carry checkpoint 分支

source edge 和 target edge 提交前都要求 carry 为空。这个边界保证闭合，但使全局搜索
看不到“保留主列，再去另一个源线续挂/甩车”的中间状态。session 内虽可连续 Get/Put，
但由确定性构造器决定，搜索无法比较其他 carry word。

### 6. cross-flow join 的定义过窄

`try_join_productive_source()` 只考虑 storage source，且该 source line 必须本身是当前
carry 某辆车的目标。它建模的是线路交换闭环，不是一般的“当前 carry 与另一个可暴露
来源形成更优 owner word”。140 案中只有 6 次 `cross_flow_join` 与这个边界一致。

### 7. 全局优先级先最小化未到位车辆数

`optimizer._priority()` 第一维是 `len(unsatisfied_active)`，第二维才是 `g+h`。在标签受限
时，会优先扩展快速完成部分目标但留下 staging/recovery 债务的状态，而不是预估总勾数
更低的状态。最终 complete 之间虽按 `SearchCost` 选最小勾数，但很多低勾数路径尚未被
扩展就到达标签边界。

### 8. lower bound 几乎看不到排序代价

当前 admissible lower bound 只有“不同 source line 数 + target line 数 + weigh 数”。
平均只有 6.821，而实际平均 17.600，平均 gap 10.779，最大 gap 27。`FlowModel` 虽统计
source inversion，但明确不用于 admissible bound，平均估计也只有 7.607。搜索无法提前
区分一个将产生 10 个临时循环的 owner word。

### 9. 合同图没有服务线之间的动态依赖

当前合同只有 `DEPOT_OUTBOUND_REHOOK -> 每个 TARGET_WINDOW`。抛丸、油漆、洗罐、
调梁以及机南、洗油北、调北等资源关系没有进入合同状态。拓扑闭包用于单步避让和 staging
排除，但不表达：

- 抛丸债务未关闭前，机南必须保留为未来通路；
- 洗罐/油漆债务未关闭前，洗油北不能成为长期 assembly line；
- target 一旦有完整关闭证书，后续不应再次 Get；
- dirty target 的 outbound、inbound 和 existing stayer 应属于一个窗口，而不是多个合同。

### 10. 代价能识别反复，但动作图无法避免

`SearchCost` 已把 hooks、repeated_gets、软 target line 再访问、route_cost 做词典序优化。这说明
问题不是漏加一个重复 Get 惩罚：勾数本身已经首先惩罚 Put/Get 循环。真正问题是低反复
方案未出现在 generator 的候选图里，或在有限标签下没有被 `unsatisfied` 优先级展开。

## 数学上的充分结构

### 1. 上一版五元组不充分

简单写成 \(s=(Y,C,A,Q,Z)\) 仍然不是 Markov 充分状态：

- 相同 yard/carry 下，机车停在不同端位，下一步可达线路不同；
- `A` 的车辆物理顺序已经存在于 `Y`，重复保存会产生双重真相；
- 相同物理停车状态可能分别承担 protected restore、target assembly 或 gate lease，未来
  必须履行的义务不同；
- rank 可行性取决于尚未提交的位置匹配，不是单个固定 rank；
- repeated handling 若仍是次级目标，需要最小历史自动机，否则未来增量代价不相同。

收紧后的状态为：

\[
s=(Y,\lambda,C,\Pi,\Gamma,\Omega,\Phi,\eta)
\]

| 分量 | 含义 |
|---|---|
| \(Y\) | 每条线的真实有序车辆序列、位置、称重/作业标志 |
| \(\lambda\) | 机车所在节点、作业端和方向 |
| \(C\) | 机后车辆的严格有序序列 |
| \(\Pi\) | `Y` 上车辆块的 assembly/restore 元数据及唯一恢复顺序，不重复保存车辆 |
| \(\Gamma\) | gate lease、必须清空的 source、整列长度资源义务 |
| \(\Omega\) | service/target window 的 `UNOPENED/OPEN/SEALED` 状态 |
| \(\Phi\) | 各目标剩余位置域及可扩展匹配 |
| \(\eta\) | handled cars 等次级代价所需的有限历史自动机 |

active/protected 集合、目标线路、车辆属性、线路图和容量属于静态问题，不应复制进每个
标签。当前 140 案所有 1842 辆 active 车的 `TargetLines` 都是单点，因此动态状态不需要
目标线路选择变量；只需要 \(\Phi\) 保存位置域。未来出现多目标输入时，才把目标域承诺
加入 \(\Phi\)。

充分性判据是：任意两段历史到达同一 \(s\) 后，可用动作集合、转移结果、目标判定和
所有未来增量代价完全相同。上面八个分量分别覆盖物理、恢复、业务和代价历史，因此满足
Bellman/Markov 条件；删掉任一动态分量，都能由本节前述反例构造两个未来不同的历史。

### 2. 统一微动作图与精确 episode 收缩

底层图只有三类边：

```text
Get(exposed prefix)
Put(carry suffix)
Weigh(carry tail)
```

episode 不是另一套策略，而是这个微动作图在两个边界签名之间的路径收缩。局部 oracle
必须返回关于

\[
(hooks,\ repeated\ handling,\ obligations,\ resource\ footprint,\ end\ signature)
\]

的完整 Pareto frontier。若只返回固定 4 条 source edge 或唯一 target edge，收缩会丢边，
数学上就不再等价。

不能规定每个 source-window 后 carry 为空。任何完整计划都可以在“carry 空且无瞬时资源
义务”的再生点切分；若两个再生点之间一直保留 carry，那整段就是一个 episode。0120W
的 3 勾反事实收益正是该条件的直接证明。

### 3. pull depth 与三类免费容器

对每条 source line，取车深度 \(d_\ell\) 必须是局部标签变量，而不是取满后的补救选择。
排序时有三类容器：

1. 尚未取出的 source suffix：零 Put、零 Get 的免费容器；
2. 当前 carry 上已经正确的前缀：零暂存成本的开放链；
3. 真实 staging line：需要 Put/Get，并受容量和 gate 约束。

0225W、0311W 使用第一类分别节省 1 勾；0309W 使用第二类节省 1 勾；0120W 把 carry
anchor 跨越下一次 source Get，节省 3 勾。当前模型主要依赖第三类，因此天然多搬。

### 4. rank-run 链覆盖，而不是任意 mixed stack

目标位置应表示为可行域和偏序。对一个 owner，把 carry/source 中最大连续 rank interval
记为顶点 \(v=[a,b]\)。若 \([a,b]\) 可以在同一物理栈上与 \([b+1,c]\) 拼接，则在 DAG
中连边。无容量约束时，最少 staging stack 数是该 DAG 的最小链覆盖，可由二分图最大
匹配得到；加入线路容量、Put/Get 成本和 gate footprint 后，变成小规模 min-cost flow。

0309W 的调梁 interval 为：

```text
[1,2] | gate | pre | [6,7] | [3,5] | [8,10]
```

其中 `[6,7] -> [8,10]` 和 `[1,2] -> [3,5]` 构成两条链，所以需要存3、存5南两条物理
链；把所有 8 辆一次放在一条线，取回顺序仍是 `6,7,3,4,5,8,9,10`，最终 Put 会违反
rank。真正多余的是把已经在 carry 上的 `[1,2]` 也暂存，而不是“两条 staging 都多余”。

`AssemblyStack` 因而可以保存多 segment，甚至多 owner，但每个 segment 必须有物理顺序、
rank domain、恢复方向和合同。仅仅取消单 owner 限制会生成无法恢复的混合列，并不优雅。

### 5. 拓扑长度包络必须进入标签

owner/rank DP 只能描述排序，不能决定路线。对每条关键资源 \(r\)，标签还需要当前整列
通过包络：

\[
B_r(Y,C,\lambda)=available\_length_r(Y)-required\_length(C,\lambda)
\]

当 \(B_r<0\) 时，进入该资源前至少要甩下足够的 carry suffix。这个 mandatory unload
是 lower bound 的一部分，而不是候选方案生成后才发现的失败。

- 0309W 带 13 辆直去调梁需要 207.5m，联6只允许 192m；
- 把调北车先最终 Put 到调梁线北，会挡住随后去调梁棚的路径；
- 0413Z 若不清空存5北，就不能使用存5南作为 rank chain。

这些关系进入 \(\Gamma\) 和合同 DAG：`clear 存5北 -> use 存5南`、`close 调梁棚 -> final
Put 调北 gate car`。这比固定“优先某线路”的规则更一般。

### 6. 目标窗口是状态机，Put 不等于 SEALED

每个目标使用：

```text
UNOPENED -> OPEN -> SEALED
```

普通增量 Put 只保持 OPEN。SEALED 证书要求：

1. 该目标所有 active 债务连同位置匹配均已满足；
2. 无 outbound blocker 和 restore obligation；
3. 无未关闭合同仍把该线当 source 或 gate；
4. 未来目标域不会再向该线分配车辆。

只有从 SEALED 目标 Get 才是语义策略反复，应成为非法边或显式 reopen 合同。当前 90 次
`target_reopens` 只是软线路再访问；仅按 active 债务判断有 20 次清零后再取。加入 restore、
source 和 gate 义务后，这些窗口仍是 OPEN，新 SEALED 语义重开为 0。模型禁止的是从
SEALED 再取，而不是存4/油漆 OPEN 窗口内必要的中间 Get。

### 7. resource-aware ordered-block-flow lower bound

定义放松图 \(R(s)\)：保留 source prefix、carry suffix、rank-chain、牵引批次、关键资源
长度阈值和必须恢复义务；放松具体路径距离及非关键线路冲突。其最短路为：

\[
h_{ROBF}(s)=\min_{\pi\in R(s)} Hooks(\pi)
\]

任何真实计划映射到一条 relaxed path，所以该值 admissible。它同时计算：

- 选哪个 pull depth；
- owner run 的必要最终 Put 数；
- rank interval 的最小链覆盖及 chain Put/Get；
- carry anchor/source suffix 可省掉的 staging；
- 穿越瓶颈前的 mandatory unload；
- gate restore 和 weigh 下界。

0128W 末段的 ROBF 下界就是 `1 Get + 6 owner-run Put = 7`，与当前一致；该证书能防止
求解器为一个已经局部最优的交替 word 继续无效搜索。

全局 frontier 应按：

\[
(g+h_{ROBF},\ g,\ open\ obligations,\ repeated\ handling,\ route\ cost)
\]

排序。`unsatisfied_count` 只能作后续 tie-break。标签 dominance 必须比较完整边界签名；
同物理状态下 obligation 或位置域不同的标签不能互相支配。

### 8. 单调合同 DAG

合同由当前债务和拓扑生成，而不是一层星形图：

```text
DEPOT_OUTBOUND_OPEN -> PAINT_WINDOW
SHOT_WINDOW -> RELEASE_MACHINE_SOUTH
WASH_WINDOW + PAINT_WINDOW -> RELEASE_WASH_OIL_NORTH
CLEAR_C5_NORTH -> USE_C5_SOUTH
CLOSE_ADJUST_SHED -> FINALIZE_ADJUST_NORTH_GATE
TARGET_WINDOW -> SEALED
```

所有合同边都在同一图中，没有失败后换策略。抛丸关闭前保留机南、洗罐/油漆关闭前
保留洗油北，都是 DAG 可达性约束的结果，而不是固定优先级。

### 9. Stage4 局部模型仍不足以修复差的交接状态

即使上述 Stage4 模型对固定起点求得最优，也无法免费消除前一阶段已经形成的 owner
交替。0128W 的存5北末段在 Stage4 开始时就是六个交替 run，局部 7 勾已达下界。

全流程目标应写成：

\[
\min_{\pi_{1:3},Y_3}\ H_{1:3}(\pi)+V_4(Y_3),
\qquad
V_4(Y)=\min_{\pi_4\mid Y}H_4(\pi_4)
\]

实际不必每次精确调用完整 Stage4，可把 \(h_{ROBF}(Y_3)\) 作为 Stage1/3 的 terminal
potential。这样前一阶段会主动减少存5北 owner/rank run、避免占用关键 gate，并把干净
目标直接关闭。必要时 Stage4 subproblem 还能返回基于 run pattern 的 Benders optimality
cut。没有这个跨阶段价值函数，“Stage4 能力再强”仍会为差交接状态支付不可避免勾数。

## 不足以解决问题的改法

以下措施最多改变少数案例，不是根治：

1. 只提高 `max_labels`：0413Z、0120W、0225W 在很少标签下已前沿耗尽；
2. 只提高 `cross_flow_join` 优先级：当前 join 的定义域本身过窄；
3. 给抛丸、洗罐、油漆增加固定排序：能改善开局，不能解决 owner word 排序；
4. 给重复 Get 增加更大惩罚：hooks 已经是第一目标，候选图无边时惩罚无效；
5. 允许任意 mixed staging：没有 recovery contract 会破坏可解性和错误诊断；
6. 发现失败后换策略或重试：会重新引入 fallback，无法定位动作图缺边；
7. 案例/车号特判：不能推广到新的线路阻塞结构。

## 重构验收边界

### 硬约束

- 140/140 actionable complete；
- 物理 replay、combined replay、pending gate、owner/rank domain、recovery obligation 全部
  0 违规；
- 逐案例勾数不高于当前 2464 基线中的对应值；
- 不出现 case id、车号特判；
- 不出现 fallback、retry、profile 或异常驱动策略切换；
- `SEALED` target 0 次重开；普通 OPEN 窗口允许按合同继续取放；
- 所有位置域收缩、rank-chain 拼接和 mixed assembly 都有可扩展匹配证书；
- episode oracle 在同一边界签名下返回完整非支配前沿，不能只保留第一个可行构造；
- `h_ROBF` 在可穷举小实例上不超过真实最优值，并对 0128W 末段给出 7 勾证书。

### 首轮行为门槛

反事实回放已经证明以下路径在现有物理模型中合法，因此它们是新动作图必须自动搜索到的
最低门槛，而不是调参后的期望值：

| 案例 | 当前 | 首轮上限 | 必须出现的能力 |
|---|---:|---:|---|
| 0225W | 31 | 30 | source suffix 作为免费栈 |
| 0309W | 33 | 29 | 保留正确 carry prefix，续接 rank chain |
| 0311W | 34 | 33 | pull depth 进入全局标签 |
| 0120W | 35 | 31 | carry anchor 跨 source window |
| 四案合计 | 133 | 123 | 不能由案例特判实现 |
| 140 案合计 | 2464 | 2225 | 逐案不回退 |

`0128W` 固定交接状态的末段仍应是 7 勾；`0413Z` 不得通过保留存5北尾车产生非法的
存5南动作。这两个负例和上面的正例同等重要：它们验证模型是在优化结构，而不是盲目
减少暂存。

### 结构评价

不再把“纯临时循环勾占比不高于 25%”“重复 Get 不高于某个经验值”设成硬目标，因为
0309W 已证明部分临时循环由 rank 链和联6长度共同强制。重构后按如下方式评价：

1. 主指标是每案超额勾数 `H(solution) - h_ROBF(start)` 及全量总和；
2. 每个 temporary Put/Get 标注为 `rank-chain`、`length-envelope`、`gate-restore` 或
   `uncertified`，只压缩最后一类；
3. `SEALED` target reopen 保持 0；债务清零后再取 20 和软线路再访问 90 作为观察指标；
4. 重复 Get、单车 Put、partial Put 批量作为二级诊断，不可凌驾于总勾数和物理可行性；
5. 位置域匹配、合同义务和资源包络必须进入 dominance signature，不能靠日志补判。

只有当 ROBF 下界实现并通过 admissibility 验证后，才设定平均 gap 的数值目标。当前
`10.779` 来自旧的 source/target 计数下界，不能与新下界直接比较。

## 实施顺序

1. 建立充分的 `BoundaryState` 和 dominance signature：物理 yard/carry、机车端位、位置域、
   assembly/restore 元数据、资源义务、窗口状态和最小代价历史一次定义清楚；
2. 把 source/target 两套闭合 session 合并成 open-carry episode 搜索，`pull_depth` 是标签，
   carry 不再被边界强制清空；
3. 实现 rank-domain interval 与最小链覆盖 oracle，使 source suffix、正确 carry prefix、
   staging line 在一个模型里比较；当前目标线路仍保持单点，不先引入无用的目标选择；
4. 把关键喉区长度、必须清空 source、gate lease 编译成资源包络和合同前置边，覆盖联6、
   存5北/存5南、调梁棚/调北等依赖；
5. 实现 `UNOPENED/OPEN/SEALED` 窗口状态机，SEALED 由完整债务和无未来资源义务共同证明；
6. 从同一 ordered-block-flow 模型提取 admissible `h_ROBF`，全局优先级改为 `g+h_ROBF`，
   `unsatisfied_count` 仅作后续 tie-break；
7. 先通过四个正反事实、两个负例和小实例穷举，再跑 140 案 replay 与逐案不回退门槛；
8. 最后把 `h_ROBF(Y3)` 接入 Stage1/3 终态评分，解决 0128W 这类 Stage4 起点已交替的问题。

每一步都替换旧的不充分抽象并删除对应旧路径，不保留兼容分支。任何失败都返回结构化的
不可行证书（位置匹配、容量、长度、gate 或合同环），不切换 fallback/profile 重试。
