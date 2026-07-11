# 四阶段并行 API 部署与调用

该服务统一编排：

1. `stage1_simple`：第一阶段编组与服务收尾；
2. `stage2_simple`：卸轮翻库与大库出库编组；
3. `stage3_simple`：大库入库；
4. `stage4_simple`：剩余目标闭合。

同一个案例的四个阶段存在严格数据依赖，因此按 `Stage1 → Stage2 → Stage3 → Stage4` 串行执行。并行发生在不同案例的完整流水线之间；每个 job 在独立、受监管的子进程中运行，既隔离 CPU 计算和模块级缓存，也能在总预算超时时单独终止。

## Windows Release 自动打包

发布 GitHub Release（状态从 draft 变为 published）后，[release-windows-server.yml](../.github/workflows/release-windows-server.yml) 会在 Windows Server 2022 runner 上自动执行：

1. 安装锁定的 Python 3.12 API 与 PyInstaller 依赖；
2. 运行 Windows API 回归测试；
3. 构建 `onedir` 服务并启动冻结后的 EXE；
4. 发送一个真实四阶段请求，验证 EXE 能以内部 `--worker` 模式并行拉起求解子进程；
5. 使用 7-Zip 最高压缩级别生成 ZIP，并把 ZIP 和 SHA256 上传到当前 Release。

Release 资产名固定为：

```text
train-cal-four-stage-api-windows-x64.zip
train-cal-four-stage-api-windows-x64.zip.sha256
```

压缩包设置了 30 MiB 硬上限。构建只收集 API、四阶段求解器和必要运行库，不包含 `data/`、`artifacts/`、测试、文档或 Streamlit。使用时完整解压 ZIP，把 `server.env.example.cmd` 复制为 `server.env.cmd` 并设置私有 `TRAIN_CAL_API_KEY`，然后运行 `start-server.cmd`；不需要另装 Python。

Release 对应的 tag 必须已经包含 workflow 和打包文件。如果 Release 是由另一个 workflow 使用默认 `GITHUB_TOKEN` 创建的，GitHub 不会再次触发 `release` 事件；这种发布链需要改用 GitHub App/PAT，或在创建 Release 的同一 workflow 中调用本构建脚本。

## 源码启动

```bash
cd /root/train_cal2
# 当前项目环境使用 Python 3.12；若没有 .venv：python3 -m venv .venv
.venv/bin/pip install -r requirements-api.txt

# Uvicorn 只启动一个 Web worker；求解并发由 TRAIN_CAL_API_WORKERS 控制。
TRAIN_CAL_API_WORKERS=2 \
TRAIN_CAL_API_MAX_PENDING=8 \
TRAIN_CAL_API_KEY='replace-with-a-secret' \
.venv/bin/uvicorn plan_api.server:app \
  --host 0.0.0.0 \
  --port 8000 \
  --workers 1
```

当前机器是 4 vCPU、内存较小。普通案例可以从 `TRAIN_CAL_API_WORKERS=1` 开始压测；确认内存余量后再调为 2。服务通过 `JOB_ROOT` 排他锁强制单实例运行，因此 Uvicorn 必须保持 `--workers 1`，同一 `JOB_ROOT` 也不能同时启动第二个服务实例。

