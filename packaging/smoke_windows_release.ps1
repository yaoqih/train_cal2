param(
    [Parameter(Mandatory = $true)]
    [string]$BundlePath
)

$ErrorActionPreference = "Stop"
$BundlePath = (Resolve-Path $BundlePath).Path
$Executable = Join-Path $BundlePath "train-cal-server.exe"
$TemporaryRoot = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { [IO.Path]::GetTempPath() }
$SmokeRoot = Join-Path $TemporaryRoot "train-cal-api-smoke-$([guid]::NewGuid().ToString('N'))"
$StdoutLog = Join-Path $SmokeRoot "server.stdout.log"
$StderrLog = Join-Path $SmokeRoot "server.stderr.log"
New-Item -ItemType Directory -Path $SmokeRoot -Force | Out-Null

$listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
$listener.Start()
$Port = ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
$listener.Stop()

Remove-Item Env:TRAIN_CAL_API_KEY -ErrorAction SilentlyContinue
$env:TRAIN_CAL_ALLOW_UNAUTHENTICATED = "true"
$env:TRAIN_CAL_API_WORKERS = "2"
$env:TRAIN_CAL_API_MAX_PENDING = "4"
$env:TRAIN_CAL_API_JOB_ROOT = Join-Path $SmokeRoot "jobs"
$env:TRAIN_CAL_API_MIN_FREE_DISK_MB = "0"

$process = Start-Process `
    -FilePath $Executable `
    -ArgumentList @("--host", "127.0.0.1", "--port", $Port, "--no-access-log") `
    -WorkingDirectory $BundlePath `
    -RedirectStandardOutput $StdoutLog `
    -RedirectStandardError $StderrLog `
    -NoNewWindow `
    -PassThru

try {
    $BaseUrl = "http://127.0.0.1:$Port"
    $ready = $false
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        if ($process.HasExited) {
            throw "Packaged server exited before becoming ready (code $($process.ExitCode))"
        }
        try {
            $health = Invoke-RestMethod -Uri "$BaseUrl/healthz" -TimeoutSec 2
            if ($health.status -eq "ok") {
                $ready = $true
                break
            }
        }
        catch {
            Start-Sleep -Milliseconds 500
        }
    }
    if (-not $ready) {
        throw "Packaged server did not become healthy"
    }

    $openApi = Invoke-RestMethod -Uri "$BaseUrl/api/plan/openapi.json" -TimeoutSec 5
    $requiredOperationFields = @($openApi.components.schemas.PlanOperation.required)
    foreach ($field in @("PassbyPath", "ByPassSwitch")) {
        if ($field -notin $requiredOperationFields) {
            throw "Packaged OpenAPI is missing required PlanOperation field: $field"
        }
    }

    $Body = @'
{
  "case_id": "0101Z",
  "request": {
    "StartStatus": [{
      "Line": "\u673a\u5357",
      "Position": 1,
      "RepairProcess": "\u6bb5",
      "Type": "C70",
      "No": "1000001",
      "Length": 14.3,
      "TargetLines": ["\u5b581\u7ebf"]
    }],
    "TerminalLines": [
      {"Line": "\u4fee1\u5e93\u5185", "IsInspectionMode": false},
      {"Line": "\u4fee2\u5e93\u5185", "IsInspectionMode": false},
      {"Line": "\u4fee3\u5e93\u5185", "IsInspectionMode": false},
      {"Line": "\u4fee4\u5e93\u5185", "IsInspectionMode": false}
    ],
    "locoNode": {"Line": "\u673a\u8d70\u68da", "End": "North"}
  },
  "options": {
    "stage1": {"time_budget_seconds": 0.2},
    "stage2": {"time_budget_seconds": 0.2},
    "stage3": {"time_budget_seconds": 0.2},
    "stage4": {"time_budget_seconds": 0.2}
  }
}
'@
    $result = Invoke-RestMethod `
        -Method Post `
        -Uri "$BaseUrl/api/plan/generate" `
        -ContentType "application/json; charset=utf-8" `
        -Body $Body `
        -TimeoutSec 30
    if ($result.Success -ne $true) {
        throw "Packaged four-stage smoke returned Success=false: $($result.Message)"
    }
    $operations = @($result.Data.Operations)
    if ($operations.Count -lt 2) {
        throw "Expected a non-empty packaged plan, found $($operations.Count) operations"
    }
    foreach ($operation in $operations) {
        foreach ($field in @("PassbyPath", "ByPassSwitch")) {
            if ($null -eq $operation.PSObject.Properties[$field]) {
                throw "Packaged operation is missing field: $field"
            }
        }
    }
    $firstPath = @($operations[0].PassbyPath)
    $firstSwitches = @($operations[0].ByPassSwitch)
    if ($firstPath.Count -ne 2 -or "L8" -in $firstPath) {
        throw "PassbyPath was not preserved as the physical path"
    }
    if ($firstSwitches.Count -ne 1 -or $firstSwitches[0] -ne "L8") {
        throw "Expected first operation ByPassSwitch to contain only L8"
    }
    $resultFiles = @(Get-ChildItem $env:TRAIN_CAL_API_JOB_ROOT -Recurse -Filter "result.json")
    if ($resultFiles.Count -ne 1) {
        throw "Expected one frozen worker result, found $($resultFiles.Count)"
    }
    Write-Host "Packaged Windows server smoke passed on port $Port"
}
finally {
    if (-not $process.HasExited) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        $process.WaitForExit(5000)
    }
    if (Test-Path $StdoutLog) {
        Write-Host "--- packaged server stdout ---"
        Get-Content $StdoutLog
    }
    if (Test-Path $StderrLog) {
        Write-Host "--- packaged server stderr ---"
        Get-Content $StderrLog
    }
}
