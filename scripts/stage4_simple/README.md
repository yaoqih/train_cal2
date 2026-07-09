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
  --out artifacts/stage4_capability_portfolio_full
```

口径：

- `Get` / `Put` / `Weigh` 每条 operation 各算一钩。
- 全局搜索状态保持闭合，`held` 只存在于闭合宏内部。
- 宏内部用 `physical.validate_candidate` 的 planlet 校验，宏结束必须无 carry。
- 对大库/对位线的 Put 会输出可选 `Positions`，replay 使用同一位置语义，支持强制对位存在空位。
- 已实现宏类型：
  - 单源闭合 sweep：一次取车，按尾部可摘顺序连续 Put 到目标或缓存。
  - target rebuild：目标线既有车与外部来车在同一 Put 中重放，支持强制台位插入。
  - Get 路径清障：先挂走阻断线路车辆，完成源线服务后原线放回。
  - Put 路径清障：目标送达路径被占用时，可先清阻断线并原线恢复。
  - target rebuild route session：先将目标 Put 路径阻断线车辆暂存到缓存线，完成目标线重建后再恢复阻断线。
  - 存5北/存5南分段转移：将存5北仍需去存5南的 active 车按业务段显式成组生成候选，避免普通前缀枚举截断。
  - spotting repack：复用 `solver_vnext.spotting` 的对位线跨线重排能力，通过邻近线路暂存后重建目标线。
- 默认启用显式 portfolio：先跑低成本 `defer` 策略；若未完成，再跑允许必要强对位重排的 `critical` 策略；按 replay、残债、勾数选择结果。
- 当前版本不声明全局最优，只输出物理/业务 replay 可验收的可行解；未覆盖的 partial 主要来自更复杂的多源缓存重排、深层阻断线清障、目标窗口局部重建和 Stage3/Stage4 形态协同。

portfolio 参数：

```bash
# 默认：低成本分支完成即停止，否则再跑 critical 分支
--stage4-portfolio completion

# 对每个 case 都评估 defer 和 critical 两条分支
--stage4-portfolio all

# 关闭 portfolio，只使用指定重排策略
--stage4-portfolio off --heavy-repack-policy defer
--stage4-portfolio off --heavy-repack-policy critical
```

最新全量验证：

```text
artifact: artifacts/stage4_capability_portfolio_full
total cases: 113
stage3 usable cases: 109
complete usable cases: 88
partial usable cases: 21
usable final unsatisfied sum: 75
avg business hooks on complete cases: 26.83
replay/combined replay hard violations: 0
```

验证示例：

```bash
python3 -m py_compile scripts/stage4_simple/solve.py scripts/solver_vnext/physical.py
python3 replay_validator.py artifacts/stage4_closed_macro_full_current/0104W_response.json \
  --request artifacts/stage4_closed_macro_full_current/0104W_stage4_request.json
```
