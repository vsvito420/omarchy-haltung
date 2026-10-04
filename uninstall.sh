#!/bin/bash
# Remove haltung. The librepods headtrack patch stays: it is idle while nobody listens.
UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
systemctl --user disable --now haltung.service 2>/dev/null
rm -f "$UNIT/haltung.service" "$HOME/.local/bin/haltung"
rm -rf "${XDG_DATA_HOME:-$HOME/.local/share}/haltung"
systemctl --user daemon-reload
echo "Removed haltung. Settings stay in ~/.config/haltung/ (delete by hand if you like)."
