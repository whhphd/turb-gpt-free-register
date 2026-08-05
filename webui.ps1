#Requires -Version 5.1
<#
.SYNOPSIS
  Turb GPT Free Register WebUI 管理脚本（Windows）

.DESCRIPTION
  解决反复强杀后端口/锁残留导致的"旧进程清不掉、端口幽灵占用"问题。

  用法:
    .\webui.ps1 start
    .\webui.ps1 stop
    .\webui.ps1 restart
    .\webui.ps1 status
    .\webui.ps1 logs

  可选环境变量 / 参数:
    -HostAddr 127.0.0.1
    -Port 5000
    -AuthCode xxx
    -OpenBrowser
    -VerboseLog
    -AutoPort   首选端口被幽灵占用时自动尝试后续端口（默认开）

.EXAMPLE
    .\webui.ps1 restart
    .\webui.ps1 restart -Port 5001
    $env:PORT=5002; .\webui.ps1 start
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('start', 'stop', 'restart', 'status', 'logs', 'help', '')]
    [string]$Command = 'help',

    [string]$HostAddr = $(if ($env:HOST) { $env:HOST } else { '127.0.0.1' }),

    [int]$Port = $(if ($env:PORT) { [int]$env:PORT } else { 5000 }),

    [string]$AuthCode = $(if ($env:AUTH_CODE) { $env:AUTH_CODE } elseif ($env:WEBUI_AUTH_CODE) { $env:WEBUI_AUTH_CODE } else { '' }),

    [switch]$OpenBrowser = ($env:OPEN_BROWSER -eq '1' -or $env:OPEN_BROWSER -eq 'true'),

    [switch]$VerboseLog = ($env:VERBOSE -eq '1' -or $env:VERBOSE -eq 'true'),

    [switch]$AutoPort = (-not ($env:AUTO_PORT -eq '0' -or $env:AUTO_PORT -eq 'false')),

    [int]$HealthTimeoutSec = 12,

    [int]$StopWaitSec = 8
)

$ErrorActionPreference = 'Stop'
$RootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $RootDir

$RunDir = Join-Path $RootDir 'run'
$LogDir = Join-Path $RootDir 'logs'
$PidFile = Join-Path $RunDir 'webui.pid'
$PortFile = Join-Path $RunDir 'webui.port'
$OutLog = Join-Path $LogDir 'webui.out.log'
$ErrLog = Join-Path $LogDir 'webui.err.log'
$Python = Join-Path $RootDir '.venv\Scripts\python.exe'

function Write-Info([string]$Message) { Write-Host "[webui] $Message" -ForegroundColor Cyan }
function Write-Ok([string]$Message) { Write-Host "[webui] $Message" -ForegroundColor Green }
function Write-Warn([string]$Message) { Write-Host "[webui] $Message" -ForegroundColor Yellow }
function Write-Err([string]$Message) { Write-Host "[webui] $Message" -ForegroundColor Red }

function Ensure-Dirs {
    New-Item -ItemType Directory -Force -Path $RunDir, $LogDir | Out-Null
}

function Get-LockPath([int]$PortNum) {
    Join-Path $env:TEMP "turb-gpt-free-register-web-$PortNum.lock"
}

function Get-WebUiProcesses {
    # 只匹配本项目 web.py，避免误杀其它 Python
    $escapedRoot = [regex]::Escape($RootDir)
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Name -match 'python(\.exe)?' -and
            $_.CommandLine -and
            $_.CommandLine -match 'web\.py' -and
            (
                $_.CommandLine -match $escapedRoot -or
                $_.CommandLine -match 'turb-gpt-free-register'
            )
        }
}

function Get-PortOwnerPids([int]$PortNum) {
    $pids = @()
    try {
        $pids = @(
            Get-NetTCPConnection -LocalPort $PortNum -ErrorAction SilentlyContinue |
                Select-Object -ExpandProperty OwningProcess -Unique |
                Where-Object { $_ -and $_ -ne 0 }
        )
    } catch {
        # fallback netstat
        $lines = netstat -ano | Select-String -Pattern ":$PortNum\s+.*LISTENING\s+(\d+)$"
        foreach ($m in $lines) {
            if ($m.Matches.Count -gt 0) {
                $pids += [int]$m.Matches[0].Groups[1].Value
            }
        }
    }
    $pids | Select-Object -Unique
}

function Test-ProcessAlive([int]$ProcessId) {
    if ($ProcessId -le 0) { return $false }
    return [bool](Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)
}

