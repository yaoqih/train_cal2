# stage4_simple

第四阶段闭合宏求解器。

默认从 Stage3 输出目录读取：

- `<case>_stage3_request.json`
- `<case>_response.json`
- `<case>_combined_response.json`
- `<case>_summary.json`

运行示例：

```bash
python3 scripts/stage4_simple/solve.py data/truth2 \
  --stage3-out artifacts/four_stage_balanced_early_release_v2/stage3 \
  --out artifacts/stage4_defer_current
```

口径：

- `Get` / `Put` / `Weigh` 每条 operation 各算一钩。
- 全局搜索状态保持闭合，`held` 只存在于闭合宏内部。
- 宏内部用 `physical.validate_candidate` 的 planlet 校验，宏结束必须无 carry。
- 已实现宏类型：
  - 单源闭合 sweep：一次取车，按尾部可摘顺序连续 Put 到目标或缓存。
  - target rebuild：目标线既有车与外部来车在同一 Put 中重放，支持强制台位插入。
  - Get 路径清障：先挂走阻断线路车辆，完成源线服务后原线放回。
- 当前版本不声明全局最优，只输出物理/业务 replay 可验收的可行解；未覆盖的 partial 主要来自更复杂的目标线内部换栈。

验证示例：

```bash
python3 -m py_compile scripts/stage4_simple/solve.py scripts/solver_vnext/physical.py
python3 replay_validator.py artifacts/stage4_closed_macro_full_current/0104W_response.json \
  --request artifacts/stage4_closed_macro_full_current/0104W_stage4_request.json
```
