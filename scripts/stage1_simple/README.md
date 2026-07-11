# stage1_simple

一个独立的第一阶段简化求解器。

目标只覆盖第一阶段：

- 去卸轮线车辆编到 `存4线`。
- 去大库车辆编到 `机南 / 洗油北 / 机走棚 / 机走北`。
- `存4线` 只接收去卸轮线车辆。
- 第一阶段结束边界是 `存4线` 不残留非卸轮阶段车，编组线不残留非大库阶段车。
- 不生成任何进出大库库内/库外线路的动作。
- 每个候选都通过 `solver_vnext.physical.validate_candidate` 校验后才执行。
- 候选覆盖北端连续编组、同线前段阻塞、进路中间线阻塞、边界污染清理和必要的咽喉释放。
- 主候选支持受控 `Get -> Put...` 复合宏动作：单源北端前缀按机尾后缀顺序分放到最多四个目标段。
  装配阶段只允许宏动作一次清空该源线的 Stage1 待办，并且必须完成 Stage1 或严格减少 Get/Put
  进路阻塞；普通债务下降仍使用单取单放。
- Stage1 主合同完成后进入显式服务收尾，最多 4 个 planlet、12 次 Get/Put；每一步必须保持
  Stage1 完成并严格改善服务质量，且已有精确对位数和南端连续到位数均不可回退。
- 对抛丸线、洗罐站、调梁棚支持同线定置闭合：整线可一次牵出且存在错误强制位时，执行受控
  `Get T -> Put T`，复用统一位号规划修复对位，不为线路或案例维护专用顺序。
- 服务收尾支持目标线重建宏动作：清出目标线污染后缀、机后保留原到位前缀、从另一源线取目标车
  回填，最后恢复原前缀，对应受控 `GPGP/GPGPP`。同源重复目标段可用渐进宏动作边放边继续取。
- 服务收尾支持双源合并取车：两个源线的北端连续段去同一个已开放真实目标时，可执行
  `Get A -> Get B -> Put T`。总牵引仍受 20 折算辆约束，且至少一次送达两辆；相较两个独立
  `Get -> Put` 少一次挂摘操作。
- 重建宏动作必须满足投入产出门槛：每 2 次 Get/Put 至少新增 1 辆直接到位，5 步保留前缀宏动作
  至少新增 3 辆，避免为单车收益消耗整条作业链。
- 每轮只生成一个候选池，并在一个上下文窗口内统一比较执行后的第一阶段 debt、Get/Put
  咽喉阻塞和目标线组；每个候选只做一次物理终审。
- 候选执行后必须能由同一个主候选生成器给出下一步合法动作，避免用实际不可执行的候选证明可继续。
- 每案只运行一次求解器。候选池没有可接受动作时直接输出拒绝原因，不再运行第二候选池或
  自动切换其他求解结果。
- 输出 response、summary、trace，便于直接交给 `replay_validator.py` 复核。

默认 `--max-hooks` 是 80。这里的 `hooks` 是求解器内部搬运批次：一次 `Get+Put` 算 1 批；
现场业务勾数应按 `Operations` 里的 `Get/Put` 次数计算，通常是搬运批次的 2 倍。称重 `Weigh`
不计入挂摘业务勾。

运行：

```bash
python3 scripts/stage1_simple/solve.py data/truth2/validation_取送车计划_20260103W.json --out artifacts/stage1_simple/20260103W
```

批量 smoke：

```bash
python3 scripts/stage1_simple/solve.py data/truth2 --out artifacts/stage1_simple_batch --limit 10
```

并行批量求解：

```bash
python3 scripts/stage1_simple/solve.py data/truth2 \
  --out artifacts/stage1_simple_batch \
  --jobs 8 \
  --time-budget-seconds 180
```

