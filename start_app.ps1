<#
.SYNOPSIS
    Start the JobFinder backend (127.0.0.1:8100) and development dashboard (5173).

.DESCRIPTION
    A development launcher for THIS project only.

    It starts two processes, records their identity in .run\*.pid, waits until
    both answer, and opens the dashboard. It tracks only what it starts, so
    stop_app.ps1 can shut them down without guessing which python.exe or
    node.exe belongs to what.

    ISOLATION - this script must never affect the separate LinkedIn/Dice
    auto-apply project:
      * it binds 8100 and 5173, never 8000
      * it refuses to run if the configured API port is 8000
      * it reads only this repository's .env, and connects only to the
        database that .env names
      * it kills nothing; stopping is stop_app.ps1's job, by recorded PID only

.PARAMETER Install
    First-run convenience: run `npm install` when node_modules is missing and
    create frontend\.env.local from the example when absent. Without this
    switch, missing dependencies are reported as errors instead.

.PARAMETER Reload
    Start uvicorn with --reload. Off by default: the reloader forks a child
    process, which makes shutdown less predictable.

.PARAMETER NoBrowser
    Do not open the dashboard in a browser.

.PARAMETER TimeoutSeconds
    How long to wait for each service to answer. Default 90.

.EXAMPLE
    .\start_app.ps1
.EXAMPLE
    .\start_app.ps1 -Install        # first run
.EXAMPLE
    .\start_app.ps1 -Reload -NoBrowser
#>
[CmdletBinding()]
param(
    [switch]$Install,
    [switch]$Reload,
    [switch]$NoBrowser,
    [ValidateRange(10, 600)]
    [int]$TimeoutSeconds = 90
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# --- Fixed facts about this project ----------------------------------------

$RepoRoot      = $PSScriptRoot
$RunDir        = Join-Path $RepoRoot '.run'
$BackendPort   = 8100
$FrontendPort  = 5173
$HealthUrl     = "http://127.0.0.1:$BackendPort/api/v1/health"
$FrontendUrl   = "http://localhost:$FrontendPort"

# The other project's port. Named here once so the guard below is greppable,
# and touched nowhere else.
$ForbiddenPort = 8000

# --- Output helpers ---------------------------------------------------------

function Write-Step   { param([string]$Message) Write-Host "  $Message" }
function Write-Ok     { param([string]$Message) Write-Host "  [ OK ] $Message" -ForegroundColor Green }
function Write-Warn   { param([string]$Message) Write-Host "  [WARN] $Message" -ForegroundColor Yellow }
function Write-Head   { param([string]$Message) Write-Host "`n$Message" -ForegroundColor Cyan }

function Stop-WithError {
    <#  A missing prerequisite is a normal outcome, not a crash. Print what is
        wrong and exactly how to fix it, then exit non-zero. #>
    param([string]$Problem, [string[]]$Fix = @())

    Write-Host ""
    Write-Host "  [FAIL] $Problem" -ForegroundColor Red
    foreach ($line in $Fix) { Write-Host "         $line" -ForegroundColor DarkGray }
    Write-Host ""
    exit 1
}

# --- Small utilities --------------------------------------------------------

function Read-DotEnv {
    <# Parse KEY=VALUE lines. Enough for the checks below - the application
       reads its own configuration through pydantic-settings. #>
    param([string]$Path)

    $values = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $values }

    foreach ($line in Get-Content -LiteralPath $Path) {
        $trimmed = $line.Trim()
        if ($trimmed -eq '' -or $trimmed.StartsWith('#')) { continue }

        $split = $trimmed.IndexOf('=')
        if ($split -lt 1) { continue }

        $key = $trimmed.Substring(0, $split).Trim()
        $value = $trimmed.Substring($split + 1).Trim().Trim('"').Trim("'")
        $values[$key] = $value
    }
    return $values
}

