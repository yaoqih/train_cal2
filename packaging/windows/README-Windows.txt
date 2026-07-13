福州东调车计划四阶段 API
Windows x64 打包版使用说明
========================================

一、适用环境
------------

- Windows Server 2019 或更高版本，或 Windows 10/11 x64。
- 本程序是便携式服务端程序，不需要另外安装 Python。
- 程序提供 HTTP API，不包含桌面操作界面。


二、解压程序
------------

1. 将 train-cal-four-stage-api-windows-x64.zip 完整解压到一个当前用户有写权限的
   固定目录，例如：D:\train-cal-server。不建议解压到 Program Files。
2. 不要直接在 ZIP 压缩包中运行程序。
3. 不要单独移动 train-cal-server.exe；它必须和 _internal 目录放在一起。

解压后的主要文件如下：

  train-cal-server.exe       API 服务主程序
  _internal\                 程序运行依赖，不能删除
  start-server.cmd           推荐的启动入口
  server.env.example.cmd     配置模板
  README-Windows.txt         本使用说明
  VERSION.txt                当前版本号


三、直接启动与可选配置
----------------------

Windows 打包版默认启用无鉴权访问，不需要配置 API Key，也不需要先创建配置文件。
直接运行 start-server.cmd 即可。

如果需要修改端口、并行数或任务目录，可以在解压目录中打开命令提示符，执行：

  copy server.env.example.cmd server.env.cmd
  notepad server.env.cmd

默认配置内容如下：

  @echo off
  set "TRAIN_CAL_ALLOW_UNAUTHENTICATED=true"
  set "TRAIN_CAL_API_WORKERS=1"
  set "TRAIN_CAL_API_MAX_PENDING=4"
  set "TRAIN_CAL_API_PORT=8000"

常用配置项：

  TRAIN_CAL_ALLOW_UNAUTHENTICATED  true 表示允许直接访问 API
  TRAIN_CAL_API_WORKERS            同时求解的任务数，默认并推荐从 1 开始
  TRAIN_CAL_API_MAX_PENDING        运行中和排队中的任务总上限，默认 4
  TRAIN_CAL_API_PORT               服务端口，默认 8000
  TRAIN_CAL_API_HOST               监听地址；默认 0.0.0.0，仅本机使用可设为 127.0.0.1
  TRAIN_CAL_API_JOB_ROOT           任务和结果保存目录，默认 artifacts\api_jobs

修改 server.env.cmd 后需要重启服务才能生效。


四、启动、检查和停止
--------------------

启动方式一：双击 start-server.cmd。

启动方式二：在命令提示符中进入解压目录后执行：

  start-server.cmd

看到类似下面的内容表示服务已启动：

  Application startup complete.
  Uvicorn running on http://0.0.0.0:8000

在服务器本机浏览器中访问以下地址进行检查：

  http://127.0.0.1:8000/healthz
  http://127.0.0.1:8000/readyz
  http://127.0.0.1:8000/api/plan/openapi.json

- healthz 返回 status=ok，表示服务进程正常。
- readyz 返回 status=ready，表示服务可以接收任务。
- 0.0.0.0 是监听地址，不是客户端访问地址。本机调用请使用 127.0.0.1；
  其他电脑调用时请使用服务器的实际 IP 地址。

停止服务：切换到服务运行窗口，按 Ctrl+C，等待程序完成关闭。


五、业务接口
------------

Windows 打包版默认不校验 API Key，业务接口可以直接访问，不需要添加
Authorization 或 X-API-Key 请求头。

常用接口：

  POST /api/plan/generate                    生成单个计划，默认同步等待结果
  POST /api/plan/generate?async=true         异步提交单个计划
  POST /api/plan/generate/batch              批量提交计划
  GET  /api/plan/jobs/{job_id}               查询异步任务状态
  GET  /api/plan/jobs/{job_id}/result        获取异步任务结果


六、完整调用示例
----------------

下面演示将一辆位于“存5线北”的车辆调往“机库线”。

前提：

1. 服务已经在本机 8000 端口启动。

打开 PowerShell，复制并执行下面的全部内容：

  $baseUrl = "http://127.0.0.1:8000"

  $body = @{
      case_id = "0101Z"
      request = @{
          StartStatus = @(
              @{
                  Line = "存5线北"
                  Position = 1
                  RepairProcess = "段"
                  Type = "C70"
                  No = "1000001"
                  Length = 14.3
                  IsHeavy = $false
                  IsWeigh = $false
                  IsClosedDoor = $false
                  TargetLines = @("机库线")
              }
          )
          TerminalLines = @(
              @{ Line = "修1库内"; IsInspectionMode = $false },
              @{ Line = "修2库内"; IsInspectionMode = $false },
              @{ Line = "修3库内"; IsInspectionMode = $false },
              @{ Line = "修4库内"; IsInspectionMode = $false }
          )
          locoNode = @{
              Line = "机库线"
              End = "North"
          }
      }
  }

  $json = $body | ConvertTo-Json -Depth 10

  $response = Invoke-RestMethod `
      -Method Post `
      -Uri "$baseUrl/api/plan/generate" `
      -ContentType "application/json; charset=utf-8" `
      -Body ([System.Text.Encoding]::UTF8.GetBytes($json))

  $resultJson = $response | ConvertTo-Json -Depth 20
  $resultJson
  $resultJson | Set-Content -Path ".\plan-result.json" -Encoding UTF8

