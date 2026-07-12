# Stage3

Stage3 从 Stage2 终态接管去大库车辆，完成修1-4库内/库外的线路与台位分配。

## 模型

1. 目标构型层为每辆入库车分配合法线路和稀疏台位。
2. 对齐层以有序块执行深位先放、门口清理和有限原子换位。
3. 物理层逐步验证路径、操作端、牵引、容量、库门和 `Positions`。

完整结果必须通过 Stage3 独立 replay、Stage1-3 合并 replay、台位规则和内部状态
投影。已证明的容量或顺序不可行会输出证书，不会进入另一套求解器。

## 运行

```bash
python3 scripts/stage3_simple/solve.py data/truth2 \
  --stage2-out artifacts/fullflow_current/truth2/stage2 \
  --out artifacts/fullflow_current/truth2/stage3
```

`input` 传 JSON 文件时运行单案，传目录时批量运行全部 `validation_*.json`。
输入不存在或目录中没有案例时直接报错。Stage2 产物缺失或未完成记为
`unavailable`，Stage3 真正求解未完成记为 `partial`，程序异常记为 `error`；批次中
存在非 complete 案例时退出码为 1。

## 验证

```bash
python3 -m pytest -q tests/test_stage3_simple_structural.py
```
