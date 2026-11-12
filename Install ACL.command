#!/usr/bin/env bash
# Adopy Campaign Manager - one-file installer for macOS (counterpart of
# "Install ACL.bat"). This single file can be sent to a new campaign manager:
# it needs nothing from an unverified checkout.
#
#   - checks Git and Python 3.11+ (offers Homebrew for Python)
#   - downloads ACL into ~/Documents/ACL at the promoted release
#   - runs install_acl.sh, which checks the machine and opens the dashboard
#     only when the check passes (a clean Mac stops at a [FAIL] list first)
#
# Main way to start it: in Terminal run  bash "Install ACL.command"
# If you double-click and macOS refuses ("unidentified developer"): System
# Settings -> Privacy & Security -> Open Anyway.
# Safe to run again: an existing installation is updated, never re-downloaded.

REPO_NAME="MojoTheApe/adopy-campaing-launcher"
REPO_URL="https://github.com/${REPO_NAME}.git"
TARGET="$HOME/Documents/ACL"

fail() {
    echo
    echo "ERROR: $*"
    echo
    read -r -p "Press Enter to close..." _ || true
    exit 1
}

echo "Adopy Campaign Manager - installing into $TARGET"
echo

# ACL or something else already listening on 9090: the install would swap files
# under a live server.
if lsof -nP -iTCP:9090 -sTCP:LISTEN >/dev/null 2>&1; then
    fail "ACL or another service is running (port 9090). Close the ACL window, finish any RPA job, then run this file again. Nothing was changed."
fi

# ------------------------------------------------------------------- Git
# On a clean Mac `git` is a stub that opens the Command Line Tools installer.
if ! xcode-select -p >/dev/null 2>&1 || ! git --version >/dev/null 2>&1; then
    xcode-select --install >/dev/null 2>&1
    fail "Apple Command Line Tools (Git) are being installed - a system window should have opened. Finish it, then run this file again."
fi

# ---------------------------------------------------------------- Python
find_python() {
    local candidate
    for candidate in python3.13 python3.12 python3.11 python3; do
        if command -v "$candidate" >/dev/null 2>&1 \
           && "$candidate" -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)" 2>/dev/null; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
}

# Homebrew lives outside the default PATH on Apple Silicon until its shellenv runs.
for brew_bin in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    [ -x "$brew_bin" ] && eval "$("$brew_bin" shellenv)"
done

if ! find_python >/dev/null; then
    if command -v brew >/dev/null 2>&1; then
        echo "Installing Python 3.12 with Homebrew ..."
        brew install python@3.12 || fail "Homebrew could not install Python. Install Python 3.11+ from python.org and run this file again."
        eval "$(brew shellenv)"
    else
        fail "Python 3.11 or newer was not found and Homebrew is not installed.
Install Python from https://www.python.org/downloads/ (or Homebrew from https://brew.sh then 'brew install python@3.12'), then run this file again."
    fi
    find_python >/dev/null || fail "Python installation finished but a working Python 3.11+ was not found. Open a new Terminal window and run this file again."
fi

# ---------------------------------------------------------------- Download
FRESH=1
[ -d "$TARGET/.git" ] && FRESH=0
if [ "$FRESH" = "1" ] && [ -e "$TARGET" ]; then
    fail "$TARGET already exists but is not an ACL installation. Nothing was changed. Move those files elsewhere and retry."
fi

if [ "$FRESH" = "1" ]; then
    CHECKOUT="$(mktemp -d "${TMPDIR:-/tmp}/acl-install.XXXXXX")"
    cleanup() { [ -n "$CHECKOUT" ] && [ -d "$CHECKOUT" ] && rm -rf "$CHECKOUT"; }
    trap cleanup EXIT
    echo "Downloading ACL (a GitHub sign-in may be requested)..."
    # Start from production even though GitHub's default branch is a feature.
    git clone --quiet --no-checkout --branch main "$REPO_URL" "$CHECKOUT" \
        || fail "Download failed. You need access to ${REPO_NAME}: sign in with the company GitHub account ('brew install gh' then 'gh auth login'), and check your connection."
