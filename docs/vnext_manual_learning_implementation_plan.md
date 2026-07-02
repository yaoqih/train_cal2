# vNext 从当前状态到达到并超越人工的实施路线

日期：2026-07-02

目标：把当前 vNext 从“有人工经验结构的规则求解器”，推进到“能从人工调车方案中学习不同层级逻辑，并在合法边界内稳定达到甚至超过人工”的系统。

本文不是最终优化清单，而是实施秩序。每一步必须有独立诊断、清晰边界、可量化验收。禁止为了短期完成率增加兜底候选或混合职责代码。

## 0. 当前基线

当前无兜底清理后的历史基线产物：

- `artifacts/no_fallback_clean_audit_20260702`
- `rtk pytest -q tests/test_vnext_rule_boundaries.py`：44 passed

当前工作树复跑结果：

- `rtk python3 scripts/generate_vnext_runtime_trace.py --root . --output-dir artifacts/codex_audit_scripts_20260702 --max-hooks 300 --check`
- 全量结果仍可复现：113 cases、completed 5、blocked 108、hooks 2877、hard physical accepted 0。
- 但当前工作树中 `tests/test_vnext_rule_boundaries.py`、`scripts/score_vnext_structures.py`、`scripts/audit_vnext_validation_sequence.py`、`scripts/compare_vnext_with_manual.py` 不存在或已删除，阶段命令链不能完整复现历史审计。

因此，当前状态不能只说“基线通过”。更准确的判断是：

| 项 | 当前判断 |
|---|---|
| runtime 主入口 | 可复现历史全量结果 |
| 硬物理接受违规 | 仍为 0 |
| 单测边界 | 当前工作树不可执行 |
| scorecard/manual compare/validation audit | 当前工作树不可执行 |
| 人工动作级学习闭环 | 尚未建立 |

当前全量结果：

| 指标 | 当前值 |
|---|---:|
| cases | 113 |
| completed | 5 |
| blocked | 108 |
| hooks | 2877 |
| hard physical accepted | 0 |
| final length warnings | 0 |
| resource structure failure | 0 |

结构评分：

| 结构 | 分数 | 结论 |
|---|---:|---|
| PhysicalBoundary | 100 | 硬物理边界稳定 |
| CandidateGenerationCoverage | 0 | 候选覆盖是最大短板 |
| HumanPhaseGate | 81 | 阶段骨架可用，但未完成学习闭环 |
| RemoteContinuity | 80 | 远端会话有效，但还不完整 |
| DepotOutboundSession | 100 | 出库 session 当前有效 |
| H4RemoteDigest | 95 | H4 主体 digest 有效 |
| Cun4H2H3Release | 100 | 存4释放边界当前稳定 |
| SerialGateSupport | 100 | 独立清门已删除，资源检查稳定 |
| SpottingCloseout | 0 | H5 需等主链稳定后再修 |

当前主要 generation gap：

| gap reason | count |
|---|---:|
| `source_prefix_blocker_requires_lease` | 82 |
| `target_not_reachable` | 51 |
| `episode_generated_zero_after_prefilter` | 44 |
| `source_not_reachable` | 40 |
| `serial_blocker_source_open` | 31 |
| `no_applicable_episode` | 21 |
| `spotting_repack_required` | 15 |

判断：当前不是策略分数问题，而是“人工计划中的结构动作不能被稳定召回”。因此先恢复可执行诊断链，再做人工计划动作级学习闭环，不先调 policy。

### 0.1 对复杂度和确定性的判断

按原路线直接进入阶段 D，不足以保证简洁优雅地达到人工，风险是越改越像一堆结构补丁。当前必须先补 A0 和复杂度/确定性约束，原因是：

| 风险 | 如果不处理会怎样 | 控制方式 |
|---|---|---|
| 工具链不可复现 | 只能依赖历史 artifact，无法证明当前修改有效 | 先恢复测试、scorecard、manual compare、validation audit |
| 人工状态缺失 | 用 solver 自己走到的状态误判人工候选召回 | 建立 manual replay |
| episode 静默过滤 | gap 下降/上升都无法解释 | 每个 prefilter 输出稳定 reason |
| 候选枚举扩张 | 看似覆盖更高，实际复杂度失控 | 每轮只增加一个结构自由度 |
| policy 过早调参 | 掩盖候选召回不足 | manual recall 达标前禁止调 policy |
| 局部优化外溢 | 从结构学习退化成搜索打补丁 | 只允许同 owner、同 intent、同结构等级内优化 |

因此，本计划的正确执行方式不是“逐个补 gap”，而是：

```text
先让诊断可复现
-> 让人工动作可标签化
-> 让人工计划状态可重放
-> 让人工结构候选可召回
-> 再用 bounded episode 修结构
-> 最后用 deterministic policy 和同结构局部优化超越人工
```

只有每一轮都通过 `complexity_budget_records.csv` 和 `determinism_check_records.csv`，才能说复杂度被分解、确定性被控制。否则即使 completed 上升，也不算路线正确。

## 1. 总体原则

### 1.1 分层原则

系统必须拆成以下层级，每一层只回答自己的问题：

| 层级 | 只回答什么 | 不负责什么 |
|---|---|---|
| 物理边界 | 这一步是否合法 | 不决定调车策略 |
| 资源结构 | 是否污染存4、修库槽位、串门、机车携带 | 不生成候选 |
| 人工标签 | 人工这一步在解决什么结构 | 不改 solver |
| 合同/流事实 | 当前有哪些 owner 债务 | 不决定动作细节 |
| episode | 能否生成一个结构候选 | 不做最终排序 |
| phase | 当前阶段允许什么 | 不弥补候选缺失 |
| policy | 多个合法候选选哪个 | 不创造不存在的候选 |
| 优化搜索 | 在合法结构空间内组合更优 | 不放松边界 |

### 1.2 禁止事项

- 禁止新增独立清门、独立让路、远端直达等兜底候选。
- 禁止一个 episode 在失败后切换成另一个结构。
- 禁止为了完成率放松物理、容量、关门车、修库槽位、串门约束。
- 禁止先调 policy 掩盖候选召回不足。
- 禁止只看完成率、钩数、远端切换作为单步验收。

### 1.3 每一步统一验收格式

每个结构修复都必须输出：

| 项 | 要求 |
|---|---|
| 输入范围 | 哪些 case、哪些 gap、哪些 family |
| 结构边界 | 这个结构负责什么，不负责什么 |
| 诊断产物 | CSV/JSON 中哪些字段证明问题 |
| 修复动作 | 改哪个模块，不改哪个模块 |
| 局部验收 | gap、candidate recall、resource、delta、phase 指标 |
| 集成验收 | 不破坏上游已通过结构 |
| 回滚条件 | 哪些指标恶化说明方向错 |

### 1.4 复杂度与确定性控制

能否达到甚至超过人工，不取决于堆多少 episode，而取决于每一层是否用最小状态表达人工策略，并且每一步都可复现、可诊断、可回滚。

每个新增或修改点必须先声明：

| 项 | 要求 |
|---|---|
| 输入 | 只读哪些状态、标签、合同、资源事实 |
| 输出 | 只产出哪些候选、记录、delta 或判定 |
| 状态归属 | 状态属于 manual replay、solver state、resource lease 还是 diagnostics |
| 枚举边界 | 最多枚举多少 family、candidate、plan step、临时线 |
| 排序规则 | 必须是确定性 tuple key，禁止隐式依赖 dict/set 顺序 |
| 失败原因 | 每个过滤点必须输出稳定 reason |
| 退出条件 | 什么指标达标后停止修改，不继续叠结构 |
| 回滚条件 | 什么指标恶化说明复杂度失控 |

复杂度预算：

