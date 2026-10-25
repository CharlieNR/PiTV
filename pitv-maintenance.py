#!/usr/bin/env python3
"""Restricted, root-owned PiTV maintenance helper.

Only this fixed action list is accepted. The TV interface never passes shell
commands, arbitrary paths, or free-form service names to a privileged process.
The updater fetches only the requested PiTV branch from the configured origin,
backs up device state, then invokes the last installed root-owned deployment
runner from a transient systemd service.
"""
from __future__ import annotations

import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

CONFIG_PATH = Path("/etc/pitv/maintenance.json")
STATUS_PATH = Path("/tmp/pitv-maintenance-status.json")
LOG_PATH = Path("/tmp/pitv-settings-maintenance.log")
RUNNER_PATH = Path("/usr/local/lib/pitv/deploy-runner.sh")
HELPER_PATH = Path("/usr/local/sbin/pitv-maintenance")
ALLOWED_BRANCHES = {"experimental", "main"}


def _append_log(message: str) -> None:
    """Append a bounded line without following attacker-planted /tmp symlinks."""
    message = re.sub(r"[\r\n\t]+", " ", str(message)).strip()[:700]
    fd = -1
    try:
        # /tmp is writable by other local processes; O_NOFOLLOW prevents this
        # root helper from appending log text to an arbitrary symlink target.
        flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(LOG_PATH), flags, 0o644)
        os.fchmod(fd, 0o644)
        handle = os.fdopen(fd, "a", encoding="utf-8")
        fd = -1
        with handle:
            handle.write(
                f"[{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}] {message}\n"
            )
    except OSError:
        pass
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


