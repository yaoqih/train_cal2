# 第四阶段：闭合宏优先与受限 held 求解方案

## 1. 批判性修订结论

上一版方案的总体方向成立：栈序不变量、目标弱序、分层势函数、统一物理校验和 replay 验收都应保留。

但审计指出的核心问题也成立：如果把 `held` 作为全局一等状态，并允许跨任意搜索决策点长期保留，会同时引发：

1. 状态空间组合爆炸。
2. MPC 只提交第一钩时的语义冲突。
3. 下界不可加，A* 最优性证明困难。
4. seen-state 去重命中率急剧下降。

因此第四阶段第一版应做减法：

```text
全局搜索状态默认保持 held 为空；
一取多放只作为闭合宏动作存在；
held 只在宏动作内部作为临时态参与逐钩校验；
只有实测证明闭合宏不够时，才引入受限 held。
```

这不是否定 held 建模，而是把 held 从“全局状态变量”降级为“宏动作内部执行态”。这样既保留连续挂摘的表达能力，又把全局搜索恢复成可去重、可控、可解释的闭合状态图。

## 2. 阶段定位

第四阶段承接第三阶段终态，完成剩余非大库车辆归位。它不重新求解大库入库、出库和台位分配，只处理前场剩余债务和少量阶段遗留债务。

第三阶段后，已知第四阶段主矛盾是：

```text
存5北/调棚/存2/存3 长栈释放
+ 调梁棚/预修/油漆/洗南/抛丸等目标线接收
+ 北端栈序和路径阻塞
```

统计事实：

| 指标 | 数值 |
|---|---:|
| active debt 车辆 | 2358 |
| 北端可直接取车辆 | 429 |
| 被前车压住车辆 | 1929 |
| 被压比例 | 81.8% |
| 前方压车数 p50 / p75 / p90 / max | 3 / 8 / 13 / 24 |

主要来源：

| 来源线 | 债务车 |
|---|---:|
| 存5线北 | 1164 |
| 调梁棚 | 250 |
| 存3线 | 201 |
| 存2线 | 186 |
| 存5线南 | 167 |

主要目标：

| 目标线 | 债务车 |
|---|---:|
| 调梁棚 | 746 |
| 预修线 | 537 |
| 油漆线 | 186 |
| 存5线南 | 157 |
| 洗罐站 | 146 |
| 抛丸线 | 111 |

所以第四阶段不能只做简单直送；必须同时具备清障、重排、缓存和路径解阻能力。

## 3. 动作口径与计费前置条件

第四阶段实现前必须先统一“一钩”的计费口径。

本方案内部采用 API 操作行计数：

```text
Get  = 1 个 operation
Put  = 1 个 operation
Weigh = 1 个 operation
```

即：

```text
Get A+B+C
Put C
Put B
Put A
```

在内部代价中计为 4 个 operation。

如果业务最终确认“一取多放”在现场勾数口径中不是逐行计数，则必须增加一个转换层：

```python
business_hook_count = hook_count_converter(operations)
```

搜索内部仍按 operation 校验物理合法性，但 portfolio 选择键和最终评价必须使用统一后的业务勾数。这个口径未确认前，不应声称“总勾数最少”。

## 4. 全局状态：闭合态优先

### 4.1 全局搜索状态

全局状态只记录闭合态，即调车机后方无车：

```python
ClosedState = (
    lines,       # 相关线路北端栈序
    loco,        # 调车机位置集合
    weighed,     # 已完成称重的车辆集合
    protected,   # 不应破坏的已满足车辆集合
)
```

全局搜索签名不包含 `held`。这保证：

1. 状态去重有效。
2. 下界可定义。
3. MPC 或滚动规划的提交边界清晰。
4. 任何提交后的状态都可安全重规划。

### 4.2 宏内部临时状态

宏动作内部允许出现 `held`：

```python
MacroState = (
    lines,
    held,
    loco,
    weighed,
)
```

但 `MacroState` 只存在于候选校验和局部搜索中。一个宏动作必须从 `held = ()` 开始，并以 `held = ()` 结束，才能提交到全局搜索。

合法宏示例：

```text
ClosedState S
  Get 存5线北 A+B+C
  Put 调梁棚 C
  Put 预修线 B
  Put 存3线 A
ClosedState S'
```

全局搜索只看到：

```text
S --service_sweep(4 operations)--> S'
```

宏内部每一步仍逐条做物理校验，并逐条输出 API 操作。