| 层级 | 允许复杂度 | 禁止 |
|---|---|---|
| 人工标签 | 线性扫描人工计划钩序，输出动作语义 | 在标签阶段推测 solver 最优解 |
| manual replay | 按人工计划钩序顺序推进状态，每钩只做一次状态快照 | 用 solver rollout 替代人工状态 |
| candidate recall | 在人工状态上跑现有 episode，记录 exact / structural / none | 为了提高 recall 临时放宽物理或资源 |
| episode | bounded template，bounded planlet，候选数有上限并记录 | 全局清线、远端直达、失败后换结构兜底 |
| resource | 只仲裁资源和污染，不生成动作 | 在资源层补候选 |
| phase | 只决定阶段允许/禁止，不弥补候选缺失 | 用 phase 让非法候选过关 |
| policy | 只在合法候选间确定性排序 | 用 policy 创造候选或掩盖召回不足 |
| 局部优化 | 只在同 owner、同 intent、同结构等级内比较 | 扩成不可解释搜索 |

确定性要求：

- 同一输入、同一代码、同一 artifact 名称必须生成同一 CSV 行集合和排序。
- 所有候选 ID 必须由 case、hook、template、plan steps 和 move_nos 稳定生成。
- 所有候选排序必须显式写在 policy 或 episode 局部排序中。
- 所有集合输出写 CSV 前必须排序或保留业务顺序。
- 所有随机、时间戳、隐式文件顺序都不能参与候选生成和评分。

复杂度失控信号：

| 信号 | 处理 |
|---|---|
| 新增 episode 后 gap 没下降但 rejected 大幅增加 | 回滚 episode，先补 prefilter reason |
| candidate count 上升但 manual recall 不升 | 回滚枚举扩张 |
| completed 上升但 hard/resource/phase warning 上升 | 回滚，不接受 |
| hook ratio 下降但 selected manual equivalent rate 下降 | 不算达到人工 |
| 新模板无法说明 owner、intent、resource 边界 | 不允许合入 |
| 同一 gap 需要连续补多个特殊 case | 停止补丁，回到人工标签和结构边界 |

每轮只允许引入一个新的结构自由度。自由度包括：新 episode、新资源 lease、新 phase 例外、新 policy lane、新临时线枚举、新 planlet step。若一轮需要两个以上自由度，说明结构还没有被分解清楚，必须退回诊断。

## 2. 目标终局

最终不是“偶然完成更多 case”，而是达成以下状态：

| 目标 | 验收标准 |
|---|---|
| 硬边界可靠 | hard physical accepted = 0；final length warnings = 0 |
| 人工语义可学习 | 人工动作标签覆盖率 >= 95% |
| 人工候选可召回 | manual candidate recall >= 90%，核心链路 >= 95% |
| 主链可解 | completed >= 98%，目标 100% |
| 远端连续性接近人工 | solver remote transition p50 <= manual p50 + 1 |
| 钩数达到人工 | hook ratio p50 <= 1.00，p90 <= 1.05 |
| 超越人工 | hook ratio p50 < 1.00，同时 completed 不下降 |
| 失败可解释 | unknown gap repeat rate = 0 |

超过人工的路径不是“更大搜索”，而是：

1. 先完整召回人工结构。
2. 再在同样边界内做更稳定的 batch/session 组合。
3. 最后用 policy 和局部结构搜索减少远端切换、空走、碎勾。

## 3. 阶段 A：冻结硬边界

### 目标

确保后续所有学习和修复都建立在真实物理边界上。

### 边界

本阶段只检查：

- 线路可达
- 端别取放
- 牵引当量
- 线路长度
- 修库槽位
- 存4北口
- 串门污染
- 关门车
- 机车携带顺序

不允许新增候选、不允许调 policy。

### 诊断命令

当前工作树必须先通过阶段 A0 恢复缺失入口后，以下命令才算完整可执行。A0 未通过时，只能用 runtime 主入口复核硬物理，不得宣称阶段 A 完成。

```bash
rtk pytest -q tests/test_vnext_rule_boundaries.py
rtk python3 scripts/generate_vnext_runtime_trace.py --root . --output-dir artifacts/phaseA_physical_boundary --max-hooks 300 --check
rtk python3 scripts/score_vnext_structures.py --artifact-dir artifacts/phaseA_physical_boundary
```

### 验收

| 指标 | 通过标准 |
|---|---:|
| unit tests | 100% pass |
| hard physical accepted | 0 |
| final length warnings | 0 |
| resource structure failure | 0 |
| connection metric failure | 0 |

### 回滚条件

- 任一已接受候选出现物理 violation。
- 为了完成率放松容量、槽位或串门规则。

## 3.5 阶段 A0：恢复可执行诊断闭环

### 目标

把历史 artifact 中已经存在的审计能力恢复到当前工作树，并补上人工状态重放基座。没有这一层，后续 D/E/F 的修复会变成按 gap 名称加 episode，而不是学习人工策略。

### 必须恢复或保留的入口

当前入口要分成“已可执行”和“待恢复/待新增”两类。后续标准命令只能引用已存在入口；待恢复入口必须在本阶段显式补齐，不能在文档里假设已经可跑。

| 入口 | 作用 | 当前状态 | 当前要求 |
|---|---|---|---|
| `scripts/generate_manual_action_labels.py` | 人工动作级标签清洗 | 已新增、可执行 | 保留确定性复跑 |
| `scripts/replay_manual_identity_labels.py` | 人工身份状态重放 | 已新增、可执行 | 保留确定性复跑 |
| `scripts/analyze_manual_uncertainty_classes.py` | 结构化不确定性分级 | 已新增、可执行 | 纳入标准诊断链 |
| `tests/test_vnext_rule_boundaries.py` | 单元级硬边界回归 | 缺失 | 必须重新可执行 |
| `scripts/score_vnext_structures.py` | 结构 scorecard | 缺失 | 必须重新可执行 |
| `scripts/audit_vnext_validation_sequence.py` | 结构验证顺序审计 | 缺失 | 必须重新可执行 |
| `scripts/compare_vnext_with_manual.py` | 人工宏观对比 | 缺失 | 必须重新可执行 |
| `scripts/label_manual_owner_blockers.py` | 人工 owner/blocker 身份标签 | 缺失 | B6 前必须新增 |
| `scripts/audit_manual_candidate_recall.py` | 人工状态重放候选召回 | 缺失 | C0 前必须新增 |
| `scripts/audit_vnext_complexity_budget.py` | 复杂度预算审计 | 缺失 | episode 修复前必须新增 |
| `scripts/audit_vnext_determinism.py` | 确定性审计 | 缺失 | episode 修复前必须新增 |

### 新增产物

本阶段新增的产物不是最终优化指标，而是学习闭环的底座：

- `manual_action_labels.csv`
- `manual_phase_segments.csv`
- `manual_identity_replay_trace.csv`
- `manual_uncertainty_class_summary.csv`
- `manual_uncertainty_evidence_examples.csv`
- `manual_owner_blocker_labels.csv`
- `manual_candidate_recall.csv`
- `manual_candidate_gap_records.csv`
- `complexity_budget_records.csv`
- `determinism_check_records.csv`

### 边界

本阶段不改 episode、不改 policy、不追 completed。只恢复诊断入口、人工解析、人工状态重放、不确定性分级、owner/blocker 标签和候选召回审计。

人工状态重放必须满足：

- 每一步从人工计划动作前的真实状态出发。
- 只检查当前 solver episode 是否能生成等价结构候选。
- 不允许用 solver 自己 rollout 出来的 `step_trace.csv` 代替人工状态。
- 不允许把 policy 是否选中混入 candidate recall。
- 人工动作解析失败必须进入 label gap，不能跳过。

