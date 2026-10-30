#!/usr/bin/env bash
# ACL install / update for macOS/Linux. Mirrors install_acl.bat: isolated .venv,
# pinned dependencies, smoke check, machine-local config, doctor, then start.
# Safe to run again at any time - an existing install is repaired in place and
# the buyer's own settings (outside this folder) are never touched.

cd "$(dirname "$0")" || exit 1
ROOT="$(pwd)"

echo
echo " ACL - Adopy Campaigns Launcher"
echo " Install / update"
echo " ============================="
echo

fail() {
    echo "ERROR: $*"
    echo
    read -r -p "Press Enter to close..." _ || true
    exit 1
}

# ---------------------------------------------------------------- Python
find_python() {
    local candidate
    for candidate in python3.13 python3.12 python3.11 python3 python; do
        if command -v "$candidate" >/dev/null 2>&1 \
           && "$candidate" -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" 2>/dev/null; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
}

if [ -x ".venv/bin/python" ]; then
    echo "Using existing .venv"
else
    SYSTEM_PY="$(find_python)" || fail "Python 3.11 or newer not found.
Install it with:  brew install python@3.12   (or from python.org), then run this file again."
    echo "Using $("$SYSTEM_PY" --version)"
    echo "Creating .venv ..."
    "$SYSTEM_PY" -m venv .venv \
        || fail "could not create ACL's isolated .venv. The system Python was NOT modified."
fi
PY=".venv/bin/python"

# A manual repair must not install packages for a candidate whose previous
# dashboard launch never became healthy: recover first, then restart this
# installer from the restored checkout so even this file is N-1.
if [ -f "scripts/acl_runtime_manager.py" ]; then
    "$PY" scripts/acl_runtime_manager.py recover-pending
    RC=$?
    if [ "$RC" = "10" ]; then
        echo "The previous ACL release was restored. Repair has stopped safely."
        echo "Run ./install_acl.sh again to finish repair using the restored files."
        exit 10
    elif [ "$RC" != "0" ]; then
        fail "an unfinished ACL update could not be recovered safely."
    fi
fi

# ------------------------------------------------------------ Dependencies
# Versions are pinned in requirements.txt on purpose - see the note there.
echo
echo "Installing dependencies..."
"$PY" -m pip install --disable-pip-version-check -q -r requirements.txt \
    || fail "dependency install failed. The lines above say why."
echo "Dependencies OK."

"$PY" scripts/acl_smoke.py \
    || fail "dependencies installed, but this ACL version does not start."

# ------------------------------------------------- Machine-local config dir
# Deliberately OUTSIDE this folder: re-cloning or moving the checkout must
# never take a buyer's accounts and tokens with it.
CONFIG_DIR="$("$PY" -c "from config.paths import get_local_config_dir; print(get_local_config_dir())" 2>/dev/null | tail -n 1)"
[ -n "$CONFIG_DIR" ] || fail "could not resolve ACL's machine folder."
echo
echo "Machine config: $CONFIG_DIR"

# Migrate legacy real files BEFORE creating examples, then provision only what
# is still missing.
"$PY" scripts/acl_migrate_config.py --provision \
    || fail "machine configuration could not be provisioned safely."

echo "  auto-update OFF - enable only after stable-channel enrollment"

# ------------------------------------------------------------------ Check
echo
echo "Checking this machine..."
echo
# Check with the SAME interpreter ACL will run under.
"$PY" scripts/acl_doctor.py
RC=$?

echo
if [ "$RC" != "0" ]; then
    echo "Install finished, but something required is still missing - see [FAIL] above."
    echo "Fix those, then run this file again."
    echo
    read -r -p "Press Enter to close..." _ || true
    exit "$RC"
fi

echo "ACL is ready - starting the dashboard..."
exec bash "$ROOT/start_dashboard.sh"
