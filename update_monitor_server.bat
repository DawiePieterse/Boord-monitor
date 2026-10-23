@echo off
setlocal EnableDelayedExpansion
:: Boord Monitor - double-click this file to update to the newest signed
:: release from GitHub, install any new dependencies, and restart the
:: monitor so the update takes effect. The monitor has no database, so
:: unlike its siblings there is no migration step.
::
:: This deliberately does NOT "git pull" a branch. A branch pull trusts
:: whoever can push to the repo, and the service this restarts runs as
:: SYSTEM - so a stolen GitHub token would mean code execution on every farm
:: running Boord Monitor. Instead it checks out a tag carrying a GPG signature
:: from the release key, and refuses to update at all if that signature is
:: missing, broken, or made by any other key. Pushing code is then not enough
:: to ship it; you also have to hold the signing key.
::
:: This is the same scheme, and the same key, as Boord's own
:: update_server.bat - the two apps are one publisher, and a farm that has
:: already trusted that key for Boord does not have to decide twice.

net session >nul 2>&1
if %errorLevel% neq 0 (
    echo This needs administrator rights to restart the server - requesting them now...
    echo If Windows shows a User Account Control prompt, click "Yes".
    powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

cd /d "%~dp0"
set "TASK=Boord Monitor Server"
set "VENV_PY=%~dp0backend\.venv\Scripts\python.exe"

:: The fingerprint of the key allowed to sign releases for THIS server. It
:: lives in data\ rather than in the checkout on purpose: a file inside the
:: repo would be rewritten by the very update it is supposed to be vouching
:: for, so an attacker who could push could also swap the fingerprint for
:: their own. data\ is gitignored, so a checkout never touches it - it is set
:: once by hand and only changes if you deliberately rotate the release key.
set "FPR_FILE=%~dp0data\release_key.fpr"
set "RELEASE_FPR="

echo.
echo ==^> Checking this server can verify signed releases...
if not exist "%FPR_FILE%" (
    echo.
    echo No release key fingerprint found at:
    echo     %FPR_FILE%
    echo.
    echo This server cannot tell a genuine release from a tampered one, so it
    echo will not update. Set it once, from this folder - type it with the
    echo redirect first, exactly as shown, which keeps a fingerprint ending
    echo in a digit from having that digit eaten as a file handle number:
    echo.
    echo     ^>data\release_key.fpr echo ^<FINGERPRINT^>
    echo.
    echo If this PC already runs Boord, it is the same key: copy the
    echo fingerprint out of Boord's own data\release_key.fpr.
    echo.
    echo The server has NOT been restarted and is still running whatever it
    echo was running before.
    pause
    exit /b 1
)
for /f "usebackq eol=# tokens=1 delims= " %%F in ("%FPR_FILE%") do (
    if not defined RELEASE_FPR set "RELEASE_FPR=%%F"
)
if not defined RELEASE_FPR (
    echo %FPR_FILE% is empty - expected a 40-character key fingerprint.
    echo Not updating.
    pause
    exit /b 1
)
echo     Only releases signed by !RELEASE_FPR! will be accepted.

:: Probe that git can actually run gpg before relying on it. A missing or
:: broken GnuPG makes verify-tag fail exactly like a bad signature does, so
:: without this check a farm with no gpg installed is told its release looks
:: tampered with - which sends them looking for an attacker instead of an
:: installer. Git for Windows' own bundled gpg fails this way too: it stores
:: keys in a keyboxd daemon the Git distribution does not ship.
set "GPG_PROG="
for /f "delims=" %%G in ('git config --get gpg.program 2^>nul') do set "GPG_PROG=%%G"
if not defined GPG_PROG set "GPG_PROG=gpg"
"%GPG_PROG%" --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo GnuPG is not working on this server, so signed releases cannot be
    echo checked. Nothing has been updated.
    echo.
    echo Tried: !GPG_PROG!
    echo.
    echo Install Gpg4win from https://gpg4win.org, then point git at it:
    echo     git config --global gpg.program "C:/Program Files/GnuPG/bin/gpg.exe"
    echo.
    echo Boord's own install.bat does both of these for you, and this app
    echo uses the same setting.
    pause
    exit /b 1
)

