@echo off
:: Boord Monitor - double-click to set up uptime alerting.
:: Registers a Scheduled Task that runs heartbeat.ps1 every 10 minutes.
:: See MANUAL.md, "The heartbeat", for the healthchecks.io account setup
:: this depends on - do that first (including creating
:: data\heartbeat_url.txt in this folder), then run this once.
::
:: One check per application: every app in apps.json can have its own ping
:: URL in data\heartbeat_url.txt. Apps with no URL are simply not reported on.
::
:: This alerts. It does not fix. setup_watchdog.bat is the other half - it
:: restarts a server that has stopped, which this cannot do, while this
:: reaches you when the PC itself is off, which the watchdog cannot do.

net session >nul 2>&1
if %errorLevel% neq 0 (
    echo This needs administrator rights to register the scheduled task - requesting them now...
    echo If Windows shows a User Account Control prompt, click "Yes".
    powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

if not exist "%~dp0data\heartbeat_url.txt" (
    echo.
    echo data\heartbeat_url.txt not found in this folder.
    echo See MANUAL.md, "The heartbeat", to create your
    echo healthchecks.io check and save its ping URL there first.
    echo.
    pause
    exit /b 1
)

schtasks /query /tn "Boord Heartbeat" >nul 2>&1
if %errorLevel% equ 0 (
    schtasks /delete /tn "Boord Heartbeat" /f >nul 2>&1
)
schtasks /create /tn "Boord Heartbeat" /tr "powershell -NoProfile -ExecutionPolicy Bypass -File \"%~dp0heartbeat.ps1\"" /sc minute /mo 10 /ru SYSTEM /rl highest /f >nul 2>&1
if %errorLevel% neq 0 (
    echo.
    echo Failed to register the scheduled task - see the error above.
    pause
    exit /b 1
)

echo.
echo ==^> Reading data\heartbeat_url.txt back, so a typo shows up now...
echo     ^(nothing is pinged by this - it only reports what it read^)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0heartbeat.ps1" -DryRun

echo.
echo Done - "Boord Heartbeat" is registered and will run every
echo 10 minutes from now on, checking in with healthchecks.io for each
echo application listed above that has a ping URL.
echo.
echo If an application says "no check configured" and you wanted it
echo watched, add a line for it to data\heartbeat_url.txt and run this again.
echo.
pause
