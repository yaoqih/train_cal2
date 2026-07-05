# stage1_simple

一个独立的第一阶段简化求解器。

目标只覆盖第一阶段：

- 去卸轮线车辆编到 `存4线`。
- 去大库车辆优先编到 `机南 / 洗油北 / 机走棚 / 机走北`，容量或拓扑需要时允许暂留/编入 `存4线`。
- 第一阶段结束边界是 `存4线` 和编组线不残留非大库、非卸轮目标车辆。
- 不生成任何进出大库库内/库外线路的动作。
- 每个候选都通过 `solver_vnext.physical.validate_candidate` 校验后才执行。
- 候选覆盖北端连续编组、同线前段阻塞、进路中间线阻塞、边界污染清理和必要的咽喉释放。
- 候选执行后必须至少保留下一步合法候选，避免把机车推进 `存4南` 等死角后无法继续。
- 输出 response、summary、trace，便于直接交给 `replay_validator.py` 复核。

默认 `--max-hooks` 是 80，用于保证第一阶段求解完整性；如果要检查严格 40 勾边界，可以显式传
`--max-hooks 40`。

运行：

```bash
python3 scripts/stage1_simple/solve.py data/truth2/validation_取送车计划_20260103W.json --out artifacts/stage1_simple/20260103W
```

批量 smoke：

```bash
python3 scripts/stage1_simple/solve.py data/truth2 --out artifacts/stage1_simple_batch --limit 10
```

当前全量验证口径：

- `data/truth2`，默认 80 勾：113/113 complete，平均 14.805 勾。
- `data/truth2 --max-hooks 40`：111/113 complete，`0309Z` 和 `0318W` 各需要 44 勾。
- `replay_validator.py` 物理复核：113/113 physical pass，state warning 0。
