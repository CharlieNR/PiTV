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
UPDATE_OUTPUT_PATH = Path("/tmp/pitv-settings-update.log")
CONTROL_KEY_PATH = Path("/etc/pitv/control.key")
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


def _open_update_output(*, append: bool):
    """Open the shared updater transcript without following symlinks in /tmp."""
    flags = os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    flags |= os.O_APPEND if append else os.O_TRUNC
    fd = os.open(str(UPDATE_OUTPUT_PATH), flags, 0o644)
    os.fchmod(fd, 0o644)
    return os.fdopen(fd, "a" if append else "w", encoding="utf-8", errors="replace")


def _append_update_output(message: str) -> None:
    with _open_update_output(append=True) as output:
        output.write(str(message))
        if not str(message).endswith("\n"):
            output.write("\n")
        output.flush()


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


def _backup_shared_control_key(config: dict, branch: str, updater_output: str) -> str:
    """Copy the privileged shared key into the exact private backup made by update.sh."""
    matches = re.findall(r"(?m)^Private settings backup:\s*(.+?)\s*$", updater_output)
    if not matches:
        raise RuntimeError(
            "update.sh did not report its settings backup directory; refusing to deploy without a key backup."
        )

    backup_dir = Path(matches[-1])
    expected_root = Path(config["home"]) / "pitv-update-backups"
    if expected_root.is_symlink() or backup_dir.is_symlink():
        raise RuntimeError("Refusing to back up control.key through a symbolic link.")
    try:
        root_resolved = expected_root.resolve(strict=True)
        backup_resolved = backup_dir.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("The private backup directory could not be verified.") from exc
    if (backup_resolved.parent != root_resolved or not backup_resolved.is_dir()
            or not backup_resolved.name.endswith("-" + branch)):
        raise RuntimeError("The update.sh backup directory is outside the expected private backup root.")
    if not CONTROL_KEY_PATH.exists():
        return "No shared control.key exists; no key copy was needed."
    if CONTROL_KEY_PATH.is_symlink() or not CONTROL_KEY_PATH.is_file():
        raise RuntimeError("Refusing to copy control.key because it is not a regular file.")
    key_copy = backup_resolved / "control.key"
    if key_copy.exists() or key_copy.is_symlink():
        raise RuntimeError("The control.key backup destination already exists.")
    account = pwd.getpwnam(config["user"])
    shutil.copy2(CONTROL_KEY_PATH, key_copy)
    os.chown(key_copy, account.pw_uid, account.pw_gid)
    os.chmod(key_copy, 0o600)
    return "Backed up the shared control key into update.sh's private settings backup."


def _standard_update_command(config: dict, branch: str) -> list[str]:
    """Build the same update.sh invocation used by `screen update`."""
    if branch not in ALLOWED_BRANCHES:
        raise RuntimeError("Only experimental and main branches are permitted.")
    return [
        "/usr/sbin/runuser", "-u", config["user"], "--",
        "/usr/bin/env",
        f"HOME={config['home']}",
        f"USER={config['user']}",
        f"LOGNAME={config['user']}",
        "GIT_TERMINAL_PROMPT=0",
        # TV Settings has no interactive OS-password/reboot prompt. The root
        # worker backs up control.key into update.sh's own settings backup, and
        # performs deploy.sh after the regular user-owned Git update completes.
        "PITV_SKIP_REBOOT_PROMPT=1",
        "PITV_SKIP_CONTROL_KEY_BACKUP=1",
        "PITV_DEFER_DEPLOY=1",
        "/usr/bin/bash", str(Path(config["repo"]) / "update.sh"), branch,
    ]


