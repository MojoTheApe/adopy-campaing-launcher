#!/usr/bin/env bash
# ACL dashboard launcher for macOS/Linux. Mirrors start_dashboard.bat step by
# step (same health check, release recovery, gated update, restart marker), so
# a change to one launcher is a change to both: see
# tests/test_posix_launcher_contract.py.
#
# Keep the Terminal window open - closing it (or Ctrl+C) stops the dashboard.

cd "$(dirname "$0")" || exit 1
ROOT="$(pwd)"

PORT=9090
URL="http://127.0.0.1:${PORT}/"
HEALTH_URL="http://127.0.0.1:${PORT}/health"

echo
echo " Adopy Campaign Manager"
echo " ========================"
echo

pause_exit() {
    # Double-clicked from Finder the window closes with the script; hold it so
    # the error can be read.
    echo
    read -r -p "Press Enter to close..." _ || true
    exit "${1:-1}"
}

BOOTSTRAP_PY=".venv/bin/python"
if [ ! -x "$BOOTSTRAP_PY" ]; then
    echo "ERROR: ACL's isolated environment is missing."
    echo "Run ./install_acl.sh, then start the dashboard again."
    pause_exit 1
fi
PY="$BOOTSTRAP_PY"
if ! "$BOOTSTRAP_PY" -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)"; then
    echo "ERROR: ACL requires Python 3.11 or newer. Run ./install_acl.sh."
    pause_exit 1
fi

# One source of truth for the machine folder: the same function the dashboard
# uses, so ACL_CONFIG_DIR and the macOS default agree with it.
CONFIG_DIR="$("$BOOTSTRAP_PY" -c "from config.paths import get_local_config_dir; print(get_local_config_dir())" 2>/dev/null | tail -n 1)"
if [ -z "$CONFIG_DIR" ]; then
    echo "ERROR: could not resolve ACL's machine folder. Run ./install_acl.sh."
    pause_exit 1
fi
export ACL_CONFIG_DIR="$CONFIG_DIR"

# Already serving? Then just open it - never kill whatever holds the port.
HEALTH_FILE="$(mktemp "${TMPDIR:-/tmp}/acl_health.XXXXXX")"
if curl -fsS -o "$HEALTH_FILE" -m 3 "$HEALTH_URL" 2>/dev/null && grep -q "ACL" "$HEALTH_FILE"; then
    rm -f "$HEALTH_FILE"
    if ! "$BOOTSTRAP_PY" scripts/acl_runtime_manager.py confirm-running >/dev/null 2>&1; then
        echo "ERROR: the running dashboard's release could not be confirmed."
        echo "Close it and send this window to support before using ACL."
        pause_exit 1
    fi
    echo "Dashboard is already running - opening it."
    open "$URL"
    sleep 2
    exit 0
fi
rm -f "$HEALTH_FILE"

# Release recovery: an update is pending until the new dashboard answers
# /health. If the previous launch died before that, restore the old commit
# before importing any code. Exit 10 = a reset happened; start a fresh copy so
# bash reads the restored launcher instead of continuing through a file that
# changed underneath it.
"$BOOTSTRAP_PY" scripts/acl_runtime_manager.py recover-pending
RECOVERY_RC=$?
if [ "$RECOVERY_RC" = "10" ]; then
    echo "Restarting the restored ACL release..."
    exec bash "$ROOT/start_dashboard.sh"
fi
if [ "$RECOVERY_RC" != "0" ]; then
    echo "ERROR: an unfinished ACL update could not be recovered safely."
    echo "Do not edit or delete the update marker. Send this window to support."
    pause_exit 1
fi

# An immutable per-commit runtime is used only when that exact commit passed
# dependency and smoke validation; otherwise the bootstrap .venv.
CURRENT_SHA="$(git rev-parse HEAD 2>/dev/null)"
if [ -n "$CURRENT_SHA" ] \
   && "$BOOTSTRAP_PY" scripts/acl_runtime_manager.py check-active --sha "$CURRENT_SHA" >/dev/null 2>&1; then
    PY="$CONFIG_DIR/runtimes/$CURRENT_SHA/bin/python"
fi

# Move this machine's own config out of the checkout BEFORE the update step.
if ! "$PY" scripts/acl_migrate_config.py; then
    echo "ERROR: machine configuration migration failed. Update/start cancelled."
    pause_exit 1
fi

