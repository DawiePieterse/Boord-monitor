#!/bin/bash
# Install the off-site watcher on a Mac (or any Unix box) that stays on and
# on the tailnet - typically the owner's Mac.
#
# Its one job is the alert the farm PC cannot send about itself: "nothing on
# the farm answers". It runs the same code as the farm monitor with
# MONITOR_ROLE=remote, as a launchd agent so it starts at login and is
# restarted if it ever exits.
#
# Usage:
#   remote/install_remote.sh https://farm-pc.tail1234.ts.net:8050 <token>
#
# The token is printed at the end of install.bat on the farm PC, and lives in
# data\remote_token.txt there. Re-run this script to change either value.
set -euo pipefail

FARM_URL="${1:-}"
TOKEN="${2:-}"
if [ -z "$FARM_URL" ] || [ -z "$TOKEN" ]; then
  echo "Usage: remote/install_remote.sh <farm monitor URL> <remote token>"
  echo "  e.g. remote/install_remote.sh https://farm-pc.tail1234.ts.net:8050 Xy3...  "
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="$REPO_ROOT/backend"
DATA="$REPO_ROOT/data-remote"
PORT="${MONITOR_PORT:-8050}"
LABEL="com.boord.monitor-remote"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

PY="$(command -v python3 || true)"
if [ -z "$PY" ] || ! "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 9))'; then
  echo "Needs Python 3.9 or newer on the PATH (python3)."
  exit 1
fi

echo "==> Virtual environment and dependencies..."
[ -d "$BACKEND/.venv" ] || "$PY" -m venv "$BACKEND/.venv"
"$BACKEND/.venv/bin/python" -m pip install --quiet --disable-pip-version-check -r "$BACKEND/requirements.txt"
mkdir -p "$DATA"

echo "==> Checking the farm monitor answers at $FARM_URL ..."
if curl -fsS --max-time 15 -H "X-Monitor-Token: $TOKEN" "$FARM_URL/api/remote/bundle" -o /dev/null; then
  echo "    It does, and it accepts the token."
else
  echo "    WARNING: could not fetch $FARM_URL/api/remote/bundle with that token."
  echo "    The watcher is installed anyway and will keep trying, but it cannot"
  echo "    send alerts until it has fetched the push key once. Check the URL,"
  echo "    the token, and that this machine is on the tailnet."
fi

if [ "$(uname)" != "Darwin" ]; then
  echo
  echo "Not a Mac, so no launchd agent. Run it under your init system with:"
  echo "  cd $BACKEND && MONITOR_ROLE=remote MONITOR_DATA_DIR=$DATA \\"
  echo "    MONITOR_FARM_URL=$FARM_URL MONITOR_REMOTE_TOKEN=<token> \\"
  echo "    .venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port $PORT"
  exit 0
fi

# The token goes into the plist, which is readable only by this user. It is
# what lets this watcher fetch the farm's push key, so it is a credential.
SELF_URL=""
TS="/Applications/Tailscale.app/Contents/MacOS/Tailscale"
if [ -x "$TS" ]; then
  NAME="$("$TS" status --json 2>/dev/null | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || true)"
  [ -n "$NAME" ] && SELF_URL="https://$NAME:$PORT/"
fi

mkdir -p "$(dirname "$PLIST")"
umask 077
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$BACKEND/.venv/bin/python</string>
    <string>-m</string><string>uvicorn</string><string>main:app</string>
    <string>--host</string><string>127.0.0.1</string>
    <string>--port</string><string>$PORT</string>
  </array>
  <key>WorkingDirectory</key><string>$BACKEND</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>MONITOR_ROLE</key><string>remote</string>
    <key>MONITOR_DATA_DIR</key><string>$DATA</string>
    <key>MONITOR_FARM_URL</key><string>$FARM_URL</string>
    <key>MONITOR_REMOTE_TOKEN</key><string>$TOKEN</string>
    <key>MONITOR_SELF_URL</key><string>$SELF_URL</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$DATA/watcher.log</string>
  <key>StandardErrorPath</key><string>$DATA/watcher.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "==> Started. Dashboard on this Mac: http://localhost:$PORT/"
echo
echo "To open it from a phone too, publish it on the tailnet:"
echo "    tailscale serve --bg --https=$PORT http://localhost:$PORT"
[ -n "$SELF_URL" ] && echo "It then answers at $SELF_URL - the link in its alerts."
echo
echo "Keep this Mac from sleeping (System Settings > Energy, 'Prevent automatic"
echo "sleeping when the display is off'): an asleep watcher watches nothing."
