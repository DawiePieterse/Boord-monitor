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
# Usage:  scripts/deploy.sh v3.51
set -euo pipefail

# --- this app ------------------------------------------------------------------
NAME="Boord Monitor"
VERSION_FILE="frontend/api.js"
FARM_DIR='C:\boord-monitor'
UPDATER="update_monitor_server.bat"
REOPEN="the dashboard on each phone"
# The release key, the same in every Boord repo and in data\release_key.fpr on
# every farm PC. Changing it is a key rotation, done everywhere at once.
RELEASE_FPR="67C64CFDD584DD140E58AF6E329C9B9DD0562A9D"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
fail() { echo "Error: $*" >&2; exit 1; }

TAG="${1:-}"
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
echo "On the farm PC: in $FARM_DIR double-click $UPDATER. If this release changed"
echo "install.ps1, double-click install.bat as well. Then fully close and reopen"
echo "$REOPEN."
