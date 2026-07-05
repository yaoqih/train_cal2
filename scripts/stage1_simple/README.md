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

当前全量验证口径：

- 严格北端作业口径下，`data/truth2` 默认 80 搬运批次：62/113 complete，51/113 partial。
- 按现场业务勾口径（`Get/Put`）：平均 35.168 勾；`0317W` 为 40 勾。
- 独立 `replay_validator.py` 复核当前输出：physical/business/state warning 均为 0。
- 目前剩余问题是求解策略覆盖不足，不是端位、路径或阶段边界规则可以放松的问题。