### 当前可执行诊断命令

这些命令已经有对应入口，必须保持可复现：

```bash
rtk python3 scripts/generate_manual_action_labels.py --root . --output-dir artifacts/phaseA0_manual_labels
rtk python3 scripts/replay_manual_identity_labels.py --root . --label-dir artifacts/phaseA0_manual_labels --output-dir artifacts/phaseA0_manual_identity --max-states 512
rtk python3 scripts/analyze_manual_uncertainty_classes.py --root . --label-dir artifacts/phaseA0_manual_labels --replay-dir artifacts/phaseA0_manual_identity --output-dir artifacts/phaseA0_manual_uncertainty
```

### A0 恢复后完整命令

以下命令包含当前缺失入口。只有对应脚本恢复后才允许纳入标准流水线：

```bash
rtk pytest -q tests/test_vnext_rule_boundaries.py
rtk python3 scripts/generate_vnext_runtime_trace.py --root . --output-dir artifacts/phaseA0_runtime --max-hooks 300 --check
rtk python3 scripts/score_vnext_structures.py --artifact-dir artifacts/phaseA0_runtime
rtk python3 scripts/audit_vnext_validation_sequence.py --artifact-dir artifacts/phaseA0_runtime
rtk python3 scripts/compare_vnext_with_manual.py --artifact-dir artifacts/phaseA0_runtime
rtk python3 scripts/label_manual_owner_blockers.py --root . --label-dir artifacts/phaseA0_manual_labels --replay-dir artifacts/phaseA0_manual_identity --uncertainty-dir artifacts/phaseA0_manual_uncertainty --output-dir artifacts/phaseA0_owner_blockers
rtk python3 scripts/audit_manual_candidate_recall.py --root . --label-dir artifacts/phaseA0_manual_labels --replay-dir artifacts/phaseA0_manual_identity --owner-blocker-dir artifacts/phaseA0_owner_blockers --output-dir artifacts/phaseA0_manual_recall
rtk python3 scripts/audit_vnext_complexity_budget.py --artifact-dir artifacts/phaseA0_runtime --manual-recall-dir artifacts/phaseA0_manual_recall
rtk python3 scripts/audit_vnext_determinism.py --root . --artifact-dir artifacts/phaseA0_runtime
```

### 验收

| 指标 | 通过标准 |
|---|---:|
| runtime 主入口 | 113 cases 可复现 |
| unit tests | 100% pass |
| scorecard/manual compare/audit scripts | 全部可执行 |
| manual action labels | 已有产物且字段完整 |
| manual identity replay | 已有产物；每条人工动作能定位动作前状态或明确 label gap |
| manual uncertainty classes | 已有产物；每类不确定项有置信度、算法动作、禁止假设 |
| owner/blocker labels | 新增产物；至少覆盖 B1 replay 通过且非 no-op 的动作 |
| manual candidate recall | 新增产物；能按结构输出 exact / structural / none |
| complexity budget | 每个脚本和候选层都有枚举上限记录 |
| determinism check | 同输入连续两次输出摘要一致 |
| hard physical accepted | 0 |

### 回滚条件

- 为了恢复历史脚本而放松当前硬物理或资源边界。
- 用 solver rollout 状态冒充人工动作状态。
- 只输出 case 级钩数对比，没有动作级 owner/blocker/intent。
- 新增诊断脚本依赖随机顺序、当前时间或不可复现文件遍历顺序。

## 4. 阶段 B：建立人工计划动作标签

### 目标

把人工计划从“结果对比”变成“可学习标签”。恢复后的 `compare_vnext_with_manual.py` 只能抽取钩数、远端切换、存4释放、机接、修库消化等宏观信号，还不足以监督 episode。

### 标签字段

新增产物建议：

- `manual_action_labels.csv`
- `manual_phase_segments.csv`
- `manual_structure_examples.csv`

当前已新增清洗入口：

```bash
rtk python3 scripts/generate_manual_action_labels.py --root . --output-dir artifacts/manual_label_cleaning_20260702
```

当前清洗产物：

- `manual_raw_operations.csv`
- `manual_action_labels.csv`
- `manual_phase_segments.csv`
- `manual_label_validation.csv`
- `manual_case_label_summary.csv`
- `manual_excluded_files.csv`
- `manual_label_cleaning_summary.json`

统计来源：

- `artifacts/manual_label_cleaning_20260702/manual_label_cleaning_summary.json`
- `artifacts/manual_identity_replay_20260702/manual_identity_replay_summary.json`
- `artifacts/manual_uncertainty_validation_20260702/manual_uncertainty_validation_summary.json`

当前清洗结果：

| 指标 | 数值 |
|---|---:|
| manual files | 118 |
| truth2 matched files | 109 |
| manual actions | 4052 |
| strict clean actions | 1724 |
| strict review actions | 2328 |
| invalid actions | 0 |
| strict clean action rate | 0.4255 |
| structural clean actions | 3414 |
| structural review actions | 638 |
| structural clean action rate | 0.8425 |
| excluded files | 1 |

这次清洗分成两个质量口径：

- `label_quality` 是严格口径，车号缺失也算 review，用于车号级准确对照。
- `structural_label_quality` 是结构口径，暂不把车号缺失当作结构错误，用于指导算法策略、episode 和 candidate recall。

因此当前已经可以用 `structural_label_quality=clean` 的 3414 条动作指导结构开发，但不能用全部 4052 条动作做车号级准确对照。

当前主要 label issue：

| issue | count | 处理 |
|---|---:|---|
| `car_no_annotation_missing` | 1793 | 按规则挂车需标末辆车号、7 辆及以上摘车需标近机首辆车号；缺失时不能做车号级准确对照 |
| `line_resolution_ambiguous` | 296 | 当前主要是 `库/注意库`，地图候选为 `机库线`，但语义仍需人工确认 |
| `ambiguous_line_alias` | 296 | 与 `line_resolution_ambiguous` 同源，不能作为 exact replay 的硬分母 |
| `count_omitted` | 263 | 省略辆数，不能作为 exact recall 标签 |
| `merged_hook_possible` | 256 | 备注含 `对/顶/代/接代/叉线接/叉线摘` 等结构信号，可能是一钩合并多个动作，只能进入 review |
| `count_cell_contains_semantic_token` | 119 | `回/停` 等写在辆数字段，保留原文并打 gap |
| `method_missing` | 116 | 方法省略，不能强推断 |
| `truth_case_missing` | 9 | 文件级状态验证缺口 |
| `aggregate_line_count_negative` | 9 | 聚合状态校验不通过，需逐案复盘 |
| `sequence_gap_possible_omitted_hook` | 2 | 明确疑似省略钩，当前为 `0130Z` 的第 24 钩 |

地图和规则清洗摘要：

| 维度 | 数值 | 说明 |
|---|---:|---|
| line `alias_exact` | 2533 | `调/洗/油/抛/机/存1` 等可确定映射 |
| line `inferred` | 1223 | `存5` 由北/南备注或唯一聚合候选解析；`修1-4/注意修N` 由库内/库外语义解析；`留道口洗` 解析到洗罐组 |
| line `ambiguous` | 296 | 主要是 `库/注意库`，暂不强洗成 clean |
| line `unknown` | 0 | `注意修2`、`留道口洗` 已进入验证层别名 |
| count `inferred` | 47 | `库外N/库外N摘` 解析为 `effective_count=N`，原始 `count_omitted` 保留 |
| site alias `存2叉=预修线` | 395 | 现场确认 `叉线` 是预修库/存2叉，不是普通道岔备注 |
| `存5 北头` endpoint hint | 393 | 现场确认是端别提示，不单独构成存4阶段 |
| car no required | 2108 | 挂车全部要求车号，摘车 7 辆及以上要求车号 |
| car no present | 337 | 人工实际写了 7 位车号的动作数 |
| air hose connect required | 5 | 只在匿名车列状态可确定时追溯到起始挂车钩 |
| air hose disconnect required | 5 | 只在整列摘清时判定接风管车辆必然被摘 |
| continuous positioning required | 319 | 修库内摘车 |
| continuous coupling required | 223 | 晚上修库挂车 |
| forklift assist expected | 181 | 中午修库挂车 |
| yard positioning required | 873 | 调梁、洗罐、油漆、抛丸相关对位 |
| route switch required | 3743 | 按 `TrackGraph` 从上一解析线路到当前线路经过的联络/渡线节点 |

