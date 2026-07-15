$ErrorActionPreference = 'Stop'

function Wait-BeforeExit {
    # 交互式窗口出错时暂停，避免双击运行时窗口闪退、错误一闪而过
    if ($Host.Name -eq 'ConsoleHost' -and -not $env:DEEPSWE_UI_NONINTERACTIVE) {
        Write-Host ''
        Read-Host '按回车键退出'
    }
}

try {
    $root = Split-Path -Parent $MyInvocation.MyCommand.Path
    $venv = Join-Path $root '.venv'
    $py = Join-Path $venv 'Scripts\python.exe'

    # Docker Compose v5 Bake may stay silent while resolving metadata on Windows.
    # Keep an explicit user choice, otherwise use the more predictable path.
    if (-not $env:COMPOSE_BAKE) { $env:COMPOSE_BAKE = 'false' }

    # $ErrorActionPreference 对原生命令无效（PowerShell 5.1），必须显式检查退出码
    if (-not (Test-Path $venv)) {
        python -m venv $venv
        if ($LASTEXITCODE -ne 0) { throw 'python -m venv 失败，请确认已安装 Python 并在 PATH 中' }
    }
    & $py -m pip install -q -r "$root\backend\requirements.txt"
    if ($LASTEXITCODE -ne 0) { throw 'pip install 失败，请检查网络或 requirements.txt' }

    if (-not (Test-Path "$root\frontend\node_modules")) {
        npm install --prefix "$root\frontend"
        if ($LASTEXITCODE -ne 0) { throw 'npm install 失败' }
    }

    # 源码没变化时跳过前端构建，避免每次启动 15-35 秒纯等待
    $dist = "$root\frontend\dist\index.html"
    $needBuild = -not (Test-Path $dist)
    if (-not $needBuild) {
        $distTime = (Get-Item $dist).LastWriteTimeUtc
        foreach ($name in @('src.tsx', 'style.css', 'index.html', 'package.json', 'tsconfig.json')) {
            $source = "$root\frontend\$name"
            if ((Test-Path $source) -and (Get-Item $source).LastWriteTimeUtc -gt $distTime) { $needBuild = $true }
        }
    }
    if ($needBuild) {
        npm run build --prefix "$root\frontend"
        if ($LASTEXITCODE -ne 0) { throw '前端构建失败' }
    }

    # 端口被占用会让新 uvicorn 立即退出（此前表现为窗口闪退）。
    # 若占用者是本工具的旧实例则自动停止接管；是其他程序则明确报错。
    $occupied = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
    if ($occupied) {
        $ownerPid = ($occupied | Select-Object -First 1).OwningProcess
        $isOurs = $false
        try {
            $health = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8765/api/health' -TimeoutSec 3
            if ($health.Content -match 'DeepSWE|"status"\s*:\s*"ok"') { $isOurs = $true }
        } catch {}
        if ($isOurs) {
            Write-Host "端口 8765 上发现旧的 DeepSWE UI 实例（PID $ownerPid），停止后接管..."
            try { Stop-Process -Id $ownerPid -Force -ErrorAction Stop } catch {}
            Start-Sleep -Seconds 1
        } else {
            throw "端口 8765 已被其他程序占用（PID $ownerPid），请先释放端口或结束该进程"
        }
    }

    # 先启动服务，健康检查通过后再打开浏览器，避免必现的连接失败页
    $server = Start-Process -FilePath $py -ArgumentList '-m', 'uvicorn', 'app.main:app', '--app-dir', "$root\backend", '--host', '127.0.0.1', '--port', '8765' -PassThru -NoNewWindow
    try {
        $ready = $false
        for ($i = 0; $i -lt 60 -and -not $ready; $i++) {
            Start-Sleep -Milliseconds 500
            if ($server.HasExited) { throw 'uvicorn 启动后立即退出，请查看上方错误输出（常见原因：端口被占用、依赖缺失）' }
            try {
                Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8765/api/health' -TimeoutSec 2 | Out-Null
                $ready = $true
            } catch {}
        }
        if ($ready) { Start-Process 'http://127.0.0.1:8765' }
        else { Write-Warning '服务未在 30 秒内就绪，浏览器未自动打开' }
        Wait-Process -Id $server.Id
    } finally {
        if (-not $server.HasExited) { Stop-Process -Id $server.Id -Force }
    }
} catch {
    Write-Host ''
    Write-Host "启动失败：$($_.Exception.Message)" -ForegroundColor Red
    Wait-BeforeExit
    exit 1
}