可用环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TRAIN_CAL_API_WORKERS` | `min(2, CPU/2)` | 同时运行的独立 job 子进程数。 |
| `TRAIN_CAL_API_MAX_PENDING` | `workers * 4` | 正在校验、排队中和运行中的任务总上限；校验前会原子预留容量。 |
| `TRAIN_CAL_API_JOB_ROOT` | `artifacts/api_jobs` | 任务、阶段产物和日志目录。 |
| `TRAIN_CAL_API_KEY` | 空 | 对外启动时必填；支持 Bearer / `X-API-Key` 鉴权。 |
| `TRAIN_CAL_ALLOW_UNAUTHENTICATED` | `false` | 仅本地开发可显式设为 `true` 以关闭鉴权。 |
| `TRAIN_CAL_CORS_ORIGINS` | 空 | 逗号分隔的允许跨域来源。 |
| `TRAIN_CAL_API_MAX_BODY_BYTES` | `10485760` | 单请求体大小上限。 |
| `TRAIN_CAL_API_MAX_BATCH_CASES` | `100` | 单次批量提交案例上限。 |
| `TRAIN_CAL_API_JOB_TIMEOUT_GRACE_SECONDS` | `120` | 四阶段预算合计之外允许的进程启动/收尾宽限时间；超时后强制终止该 job 子进程。 |
| `TRAIN_CAL_API_JOB_TERMINATE_GRACE_SECONDS` | `5` | 超时或服务关闭时，SIGTERM 到 SIGKILL 之间的等待时间。 |
| `TRAIN_CAL_API_JOB_TTL_HOURS` | `168` | 已结束任务的自动保留时间；设为 `0` 可关闭清理。 |
| `TRAIN_CAL_API_CLEANUP_INTERVAL_SECONDS` | `3600` | 过期任务清理周期。 |
| `TRAIN_CAL_API_MIN_FREE_DISK_MB` | `512` | `/readyz` 要求的最小可用磁盘空间。 |

健康检查：

```bash
curl http://127.0.0.1:8000/healthz
curl http://127.0.0.1:8000/readyz
```

OpenAPI 描述：

```text
GET /api/plan/openapi.json
```

## 单案同步调用

`POST /api/plan/generate` 保持原有业务请求体契约。`case_id` 可通过查询参数或 `X-Case-Id` 传递：

```bash
curl -X POST 'http://127.0.0.1:8000/api/plan/generate?case_id=0202Z' \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer replace-with-a-secret' \
  --data-binary @data/truth2/validation_取送车计划_20260202Z.json
```

同步响应仍为原契约：

```json
{
  "Success": true,
  "Message": "",
  "StatusCode": 200,
  "Data": {
    "Operations": [],
    "GeneratedEndStatus": []
  }
}
```

求解返回 `partial` 是正常业务结果，HTTP 仍返回 200，但 `Success=false`，`Message` 会给出首个未闭合阶段和阻塞原因。输入错误返回 4xx，服务或回放门禁错误返回 500。

四阶段默认预算累计可达 1080 秒。跨网关部署时不建议让同步连接等待这么久，生产调用优先使用异步模式。

## 单案异步调用

在查询参数增加 `async=true`，或发送 `Prefer: respond-async`：

```bash
curl -X POST 'http://127.0.0.1:8000/api/plan/generate?async=true&case_id=0202Z' \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer replace-with-a-secret' \
  --data-binary @data/truth2/validation_取送车计划_20260202Z.json
```

服务返回 202：

```json
{
  "Success": true,
  "Message": "任务已提交",
  "StatusCode": 202,
  "Data": {
    "JobId": "...",
    "CaseId": "0202Z",
    "Status": "queued",
    "StatusUrl": "/api/plan/jobs/...",
    "ResultUrl": "/api/plan/jobs/.../result"
  }
}
```

轮询状态和结果：

```bash
curl -H 'Authorization: Bearer replace-with-a-secret' \
  http://127.0.0.1:8000/api/plan/jobs/<job_id>

curl -H 'Authorization: Bearer replace-with-a-secret' \
  http://127.0.0.1:8000/api/plan/jobs/<job_id>/result
