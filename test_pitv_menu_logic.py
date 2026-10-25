"""Hardware-free tests for the V2 menu's state and Settings guards."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from unittest.mock import mock_open, patch

import tv_menu


class RecentGamesTests(unittest.TestCase):
    def test_carousel_lists_only_installed_priority_games(self):
        installed = {"vitetris", "ninvaders", "2048"}

        with patch.object(
            tv_menu, "resolve_binary",
            side_effect=lambda name: f"/usr/games/{name}" if name in installed else None,
        ):
            games = tv_menu._available_carousel_games()

        keys = [item[1] for item in games]
        self.assertEqual(keys, ["TETRIS", "INVADERS", "IMPOSTER", "2048"])

    def test_recent_games_are_unique_newest_first_and_install_filtered(self):
        tv_menu.pitv_state.write_value(
            "RECENT_GAMES",
            ["TETRIS", "TETRIS", "2048", "SNAKE", "BOULDERDASH"],
        )
        installed = {"vitetris", "phear"}

        with patch.object(
            tv_menu, "resolve_binary",
            side_effect=lambda name: f"/usr/games/{name}" if name in installed else None,
        ):
            games = tv_menu._recent_game_items()

        keys = [item[1] for item in games]
        self.assertEqual(keys, ["TETRIS", "SNAKE", "BOULDERDASH"])
        self.assertEqual(len(keys), len(set(keys)))

    def test_recording_a_game_moves_it_to_front_without_duplicates(self):
        tv_menu.pitv_state.write_value("RECENT_GAMES", ["SNAKE", "TETRIS", "SNAKE"])
        tv_menu._record_recent_game("TETRIS")
        tv_menu._record_recent_game("2048")
        saved = tv_menu.pitv_state.read_value("RECENT_GAMES", [])
        self.assertEqual(saved[:3], ["2048", "TETRIS", "SNAKE"])
        self.assertEqual(len(saved), len(set(saved)))
        self.assertLessEqual(len(saved), 12)


class SettingsGuardTests(unittest.TestCase):
    def setUp(self):
        names = (
            "settings_page", "settings_selected", "current_view",
            "settings_message", "settings_message_until",
            "settings_authorized_until", "settings_pending_page",
        )
        self.old_globals = {name: getattr(tv_menu, name) for name in names}

    def tearDown(self):
        for name, value in self.old_globals.items():
            setattr(tv_menu, name, value)

    def test_default_emergency_code_cannot_authorise_sensitive_actions(self):
        tv_menu.settings_page = "SYSTEM"
        tv_menu.settings_selected = 2
        tv_menu.current_view = "SETTINGS"
        tv_menu.settings_authorized_until = time.monotonic() + 300

        with patch.object(tv_menu.pitv_secrets, "is_default_emergency_code", return_value=True):
            tv_menu._settings_select_action(
                ("Update experimental", "", "", "CONFIRM_UPDATE_EXPERIMENTAL")
            )

        self.assertEqual(tv_menu.current_view, "SETTINGS")
        self.assertEqual(tv_menu.settings_page, "SYSTEM")
        self.assertEqual(tv_menu.settings_selected, 6)
        self.assertIn("private admin code", tv_menu.settings_message.lower())

    def test_expired_authorisation_reopens_admin_code_prompt(self):
        tv_menu.settings_authorized_until = 0
        tv_menu.settings_pending_page = "ROOT"
        tv_menu.current_view = "SETTINGS"

        tv_menu._settings_open_page("PINS")

        self.assertEqual(tv_menu.current_view, "SETTINGS_AUTH")
        self.assertEqual(tv_menu.settings_pending_page, "PINS")


class GraphicsRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.old_globals = {
            "_GRAPHIC_UI_FAILED": tv_menu._GRAPHIC_UI_FAILED,
            "_GRAPHIC_RENDER_RETRY_AFTER": tv_menu._GRAPHIC_RENDER_RETRY_AFTER,
            "_GRAPHIC_LAST_ERROR_LOG": tv_menu._GRAPHIC_LAST_ERROR_LOG,
            "_GRAPHIC_WAIT_SCREEN_CLEARED": tv_menu._GRAPHIC_WAIT_SCREEN_CLEARED,
        }

    def tearDown(self):
        for name, value in self.old_globals.items():
            setattr(tv_menu, name, value)

    def test_v2_render_error_blanks_and_retries_without_text_fallback(self):
        tv_menu._GRAPHIC_WAIT_SCREEN_CLEARED = False
        tv_menu._GRAPHIC_RENDER_RETRY_AFTER = 0.0
        with (
            patch.object(tv_menu.pitv_framebuffer, "clear_framebuffer") as clear,
            patch.object(tv_menu, "log"),
        ):
            tv_menu._handle_v2_render_error(RuntimeError("simulated render error"))

        clear.assert_called_once_with()
        self.assertTrue(tv_menu._GRAPHIC_UI_FAILED)
        self.assertTrue(tv_menu._GRAPHIC_WAIT_SCREEN_CLEARED)
        self.assertGreater(tv_menu._GRAPHIC_RENDER_RETRY_AFTER, time.monotonic())

    def test_successful_v2_render_clears_the_retry_state(self):
        tv_menu._GRAPHIC_UI_FAILED = True
        tv_menu._GRAPHIC_RENDER_RETRY_AFTER = time.monotonic() + 10
        tv_menu._GRAPHIC_WAIT_SCREEN_CLEARED = True
        tv_menu._GRAPHIC_LAST_ERROR_LOG = time.monotonic()
        tv_menu._mark_v2_render_success()
        self.assertFalse(tv_menu._GRAPHIC_UI_FAILED)
        self.assertEqual(tv_menu._GRAPHIC_RENDER_RETRY_AFTER, 0.0)
        self.assertFalse(tv_menu._GRAPHIC_WAIT_SCREEN_CLEARED)



class MaintenanceStatusTests(unittest.TestCase):
    def test_up_to_date_status_is_preserved_for_settings_ui(self):
        payload = {
            "status": "up-to-date",
            "message": "No new updates are available on experimental.",
            "branch": "experimental",
            "updated_at": int(time.time()),
        }
        with patch("builtins.open", mock_open(read_data=json.dumps(payload))):
            status = tv_menu._settings_maintenance_status()

        self.assertEqual(status["status"], "up-to-date")
        self.assertEqual(status["message"], "No new updates are available on experimental.")
        self.assertEqual(status["branch"], "experimental")


class TVPowerSleepRequestTests(unittest.TestCase):
    """TV standby must stop foreground activity without shutting down the Pi."""

    def setUp(self):
        self.old_power_state = tv_menu.tv_power_state
        self.old_tv_input = tv_menu.tv_input_phys
        self.old_ui_state = tv_menu.tv_ui_state
        self.old_input_queue = list(tv_menu.input_queue)

    def tearDown(self):
        tv_menu.tv_power_state = self.old_power_state
        tv_menu.tv_input_phys = self.old_tv_input
        tv_menu.tv_ui_state = self.old_ui_state
        tv_menu.input_queue[:] = self.old_input_queue

    def test_new_standby_state_requests_foreground_activity_exit(self):
        tv_menu.tv_power_state = "on"
        tv_menu.input_queue[:] = ["LEFT", "SELECT"]

        with patch.object(tv_menu, "TV_STATE_FILE", "/dev/null"), patch.object(tv_menu, "log"):
            tv_menu._set_tv_power("off")

        self.assertEqual(tv_menu.tv_power_state, "off")
        self.assertEqual(tv_menu.input_queue, ["HOME"])

    def test_repeated_standby_poll_does_not_repeat_exit_signal(self):
        tv_menu.tv_power_state = "off"
        tv_menu.input_queue[:] = ["SELECT"]

        with patch.object(tv_menu, "TV_STATE_FILE", "/dev/null"), patch.object(tv_menu, "log"):
            tv_menu._set_tv_power("off")

        self.assertEqual(tv_menu.input_queue, ["SELECT"])

    def test_tv_power_on_does_not_change_input_or_send_cec(self):
        tv_menu.tv_power_state = "off"
        tv_menu.tv_input_phys = "10:00"
        tv_menu.input_queue[:] = ["HOME"]

        with (
            patch.object(tv_menu, "TV_STATE_FILE", "/dev/null"),
            patch.object(tv_menu, "_run_cec_cmd") as cec_command,
            patch.object(tv_menu, "log"),
        ):
            tv_menu._set_tv_power("on")

        self.assertEqual(tv_menu.tv_power_state, "on")
        self.assertEqual(tv_menu.tv_input_phys, "10:00")
        self.assertEqual(tv_menu.input_queue, ["HOME"])
        cec_command.assert_not_called()

    def test_ui_state_publisher_writes_sleeping_state(self):
        old_state = tv_menu.tv_ui_state
        with tempfile.TemporaryDirectory() as temp_dir:
            state_path = f"{temp_dir}/pitv-ui-state"
            with patch.object(tv_menu, "TV_UI_STATE_FILE", state_path):
                tv_menu._set_tv_ui_state("sleeping", force=True)
                with open(state_path, encoding="ascii") as handle:
                    self.assertEqual(handle.read(), "sleeping")
        tv_menu.tv_ui_state = old_state


if __name__ == "__main__":
    unittest.main(verbosity=2)
