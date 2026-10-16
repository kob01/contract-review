# 本地开发启动脚本 —— 只能用 PowerShell 运行（windows powershell 或 pwsh）
# 如果你的终端是 Git Bash / WSL / macOS / Linux 的 sh、bash，请改用 ./scripts/dev.sh；
# 用 sh 直接执行本文件会被当成 shell 语法，报 "syntax error near unexpected token `newline'"。
# 用法：
#   .\scripts\dev.ps1            # 起数据库 + Redis + 后端/worker(后台) + 前端(前台)
#   .\scripts\dev.ps1 -DbOnly    # 只起数据库、Redis 与 Qdrant
#   .\scripts\dev.ps1 -NoFrontend  只起数据库 + Redis + 后端（后端日志留在前台）

param(
    [switch]$DbOnly,
    [switch]$NoFrontend
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

# 实际端口取自 .env 的 DB_PORT（没配则用 compose 默认的 5433），避免提示信息里写错端口
$dbPort = '5433'
$envLine = Select-String -Path "$root\.env", "$root\.env.example" -Pattern '^\s*DB_PORT\s*=\s*(\d+)' -ErrorAction SilentlyContinue | Select-Object -Last 1
if ($envLine) { $dbPort = $envLine.Matches[0].Groups[1].Value }

# 在仓库根目录执行 compose，保证 docker compose 能读到根目录的 .env（PGVECTOR_IMAGE / DB_PORT / REDIS_IMAGE / QDRANT_IMAGE）
Write-Host "==> 启动 PostgreSQL、Redis(队列) 与 Qdrant(向量)" -ForegroundColor Cyan
Push-Location $root
try {
    docker compose up -d db redis qdrant
    # docker 是外部程序，$ErrorActionPreference 不会拦住它的非零退出码，必须自己查 $LASTEXITCODE
    if ($LASTEXITCODE -ne 0) {
        $hint = "依赖服务启动失败（exit=$LASTEXITCODE）。常见原因：Docker Desktop 未运行，或宿主机端口已被其他容器占用。"
        $hint += "端口冲突时：在 .env 里改 DB_PORT / REDIS_PORT（当前 DB 映射宿主机 $dbPort），并把 DATABASE_URL / REDIS_URL 同步改成同一个值。"
        throw $hint
    }
} finally {
    Pop-Location
}

if ($DbOnly) {
    docker compose -f "$root\docker-compose.yml" ps db redis qdrant
    Write-Host "数据库已就绪：localhost:$dbPort (contract_review/contract_review)" -ForegroundColor Green
    Write-Host "Redis 已就绪：localhost:6379（队列后端）" -ForegroundColor Green
    return
}

# 审查任务已交给独立 worker 进程：本地不起 worker 的话，任务会一直排在队列里
Write-Host "==> 启动审查 worker（后台，日志：Receive-Job `$workerJob）" -ForegroundColor Cyan
$workerJob = Start-Job -ScriptBlock {
    param($path)
    Set-Location $path
    uv run python -m app.tasks.worker 2>&1
} -ArgumentList "$root\backend"

if ($NoFrontend) {
    Write-Host "==> 启动后端（前台，Ctrl+C 结束）" -ForegroundColor Cyan
    Write-Host "    提示：Redis 未起时后端会自动退回进程内执行，但队列任务不会被本 worker 消费" -ForegroundColor DarkGray
    Set-Location "$root\backend"
    try {
        uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
    } finally {
        Stop-Job $workerJob
        Remove-Job $workerJob
    }
    return
}

$backendJob = Start-Job -ScriptBlock {
    param($path)
    Set-Location $path
    uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000 2>&1
} -ArgumentList "$root\backend"

Write-Host "==> 后端与 worker 在后台运行，日志：Receive-Job `$backendJob / Receive-Job `$workerJob" -ForegroundColor Cyan
Write-Host "==> 启动前端 Vite（前台，Ctrl+C 结束）" -ForegroundColor Cyan
Set-Location "$root\frontend"

try {
    npm run dev
} finally {
    Stop-Job $backendJob, $workerJob
    Remove-Job $backendJob, $workerJob
    Write-Host "后端与 worker 任务已停止" -ForegroundColor Yellow
}