function Test-PortFree([int]$PortNum) {
    try {
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $PortNum)
        $listener.Start()
        $listener.Stop()
        return $true
    } catch {
        return $false
    }
}

function Test-PortIsGhost([int]$PortNum) {
    # 端口被占用，但 OwningProcess 全部不存在 -> 幽灵端口
    $owners = @(Get-PortOwnerPids $PortNum)
    if ($owners.Count -eq 0) {
        # 没有 owner 但仍绑不上，也按幽灵/残留处理
        return -not (Test-PortFree $PortNum)
    }
    $alive = @($owners | Where-Object { Test-ProcessAlive $_ })
    return ($alive.Count -eq 0)
}

function Remove-WebUiLocks([int[]]$Ports) {
    foreach ($p in $Ports) {
        $lock = Get-LockPath $p
        if (Test-Path -LiteralPath $lock) {
            try {
                $item = Get-Item -LiteralPath $lock -Force
                $item.Attributes = 'Normal'
                Remove-Item -LiteralPath $lock -Force -ErrorAction Stop
                Write-Info "已删除锁文件: $lock"
            } catch {
                Write-Warn "锁文件删除失败: $lock ($($_.Exception.Message))"
            }
        }
    }
    # 清扫所有本项目锁
    Get-ChildItem -Path $env:TEMP -Filter 'turb-gpt-free-register-web-*.lock' -ErrorAction SilentlyContinue |
        ForEach-Object {
            try {
                $_.Attributes = 'Normal'
                Remove-Item -LiteralPath $_.FullName -Force -ErrorAction Stop
            } catch {}
        }
}

function Stop-WebUiTree([int]$ProcessId) {
    if (-not (Test-ProcessAlive $ProcessId)) { return }
    # 先尝试温和结束
    try {
        Stop-Process -Id $ProcessId -ErrorAction SilentlyContinue
    } catch {}
    $deadline = (Get-Date).AddSeconds([Math]::Max(1, $StopWaitSec))
    while ((Get-Date) -lt $deadline -and (Test-ProcessAlive $ProcessId)) {
        Start-Sleep -Milliseconds 300
    }
    if (Test-ProcessAlive $ProcessId) {
        # 进程树强杀（含子进程）
        $null = & taskkill.exe /F /T /PID $ProcessId 2>&1
        Start-Sleep -Milliseconds 400
    }
}

