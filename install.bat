@echo off
:: Boord Monitor - double-click to install the monitor on the farm PC.
:: It runs BESIDE the Boord apps (its own port, its own auto-start task)
:: and only looks at them. Install Boord first for the restart button.
::
:: "-ExecutionPolicy Bypass" is scoped to this one invocation - it changes
:: no system-wide setting; Windows blocks local PowerShell scripts by default.

net session >nul 2>&1
if %errorLevel% neq 0 (
    echo This installer needs administrator rights - requesting them now...
    echo If Windows shows a User Account Control prompt, click "Yes".
    powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
echo.
pause
