# Boord Monitor - Windows server installer (the farm PC)
#
# Sets up the monitor to run beside the other Boord apps on the same PC:
# installs Python if needed, creates the virtual environment, installs
# dependencies, finds Boord's folder (for its watchdog), sets the admin PIN,
# writes the launcher and registers the monitor to auto-start at boot as
# SYSTEM. Safe to re-run - each step checks what is already done.
#
# SYSTEM, like its siblings, and for a reason of its own: the restart button
# runs Boord's watchdog.ps1, which ends and starts other SYSTEM tasks and
# stops their processes. An ordinary account could look but not act.
#
# Run via install.bat, which handles the administrator-elevation prompt.

$ErrorActionPreference = "Stop"

$RepoRoot = $PSScriptRoot
$BackendDir = Join-Path $RepoRoot "backend"
$VenvDir = Join-Path $BackendDir ".venv"
$DataDir = Join-Path $RepoRoot "data"
$Port = 8050
$TaskName = "Boord Monitor Server"
$PythonVersion = "3.11.9"
$PythonInstallerUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-amd64.exe"
$LauncherPath = Join-Path $RepoRoot "start_monitor_server.bat"
$ReleaseKeyPath = Join-Path $RepoRoot "release-key.asc"
$FprFile = Join-Path $DataDir "release_key.fpr"
# The default guess for Boord's folder, if Boord is checked out beside this repo.
$DefaultBoordDir = Join-Path (Split-Path $RepoRoot -Parent) "Boord"

function Write-Step($m) { Write-Host ""; Write-Host "==> $m" -ForegroundColor Cyan }
function Write-Ok($m)   { Write-Host "    $m" -ForegroundColor Green }
function Write-Warn($m) { Write-Host "    $m" -ForegroundColor Yellow }
function Write-Err($m)  { Write-Host "    $m" -ForegroundColor Red }

function Test-PythonOk($exe) {
    if (-not $exe -or -not (Test-Path $exe)) { return $false }
    try {
        $out = & $exe -c "import sys; print(sys.version_info[0]); print(sys.version_info[1]); print('64BIT' if sys.maxsize > 2**32 else '32BIT')" 2>$null
        if (-not $out -or $out.Count -lt 3) { return $false }
        return ([int]$out[0] -eq 3 -and [int]$out[1] -ge 9 -and $out[2].Trim() -eq "64BIT")
    } catch { return $false }
}

