"""Hardware-free tests for Settings and the CLI sharing update.sh."""
from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HELPER_PATH = Path(__file__).with_name("pitv-maintenance.py")
SPEC = importlib.util.spec_from_file_location("pitv_maintenance", HELPER_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Could not load pitv-maintenance.py for tests.")
pitv_maintenance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pitv_maintenance)


class StandardUpdaterCommandTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "user": "charlie",
            "home": "/home/charlie",
            "repo": "/home/charlie/PiTV",
        }

    def test_experimental_settings_update_runs_shared_update_script(self):
        command = pitv_maintenance._standard_update_command(self.config, "experimental")
        self.assertEqual(
            command[-3:],
            ["/usr/bin/bash", "/home/charlie/PiTV/update.sh", "experimental"],
        )
        self.assertIn("PITV_SKIP_REBOOT_PROMPT=1", command)
        self.assertIn("PITV_DEFER_DEPLOY=1", command)
        self.assertIn("GIT_TERMINAL_PROMPT=0", command)

    def test_main_settings_update_uses_the_standard_main_branch_argument(self):
        command = pitv_maintenance._standard_update_command(self.config, "main")
        self.assertEqual(
            command[-3:],
            ["/usr/bin/bash", "/home/charlie/PiTV/update.sh", "main"],
        )

    def test_update_runs_as_the_normal_pitv_user(self):
        command = pitv_maintenance._standard_update_command(self.config, "experimental")
        self.assertEqual(command[0:4], ["/usr/sbin/runuser", "-u", "charlie", "--"])
        self.assertIn("/usr/bin/env", command)
        self.assertNotEqual(command[0], "/usr/bin/sudo")

    def test_unknown_branch_is_refused(self):
        with self.assertRaises(RuntimeError):
            pitv_maintenance._standard_update_command(self.config, "other")

    def test_systemd_update_launch_is_nonblocking(self):
        command = pitv_maintenance._systemd_run_command(
            "pitv-update-experimental-12345",
            ["/usr/local/sbin/pitv-maintenance", "update-worker", "experimental"],
        )
        self.assertIn("--no-block", command)
        self.assertIn("--property=TimeoutStartSec=infinity", command)
        self.assertLess(command.index("--no-block"), command.index("--"))

    def test_unsafe_systemd_unit_name_is_refused(self):
        with self.assertRaises(RuntimeError):
            pitv_maintenance._systemd_run_command(
                "pitv-update-experimental; reboot",
                ["/usr/local/sbin/pitv-maintenance", "update-worker", "experimental"],
            )

    def test_control_key_backup_path_must_match_update_sh_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            root = home / "pitv-update-backups"
            backup = root / "20261025-123000-experimental"
            backup.mkdir(parents=True)
            config = {"home": str(home), "user": "nobody"}
            output = f"Private settings backup: {backup}\n"
            with patch.object(pitv_maintenance, "CONTROL_KEY_PATH", home / "missing-control.key"):
                result = pitv_maintenance._backup_shared_control_key(config, "experimental", output)
            self.assertIn("No shared control.key", result)

    def test_control_key_is_copied_to_the_exact_private_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            root = home / "pitv-update-backups"
            backup = root / "20261025-123000-experimental"
            backup.mkdir(parents=True)
            key = home / "control.key"
            key.write_text("test-key")
            config = {"home": str(home), "user": "nobody"}
            output = f"Private settings backup: {backup}\n"
            fake_account = type("Account", (), {"pw_uid": os.getuid(), "pw_gid": os.getgid()})()
            with (
                patch.object(pitv_maintenance, "CONTROL_KEY_PATH", key),
                patch.object(pitv_maintenance.pwd, "getpwnam", return_value=fake_account),
            ):
                result = pitv_maintenance._backup_shared_control_key(config, "experimental", output)
            self.assertTrue((backup / "control.key").is_file())
            self.assertEqual((backup / "control.key").read_text(), "test-key")
            self.assertEqual((backup / "control.key").stat().st_mode & 0o777, 0o600)
            self.assertIn("Backed up the shared control key", result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
