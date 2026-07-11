# stage4_simple

Stage4 是 Stage3 完成大库主流程后的前场残债闭合器。它读取 Stage3 的最终状态，处理存车、洗油抛、调梁、预修、机区和强台位重排；修 1-4 库目标与卸轮目标仍属于上游阶段契约，不由 Stage4 重新规划。

## 运行

默认读取以下 Stage3 产物：

- `<case>_stage3_request.json`
- `<case>_response.json`
- `<case>_combined_response.json`
- `<case>_summary.json`

```bash
python scripts/stage4_simple/solve.py data/truth2 \
  --stage3-out artifacts/four_stage_balanced_early_release_v2/stage3 \
  --out artifacts/stage4_refactor_single_full_final \
  --time-budget-seconds 240
```

求解器只有一套确定性的 `structural` 策略。每一步只生成一次候选、执行一次物理/业务校验并在同一个排序池中选择；没有第二策略、失败重跑或多结果选优。命令行与 `plan_api` 也只暴露时间、宏数量和候选数量上限。

## 不变量

- `Get`、`Put`、`Weigh` 每条 operation 各计一钩。
- 全局状态始终闭合；持车只存在于单个 session 内，session 退出时必须清空。
- 每个候选先经 `physical.validate_candidate` 验证端别、路径、占线、牵引长度、摘挂顺序、容量、台位和业务组窗。
- 初始已满足车辆受硬保护，不能因后续宏变成未满足。
- 仍有入线需求的目标线被预约，不能用作持久缓存。
- 持久 Put 不能新增其他残债车辆的服务路径锁或机车接近锁。
- `机南/洗油北/机走棚/调梁线北/机北2` 使用单调 gate lease：允许临时取下并原样恢复初始 blocker，但候选结束时不能给仍未关闭的服务族新增 gate 占车。
- 临存车辆必须能从缓存线再次取出；缓存线不能位于该组从源线到真实目标的中间必经路径上。
- 对位线 Put 不能埋住多个迁出目标；仅当迁出车辆同目标、目标可接收且清理前缀不超牵引上限时允许可恢复堆叠。
- 单目标车辆总长超过线路有效长度时，先精确求最少留置辆数，再求其余车辆的可行方案；终止时同时校验留置辆数和留置总长足以覆盖赤字，容量留置不伪装成搜索失败。

这些约束直接使用 `TrackGraph`、当前占线和 Request 目标计算。因此，“抛丸未完成时不能占住机南”“洗南/洗北未完成时不能占住洗油北”“调梁棚未完成时保护调梁线北”等结论来自当前拓扑依赖，不是按案例或车号写死。

## 结构能力

- `service sweep`：从北端取一个可牵前缀，按机后尾端可摘顺序连续送真实目标。
- `storage shaping`：显式处理存5北到存5南的完整业务段，并把容量留置车辆单独送往中性缓存。
- `corridor release`：临时取走挡住串联目标的已满足尾段，完成服务后按原结构恢复。
- `route clearing`：在同一闭合 session 内清理 Get/Put 路径 blocker、完成主服务并恢复临时占线。
- `target rebuild`：联合目标线既有车和外部来车一次重放，支持强制台位插入。
- `layout rebuild`：先求最终合法布局，再按连续块分配临停线并重建多个参与线路。
- `ordered prefix restore`：先取目标既有车，再取“已满足前缀 + 深层目标车”，按深位到浅位回放目标并把前缀恢复原线；典型占用目标由 10 步降到 5 步。
- `multi-source same-target`：按目标最终位置序，从 2-3 条独立源线连续 Get，目标线只重建一次；总挂车始终受 20 当量约束。
- `partial drop + continue Get`：先摘首源尾段，保留头段，再取第二源同目标车并合挂，整个 session 最终清空持车。
- `same-line spotting repack`：对已经在目标线但台位/组窗错误的车辆分块临停、逆序回取和重放。
- `dirty terminal stack`：普通非通道终端线允许可证明能清理的目标车堆叠；对位线和待用通道不允许。
- `dynamic target refresh`：多目标车辆按当前容量重新选择等价目标，候选探测和真实接纳使用同一状态语义。

