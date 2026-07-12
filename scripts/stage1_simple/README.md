# Stage1

Stage1 将去卸轮车辆编入 `存4线`，将去大库车辆编入
`机南/洗油北/机走棚/机走北`，并在不破坏主合同的前提下完成前场服务收尾。

## 边界

- 不操作修1-4库内、库外线路。
- `存4线` 结束时只保留卸轮合同车辆。
- 四条大库编组线结束时不保留非大库车辆。
- `Get` 只取北端前缀，`Put` 只摘机后尾部。
- 所有单步和复合 planlet 都由 `solver_vnext.physical` 终审。
- 单案只运行一个候选体系，不做失败重试、portfolio 或 fallback。

## 运行

```bash
python3 scripts/stage1_simple/solve.py data/truth2 \
  --out artifacts/fullflow_current/truth2/stage1 \
  --jobs 8 \
  --time-budget-seconds 300
```

`input` 传 JSON 文件时运行单案，传目录时批量运行全部 `validation_*.json`；
`--jobs` 只控制不同案例的并行度，不切换求解策略。
输入不存在或目录中没有案例时直接报错；批次中存在 partial/error 时退出码为 1。

`summary.business_hooks` 按 `Get/Put` 操作行计数；`summary.hooks` 是内部
planlet 批次数，两者不能混用。

## 验证

```bash
python3 -m pytest -q tests/test_stage1_service_capability.py
```