def _write_status(status: str, message: str, branch: str = "") -> None:
    """Atomically publish non-secret maintenance state for the TV UI."""
    data = {
        "status": status,
        "message": str(message)[:180],
        "branch": branch if branch in ALLOWED_BRANCHES else "",
        "updated_at": int(time.time()),
    }
    try:
        fd, name = tempfile.mkstemp(prefix=".pitv-maintenance-", dir="/tmp", text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(name, 0o644)
            os.replace(name, STATUS_PATH)
        finally:
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass
    except OSError as exc:
        _append_log(f"Could not publish maintenance status: {type(exc).__name__}")


def _read_config() -> dict:
    if os.geteuid() != 0:
        raise RuntimeError("This helper must be invoked via its restricted sudo rule.")
    if CONFIG_PATH.is_symlink():
        raise RuntimeError("Maintenance configuration must not be a symlink.")
    st = CONFIG_PATH.stat()
    if st.st_uid != 0 or st.st_mode & 0o022:
        raise RuntimeError("Maintenance configuration must be root-owned and not group/world-writable.")
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    user = str(data.get("user", ""))
    home = str(data.get("home", ""))
    repo = str(data.get("repo", ""))
    account = pwd.getpwnam(user)
    if not user or home != account.pw_dir:
        raise RuntimeError("Configured PiTV user/home does not match the system account.")
    expected_repo = str(Path(home) / "PiTV")
    if repo != expected_repo or not (Path(repo) / ".git").is_dir():
        raise RuntimeError("PiTV Git checkout was not found at the configured home path.")
    return {"user": user, "home": home, "repo": repo}


def _as_user(config: dict, git_args: list[str], timeout: int = 90) -> subprocess.CompletedProcess:
    command = [
        "/usr/sbin/runuser", "-u", config["user"], "--",
        "/usr/bin/env",
        f"HOME={config['home']}", f"USER={config['user']}",
        f"LOGNAME={config['user']}", "GIT_TERMINAL_PROMPT=0",
        "/usr/bin/git", "-C", config["repo"], *git_args,
    ]
    return subprocess.run(command, cwd=config["home"], capture_output=True,
                          text=True, timeout=timeout, check=False)


def _git_checked(config: dict, args: list[str], label: str) -> str:
    result = _as_user(config, args)
    if result.returncode:
        _append_log(f"{label} failed (exit {result.returncode}).")
        raise RuntimeError(f"{label} failed. See Settings → Logs.")
    return (result.stdout or "").strip()


def _is_up_to_date(
    branch: str,
    current_branch: str,
    local_head: str,
    remote_head: str,
    working_tree_status: str,
) -> bool:
    """Only skip an update when the requested branch is current, clean and at origin."""
    return (
        branch in ALLOWED_BRANCHES
        and current_branch == branch
        and bool(local_head)
        and local_head == remote_head
        and not working_tree_status.strip()
    )


def _install_root_helper_after_update(config: dict) -> None:
    """Install the refreshed fixed-action helper from the trusted checkout."""
    candidate = Path(config["repo"]) / "pitv-maintenance.py"
    if not candidate.is_file() or candidate.is_symlink():
        # Stable/main may intentionally predate the helper; preserve the
        # currently installed helper rather than replacing it with absent code.
        return
    temporary = HELPER_PATH.with_name(HELPER_PATH.name + ".new")
    shutil.copyfile(candidate, temporary)
    os.chown(temporary, 0, 0)
    os.chmod(temporary, 0o755)
    os.replace(temporary, HELPER_PATH)


def _install_next_runner(config: dict) -> None:
    """Keep the trusted, non-self-overwriting deployment runner current."""
    candidate = Path(config["repo"]) / "deploy.sh"
    if not candidate.is_file() or candidate.is_symlink():
        return
    RUNNER_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = RUNNER_PATH.with_name(RUNNER_PATH.name + ".new")
    shutil.copyfile(candidate, temporary)
    os.chown(temporary, 0, 0)
    os.chmod(temporary, 0o755)
    os.replace(temporary, RUNNER_PATH)


def _backup_state(config: dict, branch: str) -> str:
    home = Path(config["home"])
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
    root = home / "pitv-update-backups"
    if root.is_symlink():
        raise RuntimeError("Refusing to use a symlink as the private backup directory.")
    destination = root / f"{stamp}-{branch}"
    root.mkdir(mode=0o700, exist_ok=True)
    uid = pwd.getpwnam(config["user"]).pw_uid
    gid = pwd.getpwnam(config["user"]).pw_gid
    os.chown(root, uid, gid)
    os.chmod(root, 0o700)
    if destination.exists() or destination.is_symlink():
        raise RuntimeError("The private backup destination already exists.")
    destination.mkdir(mode=0o700)
    os.chown(destination, uid, gid)
    os.chmod(destination, 0o700)
    pitv_dir = home / ".pitv"
    if pitv_dir.is_symlink():
        raise RuntimeError("Refusing to back up ~/.pitv through a symbolic link.")
    if pitv_dir.is_dir():
        target = destination / "pitv"
        # Copy symlinks as symlinks; do not let a root backup traverse paths
        # placed by the unprivileged PiTV account outside its own state folder.
        shutil.copytree(pitv_dir, target, symlinks=True)
        for item in [target, *target.rglob("*")]:
            if item.is_symlink():
                continue
            try:
                os.chown(item, uid, gid, follow_symlinks=False)
                os.chmod(item, 0o700 if item.is_dir() else 0o600, follow_symlinks=False)
            except OSError:
                pass
    control_key = Path("/etc/pitv/control.key")
    if control_key.is_file():
        shutil.copy2(control_key, destination / "control.key")
        key_copy = destination / "control.key"
        os.chown(key_copy, pwd.getpwnam(config["user"]).pw_uid,
                 pwd.getpwnam(config["user"]).pw_gid)
        os.chmod(key_copy, 0o600)
    return str(destination)


def _update_worker(branch: str) -> int:
    if os.geteuid() != 0:
        raise RuntimeError("The update worker must run as root inside its transient systemd unit.")
    # systemd sets INVOCATION_ID for transient services. This prevents someone
    # from using the sudoers entry to invoke the long-running worker directly
    # inside the menu service's own process group.
    if not re.fullmatch(r"[0-9a-f]{32}", os.environ.get("INVOCATION_ID", "")):
        raise RuntimeError("Update workers may only run as detached systemd tasks.")
    if branch not in ALLOWED_BRANCHES:
        raise RuntimeError("Only experimental and main branches are permitted.")
    config = _read_config()
    repo = config["repo"]

    _write_status("running", f"Preparing update from {branch}.", branch)
    _append_log(f"Update requested for branch {branch}.")
    origin = _git_checked(config, ["remote", "get-url", "origin"], "Checking repository origin").strip()
    trusted_origins = {
        "https://github.com/charlienr/pitv",
        "https://github.com/charlienr/pitv.git",
        "git@github.com:charlienr/pitv.git",
        "ssh://git@github.com/charlienr/pitv.git",
    }
    if origin.rstrip("/").lower() not in trusted_origins:
        raise RuntimeError("Refusing update: origin must be the official CharlieNR/PiTV GitHub repository.")
    if not RUNNER_PATH.is_file() or RUNNER_PATH.is_symlink():
        raise RuntimeError("Trusted deployment runner is missing. Run deploy.sh from the PiTV checkout once.")

    _write_status("running", f"Checking {branch} for new commits.", branch)
    _append_log(f"Checking origin/{branch} before creating an update backup.")
    _git_checked(config, ["fetch", "origin", f"refs/heads/{branch}:refs/remotes/origin/{branch}"],
                 "Fetching target branch")

    # A repeated Settings update is a successful no-op only when the requested
    # branch is checked out, the working tree is clean, and HEAD exactly matches
    # the freshly fetched remote commit. Otherwise continue with backup/stash/
    # fast-forward/deploy as normal.
    current_branch = _git_checked(config, ["branch", "--show-current"], "Checking current branch")
    local_head = _git_checked(config, ["rev-parse", "HEAD"], "Checking local source revision")
    remote_head = _git_checked(
        config, ["rev-parse", f"refs/remotes/origin/{branch}"], "Checking remote source revision"
    )
    working_tree = _git_checked(
        config, ["status", "--porcelain", "--untracked-files=all"], "Checking local edits"
    )
    if _is_up_to_date(branch, current_branch, local_head, remote_head, working_tree):
        message = f"No new updates are available on {branch}."
        _write_status("up-to-date", message, branch)
        _append_log(message)
        return 0

    backup = _backup_state(config, branch)
    _append_log(f"Private device-settings backup created at {backup}.")
    _write_status("running", "Preserving local edits and preparing deployment.", branch)

    dirty = _git_checked(config, ["status", "--porcelain", "--untracked-files=all"],
                          "Checking local edits")
    if dirty:
        _append_log("Local edits found; preserving them in Git stash.")
        _git_checked(config, ["stash", "push", "--include-untracked",
                              "-m", f"PiTV UI update backup before {branch}"],
                     "Stashing local edits")

    local_ref = _as_user(config, ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"])
    if local_ref.returncode == 0:
        _git_checked(config, ["switch", branch], "Switching target branch")
    else:
        _git_checked(config, ["switch", "--track", "-c", branch, f"origin/{branch}"],
                     "Creating local target branch")
    _git_checked(config, ["pull", "--ff-only", "origin", branch], "Fast-forwarding target branch")

    # Refresh the helper only from the configured repository after its
    # fetch/pull has completed. The current Python process continues on its
    # already-loaded code; future calls use the refreshed installed file.
    _install_root_helper_after_update(config)
    _write_status("running", "Installing updated PiTV files and restarting services.", branch)
    _append_log(f"Starting trusted deployment runner for {branch}.")
    env = os.environ.copy()
    env.update({
        "HOME": config["home"],
        "SUDO_USER": config["user"],
        "PITV_SKIP_REBOOT_PROMPT": "1",
    })
    with LOG_PATH.open("a", encoding="utf-8") as log_file:
        completed = subprocess.run(
            ["/usr/bin/bash", str(RUNNER_PATH)],
            cwd=repo, env=env, stdin=subprocess.DEVNULL,
            stdout=log_file, stderr=subprocess.STDOUT,
            timeout=1800, check=False,
        )
    if completed.returncode:
        _write_status("failed", f"Deployment failed with exit code {completed.returncode}.", branch)
        _append_log(f"Deployment failed (exit {completed.returncode}); private settings backup retained.")
        return completed.returncode

    # The deployed runner has copied the new source to /opt/pitv. Keep the
    # next detached update runner in sync without ever overwriting a running
    # shell script's own file.
    _install_next_runner(config)
    _write_status("complete", f"PiTV update from {branch} completed.", branch)
    _append_log(f"PiTV update from {branch} completed.")
    return 0


def _systemd_run(unit: str, argv: list[str]) -> None:
    if not re.fullmatch(r"[a-zA-Z0-9_.@-]{1,100}", unit):
        raise RuntimeError("Invalid internal transient unit name.")
    command = [
        "/usr/bin/systemd-run", "--quiet", "--collect",
        f"--unit={unit}", "--property=Type=oneshot", "--",
        *argv,
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=12, check=False)
    if result.returncode:
        raise RuntimeError("Could not start the system maintenance task. Check Settings → Logs.")


def _start_update(branch: str) -> None:
    if branch not in ALLOWED_BRANCHES:
        raise RuntimeError("Only experimental and main branches are permitted.")
    _read_config()
    try:
        current = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        if current.get("status") == "running" and time.time() - int(current.get("updated_at", 0)) < 3600:
            raise RuntimeError("A PiTV maintenance task is already running.")
    except FileNotFoundError:
        pass
    except json.JSONDecodeError:
        pass
    unit = f"pitv-update-{branch}-{int(time.time())}"
    # Publish before asking systemd to start the worker. The worker can start
    # immediately and must be able to replace this with "running" without a
    # late "queued" write racing over its progress.
    _write_status("queued", f"Update from {branch} queued.", branch)
    _append_log(f"Queued detached update from {branch}.")
    _systemd_run(unit, [str(HELPER_PATH), "update-worker", branch])


def _start_system_action(action: str) -> None:
    _read_config()
    unit = f"pitv-maint-{action}-{int(time.time())}"
    if action == "restart-menu":
        argv = ["/usr/bin/systemctl", "restart", "pitv-menu.service"]
        message = "PiTV menu restart requested."
    elif action == "reboot":
        config = _read_config()
        home = Path(config["home"])
        flag = home / "shutdown_flag.txt"
        # A fixed reason string; never accept arbitrary file paths or shell text.
        flag.write_text("Restart requested from PiTV V2 Settings.\n", encoding="utf-8")
        os.chown(flag, pwd.getpwnam(config["user"]).pw_uid, pwd.getpwnam(config["user"]).pw_gid)
        os.chmod(flag, 0o600)
        argv = ["/usr/bin/systemctl", "reboot"]
        message = "Raspberry Pi reboot requested."
    else:
        raise RuntimeError("Unsupported system action.")
    # Publish before launch: restarting pitv-menu can stop the caller's service
    # cgroup almost immediately after systemd has accepted the transient unit.
    _write_status("queued", message)
    _append_log(message)
    _systemd_run(unit, argv)


def _print_status() -> None:
    try:
        data = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        print(f"{data.get('status', 'unknown')}: {data.get('message', '')}")
    except (OSError, json.JSONDecodeError):
        print("No PiTV maintenance task has run in this boot.")


def main(argv: list[str]) -> int:
    if os.geteuid() != 0:
        print("Access denied: invoke this tool through its restricted sudo rule.", file=sys.stderr)
        return 2
    args = argv[1:]
    try:
        if args == ["status"]:
            _read_config()
            _print_status()
            return 0
        if args == ["restart-menu"]:
            _start_system_action("restart-menu")
            return 0
        if args == ["reboot"]:
            _start_system_action("reboot")
            return 0
        if len(args) == 2 and args[0] == "update" and args[1] in ALLOWED_BRANCHES:
            _start_update(args[1])
            return 0
        if len(args) == 2 and args[0] == "update-worker" and args[1] in ALLOWED_BRANCHES:
            return _update_worker(args[1])
        print("Usage: pitv-maintenance <status|restart-menu|reboot|update experimental|update main>", file=sys.stderr)
        return 2
    except Exception as exc:
        _write_status("failed", f"Maintenance failed: {type(exc).__name__}: {exc}")
        _append_log(f"Maintenance failed: {type(exc).__name__}: {exc}")
        print(f"PiTV maintenance failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
