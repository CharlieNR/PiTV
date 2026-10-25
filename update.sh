#!/usr/bin/env bash
# PiTV self-update helper, invoked by the installed screen command.
set -euo pipefail

TARGET_BRANCH="${1:-}"
case "$TARGET_BRANCH" in
    experimental|main) ;;
    *)
        echo "Usage: update.sh [experimental|main] [--force --reset]" >&2
        exit 2
        ;;
esac
shift || true

FORCE_RESET=0
if [[ "$#" -eq 2 && ( ( "$1" == "--force" && "$2" == "--reset" ) || ( "$1" == "--reset" && "$2" == "--force" ) ) ]]; then
    FORCE_RESET=1
elif [[ "$#" -ne 0 ]]; then
    echo "Usage: update.sh [experimental|main] [--force --reset]" >&2
    echo "The reset option requires BOTH --force and --reset." >&2
    exit 2
fi

# Reset is deliberately limited to experimental; main is the user's backup
# branch and must never be destructively reset by this shortcut.
if [[ "$FORCE_RESET" -eq 1 && "$TARGET_BRANCH" != "experimental" ]]; then
    echo "Refusing to reset persistent data while updating main." >&2
    echo "Use: screen update --force --reset" >&2
    exit 2
fi

if [[ "$(id -u)" -eq 0 ]]; then
    echo "Please run this as your normal PiTV login user, without sudo." >&2
    exit 1
fi

REPO="${PITV_REPO:-$HOME/PiTV}"
if [[ ! -d "$REPO/.git" ]]; then
    echo "PiTV Git checkout not found at: $REPO" >&2
    echo "Expected the repository at ~/PiTV, or set PITV_REPO to its location." >&2
    exit 1
fi
REPO="$(git -C "$REPO" rev-parse --show-toplevel)"
if [[ "$(git -C "$REPO" remote get-url origin)" != *"CharlieNR/PiTV"* ]]; then
    echo "Refusing to update: origin is not recognised as CharlieNR/PiTV." >&2
    echo "Repository: $REPO" >&2
    exit 1
fi

STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_ROOT="${PITV_BACKUP_DIR:-$HOME/pitv-update-backups}"
BACKUP_DIR="$BACKUP_ROOT/$STAMP-$TARGET_BRANCH"

if [[ "$FORCE_RESET" -eq 1 ]]; then
    echo
    echo "=============================================================="
    echo "FORCE RESET: ALL SAVED PiTV ACCOUNTS, PINS AND SETTINGS WILL"
    echo "BE DELETED. No PiTV state backup will be made."
    echo "A sudo password will be requested before anything is deleted."
    echo "=============================================================="
else
    # Back up device-only settings before touching the checkout or deploying.
    # The backup is private and outside Git.
    umask 077
    mkdir -p "$BACKUP_DIR"
    chmod 700 "$BACKUP_ROOT" "$BACKUP_DIR"

    if [[ -d "$HOME/.pitv" ]]; then
        cp -a "$HOME/.pitv" "$BACKUP_DIR/pitv"
        chmod -R go-rwx "$BACKUP_DIR/pitv"
        echo "Backed up PiTV accounts, codes and settings."
    else
        echo "No ~/.pitv settings directory found; nothing to back up there."
    fi

    if sudo test -f /etc/pitv/control.key; then
        sudo cp -p /etc/pitv/control.key "$BACKUP_DIR/control.key"
        sudo chown "$(id -u):$(id -g)" "$BACKUP_DIR/control.key"
        chmod 600 "$BACKUP_DIR/control.key"
        echo "Backed up the shared control key."
    fi

    echo "Private settings backup: $BACKUP_DIR"
fi

echo "Updating $REPO from branch '$TARGET_BRANCH'..."

# Confirm that the target exists before shelving local work.
git -C "$REPO" fetch origin "refs/heads/$TARGET_BRANCH:refs/remotes/origin/$TARGET_BRANCH"

# Preserve local code edits instead of overwriting them. This is a Git source
# stash, not a backup of the PiTV account/settings save file.
if [[ -n "$(git -C "$REPO" status --porcelain --untracked-files=all)" ]]; then
    echo "Local repository edits found; preserving them in Git stash..."
    git -C "$REPO" stash push --include-untracked \
        -m "PiTV auto-backup before update to $TARGET_BRANCH at $STAMP"
    echo "Local edits are preserved. Inspect them later with: git -C \"$REPO\" stash list"
fi

if git -C "$REPO" show-ref --verify --quiet "refs/heads/$TARGET_BRANCH"; then
    git -C "$REPO" switch "$TARGET_BRANCH"
else
    git -C "$REPO" switch --track -c "$TARGET_BRANCH" "origin/$TARGET_BRANCH"
fi

# Fast-forward only: never discard local commits or rewrite branch history.
git -C "$REPO" pull --ff-only origin "$TARGET_BRANCH"

if [[ "$FORCE_RESET" -eq 1 ]]; then
    echo
    echo "Authentication is required before deleting saved state."
    # Invalidate a cached sudo ticket so a password is requested where the
    # machine's sudo policy permits it. If authentication fails, delete nothing.
    sudo -k
    if ! sudo -v; then
        echo "Authentication failed; saved PiTV data has NOT been deleted." >&2
        exit 1
    fi

    # Delete the new central save file and all older split-file saves. Removing
    # the legacy files as well prevents deployment from importing them back.
    rm -f \
        "$HOME/.pitv/pitv-state.json" \
        "$HOME/.pitv/guests.json" \
        "$HOME/.pitv/admin.json" \
        "$HOME/.pitv/pins.json" \
        "$HOME/.pitv/sky.json" \
        "$HOME/.pitv/network.json" \
        "$HOME/.pitv/pairing.json" \
        "$HOME/.pitv/lock-state.json" \
        "$HOME/.pitv/emergency-code" \
        "$HOME/.pitv/control-key" \
        "$HOME/.pitv/portal-secret" \
        "$HOME/.pitv/admin-secret" \
        "$HOME/.pitv/remote-keyboard-lease.json" \
        "$HOME/.pitv/remote-keyboard-lease.lock"
    sudo rm -f /etc/pitv/control.key
    echo "Saved PiTV state deleted; deployment will create a fresh default save file."
    echo "No PiTV account/settings backup was created."
fi

cd "$REPO"
echo "Deploying the updated '$TARGET_BRANCH' version..."
# Invoke through Bash so deployment still works if Git checkout loses the executable bit.
bash ./deploy.sh

# Return the user's shell to their home directory after deployment.
cd "$HOME"

echo
echo "PiTV update complete on branch '$TARGET_BRANCH'."
if [[ "$FORCE_RESET" -eq 0 ]]; then
    echo "Private settings backup: $BACKUP_DIR"
else
    echo "PiTV was reset without making a PiTV settings backup."
fi
echo "Returned to: $HOME"
echo "The deploy script offered the normal reboot prompt; no reboot was forced."
