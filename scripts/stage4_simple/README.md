# Stage4

Stage4 接管 Stage3 后仍在前场的可行动债务，以有序车辆块、开放 carry episode、
语义目标窗口和拓扑资源义务构造调车方案。

## 活跃模块

- `scope.py`：冻结 active、protected、容量留置和目标合同；
- `domain.py` / `contracts.py`：闭合 session、临编 owner 和出库重挂合同；
- `topology.py`：共享区段 gate 与资源闭包；
- `model.py`：构建来源块、目标窗口、逆序与松弛成本；
- `search.py`：定义语义窗口、有序块流下界和统一物理转移；
- `construct.py`：生成 source-window 标签边，并维护 owner 栈与恢复租约；
- `planner.py`：生成 target-window 闭合会话和强制出库重挂 checkpoint；
- `episode.py`：在既有线路访问骨架上求最短开放 carry 投影；
- `optimizer.py`：以安全下界和 owner-run 排序估计执行块流标签搜索；
- `solve.py`：生成证书并执行独立及合并 replay。

合法边只有 `Get(prefix)`、`Put(carry suffix)` 和 `Weigh(tail)`。容量必然留置与
真实 active residual 分开报告；求解器不使用案例特判、profile、retry 或 fallback。

目标位置分为两层：`Stage4Problem` 保存不可变的物理位次基准和目标 owner 顺序，
每个 source 标签持有独立 rank ledger。spotting 临编栈使用稠密 owner 序号，只允许
严格相邻的低位次块前插；物理位置中的空号不会制造假断点，不同标签也不会互相污染。
当拓扑资源上的阻塞块全部属于同一 active owner 时，资源租约直接转化为该 owner 的
持续临编栈，后续 source-window 可以在同一列上续挂。

外层 frontier 默认最多评估 128 个标签。普通 source-window 固定评估 16 个 choice
标签；只有终局债务已全部进入显式 owner 栈，或初始状态仅有一条来源线且 owner 至少
交替 7 段时，才使用外层全局预算。该边界是状态条件，不根据失败、重试或案例编号切换。

每个 complete 标签先执行 1-label 的廉价成对事件筛选；主 frontier 结束后，只对最佳
incumbent 执行一次 8192-label 开放 carry 投影。投影把每个既有 hook 看成可跳过或执行
合法 `Get(prefix)`、`Put(carry suffix)`、`Weigh(tail)` 的访问槽，并包含三类严格回放边：

- 取消临时 `Put -> Get` 租约，让车辆继续留在 carry；
- 延迟 source suffix 到后续同线 Get；
- 将最高连续 final-rank 块直接提交到 spotting 目标的最终位置，删除后续恢复 Get。

所有候选都从 Stage4 初态重新通过统一物理转移和完整终态验证。episode 停止会明确报告
标签预算或时间预算耗尽，不会切换 fallback。

目标 Put 只把窗口置为 `OPEN`。仅当 active/position 债务完成，并且不存在 protected
恢复、未来 source 或 gate 义务时才置为 `SEALED`。用于剪枝的 `hook_lower_bound` 只有
“来源线数 + 目标线数 + 待过磅数”，保证 admissible；更强的最少 owner-run 结果只用于
标签排序，绝不用于剪枝。拓扑资源仍由每条候选边的统一物理转移严格验证。

出大库重挂只冻结业务要求的获取前缀：先 Get 存4骨干，再 Get 卸轮线上必须进入油漆的
前缀。该窗口内后续临时 Put/Get 可以被 episode 重排，但上述两次获取及其先后不可改变。

## 运行

```bash
python3 scripts/stage4_simple/solve.py data/truth2 \
  --stage3-out artifacts/fullflow_current/truth2/stage3 \
  --out artifacts/fullflow_current/truth2/stage4 \
  --time-budget-seconds 30 \
  --max-labels 128 \
  --max-expansions 30000
```

`input` 传 JSON 文件时运行单案，传目录时批量运行全部 `validation_*.json`。
输入不存在或目录中没有案例时直接报错。Stage3 产物缺失或未完成记为
`unavailable`，Stage4 真正求解未完成记为 `partial`，程序异常记为 `error`；批次中
存在非 complete 案例时退出码为 1。

## 验证

```bash
python3 -m pytest -q tests/test_stage4_structural_sessions.py tests/test_plan_api.py

python3 scripts/audit_stage4_episode_optimizer.py \
  --episode-max-labels 8192 \
  --out artifacts/stage4_block_flow_final/episode_projection_audit_target_suffix.json

python3 scripts/audit_stage4_frozen_resolve.py \
  --case 0127W --case 0225W --case 0311W \
  --out artifacts/stage4_block_flow_final/frozen_resolve_selected.json
```

冻结的 truth2/truth3 共 140 例原始产物位于
`artifacts/stage4_block_flow_final`，原始总计 2464 勾。开放 carry episode 审计对同一
起点和响应执行重新物理回放后得到 2225 勾，平均从 17.600 降到 15.893；85 例改善、
减少 239 勾、0 例回退，只有 3 例耗尽 8192 标签。140/140 的 planlet、独立 replay、
combined replay 和强制重挂前缀均通过，语义 SEALED 重开为 0。该投影审计不改变
Stage3 交接状态；`audit_stage4_frozen_resolve.py` 才会从冻结起点真实重跑全局优化器。
12 个高难冻结起点以 60 秒、128 外层标签重求解得到 336 -> 308 勾，12/12 complete、
0 回退，独立和 combined replay 全部通过。
