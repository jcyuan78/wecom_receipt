# 启动企业微信文件接收服务
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "创建虚拟环境并安装依赖..."
    python -m venv .venv
    & $python -m pip install --upgrade pip
    & $python -m pip install -r requirements.txt
}

if (-not (Test-Path (Join-Path $PSScriptRoot ".env"))) {
    Write-Warning "未找到 .env，请先复制 .env.example 为 .env 并填写企业微信配置。"
}

$port = $env:WECOM_PORT
if (-not $port -and (Test-Path (Join-Path $PSScriptRoot ".env"))) {
    $match = Select-String -Path (Join-Path $PSScriptRoot ".env") -Pattern '^\s*WECOM_PORT\s*=\s*(\d+)' | Select-Object -First 1
    if ($match) { $port = $match.Matches[0].Groups[1].Value }
}
if (-not $port) { $port = "8080" }

$bindHost = $env:WECOM_HOST
if (-not $bindHost -and (Test-Path (Join-Path $PSScriptRoot ".env"))) {
    $match = Select-String -Path (Join-Path $PSScriptRoot ".env") -Pattern '^\s*WECOM_HOST\s*=\s*(\S+)' | Select-Object -First 1
    if ($match) { $bindHost = $match.Matches[0].Groups[1].Value }
}
if (-not $bindHost) { $bindHost = "0.0.0.0" }

Write-Host "监听 http://${bindHost}:$port/wecom/callback"
& $python -m uvicorn app.main:app --host $bindHost --port $port
