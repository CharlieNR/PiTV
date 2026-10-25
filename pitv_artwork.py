#!/usr/bin/env python3
"""Offline PiTV icon artwork with an on-disk cache that survives reboots.

Menu artwork is made from the bundled Lucide SVG assets. CairoSVG rasterises an
icon only when its cache is missing or older than the source SVG. Finished
artwork cards are cached separately, so a later boot can load PNGs directly
without redrawing the gradient or rerasterising the icon.
"""
from __future__ import annotations

import os
import tempfile
from functools import lru_cache

from PIL import Image, ImageDraw

_ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "icons")
_CACHE_ROOT = os.path.join(os.path.expanduser("~"), ".cache", "pitv")
_ICON_CACHE_DIR = os.path.join(_CACHE_ROOT, "icons")
_ART_CACHE_DIR = os.path.join(_CACHE_ROOT, "artwork")
_ICON_RASTER_VERSION = "v1"
_ART_CACHE_VERSION = "v1"

# Only actual menu/game destinations are listed here. Matrix rain deliberately
# stays as its existing animated-looking rain artwork rather than a static icon.
ICON_MAP = {
    "FIRE": "flame",
    "CLOCK": "clock",
    "DURATION_0": "clock",
    "DURATION_1": "clock",
    "DURATION_2": "clock",
    "DURATION_3": "clock",
    "MIRROR": "airplay",
    "AIRPLAY": "airplay",
    "GAMES": "gamepad-2",
    "IMPOSTER": "user-round-x",
    "2048": "grid-2x2",
    "BOULDERDASH": "gem",
    "BREAKOUT": "brick-wall",
    "CURSEOFWAR": "swords",
    "DOPEWARS": "chart-no-axes-combined",
    "CRAWL": "skull",
    "MINESWEEPER": "bomb",
    "MOONBUGGY": "car",
    "NETHACK": "scroll-text",
    "TICTACTOE": "grid-3x3",
    "TTT_COMPUTER": "grid-3x3",
    "TTT_LOCAL": "grid-3x3",
    "SNAKE": "worm",
    "INVADERS": "bug",
    "SHOOTER": "rocket",
    "SUPERTUX": "bird",
    "TETRIS": "blocks",
    "TYRIAN": "rocket",
}

_PALETTES = {
    "FIRE": ((31, 15, 39), (239, 74, 25), (255, 191, 64)),
    "CLOCK": ((6, 17, 54), (31, 77, 149), (171, 222, 255)),
    "MIRROR": ((13, 14, 48), (50, 52, 180), (45, 220, 255)),
    "GAMES": ((13, 12, 52), (96, 37, 156), (255, 76, 173)),
    "IMPOSTER": ((14, 18, 43), (46, 55, 111), (248, 82, 141)),
    "2048": ((34, 25, 61), (171, 85, 89), (255, 200, 90)),
    "BOULDERDASH": ((22, 22, 42), (104, 69, 52), (83, 255, 194)),
    "BREAKOUT": ((14, 21, 58), (35, 89, 177), (255, 102, 126)),
    "CURSEOFWAR": ((14, 30, 43), (26, 100, 111), (243, 214, 102)),
    "DOPEWARS": ((22, 18, 42), (85, 48, 99), (255, 178, 98)),
    "CRAWL": ((12, 14, 30), (76, 47, 75), (255, 150, 79)),
    "MINESWEEPER": ((15, 36, 53), (42, 111, 148), (145, 226, 255)),
    "MOONBUGGY": ((16, 28, 56), (95, 110, 145), (255, 217, 143)),
    "NETHACK": ((14, 17, 35), (73, 44, 88), (219, 121, 255)),
    "TICTACTOE": ((13, 26, 58), (41, 114, 177), (84, 231, 224)),
    "SNAKE": ((4, 31, 33), (12, 112, 95), (117, 255, 136)),
    "INVADERS": ((8, 15, 46), (54, 42, 153), (100, 239, 255)),
    "SHOOTER": ((7, 16, 48), (38, 79, 165), (255, 103, 204)),
    "SUPERTUX": ((9, 40, 72), (44, 131, 190), (210, 247, 255)),
    "TETRIS": ((14, 24, 59), (63, 68, 181), (255, 179, 83)),
    "TYRIAN": ((10, 10, 38), (85, 37, 153), (100, 228, 255)),
}
_DEFAULT_PALETTE = ((13, 12, 52), (50, 75, 130), (100, 190, 255))