### 4.3 受限 held 例外

只有当实测发现闭合宏无法解决某类 case 时，才开放受限 held。受限 held 必须满足：

1. 只在固定深度的局部搜索窗口内存在。
2. 必须在 `K` 钩内清空。
3. 不进入长期全局 seen-state。
4. 状态签名使用规范化摘要：

```text
held_signature = (
    target_sequence,
    force_position_flags,
    weigh_pending_flags,
    pull_equivalent,
)
```

默认第一版不启用长期 held。

## 5. 线路分层与流程控制

目标线层级：

```python
TIER_1 = {"抛丸线", "洗罐站", "油漆线", "调梁棚", "机库线", "预修线"}
TIER_2 = {"洗罐线北", "调梁线北", "机走棚"}
TIER_3 = {"机走北", "存5线南", "其余线路"}
```

层级是软门控，不是硬禁止。

候选排序时优先降低：

```text
(
    tier1_debt,
    tier1_blocked,
    tier2_debt,
    tier2_blocked,
    tier3_debt,
    tier3_blocked,
)
```

但跨层动作允许存在，前提是它属于：

1. 清障。
2. 路径解阻。
3. 顺路归位。
4. 打破 ready 循环。

## 6. 自阻塞目标线专项

### 6.1 问题

`target_ready(T)` 不能作为所有 Put 的前置硬条件。对于 `调梁棚 -> 调梁棚`、`预修线 -> 预修线` 这类自阻塞目标线，会出现：

```text
T not ready，因为 T 上有要离开的车或要重排的车；
外部车不能送入 T；
要让 T ready 又必须先动 T 内部车；
T 内部车可能也需要临时缓存。
```

这会形成循环等待。

### 6.2 自阻塞线识别

目标线 `T` 满足任一条件时，进入自阻塞处理：

1. `T` 上有目标仍为 `T` 但当前未满足的 active 车。
2. `T` 上有必须离开 `T` 的 active 车，且外部还有目标为 `T` 的 inbound 债务。
3. `T` 上 protected satisfied 车压住 active 债务车。
4. `T` 是强制位置线，当前线内顺序不满足可达台位。

### 6.3 原地重排宏

自阻塞目标线使用独立宏：

```text
self_restack(T)
```

流程：

1. 从 `T` 北端取出冲突前缀。
2. 把必须离开的车送往目标或安全缓存。
3. 把仍属于 `T` 的车按弱序重建回 `T`。
4. 保证宏结束时 `held` 清空。

`self_restack(T)` 的合法性不依赖 `target_ready(T)`，否则会自锁。它只依赖硬物理校验、容量、front-accessible 和 protected damage。

第一版重点实现：

```text
self_restack(调梁棚)
self_restack(预修线)
self_restack(洗罐站)
self_restack(油漆线)
self_restack(抛丸线)
```

## 7. 目标线 ready 与 front-accessible

### 7.1 ready 定义

普通目标线 `T` ready 当且仅当：

1. `T` 上不存在必须离开 `T` 的 active 车。
2. `T` 上不存在未满足且目标为 `T` 的自阻塞车。
3. `T` 上 protected satisfied 车不会因新 Put 变成未满足。
4. 新 Put 后目标线容量足够。
5. 新 Put 后目标线北端可访问关系不破坏后续债务。

### 7.2 not ready 处理

普通送入宏默认不向 not-ready 目标 Put。

例外只有三类：

1. `self_restack(T)` 内部重建。
2. 打破强连通环时的缓存破环动作。
3. 被 beam/MPC 证明能在宏内闭合并减少主进度债务的动作。

### 7.3 front-accessible 守卫

任何 Put 后必须检查：

```text
新放车是否仍在目标线可处理前缀中；
是否把更高优先级债务压到南侧；
是否破坏强制台位；
是否破坏 protected satisfied 车；
是否制造不可清理的缓存污染。
```

这个守卫是硬拒绝，不进入势函数讨价还价。

## 8. 称重债务

称重状态按车独立记录：

```python
weighed: set[car_no]
pending_weigh(car) = car.IsWeigh and car.No not in weighed
```

称重动作只允许：

```text
held 非空
held 尾车 pending_weigh
机库称重位可达且长度允许
```

如果宏内部一次挂多辆待称重车：

```text
held = [w1, w2, w3]
```

第一次 `Weigh` 只能标记尾车 `w3`。`w1`、`w2` 仍是独立称重债务，后续必须再次成为 held 尾车并经过机库称重位。