```

结果接口除标准 `Success/Message/StatusCode/Data` 外，还增加 `Meta`：

- `OperationCount`：全部 `Get/Put/Weigh` 操作行数；
- `GetPutHookCount`：现场挂/摘业务口径，只统计 `Get/Put`；
- `WeighOperationCount`：单独统计 `Weigh`；
- `CompletedStage`：业务状态为 `complete` 的最后阶段；
- `LastSafeStage`：通过累计回放门禁的最后阶段，partial 时可能大于 `CompletedStage`；
- 各阶段 summary 与 replay gate。

## 带求解参数的单案提交

需要调整安全公开参数时，可使用 envelope：

```json
{
  "case_id": "0202Z",
  "request": {
    "StartStatus": [],
    "TerminalLines": [],
    "locoNode": {"Line": "机库线", "End": "North"}
  },
  "options": {
    "stage1": {
      "profile": "balanced",
      "max_hooks": 80,
      "time_budget_seconds": 300
    },
    "stage2": {"time_budget_seconds": 300},
    "stage3": {"time_budget_seconds": 180},
    "stage4": {
      "time_budget_seconds": 300,
      "max_macros": 160,
      "max_candidates_per_step": 96
    }
  }
}
```

`include-stage-partial`、`allow-depot-in-buffer`、`accept-upper-bound` 等实验开关没有暴露给外部调用。首个上游阶段为 partial/error 或回放不通过时，流水线立即停止，不会让下游在不安全状态上继续运行。

公开参数还有服务端硬上限：单阶段预算最多 900 秒、四阶段合计最多 1800 秒，Stage1 `max_hooks` 最多 500，Stage4 `max_macros` 最多 500、每步候选最多 256。

## 批量并行提交

`POST /api/plan/generate/batch` 一次提交多个案例，服务按照 `TRAIN_CAL_API_WORKERS` 并行运行：

```bash
jq -n \
  --slurpfile first data/truth2/validation_取送车计划_20260202Z.json \
  --slurpfile second data/truth2/validation_取送车计划_20260203W.json \
  '{
    options: {stage1: {time_budget_seconds: 300}},
    cases: [
      {case_id: "0202Z", request: $first[0]},
      {case_id: "0203W", request: $second[0]}
    ]
  }' \
| curl -X POST http://127.0.0.1:8000/api/plan/generate/batch \
    -H 'Content-Type: application/json' \
    -H 'Authorization: Bearer replace-with-a-secret' \
    --data-binary @-
```

全部任务成功入队时批量接口返回 202 和每个案例独立的 `JobId`。参数错误、未鉴权、队列已满分别可能返回 422、401、429；极少数执行器在批量中途失败时返回 207，并在 `AcceptedJobs` 中明确列出已入队任务。相同 `case_id` 也不会串数据，因为真实存储主键是 UUID job id，每个任务独享：

```text
artifacts/api_jobs/<job_id>/
  input/
  stage1/
  stage2/
  stage3/
  stage4/
  logs/
  job.json
  result.json
```

## 结果安全门禁

每阶段结束后，API 会从原始请求回放当前累计 response：

- 回放通过且阶段 `complete`：进入下一阶段；
- 回放通过但阶段 `partial`：停止并返回最新安全的部分计划；
- schema/physical/business/state 任一回放违规或阶段异常：任务标记为 failed，并回退返回上一个通过门禁的累计计划。

最终对外返回的是 Stage4 `combined_response`，即相对于原始请求的四阶段完整 Operations；不会误返回只包含第四阶段增量的 `response`。`GeneratedEndStatus` 由最终累计 Operations 统一回放生成。

## 生产运行限制

- API 任务状态和产物会落盘，但服务重启时不会自动续跑 queued/running 任务；没有完整结果的任务会标记为 `interrupted`。调用方需要重新提交。
- 每个 job 的硬 wall-clock 上限为四阶段预算合计加 `TRAIN_CAL_API_JOB_TIMEOUT_GRACE_SECONDS`；超时后先 SIGTERM，宽限期后仍未退出则 SIGKILL。
- 服务关闭时会把未完成任务标记为 interrupted，并终止求解子进程。
- 输入、trace、日志和结果包含业务数据，目录与文件默认按私有权限创建，并按 TTL 自动清理；服务关闭会等待已开始的清理结束后再释放实例锁。仍应使用专用低权限账户、独立磁盘配额和备份/审计策略。
- 对外必须放在 HTTPS 反向代理后；代理层也要设置请求体上限。API 返回相对 `StatusUrl/ResultUrl`，客户端应基于当前服务地址解析。
- `/healthz` 是存活检查；`/readyz` 会检查任务执行器状态和最小磁盘余量，不满足时返回 503。
- 同步接口用于兼容原契约；生产推荐异步模式。同步连接断开后求解任务仍可能继续，因此客户端不要把同步重试当成幂等操作。