function Test-PortListening {
    param([int]$Port)

    try {
        $found = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop
        if ($found) { return $true }
    } catch {
        # Get-NetTCPConnection throws when nothing matches, and does not exist
        # on every host. Fall through to a plain connect test.
    }

    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne(400)) { return $false }
        $client.EndConnect($async)
        return $true
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Test-TcpReachable {
    param([string]$TargetHost, [int]$Port, [int]$TimeoutMs = 2000)

    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect($TargetHost, $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne($TimeoutMs)) { return $false }
        $client.EndConnect($async)
        return $true
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Invoke-HttpGet {
    <# Return status code and body for any response, including 4xx/5xx, which
       Invoke-WebRequest raises on in Windows PowerShell. A degraded backend
       answers 503 with the diagnosis in the body, and that body is the point. #>
    param([string]$Url, [int]$TimeoutSec = 5)

    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec $TimeoutSec -ErrorAction Stop
        return [pscustomobject]@{ StatusCode = [int]$response.StatusCode; Body = $response.Content }
    } catch [System.Net.WebException] {
        $webResponse = $_.Exception.Response
        if ($null -eq $webResponse) { return $null }

        $body = ''
        try {
            $reader = New-Object System.IO.StreamReader($webResponse.GetResponseStream())
            $body = $reader.ReadToEnd()
            $reader.Close()
        } catch { }

        return [pscustomobject]@{ StatusCode = [int]$webResponse.StatusCode; Body = $body }
    } catch {
        return $null
    }
}

# --- PID tracking -----------------------------------------------------------

function Save-PidFile {
    <# Record enough to prove identity later. A bare PID is not enough: Windows
       reuses PIDs, and stop_app must never kill a stranger that inherited one. #>
    param(
        [string]$Service,
        [System.Diagnostics.Process]$Process,
        [int]$Port,
        [string]$Command
    )

    $record = [ordered]@{
        service      = $Service
        pid          = $Process.Id
        processName  = $Process.ProcessName
        startTimeUtc = $Process.StartTime.ToUniversalTime().ToString('o')
        port         = $Port
        command      = $Command
        repoRoot     = $RepoRoot
        startedBy    = 'start_app.ps1'
        startedAtUtc = (Get-Date).ToUniversalTime().ToString('o')
    }

    $path = Join-Path $RunDir "$Service.pid"
    ($record | ConvertTo-Json -Depth 3) | Out-File -LiteralPath $path -Encoding utf8
    return $path
}

function Get-TrackedProcess {
    <# Return the live process a PID file describes, or $null when the file is
       stale. Identity = same PID, same image name, same start time. #>
    param([string]$Service)

    $path = Join-Path $RunDir "$Service.pid"
    if (-not (Test-Path -LiteralPath $path)) { return $null }

    try {
        $record = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
    } catch {
        return $null
    }

    $process = Get-Process -Id $record.pid -ErrorAction SilentlyContinue
    if (-not $process) { return $null }
    if ($process.ProcessName -ne $record.processName) { return $null }

    try {
        $recorded = [datetime]::Parse($record.startTimeUtc).ToUniversalTime()
        $actual = $process.StartTime.ToUniversalTime()
        if ([math]::Abs(($actual - $recorded).TotalSeconds) -gt 2) { return $null }
    } catch {
        return $null
    }

    return $process
}

function Start-TrackedService {
    param(
        [string]$Service,
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$WorkingDirectory,
        [int]$Port
    )

    $stdout = Join-Path $RunDir "$Service.out.log"
    $stderr = Join-Path $RunDir "$Service.err.log"

    $process = Start-Process -FilePath $FilePath `
        -ArgumentList $Arguments `
        -WorkingDirectory $WorkingDirectory `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -PassThru

    # Start-Process returns before the image is fully initialised; StartTime is
    # only readable once it is.
    Start-Sleep -Milliseconds 400

    $command = "$FilePath $($Arguments -join ' ')"
    $pidFile = Save-PidFile -Service $Service -Process $process -Port $Port -Command $command

    return [pscustomobject]@{
        Process = $process
        PidFile = $pidFile
        StdOut  = $stdout
        StdErr  = $stderr
    }
}

function Show-ServiceLogTail {
    <# Only ever called for a service THIS run started. The logs of a service
       left running by an earlier run describe that run, and printing them
       under a failure message would explain the wrong thing. #>
    param([string]$Service, [int]$Lines = 20)

    foreach ($stream in @('err', 'out')) {
        $path = Join-Path $RunDir "$Service.$stream.log"
        if (-not (Test-Path -LiteralPath $path)) { continue }

        $content = Get-Content -LiteralPath $path -Tail $Lines -ErrorAction SilentlyContinue
        if (-not $content) { continue }

        Write-Host "         --- $Service.$stream.log (last $Lines lines) ---" -ForegroundColor DarkGray
        foreach ($line in $content) { Write-Host "         $line" -ForegroundColor DarkGray }
    }
}

# ===========================================================================
#  1. Preflight
# ===========================================================================

Write-Host ""
Write-Host "JobFinder development launcher" -ForegroundColor Cyan
Write-Host "backend 127.0.0.1:$BackendPort   dashboard localhost:$FrontendPort" -ForegroundColor DarkGray

Write-Head "Checking prerequisites"

# --- Repository root --------------------------------------------------------

foreach ($marker in @('pyproject.toml', 'app\main.py', 'frontend\package.json')) {
    if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot $marker))) {
        Stop-WithError -Problem "This does not look like the JobFinder repository root: $marker is missing." `
            -Fix @("Run the script from the repository root:", "  cd '<repo>'; .\start_app.ps1")
    }
}
Write-Ok "Repository root: $RepoRoot"

