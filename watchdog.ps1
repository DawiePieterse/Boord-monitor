# Boord Monitor - watchdog: make sure the servers on this PC are actually
# answering, and restart the ones that are not.
#
# Run every 5 minutes via a Scheduled Task ("Boord Watchdog", registered by
# install.ps1 and by setup_watchdog.bat).
#
# It lived in Boord's repo until Monitor 2.0. It moved here so the farm has
# one place that keeps its apps running, and one table of those apps
# (apps.json, beside this file) instead of three copies kept in step by hand.
# It is still its own scheduled task and not part of the monitor's server on
# purpose: a crashed monitor must not also mean no restarts - and this is
# what restarts the monitor.
#
# Why this exists. Every app here is registered as "/sc onstart" - Windows
# starts it at boot and then never looks at it again. If uvicorn exits at
# 11:00 (an unhandled error, a bad update, Windows reclaiming memory), the
# task simply ends and nothing brings it back until somebody reboots the PC.
# On a pack house floor that is a morning of paper.
#
# This is NOT the same job as heartbeat.ps1. That one tells an outside
# service the server is alive so a human gets an email when it is not; it
# never touches the server. This one is the local half: it notices and puts
# the server back. They are complementary and both should be registered -
# a watchdog cannot report a PC that is switched off.
#
# What counts as "up": the port answers HTTP at all. Any status code - 200,
# 404, even 500 - means uvicorn is serving and this script leaves it alone.
# Only a refused connection or a timeout is treated as down. Restarting a
# server because one endpoint returns 500 would take a working pack house
# offline to fix a bug that a restart cannot fix.

param(
    # Report what is up and what is down, restart nothing. Used by
    # setup_watchdog.bat so the first run shows its work.
    [switch]$CheckOnly,
    # Restart this one app now, by its Name in the table below, whether or
    # not it is answering. Used by Boord Monitor's restart button, so that a
    # restart asked for from a phone goes through exactly the same stop,
    # prove-the-port-is-free and stand-down-during-updates steps as an
    # automatic one. Exits 0 when the app is answering again, 2 when it was
    # refused because an update or install is running, 1 otherwise.
    [string]$Restart = "",
    # A single missed probe is not an outage - a farm PC doing a virus scan
    # can stall a request past ten seconds. Only act after this many probes
    # in a row fail.
    [int]$Probes = 3,
    [int]$ProbeGapSeconds = 15,
    [int]$ProbeTimeoutSeconds = 10
)

$ErrorActionPreference = "Stop"

# The servers this PC may run, from apps.json - the same table the
# monitor's backend/apps.py reads. Port and task name are what install.ps1 in
# each project registers; the repo folder is read back from the task itself
# below, so the table does not have to know where anything is checked out.
# An app whose task is not registered on this PC is skipped, not reported as
# down - that is how one file covers a machine running two of these and a
# machine running all of them.
# Health is the path to probe. It is "/" for most, but Kudde refuses every
# request that did not arrive over Tailscale - including from this PC's own
# console - and answers only /healthz, which exists precisely to prove the
# process is up without saying anything about the herd. Probing "/" there
# gets a 403 from a perfectly healthy server.
$AppsFile = Join-Path $PSScriptRoot "apps.json"

$LogFile   = Join-Path (Join-Path $PSScriptRoot "data") "watchdog.log"
$StopPs1   = Join-Path $PSScriptRoot "stop_server.ps1"
$PauseFile = Join-Path (Join-Path $PSScriptRoot "data") "watchdog.pause"

# Command lines that mean somebody is deliberately working on a server right
# now. Restarting into the middle of an update is the one thing this script
# could do that is worse than the outage it is fixing: an updater stops the
# server on purpose so Alembic can migrate the database with nothing writing
# to it (see the header of stop_server.ps1), and a watchdog that helpfully
# starts it again puts a live server on top of a half-migrated schema. Every
# app's updater comes from apps.json (its "updater"), so an app added there
# is stood down for without anyone remembering to add it here.
$MaintenanceMarkers = @(
    "install.ps1",
    "uninstall.ps1",
    "stop_server.ps1",
    "alembic"
)

