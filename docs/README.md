# train_cal2 文档入口

本文档目录只保留五类信息，避免业务规则、方案设计、人工基准和运行诊断混在一起。

| 文档 | 作用 |
|---|---|
| `福州调车业务文档.md` | 业务规则、线路能力、调车约束和输出要求 |
| `接口文档.md` | WebAPI 输入输出契约 |
| `人工调车案例基准研究.md` | 人工案例的阶段、勾数和结构基准 |
| `scripts_vnext_新人导读.md` | 面向新人解释 `scripts/solver_vnext` 的入口、流程、模块职责和诊断产物 |
| `flow_edge_structure_model_design.md` | 目标求解结构、P0-P10 / H1-H5 / R1-R6 验收标准 |
| `P10_人工差距结构诊断.md` | 当前 `truth2` runtime 评估、人工差距和下一步结构修复看板 |
| `P10_runtime_结构深度审核.md` | 当前 runtime 实现分层、冗杂风险、必要复杂度和重构边界审核 |

当前主评估产物：

```text
artifacts/current_truth2_eval/
```

重跑命令：

```bash
rtk python3 scripts/generate_vnext_runtime_trace.py --root . --output-dir artifacts/current_truth2_eval --max-hooks 300 --check
```

当前目标不是继续堆文档，而是围绕这些指标做结构本体：

| 指标 | 目标 |
|---|---|
| 完成率 | 113 / 113，或明确剔除输入物理不可行案例 |
| 硬物理违规 | 0 |
| 业务勾数 | 人工可比案例不高于人工计划软上界 |
| 远端业务勾 | `remote_hook_delta_p50 <= 1` |
| 远端/非远端业务勾切换 | P50 <= 5，P90 <= 8，后续补人工切换基准后改为逐案 `solver <= manual + 1` |
| R1-R6 | 全部通过 |