# --- Configuration ----------------------------------------------------------

$envPath = Join-Path $RepoRoot '.env'
if (-not (Test-Path -LiteralPath $envPath)) {
    Stop-WithError -Problem "No .env file found at $envPath" `
        -Fix @("Copy-Item .env.example .env", "then set POSTGRES_PASSWORD in .env")
}

$config = Read-DotEnv -Path $envPath

$configuredPort = $BackendPort
if ($config.ContainsKey('API_PORT') -and $config['API_PORT']) { $configuredPort = [int]$config['API_PORT'] }

if ($configuredPort -eq $ForbiddenPort) {
    Stop-WithError -Problem "API_PORT is set to $ForbiddenPort, which belongs to the separate LinkedIn/Dice project." `
        -Fix @("Set API_PORT=$BackendPort in .env. This launcher will not bind $ForbiddenPort.")
}
if ($configuredPort -ne $BackendPort) {
    Stop-WithError -Problem "API_PORT in .env is $configuredPort, but this launcher and the dashboard both expect $BackendPort." `
        -Fix @("Set API_PORT=$BackendPort in .env, or start uvicorn manually on $configuredPort.")
}
Write-Ok "Configuration: .env (API_PORT=$configuredPort)"

# --- Virtual environment ----------------------------------------------------

$venvPython = Join-Path $RepoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    Stop-WithError -Problem "No virtual environment at .venv" `
        -Fix @("python -m venv .venv", ".\.venv\Scripts\Activate.ps1", "pip install -e `".[dev]`"")
}

& $venvPython -c "import uvicorn, fastapi" | Out-Null
if ($LASTEXITCODE -ne 0) {
    Stop-WithError -Problem "The virtual environment exists but its dependencies are not installed (see the import error above)." `
        -Fix @("Install them with:", "  .\.venv\Scripts\python.exe -m pip install -e `".[dev]`"")
}
$pythonVersion = (& $venvPython -c "import sys; print('.'.join(map(str, sys.version_info[:3])))").Trim()
Write-Ok "Python $pythonVersion (.venv)"

# --- Node and npm -----------------------------------------------------------

$npmCommand = Get-Command 'npm.cmd' -ErrorAction SilentlyContinue
if (-not $npmCommand) { $npmCommand = Get-Command 'npm' -ErrorAction SilentlyContinue }
$nodeCommand = Get-Command 'node' -ErrorAction SilentlyContinue

if (-not $nodeCommand -or -not $npmCommand) {
    $missing = @()
    if (-not $nodeCommand) { $missing += 'node' }
    if (-not $npmCommand) { $missing += 'npm' }
    Stop-WithError -Problem "$($missing -join ' and ') not found on PATH." `
        -Fix @("Install Node.js 20 or newer from https://nodejs.org/", "then open a new terminal so PATH is refreshed.")
}
Write-Ok "Node $((& node --version).Trim()), npm $((& $npmCommand.Source --version).Trim())"

# --- Frontend dependencies and configuration --------------------------------

$frontendDir = Join-Path $RepoRoot 'frontend'
$nodeModules = Join-Path $frontendDir 'node_modules'
$frontendEnv = Join-Path $frontendDir '.env.local'

if (-not (Test-Path -LiteralPath $nodeModules)) {
    if (-not $Install) {
        Stop-WithError -Problem "Frontend dependencies are not installed (frontend\node_modules is missing)." `
            -Fix @("Run once:", "  .\start_app.ps1 -Install", "or install them yourself:", "  cd frontend; npm install")
    }

    Write-Step "Installing frontend dependencies (npm install)..."
    Push-Location $frontendDir
    try {
        & $npmCommand.Source install
        if ($LASTEXITCODE -ne 0) {
            Stop-WithError -Problem "npm install failed (exit code $LASTEXITCODE)." `
                -Fix @("Run 'npm install' in frontend\ and read the output.")
        }
    } finally {
        Pop-Location
    }
}
Write-Ok "Frontend dependencies present"

if (-not (Test-Path -LiteralPath $frontendEnv)) {
    $example = Join-Path $frontendDir '.env.example'
    if (-not $Install) {
        Stop-WithError -Problem "frontend\.env.local is missing. The dashboard refuses to start without VITE_API_BASE_URL." `
            -Fix @("Run once:", "  .\start_app.ps1 -Install", "or create it yourself:", "  Copy-Item frontend\.env.example frontend\.env.local")
    }
    Copy-Item -LiteralPath $example -Destination $frontendEnv
    Write-Step "Created frontend\.env.local from the example."
}
Write-Ok "Frontend configuration: frontend\.env.local"

# --- PostgreSQL -------------------------------------------------------------

$pgHost = if ($config.ContainsKey('POSTGRES_HOST') -and $config['POSTGRES_HOST']) { $config['POSTGRES_HOST'] } else { '127.0.0.1' }
$pgPort = if ($config.ContainsKey('POSTGRES_PORT') -and $config['POSTGRES_PORT']) { [int]$config['POSTGRES_PORT'] } else { 5432 }
$pgDatabase = if ($config.ContainsKey('POSTGRES_DB') -and $config['POSTGRES_DB']) { $config['POSTGRES_DB'] } else { 'resume_job_discovery' }

if (-not (Test-TcpReachable -TargetHost $pgHost -Port $pgPort)) {
    Stop-WithError -Problem "PostgreSQL is not accepting connections on ${pgHost}:${pgPort}." `
        -Fix @(
            "Start the service:",
            "  Get-Service postgresql* | Start-Service",
            "or, if you use the bundled stack:",
            "  docker compose up -d      (then set POSTGRES_PORT=5433 in .env)"
        )
}
Write-Ok "PostgreSQL reachable on ${pgHost}:${pgPort} (database ${pgDatabase})"

if (-not (Test-Path -LiteralPath $RunDir)) {
    New-Item -ItemType Directory -Path $RunDir | Out-Null
}

# ===========================================================================
#  2. Start the backend
# ===========================================================================

Write-Head "Starting services"

$backendAlreadyUp = $false
$tracked = Get-TrackedProcess -Service 'backend'

if ($tracked) {
    Write-Warn "Backend already running (PID $($tracked.Id)) - leaving it alone."
    $backendAlreadyUp = $true
} elseif (Test-PortListening -Port $BackendPort) {
    Write-Warn "Port $BackendPort is already in use by a process this script did not start."
    Write-Step "       Not starting a second backend. Run .\stop_app.ps1 first if it is a stale instance."
    $backendAlreadyUp = $true
} else {
    $uvicornArgs = @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', "$BackendPort")
    if ($Reload) { $uvicornArgs += '--reload' }

    $backend = Start-TrackedService -Service 'backend' `
        -FilePath $venvPython -Arguments $uvicornArgs -WorkingDirectory $RepoRoot -Port $BackendPort

    Write-Ok "Backend started (PID $($backend.Process.Id))"
}

# ===========================================================================
#  3. Start the frontend
# ===========================================================================

$frontendAlreadyUp = $false
$tracked = Get-TrackedProcess -Service 'frontend'

if ($tracked) {
    Write-Warn "Dashboard already running (PID $($tracked.Id)) - leaving it alone."
    $frontendAlreadyUp = $true
} elseif (Test-PortListening -Port $FrontendPort) {
    Write-Warn "Port $FrontendPort is already in use by a process this script did not start."
    Write-Step "       Not starting a second dev server. Vite is pinned to $FrontendPort (strictPort)."
    $frontendAlreadyUp = $true
} else {
    $frontend = Start-TrackedService -Service 'frontend' `
        -FilePath $npmCommand.Source -Arguments @('run', 'dev') -WorkingDirectory $frontendDir -Port $FrontendPort

    Write-Ok "Dashboard started (PID $($frontend.Process.Id))"
}

# ===========================================================================
#  4. Wait until both answer
# ===========================================================================

Write-Head "Waiting for services"

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$backendReady = $false
$databaseUp = $false

while ((Get-Date) -lt $deadline) {
    $response = Invoke-HttpGet -Url $HealthUrl -TimeoutSec 5
    if ($response) {
        $backendReady = $true
        try {
            $payload = $response.Body | ConvertFrom-Json
            $dbCheck = $payload.data.checks | Where-Object { $_.name -eq 'database' }
            $databaseUp = ($dbCheck -and $dbCheck.status -eq 'up')
        } catch {
            $databaseUp = $false
        }
        break
    }

    if (-not $backendAlreadyUp -and $backend.Process.HasExited) {
        Write-Host ""
        Write-Host "  [FAIL] The backend exited immediately (code $($backend.Process.ExitCode))." -ForegroundColor Red
        Show-ServiceLogTail -Service 'backend'
        Write-Host ""
        exit 1
    }

    Start-Sleep -Milliseconds 700
}

if (-not $backendReady) {
    Write-Host ""
    Write-Host "  [FAIL] The backend did not answer $HealthUrl within $TimeoutSeconds seconds." -ForegroundColor Red
    if ($backendAlreadyUp) {
        Write-Host "         Port $BackendPort is held by a process this run did not start, and it is not serving the API." -ForegroundColor DarkGray
        Write-Host "         Run .\stop_app.ps1, or find the owner with: Get-NetTCPConnection -LocalPort $BackendPort -State Listen" -ForegroundColor DarkGray
    } else {
        Show-ServiceLogTail -Service 'backend'
    }
    Write-Host ""
    exit 1
}

if ($databaseUp) {
    Write-Ok "Backend healthy   $HealthUrl"
} else {
    Write-Warn "Backend is answering but reports the database as DOWN."
    Write-Step "       Check POSTGRES_PASSWORD and POSTGRES_DB in .env, and that '$pgDatabase' exists."
}

$frontendReady = $false
while ((Get-Date) -lt $deadline) {
    $response = Invoke-HttpGet -Url $FrontendUrl -TimeoutSec 5
    if ($response -and $response.StatusCode -eq 200) { $frontendReady = $true; break }

    if (-not $frontendAlreadyUp -and $frontend.Process.HasExited) {
        Write-Host ""
        Write-Host "  [FAIL] The dev server exited immediately (code $($frontend.Process.ExitCode))." -ForegroundColor Red
        Show-ServiceLogTail -Service 'frontend'
        Write-Host ""
        exit 1
    }

    Start-Sleep -Milliseconds 700
}

if (-not $frontendReady) {
    Write-Host ""
    Write-Host "  [FAIL] The dashboard did not answer $FrontendUrl within $TimeoutSeconds seconds." -ForegroundColor Red
    if ($frontendAlreadyUp) {
        Write-Host "         Port $FrontendPort is held by a process this run did not start, and it is not serving the dashboard." -ForegroundColor DarkGray
        Write-Host "         Run .\stop_app.ps1, or find the owner with: Get-NetTCPConnection -LocalPort $FrontendPort -State Listen" -ForegroundColor DarkGray
    } else {
        Show-ServiceLogTail -Service 'frontend'
    }
    Write-Host ""
    exit 1
}
Write-Ok "Dashboard ready   $FrontendUrl"

# ===========================================================================
#  5. Done
# ===========================================================================

Write-Head "Running"
Write-Host "  Dashboard   $FrontendUrl"
Write-Host "  API docs    http://127.0.0.1:$BackendPort/docs"
Write-Host "  Health      $HealthUrl"
Write-Host "  Logs        .run\backend.*.log, .run\frontend.*.log"
Write-Host ""
Write-Host "  Stop with   .\stop_app.ps1" -ForegroundColor DarkGray
Write-Host ""

if (-not $NoBrowser) {
    Start-Process $FrontendUrl | Out-Null
}

exit 0
