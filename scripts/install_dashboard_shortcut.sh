#!/usr/bin/env bash
# Puts "Adopy Campaign Manager.command" on the Desktop (counterpart of
# install_dashboard_shortcut.ps1). Double-clicking it opens Terminal and runs
# start_dashboard.sh. Re-run any time the project folder moves - it overwrites.

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LAUNCHER="$ROOT/start_dashboard.sh"
SHORTCUT="$HOME/Desktop/Adopy Campaign Manager.command"

if [ ! -f "$LAUNCHER" ]; then
    echo "Dashboard launcher not found: $LAUNCHER"
    exit 1
fi
chmod +x "$LAUNCHER" "$ROOT/install_acl.sh" 2>/dev/null

mkdir -p "$HOME/Desktop" || exit 1
printf '#!/usr/bin/env bash\nexec bash "%s"\n' "$LAUNCHER" >"$SHORTCUT" || exit 1
chmod +x "$SHORTCUT" || exit 1
echo "Desktop shortcut: $SHORTCUT"
