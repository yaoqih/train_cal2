# Stage3

Stage3 从 Stage2 终态接管去大库车辆，完成修1-4库内/库外的线路与台位分配。

## 模型

1. `placement.py` 把库内台位、库外线路和混合目标放入同一个有限域，统一约束强制
   台位、固定车访问前沿、厂段修顺序、库外长度与恢复车占用。根节点和搜索节点使用
   Hall 缺集及加权容量必要条件；不可行证书可独立复算。
2. placement 使用 best-first branch-and-bound。第一目标严格按“暴露终点 run + 是否
   暂存的一对共享 Put/Get”计分，暂存车辆数只作第二目标，避免把同一批暂存逐车重复
   计费。昂贵的“零暂存是否可能”DP 只在节点真正进入竞争时惰性精化，并在一次求解内
   按物理域缓存。预算只截断同一棵搜索树，不触发备用布局。
3. 操作层是一个物理 `State` 图上的统一搜索。模型允许的 Get 前缀和 Put 后缀都是
   primitive 边；容量交换、直接转送、暴露车合批、库门链和混合门区处理只是由同样
   合法操作组成的精确 shortcut，不提供独有可达性。暴露车合批会在同目标车组仍在
   牵出线时先 Get 再共同 Put，避免之后独立 Get/Put。台位对位由 held blocker debt
   这一纯状态势引导，不再在每个外层节点内重复运行局部暂存搜索。
4. 引导 OPEN 与可采纳 anchor OPEN 共享同一状态最优标签、expanded ledger 和 parent
   record。首解势函数为 `g + 2 * pending`：少一辆未闭包车最多抵两勾，同等闭包进度下
   必然先选真实勾数更少的路径，避免批量宏用隐藏暂存换进度。其后再比较 `f=g+h`、
   alignment debt 和 staging affinity。anchor 严格按 pathmax 后的可采纳 `f` 展开；
   只有 `min OPEN f` 不可能改善 incumbent 时才声明该构型已证明。
5. 稳定闭包只用于下界和进度，不锁死物理状态。恢复车同线的临时位置变化不会被错误
   计作必需 Get；无强制台位的多外库目标车可在任一允许外库线结束，placement 线路仅
   承担容量构型含义。库内位置和显式 `ForceTargetPosition` 是硬台位；无强制的库外
   `Position` 是 compact 序号，允许插入车辆时整体平移。
6. placement 构型始终按业务勾下界、placement 分和稳定模板顺序筛选，不以 partial
   进度压过低成本构型。每个 `(template, layout)` 的 records、OPEN 和 incumbent 会在
   25→100→… 的累计预算间持续复用，扩大预算不会从根重搜。所有不同构型先各完成一次
   25-expansion 筛查；随后严格按可采纳下界和已观察完整解勾数集中加深，不按 partial
   进度分配质量预算。预算内得到但尚未证明的 complete 会继续优化，只有已证明构型才
   停止加深。
7. 物理层逐步验证路径、操作端、牵引、容量、库门和最终 `Positions`。完整结果还必须
   通过 Stage3 独立 replay、Stage1-3 合并 replay、台位规则和内部状态投影。

严格下界区分“固定构型”和“全 placement 松弛”两个作用域。多条库内线的对位倒置、
门位债务以及多辆暂存车都可能共享同一操作，因此共享债务取最大值而不相加；任何下界
超过已验证完整解都会写入 `lower_bound_validation_violations` 并标记证书无效。全域 gap
为 0 时即已证明达到下界，无需枚举完所有 placement。

主路径只有这一套模型；预算耗尽、已证明不可行和程序异常具有不同语义，不存在
失败后切换的备用求解器。

## 运行

```bash
python3 scripts/stage3_simple/solve.py data/truth2 \
  --stage2-out artifacts/fullflow_current/truth2/stage2 \
  --out artifacts/fullflow_current/truth2/stage3
```

`input` 传 JSON 文件时运行单案，传目录时批量运行全部 `validation_*.json`。
输入不存在或目录中没有案例时直接报错。Stage2 产物缺失或未完成记为
`unavailable`，Stage3 在显式预算内未完成记为 `partial`。程序异常保留 traceback
并立即终止批次，不会降级成普通不可解案例。批次中存在非 complete 案例时退出码为 1。
`partial` 会报告内部尝试勾数和稳定闭包车辆数，但响应的 `Operations` 与
`GeneratedEndStatus` 始终为空，不能把未完成事务交给下游执行。
`elapsed_seconds`/`wall_elapsed_seconds` 是整案墙钟时间，`chosen_search_seconds` 仅表示
最终入选构型在可恢复搜索中的累计活跃时间。

## 验证

```bash
.venv/bin/python -m pytest -q \
  tests/test_stage3_transactions.py \
  tests/test_stage3_unified_model.py \
  tests/test_stage3_outer_capacity_witness.py \
  tests/test_stage3_simple_structural.py
```