# ---------------------------------------------------------------- Updates
# Same rules as the Windows launcher: only on machines that opted in
# (`auto_update`) or when the dashboard's Update button asked (`update_once`,
# deleted BEFORE the update runs so a failing candidate is not retried forever).
# The order inside run_update is the point: new code is downloaded but NOT
# activated until its dependencies are installed and it is shown to start.
run_update() {
    echo "Checking for updates..."
    local head_before head_after
    head_before="$(git rev-parse HEAD 2>/dev/null)"

    # fetch, not pull: downloads without touching the working tree.
    if ! git fetch --quiet origin main --tags; then
        echo "  could not reach GitHub - continuing with the installed version."
        return 0
    fi

    head_after="$(git rev-parse origin/main 2>/dev/null)"
    if [ -z "$head_after" ]; then
        echo "  promoted channel origin/main is unavailable - skipping update."
        return 0
    fi
    if [ "$head_before" = "$head_after" ]; then
        echo "  already up to date."
        return 0
    fi

    if ! "$PY" scripts/acl_update_gate.py --candidate origin/main; then
        echo "  candidate is not a promoted release - continuing with the installed version."
        return 0
    fi

    echo "  new version available - preparing its isolated runtime..."
    local req
    req="$(mktemp "${TMPDIR:-/tmp}/acl_requirements.XXXXXX")"
    git show origin/main:requirements.txt >"$req" 2>/dev/null || cp requirements.txt "$req"

    if ! "$BOOTSTRAP_PY" scripts/acl_runtime_manager.py prepare --sha "$head_after" --requirements "$req"; then
        echo
        echo "  WARNING: the new version's isolated runtime could not be prepared."
        echo "  The update was NOT applied - you are still on the working version."
        echo
        return 0
    fi

    local runtime_next="$CONFIG_DIR/runtimes/$head_after/bin/python"
    if ! "$BOOTSTRAP_PY" scripts/acl_runtime_manager.py mark-pending --previous "$head_before" --candidate "$head_after"; then
        echo "  recoverable update marker could not be written - update skipped."
        return 0
    fi

    if ! git merge --ff-only origin/main >/dev/null 2>&1; then
        echo "  local changes block the update - continuing with the installed version."
        "$BOOTSTRAP_PY" scripts/acl_runtime_manager.py recover-pending >/dev/null 2>&1
        return 0
    fi

    # Installed cleanly is not the same as starts.
    if ! "$runtime_next" scripts/acl_smoke.py >/dev/null 2>&1; then
        echo "  WARNING: the new version does not start on this machine - rolling back."
        "$BOOTSTRAP_PY" scripts/acl_runtime_manager.py recover-pending >/dev/null 2>&1
        if [ $? -ne 10 ]; then
            echo "  ROLLBACK FAILED. Run ./install_acl.sh, and send this line on."
        else
            echo "  rolled back to the previous working version; candidate quarantined."
        fi
        return 20
    fi

    if ! "$runtime_next" scripts/acl_runtime_manager.py activate --sha "$head_after" >/dev/null 2>&1; then
        echo "  WARNING: the new runtime could not be activated - rolling back."
        "$BOOTSTRAP_PY" scripts/acl_runtime_manager.py recover-pending >/dev/null 2>&1
        [ $? -ne 10 ] && echo "  ROLLBACK FAILED. Run ./install_acl.sh, and send this line on."
        return 20
    fi

    PY="$runtime_next"
    echo "  updated and smoke-verified; awaiting dashboard health confirmation."
    return 0
}

UPDATE_NOW=""
[ -e "$CONFIG_DIR/auto_update" ] && UPDATE_NOW=1
if [ -e "$CONFIG_DIR/update_once" ]; then
    rm -f "$CONFIG_DIR/update_once"
    UPDATE_NOW=1
fi
if [ -n "$UPDATE_NOW" ] && git rev-parse --git-dir >/dev/null 2>&1; then
    run_update
    if [ $? -eq 20 ]; then
        echo
        echo "The failed candidate was rolled back and quarantined."
        echo "Start ACL again to use the restored release."
        pause_exit 1
    fi
fi

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo
    echo "ERROR: port $PORT is used by another application."
    echo "ACL did not terminate that process. Close it or run serve.py on another port."
    pause_exit 1
fi

echo "Starting server on port $PORT..."
# Tells the dashboard a launcher is watching it, which is what makes the
# header's Update button safe to offer.
export ACL_SUPERVISED=1
"$PY" serve.py --port "$PORT" &
SERVER_PID=$!
# Closing this window / Ctrl+C stops the server too.
trap 'kill "$SERVER_PID" 2>/dev/null; exit 0' INT TERM HUP

TRIES=0
until curl -fsS -o /dev/null -m 3 "$HEALTH_URL" 2>/dev/null; do
    sleep 1
    TRIES=$((TRIES + 1))
    if ! kill -0 "$SERVER_PID" 2>/dev/null || [ "$TRIES" -ge 90 ]; then
        echo
        echo "Server did not come up. Run this to see why:"
        echo "    .venv/bin/python serve.py --port $PORT"
        echo "The pending release will be rolled back automatically on the next start."
        kill "$SERVER_PID" 2>/dev/null
        pause_exit 1
    fi
done

if ! "$BOOTSTRAP_PY" scripts/acl_runtime_manager.py confirm-running >/dev/null 2>&1; then
    echo "ERROR: ACL is healthy, but its release marker could not be confirmed."
    echo "Close this window and send the error to support before using the dashboard."
    kill "$SERVER_PID" 2>/dev/null
    pause_exit 1
fi
echo "Dashboard is up - opening browser."
open "$URL"
echo
echo "Keep this window open. Close it to stop the dashboard."
echo

# Hold the window - and watch for the one thing the dashboard cannot do for
# itself: clicking Update writes this marker and exits the server; the update
# then runs here, on the same gated path as a normal start.
RESTART_MARKER="$CONFIG_DIR/restart_request.json"
while [ ! -e "$RESTART_MARKER" ]; do
    sleep 3
    if ! kill -0 "$SERVER_PID" 2>/dev/null && [ ! -e "$RESTART_MARKER" ]; then
        echo "The dashboard stopped."
        exit 0
    fi
done

# Swap the markers before doing anything else: leaving the request in place
# would restart this machine forever if the next step failed.
rm -f "$RESTART_MARKER"
echo "requested by the dashboard" >"$CONFIG_DIR/update_once"

echo
echo "Update requested from the dashboard - restarting ACL..."
# Wait for the old server to release the port, otherwise the relaunch would
# find a healthy dashboard and open the browser on the version being replaced.
WAIT=0
while curl -fsS -o /dev/null -m 2 "$HEALTH_URL" 2>/dev/null; do
    WAIT=$((WAIT + 1))
    if [ "$WAIT" -ge 30 ]; then
        echo
        echo "ERROR: the dashboard did not stop, so it was not updated."
        echo "Close this window and start ACL again."
        pause_exit 1
    fi
    sleep 1
done
exec bash "$ROOT/start_dashboard.sh"
