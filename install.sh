#!/bin/bash
# Install haltung for the current user:
#   ~/.local/share/haltung/            program
#   ~/.local/bin/haltung               command (haltung --calibrate starts a calibration)
#   ~/.config/systemd/user/haltung.service   starts with the graphical session
# and adds the head-tracking stream to the librepods daemon of the omapods plugin.
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/haltung"
BIN="$HOME/.local/bin"
UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
PLUGIN="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/plugins/io.github.thisisgm.omapods"

python3 -c 'import gi; gi.require_version("Gtk4LayerShell", "1.0")' 2>/dev/null || {
  echo "Missing GTK4 layer shell. Install it with: omarchy pkg add python-gobject gtk4-layer-shell"; exit 1; }

# 1. librepods: the AirPods only stream head tracking to the daemon's own connection,
#    so the daemon has to pass the frames on. Patch and rebuild it once.
if [[ ! -d $PLUGIN/daemon ]]; then
  echo "The omapods plugin (librepods daemon) is missing. Install it first:"
  echo "  omarchy plugin add https://github.com/thisisgm/omarchy-pods --enable"
  exit 1
fi
if grep -q '"headtrack"' "$PLUGIN/daemon/main.cpp"; then
  echo "librepods already has the headtrack verb."
else
  echo "Patching librepods (headtrack verb)…"
  git -C "$PLUGIN" apply "$SRC/librepods-headtrack.patch"
  cmake -S "$PLUGIN/daemon" -B "$PLUGIN/daemon/build" -G Ninja -DBUILD_TESTING=OFF >/dev/null
  cmake --build "$PLUGIN/daemon/build"
  cmake --install "$PLUGIN/daemon/build" --prefix "$HOME/.local" >/dev/null
  systemctl --user restart librepods.service
fi

# 2. the panel
mkdir -p "$DATA" "$BIN" "$UNIT"
install -m 644 "$SRC/haltung.py" "$SRC/head3d.py" "$DATA/"
printf '#!/bin/sh\nexec python3 "%s/haltung.py" "$@"\n' "$DATA" >"$BIN/haltung"
chmod 755 "$BIN/haltung"
install -m 644 "$SRC/haltung.service" "$UNIT/haltung.service"
systemctl --user daemon-reload
systemctl --user enable haltung.service >/dev/null
systemctl --user restart haltung.service

echo "Installed haltung. The panel sits at the top of DP-1; change monitor, height and"
echo "thresholds in ~/.config/haltung/config.json, then: systemctl --user restart haltung"
echo "Calibrate: click the panel (or run haltung --calibrate), look ahead, then look down and up."
