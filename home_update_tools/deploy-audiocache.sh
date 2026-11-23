#!/usr/bin/env bash
# Deploy the Apple Music audio cache from a checkout of the code branch into the MA container.
#
#   REPO=/path/to/checkout BACKUP_DIR=/path/to/backups ./deploy-audiocache.sh [container]
#   DRY_RUN=1 ...   runs the checks only and changes nothing
#
# Copies five provider files and the generated en.json into the container, checks them byte for
# byte, then restarts (or starts) the container. Works on a running or a stopped container.
# Refuses to run when the checkout has uncommitted changes in those files, when the container's
# image is not the version the branch is based on, or when upstream changed those files between
# the image and the branch base.
set -euo pipefail

: "${REPO:?set REPO to your checkout of the apple-music-audio-cache branch}"
: "${BACKUP_DIR:?set BACKUP_DIR to a private folder for backups}"
C=${1:-music-assistant}
PY=/app/venv/bin/python
PROVIDER=music_assistant/providers/apple_music
TRANSLATIONS=music_assistant/translations/en.json
FILES=(audio_cache.py constants.py provider.py streaming.py strings.json)

fail() { echo "error: $*" >&2; exit 1; }
digest() { sha256sum | cut -d' ' -f1; }

# 1. deploy exactly what is committed
[ -z "$(git -C "$REPO" status --porcelain -- "$PROVIDER" "$TRANSLATIONS")" ] \
  || fail "uncommitted changes in the files to deploy"

# 2. the image must be what the branch is based on
IMG=$(docker inspect "$C" --format '{{.Image}}')
REV=$(docker inspect "$C" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')
VERSION=$(docker run --rm --entrypoint "$PY" "$IMG" \
  -c 'from importlib.metadata import version; print(version("music-assistant"))')
git -C "$REPO" cat-file -e "$REV^{commit}" 2>/dev/null \
  || fail "image revision $REV is not in the checkout: run 'git fetch upstream --tags'"
git -C "$REPO" merge-base --is-ancestor "$REV" HEAD \
  || fail "the image ($VERSION, $REV) is newer than the branch: rebase the branch onto $REV first"
BASE=$(git -C "$REPO" merge-base HEAD upstream/stable)
git -C "$REPO" diff --quiet "$REV" "$BASE" -- "$PROVIDER" "$TRANSLATIONS" \
  || fail "upstream changed these files after the image: rebase the branch onto $REV first"

# 3. where the package is inside the container
ROOT=$(docker run --rm --entrypoint "$PY" "$IMG" \
  -c 'import importlib.util,os; print(os.path.dirname(importlib.util.find_spec("music_assistant").origin))')
echo "checks passed: $C runs MA $VERSION ($REV), branch is at $(git -C "$REPO" rev-parse --short HEAD)"
[ "${DRY_RUN:-}" = 1 ] && { echo "dry run: nothing changed"; exit 0; }

# 4. keep what is there now
BK="$BACKUP_DIR/pre-deploy-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BK" && chmod 700 "$BK"
docker cp "$C:$ROOT/providers/apple_music" "$BK/apple_music"
docker cp "$C:$ROOT/translations/en.json" "$BK/en.json"

# 5. copy, then compare byte for byte
for f in "${FILES[@]}"; do
  docker cp "$REPO/$PROVIDER/$f" "$C:$ROOT/providers/apple_music/$f"
done
docker cp "$REPO/$TRANSLATIONS" "$C:$ROOT/translations/en.json"
for f in "${FILES[@]}"; do
  [ "$(digest < "$REPO/$PROVIDER/$f")" = "$(docker cp "$C:$ROOT/providers/apple_music/$f" - | tar -xO | digest)" ] \
    || fail "$f differs after the copy"
done
[ "$(digest < "$REPO/$TRANSLATIONS")" = "$(docker cp "$C:$ROOT/translations/en.json" - | tar -xO | digest)" ] \
  || fail "en.json differs after the copy"
echo "deployed: files in $C are identical to the checkout; previous files saved in $BK"

docker restart "$C" > /dev/null && echo "restarted $C"