function Stop-AllWebUi {
    Ensure-Dirs
    $procs = @(Get-WebUiProcesses)
    $pidFromFile = 0
    if (Test-Path -LiteralPath $PidFile) {
        $raw = (Get-Content -LiteralPath $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
        [void][int]::TryParse(($raw -replace '\D', ''), [ref]$pidFromFile)
    }

    $targets = @()
    foreach ($p in $procs) { $targets += [int]$p.ProcessId }
    if ($pidFromFile -gt 0) { $targets += $pidFromFile }

    # 也清理占用常见端口、且命令行含 web.py 的进程
    foreach ($port in @($Port, 5000, 5001, 5002, 5003, 5004, 5005)) {
        foreach ($opid in (Get-PortOwnerPids $port)) {
            if (-not (Test-ProcessAlive $opid)) { continue }
            $cim = Get-CimInstance Win32_Process -Filter "ProcessId=$opid" -ErrorAction SilentlyContinue
            if ($cim -and $cim.CommandLine -and $cim.CommandLine -match 'web\.py') {
                $targets += $opid
            }
        }
    }

    $targets = $targets | Select-Object -Unique
    if (-not $targets -or $targets.Count -eq 0) {
        Write-Info '没有正在运行的 WebUI 进程'
    } else {
        Write-Info ("正在停止 WebUI 进程: " + ($targets -join ', '))
        foreach ($targetPid in $targets) {
            Stop-WebUiTree -ProcessId $targetPid
            if (Test-ProcessAlive $targetPid) {
                Write-Warn "进程 $targetPid 仍未退出"
            } else {
                Write-Ok "已停止 PID=$targetPid"
            }
        }
    }

    Remove-WebUiLocks -Ports @($Port, 5000, 5001, 5002, 5003, 5004, 5005)
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue

    # 报告幽灵端口
    foreach ($p in @($Port, 5000, 5001, 5002)) {
        if (Test-PortIsGhost $p) {
            Write-Warn "端口 $p 仍是幽灵占用（进程已不存在但 LISTEN 残留）。Windows 用户态无法清掉，脚本会自动换端口；彻底清理请重启电脑。"
        }
    }
}

function Test-WebUiHealth([string]$BindHost, [int]$PortNum, [int]$TimeoutSec = 8) {
    $url = "http://127.0.0.1:$PortNum/login"
    if ($BindHost -notin @('0.0.0.0', '::', '127.0.0.1', 'localhost')) {
        $url = "http://${BindHost}:$PortNum/login"
    }
    try {
        $resp = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec $TimeoutSec
        return ($resp.StatusCode -ge 200 -and $resp.StatusCode -lt 500)
    } catch {
        return $false
    }
}

function Resolve-StartPort([int]$Preferred) {
    $candidates = @($Preferred)
    if ($AutoPort) {
        foreach ($delta in 1..20) {
            $candidates += ($Preferred + $delta)
        }
        foreach ($extra in 5000, 5001, 5002, 5003, 5004, 5005, 5010, 5080, 5088) {
            if ($candidates -notcontains $extra) { $candidates += $extra }
        }
    }

    foreach ($p in $candidates) {
        if ($p -lt 1 -or $p -gt 65535) { continue }

        # 真实本项目进程占用：先停掉
        $owners = @(Get-PortOwnerPids $p | Where-Object { Test-ProcessAlive $_ })
        foreach ($opid in $owners) {
            $cim = Get-CimInstance Win32_Process -Filter "ProcessId=$opid" -ErrorAction SilentlyContinue
            if ($cim -and $cim.CommandLine -match 'web\.py') {
                Write-Info "端口 $p 被本项目 WebUI(PID=$opid) 占用，先停止"
                Stop-WebUiTree -ProcessId $opid
            }
        }

        if (Test-PortIsGhost $p) {
            Write-Warn "跳过幽灵端口 $p"
            continue
        }

        if (Test-PortFree $p) {
            return $p
        }

        # 端口忙且不是幽灵：可能是别的软件
        $aliveOwners = @(Get-PortOwnerPids $p | Where-Object { Test-ProcessAlive $_ })
        if ($aliveOwners.Count -gt 0) {
            Write-Warn ("端口 $p 被其它进程占用: " + ($aliveOwners -join ','))
        } else {
            Write-Warn "端口 $p 不可用"
        }
    }
    throw "没有可用端口（首选 $Preferred）。请关闭占用程序，或重启电脑清理幽灵端口。"
}

function Start-WebUi {
    Ensure-Dirs

    if (-not (Test-Path -LiteralPath $Python)) {
        throw "找不到虚拟环境 Python: $Python`n请先: python -m venv .venv && .\.venv\Scripts\pip install -r requirements.txt"
    }

    # 启动前尽量清掉本项目旧实例与锁
    $existing = @(Get-WebUiProcesses)
    if ($existing.Count -gt 0) {
        Write-Info '检测到已有 WebUI，先执行 stop'
        Stop-AllWebUi
        Start-Sleep -Seconds 1
    } else {
        Remove-WebUiLocks -Ports @($Port, 5000, 5001, 5002, 5003, 5004, 5005)
    }

    $usePort = Resolve-StartPort -Preferred $Port
    if ($usePort -ne $Port) {
        Write-Warn "首选端口 $Port 不可用，改用 $usePort"
    }

    $args = @('web.py', '--host', $HostAddr, '--port', "$usePort")
    if ($OpenBrowser) { $args += '--open-browser' }
    if ($VerboseLog) { $args += '--verbose' }
    if ($AuthCode) { $args += @('--auth-code', $AuthCode) }

    # 轮转日志，避免无限增长
    foreach ($f in @($OutLog, $ErrLog)) {
        if ((Test-Path -LiteralPath $f) -and ((Get-Item -LiteralPath $f).Length -gt 5MB)) {
            Move-Item -LiteralPath $f -Destination ($f + '.1') -Force -ErrorAction SilentlyContinue
        }
    }

    Write-Info "启动: $Python $($args -join ' ')"
    $proc = Start-Process -FilePath $Python `
        -ArgumentList $args `
        -WorkingDirectory $RootDir `
        -RedirectStandardOutput $OutLog `
        -RedirectStandardError $ErrLog `
        -PassThru `
        -WindowStyle Hidden

    Set-Content -LiteralPath $PidFile -Value $proc.Id -Encoding ascii
    Set-Content -LiteralPath $PortFile -Value $usePort -Encoding ascii

    $ok = $false
    $deadline = (Get-Date).AddSeconds([Math]::Max(3, $HealthTimeoutSec))
    while ((Get-Date) -lt $deadline) {
        if ($proc.HasExited) { break }
        if (Test-WebUiHealth -BindHost $HostAddr -PortNum $usePort -TimeoutSec 3) {
            $ok = $true
            break
        }
        Start-Sleep -Milliseconds 500
    }

    if (-not $ok) {
        $tail = ''
        if (Test-Path -LiteralPath $ErrLog) {
            $tail = (Get-Content -LiteralPath $ErrLog -Tail 20 -ErrorAction SilentlyContinue) -join "`n"
        }
        if ($proc.HasExited) {
            throw "WebUI 启动后立即退出 (exit=$($proc.ExitCode))。日志:`n$tail"
        }
        # 进程在但健康检查失败：可能是幽灵端口导致"假监听"
        Write-Warn "健康检查超时。若页面打不开，请查看日志: $ErrLog"
        if ($tail) { Write-Host $tail }
        throw "WebUI 未通过健康检查: http://127.0.0.1:$usePort/login"
    }

    $url = "http://127.0.0.1:$usePort"
    Write-Ok "启动成功 PID=$($proc.Id)  地址=$url"
    Write-Info "日志: $ErrLog"
    if ($OpenBrowser) {
        Start-Process $url | Out-Null
    }
    return @{ Pid = $proc.Id; Port = $usePort; Url = $url }
}

function Show-Status {
    Ensure-Dirs
    $procs = @(Get-WebUiProcesses)
    $savedPid = 0
    $savedPort = $Port
    if (Test-Path -LiteralPath $PidFile) {
        [void][int]::TryParse((Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1), [ref]$savedPid)
    }
    if (Test-Path -LiteralPath $PortFile) {
        [void][int]::TryParse((Get-Content $PortFile -ErrorAction SilentlyContinue | Select-Object -First 1), [ref]$savedPort)
    }

    if ($procs.Count -eq 0) {
        Write-Info 'WebUI 未运行'
        foreach ($p in @($savedPort, 5000, 5001, 5002)) {
            if (Test-PortIsGhost $p) {
                Write-Warn "检测到幽灵端口 $p（进程已死，LISTEN 残留）-> 请换端口启动或重启电脑"
            }
        }
        return 1
    }

    foreach ($p in $procs) {
        Write-Ok "运行中 PID=$($p.ProcessId)"
        if ($p.CommandLine) { Write-Host "  $($p.CommandLine)" }
    }

    $checkPort = $savedPort
    if (Test-WebUiHealth -BindHost $HostAddr -PortNum $checkPort -TimeoutSec 4) {
        Write-Ok "健康检查通过: http://127.0.0.1:$checkPort"
    } else {
        Write-Warn "进程在，但 http://127.0.0.1:$checkPort 无响应（可能端口错乱/幽灵占用）"
        Write-Info '建议执行: .\webui.ps1 restart'
    }
    return 0
}

function Show-Logs {
    Ensure-Dirs
    if (-not (Test-Path -LiteralPath $ErrLog)) {
        New-Item -ItemType File -Path $ErrLog -Force | Out-Null
    }
    Write-Info "跟踪日志 Ctrl+C 退出: $ErrLog"
    Get-Content -LiteralPath $ErrLog -Wait -Tail 80
}

function Show-Help {
    @"
用法: .\webui.ps1 <command> [options]

commands:
  start      启动 WebUI（自动避开幽灵端口）
  stop       停止本项目所有 WebUI，并清理锁文件
  restart    停止后启动（推荐日常使用）
  status     查看状态与健康检查
  logs       跟踪 webui.err.log

options:
  -HostAddr 127.0.0.1
  -Port 5000
  -AuthCode xxxx
  -OpenBrowser
  -VerboseLog
  -AutoPort            # 默认开启：端口被幽灵占用时自动 +1 尝试

examples:
  .\webui.ps1 restart
  .\webui.ps1 restart -Port 5001
  .\webui.ps1 stop
  .\webui.ps1 status

说明:
  Windows 强杀后可能留下"幽灵端口"（PID 已不存在仍 LISTEN）。
  用户态无法清除，本脚本会自动换端口启动。
  要彻底释放 5000/5001，只能重启电脑。
"@ | Write-Host
}

# ---- main ----
switch ($Command) {
    'start' { Start-WebUi | Out-Null }
    'stop' { Stop-AllWebUi }
    'restart' {
        Stop-AllWebUi
        Start-Sleep -Seconds 1
        Start-WebUi | Out-Null
    }
    'status' { exit (Show-Status) }
    'logs' { Show-Logs }
    default { Show-Help }
}