因此宏生成时要避免无意义地把多辆待称重车埋在 held 内部。允许的策略：

1. 只取一个待称重车作为尾车称重。
2. 称重尾车后先 Put 走尾车，再让下一辆待称重车成为尾车。
3. 如果无法在闭合宏内完成，保留该车为未完成债务，不伪装完成。

最终 replay 后仍由业务满意度判断称重是否完成。

## 9. 目标内部排序：弱序

目标线内部排序不构造强制全序，而构造硬约束偏序：

```text
P_d = (
    强制台位边
  + 关门车顺位边
  + 称重尾位边
  + protected satisfied 固定边
)
```

同目标普通车辆只做软排序：

1. 同目标尽量成段。
2. 同业务类型尽量成段。
3. 高紧迫度车靠近北端可取侧。
4. 减少碎片和逆序。

由于目标线从北端 Put，最终更靠南的车应更早到达。这个反向关系只作为候选排序和 `self_restack` 重建顺序，不作为所有车辆的强制全序。

## 10. 势函数与主进度护栏

### 10.1 势函数量纲

所有势函数项尽量折算为“预期额外 operation 数”，避免权重玄学。

示例：

| 项 | 估算口径 |
|---|---|
| 被一个不同目标车压住 | 至少 2 个额外 operation：取出 + 放回/送走 |
| 目标线 not ready | 至少 2 个额外 operation：清目标线前缀 |
| 缓存污染走廊线 | 至少 2 个额外 operation：后续清空 |
| 多一个目标碎片 | 约 1 次额外 Put 或 Get 切分 |
| 多一个 route blocker | 至少 2 个额外 operation 清障 |

推荐评分：

```text
score(macro) =
(
    main_progress_after,
    expected_extra_operations_after,
    operation_count,
    hot_throat_cost,
    route_cost,
    stable_tiebreaker,
)
```

### 10.2 主进度量

定义主进度：

```python
main_progress(state) = (
    tier1_debt_count,
    tier1_blocked_count,
    tier2_debt_count,
    tier2_blocked_count,
    tier3_debt_count,
    tier3_blocked_count,
    protected_damage_count,
)
```

其中 `protected_damage_count` 必须恒为 0。

### 10.3 防振荡护栏

允许势函数局部上升，但主进度必须受控。

规则：

1. 每个闭合宏后记录 `main_progress`。
2. 若连续 `K` 个宏没有让 `main_progress` 严格下降，触发止损。
3. 止损后切换 portfolio 分支，例如：
   - 强制 `self_restack`。
   - 强制 route unblock。
   - 放宽缓存线。
   - 改用小 beam。
4. 若仍无进展，输出 partial 和阻塞原因。

建议初值：

```python
MAX_NO_PROGRESS_MACROS = 8
MAX_STALE_BEST_MACROS = 12
```

## 11. 宏动作集合

全局搜索只提交闭合宏。

| 宏动作 | 作用 | held 是否进入全局状态 |
|---|---|---|
| `direct_move` | 单源同目标直送 | 否 |
| `service_sweep` | 一取多放，宏内连续 Put | 否 |
| `target_prepare` | 清目标线北端冲突 | 否 |
| `self_restack` | 目标线自身原地重排 | 否 |
| `route_unblock` | 清路径阻塞资源 | 否 |
| `cache_repack` | 缓存线局部重码 | 否 |
| `cun5_release` | 存5北/存5南专项处理 | 否 |
| `weigh_sweep` | 称重尾位处理 | 否 |

宏动作内部可以包含多条原子 operation：

```text
Get
Put
Weigh
Put
...
```

但宏必须满足：

```text
start.held == ()
end.held == ()
```

## 12. 下界与最优性声明

第一版不追求无条件全局最优证明。

可用于排序的松弛下界：

```text
LB_relaxed =
  count(non_empty_source_lines_with_debt)
+ count(target_groups_still_needing_put)
+ count(pending_weigh_cars)
```

这是忽略栈约束、忽略缓存限制、忽略 protected 冻结后的低估，可作为 A* 排序启发。

但只在满足以下条件时声明证明：

1. 搜索动作集是闭合宏全集或明确列出裁剪限制。
2. 使用的 LB 是 admissible。
3. 队列剩余最小界不小于当前最优解。

否则输出：

```text
optimality = feasible_unproved
```

更诚实的第一版定位：

```text
带松弛下界排序的 anytime beam / weighted search
```

