#!/bin/bash
# Deploy Boord Monitor: sign the release tag on the Mac and push it to GitHub.
#
# This is what "deploy" means for this app. Farm servers install only tags
# signed by the release key (the updater checks the signature against
# data\release_key.fpr), so a release is not pushed code, it is a signed tag,
# and the key lives only on the owner's Mac. The same script, with the few
# lines of configuration below, is in every Boord repo, so a deploy is the one
# command `scripts/deploy.sh vX.Y` whichever app it is.
#
# Unlike ship.sh and release.sh it asks nothing: it is run by Claude Code or
# from a terminal when the decision to release has already been taken. The
# only prompt is GnuPG's for the key's passphrase. Instead of asking, it
# refuses, stopping at the first of these that fails and changing nothing:
#   - the tag looks like v3.51 (the updater sorts 'v*' tags by version order);
#   - the tree is clean, main is checked out and pulled fast-forward;
#   - VERSION in the frontend equals the tag, so every screen header tells the
#     truth about what it runs;
#   - the tag exists neither locally nor on origin (a released tag never moves);
#   - the release key is in this Mac's GnuPG keyring.
# Then it pushes main if it is ahead, signs the tag, verifies the signature
# is the release key's (deleting the tag if not), and pushes the tag.
#
# Last, it installs the release on the farm PC through Boord Monitor's Update
# (Monitor 2.2+): the same updater a person double-clicks there, run
# unattended as SYSTEM, which installs only a tag signed by the release key.
# It asks with the remote token this Mac's off-site watcher already holds
# (its launchd plist), waits for the update, and checks the app reports the
# new tag. If that can't be done, the tag is still out and it says what to
# run on the farm PC instead; --no-farm skips this step.
#
# Usage:  scripts/deploy.sh v3.51 [--no-farm]
set -euo pipefail

# --- this app ------------------------------------------------------------------
NAME="Boord Monitor"
VERSION_FILE="frontend/api.js"
FARM_DIR='C:\boord-monitor'
UPDATER="update_monitor_server.bat"
# Its key in Boord Monitor's apps.json, and its address on the tailnet, for
# installing it on the farm PC once the tag is out.
MONITOR_KEY="monitor"
APP_URL="https://bekfontein-server.taile32cb0.ts.net:8050/"
REOPEN="the dashboard on each phone"
# The release key, the same in every Boord repo and in data\release_key.fpr on
# every farm PC. Changing it is a key rotation, done everywhere at once.
RELEASE_FPR="67C64CFDD584DD140E58AF6E329C9B9DD0562A9D"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
fail() { echo "Error: $*" >&2; exit 1; }

TAG="${1:-}"
NO_FARM=""
[ "${2:-}" = "--no-farm" ] && NO_FARM=1
[ -n "$TAG" ] || fail "usage: scripts/deploy.sh <tag>   e.g. scripts/deploy.sh v3.51   (newest: $(git tag --list 'v*' --sort=-v:refname | head -1))"
[[ "$TAG" =~ ^v[0-9]+(\.[0-9]+)*$ ]] || fail "tag must look like v3.51: $UPDATER picks the newest 'v*' tag by version order."

# --- a clean, current main -------------------------------------------------------
[ -z "$(git status --porcelain)" ] || { git status --short >&2; fail "working tree is not clean. The tag signs a commit, so anything uncommitted would not be in the release."; }
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
[ "$BRANCH" = "main" ] || fail "on branch '$BRANCH', not main. A release is a commit on main: merge first."
# This pull is what stops a tag landing on an old commit: Boord Notes' v2.16
# was signed in a stale clone and pointed at a months-old commit.
git pull -q --ff-only origin main || fail "could not fast-forward main from origin. Resolve that first."
git fetch -q --tags origin

