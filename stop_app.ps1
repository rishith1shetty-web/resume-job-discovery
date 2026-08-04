<#
.SYNOPSIS
    Stop the JobFinder backend and dashboard started by start_app.ps1.

.DESCRIPTION
    Stops only what start_app.ps1 recorded in .run\*.pid, and only after
    proving the recorded PID still belongs to the process that was started.

    It never selects processes by name. `Stop-Process -Name python` would kill
    every Python process on the machine, including the separate LinkedIn/Dice
    auto-apply project. This script cannot do that: it starts from a PID file,
    verifies image name and start time, and refuses anything that fails the
    check. A PID that has been recycled by an unrelated process is reported as
    a stale file and removed, not killed.

    Child processes are stopped too - npm spawns node, and uvicorn --reload
    spawns a worker - but only children created after the tracked parent
    started, so a recycled parent PID cannot drag in strangers.

.PARAMETER Service
    Stop one service only: backend or frontend. Default: both.

.PARAMETER ClearLogs
    Also delete .run\*.log. Logs are kept by default so a crash can be read
    after the fact.

.EXAMPLE
    .\stop_app.ps1
.EXAMPLE
    .\stop_app.ps1 -Service backend
#>
[CmdletBinding()]
param(
    [ValidateSet('backend', 'frontend', 'all')]
    [string]$Service = 'all',

    [switch]$ClearLogs
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$RepoRoot = $PSScriptRoot
$RunDir   = Join-Path $RepoRoot '.run'

# The other project's port. This script refuses to stop anything listening on
# it, whatever a PID file might claim.
$ForbiddenPort = 8000

function Write-Ok   { param([string]$Message) Write-Host "  [ OK ] $Message" -ForegroundColor Green }
function Write-Warn { param([string]$Message) Write-Host "  [WARN] $Message" -ForegroundColor Yellow }
function Write-Info { param([string]$Message) Write-Host "  [    ] $Message" -ForegroundColor DarkGray }
function Write-Fail { param([string]$Message) Write-Host "  [FAIL] $Message" -ForegroundColor Red }

function Get-ProtectedProcessIds {
    <# PIDs that must never be stopped: anything serving the other project's
       port, this shell, and its ancestors. #>
    $protected = New-Object 'System.Collections.Generic.HashSet[int]'
    [void]$protected.Add($PID)

    try {
        foreach ($connection in Get-NetTCPConnection -LocalPort $ForbiddenPort -State Listen -ErrorAction Stop) {
            [void]$protected.Add([int]$connection.OwningProcess)
        }
    } catch {
        # Nothing is listening on that port, or the cmdlet is unavailable.
    }

    # Walk up from this shell so we cannot stop whatever launched us.
    $current = $PID
    for ($depth = 0; $depth -lt 8; $depth++) {
        $info = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $current" -ErrorAction SilentlyContinue
        if (-not $info) { break }

        $parent = [int]$info.ParentProcessId
        if ($parent -le 0) { break }

        [void]$protected.Add($parent)
        $current = $parent
    }

    return $protected
}

function Read-PidRecord {
    param([string]$Path)

    try {
        return Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    } catch {
        return $null
    }
}

function Resolve-TrackedProcess {
    <# Return the live process the record describes, or $null.

       Identity is three facts, not one: the PID exists, the image name still
       matches, and the start time matches to within two seconds. Windows
       reuses PIDs, so the PID alone proves nothing. #>
    param($Record)

    $process = Get-Process -Id $Record.pid -ErrorAction SilentlyContinue
    if (-not $process) { return $null }
    if ($process.ProcessName -ne $Record.processName) { return $null }

    try {
        $recorded = [datetime]::Parse($Record.startTimeUtc).ToUniversalTime()
        if ([math]::Abs(($process.StartTime.ToUniversalTime() - $recorded).TotalSeconds) -gt 2) {
            return $null
        }
    } catch {
        return $null
    }

    return $process
}

function Get-TrackedDescendants {
    <# Descendants of $ParentId that started after it did.

       The start-time test matters: without it, a process that inherited a
       recycled parent PID would look like our child. #>
    param([int]$ParentId, [datetime]$ParentStartUtc)

    $snapshot = @(Get-CimInstance -ClassName Win32_Process -ErrorAction SilentlyContinue |
        Select-Object ProcessId, ParentProcessId, Name, CreationDate)

    $descendants = @()
    # A set, not a membership test against $descendants: an empty array has no
    # ProcessId property, and under Set-StrictMode reading one is an error.
    $seen = New-Object 'System.Collections.Generic.HashSet[int]'
    $frontier = @($ParentId)

    while ($frontier.Count -gt 0) {
        $next = @()

        foreach ($parent in $frontier) {
            foreach ($candidate in $snapshot) {
                if ([int]$candidate.ParentProcessId -ne [int]$parent) { continue }
                if (-not $seen.Add([int]$candidate.ProcessId)) { continue }

                if ($candidate.CreationDate) {
                    $created = ([datetime]$candidate.CreationDate).ToUniversalTime()
                    if ($created -lt $ParentStartUtc.AddSeconds(-2)) { continue }
                }

                $descendants += $candidate
                $next += [int]$candidate.ProcessId
            }
        }

        $frontier = $next
    }

    return $descendants
}

function Stop-TrackedService {
    param([string]$Name)

    $pidFile = Join-Path $RunDir "$Name.pid"

    if (-not (Test-Path -LiteralPath $pidFile)) {
        Write-Info "$Name : not running (no PID file)"
        return
    }

    $record = Read-PidRecord -Path $pidFile
    if (-not $record) {
        Remove-Item -LiteralPath $pidFile -Force
        Write-Warn "$Name : PID file was unreadable and has been removed."
        return
    }

    # A PID file claiming the other project's port is not something to act on.
    if ($record.PSObject.Properties.Name -contains 'port' -and [int]$record.port -eq $ForbiddenPort) {
        Write-Fail "$Name : the PID file names port $ForbiddenPort, which belongs to the other project. Refusing to stop it."
        return
    }

    $process = Resolve-TrackedProcess -Record $record
    if (-not $process) {
        Remove-Item -LiteralPath $pidFile -Force
        Write-Info "$Name : not running (stale PID file for PID $($record.pid) removed)"
        return
    }

    $protected = Get-ProtectedProcessIds
    if ($protected.Contains([int]$process.Id)) {
        Write-Fail "$Name : PID $($process.Id) is protected (this shell, an ancestor, or a listener on port $ForbiddenPort). Not stopped."
        return
    }

    $startUtc = $process.StartTime.ToUniversalTime()
    $children = @(Get-TrackedDescendants -ParentId $process.Id -ParentStartUtc $startUtc)

    $stopped = @()
    $skipped = @()

    # Children first, so the parent cannot respawn them on its way out.
    foreach ($child in $children) {
        $childId = [int]$child.ProcessId

        if ($protected.Contains($childId)) {
            $skipped += "$($child.Name) ($childId, protected)"
            continue
        }

        try {
            Stop-Process -Id $childId -Force -ErrorAction Stop
            $stopped += "$($child.Name) ($childId)"
        } catch {
            # Already gone, most likely: the parent took it down first.
            if (Get-Process -Id $childId -ErrorAction SilentlyContinue) {
                $skipped += "$($child.Name) ($childId, $($_.Exception.Message))"
            }
        }
    }

    try {
        Stop-Process -Id $process.Id -Force -ErrorAction Stop
        $stopped += "$($process.ProcessName) ($($process.Id))"
    } catch {
        # npm's cmd.exe wrapper exits by itself once its node child is gone, so
        # "no such process" here means the shutdown worked, not that it failed.
        if (Get-Process -Id $process.Id -ErrorAction SilentlyContinue) {
            Write-Fail "$Name : could not stop PID $($process.Id) - $($_.Exception.Message)"
            return
        }
        $stopped += "$($process.ProcessName) ($($process.Id), exited with its children)"
    }

    Remove-Item -LiteralPath $pidFile -Force

    $port = if ($record.PSObject.Properties.Name -contains 'port') { $record.port } else { '?' }
    Write-Ok "$Name : stopped on port $port - $($stopped -join ', ')"

    foreach ($entry in $skipped) {
        Write-Warn "$Name : left running - $entry"
    }
}

# ---------------------------------------------------------------------------

Write-Host ""
Write-Host "JobFinder - stopping services" -ForegroundColor Cyan

if (-not (Test-Path -LiteralPath $RunDir)) {
    Write-Info "Nothing to stop: .run\ does not exist (start_app.ps1 has not run here)."
    Write-Host ""
    exit 0
}

$services = if ($Service -eq 'all') { @('frontend', 'backend') } else { @($Service) }

foreach ($name in $services) {
    Stop-TrackedService -Name $name
}

# Any *.pid left behind by an older layout is reported, never acted on blindly.
$orphans = @(Get-ChildItem -LiteralPath $RunDir -Filter '*.pid' -ErrorAction SilentlyContinue |
    Where-Object { $_.BaseName -notin @('backend', 'frontend') })

foreach ($orphan in $orphans) {
    Write-Warn "Unrecognised PID file left in place: $($orphan.Name)"
}

if ($ClearLogs) {
    Get-ChildItem -LiteralPath $RunDir -Filter '*.log' -ErrorAction SilentlyContinue |
        Remove-Item -Force -ErrorAction SilentlyContinue
    Write-Info "Logs cleared."
} else {
    Write-Info "Logs kept in .run\ for inspection."
}

Write-Host ""
exit 0
