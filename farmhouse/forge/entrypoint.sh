#!/bin/sh
# Keep the app's state on the persistent volume (/state) while the code stays in the image.
# The upstream app writes next to its code: collection.db, scryfall_cache.db, data/ (Scryfall bulk DB, ChromaDB index),
# user-decks/. Point each at /state with a symlink; seed data/ and user-decks/ from the image the first time.
set -eu
S="${STATE_DIR:-/state}"
mkdir -p "$S"

for d in data user-decks results; do
  if [ ! -e "$S/$d" ]; then
    if [ -d "/app/$d" ]; then cp -a "/app/$d" "$S/$d"; else mkdir -p "$S/$d"; fi
  fi
  rm -rf "/app/$d"
  ln -s "$S/$d" "/app/$d"
done

for f in collection.db scryfall_cache.db; do
  rm -f "/app/$f"
  ln -s "$S/$f" "/app/$f"       # sqlite creates the target on first open
done

# Forge looks decks up by name in its user folder; the app exports the decks it simulates there.
mkdir -p "${FORGE_DECKS_DIR:-/tmp/.forge/decks/commander}"
# Seed it with the bundled precon decks (163 small files); never overwrite a deck that is already there.
if [ -d /app/precon-decks ]; then cp -n /app/precon-decks/*.dck "${FORGE_DECKS_DIR:-/tmp/.forge/decks/commander}"/ 2>/dev/null || true; fi

# A virtual display for Forge (it initialises AWT even in command-line mode; see the Dockerfile). Best effort: the app itself does not need it.
if command -v Xvfb >/dev/null 2>&1; then
  mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix 2>/dev/null || true
  Xvfb "${DISPLAY:-:99}" -screen 0 1280x800x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    [ -S "/tmp/.X11-unix/X${DISPLAY#:}" ] && break
    sleep 0.3
  done
fi

exec "$@"