function Write-Log($msg) {
    $line = "{0}  {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Write-Host $line
    try {
        $dir = Split-Path -Parent $LogFile
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        # Keep one generation and cap it. This runs 288 times a day forever;
        # an uncapped log on a farm PC nobody administers is a disk-full
        # outage waiting a couple of years to happen.
        if ((Test-Path $LogFile) -and ((Get-Item $LogFile).Length -gt 1MB)) {
            Move-Item $LogFile "$LogFile.1" -Force
        }
        Add-Content -Path $LogFile -Value $line -Encoding ASCII
    } catch { }
}

function Test-AppUp($port, $path, $timeoutSeconds) {
    try {
        Invoke-WebRequest -Uri "http://127.0.0.1:$port$path" -UseBasicParsing `
            -TimeoutSec $timeoutSeconds | Out-Null
        return $true
    } catch {
        # An exception carrying a Response is an HTTP answer - 404, 500,
        # whatever. Something is listening and serving, so the server is up.
        # No Response means a refused connection, a DNS failure or a timeout:
        # nothing is there.
        #
        # Deliberately tests the property rather than catching a type. Windows
        # PowerShell 5.1 throws System.Net.WebException here; PowerShell 7
        # throws HttpResponseException. Both expose .Response and neither sets
        # it for a refused connection, but a "catch [System.Net.WebException]"
        # silently misses on 7 - so the farm (5.1) would read a 404 as up and
        # any machine used to test this script would read the same 404 as
        # down, which is the worst way to be wrong.
        if ($_.Exception.Response) { return $true }
        return $false
    }
}

function Get-TaskRepoRoot($taskName) {
    # The scheduled task's action is the full path to that project's
    # start_*.bat, which sits at the top of its repo. Reading it back beats
    # hardcoding folder names here: it stays correct if a farm checks a
    # project out somewhere unexpected, and it returns $null rather than a
    # wrong guess if anything about the task is not what we expect - and a
    # $null repo root is what stops this script from killing a process it
    # cannot prove is ours.
    try {
        $raw = cmd /c "schtasks /query /tn ""$taskName"" /xml ONE 2>nul"
        if ($LASTEXITCODE -ne 0 -or -not $raw) { return $null }
        $text = ($raw -join "`n").TrimStart([char]0xFEFF)
        $xml = [xml]$text
        $cmdPath = $xml.Task.Actions.Exec.Command
        if (-not $cmdPath) { return $null }
        $cmdPath = $cmdPath.Trim('"')
        if (-not (Test-Path $cmdPath)) { return $null }
        return (Split-Path -Parent $cmdPath)
    } catch {
        return $null
    }
}

function Test-TaskRegistered($taskName) {
    # Through cmd, because schtasks /query writes to stderr when the task is
    # absent, and $ErrorActionPreference = "Stop" turns a native command's
    # redirected stderr into a terminating error. install.ps1 documents the
    # trap at length; it is the same one here.
    cmd /c "schtasks /query /tn ""$taskName"" >nul 2>&1"
    return ($LASTEXITCODE -eq 0)
}

function Get-MaintenanceReason {
    if (Test-Path $PauseFile) {
        $age = (Get-Date) - (Get-Item $PauseFile).LastWriteTime
        if ($age.TotalHours -lt 12) {
            return "data\watchdog.pause exists (created $([int]$age.TotalMinutes) minutes ago)"
        }
        # A pause file left behind by an update that crashed would otherwise
        # disable every restart on this PC silently and permanently, which is
        # the failure this whole script is meant to prevent. After 12 hours
        # it is treated as forgotten rather than deliberate.
        Write-Log "NOTE: ignoring data\watchdog.pause - it is $([int]$age.TotalHours) hours old and looks forgotten. Delete it."
    }
    try {
        $procs = Get-CimInstance Win32_Process -ErrorAction Stop |
            Where-Object { $_.CommandLine }
    } catch {
        return $null
    }
    foreach ($p in $procs) {
        # Skip this very script and its own PowerShell host, or the marker
        # list would match the watchdog itself the moment it calls
        # stop_server.ps1.
        if ($p.ProcessId -eq $PID) { continue }
        if ($p.CommandLine -like "*watchdog.ps1*") { continue }
        foreach ($marker in $MaintenanceMarkers) {
            if ($p.CommandLine -like "*$marker*") {
                return "'$marker' is running (PID $($p.ProcessId))"
            }
        }
    }
    return $null
}

