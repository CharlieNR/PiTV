"""Hardware-free smoke tests for the V2 carousel state and strip renderer."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from PIL import Image

import pitv_tvui


class CarouselSmokeTests(unittest.TestCase):
    def setUp(self):
        self.saved_carousel = {
            key: (list(value) if key == "items" else value)
            for key, value in pitv_tvui._CAROUSEL.items()
        }
        with pitv_tvui._CAROUSEL_LOCK:
            pitv_tvui._CAROUSEL.update({
                "view": "MENU",
                "items": [
                    ("TETRIS", "TETRIS", "Puzzle game"),
                    ("SPACE INVADERS", "INVADERS", "Arcade game"),
                    ("IMPOSTER", "IMPOSTER", "Party game"),
                    ("2048", "2048", "Number puzzle"),
                ],
                "position": 0.0,
                "target": None,
                "generation": 100,
                "autoplay": False,
                "interval": 8.0,
                "pause_until": 0.0,
                "next_auto": 9999999999.0,
                "error": False,
                "revision": 100,
            })
        pitv_tvui._CAROUSEL_WAKE.clear()

    def tearDown(self):
        with pitv_tvui._CAROUSEL_LOCK:
            pitv_tvui._CAROUSEL.update(self.saved_carousel)
        pitv_tvui._CAROUSEL_WAKE.clear()

    def test_empty_carousel_renders_a_valid_strip(self):
        strip = pitv_tvui._draw_carousel_strip([], 0)
        self.assertEqual(strip.size, (pitv_tvui.WIDTH, 295))
        self.assertEqual(strip.mode, "RGB")

    def test_cards_render_without_game_art_assets_or_framebuffer(self):
        def fake_artwork(key, width, height, source_width, source_height):
            return Image.new("RGB", (width, height), (30, 60, 90))

        with patch.object(pitv_tvui, "_fitted_artwork", side_effect=fake_artwork):
            strip = pitv_tvui._draw_carousel_strip(
                pitv_tvui._CAROUSEL["items"], 1.25
            )
        self.assertEqual(strip.size, (pitv_tvui.WIDTH, 295))
        self.assertEqual(strip.mode, "RGB")

    def test_home_warmup_primes_transition_frames_without_framebuffer_writes(self):
        items = [
            ("TETRIS", "TETRIS", "Puzzle game"),
            ("SPACE INVADERS", "INVADERS", "Arcade game"),
            ("IMPOSTER", "IMPOSTER", "Party game"),
            ("2048", "2048", "Number puzzle"),
        ]
        canvas = Image.new("RGB", (pitv_tvui.WIDTH, pitv_tvui.HEIGHT))
        with (
            patch.object(pitv_tvui, "_background", return_value=canvas),
            patch.object(pitv_tvui, "_nav"),
            patch.object(pitv_tvui, "_draw_home"),
            patch.object(
                pitv_tvui, "_draw_carousel_strip",
                return_value=Image.new("RGB", (pitv_tvui.WIDTH, 295)),
            ) as draw_strip,
            patch.object(pitv_tvui.pitv_framebuffer, "display_image_region") as write_region,
            patch.object(pitv_tvui.pitv_framebuffer, "display_prepared_frame") as write_full,
        ):
            stats = pitv_tvui.warm_render_cache(
                home_items=[("GAMES", "GAMES", "Open games")],
                carousel_games=items,
                recent_games=[],
            )

        self.assertEqual(draw_strip.call_count, 6)
        self.assertEqual(stats["carousel_games"], 4)
        write_region.assert_not_called()
        write_full.assert_not_called()

    def test_index_tracks_the_most_focused_card(self):
        pitv_tvui._CAROUSEL["position"] = 1.4
        self.assertEqual(pitv_tvui.carousel_index(), 1)
        pitv_tvui._CAROUSEL["position"] = 2.6
        self.assertEqual(pitv_tvui.carousel_index(), 3)

    def test_manual_navigation_requests_a_single_slide(self):
        self.assertTrue(pitv_tvui.request_carousel_step(1))
        self.assertEqual(pitv_tvui._CAROUSEL["target"], 1)
        self.assertGreater(pitv_tvui._CAROUSEL["pause_until"], 0.0)

    def test_regional_failure_falls_back_to_instant_manual_navigation(self):
        with pitv_tvui._CAROUSEL_LOCK:
            pitv_tvui._CAROUSEL["error"] = True
            pitv_tvui._CAROUSEL["position"] = 0.0
            pitv_tvui._CAROUSEL["target"] = None
            original_revision = pitv_tvui._CAROUSEL["revision"]

        self.assertTrue(pitv_tvui.request_carousel_step(1))
        self.assertEqual(pitv_tvui.carousel_index(), 1)
        self.assertIsNone(pitv_tvui._CAROUSEL["target"])
        self.assertEqual(pitv_tvui.carousel_revision(), original_revision + 1)

    def test_manual_navigation_is_ignored_outside_home(self):
        pitv_tvui._CAROUSEL["view"] = "SETTINGS"
        self.assertFalse(pitv_tvui.request_carousel_step(1))
        self.assertIsNone(pitv_tvui._CAROUSEL["target"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
