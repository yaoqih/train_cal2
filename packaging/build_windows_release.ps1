param(
    [string]$Version = "dev",
    [string]$OutputDirectory = "release-dist",
    [int]$MaximumArchiveSizeMiB = 30
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$SpecPath = Join-Path $PSScriptRoot "windows_server.spec"
$SupportPath = Join-Path $PSScriptRoot "windows"
$DistPath = Join-Path $ProjectRoot "dist"
$BundlePath = Join-Path $DistPath "train-cal-server"
$ReleasePath = Join-Path $ProjectRoot $OutputDirectory
$AssetName = "train-cal-four-stage-api-windows-x64.zip"
$AssetPath = Join-Path $ReleasePath $AssetName
$ChecksumPath = "$AssetPath.sha256"

Push-Location $ProjectRoot
try {
    python -m PyInstaller --noconfirm --clean $SpecPath
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }

    Copy-Item (Join-Path $SupportPath "*") $BundlePath -Recurse -Force
    Set-Content -Path (Join-Path $BundlePath "VERSION.txt") -Value $Version -Encoding ascii

    $licenseDirectory = Join-Path $BundlePath "licenses"
    New-Item -ItemType Directory -Path $licenseDirectory -Force | Out-Null
    $pythonHome = Split-Path (Get-Command python).Source -Parent
    $pythonLicense = Join-Path $pythonHome "LICENSE.txt"
    if (-not (Test-Path $pythonLicense)) {
        throw "Missing CPython license file: $pythonLicense"
    }
    Copy-Item $pythonLicense (Join-Path $licenseDirectory "CPython-LICENSE.txt")

    $sitePackages = (& python -c "import sysconfig; print(sysconfig.get_paths()['purelib'])").Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "Could not locate Python site-packages"
    }
    foreach ($packagePrefix in @(
        "starlette",
        "uvicorn",
        "anyio",
        "click",
        "colorama",
        "h11",
        "idna",
        "typing_extensions",
        "pyinstaller"
    )) {
        $distInfo = Get-ChildItem $sitePackages -Directory -Filter "$packagePrefix-*.dist-info" |
            Select-Object -First 1
        if (-not $distInfo) {
            throw "Missing dist-info for license collection: $packagePrefix"
        }
        $licenseFiles = Get-ChildItem $distInfo.FullName -Recurse -File |
            Where-Object { $_.Name -match "^(LICENSE|COPYING)" }
        if (-not $licenseFiles) {
            throw "Missing license file for: $packagePrefix"
        }
        foreach ($license in $licenseFiles) {
            $destinationName = "$($distInfo.Name)-$($license.Name)"
            Copy-Item $license.FullName (Join-Path $licenseDirectory $destinationName)
        }
    }

    foreach ($forbidden in @("data", "artifacts", "tests", "docs")) {
        if (Test-Path (Join-Path $BundlePath $forbidden)) {
            throw "Unexpected directory in Windows bundle: $forbidden"
        }
    }

    New-Item -ItemType Directory -Path $ReleasePath -Force | Out-Null
    Remove-Item $AssetPath, $ChecksumPath -Force -ErrorAction SilentlyContinue

    Push-Location $DistPath
    try {
        & 7z a -tzip -mx=9 $AssetPath "train-cal-server"
        if ($LASTEXITCODE -ne 0) {
            throw "7-Zip failed with exit code $LASTEXITCODE"
        }
    }
    finally {
        Pop-Location
    }

    $asset = Get-Item $AssetPath
    $maximumBytes = $MaximumArchiveSizeMiB * 1MB
    if ($asset.Length -gt $maximumBytes) {
        throw "Windows archive is $([math]::Round($asset.Length / 1MB, 2)) MiB; limit is $MaximumArchiveSizeMiB MiB"
    }

    $hash = (Get-FileHash -Path $AssetPath -Algorithm SHA256).Hash.ToLowerInvariant()
    Set-Content -Path $ChecksumPath -Value "$hash  $AssetName" -Encoding ascii
    Write-Host "Windows release archive: $AssetPath ($([math]::Round($asset.Length / 1MB, 2)) MiB)"

    if ($env:GITHUB_OUTPUT) {
        "asset_path=$AssetPath" | Out-File -FilePath $env:GITHUB_OUTPUT -Encoding utf8 -Append
        "checksum_path=$ChecksumPath" | Out-File -FilePath $env:GITHUB_OUTPUT -Encoding utf8 -Append
        "bundle_path=$BundlePath" | Out-File -FilePath $env:GITHUB_OUTPUT -Encoding utf8 -Append
        "archive_size_bytes=$($asset.Length)" | Out-File -FilePath $env:GITHUB_OUTPUT -Encoding utf8 -Append
    }
}
finally {
    Pop-Location
}
