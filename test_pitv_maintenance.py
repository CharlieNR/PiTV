"""Hardware-free tests for PiTV's update no-op decision."""
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


class UpToDateDetectionTests(unittest.TestCase):
    def test_clean_requested_branch_at_remote_head_is_up_to_date(self):
        self.assertTrue(pitv_maintenance._is_up_to_date(
            "experimental", "experimental", "abc123", "abc123", ""
        ))

    def test_local_changes_prevent_up_to_date_shortcut(self):
        self.assertFalse(pitv_maintenance._is_up_to_date(
            "experimental", "experimental", "abc123", "abc123", " M tv_menu.py"
        ))

    def test_different_local_and_remote_revisions_need_update(self):
        self.assertFalse(pitv_maintenance._is_up_to_date(
            "experimental", "experimental", "abc123", "def456", ""
        ))

    def test_different_checked_out_branch_needs_switch_and_deploy(self):
        self.assertFalse(pitv_maintenance._is_up_to_date(
            "experimental", "main", "abc123", "abc123", ""
        ))

    def test_unrecognised_branch_is_never_reported_up_to_date(self):
        self.assertFalse(pitv_maintenance._is_up_to_date(
            "other", "other", "abc123", "abc123", ""
        ))


if __name__ == "__main__":
    unittest.main(verbosity=2)
