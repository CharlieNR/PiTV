#!/usr/bin/env bash
# PiTV self-update helper, invoked by the installed screen command.
set -euo pipefail

TARGET_BRANCH="${1:-}"
case "$TARGET_BRANCH" in
    experimental|main) ;;
    *)
        echo "Usage: update.sh [experimental|main]" >&2
        exit 2
        ;;
esac

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

# Back up device-only settings before touching the checkout or deploying code.
# The backup is private and outside Git.
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_ROOT="${PITV_BACKUP_DIR:-$HOME/pitv-update-backups}"
BACKUP_DIR="$BACKUP_ROOT/$STAMP-$TARGET_BRANCH"
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
echo "Updating $REPO from branch '$TARGET_BRANCH'..."

# Confirm that the target exists before shelving local work.
git -C "$REPO" fetch origin "refs/heads/$TARGET_BRANCH:refs/remotes/origin/$TARGET_BRANCH"

# Preserve local edits instead of overwriting them. They are stashed and are
# deliberately not auto-applied over the updated code.
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

cd "$REPO"
echo "Deploying the updated '$TARGET_BRANCH' version..."
./deploy.sh

# Return the user's shell to their home directory after deployment.
cd "$HOME"

echo
echo "PiTV update complete on branch '$TARGET_BRANCH'."
echo "Private settings backup: $BACKUP_DIR"
echo "Returned to: $HOME"
echo "The deploy script offered the normal reboot prompt; no reboot was forced."
