"""Hardware-free tests for Settings and the CLI sharing update.sh."""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