try {
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Err "This script needs to run as Administrator. Run install.bat instead."
        exit 1
    }

    Write-Host ""
    Write-Host "Boord Monitor - Server Installer" -ForegroundColor Cyan
    Write-Host "================================================" -ForegroundColor Cyan

    # --- Step 1: Find or install Python ---
    Write-Step "Checking for Python 3.9+ (64-bit)..."
    $pythonExe = $null
    $existing = Get-Command python -ErrorAction SilentlyContinue
    if ($existing -and (Test-PythonOk $existing.Source)) {
        $pythonExe = $existing.Source
        Write-Ok "Found a compatible Python at $pythonExe"
    } else {
        $wellKnown = Join-Path $env:ProgramFiles "Python311\python.exe"
        if (Test-PythonOk $wellKnown) {
            $pythonExe = $wellKnown
            Write-Ok "Found a compatible Python at $pythonExe"
        } else {
            Write-Warn "No compatible 64-bit Python 3.9+ found - downloading Python $PythonVersion..."
            $installer = Join-Path $env:TEMP "python-$PythonVersion-amd64.exe"
            Invoke-WebRequest -Uri $PythonInstallerUrl -OutFile $installer -UseBasicParsing
            Start-Process -FilePath $installer -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_test=0" -Wait
            Remove-Item $installer -ErrorAction SilentlyContinue
            $pythonExe = $wellKnown
            if (-not (Test-PythonOk $pythonExe)) {
                Write-Err "Python installation could not be confirmed. Install Python 3.9+ (64-bit) from python.org and re-run."
                exit 1
            }
            Write-Ok "Installed Python at $pythonExe"
        }
    }

    # --- Step 2: Git and GnuPG (both only needed by update_monitor_server.bat) ---
    #
    # Neither is required to RUN the server, so nothing here is fatal - a farm
    # that cannot update is still a farm that works. They are checked at
    # install time anyway because the alternative is finding out months later,
    # in the middle of wanting an update.
    Write-Step "Checking for Git..."
    if (Get-Command git -ErrorAction SilentlyContinue) {
        Write-Ok "Found Git"
    } else {
        Write-Warn "Git is not installed. The server will still run, but update_monitor_server.bat"
        Write-Warn "cannot fetch updates without it. Get it from https://git-scm.com/download/win"
    }

    # Updates are signed-tag only (see update_monitor_server.bat), so the update
    # path needs a working gpg. Git for Windows bundles one, but it keeps keys
    # in a keyboxd daemon the Git distribution does not ship, so it fails
    # exactly like a bad signature does - which sends people looking for an
    # attacker instead of an installer. Reject it by path, same as Boord does.
    Write-Step "Checking for GnuPG (used to verify signed releases)..."
    $gpgExe = $null
    $gpgCmd = Get-Command gpg -ErrorAction SilentlyContinue
    if ($gpgCmd -and $gpgCmd.Source -notlike "*\Git\usr\bin\*") {
        $gpgExe = $gpgCmd.Source
        Write-Ok "Found GnuPG at $gpgExe"
    } else {
        foreach ($candidate in @((Join-Path $env:ProgramFiles "GnuPG\bin\gpg.exe"),
                                 (Join-Path ${env:ProgramFiles(x86)} "GnuPG\bin\gpg.exe"))) {
            if ((Test-Path $candidate) -and -not $gpgExe) { $gpgExe = $candidate }
        }
        if ($gpgExe) {
            Write-Ok "Found GnuPG at $gpgExe"
        } else {
            Write-Warn "GnuPG not found. The server runs fine without it, but"
            Write-Warn "update_monitor_server.bat cannot verify a release and will refuse to"
            Write-Warn "install one. Boord's own install.bat installs Gpg4win and points git"
            Write-Warn "at it - if this PC runs Boord, run that once and this is handled."
            Write-Warn "Otherwise: https://gpg4win.org"
        }
    }

    # Importing the public key from the repo is safe: what actually decides
    # which releases are trusted is the fingerprint in data\release_key.fpr,
    # which lives outside the repo. A swapped key would not match it and
    # update_monitor_server.bat would refuse the release.
    if ($gpgExe -and (Test-Path $ReleaseKeyPath)) {
        try {
            # Not -Wait, and time-limited. GnuPG starts keyboxd and gpg-agent on
            # its first run, and on a machine whose keyring has just been created
            # that start-up can hang indefinitely with its output redirected into
            # a non-interactive process. Importing the key is a convenience; it
            # must never be able to block the install.
            $gpgLog = Join-Path $env:TEMP "boord-monitor-gpg-import.log"
            $proc = Start-Process -FilePath $gpgExe `
                -ArgumentList @("--batch", "--yes", "--import", $ReleaseKeyPath) `
                -NoNewWindow -PassThru `
                -RedirectStandardError $gpgLog -RedirectStandardOutput "$gpgLog.out"
            try { $null = $proc.Handle } catch { }
            if ($proc.WaitForExit(60000)) {
                $exit = $null
                try { $exit = $proc.ExitCode } catch { }
                if ($exit -eq 0) { Write-Ok "Imported the release key" }
                else { Write-Warn "Importing release-key.asc did not report success - see $gpgLog" }
            } else {
                try { $proc.Kill() } catch { }
                Write-Warn "gpg did not finish within 60 seconds - skipped the key import."
                Write-Warn "Run this once in a Command Prompt, which lets it finish:"
                Write-Warn "    ""$gpgExe"" --import release-key.asc"
            }
            Remove-Item "$gpgLog.out" -ErrorAction SilentlyContinue
        } catch {
            Write-Warn "Could not import release-key.asc: $($_.Exception.Message)"
        }
    }

    # --- Step 3: Locate Boord's folder ---
    # The monitor reads Boord's data\watchdog.log and runs its watchdog.ps1
    # for the restart button. Read from the "Boord Server" task first, the
    # way watchdog.ps1 finds every app's folder, so an unusual checkout
    # location is still found. Not fatal if it is missing: everything but the
    # restart button and the watchdog log works without it.
    Write-Step "Locating Boord's folder (for its watchdog)..."
    $boordDir = $null
    try {
        $raw = cmd /c "schtasks /query /tn ""Boord Server"" /xml ONE 2>nul"
        if ($LASTEXITCODE -eq 0 -and $raw) {
            $xml = [xml](($raw -join "`n").TrimStart([char]0xFEFF))
            $cmdPath = "$($xml.Task.Actions.Exec.Command)".Trim('"')
            if ($cmdPath -and (Test-Path $cmdPath)) { $boordDir = Split-Path -Parent $cmdPath }
        }
    } catch { }
    if (-not $boordDir -and (Test-Path (Join-Path $DefaultBoordDir "watchdog.ps1"))) {
        $boordDir = (Resolve-Path $DefaultBoordDir).Path
    }
    if ($boordDir -and (Test-Path (Join-Path $boordDir "watchdog.ps1"))) {
        Write-Ok "Found it at $boordDir"
    } else {
        Write-Warn "Could not find Boord's folder. The monitor will still watch every app,"
        Write-Warn "but the Restart button and the watchdog log need it."
        $entered = Read-Host "Full path to Boord's folder (e.g. C:\Boord), or Enter to skip"
        if ($entered -and (Test-Path (Join-Path $entered "watchdog.ps1"))) {
            $boordDir = (Resolve-Path $entered).Path
        } else {
            $boordDir = $DefaultBoordDir
            Write-Warn "Skipped. Re-run this installer once Boord is installed."
        }
    }

    # --- Step 4: Virtual environment ---
    Write-Step "Setting up the app's virtual environment..."
    if (-not (Test-Path $VenvDir)) { & $pythonExe -m venv $VenvDir; Write-Ok "Created virtual environment" }
    else { Write-Ok "Virtual environment already exists" }
    $venvPython = Join-Path $VenvDir "Scripts\python.exe"
    # Deliberately NOT $VenvDir\Scripts\pip.exe. That is a launcher stub
    # generated when the venv is created - a brand-new unsigned executable,
    # which Windows Application Control (Smart App Control / WDAC / AppLocker)
    # blocks outright on a machine that enforces one: "Program 'pip.exe'
    # failed to run: An Application Control policy has blocked this file".
    # The venv's python.exe is a COPY of the Python Software Foundation
    # binary and keeps its Authenticode signature, so `python -m pip` runs
    # where `pip.exe` cannot. Same pip, same result, one fewer unsigned
    # binary - so this is the right call even where nothing is enforcing.

    # --- Step 5: Dependencies ---
    Write-Step "Installing app dependencies (first run can take a few minutes)..."
    & $venvPython -m pip install --quiet --disable-pip-version-check -r (Join-Path $BackendDir "requirements.txt")
    if ($LASTEXITCODE -ne 0) {
        Write-Err "Dependency install failed."
        Write-Err "Usually this is no internet, or pip being blocked by a proxy."
        Write-Err "If the error above mentions an Application Control policy, this PC"
        Write-Err "enforces one and has blocked a binary inside .venv. Nothing here"
        Write-Err "needs the policy relaxed - tell whoever manages it which file was"
        Write-Err "blocked. Re-running this installer is safe; it reuses the venv."
        exit 1
    }
    Write-Ok "Dependencies installed"

    # --- Step 5b: Admin PIN ---
    # The restart button and turning on alerts sit behind this PIN; looking
    # is open to the tailnet. Set here, at the server, because the web page
    # deliberately cannot set the first one (backend/admin_auth.py). A re-run keeps the
    # existing PIN - backend\set_pin.py changes it.
    if (-not (Test-Path (Join-Path $DataDir "admin_pin.txt"))) {
        Write-Step "Choose the admin PIN (at least 4 characters)..."
        Push-Location $BackendDir
        & $venvPython set_pin.py
        Pop-Location
        if ($LASTEXITCODE -ne 0) {
            Write-Warn "No PIN was set. Restarts and alerts stay locked until you run, in the backend folder:"
            Write-Warn "    .venv\Scripts\python set_pin.py"
        }
    } else {
        Write-Ok "Admin PIN already set (backend\set_pin.py changes it)"
    }

    # --- Step 6: Launcher ---
    Write-Step "Creating the server launcher..."
    # --host 127.0.0.1 is load-bearing, not a default. Looking needs no
    # sign-in, so whoever can reach this port can read the state of every
    # app on this PC. Binding loopback means the only way in is `tailscale
    # serve`, reachable inside the tailnet only.
    $launcher = @"