def _update_worker(branch: str) -> int:
    if os.geteuid() != 0:
        raise RuntimeError("The update worker must run as root inside its transient systemd unit.")
    # This worker is detached from pitv-menu.service, which deploy.sh restarts.
    if not re.fullmatch(r"[0-9a-f]{32}", os.environ.get("INVOCATION_ID", "")):
        raise RuntimeError("Update workers may only run as detached systemd tasks.")
    if branch not in ALLOWED_BRANCHES:
        raise RuntimeError("Only experimental and main branches are permitted.")
    config = _read_config()
    repo = config["repo"]

    _write_status("running", f"Starting the standard update script for {branch}.", branch)
    _append_log(f"Starting standard update.sh workflow for branch {branch}.")
    origin = _git_checked(config, ["remote", "get-url", "origin"], "Checking repository origin").strip()
    trusted_origins = {
        "https://github.com/charlienr/pitv",
        "https://github.com/charlienr/pitv.git",
        "git@github.com:charlienr/pitv.git",
        "ssh://git@github.com/charlienr/pitv.git",
    }
    if origin.rstrip("/").lower() not in trusted_origins:
        raise RuntimeError("Refusing update: origin must be the official CharlieNR/PiTV GitHub repository.")
    update_script = Path(repo) / "update.sh"
    if not update_script.is_file() or update_script.is_symlink():
        raise RuntimeError("The standard updater update.sh is missing from the PiTV checkout.")
    if not RUNNER_PATH.is_file() or RUNNER_PATH.is_symlink():
        raise RuntimeError("The trusted deployment runner is missing. Run deploy.sh from SSH once.")

    command = _standard_update_command(config, branch)
    _write_status("running", f"Running update.sh {branch}; live output is shown on the TV.", branch)
    _append_log(f"Executing the shared updater: {update_script} {branch}.")
    try:
        # This is the actual update.sh stdout/stderr, not a second implementation
        # of its Git/back-up logic. No artificial timeout can turn a slow update
        # into a false failure while the underlying command is still running.
        with _open_update_output(append=True) as output:
            completed = subprocess.run(
                command, cwd=repo, stdin=subprocess.DEVNULL,
                stdout=output, stderr=subprocess.STDOUT, check=False,
            )
    except Exception as exc:
        message = f"Updater could not complete: {type(exc).__name__}: {exc}"
        try:
            _append_update_output(f"\nERROR: {message}\n")
        except OSError:
            pass
        _write_status("failed", message, branch)
        _append_log(message)
        return 1

    if completed.returncode:
        message = f"Update failed with exit code {completed.returncode}. See the terminal output above."
        _append_update_output(f"\nERROR: {message}\n")
        _write_status("failed", message, branch)
        _append_log(message)
        return completed.returncode

    try:
        updater_output = UPDATE_OUTPUT_PATH.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        message = f"Could not read update.sh output: {type(exc).__name__}: {exc}"
        _append_update_output(f"\nERROR: {message}\n")
        _write_status("failed", message, branch)
        _append_log(message)
        return 1
    if f"No new updates are available on {branch}." in updater_output:
        message = f"No new updates are available on {branch}."
        _write_status("up-to-date", message, branch)
        _append_log(message)
        return 0

    # update.sh makes the account/settings backup as the user. Since it skips
    # the sudo-only key copy in TV mode, securely add the control key to that
    # very same backup here, before any deployment can alter the system.
    try:
        backup_message = _backup_shared_control_key(config, branch, updater_output)
        _append_update_output(backup_message + "\n")
        _append_log(backup_message)
    except Exception as exc:
        message = f"Settings backup failed: {type(exc).__name__}: {exc}"
        _append_update_output(f"\nERROR: {message}\n")
        _write_status("failed", message, branch)
        _append_log(message)
        return 1

    # update.sh has completed its normal backup/Git flow. It defers only the
    # privileged deploy step, which runs from a trusted root-owned copy so it
    # never needs an invisible sudo password prompt on the TV.
    _write_status("running", f"Installing updated PiTV files from {branch}.", branch)
    _append_log(f"Starting trusted root deployment for branch {branch}.")
    _append_update_output("\nStarting the trusted deployment step...\n")
    runner_fd, runner_copy = tempfile.mkstemp(prefix="pitv-deploy-runner-", suffix=".sh", dir="/run")
    os.close(runner_fd)
    try:
        # deploy.sh replaces the installed runner with its updated version.
        # Execute a separate copy so it cannot overwrite its own running source.
        shutil.copyfile(RUNNER_PATH, runner_copy)
        os.chown(runner_copy, 0, 0)
        os.chmod(runner_copy, 0o700)
        deploy_env = os.environ.copy()
        deploy_env.update({
            "HOME": config["home"],
            "USER": config["user"],
            "LOGNAME": config["user"],
            "SUDO_USER": config["user"],
            "PITV_SKIP_REBOOT_PROMPT": "1",
        })
        with _open_update_output(append=True) as output:
            deploy_result = subprocess.run(
                ["/usr/bin/bash", runner_copy], cwd=repo, env=deploy_env,
                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                check=False,
            )
    except Exception as exc:
        message = f"Deployment could not complete: {type(exc).__name__}: {exc}"
        try:
            _append_update_output(f"\nERROR: {message}\n")
        except OSError:
            pass
        _write_status("failed", message, branch)
        _append_log(message)
        return 1
    finally:
        try:
            os.unlink(runner_copy)
        except OSError:
            pass

    if deploy_result.returncode:
        message = f"Deployment failed with exit code {deploy_result.returncode}. See the terminal output above."
        _append_update_output(f"\nERROR: {message}\n")
        _write_status("failed", message, branch)
        _append_log(message)
        return deploy_result.returncode

    message = f"Standard update from {branch} completed."
    _write_status("complete", message, branch)
    _append_log(message)
    return 0