else
    CHECKOUT="$TARGET"
    REMOTE="$(git -C "$CHECKOUT" remote get-url origin 2>/dev/null)"
    case "$REMOTE" in
        https://github.com/${REPO_NAME}|https://github.com/${REPO_NAME}.git|git@github.com:${REPO_NAME}|git@github.com:${REPO_NAME}.git) ;;
        *) fail "This folder has a different Git origin; refusing to update it." ;;
    esac
    if [ -n "$(git -C "$CHECKOUT" status --porcelain --untracked-files=no)" ]; then
        fail "Tracked local changes present. Save/review them before updating ACL."
    fi
fi

git -C "$CHECKOUT" fetch --quiet origin main:refs/remotes/origin/main --tags \
    || fail "Could not fetch the production release from GitHub."

# ----------------------------------------------------------------- Release
# Install the NEWEST valid release reachable from origin/main. Main may be ahead
# of the last promoted release (its delivery run pending or cancelled); the
# update gate in the launcher simply waits for that, a fresh install must not.
# A valid release is an annotated acl-release-* tag whose own commit it records,
# which links a CI run of this repository and records success. (Same record the
# Windows installer falls back to when it has no GitHub token to ask the API;
# the launcher's gate re-checks every update.)
MAIN_SHA="$(git -C "$CHECKOUT" rev-parse 'origin/main^{commit}')"
case "$MAIN_SHA" in
    [0-9a-f]*) [ "${#MAIN_SHA}" = "40" ] || fail "Cannot resolve the production release." ;;
    *) fail "Cannot resolve the production release." ;;
esac

# pick_release <checkout> <main sha> <repo name>: prints "<tag> <commit>" for the
# newest valid release tag merged into the main sha; prints nothing and returns 1
# when none qualifies. Invalid tags are skipped, never fall back past main.
pick_release() {
    local checkout="$1" main_sha="$2" repo="$3" tag tag_sha annotation
    for tag in $(git -C "$checkout" tag --merged "$main_sha" --list 'acl-release-*' '--sort=-*committerdate'); do
        [ "$(git -C "$checkout" cat-file -t "$tag" 2>/dev/null)" = "tag" ] || continue
        tag_sha="$(git -C "$checkout" rev-parse "$tag^{commit}" 2>/dev/null)" || continue
        [ "${#tag_sha}" = "40" ] || continue
        annotation="$(git -C "$checkout" cat-file -p "$tag")"
        echo "$annotation" | grep -q "^object $tag_sha\$" || continue
        echo "$annotation" | grep -Eiq "https://github\.com/${repo}/actions/runs/[0-9]+" || continue
        echo "$annotation" | grep -Eiwq success || continue
        echo "$tag $tag_sha"
        return 0
    done
    return 1
}

PICKED="$(pick_release "$CHECKOUT" "$MAIN_SHA" "$REPO_NAME")" \
    || fail "Production has no annotated ACL release tag with a successful CI run. Nothing will be installed from an unverified commit."
RELEASE_TAG="${PICKED%% *}"
SHA="${PICKED##* }"
[ -n "$RELEASE_TAG" ] && [ "${#SHA}" = "40" ] \
    || fail "Production has no annotated ACL release tag with a successful CI run. Nothing will be installed from an unverified commit."
if [ "$FRESH" = "0" ] && ! git -C "$CHECKOUT" merge-base --is-ancestor HEAD "$SHA"; then
    fail "This installation has diverged from production; refusing to update it."
fi

# The release may predate Mac support: check BEFORE touching the tree,
# so nothing is checked out or moved and the user gets a plain message.
for mac_script in install_acl.sh start_dashboard.sh scripts/install_dashboard_shortcut.sh; do
    git -C "$CHECKOUT" cat-file -e "$SHA:$mac_script" 2>/dev/null \
        || fail "The current production release does not support macOS yet (missing $mac_script). Nothing was installed. Ask Evgeni for a Mac-ready release."
done

echo "Installing release $RELEASE_TAG ($SHA)"
git -C "$CHECKOUT" checkout --quiet --detach "$SHA" || fail "Could not check out the release."

if [ "$FRESH" = "1" ]; then
    mkdir -p "$(dirname "$TARGET")"
    mv "$CHECKOUT" "$TARGET" || fail "Could not move the download into $TARGET."
    CHECKOUT=""
fi

# --------------------------------------------------------- Shortcut + setup
bash "$TARGET/scripts/install_dashboard_shortcut.sh" \
    || echo "WARNING: Desktop shortcut could not be created; use $TARGET/start_dashboard.sh."

# install_acl.sh checks the machine and then hands over to the dashboard.
exec bash "$TARGET/install_acl.sh"