- 默认使用 `balanced` profile；`--profile` 是显式实验参数，不做自动多profile选择。
- `--jobs` 只并行不同案例，不改变单案决策逻辑。
- 主合同完成、Stage1边界纯度和物理合法性是硬约束；上下文窗口比较主合同债务、咽喉阻塞、
  Stage3/Stage4质量和路径代价。
- 服务目标覆盖前场、调梁、预修/机库和存车区域；去 `机走棚 / 机走北` 的车辆按业务口径不计入
  服务到位，这两条线在 Stage1 只作为大库编组线。
- 强制位置车使用 `physical.planned_positions_for_batch` 生成本次 Put 的位置方案；普通批次保持北端
  栈式落位，不会为了既有强制位车虚构从北端插入其后方的动作。
- summary 同时输出 `service_quality`、`service_gain`、服务收尾 planlet 和 Get/Put 用量。

下游友好度统计：

```bash
python3 scripts/analyze_stage1_friendliness.py \
  --stage1-dir artifacts/stage1_simple_batch \
  --truth-dir data/truth2
```

输出在 `<stage1-dir>/stage1_friendliness/`：

- `stage1_friendliness_cases.csv`：单案总指标。
- `stage1_friendliness_lines.csv`：四条编组线的装载、纯度和分组碎片。
- `stage1_friendliness_vehicles.csv`：编组线逐车明细。
- `stage1_friendliness_summary.json`：汇总和最差案例清单。

核心指标：

- `stage3_ready_score`：第三阶段主列友好分，0-100；按第三阶段模板暴露顺序统计大库车分组是否连续。
- `stage3_group_run_count` / `stage3_extra_fragment_count`：大库分组切换和碎片数；越低越好。
- `depot_unassembled_count`：第一阶段结束仍未编入四条编组线的大库阶段车。
- `boundary_pollution_count`：`存4线` 和四条编组线的阶段边界污染。
- `stage4_tail_debt_count`：第一阶段后仍未到位的非大库剩余债务。
- `stage4_access_blocked_debt_count`：这些剩余债务里被前端非债务车挡住的数量。

`replay_validator.py` 可用于复核 physical/state consistency；由于本脚本只输出第一阶段计划，完整业务终点
target-line 校验预期不会全部满足。

当前定置闭合结果（`artifacts/stage1_spotting_full_v3`）：

- `data/truth2` 共 113 案：112 complete、1 partial、0 error。
- 平均 9.239 个 planlet、20.894 次 Get/Put；相比上一稳定基线平均增加 0.354 次 Get/Put。
- 唯一 partial 为 `0103W`，四条编组线容量赤字 77.7m。
- 113/113 通过 physical 和 state consistency 回放。
- 在 85 个成功回放人工计划 case 上，其他真实目标严格新增到位 796 辆（人工 473），南端连续
  新增 838 辆（人工 164）。
- 上述到位数保留业务位号并叠加强制位校验。447 辆强制位车中，算法最终精确对位 263 辆，
  人工 185 辆；到达目标线后的精确对位率分别为 92.9% 和 70.9%。
- 算法 Get/Put 总数 1791，人工计划 1755；算法平均每案多 0.424 次 Get/Put。
- 排除预修线和机库线后，算法新增到位 514 辆、人工计划 359 辆；调梁区域新增 230 辆，人工
  126 辆；前场洗油抛新增 133 辆，人工 118 辆。
- 8 个同线定置闭合使用 16 次 Get/Put，直接增加 12 辆精确对位和 18 辆南端连续到位。
- 双源 `GGP` 共使用 6 次；`0122W` 通过一次双源合并
  取 6 辆送调梁棚，将该案服务净增从 5 辆提高到 11 辆。

当前机后车列只存在于一个有界 planlet 内，不能跨搜索节点长期保留；也没有把第一阶段和滚动入库
联合成一个持续合同。部分人工长链仍依赖这两种能力，后续应扩展统一机后栈状态，而不是增加
目标线路 rank 或 case 特判。