echo.
echo ==^> Fetching signed releases from GitHub...
:: --force so a retagged release is picked up rather than silently keeping
:: the stale local tag. It still has to pass the signature check below.
::
:: -c gc.auto=0: a plain fetch triggers git's automatic gc, whose prune step
:: on Windows tries to rmdir the now-empty .git\objects\XX folders. While
:: Defender, the search indexer or a backup agent is mid-scan of .git it
:: holds a handle on one, the rmdir gets "access denied", and Git for
:: Windows stops to ask "Deletion of directory '.git/objects/00' failed.
:: Should I try again? ^(y/n^)" - hanging this window with nobody to answer.
:: This script only needs the refs; it has no business repacking a farm's
:: repo, so turn that gc off for the fetch. Cleaning loose objects is a
:: deliberate, server-stopped maintenance job, not an update side effect.
git -c gc.auto=0 fetch --tags --force origin
if %errorLevel% neq 0 (
    echo.
    echo Fetch failed - check the error above ^(no internet, or this folder
    echo isn't a git checkout^). The server has NOT been restarted, so it's
    echo still running whatever it was before.
    echo.
    echo If it says "Permission denied ^(publickey^)", this is the SSH deploy
    echo key: this window is elevated, and an elevated session run as a
    echo DIFFERENT administrator account has a different %%USERPROFILE%%, with
    echo no key in its .ssh folder.
    pause
    exit /b 1
)

:: Newest release tag by version order, not by date - a tag's date is
:: attacker-controlled, its version number is what humans reason about.
set "NEWTAG="
for /f "delims=" %%T in ('git tag --list "v*" --sort^=-v:refname 2^>nul') do (
    if not defined NEWTAG set "NEWTAG=%%T"
)
if not defined NEWTAG (
    echo.
    echo No release tags ^(v*^) found in this repository. Nothing to update to.
    echo The server has NOT been restarted.
    pause
    exit /b 1
)

set "CURTAG=not on a release tag"
for /f "delims=" %%C in ('git describe --tags --exact-match --match "v*" HEAD 2^>nul') do set "CURTAG=%%C"

echo.
echo     Currently running: !CURTAG!
echo     Newest release:    !NEWTAG!

if "!CURTAG!"=="!NEWTAG!" (
    echo     Already on the newest signed release - skipping the code update.
) else (
    echo.
    echo ==^> Verifying the signature on !NEWTAG!...
    :: --raw prints GPG's machine-readable status lines. A VALIDSIG line means
    :: the signature is good; requiring OUR fingerprint on that line is the
    :: part that matters, because a plain "good signature" only proves the tag
    :: was signed by *some* key present in this machine's keyring.
    set "VERIFY_OUT=%TEMP%\boord_monitor_verify.txt"
    git verify-tag --raw "!NEWTAG!" > "!VERIFY_OUT!" 2>&1
    findstr /C:"VALIDSIG" "!VERIFY_OUT!" > "!VERIFY_OUT!.sig"
    findstr /I /C:"!RELEASE_FPR!" "!VERIFY_OUT!.sig" >nul
    if errorlevel 1 (
        findstr /C:"NO_PUBKEY" "!VERIFY_OUT!" >nul
        if not errorlevel 1 (
            echo.
            echo The release key is not in this server's keyring, so !NEWTAG!
            echo cannot be checked. This is NOT a sign of tampering - the key
            echo simply has not been imported on this machine yet.
            echo.
            echo Import it and run this again:
            echo     "!GPG_PROG!" --import release-key.asc
        ) else (
            echo.
            echo *** SIGNATURE CHECK FAILED for !NEWTAG! ***
            echo.
            echo This release is not signed by the key this server trusts. That
            echo means one of:
            echo   - the release key was rotated and this server wasn't told
            echo   - the release genuinely wasn't signed
            echo   - someone tampered with the repository
            echo.
            echo Full gpg output: !VERIFY_OUT!
            echo.
            echo Do not work around this by checking the tag out by hand - find
            echo out why it failed first.
        )
        echo.
        echo Nothing has been changed. The server is still running !CURTAG!.
        pause
        exit /b 1
    )
    echo     Signature OK.

    echo.
    echo ==^> Updating to !NEWTAG!...
    :: --force so a half-finished edit on the server can't block a deploy.
    :: Anything under data\ is gitignored and is left alone; only tracked
    :: code files are reset to exactly what the signed tag contains.
    git checkout --force "!NEWTAG!"
    if errorlevel 1 (
        echo.
        echo Checkout failed - check the error above. The server has NOT been
        echo restarted, so it's still running whatever it was before.
        pause
        exit /b 1
    )
)

echo.
echo ==^> Installing any new dependencies...
:: python -m pip, not Scripts\pip.exe: that stub is generated with the venv
:: and is an unsigned executable, which Windows Application Control blocks on
:: a machine that enforces one. The venv's python.exe is a copy of the signed
:: PSF binary and runs fine.
"%VENV_PY%" -m pip install --quiet --disable-pip-version-check -r "%~dp0backend\requirements.txt"

:: Ask the venv directly rather than trusting pip's exit code. A release that
:: adds a dependency cannot run without it, so a half-finished install would
:: take the farm from "an update didn't apply" to "the server no longer
:: starts", discovered by whoever opens the app next morning.
::
:: The list is exactly backend\requirements.txt (tests/test_api.py fails if
:: they drift). Labour's copy once named two of Boord Owner's libraries it
:: never installs, so every update stopped here on a perfectly healthy server.
"%VENV_PY%" -c "import fastapi, uvicorn, httpx, pywebpush" >nul 2>&1
if errorlevel 1 (
    echo.
    echo The server's dependencies are not fully installed, so this update
    echo cannot be applied. The monitor has NOT been restarted - it is still running whatever it was before.
    echo.
    echo This is nearly always no internet, or pip being blocked. Try again
    echo once the machine is online:
    echo     update_monitor_server.bat
    pause
    exit /b 1
)

:: Leave a note of the tag now checked out, for whoever has to work out what
:: a machine is running when git cannot answer.
::
:: The redirect is written FIRST, which looks odd and is load-bearing. cmd
:: reads a digit sitting immediately before a redirection arrow as a file
:: handle number, so putting the tag first and the arrow after it eats the
:: tag's last character whenever that character is a digit - which, for a
:: version number, is almost always. v1.5.1 stored "v1.5." because the
:: trailing 1 was taken as handle 1; a v1.6.0 would store nothing at all,
:: because the trailing 0 is taken as handle 0 and the file is opened as
:: stdin and left empty. Leading with the redirect leaves no digit beside
:: the arrow.
::
:: (The arrow is spelled out in words rather than typed in these comment
:: lines because a redirection character inside a :: line is only reliably
:: ignored at the top level of a script.)
>"%~dp0data\installed_version.txt" echo !NEWTAG!

echo.
echo ==^> Stopping the server...
:: Not just "schtasks /end". That ends the launcher; the uvicorn process it
:: spawned can outlive it and keep port 8050, and the new release then
:: cannot start - the old one carries on with nothing to start it again if
:: it dies. stop_server.ps1 ends the task, checks the port is actually free,
:: and refuses rather than guessing.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_server.ps1"
if errorlevel 1 (
    echo.
    echo *** THE SERVER COULD NOT BE STOPPED ***
    echo.
    echo Something is still listening on port 8050. The code is on !NEWTAG!
    echo but the server was never restarted, so it is still running the
    echo release from before. Read the message above for what holds the
    echo port, stop it, and run this again.
    echo.
    pause
    exit /b 1
)

echo.
echo ==^> Starting the server...
schtasks /run /tn "%TASK%" >nul

echo.
echo ==^> Checking it came back up...
set OK=
for /l %%i in (1,1,20) do (
    if not defined OK (
        timeout /t 1 /nobreak >nul
        powershell -NoProfile -Command "try { if ((Invoke-WebRequest -Uri 'http://localhost:8050/' -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200) { exit 0 } } catch {}; exit 1" && set OK=1
    )
)
if defined OK (
    echo     Server is back up on port 8050, running !NEWTAG!.
) else (
    echo     Server did not answer in 20s - data\server.log says why.
)
echo.
pause
