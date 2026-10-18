#!/bin/bash
# Cut a signed Boord Monitor release.
#
# Farm servers run update_monitor_server.bat, which refuses any tag that isn't signed
# by the release key (see MANUAL.md, "Updates"). So a release is not "pushed
# code" - it is a signed tag. This script is the only supported way to make
# one, because the two things easiest to get wrong by hand are both fatal in
# opposite directions: an unsigned tag silently fails to deploy anywhere, and
# a tag whose version disagrees with Monitor.VERSION in frontend/api.js makes every device's header
# lie about what it is running.
#
# Usage:  scripts/release.sh v1.0
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

TAG="${1:-}"
if [ -z "$TAG" ]; then
  echo "Usage: scripts/release.sh <tag>     e.g. scripts/release.sh v2.1"
  exit 1
fi

if [[ ! "$TAG" =~ ^v[0-9]+(\.[0-9]+)*$ ]]; then
  echo "Error: tag must look like v2.1 or v2.1.3 - update_monitor_server.bat picks the"
  echo "newest tag matching 'v*' by version order, and a tag it can't sort"
  echo "sensibly is a tag that deploys at the wrong time."
  exit 1
fi

if git rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
  echo "Error: tag $TAG already exists locally. Pick a new version - never"
  echo "re-point a released tag, or servers already on it will not re-deploy."
  exit 1
fi

# --- the working tree must be exactly what gets signed -----------------------
if [ -n "$(git status --porcelain)" ]; then
  echo "Error: working tree is not clean. Commit or stash first - the tag"
  echo "signs a commit, so anything uncommitted is NOT in the release."
  git status --short
  exit 1
fi

# --- the in-app version must match the tag -----------------------------------
# Monitor.VERSION in frontend/api.js is rendered in every screen header specifically so you can see
# which build a device is on. If it disagrees with the tag, that display is
# worse than useless during an incident.
APP_VERSION="$(sed -n 's/^  VERSION: "\(.*\)",$/\1/p' frontend/api.js)"
EXPECTED="${TAG#v}"
if [ "$APP_VERSION" != "$EXPECTED" ]; then
  echo "Error: version mismatch."
  echo "  tag:                       $TAG  (expects VERSION \"$EXPECTED\")"
  echo "  frontend/api.js:    \"$APP_VERSION\""
  echo
  echo "Set VERSION to \"$EXPECTED\" in frontend/api.js, commit, re-run."
  exit 1
fi

# --- a signing key must actually be configured -------------------------------
SIGNING_KEY="$(git config --get user.signingkey || true)"
if [ -z "$SIGNING_KEY" ]; then
  echo "Error: no git user.signingkey set, so 'git tag -s' would fail or sign"
  echo "with the wrong key. Set it to the release key:"
  echo "    git config user.signingkey <KEY-ID>"
  exit 1
fi

echo "About to sign and tag:"
echo "  tag:     $TAG"
echo "  commit:  $(git rev-parse --short HEAD)  $(git log -1 --pretty=%s)"
echo "  branch:  $(git rev-parse --abbrev-ref HEAD)"
echo "  key:     $SIGNING_KEY"
echo
read -r -p "Create this signed tag? [y/N] " reply
case "$reply" in
  [yY]|[yY][eE][sS]) ;;
  *) echo "Aborted - nothing created."; exit 1 ;;
esac

git tag -s "$TAG" -m "Boord Monitor $TAG"

# Verify locally before it ever leaves this machine. If this fails, the tag is
# useless to every farm server, and it is far cheaper to find out here.
if ! git verify-tag "$TAG" >/dev/null 2>&1; then
  echo "Error: the tag was created but does not verify. Deleting it."
  git tag -d "$TAG"
  exit 1
fi

echo
echo "Signed tag $TAG created and verified locally."
git verify-tag "$TAG" 2>&1 | sed 's/^/    /'
echo
echo "It is NOT pushed yet. Farm servers only see it once you run:"
echo
echo "    git push origin $TAG"
echo
echo "Confirm the fingerprint above matches data/release_key.fpr on the farm"
echo "servers - if you have rotated keys, they will refuse this release until"
echo "that file is updated (MANUAL.md, Updates)."
