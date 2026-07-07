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
- 在候选静态排序之外，尾段会用小窗口动态选择比较执行后的第一阶段 debt、Get/Put
  咽喉阻塞和目标线组，避免把已完成阶段车反复倒出再编回。
- 候选执行后必须至少保留下一步合法候选，避免把机车推进 `存4南` 等死角后无法继续。
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

下游友好 profile：

```bash
python3 scripts/stage1_simple/solve.py data/truth2/validation_取送车计划_20260104Z.json \
  --out artifacts/stage1_simple_portfolio/0104Z \
  --portfolio-profiles default \
  --portfolio-objective stage4 \
  --time-budget-seconds 160
```

- 默认 `--profile baseline` 保持原第一阶段主合同排序。
- `balanced / stage3 / stage4` 会在同等主进度下参考下游势函数指标。
- `balanced / stage3 / stage4` 还会启用低风险直送：只送 `油漆线 / 抛丸线 / 洗罐站 / 预修线`，
  且必须是北端连续段、无强制位车、目标线为空或全是已满足且无强制位车；`调梁棚` 不做直送。
- `balanced / stage3 / stage4` 还会优先选择安全的“阻挡车整段直归真实目标线”：
  仅限 `机库线 / 预修线 / 油漆线 / 洗罐站 / 抛丸线`，目标线必须为空或全是已满足车；
  单车直归只允许从非存线来源触发，避免存线单车来回倒造成循环。
- `--portfolio-profiles default` 会同时跑 `baseline,balanced,stage3,stage4` 并择优；完整解优先，其次比较
  Stage1 debt、边界污染、Stage3 碎片、Stage4 下界和业务勾数。
- `--portfolio-objective stage4` 用于降低第四阶段压力：完整解和第一阶段主合同仍是硬前置，
  其后优先比较 Stage4 access blocked、Stage4 lower bound、Stage4 tail debt，再比较 Stage3 碎片和业务勾数。

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

当前全量验证口径：

- `data/truth2` 默认 80 搬运批次、单案 `--time-budget-seconds 120`：112/113 complete，1/113 partial，0 error。
- 平均搬运批次 9.796；按现场业务勾口径（`Get/Put`）平均 19.593 勾。
- 唯一 partial 是 `0103W`：大库阶段车所需编组长度超过 `机南 / 洗油北 / 机走棚 / 机走北`
  合计容量，summary 中标记 `assembly_capacity_deficit_m:77.7` 和 `assembly_capacity_impossible`。
- `replay_validator.py` 可用于复核 physical/state consistency；由于本脚本只输出第一阶段计划，完整业务终点
  target-line 校验预期不会全部满足。