调用成功后，PowerShell 会显示结果，并在当前目录生成 plan-result.json。
当前版本的示例结果如下；PassbyPath 等路径细节可能随版本调整：

  {
    "Success": true,
    "Message": "",
    "StatusCode": 200,
    "Data": {
      "Operations": [
        {
          "Index": 1,
          "Line": "存5线北",
          "Action": "Get",
          "MoveCars": ["1000001"],
          "TrainCars": ["1000001"],
          "PassbyPath": [
            "机库线", "渡4", "机北2", "机北1", "渡2", "联6", "渡1",
            "存5线北"
          ]
        },
        {
          "Index": 2,
          "Line": "机库线",
          "Action": "Put",
          "MoveCars": ["1000001"],
          "TrainCars": [],
          "PassbyPath": [
            "存5线北", "渡1", "联6", "渡2", "机北1", "机北2", "渡4",
            "机库线"
          ]
        }
      ],
      "GeneratedEndStatus": [
        {
          "No": "1000001",
          "Line": "机库线",
          "Position": 1
        }
      ]
    }
  }

主要结果字段：

  Success                    是否成功完成四阶段求解
  Data.Operations            调车作业步骤（勾计划）
  Operations[].Action        Get=取车，Put=放车，Weigh=称重
  Operations[].MoveCars      本次动作涉及的车号
  Operations[].PassbyPath    本勾经过的线路和节点
  Data.GeneratedEndStatus    所有车辆计算后的最终线路和台位

不要只根据 HTTP 状态码判断求解是否完整完成；收到响应后还应检查 Success。


七、从其他电脑访问
------------------

1. 确认 TRAIN_CAL_API_HOST 使用默认值 0.0.0.0。
2. 在 Windows 防火墙中仅向可信网段放行所用 TCP 端口，例如 8000。
3. 将示例中的 127.0.0.1 改成服务器实际 IP，例如：

  http://192.168.1.20:8000

当前服务没有 API Key 鉴权，任何能够连接该端口的设备都可以提交任务。仅在可信
内网中开放，并使用 Windows 防火墙限制来源地址。不要把 HTTP 端口直接暴露到公网；
如需跨网络访问，应在前方配置带鉴权的 HTTPS 反向代理。


八、任务文件
------------

默认情况下，请求、阶段产物、结果和日志保存在：

  artifacts\api_jobs\<job_id>\

常见文件包括：

  job.json                   任务状态
  result.json                最终结果
  logs\supervisor.log        工作进程日志

任务默认保留 168 小时（7 天），服务会定期清理过期任务。如需把任务放到其他磁盘，
可在 server.env.cmd 中设置 TRAIN_CAL_API_JOB_ROOT，例如：

  set "TRAIN_CAL_API_JOB_ROOT=D:\train-cal-jobs"


九、常见问题
------------

1. 双击 start-server.cmd 后窗口立即关闭

   在解压目录的地址栏输入 cmd 并回车，再执行 start-server.cmd，以便查看错误信息。
   常见原因是端口已被占用、目录没有写权限或配置值格式错误。

2. 提示端口已被占用

   修改 server.env.cmd 中的 TRAIN_CAL_API_PORT，例如改为 8001，然后重启服务；
   调用地址也要改成相同端口。

3. readyz 返回 503

   服务当前不可接收任务。检查返回内容和服务窗口日志；默认要求任务磁盘至少有
   512 MiB 可用空间。

4. 返回 422

   请求字段或业务数据校验失败。查看响应中的 Message 和 Errors，重点检查车号、
   股道名、台位、目标股道和 case_id。case_id 格式必须是 4 位数字加 W 或 Z，
   例如 0227W。

5. 返回 429

   运行中和排队中的任务已达到 TRAIN_CAL_API_MAX_PENDING。等待已有任务结束，或在
   评估 CPU、内存后调整 WORKERS 和 MAX_PENDING。

6. 服务启动时提示 JOB_ROOT 已被使用

   同一个任务目录只能由一个服务实例使用。停止重复启动的实例，或者为不同实例
   分别设置 TRAIN_CAL_API_JOB_ROOT。
