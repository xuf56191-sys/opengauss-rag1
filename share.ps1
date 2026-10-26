<#
  share.ps1 —— 一键把本地服务暴露成临时公网地址（免注册、免绑卡）

  用法（在项目根目录打开 PowerShell）：
      .\share.ps1
      或指定端口： .\share.ps1 -Port 8000

  停止：先 Ctrl+C 断开隧道，再关闭弹出的服务窗口。

  说明：
  - 用的是 localhost.run 的免费 SSH 反向隧道，不需要账号
  - 地址形如 https://xxxx.lhr.life，每次重启都会变
  - 你的电脑必须保持开机/联网，别人才能访问
  - 调用 /ask、/agent 需要请求头 X-API-Key（脚本会打印出来）
#>
param([int]$Port = 8000)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Set-Location $root

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "找不到 venv：$python" -ForegroundColor Red
    exit 1
}

$envFile = Join-Path $root ".env"
if (-not (Test-Path $envFile)) {
    Write-Host '缺少 .env：请先执行 copy .env.example .env 并填写两个 API Key' -ForegroundColor Red
    exit 1
}

# 读取（或生成）访问密钥
$lines = Get-Content $envFile -Encoding UTF8
$keyLine = $lines | Where-Object { $_ -match '^\s*API_ACCESS_KEY\s*=' } | Select-Object -First 1
if (-not $keyLine) {
    $accessKey = (& $python -c "import secrets; print(secrets.token_urlsafe(32))").Trim()
    Add-Content -Path $envFile -Value "API_ACCESS_KEY=$accessKey" -Encoding UTF8
    Write-Host "已在 .env 生成 API_ACCESS_KEY" -ForegroundColor Yellow
} else {
    $accessKey = ($keyLine -split '=', 2)[1].Trim()
}

Write-Host "启动本地服务（端口 $Port）..." -ForegroundColor Cyan
$serviceCmd = "`$env:PORT='$Port'; `$env:API_ACCESS_KEY='$accessKey'; & '$python' docker-entrypoint.py"
Start-Process -FilePath "powershell" -ArgumentList "-NoExit", "-Command", $serviceCmd -WorkingDirectory $root | Out-Null

$ready = $false
for ($i = 0; $i -lt 45; $i++) {
    Start-Sleep -Seconds 2
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/health" -UseBasicParsing -TimeoutSec 3
        if ($r.StatusCode -eq 200) { $ready = $true; break }
    } catch { }
}
if (-not $ready) {
    Write-Host "服务 90 秒内没有就绪，去看新弹出的窗口里的报错" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "本地已就绪：http://127.0.0.1:$Port/docs" -ForegroundColor Green
Write-Host "调用需要请求头：X-API-Key: $accessKey" -ForegroundColor Yellow
Write-Host "接下来会打印公网地址（https://xxxx.lhr.life）——保持本窗口打开，Ctrl+C 断开" -ForegroundColor Yellow
Write-Host ""

ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL -o ServerAliveInterval=30 -R "80:localhost:$Port" nokey@localhost.run