下一步补法不是继续加正则，而是补一个人工计划身份状态重放层：

| 补法 | 覆盖对象 | 自动程度 | 产物 |
|---|---:|---|---|
| B1 身份状态重放 | 4003 条参考候选动作 | 自动，需用已有车号备注校准端别 | `manual_identity_replay_trace.csv` |
| B2 省略辆数结构化 | 216 条仍未解析；47 条 `库外N` 已高置信解析 | 分层：`库外N` 可自动；`存4 - 北头` 是提醒；`北头代N` 需拆语义 | `manual_inferred_counts.csv` |
| B3 合并钩拆解 | 256 条 `merged_hook_possible` | 59 条备注型可回放；27 条 `存5北头顶N` 是复合位移；其余保留 gap | `manual_compound_hook_labels.csv` |
| B4 库/机语义确认 | 296 条 `库/注意库` | `库/机 +1` 和 `称` 可作为机车/称重 no-op；其他仍需状态验证 | `manual_line_resolution_review.csv` |
| B5 地图业务名补齐 | 3743 条经路节点 | 未知线已清零；仍需补现场道岔别名，如 `L11=;14` | `route_switch_name_map.csv` |
| B6 owner/blocker 身份标签 | 当前 `owner_nos/blocker_nos` 为空 | 只覆盖 replay 通过且非 no-op 的动作，不能猜 review/gap 动作 | `manual_owner_blocker_labels.csv` |

当前已新增 B1 验证入口：

```bash
rtk python3 scripts/replay_manual_identity_labels.py --root . --label-dir artifacts/manual_label_cleaning_20260702 --output-dir artifacts/manual_identity_replay_20260702 --max-states 512
```

B1 当前产物：

- `manual_identity_replay_trace.csv`
- `manual_identity_replay_case_summary.csv`
- `manual_identity_replay_summary.json`

B1 严格验证结果：

| 指标 | 数值 |
|---|---:|
| hook count | 4052 |
| replayed hooks | 1118 |
| replayed hook rate | 0.2759 |
| reference hooks | 4003 |
| reference replayed hooks | 1106 |
| reference replayed hook rate | 0.2763 |
| identity no-op hooks | 131 |
| replayed unique hooks | 124 |
| replayed ambiguous hooks | 996 |
| unique moved hooks | 563 |
| matched car-no hooks | 105 |
| missing car-no hooks after replay | 615 |
| completed cases | 6 |
| identity conflicts | 42 |
| state-space exceeded | 34 |
| truth missing hooks | 214 |

B1 已验证的线路 scope，不再按单股道理解：

| 人工写法 | 经身份/车号验证后的 scope | 证据 |
|---|---|---|
| `洗` | `洗罐站|洗罐线北|洗油北` | `洗 +3/+4/+6` 单独 `洗罐站` 不足，组合后可重放 |
| `留道口洗` | `洗罐站|洗罐线北|洗油北` | 现场名进入洗罐组 scope，不能作为未知线 |
| `调` | `调梁棚|调梁线北` | `调 +N` 在部分案例需要调梁北线共同参与 |
| `存2 + 叉线` | `预修线/存2叉` | 现场确认 `叉线` 就是预修库/存2叉；备注车号也落在 `预修线`，例如 `0115Z/0204Z/0303Z` |
| `存2 - 叉线` | `预修线/存2叉` | 现场确认目的语义是预修库/存2叉；身份回放仍保留端别和车序不确定 |
| `修N` | `修N库外|修N库内` | `修N +7` 的首尾车号跨库外和库内，例如 `0204Z`、`0306W`、`0327W` |
| `注意修N` | `修N库外|修N库内` | `注意修2 +6` 经状态回放可进入修库组 scope |

B1 已验证的非线路规则：

| 规则 | 状态 | 算法含义 |
|---|---|---|
| `库外N` | 已验证 | `effective_count=N`，但保留原始 `count_omitted` 便于审计 |
| 多车号备注 | 已验证 | 备注中的所有车号必须属于同一移动批次，只匹配首/尾车号不够 |
| `存5线北 + 北头` | 已验证 | 取 `position_low` 端；不能扩展到 `存2/存3/机/调` |
| `库/机 +1` 空备注 | 现场确认 | 机车动作，不计货车身份 delta |
| `称` | 现场确认 | 称重/定位，不计货车身份 delta |
| `存4 - 北头` 且无数量 | 现场确认 | 提醒/口位信号，不计货车身份 delta；`北头代N` 仍是复合 gap |
| `接` | 现场确认 | 接风管，不是车辆身份移动 |
| `存2 +/- 叉线` | 现场确认 | 业务线均收敛到 `预修线/存2叉`；`-` 的端别和车序仍需后续车号验证 |
| `0103W` | 现场确认 | 假日合并钩计划，不作为算法学习参考分母 |
| `存5 北头顶N` | 现场确认但未自动拆解 | 北端顶着 N 辆往南移动；可抽象为存5内部复合位移，但是否进入存5南无法推断 |
| 机走线 | 现场确认但未自动拆解 | 人工不拆机走北/南，算法需要临停容量与终点容量两套模型 |

B1 首个 blocker 分布：

| blocker | cases | 判断 |
|---|---:|---|
| 身份冲突 | 42 | 多为车号备注与当前身份状态不一致，不能进入训练；`0103W` 已从参考分母剔除 |
| 状态空间超限 | 34 | 主要剩余为缺车号端别组合、机/调空备注、预修线端别和车序不确定 |
| 合并钩/对位/代/顶 | 26 | `存5北头顶N` 已明确为复合位移，不再当普通 `+/-` |
| 省略辆数 | 3 | 主要是空备注 `调/机 -`，仍不能推断 |
| truth missing | 9 | 不能做身份重放 |

因此 B1 的结论是：结构层已经可以指导 episode 方向，但身份层仍不能全量指导 owner/blocker。下一步不应追求提高 replay rate，而应优先把 `存5北头顶N`、预修线端别/车序、`机走线未分段` 和 `北头代N` 做成显式结构模型；车号补全可显著降低状态空间。

本轮新增结构化不确定性产物：

```bash
rtk python3 scripts/analyze_manual_uncertainty_classes.py --root . --output-dir artifacts/manual_uncertainty_validation_20260702
```

产物：

- `manual_uncertainty_class_assignments.csv`
- `manual_uncertainty_class_summary.csv`
- `manual_uncertainty_evidence_examples.csv`
- `manual_uncertainty_validation_summary.json`

结构化不确定性分级：