而不是强行声称 A* 全局最优。

## 13. 存5专项

`存5线北 -> 存5线南` 有 157 辆债务，且存5南存在 overfull 案例。因此存5不能按普通线处理。

规则：

1. `存5线北` 是优先释放源线。
2. `存5线北` 未清到可控状态前，延后向 `存5线南` Put。
3. `存5线南` 不作普通缓存线。
4. 从存5南侧去北侧目标，必须额外检查北端接近路径。
5. `cun5_release` 宏必须优先保持存5北到一级目标线的通道。

## 14. 初态假设

第四阶段从第三阶段 response replay 得到初态。

必须显式校验：

```text
stage3_final_held == ()
```

如果 Stage3 响应模型没有 held 概念，则通过 replay 后的操作终点和最终车位确认：

1. 最后一条 operation 后调车机不携带车辆。
2. 所有车辆都在某条股道上。
3. 不存在 “TrainCars 非空但未 Put” 的未闭合迹象。

若不能证明 held 为空：

```text
status = partial
blocking_reason = stage3_unclosed_held
```

不要把非空 held 默默带入第四阶段。

## 15. 搜索流程

### 15.1 主流程

```python
state = initial_closed_state()
best_progress = main_progress(state)
stale = 0

while not complete(state) and time_left():
    macros = generate_closed_macros(state)
    valid = [m for m in macros if validate_macro(m)]
    chosen = choose_by_score(valid)
    if chosen is None:
        break

    state = apply_closed_macro(state, chosen)
    progress = main_progress(state)

    if progress < best_progress:
        best_progress = progress
        stale = 0
    else:
        stale += 1

    if stale >= MAX_STALE_BEST_MACROS:
        switch_portfolio_branch_or_stop()
```

### 15.2 Portfolio

5 分钟预算内：

1. `greedy_closed_macro`：快速保底。
2. `structural_service_sweep`：按源线和目标 ready 处理大头流向。
3. `self_restack_first`：优先处理调梁棚、预修等自阻塞目标。
4. `beam_closed_macro`：小宽度前瞻闭合宏。
5. `relaxed_astar_small_debt`：只在低债务 case 中尝试证明闭合宏模型内最优。

选择键：

```text
(
    final_unsatisfied_count,
    business_hook_count,
    operation_count,
    hot_throat_cost,
    route_cost,
    cache_pollution,
)
```

如果业务勾数尚未统一，则先用 `operation_count`，但 summary 必须标明：

```text
hook_count_definition = operation_rows
```

## 16. 实施路线

### A. 闭合宏 MVP

1. 建立 `ClosedState`。
2. 实现 `direct_move` 和 `service_sweep`。
3. 宏内部逐条校验 Get/Put。
4. 宏结束强制 `held == ()`。
5. replay + `unsatisfied_cars` 验收。

### B. 硬骨头

1. `self_restack(调梁棚)`。
2. `self_restack(预修线)`。
3. 多次称重债务。
4. 存5专项。
5. 主进度单调护栏。

### C. 提质

1. 势函数按预期额外 operation 标定。
2. route unblock 宏。
3. closed macro beam。
4. 小债务 relaxed A*。

### D. 再评估是否需要长期 held

只有当闭合宏模型在 113 个 case 中出现明确证据：

```text
必须跨宏持有 held 才能完成或显著省勾
```

才增加受限 held。否则不引入。

## 17. 验收指标

| 指标 | 目标 |
|---|---|
| replay hard error | 0 |
| protected damage | 0 |
| final_unsatisfied_count | 优先为 0 |
| complete case 数 | 最大化 |
| business_hook_count / operation_count | 最小化 |
| `put_not_front_accessible` | 接近 0 |
| `get_route_blocked` / `put_route_blocked` | 明显下降 |
| self-blocking unresolved | 明显下降 |
| pending_weigh unresolved | 可解释且可追踪 |
| no-progress stop | 有 trace 原因 |

## 18. 最终口径

第四阶段第一版采用：

```text
闭合宏优先；
held 只在宏内存在；
全局状态不含 held；
自阻塞目标线专门处理；
称重按车独立、多次触发；
势函数统一到预期额外 operation；
主进度设置单调护栏；
不轻易声明全局最优；
最终以 replay 和全车满意度裁判。
```

这比“长期 held 全局状态”少一些理论表达能力，但更符合当前数据和 5 分钟预算，也更容易调试、验收和解释。
