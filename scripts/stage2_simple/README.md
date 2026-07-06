# stage2_simple

第二阶段求解器：从 `stage1_simple` 的输出状态继续，完成卸轮翻库与大库出库编组。

核心模型是操作行级 Dijkstra：

- 一条 `Get` 或 `Put` 计一勾；
- 线路按北端可达前缀建模；
- 机后车列允许跨多行携带，用来处理 `OFF...C4`；
- `存4线` 新增北侧段必须满足 `OFF* C4*`；
- 不使用 `存4南` 中转，不建模脱轨器；
- 最终验收以 replay 物理规则 + 阶段债务为准。

运行：

```bash
python3 scripts/stage2_simple/solve.py data/truth2 \
  --stage1-out artifacts/stage1_simple_initial_depot_done \
  --out artifacts/stage2_simple
```