# --- the in-app version must match the tag ------------------------------------
APP_VERSION="$(sed -n 's/^  VERSION: "\(.*\)",$/\1/p' "$VERSION_FILE")"
EXPECTED="${TAG#v}"
[ "$APP_VERSION" = "$EXPECTED" ] || fail "version mismatch: $TAG expects VERSION \"$EXPECTED\" but $VERSION_FILE says \"$APP_VERSION\". Bump it (and the service-worker CACHE if the frontend changed), commit, run again."

# --- the tag must be new, here and on origin --------------------------------------
# (origin's tags were fetched above, so this also catches a tag only origin has.)
git rev-parse -q --verify "refs/tags/$TAG" >/dev/null && fail "tag $TAG already exists (here or on origin). Never re-point a released tag; use the next version."
git ls-remote --exit-code --tags origin "refs/tags/$TAG" >/dev/null 2>&1 && fail "tag $TAG already exists on origin. Never re-point a released tag; use the next version."

# --- the release key must be here ----------------------------------------------
gpg --batch --list-secret-keys "$RELEASE_FPR" >/dev/null 2>&1 || fail "the release key $RELEASE_FPR is not in this machine's GnuPG keyring. Releases are signed on the owner's Mac only."

# --- push main if it is ahead ------------------------------------------------------
# A tag on a commit origin has never seen is a release that exists only as a
# tag; anyone reading main cannot see the code the farm is running.
if [ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ]; then
  echo "==> main is ahead of origin, pushing it first"
  git push -q origin main
fi

# --- sign, verify, push -------------------------------------------------------------
echo "==> Signing $TAG at $(git rev-parse --short HEAD)  $(git log -1 --pretty=%s)"
# gpg.format is forced: a machine whose git config says "ssh" would otherwise
# make an SSH-signed tag no farm's gpg can verify.
git -c gpg.format=openpgp -c user.signingkey="$RELEASE_FPR" tag -s "$TAG" -m "$NAME $TAG"

# VALIDSIG's first field is the fingerprint of the key that made the signature.
# Anything else is deleted, not pushed: a tag no farm accepts only blocks the
# next one. (|| true: under pipefail a failed verify would otherwise end the
# script before the tag is deleted.)
SIG_FPR="$(git -c gpg.format=openpgp verify-tag --raw "$TAG" 2>&1 \
  | awk '$1=="[GNUPG:]" && $2=="VALIDSIG"{print $3; exit}' || true)"
if [ "$SIG_FPR" != "$RELEASE_FPR" ]; then
  git tag -d "$TAG" >/dev/null
  fail "the new tag did not verify as signed by $RELEASE_FPR (got '${SIG_FPR:-no valid signature}'). Tag deleted, nothing pushed."
fi
echo "==> Verified: signed by $RELEASE_FPR"

echo "==> Pushing $TAG"
git push -q origin "refs/tags/$TAG"

echo
echo "Deployed $NAME $TAG to GitHub."

# --- install it on the farm PC ------------------------------------------------
# The off-site watcher's plist holds the farm monitor's address and the remote
# token (remote/install_remote.sh in Boord Monitor). BOORD_MONITOR_URL and
# BOORD_MONITOR_TOKEN stand in for it on a machine without one.
farm_steps() {
  echo "On the farm PC: in $FARM_DIR double-click $UPDATER. If this release changed"
  echo "install.ps1, double-click install.bat as well. Then fully close and reopen"
  echo "$REOPEN."
}
# A list is printed a line each. Monitor 2.2 wrote each output line as
# PowerShell's string object, {"value": "...", ...}, so take its value.
json_field() { python3 -c 'import json,sys; v=json.load(sys.stdin).get(sys.argv[1]); print("\n".join(x.get("value", "") if isinstance(x, dict) else str(x) for x in v) if isinstance(v, list) else ("" if v is None else v))' "$1" 2>/dev/null; }