def _ensure_dir(path: str) -> None:
    os.makedirs(path, mode=0o700, exist_ok=True)


def _newer_than_source(cache_path: str, source_path: str) -> bool:
    try:
        return os.path.getmtime(cache_path) >= os.path.getmtime(source_path)
    except OSError:
        return False


@lru_cache(maxsize=64)
def _icon_base(icon_name: str) -> Image.Image:
    """Read or create a 256px transparent PNG of a bundled Lucide SVG icon."""
    source_path = os.path.join(_ICON_DIR, f"{icon_name}.svg")
    if not os.path.isfile(source_path):
        raise FileNotFoundError(f"PiTV icon asset is missing: {source_path}")

    _ensure_dir(_ICON_CACHE_DIR)
    cache_path = os.path.join(_ICON_CACHE_DIR, f"{icon_name}-{_ICON_RASTER_VERSION}-256.png")
    if os.path.isfile(cache_path) and _newer_than_source(cache_path, source_path):
        with Image.open(cache_path) as opened:
            return opened.convert("RGBA")

    # SVG assets use Lucide's currentColor stroke. Replace it with a predictable
    # near-white colour so the icons are crisp and consistent on every Pi.
    with open(source_path, "rb") as source:
        svg = source.read().replace(b"currentColor", b"#eff7ff")

    try:
        import cairosvg
    except ImportError as exc:
        raise RuntimeError(
            "The PiTV V2 icon renderer requires python3-cairosvg. "
            "Run the PiTV deploy/update command to install dependencies."
        ) from exc

    fd, temp_path = tempfile.mkstemp(prefix=".icon-", suffix=".png", dir=_ICON_CACHE_DIR)
    os.close(fd)
    try:
        cairosvg.svg2png(bytestring=svg, write_to=temp_path, output_width=256, output_height=256)
        os.replace(temp_path, cache_path)
    finally:
        try:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
        except OSError:
            pass
    with Image.open(cache_path) as opened:
        return opened.convert("RGBA")



# Icons directly used by V2 screens in addition to destination-card artwork.
_STANDALONE_UI_ICONS = {
    "airplay",
    "bug",
    "chart-no-axes-combined",
    "clock",
    "gamepad-2",
    "house",
    "lock-keyhole",
    "monitor",
    "scroll-text",
    "user-round",
}


def warm_icon_cache() -> int:
    """Pre-rasterise bundled icons during deployment, not during TV navigation.

    CairoSVG's first rasterisation of an icon can take long enough to cause a
    visible pause on a Pi Zero. This fills the persistent PNG cache for all
    mapped destination icons and standalone V2 screen icons before the UI
    service starts. Failures are reported but never make deployment fail.
    """
    names = sorted(set(ICON_MAP.values()) | _STANDALONE_UI_ICONS)
    warmed = 0
    for name in names:
        try:
            _icon_base(name)
            warmed += 1
        except Exception as exc:
            print(f"WARNING: could not prewarm PiTV icon {name!r}: {exc}")
    return warmed


@lru_cache(maxsize=128)
def load_icon(icon_name: str, size: int = 64) -> Image.Image:
    """Return a correctly proportioned transparent icon at the requested size."""
    size = max(1, int(size))
    icon = _icon_base(str(icon_name))
    if icon.size == (size, size):
        return icon
    return icon.resize((size, size), resample=getattr(Image, "Resampling", Image).LANCZOS)


