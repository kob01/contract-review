# 本地开发启动脚本（Windows PowerShell）
# 用法：
#   .\scripts\dev.ps1            # 起数据库 + 后端 + 前端
#   .\scripts\dev.ps1 -NoFrontend
#   .\scripts\dev.ps1 -DbOnly

param(
    [switch]$DbOnly,
    [switch]$NoFrontend
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

Write-Host "==> 启动 PostgreSQL(pgvector)" -ForegroundColor Cyan
docker compose -f "$root\docker-compose.yml" up -d db

if ($DbOnly) {
    Write-Host "数据库已就绪：localhost:5432 (contract_review/contract_review)" -ForegroundColor Green
    return
}

$backendJob = Start-Job -ScriptBlock {
    param($path)
    Set-Location $path
    uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000 2>&1
} -ArgumentList "$root\backend"

Write-Host "==> 后端在后台运行，日志：Receive-Job `$backendJob" -ForegroundColor Cyan
Write-Host "==> 启动前端 Vite（前台，Ctrl+C 结束）" -ForegroundColor Cyan
Set-Location "$root\frontend"

try {
    npm run dev
} finally {
    Stop-Job $backendJob
    Remove-Job $backendJob
    Write-Host "后端任务已停止" -ForegroundColor Yellow
}