MON_URL="${BOORD_MONITOR_URL:-}"
MON_TOKEN="${BOORD_MONITOR_TOKEN:-}"
PLIST="$HOME/Library/LaunchAgents/com.boord.monitor-remote.plist"
if [ -z "$MON_URL" ] && [ -f "$PLIST" ] && [ -x /usr/libexec/PlistBuddy ]; then
  MON_URL="$(/usr/libexec/PlistBuddy -c 'Print :EnvironmentVariables:MONITOR_FARM_URL' "$PLIST" 2>/dev/null || true)"
  MON_TOKEN="$(/usr/libexec/PlistBuddy -c 'Print :EnvironmentVariables:MONITOR_REMOTE_TOKEN' "$PLIST" 2>/dev/null || true)"
fi
MON_URL="${MON_URL%/}"

if [ -n "$NO_FARM" ]; then
  echo; echo "--no-farm: not installing it on the farm PC."; farm_steps; exit 0
fi
if [ -z "$MON_URL" ] || [ -z "$MON_TOKEN" ]; then
  echo; echo "No Boord Monitor address and token on this Mac (no off-site watcher), so it is not installed yet."
  farm_steps; exit 0
fi

echo
echo "==> Installing $TAG on the farm PC (Boord Monitor's Update)"
# The farm fetches tags from GitHub itself; the push above is already there.
RESP="$(curl -sS --max-time 90 -X POST -H "X-Monitor-Token: $MON_TOKEN" -w '\n%{http_code}' \
  "$MON_URL/api/apps/$MONITOR_KEY/update" 2>&1 || true)"
CODE="$(printf '%s' "$RESP" | tail -n 1)"
BODY="$(printf '%s' "$RESP" | sed '$d')"
if [ "$CODE" != "200" ]; then
  echo "Error: the farm's Boord Monitor did not start the update (HTTP ${CODE:-none}): $(printf '%s' "$BODY" | json_field detail || printf '%s' "$BODY")" >&2
  case "$CODE" in 404|405) echo "(The farm's Boord Monitor is older than 2.2, which added Update. Install Monitor 2.2 there once by hand.)" >&2 ;; esac
  echo "The tag is out. Install it by hand:" >&2
  farm_steps >&2; exit 3
fi
REQUESTED="$(printf '%s' "$BODY" | json_field requested || true)"

# Follow it. Monitor's own update restarts the monitor, so a few failed polls
# in a row are expected; only the deadline ends the wait.
DEADLINE=$(( $(date +%s) + 20 * 60 ))
STATE="queued"; SHOWN=""
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  sleep 10
  JOB="$(curl -fsS --max-time 15 -H "X-Monitor-Token: $MON_TOKEN" "$MON_URL/api/update" 2>/dev/null || true)"
  [ -n "$JOB" ] || continue
  [ "$(printf '%s' "$JOB" | json_field requested)" = "$REQUESTED" ] || continue
  STATE="$(printf '%s' "$JOB" | json_field state || true)"
  [ "$STATE" != "$SHOWN" ] && { echo "    $STATE"; SHOWN="$STATE"; }
  case "$STATE" in queued|running) continue ;; esac
  break
done
if [ "$STATE" != "done" ]; then
  echo "Error: the farm update ended '$STATE'. What the updater said:" >&2
  printf '%s' "${JOB:-}" | json_field output | sed 's/^/    | /' >&2
  echo "The tag is out; nothing on the farm changed unless the lines above say so. Install it by hand:" >&2
  farm_steps >&2; exit 3
fi

# The proof is the app itself saying it runs the new tag.
LIVE="$(curl -fsS --max-time 15 "${APP_URL%/}/api/version" 2>/dev/null | json_field tag || true)"
if [ "$LIVE" != "$TAG" ]; then
  echo "Error: the update finished, but ${APP_URL%/}/api/version reports '${LIVE:-nothing}', not $TAG." >&2
  farm_steps >&2; exit 3
fi
echo "==> The farm PC runs $NAME $TAG."
echo "If this release changed install.ps1, double-click install.bat in $FARM_DIR on the"
echo "farm PC as well. Then fully close and reopen $REOPEN."