| class | 数量 | 置信度 | 当前结论 | 算法动作 |
|---|---:|---|---|---|
| `forkline_plus` | 186 | high | `存2 + 叉线` 来源为 `预修线/存2叉` | 候选来源收敛到预修线 |
| `forkline_minus` | 209 | medium_high | `存2 - 叉线` 目的线为 `预修线/存2叉`，端别/车序未闭合 | 目的线收敛到预修线，但保留端别不确定 |
| `kuwai_count` | 47 | high | `库外N` 可解析辆数 | 使用 `effective_count=N` |
| `machine_loco_noop` | 114 | high | 机车/称重/回停不产生货车身份 delta | 排除出 freight identity 分母 |
| `storage4_north_blank` | 17 | high | `存4 - 北头` 空辆数是提醒/口位信号 | 只作为阶段/资源信号 |
| `storage5_north_endpoint` | 366 | high | `存5 北头` 是端别提示 | `存5线北 + 北头` 取 low 端 |
| `storage5_push_reposition` | 27 | high structure / low endpoint | `存5 北头顶N` 是内部复合位移 | 建 bounded 存5重排 episode，不强推存5南 |
| `machine_corridor_south` | 86 | medium | 人工 `机/南` 是机走线聚合，不精确分段 | 建机走线聚合容量模型 |
| `north_head_substitute` | 27 | low | `北头代N` 未解 | 保留结构 gap |
| `substitute_note` | 25 | low | `代N/南代N/机代N` 未解 | 保留结构 gap，等待现场字典 |

身份状态重放必须按人工计划执行，不能调用 solver 搜索。每一步只允许使用当前人工动作的 `resolved_line/method/count` 和当前状态：

1. 从 truth2 初始车辆、位置和调车机位置构建线路有序车列。
2. `+` 从指定线路按可达端取车，生成 `moving_nos` 和应标末辆车号。
3. `-` 从调车机后车列摘放到指定线路，生成被摘批次和 7 辆及以上应标首辆车号。
4. 如果人工计划已有 7 位车号，用它反校准线路端别和车列方向；不匹配则输出 `identity_replay_conflict`。
5. 状态遇到 `库/注意库`、省略辆数、合并钩时默认暂停；只有现场确认的机车/称重 no-op、`库外N` 和已验证备注型复合钩可以继续。
6. 只有 identity replay 连续通过的动作，才进入车号级 recall 和 owner/blocker 学习。

清洗标准：

| 等级 | 含义 | 后续用途 |
|---|---|---|
| `clean` | 线路、方法、辆数、基础聚合校验、必填车号和可确定风管标注均无问题 | 可进入 manual replay 和 candidate recall |
| `review` | 存在省略、合并、端别不明、车号缺失或状态验证缺口 | 只能用于人工复盘和规则补强，不能算 recall 分母 |
| `invalid` | 表头、案号、重复钩号等硬错误 | 不进入学习闭环 |

省略勾和合并勾处理原则：

- 省略钩不补猜；只记录 `sequence_gap_possible_omitted_hook` 或 `count_omitted`。
- 合并钩不拆猜；只记录 `merged_hook_possible`，等待现场确认或专门规则拆解。已确认的 `存5北头顶N` 仍保留为复合结构 gap，不降级成普通单钩。
- `回/停/顶/称/代/对N/接代/叉线接/叉线摘` 都是结构信号，不直接当作普通辆数或普通动作。
- `叉线` 已现场确认为 `预修线/存2叉`，不是普通道岔备注；`存2 +/- 叉线` 的业务线可收敛到 `预修线`，但 `存2 - 叉线` 的端别和精确车序仍需后续车号验证。
- `北头` 不能泛化成 H2 存4阶段；在 `存2叉` 语境下没有结构意义，在 `存5` 语境下只是端别提示。
- `存5 北头顶N` 不能按普通单钩学习；它是存5内部复合位移，南端是否为 `存5线南` 不能从人工单推出。
- 单独 7 位车号是必填注释，不再作为合并钩信号；只有和 `对/顶/代/叉线接摘` 等复合语义同现时才进入合并钩 review。
- `存5` 只有在备注端别或聚合状态能唯一确定时才进入 `inferred`；否则保留候选 `存5线北|存5线南`。
- `库/注意库` 虽然地图候选为 `机库线`，但业务语义不稳定，仍保留 `ambiguous`，不能自动进入 exact replay。
- 风管状态采用匿名车列保守状态机：可确定达到 10 辆时回填起始挂车钩的 `接`，只有整列摘清时才判定 `摘` 必填；部分摘解不猜方向。
- review 标签可以参与统计人工习惯，但不能作为 exact / structural recall 的硬分母。

清洗和 B1 回放入口已做确定性复跑：`artifacts/manual_label_cleaning_20260702` / `artifacts/manual_identity_replay_20260702` 与各自 repeat 目录的 `diff -rq` 均无差异。

每条人工动作至少包含：

| 字段 | 含义 |
|---|---|
| `case_id` | 案例 |
| `manual_hook` | 人工钩序 |
| `shift` | 班次，区分晚上连续连挂和中午叉车协助 |
| `line_raw` / `line` / `resolved_line` | 原始股道、运行时归一化股道、地图清洗后的股道 |
| `line_candidates` | 端别或库内外候选 |
| `line_resolution_status` | exact / alias_exact / inferred / ambiguous / unknown |
| `effective_count` | 高置信推断后的辆数；例如 `库外N`，原始 `count` 不被覆盖 |
| `count_resolution_status` | exact / inferred / omitted / semantic / not_applicable |
| `phase_label` | H1/H2/H3/H4/H5 |
| `family` | 对应 ContractFamily |
| `intent` | 动作意图 |
| `owner_nos` | 本动作真正服务的车；当前原始清洗产物中为空，必须由 B6 生成 |
| `blocker_nos` | 被带走/让路/恢复的车；当前原始清洗产物中为空，必须由 B6 生成 |
| `temporary_lines` | 临时使用线路 |
| `resource_owner` | 存4、修库槽位、串门等 owner |
| `expected_delta` | 预期减少的合同债务 |
| `manual_rationale` | 人工为什么这么做 |
| `structural_label_quality` | 忽略车号缺失后的结构质量，用于算法结构开发 |
| `annotation_label_quality` | 必填注释质量，用于车号级准确对照 |
| `manual_reference_scope` | 是否可作为算法学习参考；如 `0103W` 被现场确认为假日合并钩计划 |
| `site_confirmed_alias` | 现场确认别名，如 `存2叉=预修线` |
| `north_head_semantics` | `北头` 的语义边界，如端别提示或无结构意义 |
| `compound_identity_replay_mode` | 复合钩是否可按普通身份移动回放 |
| `compound_algorithm_hint` | 复合钩给算法的结构提示，如存5内部位移 |
| `car_no_annotation_*` | 车号必填、是否存在、规则类型、实际车号 |
| `air_hose_*` | 接/摘风管必填、是否存在、追溯起始钩和状态可信度 |
| `continuous_positioning_required` | 大库内摘车连续对位要求 |
| `continuous_coupling_required` | 晚上大库连续连挂要求 |
| `forklift_assist_expected` | 中午大库挂车叉车协助预期 |
| `yard_positioning_required` | 调梁/洗罐/油漆/抛丸对位要求 |
| `route_passby_path` / `route_switch_nodes` | 按地图拓扑得到的经路和联络/渡线节点 |

### 标签分类

人工动作先不要细分到代码模板，先标成结构语义：

| 标签 | 说明 |
|---|---|
| `front_service_direct` | 前场服务直达 |
| `front_shape_before_remote` | 远端前的前场塑形 |
| `cun4_port_forming` | 存4释放口成形 |
| `cun4_release_accept` | 存4释放承接 |
| `remote_session_digest` | 远端连续消化 |
| `depot_outbound_release` | 修库/库外出库释放 |
| `depot_inbound_digest` | 入库多目标消化 |
| `owner_prefix_access` | owner 内前缀让路 |
| `owner_serial_gate` | owner 内串门处理 |
| `depot_slot_swap` | 修库槽位冲突 |
| `spotting_repack` | 定置线重排 |
| `tail_closeout` | 尾项收束 |

