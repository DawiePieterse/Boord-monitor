# Boord Monitor - stop the server, and prove it actually stopped.
#
# Adapted from Boord's stop_server.ps1, which has the fuller history.
#
# "schtasks /end" ends the launcher (start_monitor_server.bat). The uvicorn
# process it spawned is a separate process and routinely outlives it, still
# listening on port 8050. The updater used to end the task, wait two seconds
# and start it again: the new uvicorn could not bind the port and exited, and
# the old one carried on running the previous release with nothing left to
# start it again if it then died - and a dead monitor is the one thing that
# cannot push to say so.
#
# Exit 0 means port $Port is genuinely free. Exit 1 means it is not, and the
# caller must not start another server on top of it.

param(
    [int]$Port = 8050,
    [string]$TaskName = "Boord Monitor Server",
    [int]$TimeoutSeconds = 20,
    [string]$RepoRoot = $PSScriptRoot
)

$ErrorActionPreference = "Stop"

$VenvDir = Join-Path (Join-Path $RepoRoot "backend") ".venv"

function Write-Step($msg) {
    Write-Host ""
    Write-Host "==> $msg" -ForegroundColor Cyan
}
function Write-Ok($msg)   { Write-Host "    $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "    $msg" -ForegroundColor Yellow }
function Write-Err($msg)  { Write-Host "    $msg" -ForegroundColor Red }

function Get-PortListeners($port) {
    # Returns the listening connections on $port, or an empty array. Wrapped
    # because Get-NetTCPConnection throws rather than returning nothing when
    # there is no match, and "nothing is listening" is the success case here.
    try {
        $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
        if ($null -eq $conns) { return @() }
        # `tailscale serve --https=8050` makes tailscaled listen on this port
        # too, bound to the PC's Tailscale address. That is the monitor's front
        # door, not a competing server, and it stays up while it restarts.
        # uvicorn binds loopback (install.ps1), so only these can be ours.
        # Without this filter the port never reads as free on a farm PC and
        # every stop is refused (Kudde found this first).
        $own = @('0.0.0.0', '::', '127.0.0.1', '::1')
        return @($conns | Where-Object { $own -contains $_.LocalAddress })
    } catch {
        return @()
    }
}

function Wait-ForPortFree($port, $seconds) {
    for ($i = 0; $i -lt $seconds; $i++) {
        if ((Get-PortListeners $port).Count -eq 0) { return $true }
        Start-Sleep -Seconds 1
    }
    return (Get-PortListeners $port).Count -eq 0
}

# --- 1: end the scheduled task, if it is registered ---------------------------
# Through cmd: schtasks writes to stderr when the task is absent or already
# stopped, and $ErrorActionPreference = "Stop" turns a native command's
# redirected stderr into a terminating error. install.ps1 documents this trap
# at length - it is what used to kill the installer on first-time installs.
cmd /c "schtasks /query /tn ""$TaskName"" >nul 2>&1"
if ($LASTEXITCODE -eq 0) {
    Write-Step "Stopping the '$TaskName' task..."
    cmd /c "schtasks /end /tn ""$TaskName"" >nul 2>&1"
} else {
    Write-Step "No '$TaskName' task registered - checking port $Port anyway..."
}

# --- 2: wait for the port to actually come free -------------------------------
if (Wait-ForPortFree $Port $TimeoutSeconds) {
    Write-Ok "Port $Port is free - the server is stopped."
    exit 0
}

# --- 3: stop what is left, but only if it is ours -----------------------------
# Deliberately NOT a blind kill of whatever owns the port. This script runs as
# SYSTEM from an automated path, and something else on the PC could have taken
# 8050 - killing an unrelated service with SYSTEM rights is a worse outcome
# than refusing to update. So: only processes running from this repo's own
# virtual environment.
Write-Warn "Port $Port is still held after $TimeoutSeconds seconds - looking at what holds it."

$refused = @()
foreach ($conn in Get-PortListeners $Port) {
    $ownerPid = $conn.OwningProcess
    $proc = $null
    try { $proc = Get-Process -Id $ownerPid -ErrorAction Stop } catch { }
    if ($null -eq $proc) { continue }

    $path = $null
    try { $path = $proc.Path } catch { }   # access denied on some processes

    if ($path -and $path.StartsWith($VenvDir, [StringComparison]::OrdinalIgnoreCase)) {
        try {
            Stop-Process -Id $ownerPid -Force -ErrorAction Stop
            Write-Ok "Stopped the leftover server process (PID $ownerPid, $path)"
        } catch {
            Write-Err "Could not stop PID $ownerPid ($path): $($_.Exception.Message)"
            $refused += "PID $ownerPid ($path)"
        }
    } else {
        $shown = if ($path) { $path } else { "$($proc.ProcessName) (path not readable)" }
        Write-Err "PID $ownerPid does not run from $VenvDir - not ours: $shown"
        $refused += "PID $ownerPid ($shown)"
    }
}

if ((Wait-ForPortFree $Port 10) -and $refused.Count -eq 0) {
    Write-Ok "Port $Port is free - the server is stopped."
    exit 0
}

Write-Host ""
Write-Err "Port $Port is STILL in use. The server has not been stopped."
foreach ($r in $refused) { Write-Err "  $r" }
Write-Err "Nothing that touches the database may run while this is true."
Write-Err "Stop that process by hand, then try again."
exit 1
