@echo off
:: Boord Monitor - double-click to keep the servers on this PC running.
::
:: Registers a Scheduled Task ("Boord Watchdog") that runs watchdog.ps1 every
:: 5 minutes. That checks each server this PC has installed - every app in
:: apps.json - and restarts any that have stopped answering. install.bat
:: registers the same task; this is for re-registering it on its own.
::
:: This is the local half of keeping the farm online. The other half is
:: setup_heartbeat.bat, which emails you when the PC itself is off or off the
:: internet - something nothing running ON that PC can ever tell you. Register
:: both.
::
:: Run it once. It survives reboots and needs nobody logged on.

net session >nul 2>&1
if %errorLevel% neq 0 (
    echo This needs administrator rights to register the scheduled task - requesting them now...
    echo If Windows shows a User Account Control prompt, click "Yes".
    powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

if not exist "%~dp0watchdog.ps1" (
    echo.
    echo watchdog.ps1 not found next to this script. Run this from the
    echo project folder it was shipped in.
    echo.
    pause
    exit /b 1
)

schtasks /query /tn "Boord Watchdog" >nul 2>&1
if %errorLevel% equ 0 (
    schtasks /delete /tn "Boord Watchdog" /f >nul 2>&1
)
schtasks /create /tn "Boord Watchdog" /tr "powershell -NoProfile -ExecutionPolicy Bypass -File \"%~dp0watchdog.ps1\"" /sc minute /mo 5 /ru SYSTEM /rl highest /f >nul 2>&1
if %errorLevel% neq 0 (
    echo.
    echo Failed to register the scheduled task - see the error above.
    pause
    exit /b 1
)

echo.
echo ==^> Registered. Checking what this PC is running right now...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0watchdog.ps1" -CheckOnly

echo.
echo Done - "Boord Watchdog" will check every 5 minutes from now on and
echo restart anything that has stopped answering. What it did is written to
echo data\watchdog.log.
echo.
pause