### 验收

| 指标 | 第一目标 | 最终目标 |
|---|---:|---:|
| manual phase label coverage | >= 90% | >= 98% |
| manual owner label coverage | >= 85% | >= 95% |
| blocker label coverage | >= 80% | >= 95% |
| unknown manual intent rate | <= 10% | <= 2% |

### 边界

本阶段不改 solver。任何“人工动作看不懂”的情况进入标签 gap，不允许直接补代码。

本阶段的核心不是给每一钩套代码模板，而是识别人工动作背后的 owner、blocker、阶段承诺和资源边界。如果标签只能回答“人工从 A 到 B 动了几辆车”，还不能进入阶段 C。

## 4.5 阶段 B6：Owner/Blocker 身份标签

### 为什么必须单独成阶段

当前 `manual_action_labels.csv` 已有 `owner_nos`、`blocker_nos` 字段，但实际值仍为空。不能在这种状态下进入 manual candidate recall；否则 exact / structural 等价只能按 source/target 粗比，无法判断候选是否服务同一个 owner，也无法发现把 blocker 当主收益的错误。

B6 的目标不是补全所有车号，而是只在 B1 identity replay 已连续通过的范围内，生成可审计的 owner/blocker 标签。

### 输入

| 输入 | 用途 |
|---|---|
| `manual_action_labels.csv` | 人工动作语义、质量、现场别名和复合钩边界 |
| `manual_identity_replay_trace.csv` | replay 通过动作的 `moved_nos_if_unique`、候选状态、车号校验 |
| `manual_uncertainty_class_summary.csv` | 哪些类别可用于 owner/blocker，哪些必须保留 gap |
| truth2 合同目标 | 判断移动批次中哪些是 owner 债务车 |

### 输出

建议新增：

- `manual_owner_blocker_labels.csv`
- `manual_owner_blocker_case_summary.csv`
- `manual_owner_blocker_gap_records.csv`

字段：

| 字段 | 含义 |
|---|---|
| `case_id` / `manual_hook` | 人工动作定位 |
| `owner_nos` | 本钩真正服务的债务车 |
| `blocker_nos` | 为接近 owner 被带出、让路、恢复或临停的车 |
| `support_nos` | 伴随移动但不应计为主收益的车 |
| `owner_source` | truth2 target / car-no note / replay unique / field-confirmed class |
| `blocker_source` | prefix relation / serial gate / temporary line / uncertainty class |
| `label_status` | clean / partial / gap |
| `gap_reason` | no_unique_replay / no_contract_owner / compound_unresolved / car_no_conflict |

### 覆盖边界

第一轮只允许覆盖：

- `replayed_unique` 且非 no-op 的动作。
- 多车号备注全部匹配移动批次的动作。
- `forkline_plus/forkline_minus`、`kuwai_count`、`storage5_north_endpoint` 等高/中高置信结构。
- `storage5_push_reposition` 只输出结构 intent，不输出 exact owner/blocker 车号。

不允许覆盖：

- `identity_conflict`。
- `state_space_exceeded`。
- `structural_gap`。
- `north_head_substitute`、`substitute_note` 低置信类别。
- `0103W` 等 `exclude_from_algorithm_learning` 样本。

### 验收

| 指标 | 第一轮目标 | 最终目标 |
|---|---:|---:|
| replay-unique owner coverage | >= 70% | >= 95% |
| replay-unique blocker coverage | >= 50% | >= 90% |
| owner/blocker conflict rate | 0 | 0 |
| low-confidence class forced labels | 0 | 0 |
| owner label determinism | repeat diff 无差异 | repeat diff 无差异 |

### 回滚条件

- 为了提高覆盖率给 `state_space_exceeded` 或 `identity_conflict` 生成 owner/blocker。
- 把 `support_nos` 计入主合同收益。
- 把 `存5北头顶N` 或 `代N` 直接拆成普通单钩 owner/blocker。

## 5. 阶段 C：Manual Candidate Recall

### 目标

在人工每一步对应的 solver 状态上，检查当前 episode 是否能生成等价结构候选。

这一步只问“生不生得出”，不问“会不会选中”。

这里的“solver 状态”不是 solver 当前 policy rollout 后的状态，而是人工计划执行到该钩之前的 replay 状态。普通 `step_trace.csv` 只能说明 solver 自己走到哪里，不能证明人工动作候选是否可召回。

进入阶段 C 的前置条件：

- B1 identity replay、结构化不确定性报告已确定性复跑。
- B6 `manual_owner_blocker_labels.csv` 已生成。
- 至少 `replayed_unique` 的 owner/blocker 标签达到第一轮覆盖目标。
- 低置信不确定项没有被强行标注。

### 核心产物

建议新增：

- `manual_candidate_recall.csv`
- `manual_candidate_gap_records.csv`
- `manual_equivalent_candidate_examples.csv`

字段：

| 字段 | 含义 |
|---|---|
| `case_id` | 案例 |
| `manual_hook` | 人工钩序 |
| `phase_label` | 人工阶段 |
| `manual_intent` | 人工结构意图 |
| `owner_nos` / `blocker_nos` | 来自 B6，不允许在 recall 阶段临时推断 |
| `candidate_generated` | 是否生成 |
| `candidate_template` | 生成模板 |
| `equivalent_level` | exact / structural / none |
| `physical_pass` | 是否物理可行 |
| `resource_pass` | 是否资源可行 |
| `contract_reduction` | 合同收益 |
| `gap_reason` | 未召回原因 |
| `state_source` | manual_replay，禁止使用 solver_rollout |
| `rejected_stage` | none / physical / resource / contract / phase |
| `reject_reason` | 被拒绝时的原始原因 |

### 等价定义

| 等价级别 | 定义 |
|---|---|
| exact | source、target、owner、blocker、临时线基本一致 |
| structural | owner 和资源意图一致，但批量或临时线不同 |
| none | 没有候选表达该人工结构 |

`structural` 不能只看 source/target 相同，必须同时满足 owner 语义一致、资源边界一致、没有把 blocker 当成主合同收益。

### 验收

| 结构 | 最低召回 | 目标召回 |
|---|---:|---:|
| H1 front service | >= 80% | >= 95% |
| owner prefix access | >= 50% | >= 95% |
| owner serial gate | >= 50% | >= 95% |
| CUN4 release accept | >= 80% | >= 95% |
| H4 remote/depot session | >= 75% | >= 95% |
| depot slot conflict | >= 60% | >= 90% |
| spotting repack | >= 60% | >= 90% |

### 边界

本阶段允许新增诊断脚本，不改 episode 和 policy。若召回低，先定位 gap，不立即补模板。

如果召回低，先输出 `manual_candidate_gap_records.csv`，按 `label_gap`、`no_applicable_episode`、`prefilter_silent`、`physical_reject`、`resource_reject`、`contract_delta_mismatch`、`phase_veto` 分类。只有 gap 类型稳定后，才允许进入阶段 D。

## 6. 阶段 D：修 owner-bound 源前缀/串门结构

### 为什么先修

当前最大 gap 是：

- `source_prefix_blocker_requires_lease`: 82
- `serial_blocker_source_open`: 31

这类问题对应人工计划中的核心哲学：挡车不是独立任务，挡车处理必须归属于消费下游债务的 owner。

本阶段只能在阶段 B/C 完成后开始。否则 `source_prefix_blocker_requires_lease` 只是 solver gap 名称，不能证明人工在这些状态下确实采用 owner-bound prefix access。

### 结构边界

本结构负责：

- 目标车在源线前缀内部，被非 owner 车挡住。
- 需要带出 blocker、送达 owner、必要时恢复 blocker。
- 串门阻挡必须在 owner 计划内被消化或证明不污染。

