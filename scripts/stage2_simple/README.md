# Stage2

Stage2 从 Stage1 终态继续，完成卸轮翻库和大库出库车辆向 `存4线` 的收集。

## 模型

求解器使用单一的栈约束单调作业 DAG。全局状态只记录已完成线路、存4收集器、
卸轮尾组、卸轮占用和机车位置；临时搬运必须在一个闭合 episode 内恢复。

硬边界：

- `存4线` 最多 Put 一次，并且是本阶段最后一个操作；
- 新增存4段满足 `OFF*C4*`；
- C4 子序列前三辆不得为关门车；
- 每步统一校验路径、容量、牵引、栈序和关门车；
- Stage2 独立 replay 与 Stage1+2 合并 replay 必须同时通过；
- 不包含 greedy、forced search、重试或 fallback。

## 运行

```bash
python3 scripts/stage2_simple/solve.py data/truth2 \
  --stage1-out artifacts/fullflow_current/truth2/stage1 \
  --out artifacts/fullflow_current/truth2/stage2
```

`input` 传 JSON 文件时运行单案，传目录时批量运行全部 `validation_*.json`。
输入不存在或目录中没有案例时直接报错。Stage1 产物缺失或未完成记为
`unavailable`，Stage2 真正求解未完成记为 `partial`，程序异常记为 `error`；批次中
存在非 complete 案例时退出码为 1。

## 验证

```bash
python3 -m pytest -q tests/test_stage2_monotone_episodes.py
```