function Restart-App($app) {
    $name = $app.Name
    $repoRoot = Get-TaskRepoRoot $app.Task

    # Stop first, and prove it stopped. schtasks /end ends the launcher .bat;
    # the uvicorn process it spawned is separate and routinely outlives it,
    # still holding the port. Starting the task again on top of that gives a
    # second uvicorn that cannot bind, exits, and leaves the hung one in
    # place - the watchdog would then "restart" it every five minutes
    # forever and never fix anything. stop_server.ps1 is the piece that
    # already gets this right, so it does the work here too.
    if ($repoRoot -and (Test-Path $StopPs1)) {
        Write-Log "  stopping $name (port $($app.Port), repo $repoRoot)..."
        # $ErrorActionPreference is "Stop" for this script, and that turns a
        # native command's redirected stderr into a terminating error - so
        # "2>&1" on the line below would abort the watchdog the moment the
        # child PowerShell printed a single warning to stderr. Relax it for
        # the length of the call, since the whole point is to capture and log
        # whatever that child says.
        $prevEap = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        try {
            $out = & powershell -NoProfile -ExecutionPolicy Bypass -File $StopPs1 `
                -Port $app.Port -TaskName $app.Task -RepoRoot $repoRoot 2>&1
            $stopped = ($LASTEXITCODE -eq 0)
        } finally {
            $ErrorActionPreference = $prevEap
        }
        foreach ($l in $out) { if ("$l".Trim()) { Write-Log "    | $l" } }
        if (-not $stopped) {
            Write-Log "  REFUSING to restart $name - port $($app.Port) is still held by something that is not ours. Needs a person."
            return $false
        }
    } else {
        # No repo root means the task's action was not readable, so there is
        # no venv path to check a surviving process against. Rather than kill
        # whatever holds the port with SYSTEM rights, end the task and hope
        # the port comes free on its own.
        Write-Log "  could not read the repo folder from task '$($app.Task)' - ending the task without the leftover-process check"
        cmd /c "schtasks /end /tn ""$($app.Task)"" >nul 2>&1"
        Start-Sleep -Seconds 3
    }

    Write-Log "  starting $name..."
    cmd /c "schtasks /run /tn ""$($app.Task)"" >nul 2>&1"
    if ($LASTEXITCODE -ne 0) {
        Write-Log "  FAILED to start task '$($app.Task)' (schtasks exit $LASTEXITCODE)"
        return $false
    }

    # Confirm it actually answers rather than reporting success on the
    # strength of having asked. A cold start runs the database migrations, so
    # allow a generous minute. Timed against the clock rather than counting
    # iterations, because each probe can itself sit on a timeout.
    $deadline = (Get-Date).AddSeconds(60)
    while ((Get-Date) -lt $deadline) {
        if (Test-AppUp $app.Port $app.Health 3) {
            Write-Log "  RECOVERED: $name is answering on port $($app.Port) again."
            return $true
        }
        Start-Sleep -Seconds 2
    }
    Write-Log "  $name did not answer on port $($app.Port) within 60 seconds of being started. Will try again next run."
    return $false
}

# --- The app table --------------------------------------------------------
# Read every run, so an app added to apps.json by an update is watched from
# the next run on. A table that cannot be read is written to the log and the
# run fails: guessing a table here is how the old copies drifted apart.
try {
    $table = Get-Content -Path $AppsFile -Raw -Encoding UTF8 | ConvertFrom-Json
    $Apps = @($table.apps | ForEach-Object {
        @{ Name = $_.name; Port = [int]$_.port; Task = $_.task; Health = $_.health }
    })
    foreach ($u in ($table.apps | ForEach-Object { $_.updater } | Where-Object { $_ } | Sort-Object -Unique)) {
        $MaintenanceMarkers += $u
    }
} catch {
    Write-Log "FAILED to read $AppsFile - nothing watched this run: $($_.Exception.Message)"
    exit 1
}
if ($Apps.Count -eq 0) {
    Write-Log "FAILED: $AppsFile lists no apps - nothing watched this run."
    exit 1
}

# --- A restart asked for by hand ------------------------------------------
# Before the boot check: a person asking for a restart five minutes after a
# reboot has looked at the app and decided, which a timer cannot.
if ($Restart) {
    $app = $Apps | Where-Object { $_.Name -eq $Restart } | Select-Object -First 1
    if (-not $app) {
        Write-Log "Asked to restart '$Restart', which is not one of: $(($Apps | ForEach-Object { $_.Name }) -join ', ')."
        exit 1
    }
    if (-not (Test-TaskRegistered $app.Task)) {
        Write-Log "Asked to restart $($app.Name), but it is not installed on this PC (no '$($app.Task)' task)."
        exit 1
    }
    $reason = Get-MaintenanceReason
    if ($reason) {
        Write-Log "NOT restarting $($app.Name) as asked: $reason. A server is meant to be down during that."
        exit 2
    }
    # Two lines on purpose: Boord Monitor counts "Restarting <name>..." lines
    # to report how often each app was restarted, and a note on the end of
    # that line would become part of the name.
    Write-Log "Restart of $($app.Name) asked for by hand (Boord Monitor)."
    Write-Log "Restarting $($app.Name)..."
    if (Restart-App $app) { exit 0 }
    exit 1
}

# --- Don't fight the boot ------------------------------------------------
# All the tasks start at once at boot, each building a Python process and
# opening a database. A watchdog that probes 90 seconds in decides they are
# all down and restarts every one of them, on every reboot.
try {
    $uptime = (Get-Date) - (Get-CimInstance Win32_OperatingSystem).LastBootUpTime
    if (-not $CheckOnly -and $uptime.TotalMinutes -lt 5) {
        Write-Log "Skipped: this PC booted $([int]$uptime.TotalMinutes) minutes ago - giving the servers time to start."
        exit 0
    }
} catch { }

# --- Probe ---------------------------------------------------------------
$down = @()
$checked = 0
foreach ($app in $Apps) {
    if (-not (Test-TaskRegistered $app.Task)) {
        if ($CheckOnly) { Write-Log "$($app.Name): not installed on this PC (no '$($app.Task)' task) - not monitored." }
        continue
    }
    $checked++
    if (Test-AppUp $app.Port $app.Health $ProbeTimeoutSeconds) {
        if ($CheckOnly) { Write-Log "$($app.Name): UP on port $($app.Port)." }
        continue
    }
    # Failed once. Probe again before believing it, so a momentary stall does
    # not cost the pack house a restart.
    $up = $false
    for ($attempt = 2; $attempt -le $Probes; $attempt++) {
        Start-Sleep -Seconds $ProbeGapSeconds
        if (Test-AppUp $app.Port $app.Health $ProbeTimeoutSeconds) { $up = $true; break }
    }
    if ($up) {
        Write-Log "$($app.Name): missed a probe on port $($app.Port) but answered on retry - left alone."
        continue
    }
    Write-Log "$($app.Name): DOWN - no answer on port $($app.Port) after $Probes probes."
    $down += $app
}

if ($checked -eq 0) {
    Write-Log "None of the apps in apps.json has its task registered on this PC - nothing to watch."
    exit 0
}

if ($down.Count -eq 0) {
    if ($CheckOnly) { Write-Log "All $checked registered servers are up." }
    exit 0
}

if ($CheckOnly) {
    Write-Log "$($down.Count) of $checked server(s) are down. (-CheckOnly: nothing was restarted.)"
    exit 1
}

# --- Restart -------------------------------------------------------------
$reason = Get-MaintenanceReason
if ($reason) {
    Write-Log "NOT restarting anything: $reason. A server is meant to be down during that."
    exit 0
}

$recovered = 0
foreach ($app in $down) {
    Write-Log "Restarting $($app.Name)..."
    if (Restart-App $app) { $recovered++ }
}

Write-Log "Done: $recovered of $($down.Count) restarted successfully."
if ($recovered -lt $down.Count) { exit 1 }
exit 0