本结构不负责：

- 全局清空某条串门线。
- 为未来未知债务提前腾线。
- 远端直达兜底。
- H5 收束整理。

### 可能的 episode 形态

建议不是恢复旧独立清门，而是新增或改造 owner episode：

```text
OwnerPrefixAccessDigestEpisode
  Get source prefix
  Put owner target group
  Put blocker back to source or legal temporary line
  Verify no serial pollution
```

或在现有相关 episode 内形成同一结构，但必须满足：

- template 名称表达 owner-bound 语义。
- `same_plan_source_return_nos` 必须准确。
- blocker 不能被当成主合同收益。
- `contract_reduction > 0` 必须来自 owner。

### 诊断

看这些文件：

- `generation_gap_records.csv`
- `structure_node_metrics.csv`
- `resource_structure_records.csv`
- `step_trace.csv`

核心查询：

```bash
rtk python3 scripts/score_vnext_structures.py --artifact-dir artifacts/phaseD_owner_prefix
rtk python3 scripts/audit_vnext_validation_sequence.py --artifact-dir artifacts/phaseD_owner_prefix
```

### 验收

| 指标 | 通过标准 |
|---|---:|
| `source_prefix_blocker_requires_lease` | 明显下降，第一轮目标 < 40 |
| `serial_blocker_source_open` | 不上升 |
| serial resource failure | 0 |
| hard physical accepted | 0 |
| owner contract reduction | > 0 |
| support-only selected | 0 |
| independent serial clear template | 0 |

### 回滚条件

- 出现独立清门或全局清线动作。
- `contract_reduction = 0` 但候选被选中。
- serial pollution failure 增加。

## 7. 阶段 E：拆解 reachability gap

### 目标

把 `source_not_reachable` 和 `target_not_reachable` 从粗粒度失败，拆成可修的物理/路径/候选顺序原因。

当前 gap：

- `target_not_reachable`: 51
- `source_not_reachable`: 40

### 边界

本阶段只做诊断和候选步骤顺序修复，不放松物理图。

### 诊断分类

| 类别 | 含义 |
|---|---|
| `loco_position_wrong` | 机车位置导致不可达 |
| `route_occupied_by_stationary` | 路径被静止车占用 |
| `source_end_blocked` | 源线端别不可取 |
| `target_approach_blocked` | 目标进路受阻 |
| `candidate_step_order_wrong` | 候选先后顺序不对 |
| `graph_missing_edge` | 线路图缺边 |
| `manual_uses_intermediate` | 人工用了中间线，当前候选没有 |

### 验收

| 指标 | 通过标准 |
|---|---:|
| unknown reachability gap | 0 |
| graph missing edge | 必须逐条人工核验 |
| hard physical accepted | 0 |
| source/target not reachable | 每轮下降或转化为明确结构 gap |

## 8. 阶段 F：清理 generated zero after prefilter

### 目标

解释并修复 `episode_generated_zero_after_prefilter`。

当前 gap：44。

### 边界

prefilter 只能表达结构边界，不能表达“我暂时不会处理”。如果条件不是硬结构边界，就要改成可诊断 reason 或删除。

### 每个 prefilter 必须回答

| 问题 | 要求 |
|---|---|
| 过滤原因是什么 | 输出明确 reason |
| 是硬边界还是策略偏好 | 必须区分 |
| 人工是否有对应合法动作 | 如果有，不能硬过滤 |
| 是否应该交给资源层拒绝 | 资源问题不应在 episode 里静默吞掉 |

### 验收

| 指标 | 通过标准 |
|---|---:|
| `episode_generated_zero_after_prefilter` | 第一轮 < 20，最终 < 5 |
| silent return | 每个关键 episode 必须有 gap reason |
| generated/yield conversion | 核心 episode 可解释 |

## 9. 阶段 G：修 H2/H3 存4释放承接

### 目标

让存4口成形、释放、机接成为可学习结构，而不是零收益 assembly 加后续消化。

当前现象：

- `cun4_release_group_assembly` selected 74
- zero effect 74
- `cun4_release_accept_digest` selected 51
- H2 template shape 失败：H2 主要是 assembly 和少量 direct

### 结构边界

H2 负责：

- 存4释放口成形
- 方向保护
- 污染控制
- 形成可被 H3 接收的批次

H3 负责：

- 存4释放
- 机接/承接
- 进入 H4 远端 digest

H2 不负责修库消化，H3 不负责前场塑形。

### 诊断

比较人工计划：

- `manual_cun4_release_hook`
- `manual_machine_accept_hook`
- `manual_phase_signature`

看 solver：

- `phase_gate_records.csv`
- `step_trace.csv`
- `staging_intent_records.csv`

### 验收

| 指标 | 通过标准 |
|---|---:|
| target_H3 / manual H3 recall | >= 0.8，目标 >= 0.95 |
| actual_H3 / target_H3 | >= 0.8 |
| H2 direct selected | 下降 |
| CUN4 resource failure | 0 |
| zero-effect H2 selected | 必须有后续证明，否则不通过 |

## 10. 阶段 H：修 H4 远端/修库 session

### 目标

让 H4 不只是“能消化”，而是学习人工计划的远端连续性：少切换、成组、按库位和方向处理。

当前有效结构：

- `depot_outbound_session`
- `remote_session_prefix_batch_digest_restore`
- `depot_inbound_prefix_multidrop_session`
- `remote_session_directional_digest`

### 边界

H4 负责：

- 修库入库
- 修库出库
- 槽位冲突
- 远端连续 session
- 远端内前缀和尾部消化

H4 不负责：

- H1 前场服务
- H2 存4口成形
- H5 收束整理

### 诊断

| 指标 | 说明 |
|---|---|
| remote transition p50 | 远端连续性 |
| internal remote transitions | 远端内部碎裂 |
| session contract reduction mean | session 有效性 |
| low effect H4 steps | 碎勾 |
| depot slot violations | 修库槽位边界 |

### 验收

| 指标 | 通过标准 |
|---|---:|
| solver remote transition p50 | <= manual p50 + 1 |
| H4 low-effect ratio | < 10% |
| depot slot resource failure | 0 |
| direct remote selected | 0 |
| session mean reduction | 不下降 |

## 11. 阶段 I：修 H5 spotting 和 closeout

### 为什么最后修

当前 H5 分数不可信，因为大多数案例没有稳定完成 H3/H4。过早修 H5 会把前面缺口包装成收束问题。

### H5 边界

H5 负责：

- 定置线最终重排
- 尾项回库
- 机区/功能线剩余债务
- 全局剩余目标收束

H5 不负责：

- 修库主债务
- 存4释放
- owner 前缀清障

### 验收

| 指标 | 通过标准 |
|---|---:|
| `spotting_repack_required` | 逐步归零 |
| H5 selected before remote debt clear | 0 |
| closeout contract reduction | > 0 |
| final unsatisfied | 下降 |

## 12. 阶段 J：policy 学习与排序

### 进入条件

只有满足以下条件才开始调 policy：

- manual candidate recall 核心链路 >= 90%
- CandidateGenerationCoverage 不再为 0
- 主要 gap 都可解释
- 资源和物理 failure 为 0

### 学习对象

policy 不学“怎么生成动作”，只学：

- 多个合法候选中选哪个
- 何时继续当前 session
- 何时提前前场塑形
- 何时进入存4释放
- 何时结束远端 session
- 何时 closeout

### 训练/验证指标

| 指标 | 含义 |
|---|---|
| selected manual equivalent rate | 选中候选是否接近人工 |
| phase transition match | 阶段切换是否接近人工 |
| session continuity gain | 是否减少远端切换 |
| hook ratio | 钩数是否达到人工 |
| blocked rate | 不可牺牲可解性 |