def _systemd_run_command(unit: str, argv: list[str]) -> list[str]:
    """Build a transient-unit command that returns as soon as systemd accepts it."""
    if not re.fullmatch(r"[a-zA-Z0-9_.@-]{1,100}", unit):
        raise RuntimeError("Invalid internal transient unit name.")
    return [
        "/usr/bin/systemd-run", "--no-block", "--quiet", "--collect",
        f"--unit={unit}", "--property=Type=oneshot",
        "--property=TimeoutStartSec=infinity", "--",
        *argv,
    ]


def _systemd_run(unit: str, argv: list[str]) -> None:
    command = _systemd_run_command(unit, argv)
    # --no-block is essential: otherwise systemd-run waits for the oneshot to
    # finish. The Settings request can then time out and report failure while
    # the detached updater continues to run unseen.
    result = subprocess.run(command, capture_output=True, text=True, timeout=12, check=False)
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(
            f"Could not start the system maintenance task{(': ' + detail[-220:]) if detail else ''}"
        )


def _has_active_update_worker() -> bool:
    """Inspect real systemd units instead of trusting a possibly stale status file."""
    result = subprocess.run(
        [
            "/usr/bin/systemctl", "list-units", "--all", "--type=service",
            "--no-legend", "pitv-update-*.service",
        ],
        capture_output=True, text=True, timeout=5, check=False,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(
            f"Could not check for an existing updater{(': ' + detail[-180:]) if detail else ''}"
        )
    active_states = {"active", "activating", "reloading", "deactivating"}
    for line in result.stdout.splitlines():
        fields = line.split()
        # systemctl list-units columns: UNIT LOAD ACTIVE SUB DESCRIPTION
        if (len(fields) >= 4 and fields[0].startswith("pitv-update-")
                and fields[2] in active_states):
            return True
    return False


def _start_update(branch: str) -> None:
    if branch not in ALLOWED_BRANCHES:
        raise RuntimeError("Only experimental and main branches are permitted.")
    _read_config()
    current = {}
    try:
        current = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    if current.get("status") in {"queued", "running"}:
        age = time.time() - int(current.get("updated_at", 0) or 0)
        # Protect the short launch race, but don't let a stale status JSON
        # permanently prevent retrying after its systemd worker disappeared.
        if age < 30:
            raise RuntimeError("A PiTV update has just been requested. Open View updater terminal for its output.")
    if _has_active_update_worker():
        raise RuntimeError("A PiTV update worker is genuinely still active. Open View updater terminal to inspect it.")
    if current.get("status") in {"queued", "running"}:
        _append_log("Ignoring stale maintenance status; no active PiTV update worker exists.")
    unit = f"pitv-update-{branch}-{int(time.time())}-{os.getpid()}"
    # Clear this attempt's transcript before the menu switches to the terminal.
    with _open_update_output(append=False):
        pass
    # Publish before launch, so the terminal view can appear before the first log line.
    _write_status("queued", f"Update from {branch} queued.", branch)
    _append_log(f"Queued detached update from {branch}.")
    try:
        _systemd_run(unit, [str(HELPER_PATH), "update-worker", branch])
    except Exception as exc:
        message = f"Could not start update worker: {type(exc).__name__}: {exc}"
        _write_status("failed", message, branch)
        _append_update_output(f"ERROR: {message}\n")
        raise


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
        branch = (
            args[-1] if len(args) >= 2 and args[0] in {"update", "update-worker"}
            and args[-1] in ALLOWED_BRANCHES else ""
        )
        message = f"Maintenance failed: {type(exc).__name__}: {exc}"
        _write_status("failed", message, branch)
        _append_log(message)
        # Failures before the worker starts must also be visible in the TV log.
        # Avoid writing into the transcript of an already-active update.
        if args and args[0] == "update":
            try:
                current = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
                busy = (current.get("status") in {"queued", "running"}
                        and time.time() - int(current.get("updated_at", 0)) < 24 * 3600)
            except (OSError, ValueError, TypeError):
                busy = False
            if not busy:
                try:
                    with _open_update_output(append=False) as output:
                        output.write(f"ERROR: {message}\n")
                except OSError:
                    pass
        elif args and args[0] == "update-worker":
            try:
                _append_update_output(f"ERROR: {message}\n")
            except OSError:
                pass
        print(f"PiTV maintenance failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
