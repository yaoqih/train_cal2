# Stage4

Stage4 接管 Stage3 后仍在前场的可行动债务，以有序车辆块、目标窗口、临编 owner
和拓扑资源租约构造闭合调车 session。

## 活跃模块

- `scope.py`：冻结 active、protected、容量留置和目标合同；
- `domain.py` / `contracts.py`：闭合 session、临编 owner 和出库重挂合同；
- `topology.py`：共享区段 gate 与资源闭包；
- `model.py`：构建来源块、目标窗口、逆序与松弛成本；
- `search.py`：定义有序 carry 状态和统一物理转移；
- `construct.py`：生成 source-window 标签边，并维护 owner 栈与恢复租约；
- `planner.py`：生成 target-window 闭合会话和强制出库重挂 checkpoint；
- `optimizer.py`：在共享 checkpoint 上执行单一块流标签搜索；
- `solve.py`：生成证书并执行独立及合并 replay。

合法边只有 `Get(prefix)`、`Put(carry suffix)` 和 `Weigh(tail)`。容量必然留置与
真实 active residual 分开报告；求解器不使用案例特判、profile、retry 或 fallback。

目标位次分为两层：`Stage4Problem` 保存不可变的物理位次基准和目标 owner 顺序，
每个 source 标签持有独立 rank ledger。spotting 临编栈使用稠密 owner 序号，只允许
严格相邻的低位次块前插；物理位置中的空号不会制造假断点，不同标签也不会互相污染。
当拓扑资源上的阻塞块全部属于同一 active owner 时，资源租约直接转化为该 owner 的
持续临编栈，后续 source-window 可以在同一列上续挂。

source-window 使用单一自适应标签边界：尚未找到闭合边时最多检查 64 个标签；找到
普通中间闭合边后保留前 6 个结构标签；进入终局恢复域后检查最多 128 个标签。每个
checkpoint 最终提交按统一代价排序的 4 条边。该边界是同一标签图的状态分类，不是
失败后换策略。

## 运行

```bash
python3 scripts/stage4_simple/solve.py data/truth2 \
  --stage3-out artifacts/fullflow_current/truth2/stage3 \
  --out artifacts/fullflow_current/truth2/stage4 \
  --time-budget-seconds 30 \
  --max-labels 64 \
  --max-expansions 30000
```

`input` 传 JSON 文件时运行单案，传目录时批量运行全部 `validation_*.json`。
输入不存在或目录中没有案例时直接报错。Stage3 产物缺失或未完成记为
`unavailable`，Stage4 真正求解未完成记为 `partial`，程序异常记为 `error`；批次中
存在非 complete 案例时退出码为 1。

## 验证

```bash
python3 -m pytest -q tests/test_stage4_structural_sessions.py tests/test_plan_api.py
```

冻结的 truth2/truth3 共 140 例最终产物位于
`artifacts/stage4_block_flow_final`。当前结果为 140/140 可行动闭合，总计 2464 勾，
平均 17.600 勾；同起点基准为 2543 勾、平均 18.164 勾，逐例无增勾。独立 replay、
combined replay、pending gate staging、owner 位次链、未回收租约均为 0 违规，峰值
牵引当量为 20。