### 验收

| 指标 | 通过标准 |
|---|---:|
| selected manual equivalent rate | >= 70%，目标 >= 85% |
| completed | 不下降 |
| remote transition p50 | 不高于上一阶段 |
| hook ratio p50 | <= 1.05，目标 <= 1.00 |

## 13. 阶段 K：超过人工

超过人工不是靠背人工计划，而是在已召回人工结构后做结构内优化。

### 可超越点

| 方向 | 说明 |
|---|---|
| 更稳定 batch | 同 owner 合并，减少碎勾 |
| 更强 session | 远端一次进入处理更多相关债务 |
| 更少空走 | 用机车位置和上一业务线减少切换 |
| 更优槽位顺序 | 修库 slot/swap 提前规划 |
| 更少重复重排 | spotting 和 closeout 合并处理 |

### 禁止超越方式

- 不允许违反人工硬规则。
- 不允许牺牲完成率换低钩数。
- 不允许用不可解释搜索替代结构。

### 最终验收

| 指标 | 达到人工 | 超越人工 |
|---|---:|---:|
| completed | >= 98% | 100% |
| hard violation | 0 | 0 |
| final warning | 0 | 0 |
| hook ratio p50 | <= 1.00 | < 1.00 |
| hook ratio p90 | <= 1.05 | <= 1.00 |
| remote transition p50 | <= manual + 1 | <= manual |
| unknown gap | 0 | 0 |

## 14. 每轮实施节奏

每一轮只做一个结构，但在进入任何结构修复前，必须先确认阶段 A0 的命令链可执行。不能依赖历史 artifact 证明当前代码。

阶段 A0 通过前，每轮只允许做诊断工具链和人工 replay 修复：

1. 恢复或新增缺失脚本。
2. 为新增脚本写出复杂度预算和确定性输入/输出。
3. 跑单测和全量 runtime。
4. 跑 scorecard、validation audit、manual compare。
5. 生成 manual action labels。
6. 生成 manual identity replay。
7. 生成 manual uncertainty validation。
8. 生成 owner/blocker labels。
9. 生成 manual candidate recall。
10. 做 determinism check：同输入连续两次摘要一致。
11. 只修诊断字段、解析字段、replay 状态和标签状态，不改 episode/policy。

阶段 A0 通过后，每一轮按以下顺序执行：

1. 固定 artifact 名称。
2. 跑当前基线。
3. 提取该结构的 manual recall gap cases。
4. 选 3-5 个代表案例做人工作业状态重放对齐。
5. 写出结构边界。
6. 写出本轮新增自由度和复杂度预算。
7. 修改最小必要模块。
8. 跑单测。
9. 跑全量 trace。
10. 跑 scorecard、manual compare、owner/blocker labels、manual candidate recall。
11. 做 determinism check 和 complexity budget check。
12. 只看本结构指标是否通过，不能用 completed 上升替代结构通过。
13. 更新文档。

标准命令：

```bash
rtk pytest -q tests/test_vnext_rule_boundaries.py
rtk python3 scripts/generate_vnext_runtime_trace.py --root . --output-dir artifacts/<stage_name> --max-hooks 300 --check
rtk python3 scripts/score_vnext_structures.py --artifact-dir artifacts/<stage_name>
rtk python3 scripts/audit_vnext_validation_sequence.py --artifact-dir artifacts/<stage_name>
rtk python3 scripts/compare_vnext_with_manual.py --artifact-dir artifacts/<stage_name>
rtk python3 scripts/generate_manual_action_labels.py --root . --output-dir artifacts/<stage_name>_manual_labels
rtk python3 scripts/replay_manual_identity_labels.py --root . --label-dir artifacts/<stage_name>_manual_labels --output-dir artifacts/<stage_name>_manual_identity --max-states 512
rtk python3 scripts/analyze_manual_uncertainty_classes.py --root . --label-dir artifacts/<stage_name>_manual_labels --replay-dir artifacts/<stage_name>_manual_identity --output-dir artifacts/<stage_name>_manual_uncertainty
rtk python3 scripts/label_manual_owner_blockers.py --root . --label-dir artifacts/<stage_name>_manual_labels --replay-dir artifacts/<stage_name>_manual_identity --uncertainty-dir artifacts/<stage_name>_manual_uncertainty --output-dir artifacts/<stage_name>_owner_blockers
rtk python3 scripts/audit_manual_candidate_recall.py --root . --label-dir artifacts/<stage_name>_manual_labels --replay-dir artifacts/<stage_name>_manual_identity --owner-blocker-dir artifacts/<stage_name>_owner_blockers --output-dir artifacts/<stage_name>_manual_recall
rtk python3 scripts/audit_vnext_complexity_budget.py --artifact-dir artifacts/<stage_name> --manual-recall-dir artifacts/<stage_name>_manual_recall
rtk python3 scripts/audit_vnext_determinism.py --root . --artifact-dir artifacts/<stage_name>
```

当前工作树若缺少上述任一入口，本轮不允许进入 episode 修复。当前已存在 `generate_manual_action_labels.py`、`replay_manual_identity_labels.py`、`analyze_manual_uncertainty_classes.py`；其余审计、owner/blocker 和 candidate recall 入口仍需补齐。

## 15. 失败定位优先级

遇到问题时按这个顺序定位，不能跳层：

1. 物理是否合法。
2. 资源是否污染。
3. 合同 owner 是否正确。
4. 人工标签是否正确。
5. 人工 replay 状态是否正确。
6. episode 是否适用。
7. episode 是否生成。
8. 候选是否被物理/资源/合同/阶段拒绝。
9. policy 是否选错。
10. 最终结果是否改善。

如果第 6 步之前没有解释清楚，不允许改第 9 步。

## 16. 当前立即下一步

当前最应该做的是：

```text
恢复当前工作树缺失的测试和审计脚本
-> 保持人工动作级标签、身份回放、不确定性报告确定性复跑
-> 新增 B6 owner/blocker 标签
-> 新增 C0 manual candidate recall
-> 做 recall gap 分类
-> 再修 owner-bound source prefix / serial gate
```

不是：

```text
调 policy
追 completed
追 hook ratio
恢复独立清门
增加远端直达兜底
用 solver step_trace 代替人工计划状态重放
跳过 manual recall 直接补 episode
```

第一轮目标先改为 A0 目标：

| 指标 | 当前 | 第一轮目标 |
|---|---:|---:|
| `tests/test_vnext_rule_boundaries.py` | 缺失 | 可执行且通过 |
| `score_vnext_structures.py` | 缺失 | 可执行 |
| `audit_vnext_validation_sequence.py` | 缺失 | 可执行 |
| `compare_vnext_with_manual.py` | 缺失 | 可执行 |
| manual action labels | 已有 | 保持确定性复跑 |
| manual identity replay | 已有 | 保持确定性复跑 |
| manual uncertainty validation | 已有 | 保持确定性复跑 |
| manual owner/blocker labels | 缺失 | 产出 CSV |
| manual candidate recall | 缺失 | 产出 CSV |
| complexity budget records | 无 | 产出 CSV |
| determinism check records | 无 | 连续两次摘要一致 |
| hard physical accepted | 0 | 0 |

A0 通过后，owner-bound 第一轮目标：

| 指标 | 当前 | 第一轮目标 |
|---|---:|---:|
| `source_prefix_blocker_requires_lease` | 82 | < 40 |
| `serial_blocker_source_open` | 31 | 不上升 |
| hard physical accepted | 0 | 0 |
| resource failure | 0 | 0 |
| independent clear template | 0 | 0 |

做到这一步，系统才开始真正具备学习人工计划中“owner 负责、先塑形、再消化、保护主结构”的能力。