@echo off
cd /d "$BackendDir"
set "MONITOR_BOORD_DIR=$boordDir"
"$venvPython" -m uvicorn main:app --host 127.0.0.1 --port $Port
"@
    Set-Content -Path $LauncherPath -Value $launcher -Encoding ASCII
    Write-Ok "Created $LauncherPath"

    # --- Step 7: Scheduled task (auto-start at boot, as SYSTEM - see the top) ---
    Write-Step "Registering the server to start automatically with Windows..."
    cmd /c "schtasks /query /tn ""$TaskName"" >nul 2>&1"
    if ($LASTEXITCODE -eq 0) {
        cmd /c "schtasks /end /tn ""$TaskName"" >nul 2>&1"
        Start-Sleep -Seconds 1
        schtasks /delete /tn "$TaskName" /f | Out-Null
    }
    schtasks /create /tn "$TaskName" /tr "`"$LauncherPath`"" /sc onstart /ru SYSTEM /rl highest /f | Out-Null
    # schtasks can't set these (copied from BoordOwner's install.ps1). The
    # default execution time limit (72h) would otherwise have Windows stop the
    # monitor three days after every boot - and a stopped monitor is the one
    # thing that can't push to say it has stopped; the farm would only find
    # out from the off-site watcher, or not at all. If the process dies (a
    # crash, Python half-updated), the task must start it again rather than
    # leave the farm unwatched until the next reboot. The battery flags undo
    # schtasks' laptop defaults, which would refuse to start it - or kill it -
    # on a PC running from a UPS. Best-effort: an old PowerShell without these
    # cmdlets still gets the task itself.
    try {
        $taskSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
            -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1) `
            -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
        Set-ScheduledTask -TaskName $TaskName -Settings $taskSettings | Out-Null
        Write-Ok "Scheduled task '$TaskName' registered (runs at every startup, no one needs to log in, restarts itself if it stops)"
    } catch {
        Write-Warn "Task registered, but couldn't set its restart policy: $($_.Exception.Message)"
        Write-Warn "Windows will stop the monitor 72 hours after each boot until this is fixed."
    }

    # --- Step 8: Start it now ---
    Write-Step "Starting the server now..."
    schtasks /run /tn "$TaskName" | Out-Null

    # --- Step 9: Confirm it answers ---
    $serverUp = $false
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Seconds 1
        try {
            $resp = Invoke-WebRequest -Uri "http://localhost:$Port/api/version" -UseBasicParsing -TimeoutSec 3
            if ($resp.StatusCode -eq 200) { $serverUp = $true; break }
        } catch { }
    }
    if ($serverUp) { Write-Ok "Server is up and answering on port $Port" }
    else {
        Write-Warn "The server did not answer on port $Port within 20 seconds."
        Write-Warn "Run start_monitor_server.bat directly in a window - errors print there"
        Write-Warn "rather than being swallowed by the Scheduled Task."
    }

    # --- Step 10: Report the address and how to publish it ---
    Write-Host ""
    Write-Host "================================================" -ForegroundColor Cyan
    Write-Host " Setup complete!" -ForegroundColor Green
    Write-Host "================================================" -ForegroundColor Cyan
    Write-Host ""
    Write-Host " On this PC: http://localhost:$Port/"
    Write-Host ""
    Write-Host " From anywhere else, publish it over Tailscale:"
    Write-Host ""
    Write-Host "   `"C:\Program Files\Tailscale\tailscale.exe`" serve --bg --https=8050 http://localhost:$Port/" -ForegroundColor Yellow
    Write-Host ""
    Write-Host " It then answers at https://<machine>.<tailnet>.ts.net:8050/ on the tailnet."
    Write-Host " Check the whole table with: tailscale serve status - the monitor's"
    Write-Host " 'Published ports' card does the same thing every minute."
    Write-Host ""
    Write-Warn "Use 'serve', NEVER 'funnel'. Funnel would publish the state of every"
    Write-Warn "app on this PC on the open internet."
    Write-Host ""
    $tokenFile = Join-Path $DataDir "remote_token.txt"
    if (Test-Path $tokenFile) {
        Write-Host " For the off-site watcher (MANUAL.md, 'The off-site watcher'), its token is:"
        Write-Host ""
        Write-Host "   $((Get-Content $tokenFile -TotalCount 1).Trim())" -ForegroundColor Cyan
        Write-Host ""
        Write-Warn "Treat it like a password: it lets the watcher send alerts to your phones."
        Write-Host ""
    }
    Write-Host " The server will now start automatically every time this PC turns on."
    Write-Host " update_monitor_server.bat installs the newest SIGNED release and restarts it."

    # --- Step 11: The trust root for updates ---
    #
    # Deliberately NOT written automatically. What this fingerprint decides is
    # which code this machine will accept in future, and this installer came
    # out of the very repository those updates come from - so a fingerprint it
    # wrote for you would be the repo vouching for itself. Typing it is the one
    # step that has to be a person's decision.
    #
    # Boord's own release_key.fpr is a different matter: it is outside both
    # repos and a human already put it there, for the same publisher and the
    # same key. Offering it saves the "where do I get the fingerprint" problem
    # without making the choice for anybody.
    if (-not (Test-Path $FprFile)) {
        $boordFpr = $null
        try {
            $candidate = Join-Path $boordDir "data\release_key.fpr"
            if (Test-Path $candidate) { $boordFpr = (Get-Content $candidate -TotalCount 1).Trim() }
        } catch { }

        Write-Host ""
        Write-Host "================================================" -ForegroundColor Yellow
        Write-Host " One step left: trust the release key" -ForegroundColor Yellow
        Write-Host "================================================" -ForegroundColor Yellow
        Write-Host " update_monitor_server.bat will refuse to install anything until this"
        Write-Host " server knows which signing key to trust. The server you just"
        Write-Host " installed runs fine without this - only updates need it."
        Write-Host ""
        if ($boordFpr) {
            Write-Host " Boord on this PC already trusts this key. Same publisher, same"
            Write-Host " key - so in this folder, run:"
            Write-Host ""
            Write-Host "     >data\release_key.fpr echo $boordFpr" -ForegroundColor Cyan
        } else {
            Write-Host " In this folder, run:"
            Write-Host ""
            Write-Host "     >data\release_key.fpr echo <FINGERPRINT>" -ForegroundColor Cyan
            Write-Host ""
            Write-Host " ...with the 40-character fingerprint from whoever maintains this"
            Write-Host " install."
        }
        Write-Host ""
        Write-Warn "Type it with the redirect first, exactly as shown. cmd reads a digit"
        Write-Warn "written immediately before a > as a file handle number, so the more"
        Write-Warn "natural 'echo <FINGERPRINT>> file' drops the fingerprint's last"
        Write-Warn "character whenever it is a digit - and the next update then fails its"
        Write-Warn "signature check, which reads as tampering rather than as a typo."
    } else {
        $fpr = (Get-Content $FprFile -TotalCount 1).Trim()
        Write-Ok "Release key fingerprint on file: $fpr"
    }
} catch {
    Write-Host ""
    Write-Err "Something went wrong:"
    Write-Err $_.Exception.Message
    exit 1
}