同等结构成本下，评分先比较真实残债下降，再比较路径不可用、目标线污染数量/深度、强台位重建缺陷和阻塞深度。大规模布局重建与直接 sweep 位于同一候选池，但具有更高结构成本，避免为了局部进展无条件整线搬运。

## 全量结果

验证产物：`artifacts/stage4_refactor_single_full_final`

| 指标 | 结果 |
|---|---:|
| truth2 案例 | 113 |
| Stage3 可用 | 109 |
| Stage3 不可用 | 4 |
| Stage3 可用且容量可行 | 102 |
| 完成 | 102/102 |
| 单目标容量不可行 | 7 |
| 容量理论最少残车 / 实际残车 | 8 / 8 |
| 非容量搜索失败 | 0 |
| complete 平均钩数 | 25.951 |
| complete 中位数 / P95 / 最大值 | 25 / 46 / 54 |
| Stage4 replay 硬违反 | 0 |
| 四阶段 combined replay 硬违反 | 0 |

与旧 `stage4_capability_portfolio_full` 的 88 个已完成案例配对：完成性回退 0，45 例减钩、18 例持平、25 例增钩，合计减少 139 钩，平均每例减少 1.58 钩；另新增 14 个完成案例。当前结果只证明可行性，不声明逐例或全局最少钩。

## 多摘多挂重构验证

本轮使用与 `fullflow_truth23_spotting_parallel_v1` 完全相同的 Stage3 输入，对 truth2 113 例和 truth3 34 例逐例配对。最终产物：

- `artifacts/stage4_multiget_refactor_final_v2_truth2`
- `artifacts/stage4_multiget_refactor_final_v2_truth3`
- `artifacts/multi_get_capability_refactor_validation`

| 指标 | truth2 | truth3 | 合计 |
|---|---:|---:|---:|
| 案例 | 113 | 34 | 147 |
| 旧 complete | 98 | 23 | 121 |
| 新 complete | 102 | 27 | 129 |
| 新增 complete / 回退 | 4 / 0 | 4 / 0 | 8 / 0 |
| 新 complete 平均钩数 | 20.696 | 11.889 | - |
| 新 complete 中位数 / P95 / 最大值 | 21 / 42.8 / 50 | 10 / 25.7 / 34 | - |
| 旧新均 complete 配对减钩 | 99 | 21 | 120 |
| 配对减钩 / 持平 / 增钩案例 | 31 / 63 / 4 | 8 / 14 / 1 | 39 / 77 / 5 |

能力分析器在 140 个有 Stage4 response 的案例中识别到：

- `multi_source_same_target_session` 45 个宏、覆盖 39 例，45/45 都是战略性多源，不是 blocker 恢复或目标既有车回取；
- `partial_drop_continue_get_session` 在 `0421W` 实际命中 1 个 4 钩宏；
- `ordered_prefix_restore` 命中 16 个宏；原 8 个 protected-prefix 死点全部完成；
- 独立重放 Stage4 140 次、combined 140 次，`schema/physical/business/state` 硬违反和警告均为 0；另 7 例因 Stage3 已是 partial，没有提交 Stage4 response。

仍有 4 个容量之外的可行动残余案例：truth2 `0309Z`，truth3 `0401W/0421Z/0428Z`。因此本轮结论是“提高可解性并消除已知 protected-prefix 死点”，不是宣称所有容量可行输入已全解。

## 验证

```bash
python -m py_compile \
  scripts/stage4_simple/solve.py \
  scripts/solver_vnext/spotting.py \
  plan_api/pipeline.py

pytest -q \
  tests/test_stage4_structural_sessions.py \
  tests/test_analyze_multi_get_capability.py \
  tests/test_plan_api.py

python scripts/analyze_multi_get_capability.py \
  --algorithm-dir artifacts/stage4_multiget_refactor_final_v2 \
  --output-dir artifacts/multi_get_capability_refactor_validation

python replay_validator.py \
  artifacts/stage4_refactor_single_full_final/0204Z_response.json \
  --request artifacts/stage4_refactor_single_full_final/0204Z_stage4_request.json
```