def _make_card(key: str, width: int, height: int) -> Image.Image:
    top, bottom, accent = _PALETTES.get(key, _DEFAULT_PALETTE)
    image = Image.new("RGB", (width, height), top)
    draw = ImageDraw.Draw(image)
    bands = 24
    for index in range(bands):
        y0 = index * height // bands
        y1 = (index + 1) * height // bands
        amount = index / max(1, bands - 1)
        colour = tuple(int(top[c] * (1 - amount) + bottom[c] * amount) for c in range(3))
        draw.rectangle((0, y0, width, y1), fill=colour)

    # This is background lighting, not a fake symbol; the centred Lucide SVG
    # remains unchanged and keeps its original square aspect ratio.
    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    icon_cx = int(width * (0.77 if width >= 800 else 0.50))
    icon_cy = int(height * 0.49)
    glow_radius = int(min(height * 0.46, width * 0.22))
    gd.ellipse(
        (icon_cx - glow_radius, icon_cy - glow_radius,
         icon_cx + glow_radius, icon_cy + glow_radius),
        fill=(accent[0], accent[1], accent[2], 48),
    )
    image = Image.alpha_composite(image.convert("RGBA"), glow)

    icon_size = max(24, int(min(height * (0.42 if width >= 800 else 0.72), width * 0.38)))
    icon = load_icon(ICON_MAP[key], 256)
    icon = icon.resize((icon_size, icon_size),
                       resample=getattr(Image, "Resampling", Image).LANCZOS)
    image.alpha_composite(icon, (icon_cx - icon_size // 2, icon_cy - icon_size // 2))
    return image.convert("RGB")


def load_cached_generated_artwork(kind: str, width: int, height: int):
    """Load persisted legacy artwork for the Matrix Rain preview, if present."""
    if str(kind).upper() != "MATRIX":
        return None
    _ensure_dir(_ART_CACHE_DIR)
    path = os.path.join(
        _ART_CACHE_DIR,
        f"matrix_{int(width)}x{int(height)}_{_ART_CACHE_VERSION}.png",
    )
    if not os.path.isfile(path):
        return None
    with Image.open(path) as opened:
        return opened.convert("RGB")


def cache_generated_artwork(kind: str, width: int, height: int, image: Image.Image) -> Image.Image:
    """Persist generated Matrix Rain preview art atomically, surviving reboots."""
    if str(kind).upper() != "MATRIX":
        return image
    _ensure_dir(_ART_CACHE_DIR)
    path = os.path.join(
        _ART_CACHE_DIR,
        f"matrix_{int(width)}x{int(height)}_{_ART_CACHE_VERSION}.png",
    )
    fd, temp_path = tempfile.mkstemp(prefix=".matrix-", suffix=".png", dir=_ART_CACHE_DIR)
    os.close(fd)
    try:
        image.save(temp_path, format="PNG", optimize=False)
        os.replace(temp_path, path)
    finally:
        try:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
        except OSError:
            pass
    return image


@lru_cache(maxsize=128)
def artwork(kind: str, width: int = 600, height: int = 280) -> Image.Image:
    """Return icon-led card art, persisted on disk and cached in memory."""
    key = str(kind).upper()
    width, height = max(1, int(width)), max(1, int(height))
    if key not in ICON_MAP:
        raise KeyError(f"No Lucide icon mapping exists for PiTV artwork key {key!r}.")

    _ensure_dir(_ART_CACHE_DIR)
    icon_path = os.path.join(_ICON_DIR, f"{ICON_MAP[key]}.svg")
    cache_path = os.path.join(
        _ART_CACHE_DIR,
        f"{key.lower()}_{width}x{height}_{_ART_CACHE_VERSION}.png",
    )
    if os.path.isfile(cache_path) and _newer_than_source(cache_path, icon_path):
        with Image.open(cache_path) as opened:
            return opened.convert("RGB")

    image = _make_card(key, width, height)
    fd, temp_path = tempfile.mkstemp(prefix=".art-", suffix=".png", dir=_ART_CACHE_DIR)
    os.close(fd)
    try:
        image.save(temp_path, format="PNG", optimize=False)
        os.replace(temp_path, cache_path)
    finally:
        try:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
        except OSError:
            pass
    return image
