#!/usr/bin/env python3
"""Image-led, offline TV interface for PiTV version 2.

The menu is drawn as a real PNG and displayed directly through the Linux
framebuffer. It does not require X11, a browser, or an internet connection.
"""
from __future__ import annotations

import json
import math
import os
import random
import threading
import time
import urllib.request
from datetime import datetime, timezone
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont, ImageOps
import pitv_framebuffer
import pitv_artwork

# Pillow 8/9 compatibility: Image.Resampling was added in Pillow 9.1.
_RESAMPLING = getattr(Image, "Resampling", Image)

WIDTH, HEIGHT = 1280, 720
PNG_PATH = os.path.join(os.path.expanduser("~"), ".pitv", "tvui.png")
BG = (5, 10, 27)
PANEL = (13, 23, 46)
PANEL_2 = (19, 32, 61)
WHITE = (245, 248, 255)
MUTED = (155, 174, 204)
BLUE = (57, 159, 255)
CYAN = (54, 226, 239)
GOLD = (255, 185, 72)

_FONT_NORMAL = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_LAST_SIGNATURE = None
_LAST_PREPARED = None
_LAST_CLOCK_TEXT = None

_NAV_CLOCK_VIEWS = {
    "MENU", "DURATION_SELECT", "GAMES_MENU", "TTT_MODE", "IMPOSTER_COUNT",
    "KEYPAD", "SETTINGS", "SETTINGS_AUTH", "SETTINGS_INPUT", "SETTINGS_STATUS",
    "SETTINGS_LOG", "SETTINGS_CONFIRM",
}

# Time is corrected for the V2 display only; the Pi's system clock is not set.
# The API runs in a daemon thread so an unavailable network cannot stall drawing.
_CLOCK_API_URL = "https://gateway.timeapi.world/timezone/Europe/London"
_CLOCK_SYNC_INTERVAL = 600.0
_CLOCK_SYNC_AFTER_BOOT = 60.0
_CLOCK_SYNC_LOCK = threading.Lock()
_CLOCK_SYNC_ANCHOR = None  # (UTC epoch seconds, monotonic seconds)
_CLOCK_SYNC_STARTED = False

try:
    from zoneinfo import ZoneInfo
    _LONDON_TIMEZONE = ZoneInfo("Europe/London")
except Exception:
    _LONDON_TIMEZONE = None


def _system_uptime_seconds():
    try:
        with open("/proc/uptime", "r", encoding="ascii") as uptime_file:
            return float(uptime_file.read().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def _sync_internal_clock_once():
    started = time.monotonic()
    request = urllib.request.Request(
        _CLOCK_API_URL,
        headers={"Accept": "application/json", "User-Agent": "PiTV-V2/1.0"},
    )
    with urllib.request.urlopen(request, timeout=4.0) as response:
        data = json.loads(response.read(65536).decode("utf-8"))
    received = time.monotonic()

    server_epoch = None
    for field in ("utc_datetime", "datetime"):
        value = data.get(field)
        if not value:
            continue
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        server_epoch = parsed.timestamp()
        break
    if server_epoch is None and data.get("unixtime") is not None:
        server_epoch = float(data["unixtime"])
    if server_epoch is None:
        raise ValueError("time API response did not contain a recognised timestamp")

    # Anchor the API timestamp to monotonic time, not the Pi's possibly-wrong
    # wall clock. Using the request midpoint limits network-latency error.
    anchor = ((started + received) / 2.0)
    global _CLOCK_SYNC_ANCHOR
    with _CLOCK_SYNC_LOCK:
        _CLOCK_SYNC_ANCHOR = (server_epoch, anchor)


def _clock_sync_worker():
    # Use Linux uptime so the first request is scheduled for boot + 60 seconds,
    # even if this process is launched a little after the Pi starts.
    deadline = time.monotonic() + max(
        0.0, _CLOCK_SYNC_AFTER_BOOT - _system_uptime_seconds()
    )
    while True:
        delay = deadline - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        try:
            _sync_internal_clock_once()
        except Exception:
            # Keep the last successful correction, or use system time until
            # the first success. Network/API failures must not stop the UI.
            pass
        deadline += _CLOCK_SYNC_INTERVAL
        now = time.monotonic()
        if deadline <= now:
            deadline = now + _CLOCK_SYNC_INTERVAL


def _start_clock_sync():
    global _CLOCK_SYNC_STARTED
    with _CLOCK_SYNC_LOCK:
        if _CLOCK_SYNC_STARTED:
            return
        _CLOCK_SYNC_STARTED = True
    threading.Thread(
        target=_clock_sync_worker,
        name="pitv-v2-clock-sync",
        daemon=True,
    ).start()


def _display_clock_text():
    _start_clock_sync()
    with _CLOCK_SYNC_LOCK:
        anchor = _CLOCK_SYNC_ANCHOR
    if anchor is None:
        return time.strftime("%H:%M")

    epoch_at_anchor, monotonic_at_anchor = anchor
    current_epoch = epoch_at_anchor + (time.monotonic() - monotonic_at_anchor)
    try:
        if _LONDON_TIMEZONE is not None:
            return datetime.fromtimestamp(
                current_epoch, _LONDON_TIMEZONE
            ).strftime("%H:%M")
    except (OverflowError, OSError, ValueError):
        pass
    return time.strftime("%H:%M", time.localtime(current_epoch))


@lru_cache(maxsize=64)
def _font(size: int, bold: bool = False):
    path = _FONT_BOLD if bold else _FONT_NORMAL
    try:
        return ImageFont.truetype(path, size=size)
    except OSError:
        return ImageFont.load_default()


def _mix(a, b, amount):
    return tuple(int(a[i] * (1.0 - amount) + b[i] * amount) for i in range(3))


@lru_cache(maxsize=1)
def _background():
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)
    # Broad, low-detail bands create a subtle cinematic navy gradient without
    # filling the framebuffer image with millions of unique gradient pixels.
    bands = 48
    for i in range(bands):
        y0 = i * HEIGHT // bands
        y1 = (i + 1) * HEIGHT // bands
        colour = _mix((8, 24, 54), (4, 8, 23), i / max(1, bands - 1))
        draw.rectangle((0, y0, WIDTH, y1), fill=colour)
    # Off-centre blue glows add depth like a streaming-TV home screen.
    glow = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((730, -230, 1470, 510), fill=(16, 91, 190, 34))
    gd.ellipse((-340, 390, 430, 1160), fill=(9, 72, 140, 25))
    return Image.alpha_composite(image.convert("RGBA"), glow).convert("RGB")


@lru_cache(maxsize=1)
def _native_framebuffer_size():
    """Read the stable display geometry once instead of querying fbdev per redraw."""
    try:
        with open("/dev/fb0", "r+b", buffering=0) as fb:
            info = pitv_framebuffer._framebuffer_info(fb.fileno())
        return (int(info["width"]), int(info["height"]))
    except (OSError, AttributeError, RuntimeError, ValueError, KeyError):
        return None


def _native_resolution(canvas):
    """Resize in native code with the cheapest filter appropriate for the scale."""
    target = _native_framebuffer_size()
    if target is None or target == canvas.size:
        return canvas

    # Upscaling is common on HDMI TVs. Nearest-neighbour is effectively free
    # compared with Lanczos and keeps the text crisp instead of softening it.
    ratio_src = canvas.width / canvas.height
    ratio_dst = target[0] / target[1]
    upscaling = target[0] >= canvas.width and target[1] >= canvas.height

    # Let prepare_image fuse the common 16:9 upscale + RGB565 conversion into
    # one native pass. This removes a temporary 1920x1080 Pillow image and a
    # separate full-screen pixel-pack pass on the Pi Zero 2 W.
    if (
        upscaling
        and abs(ratio_src - ratio_dst) < 0.002
        and getattr(pitv_framebuffer, "NATIVE_RGB565_SCALE_PACK_AVAILABLE", False)
    ):
        return canvas

    method = _RESAMPLING.NEAREST if upscaling else _RESAMPLING.BILINEAR
    if abs(ratio_src - ratio_dst) < 0.002:
        return canvas.resize(target, resample=method)

    fitted = ImageOps.contain(canvas, target, method=method)
    output = Image.new("RGB", target, (0, 0, 0))
    output.paste(fitted, ((target[0]-fitted.width)//2, (target[1]-fitted.height)//2))
    return output


def _starfield(draw, w, h, seed, count=50):
    rng = random.Random(seed)
    for _ in range(count):
        x = rng.randint(0, max(0, w - 1))
        y = rng.randint(0, max(0, h - 1))
        r = rng.choice((1, 1, 1, 2, 3))
        c = rng.choice(((158, 215, 255), (255, 255, 255), (82, 165, 255)))
        draw.ellipse((x-r, y-r, x+r, y+r), fill=c)
    for _ in range(max(2, count // 15)):
        x = rng.randint(15, max(16, w - 15))
        y = rng.randint(12, max(13, h - 12))
        draw.line((x-5, y, x+5, y), fill=(118, 190, 255), width=1)
        draw.line((x, y-5, x, y+5), fill=(118, 190, 255), width=1)


def _base_art(kind, w, h):
    key = str(kind).upper()
    palettes = {
        "FIRE": ((31, 15, 39), (239, 74, 25), (255, 191, 64)),
        "CLOCK": ((6, 17, 54), (31, 77, 149), (171, 222, 255)),
        "MATRIX": ((2, 19, 27), (0, 93, 56), (61, 255, 141)),
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
    top, bottom, accent = palettes.get(key, palettes["GAMES"])
    im = Image.new("RGB", (w, h), top)
    d = ImageDraw.Draw(im)
    bands = 32
    for i in range(bands):
        y0 = i * h // bands
        y1 = (i + 1) * h // bands
        colour = _mix(top, bottom, i / max(1, bands - 1))
        d.rectangle((0, y0, w, y1), fill=colour)
    seed = sum((i + 1) * ord(c) for i, c in enumerate(key)) + w * 3 + h
    _starfield(d, w, h, seed, max(20, (w * h) // 4000))
    return im, d, accent


@lru_cache(maxsize=72)
def _artwork(kind, w=600, h=280):
    """Make a local, offline cover illustration for each menu destination."""
    key = str(kind).upper()
    if key != "MATRIX" and key in pitv_artwork.ICON_MAP:
        return pitv_artwork.artwork(key, int(w), int(h))
    if key == "MATRIX":
        cached_matrix = pitv_artwork.load_cached_generated_artwork(key, int(w), int(h))
        if cached_matrix is not None:
            return cached_matrix
    im, d, accent = _base_art(key, int(w), int(h))
    w, h = im.size

    def pt(x, y):
        return (int(x * w), int(y * h))

    def poly(points, fill, outline=None):
        scaled = [pt(x, y) for x, y in points]
        d.polygon(scaled, fill=fill, outline=outline)

    def ellipse(box, fill, outline=None, width=1):
        x0, y0, x1, y1 = box
        d.ellipse((int(x0*w), int(y0*h), int(x1*w), int(y1*h)),
                  fill=fill, outline=outline, width=width)

    def rect(box, fill, outline=None, width=1, radius=0):
        x0, y0, x1, y1 = box
        xy = (int(x0*w), int(y0*h), int(x1*w), int(y1*h))
        if radius:
            d.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=width)
        else:
            d.rectangle(xy, fill=fill, outline=outline, width=width)

    # Build a different visual language for each feature / game. These are
    # original illustrations, not screenshots or copied streaming artwork.
    if key == "FIRE":
        for layer, colour in [
            (0.18, (255, 69, 24)), (0.27, (255, 112, 27)),
            (0.35, (255, 179, 48)), (0.22, (255, 226, 104)),
        ]:
            x0 = layer * w
            poly([(x0, 1.0), (x0-0.06, 0.68), (x0+0.06, 0.48),
                  (x0+0.02, 0.24), (x0+0.16, 0.42), (x0+0.20, 0.68),
                  (x0+0.25, 0.45), (x0+0.33, 0.71), (x0+0.38, 1.0)], colour)
        ellipse((0.43, 0.5, 0.75, 1.25), (255, 205, 65))
        ellipse((0.51, 0.66, 0.68, 1.1), (255, 241, 176))
    elif key == "CLOCK":
        ellipse((0.66, 0.08, 0.98, 0.58), (119, 184, 255))
        ellipse((0.73, 0.02, 1.02, 0.44), (24, 61, 129))
        ellipse((0.10, 0.12, 0.50, 0.82), (58, 116, 197), (169, 222, 255), 3)
        ellipse((0.15, 0.17, 0.45, 0.77), (10, 23, 63))
        d.line((int(.30*w), int(.47*h), int(.30*w), int(.27*h)), fill=(245, 248, 255), width=max(2,w//100))
        d.line((int(.30*w), int(.47*h), int(.41*w), int(.55*h)), fill=(245, 248, 255), width=max(2,w//100))
        ellipse((0.27, 0.44, 0.33, 0.50), (245, 248, 255))
    elif key == "MATRIX":
        rng = random.Random(381 + w)
        for col in range(18):
            x = int((col + .5) * w / 18)
            y = rng.randint(-h//3, h)
            for row in range(6):
                yy = y + row * (h//8)
                if 0 <= yy < h:
                    cw = max(3,w//100)
                    hh = max(4,h//12)
                    brightness = max(50, 240-row*30)
                    d.rounded_rectangle((x, yy, x+cw, yy+hh),
                        radius=2, fill=(20, brightness, min(150,brightness)))
        poly([(0, .94), (1, .74), (1, 1), (0, 1)], (0, 49, 36))
    elif key in ("MIRROR", "AIRPLAY"):
        # Phone and radiating wave arcs.
        rect((0.38, 0.16, 0.61, 0.86), (12, 21, 55), (104, 226, 255), 4, 18)
        rect((0.405, 0.23, 0.585, 0.76), (49, 113, 177), radius=10)
        ellipse((0.46, 0.78, 0.53, 0.82), (225, 248, 255))
        for box, width in [((0.14, .33, .87, .91), 5), ((0.04,.20,.97,1.02),3)]:
            d.arc((int(box[0]*w),int(box[1]*h),int(box[2]*w),int(box[3]*h)),
                  205, 335, fill=accent, width=max(2,w//120))
        poly([(0.47,.58),(.53,.58),(.50,.48)], (244,253,255))
    elif key in ("GAMES", "SHOOTER", "TYRIAN", "INVADERS"):
        if key == "GAMES":
            # Big glowing arcade controller.
            rect((0.20,.35,.80,.78), (24,35,99), accent, 5, 32)
            rect((.27,.43,.40,.65), (10,17,48), (146,220,255), 2, 10)
            d.line((*pt(.335,.47), *pt(.335,.61)), fill=(220,245,255), width=max(4,w//60))
            d.line((*pt(.29,.54), *pt(.38,.54)), fill=(220,245,255), width=max(4,w//60))
            for x,y,c in [(.64,.48,(255,93,146)),(.72,.57,(85,239,255)),(.63,.64,(255,199,81))]:
                ellipse((x-.03,y-.05,x+.03,y+.01),c)
            ellipse((.44,.02,.58,.21),(109,79,255))
        elif key == "INVADERS":
            for row in range(3):
                for col in range(7):
                    x=.15+col*.115; y=.22+row*.17
                    c=((81,247,199),(107,182,255),(252,106,174))[row]
                    rect((x,y,x+.055,y+.06),c,radius=3)
                    d.line((*pt(x-.005,y+.04),*pt(x+.015,y+.08)),fill=c,width=3)
                    d.line((*pt(x+.06,y+.04),*pt(x+.08,y+.08)),fill=c,width=3)
            poly([(.43,.84),(.50,.68),(.57,.84),(.62,.91),(.38,.91)],(238,251,255))
        else:
            poly([(.50,.60),(.57,.80),(.52,.77),(.50,.95),(.48,.77),(.43,.80)],(229,242,255),accent)
            ellipse((.43,.60,.57,.77), (75,135,255), (192,238,255), 2)
            for _ in range(12):
                rng=random.Random(712+_)
                x=rng.random(); y=rng.random()*.9
                ellipse((x,y,x+.008,y+.02),(190,226,255))
            ellipse((.06,.14,.28,.46),(66,81,166),(120,193,255),2)
    elif key == "IMPOSTER":
        ellipse((.35,.08,.67,.69),(213,69,116))
        ellipse((.42,.17,.58,.37),(245,221,231))
        poly([(.25,1),(.28,.68),(.39,.52),(.61,.52),(.72,.68),(.76,1)],(20,27,63))
        ellipse((.68,.10,.89,.34),(82,130,240))
        poly([(.62,.98),(.67,.56),(.78,.44),(.89,.62),(.95,1)],(19,24,57))
    elif key == "2048":
        colours=[(239,145,89),(245,187,97),(249,211,133),(239,117,126),(132,137,246),(105,186,233)]
        rng=random.Random(2048)
        for row in range(3):
            for col in range(4):
                x=.27+col*.12; y=.20+row*.18
                c=colours[(row*4+col)%len(colours)]
                rect((x,y,x+.105,y+.155),c,radius=7)
                ellipse((x+.027,y+.04,x+.074,y+.09),_mix(c,(255,255,255),.45))
        rect((.20,.14,.78,.78),None, (255,229,179), 2, 12)
    elif key == "BOULDERDASH":
        poly([(0,.25),(.14,.05),(.31,.30),(.43,.10),(.56,.31),(.72,.08),(.88,.29),(1,.12),(1,1),(0,1)],(65,57,75))
        for x,y in [(.23,.46),(.44,.70),(.69,.44),(.82,.78),(.35,.32)]:
            poly([(x,y-.06),(x+.05,y),(x,y+.07),(x-.05,y)],(70,240,192),(199,255,226))
        ellipse((.06,.70,.17,.85),(112,96,78))
    elif key == "BREAKOUT":
        colours=[(255,101,135),(255,179,88),(113,214,255),(143,118,255)]
        for r in range(3):
            for c in range(7):
                rect((.12+c*.11,.12+r*.13,.21+c*.11,.21+r*.13),colours[r%len(colours)],radius=3)
        ellipse((.63,.60,.68,.65),(255,255,255))
        d.line((*pt(.42,.88),*pt(.77,.88)),fill=(246,252,255),width=max(7,w//40))
        poly([(.65,.65),(.70,.65),(.68,.70)],(255,255,255))
    elif key in ("CURSEOFWAR","DOPEWARS"):
        # Strategy map / neon city and rising market-like skyline.
        for x in range(0,w,w//8):
            d.line((x,0,x,h),fill=(80,142,157),width=1)
        for y in range(0,h,h//5):
            d.line((0,y,w,y),fill=(80,142,157),width=1)
        rect((.12,.20,.33,.43),(23,139,129),(114,244,213),2)
        rect((.37,.38,.61,.62),(46,98,191),(144,203,255),2)
        rect((.64,.14,.86,.39),(189,104,86),(255,202,132),2)
        for i in range(6):
            x=.08+i*.14
            topy=.84-random.Random(77+i).random()*.36
            rect((x,topy,x+.09,.94),(28+5*i,49+8*i,87+7*i))
        d.line((*pt(.12,.80),*pt(.38,.67),*pt(.58,.73),*pt(.83,.40)),fill=accent,width=max(4,w//90))
    elif key == "CRAWL":
        poly([(0,.18),(.22,.32),(.12,.51),(.32,.69),(.27,1),(0,1)],(38,35,59))
        poly([(1,.16),(.80,.31),(.90,.56),(.70,.77),(.78,1),(1,1)],(38,35,59))
        rect((.32,.20,.68,.91),(13,20,42),(161,106,223),4)
        rect((.39,.28,.61,.84),(42,37,79),(255,175,93),3)
        ellipse((.43,.38,.57,.63),(255,145,65))
        ellipse((.46,.41,.54,.57),(255,222,127))
    elif key == "MINESWEEPER":
        for r in range(4):
            for c in range(6):
                x=.20+c*.105; y=.15+r*.17
                col=(34,91,128) if (r+c)%2 else (41,111,147)
                rect((x,y,x+.095,y+.15),col,(135,201,235),1,3)
        ellipse((.41,.30,.51,.41),(14,25,46),(220,240,255),2)
        for angle in range(0,360,45):
            a=math.radians(angle)
            x0=.46+math.cos(a)*.08; y0=.355+math.sin(a)*.08
            x1=.46+math.cos(a)*.13; y1=.355+math.sin(a)*.13
            d.line((*pt(x0,y0),*pt(x1,y1)),fill=(255,211,118),width=3)
    elif key == "MOONBUGGY":
        poly([(0,.68),(.13,.61),(.25,.67),(.34,.59),(.51,.70),(.66,.62),(.78,.69),(1,.56),(1,1),(0,1)],(81,96,133))
        ellipse((.65,.07,.82,.29),(213,226,247))
        rect((.29,.56,.65,.68),(39,53,80),(205,224,255),3,5)
        poly([(.38,.56),(.45,.43),(.56,.43),(.62,.56)],(71,172,224),(208,241,255))
        ellipse((.33,.65,.46,.79),(10,18,39),(190,222,255),3)
        ellipse((.55,.65,.68,.79),(10,18,39),(190,222,255),3)
    elif key == "NETHACK":
        rect((.18,.15,.82,.92),(29,24,51),(165,111,212),3)
        for i in range(4):
            rect((.25+i*.13,.22,.31+i*.13,.28),(116,76,147))
            rect((.25+i*.13,.80,.31+i*.13,.86),(116,76,147))
        ellipse((.37,.33,.63,.70),(92,43,142),(226,171,255),3)
        ellipse((.43,.40,.57,.63),(255,177,94))
        ellipse((.46,.44,.54,.58),(255,239,171))
    elif key in ("TICTACTOE", "TTT_COMPUTER", "TTT_LOCAL"):
        for i in (1,2):
            d.line((*pt(.24,.15+i*.21),*pt(.76,.15+i*.21)),fill=(145,231,255),width=5)
            d.line((*pt(.24+i*.17,.15),*pt(.24+i*.17,.78)),fill=(145,231,255),width=5)
        ellipse((.28,.20,.39,.35),None,(255,197,99),5)
        d.line((*pt(.52,.39),*pt(.64,.51)),fill=(255,112,167),width=7)
        d.line((*pt(.64,.39),*pt(.52,.51)),fill=(255,112,167),width=7)
        ellipse((.61,.58,.72,.71),None,(255,197,99),5)
    elif key == "SNAKE":
        points=[pt(.13,.70),pt(.13,.48),pt(.29,.48),pt(.29,.68),pt(.46,.68),pt(.46,.34),pt(.67,.34),pt(.67,.51),pt(.83,.51)]
        d.line(points,fill=(115,255,142),width=max(12,w//35),joint="curve")
        for x,y in [(.13,.70),(.29,.48),(.46,.68),(.67,.34),(.83,.51)]:
            ellipse((x-.025,y-.045,x+.025,y+.045),(156,255,177))
        ellipse((.79,.30,.88,.45),(255,118,131))
    elif key == "SUPERTUX":
        poly([(0,.62),(.16,.34),(.32,.64),(.54,.28),(.75,.61),(.88,.40),(1,.62),(1,1),(0,1)],(108,195,235))
        poly([(0,.76),(.2,.62),(.38,.77),(.59,.59),(.78,.78),(1,.67),(1,1),(0,1)],(221,247,255))
        ellipse((.40,.30,.61,.69),(9,25,48),(213,243,255),3)
        ellipse((.42,.20,.59,.48),(10,31,55),(217,242,255),3)
        ellipse((.445,.30,.48,.36),(255,255,255))
        ellipse((.52,.30,.555,.36),(255,255,255))
        poly([(.47,.39),(.54,.39),(.50,.44)],(255,177,70))
    elif key == "TETRIS":
        blocks=[(.20,.15,(255,111,123)),(.32,.15,(255,111,123)),(.20,.29,(255,111,123)),
                (.48,.31,(101,224,255)),(.60,.31,(101,224,255)),(.60,.45,(101,224,255)),
                (.36,.56,(255,196,95)),(.48,.56,(255,196,95)),(.60,.56,(255,196,95)),
                (.48,.70,(173,134,255)),(.60,.70,(173,134,255)),(.72,.70,(173,134,255))]
        for x,y,c in blocks:
            rect((x,y,x+.105,y+.13),c,(255,255,255),1,2)
    else:
        # Generic ambient/utility art: layered horizon, glowing disc and arcs.
        ellipse((.60,.08,.90,.52),_mix(accent,(255,255,255),.1))
        poly([(0,.68),(.18,.48),(.34,.66),(.50,.45),(.69,.68),(.84,.51),(1,.66),(1,1),(0,1)],(11,32,68))
        poly([(0,.82),(.28,.66),(.43,.82),(.72,.63),(1,.82),(1,1),(0,1)],(13,57,89))

    # Matrix Rain remains the original rain artwork, cached persistently after first generation.
    if key == "MATRIX":
        return pitv_artwork.cache_generated_artwork(key, int(w), int(h), im)
    return im


@lru_cache(maxsize=128)
def _fitted_artwork(key, width, height, source_width, source_height):
    art = _artwork(str(key), int(source_width), int(source_height))
    return ImageOps.fit(art, (int(width), int(height)), method=_RESAMPLING.BILINEAR)


@lru_cache(maxsize=32)
def _shaded_artwork(key, width, height, source_width, source_height, left_alpha):
    """Cache the tinted hero art so selection changes avoid rebuilding its shade."""
    cover = _fitted_artwork(
        str(key), int(width), int(height), int(source_width), int(source_height)
    )
    return _with_hero_shade(cover, int(left_alpha))


@lru_cache(maxsize=128)
def _cover_mask(width, height, radius):
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, width-1, height-1),
                                           radius=radius, fill=255)
    return mask


def _cover(canvas, box, key, radius=16, image_size=(600, 280)):
    x0, y0, x1, y1 = map(int, box)
    width, height = max(1, x1-x0), max(1, y1-y0)
    art = _fitted_artwork(str(key), width, height, image_size[0], image_size[1])
    mask = _cover_mask(width, height, int(radius))
    canvas.paste(art, (x0, y0), mask)


def _label(draw, xy, text, size=18, colour=WHITE, bold=False, anchor=None):
    draw.text(xy, str(text), font=_font(size, bold), fill=colour, anchor=anchor)


def _pill(draw, box, text, fill=(29, 71, 126), fg=WHITE, size=13):
    draw.rounded_rectangle(box, radius=(box[3]-box[1])//2, fill=fill)
    draw.text(((box[0]+box[2])//2, (box[1]+box[3])//2), str(text),
              font=_font(size, True), fill=fg, anchor="mm")


def _lock_icon(draw, x, y, scale=1.0, colour=(222, 235, 255)):
    # The lock is a real icon asset, rendered with the same proportions as the SVG.
    try:
        icon = pitv_artwork.load_icon("lock-keyhole", size=max(16, int(30 * scale)))
        draw._image.paste(icon, (int(x), int(y)), icon)
    except (AttributeError, OSError, RuntimeError, ValueError):
        # Keep the menu running if an asset cache is damaged; icons are rebuilt
        # on the next normal render.
        return


def _nav(canvas, section, clock_text, nav_focus=False, nav_selected=0):
    d = ImageDraw.Draw(canvas)
    logo = pitv_artwork.load_icon("monitor", size=30)
    canvas.paste(logo, (42, 18), logo)
    _label(d, (82, 20), "PiTV", 23, WHITE, True)
    items = [
        ("HOME", "MENU", 192, "house"),
        ("SCREENS", "DURATION_SELECT", 294, "monitor"),
        ("GAMES", "GAMES_MENU", 414, "gamepad-2"),
        ("SETTINGS", "SETTINGS", 536, "settings"),
    ]
    setting_views = {
        "SETTINGS", "SETTINGS_AUTH", "SETTINGS_INPUT",
        "SETTINGS_STATUS", "SETTINGS_LOG", "SETTINGS_CONFIRM",
    }
    for index, (label, active_view, x, icon_name) in enumerate(items):
        active = (
            (section == "HOME" and label == "HOME")
            or (section == "SCREENS" and label == "SCREENS")
            or (section == "GAMES" and label == "GAMES")
            or (section in setting_views and label == "SETTINGS")
        )
        focused = bool(nav_focus and index == int(nav_selected))
        if active or focused:
            d.rounded_rectangle((x-13, 15, x+96, 51), radius=16,
                                fill=(25, 71, 130) if active or focused else PANEL)
            d.rounded_rectangle((x-13, 46, x+96, 50), radius=2,
                                fill=CYAN if focused or active else (39, 63, 99))
        icon = pitv_artwork.load_icon(icon_name, size=19)
        canvas.paste(icon, (x-1, 20), icon)
        _label(d, (x+24, 25), label, 13,
               WHITE if active or focused else MUTED, active or focused)
    _label(d, (1230, 29), clock_text, 15, MUTED, anchor="ra")
    d.line((40, 62, 1240, 62), fill=(35, 59, 94), width=1)



def _with_hero_shade(cover, left_alpha=185):
    cover = cover.convert("RGBA")
    overlay = Image.new("RGBA", cover.size, (0,0,0,0))
    od = ImageDraw.Draw(overlay)
    w, h = cover.size
    for i in range(0, w, 8):
        strength = int(left_alpha * (1 - i / max(1,w)) ** 1.5)
        od.rectangle((i,0,min(w,i+8),h), fill=(3,8,24,strength))
    return Image.alpha_composite(cover, overlay).convert("RGB")


def _draw_carousel_strip(items, position):
    """Render only the carousel strip; cards and source art are reused from LRU caches."""
    strip = Image.new("RGB", (WIDTH, 295), (7, 13, 30))
    d = ImageDraw.Draw(strip)
    for band in range(24):
        y0 = band * strip.height // 24
        y1 = (band + 1) * strip.height // 24
        d.rectangle((0, y0, WIDTH, y1),
                    fill=_mix((8, 15, 34), (17, 36, 68), band / 23))
    d.ellipse((360, -180, 920, 340), fill=(16, 43, 83))
    d.line((38, 10, 1242, 10), fill=(26, 45, 75), width=1)

    if not items:
        d.rounded_rectangle((150, 42, 1130, 237), radius=24,
                            fill=(12, 24, 48), outline=(42, 68, 105), width=2)
        _label(d, (640, 115), "NO CAROUSEL GAMES AVAILABLE", 25, WHITE, True, anchor="mm")
        _label(d, (640, 158), "Open Games to browse installed titles.", 16, MUTED, anchor="mm")
        return strip

    count = len(items)
    pos = float(position) % count
    visible = []
    for index, item in enumerate(items):
        distance = float(index) - pos
        while distance > count / 2:
            distance -= count
        while distance < -count / 2:
            distance += count
        if abs(distance) <= 2.05:
            visible.append((distance, item))
    visible.sort(key=lambda pair: abs(pair[0]), reverse=True)

    for distance, item in visible:
        title, key, description = item
        abs_distance = abs(distance)
        scale = max(0.66, 1.0 - 0.18 * min(abs_distance, 1.9))
        card_w = int(410 * scale)
        card_h = int(218 * scale)
        cx = int(WIDTH / 2 + distance * 354)
        x0 = cx - card_w // 2
        y0 = 30 + (218 - card_h) // 2
        x1, y1 = x0 + card_w, y0 + card_h
        active = abs_distance < 0.5
        edge = CYAN if active else (35, 64, 103)
        edge_width = 3 if active else 1
        d.rounded_rectangle((x0-3, y0-3, x1+3, y1+3), radius=20,
                            fill=(15, 31, 57), outline=edge, width=edge_width)
        art = _fitted_artwork(str(key), card_w-10, card_h-10, 500, 260)
        if not active:
            art = Image.blend(art, Image.new("RGB", art.size, (3, 7, 18)),
                              min(0.54, 0.20 + abs_distance * 0.15))
        mask = _cover_mask(art.width, art.height, 15)
        strip.paste(art, (x0+5, y0+5), mask)
        # A dark lower ribbon keeps titles legible over any game artwork.
        d.rounded_rectangle((x0+5, y1-57, x1-5, y1-5), radius=12,
                            fill=(5, 12, 27))
        title_size = 22 if active else 16
        _label(d, (cx, y1-39), title, title_size,
               WHITE if active else (195, 208, 229), True, anchor="mm")
        if active and card_w > 350:
            detail = str(description or "").strip()
            if len(detail) > 58:
                detail = detail[:55].rstrip() + "..."
            _label(d, (cx, y1-16), detail, 12, (199, 216, 239), anchor="mm")

    # TV-style pagination: the active dot is longer and brighter, not numeric.
    dot_gap = 18
    total_width = max(0, count-1) * dot_gap + 16
    x_start = (WIDTH - total_width) // 2
    selected = int(round(pos)) % count
    for idx in range(count):
        x = x_start + idx * dot_gap
        if idx == selected:
            d.rounded_rectangle((x-4, 275, x+12, 282), radius=4, fill=CYAN)
        else:
            d.ellipse((x, 276, x+6, 282), fill=(91, 112, 145))
    return strip


_CAROUSEL_LOCK = threading.RLock()
_CAROUSEL_WAKE = threading.Event()
_CAROUSEL_STOP = threading.Event()
_CAROUSEL_THREAD = None
_CAROUSEL = {
    "view": None,
    "items": [],
    "position": 0.0,
    "target": None,
    "generation": 0,
    "autoplay": True,
    "interval": 8.0,
    "pause_until": 0.0,
    "next_auto": time.monotonic() + 8.0,
    "error": False,
}


def carousel_index():
    """Return the carousel's currently focused item, including in-flight moves."""
    with _CAROUSEL_LOCK:
        items = _CAROUSEL.get("items") or []
        if not items:
            return 0
        return int(round(float(_CAROUSEL.get("position", 0.0)))) % len(items)


def suspend_carousel():
    """Cancel animation immediately before a full-screen external activity starts."""
    with _CAROUSEL_LOCK:
        _CAROUSEL["view"] = "SUSPENDED"
        _CAROUSEL["target"] = None
        _CAROUSEL["position"] = float(round(_CAROUSEL["position"]))
        _CAROUSEL["generation"] += 1
        _CAROUSEL_WAKE.set()


def request_carousel_step(direction):
    """Request a smooth one-card move; rapid input updates the pending target."""
    direction = 1 if int(direction) > 0 else -1
    with _CAROUSEL_LOCK:
        items = _CAROUSEL.get("items") or []
        if _CAROUSEL.get("view") != "MENU" or len(items) < 2:
            return False
        base = (_CAROUSEL["target"] if _CAROUSEL["target"] is not None
                else round(float(_CAROUSEL["position"])))
        _CAROUSEL["target"] = int(base) + direction
        _CAROUSEL["generation"] += 1
        _CAROUSEL["pause_until"] = time.monotonic() + 6.0
        _CAROUSEL["next_auto"] = _CAROUSEL["pause_until"] + float(_CAROUSEL["interval"])
        _CAROUSEL_WAKE.set()
        return True


def _sync_carousel_runtime(state):
    global _CAROUSEL_THREAD
    items = [tuple(item) for item in state.get("carousel_games", [])]
    view = str(state.get("view", ""))
    enabled = bool(state.get("carousel_autoplay", True))
    try:
        interval = min(30.0, max(4.0, float(state.get("carousel_interval", 8))))
    except (TypeError, ValueError):
        interval = 8.0
    with _CAROUSEL_LOCK:
        old_items = _CAROUSEL["items"]
        changed_items = items != old_items
        changed_view = view != _CAROUSEL["view"]
        changed_settings = enabled != _CAROUSEL["autoplay"] or interval != _CAROUSEL["interval"]
        _CAROUSEL["view"] = view
        _CAROUSEL["autoplay"] = enabled
        _CAROUSEL["interval"] = interval
        runtime_changed = changed_items or changed_view or changed_settings
        if changed_items:
            _CAROUSEL["items"] = items
            requested = max(0, int(state.get("carousel_index", 0) or 0))
            _CAROUSEL["position"] = float(requested % len(items)) if items else 0.0
            _CAROUSEL["target"] = None
            _CAROUSEL["generation"] += 1
            _CAROUSEL["error"] = False
        elif changed_view and view != "MENU":
            # Cancel a transition before the new page is rendered.
            _CAROUSEL["target"] = None
            _CAROUSEL["position"] = float(round(_CAROUSEL["position"]))
            _CAROUSEL["generation"] += 1
        elif changed_view and view == "MENU":
            _CAROUSEL["position"] = float(round(_CAROUSEL["position"]))
            _CAROUSEL["target"] = None
            _CAROUSEL["generation"] += 1
        if changed_settings:
            _CAROUSEL["next_auto"] = time.monotonic() + interval
        if runtime_changed:
            _CAROUSEL_WAKE.set()
        if _CAROUSEL_THREAD is None or not _CAROUSEL_THREAD.is_alive():
            _CAROUSEL_STOP.clear()
            _CAROUSEL_THREAD = threading.Thread(
                target=_carousel_worker, name="pitv-carousel", daemon=True
            )
            _CAROUSEL_THREAD.start()


def _carousel_worker():
    """Animate only the carousel band; never ask the main renderer for a full frame."""
    while not _CAROUSEL_STOP.is_set():
        now = time.monotonic()
        with _CAROUSEL_LOCK:
            items = list(_CAROUSEL["items"])
            view = _CAROUSEL["view"]
            target = _CAROUSEL["target"]
            enabled = bool(_CAROUSEL["autoplay"])
            next_auto = float(_CAROUSEL["next_auto"])
            pause_until = float(_CAROUSEL["pause_until"])
            failed = bool(_CAROUSEL["error"])
            if view == "MENU" and len(items) > 1 and not failed and target is None:
                due = max(next_auto, pause_until)
                if enabled and now >= due:
                    target = round(float(_CAROUSEL["position"])) + 1
                    _CAROUSEL["target"] = target
                    _CAROUSEL["generation"] += 1
                    _CAROUSEL["next_auto"] = now + float(_CAROUSEL["interval"])
            generation = int(_CAROUSEL["generation"])
            start = float(_CAROUSEL["position"])
            target = _CAROUSEL["target"]
            interval = float(_CAROUSEL["interval"])
        if view != "MENU" or len(items) < 2 or failed or target is None:
            wait_for = 0.25
            with _CAROUSEL_LOCK:
                if enabled and view == "MENU" and not failed:
                    wait_for = min(wait_for, max(0.03, max(next_auto, pause_until)-time.monotonic()))
            _CAROUSEL_WAKE.wait(wait_for)
            _CAROUSEL_WAKE.clear()
            continue

        destination = float(target)
        duration = 0.38
        steps = 10
        interrupted = False
        for step in range(1, steps+1):
            fraction = step / steps
            eased = fraction * fraction * (3.0 - 2.0 * fraction)
            position = start + (destination-start) * eased
            # Generate the frame without holding the shared state lock. The
            # input loop and full-screen renderer can continue while Pillow
            # composes the artwork; generation is rechecked before writing.
            with _CAROUSEL_LOCK:
                if (_CAROUSEL["generation"] != generation
                        or _CAROUSEL["view"] != "MENU"
                        or not _CAROUSEL["items"]):
                    interrupted = True
                    break
                draw_items = list(_CAROUSEL["items"])
            try:
                strip = _draw_carousel_strip(draw_items, position)
            except Exception as exc:
                with _CAROUSEL_LOCK:
                    if _CAROUSEL["generation"] == generation:
                        _CAROUSEL["error"] = True
                        _CAROUSEL["target"] = None
                        _CAROUSEL["generation"] += 1
                interrupted = True
                try:
                    print(f"PiTV carousel drawing disabled after error: {type(exc).__name__}: {exc}")
                except Exception:
                    pass
                break

            # Keep the same lock order as the full renderer: carousel state,
            # then framebuffer lock. If another view arrived while drawing,
            # discard this stale frame instead of overwriting that screen.
            with _CAROUSEL_LOCK:
                if (_CAROUSEL["generation"] != generation
                        or _CAROUSEL["view"] != "MENU"
                        or draw_items != _CAROUSEL["items"]):
                    interrupted = True
                    break
                try:
                    pitv_framebuffer.display_image_region(
                        strip, (0, 70, WIDTH, 365), source_size=(WIDTH, HEIGHT)
                    )
                except Exception as exc:
                    _CAROUSEL["error"] = True
                    _CAROUSEL["target"] = None
                    _CAROUSEL["generation"] += 1
                    interrupted = True
                    # Don't flood the log or retry the same failing write forever.
                    try:
                        print(f"PiTV carousel animation disabled after framebuffer error: {exc}")
                    except Exception:
                        pass
                    break
                _CAROUSEL["position"] = position
            time.sleep(duration / steps)

        with _CAROUSEL_LOCK:
            if interrupted:
                continue
            if _CAROUSEL["generation"] == generation and _CAROUSEL["view"] == "MENU":
                _CAROUSEL["position"] = destination
                _CAROUSEL["target"] = None
                _CAROUSEL["next_auto"] = time.monotonic() + interval
                _CAROUSEL_WAKE.clear()


def _draw_home(canvas, state):
    d = ImageDraw.Draw(canvas)
    items = state.get("home_items", [])
    selected = max(0, min(int(state.get("selected_art", 0)), max(0, len(items)-1)))
    focus = str(state.get("home_focus", "carousel"))
    nav_selected = int(state.get("nav_selected", 0))
    carousel_items = state.get("carousel_games", [])
    carousel_position = float(state.get("carousel_index", 0) or 0)
    strip = _draw_carousel_strip(carousel_items, carousel_position)
    canvas.paste(strip, (0, 70))

    _label(d, (54, 370), "QUICK ACCESS", 12, MUTED, True)
    tile_w, tile_h, tile_gap = 216, 83, 18
    total = len(items)*tile_w+(len(items)-1)*tile_gap
    x0 = (WIDTH-total)//2
    for idx, (title, art_key, _desc) in enumerate(items):
        x = x0 + idx*(tile_w+tile_gap)
        y = 392
        chosen = idx == selected and focus == "shortcuts"
        d.rounded_rectangle((x, y, x+tile_w, y+tile_h+33), radius=15,
                            fill=(20, 63, 109) if chosen else PANEL,
                            outline=CYAN if chosen else (37, 58, 89),
                            width=3 if chosen else 1)
        _cover(canvas, (x+5, y+5, x+tile_w-5, y+tile_h), art_key, 10, (500, 250))
        d = ImageDraw.Draw(canvas)
        _label(d, (x+tile_w//2, y+tile_h+18), title, 14,
               WHITE if chosen else (194, 210, 233), chosen, anchor="mm")

    d = ImageDraw.Draw(canvas)
    _label(d, (54, 524), "RECENTLY PLAYED", 12, MUTED, True)
    recent = state.get("recent_games", [])
    recent_selected = max(0, min(int(state.get("selected_recent", 0)), max(0, len(recent)-1)))
    if not recent:
        _label(d, (640, 581), "Games you launch will appear here.", 16,
               (128, 150, 183), anchor="mm")
    else:
        card_w, card_h, gap = 184, 77, 13
        total = len(recent)*card_w + max(0, len(recent)-1)*gap
        x0 = (WIDTH-total)//2
        for idx, (title, key, _desc) in enumerate(recent[:6]):
            x = x0 + idx*(card_w+gap)
            y = 542
            chosen = idx == recent_selected and focus == "recent"
            d.rounded_rectangle((x, y, x+card_w, y+card_h+31), radius=12,
                                fill=(20, 63, 109) if chosen else PANEL,
                                outline=CYAN if chosen else (37, 58, 89),
                                width=3 if chosen else 1)
            _cover(canvas, (x+4, y+4, x+card_w-4, y+card_h), key, 8, (500, 250))
            d = ImageDraw.Draw(canvas)
            _label(d, (x+card_w//2, y+card_h+17), title, 12,
                   WHITE if chosen else (194, 210, 233), chosen, anchor="mm")

    d = ImageDraw.Draw(canvas)
    hints = {
        "carousel": "LEFT / RIGHT  BROWSE GAMES     SELECT  PLAY     UP  NAVIGATION     DOWN  SHORTCUTS",
        "shortcuts": "LEFT / RIGHT  CHOOSE     SELECT  OPEN     UP  GAMES     DOWN  RECENTLY PLAYED",
        "recent": "LEFT / RIGHT  CHOOSE     SELECT  PLAY AGAIN     UP  SHORTCUTS",
        "nav": "LEFT / RIGHT  NAVIGATE     SELECT  OPEN     DOWN  HOME CONTENT",
    }
    _label(d, (640, 692), hints.get(focus, hints["carousel"]), 12, MUTED, True, anchor="mm")
    return canvas



def _draw_settings(canvas, state):
    d = ImageDraw.Draw(canvas)
    _nav(canvas, "SETTINGS", state.get("clock_text", ""), False, 0)
    page = str(state.get("settings_path_label", "Settings"))
    _label(d, (54, 84), page.upper(), 22, WHITE, True)
    description = str(state.get("settings_description", "Manage PiTV from the TV remote."))
    _label(d, (54, 112), description[:100], 13, MUTED)
    items = list(state.get("settings_items", []))
    selected = max(0, min(int(state.get("settings_selected", 0)), max(0, len(items)-1)))
    start = max(0, min(max(0, len(items)-6), selected-3))
    if not items:
        _label(d, (640, 350), "Nothing to show here.", 20, MUTED, anchor="mm")
    else:
        for visible_index, item_index in enumerate(range(start, min(len(items), start+6))):
            item = items[item_index]
            label = str(item[0])
            status = str(item[1]) if len(item) > 1 else ""
            detail = str(item[2]) if len(item) > 2 else ""
            y = 148 + visible_index * 76
            chosen = item_index == selected
            d.rounded_rectangle((54, y, 1226, y+64), radius=14,
                                fill=(22, 66, 115) if chosen else PANEL,
                                outline=CYAN if chosen else (35, 55, 87),
                                width=3 if chosen else 1)
            _label(d, (78, y+17), label[:55], 17,
                   WHITE if chosen else (212, 223, 241), chosen)
            if detail:
                _label(d, (79, y+42), detail[:90], 11, MUTED)
            if status:
                _label(d, (1197, y+30), status[:24], 13,
                       CYAN if chosen else (168, 194, 223), True, anchor="ra")
    message = str(state.get("settings_message", ""))
    if message:
        _label(d, (640, 635), message[:115], 13, GOLD, True, anchor="mm")
    _label(d, (640, 687), "ARROWS  MOVE     SELECT  CONFIRM     BACK  PREVIOUS MENU",
           12, MUTED, True, anchor="mm")


def _draw_settings_auth(canvas, state):
    d = ImageDraw.Draw(canvas)
    _nav(canvas, "SETTINGS", state.get("clock_text", ""), False, 0)
    _label(d, (640, 110), "ADMINISTRATOR AUTHORISATION", 23, WHITE, True, anchor="mm")
    _label(d, (640, 148), "Enter the six-digit emergency code to continue.", 16, MUTED, anchor="mm")
    entered = str(state.get("settings_auth_entered", ""))
    masked = "  ".join(["*"] * min(6, len(entered)) + ["_"] * max(0, 6-len(entered)))
    _label(d, (640, 193), masked, 31, CYAN, True, anchor="mm")
    layout = [["1","2","3"],["4","5","6"],["7","8","9"],["DEL","0","OK"]]
    selected_row = max(0, min(3, int(state.get("settings_key_row", 0))))
    selected_col = max(0, min(2, int(state.get("settings_key_col", 0))))
    card_w, card_h, gx, gy = 140, 62, 18, 10
    x0 = (WIDTH - (3*card_w+2*gx))//2
    for row, values in enumerate(layout):
        for col, value in enumerate(values):
            x = x0 + col*(card_w+gx)
            y = 228 + row*(card_h+gy)
            chosen = row == selected_row and col == selected_col
            d.rounded_rectangle((x,y,x+card_w,y+card_h), radius=13,
                                fill=(22, 83, 143) if chosen else PANEL,
                                outline=CYAN if chosen else (39,63,99),
                                width=3 if chosen else 1)
            _label(d,(x+card_w//2,y+card_h//2),value,21,WHITE,True,anchor="mm")
    error = str(state.get("settings_message", ""))
    if error:
        _label(d, (640, 545), error[:95], 14, (255, 144, 160), True, anchor="mm")
    _label(d, (640, 665), "BACK  CANCEL", 13, MUTED, True, anchor="mm")


def _draw_settings_input(canvas, state):
    d = ImageDraw.Draw(canvas)
    _nav(canvas, "SETTINGS", state.get("clock_text", ""), False, 0)
    prompt = str(state.get("settings_input_prompt", "Enter text"))
    mode = str(state.get("settings_input_mode", "text"))
    _label(d, (640, 96), prompt.upper()[:72], 21, WHITE, True, anchor="mm")
    value = str(state.get("settings_text", ""))
    shown = "  ".join("*" for _ in value) if mode == "pin" else value
    _label(d, (640, 139), shown[-36:] or "_", 22, CYAN, True, anchor="mm")
    if mode in ("pin", "auth_pin"):
        layout = [["1","2","3"],["4","5","6"],["7","8","9"],["DEL","0","OK"]]
    else:
        layout = [
            list("ABCDEFG"),
            list("HIJKLMN"),
            list("OPQRSTU"),
            ["V", "W", "X", "Y", "Z", "_", "<"],
            ["0","1","2","3","4","5","6"],
            ["7","8","9","-",".","CASE","DONE"],
        ]
        if state.get("settings_input_lowercase"):
            layout = [
                [key.lower() if len(key) == 1 and key.isalpha() else key for key in row]
                for row in layout
            ]
    row_sel = max(0, min(len(layout)-1, int(state.get("settings_key_row",0))))
    col_sel = max(0, min(max(len(row) for row in layout)-1, int(state.get("settings_key_col",0))))
    if mode in ("pin", "auth_pin"):
        card_w, card_h, gap_x, gap_y = 140, 62, 18, 10
        x0 = (WIDTH-(3*card_w+2*gap_x))//2
        y0 = 205
    else:
        card_w, card_h, gap_x, gap_y = 148, 48, 10, 8
        x0 = (WIDTH-(7*card_w+6*gap_x))//2
        y0 = 190
    for row, values in enumerate(layout):
        for col, key in enumerate(values):
            x = x0 + col*(card_w+gap_x)
            y = y0 + row*(card_h+gap_y)
            chosen = row == row_sel and col == col_sel
            d.rounded_rectangle((x,y,x+card_w,y+card_h), radius=10,
                                fill=(22, 83, 143) if chosen else PANEL,
                                outline=CYAN if chosen else (39,63,99),
                                width=3 if chosen else 1)
            label = {"_":"SPACE","<":"DELETE","CASE":"case","DONE":"DONE"}.get(key,key)
            _label(d,(x+card_w//2,y+card_h//2),label,15,WHITE,True,anchor="mm")
    _label(d, (640, 660), "ARROWS  MOVE     SELECT  ADD CHARACTER     BACK  CANCEL",
           12, MUTED, True, anchor="mm")


def _draw_settings_log(canvas, state):
    d = ImageDraw.Draw(canvas)
    _nav(canvas, "SETTINGS", state.get("clock_text", ""), False, 0)
    _label(d, (54, 88), "RECENT PI TV LOGS", 22, WHITE, True)
    lines = list(state.get("settings_log_lines", []))
    scroll = max(0, min(int(state.get("settings_log_scroll",0)), max(0,len(lines)-18)))
    visible = lines[scroll:scroll+18]
    for idx, line in enumerate(visible):
        y = 126 + idx*28
        _label(d, (54, y), str(line)[:150], 12,
               (255, 193, 124) if "error" in str(line).lower() or "fail" in str(line).lower() else (190, 207, 230))
    _label(d, (640, 686), "UP / DOWN  SCROLL     LEFT  CLEAR WITH CONFIRMATION     BACK  SETTINGS",
           12, MUTED, True, anchor="mm")



def _draw_games(canvas, state):
    d = ImageDraw.Draw(canvas)
    _label(d, (54, 77), "GAMES LIBRARY", 22, WHITE, True)
    _label(d, (1224, 81), "PIN PROTECTED", 13, MUTED, True, anchor="ra")
    games = state["games"]
    selected = max(0, min(int(state.get("selected_game", 0)), len(games)-1))
    cols = 5
    gap_x, gap_y = 14, 9
    card_w, card_h = 212, 133
    total_w = cols*card_w+(cols-1)*gap_x
    x0 = (WIDTH-total_w)//2
    y0 = 97
    rows = (len(games)+cols-1)//cols
    total_h = rows*card_h+(rows-1)*gap_y
    # If the game list grows, move the selected row into view rather than
    # shrinking the artwork into unreadable thumbnails.
    max_rows = max(1, (HEIGHT-y0-58+gap_y)//(card_h+gap_y))
    first_row = max(0, min(rows-max_rows, selected//cols-max_rows//2))
    for idx, (title, key, tag) in enumerate(games):
        row, col = divmod(idx, cols)
        if row < first_row or row >= first_row+max_rows:
            continue
        x = x0+col*(card_w+gap_x)
        y = y0+(row-first_row)*(card_h+gap_y)
        chosen = idx == selected
        bg = (24, 58, 105) if chosen else PANEL
        d.rounded_rectangle((x,y,x+card_w,y+card_h),radius=15,
                            fill=bg,outline=(67,170,255) if chosen else (36,54,86),
                            width=3 if chosen else 1)
        art_box=(x+7,y+7,x+card_w-7,y+92)
        _cover(canvas,art_box,key,10,(440,180))
        d=ImageDraw.Draw(canvas)
        # Every game uses a PIN, represented visually instead of a line of status text.
        _lock_icon(d,x+card_w-33,y+12,.68,(224,239,255))
        _label(d,(x+card_w//2,y+103),title,14,WHITE if chosen else (208,220,240),True,anchor="mm")
        if tag == "NOT INSTALLED":
            _pill(d,(x+card_w-87,y+111,x+card_w-10,y+128),"NOT INSTALLED",(71,43,53),(255,191,192),9)
        elif tag:
            # Keep availability feedback discreet and let the cover art lead.
            d.ellipse((x+12,y+116,x+19,y+123),fill=CYAN if chosen else (107,133,167))
    d = ImageDraw.Draw(canvas)
    _label(d,(54,685),"ARROWS  BROWSE     SELECT  PLAY     BACK  HOME",13,MUTED,True)
    _label(d,(1224,685),f"{selected+1} / {len(games)}",13,MUTED,anchor="ra")


def _draw_duration(canvas, state):
    d=ImageDraw.Draw(canvas)
    label=state.get("selected_art_label","SCREENSAVER")
    key=state.get("selected_art_key","CLOCK")
    _label(d,(54,78),"SCREENSAVERS",14,MUTED,True)
    hero_box=(48,101,1232,340)
    cover=_shaded_artwork(
        key, hero_box[2]-hero_box[0], hero_box[3]-hero_box[1], 840, 360, 155
    )
    mask=Image.new("L",cover.size,0)
    ImageDraw.Draw(mask).rounded_rectangle((0,0,cover.width-1,cover.height-1),radius=22,fill=255)
    canvas.paste(cover,hero_box[:2],mask)
    d=ImageDraw.Draw(canvas)
    _label(d,(79,222),label,34,WHITE,True)
    d.rounded_rectangle(hero_box,radius=22,outline=(49,124,211),width=2)
    options=state.get("durations",[])
    selected=max(0,min(int(state.get("selected_duration",0)),len(options)-1))
    card_w,card_h,gap=270,170,18
    total=len(options)*card_w+(len(options)-1)*gap
    x0=(WIDTH-total)//2
    for idx, title in enumerate(options):
        x=x0+idx*(card_w+gap); y=393
        chosen=idx==selected
        d=ImageDraw.Draw(canvas)
        d.rounded_rectangle((x,y,x+card_w,y+card_h),radius=18,
                            fill=(22,60,107) if chosen else PANEL,
                            outline=CYAN if chosen else (39,63,99),width=3 if chosen else 1)
        _cover(canvas,(x+7,y+7,x+card_w-7,y+111),
               ("DURATION_"+str(idx)),12,(500,220))
        d=ImageDraw.Draw(canvas)
        _label(d,(x+card_w//2,y+140),title,20,WHITE if chosen else (194,210,233),True,anchor="mm")
    d=ImageDraw.Draw(canvas)
    _label(d,(54,682),"ARROWS  CHOOSE     SELECT  START     BACK  HOME",13,MUTED,True)


def _draw_ttt(canvas, state):
    d=ImageDraw.Draw(canvas)
    _label(d,(54,79),"NOUGHTS & CROSSES",22,WHITE,True)
    options=[("1 PLAYER","TTT_COMPUTER"),("2 PLAYER","TTT_LOCAL")]
    selected=int(state.get("ttt_selected",0))
    card_w,card_h,gap=430,340,32
    x0=(WIDTH-(2*card_w+gap))//2
    y=176
    for idx,(label,key) in enumerate(options):
        x=x0+idx*(card_w+gap); chosen=idx==selected
        d.rounded_rectangle((x,y,x+card_w,y+card_h),radius=24,
                            fill=(23,60,109) if chosen else PANEL,
                            outline=CYAN if chosen else (39,63,99),width=4 if chosen else 1)
        _cover(canvas,(x+12,y+12,x+card_w-12,y+257),key,18,(560,340))
        d=ImageDraw.Draw(canvas)
        _label(d,(x+card_w//2,y+291),label,26,WHITE if chosen else MUTED,True,anchor="mm")
    d=ImageDraw.Draw(canvas)
    _label(d,(54,681),"LEFT / RIGHT  CHOOSE     SELECT  CONFIRM     BACK  RETURN",13,MUTED,True)


def _draw_imposter_count(canvas, state):
    d=ImageDraw.Draw(canvas)
    _label(d,(54,79),"IMPOSTER",22,WHITE,True)
    _label(d,(1224,81),"PARTY GAME",13,MUTED,True,anchor="ra")
    options=state.get("imposter_counts",[])
    if not options:
        options=[3]
    selected=max(0,min(int(state.get("imposter_count_index",0)),len(options)-1))
    # Show a centred, scrollable strip so a larger group of active PINs does
    # not push player cards off either edge of the TV.
    card_w,card_h,gap=132,168,14
    visible=min(len(options),7)
    total=visible*card_w+max(0,visible-1)*gap
    x0=(WIDTH-total)//2
    first=max(0,min(len(options)-visible,selected-visible//2))
    y=260
    for idx in range(first,first+visible):
        count=options[idx]
        x=x0+(idx-first)*(card_w+gap); chosen=idx==selected
        d.rounded_rectangle((x,y,x+card_w,y+card_h),radius=18,
                            fill=(24,62,113) if chosen else PANEL,
                            outline=CYAN if chosen else (39,63,99),width=3 if chosen else 1)
        # Use the bundled user icon instead of approximating people with circles and blocks.
        avatar = pitv_artwork.load_icon("user-round", size=27)
        for person in range(min(count,5)):
            px=x+18+(person%3)*36
            py=y+28+(person//3)*39
            canvas.paste(avatar, (px, py), avatar)
        _label(d,(x+card_w//2,y+119),str(count),28,WHITE if chosen else MUTED,True,anchor="mm")
        if chosen:
            _pill(d,(x+34,y+143,x+card_w-34,y+164),"SELECTED",(29,104,165),WHITE,9)
    _label(d,(640,205),"How many players are joining?",19,(214,227,245),anchor="mm")
    _label(d,(54,681),"UP  ADD PLAYER     DOWN  REMOVE PLAYER     SELECT  CONTINUE     BACK  CANCEL",13,MUTED,True)


def _draw_keypad(canvas, state):
    d=ImageDraw.Draw(canvas)
    is_lock=bool(state.get("is_lock"))
    title="SYSTEM LOCKED" if is_lock else str(state.get("keypad_title","PLAYER ACCESS"))
    _label(d,(54,78),title,22,WHITE,True)
    _label(d,(1224,81),"ENTER CODE",13,MUTED,True,anchor="ra")
    d.rounded_rectangle((300,115,980,201),radius=18,fill=(12,22,45),outline=(38,68,111),width=2)
    entered=str(state.get("keypad_entered",""))
    masked=" ".join("*" if i<len(entered) and entered[i]!="_" else "_" for i in range(6))
    _label(d,(640,156),masked,34,CYAN if not state.get("keypad_error") else (255,117,142),True,anchor="mm")
    if state.get("keypad_error"):
        _label(d,(640,192),state["keypad_error"],13,(255,155,170),anchor="mm")
    layout=[["1","2","3"],["4","5","6"],["7","8","9"],["DEL","0","OK"]]
    card_w,card_h,gap_x,gap_y=148,69,18,10
    total=3*card_w+2*gap_x
    x0=(WIDTH-total)//2
    y0=224
    row_sel=int(state.get("keypad_row",0)); col_sel=int(state.get("keypad_col",0))
    for r,row in enumerate(layout):
        for c,value in enumerate(row):
            x=x0+c*(card_w+gap_x); y=y0+r*(card_h+gap_y); chosen=(r==row_sel and c==col_sel)
            fill=(22,91,157) if chosen else PANEL
            edge=CYAN if chosen else (39,63,99)
            d.rounded_rectangle((x,y,x+card_w,y+card_h),radius=15,fill=fill,outline=edge,width=3 if chosen else 1)
            _label(d,(x+card_w//2,y+card_h//2),value,23 if len(value)<3 else 17,
                   WHITE if chosen else (215,228,247),True,anchor="mm")
    _label(d,(640,565),"Enter your six-digit code",16,MUTED,anchor="mm")
    _label(d,(640,667),"ARROWS  MOVE     SELECT  ENTER     BACK  DELETE / RETURN",13,MUTED,True,anchor="mm")


def _draw_lock(canvas, state):
    d = ImageDraw.Draw(canvas)
    # Use the real Lucide lock icon; the circle is only a restrained backdrop.
    d.ellipse((530, 145, 750, 365), fill=(12, 25, 52), outline=(80, 154, 241), width=4)
    icon = pitv_artwork.load_icon("lock-keyhole", size=142)
    canvas.paste(icon, (569, 184), icon)
    _label(d, (640, 418), "SYSTEM LOCKED DOWN", 30, WHITE, True, anchor="mm")
    _label(d, (640, 469), "An administrator must authorise access from SSH.", 17, MUTED, anchor="mm")
    _label(d, (640, 508), "The TV remains locked until the emergency code is accepted.", 15, MUTED, anchor="mm")




def _draw_music(canvas, state):
    """Dedicated full-screen, image-rendered AirPlay status page for V2."""
    d = ImageDraw.Draw(canvas)
    connected = bool(state.get("music_connected"))
    device = str(state.get("music_device") or "").strip()
    title = str(state.get("music_title") or "").strip()
    artist = str(state.get("music_artist") or "").strip()

    logo = pitv_artwork.load_icon("monitor", size=30)
    canvas.paste(logo, (46, 22), logo)
    _label(d, (86, 24), "PiTV", 23, WHITE, True)
    _label(d, (162, 29), "STREAM MUSIC", 14, (119, 190, 255), True)
    status = "CONNECTED" if connected else "READY FOR AIRPLAY"
    status_fill = (17, 92, 94) if connected else (29, 58, 92)
    _pill(d, (1004, 20, 1234, 58), status, status_fill, WHITE, 12)
    d.line((40, 72, 1240, 72), fill=(35, 59, 94), width=1)

    d.rounded_rectangle((150, 108, 1130, 618), radius=30,
                        fill=(10, 20, 43), outline=(39, 67, 108), width=2)

    # Use the bundled AirPlay SVG artwork instead of a text glyph or approximation.
    icon_size = 82 if not connected else 64
    airplay = pitv_artwork.load_icon("airplay", size=icon_size)
    icon_box = (590, 145, 690, 245)
    d.rounded_rectangle(icon_box, radius=24, fill=(16, 43, 78),
                        outline=(41, 96, 151), width=2)
    icon_x = icon_box[0] + (100 - icon_size) // 2
    icon_y = icon_box[1] + (100 - icon_size) // 2
    canvas.paste(airplay, (icon_x, icon_y), airplay)

    if not connected:
        _label(d, (640, 307), "Ready to play music", 32, WHITE, True, anchor="mm")
        _label(d, (640, 356), "Select PiTV from the AirPlay menu on your device.", 19,
               (187, 205, 231), anchor="mm")
        _label(d, (640, 394), "Your music will play through the TV audio system.",
               16, MUTED, anchor="mm")
    elif title:
        _label(d, (640, 292), "NOW PLAYING", 13, (117, 195, 255), True, anchor="mm")
        title_text = title if len(title) <= 54 else title[:51].rstrip() + "..."
        artist_text = artist if len(artist) <= 76 else artist[:73].rstrip() + "..."
        _label(d, (640, 350), title_text, 29, WHITE, True, anchor="mm")
        if artist_text:
            _label(d, (640, 393), artist_text, 19, (187, 205, 231), anchor="mm")
        if device:
            device_text = device if len(device) <= 65 else device[:62].rstrip() + "..."
            _label(d, (640, 464), "Playing from " + device_text, 15, MUTED, anchor="mm")
    else:
        connected_text = device if len(device) <= 58 else device[:55].rstrip() + "..."
        _label(d, (640, 315), "Connected and ready", 30, WHITE, True, anchor="mm")
        _label(d, (640, 362), connected_text or "AirPlay device", 20,
               (187, 205, 231), anchor="mm")
        _label(d, (640, 405), "Start playing audio on your device.", 16, MUTED, anchor="mm")

    _label(d, (640, 665), "BACK   RETURN TO PiTV", 14, MUTED, True, anchor="mm")


def _imposter_header(canvas, title, tag="PARTY GAME"):
    d = ImageDraw.Draw(canvas)
    logo = pitv_artwork.load_icon("monitor", size=30)
    canvas.paste(logo, (42, 20), logo)
    _label(d, (82, 22), "PiTV", 23, WHITE, True)
    _label(d, (158, 28), title, 15, (119, 190, 255), True)
    _pill(d, (1000, 18, 1236, 56), tag, (27, 61, 96), WHITE, 11)
    d = ImageDraw.Draw(canvas)
    d.line((40, 70, 1240, 70), fill=(35, 59, 94), width=1)
    return d


def _imposter_footer(draw, text):
    _label(draw, (640, 681), text, 14, MUTED, True, anchor="mm")


def _draw_imposter_word(canvas, state):
    selection_stage = str(state.get("selection_stage") or "word").strip().lower()
    category_stage = selection_stage == "category"
    screen_title = "IMPOSTER  /  CHOOSE CATEGORY" if category_stage else "IMPOSTER  /  CHOOSE WORD"
    status_tag = "CATEGORY SELECTION" if category_stage else "WORD SELECTION"
    d = _imposter_header(canvas, screen_title, status_tag)
    d.rounded_rectangle((128, 116, 1152, 602), radius=28, fill=(10, 20, 43),
                        outline=(39, 67, 108), width=2)
    icon = pitv_artwork.load_icon("scroll-text", size=72)
    canvas.paste(icon, (604, 152), icon)
    d = ImageDraw.Draw(canvas)
    _label(d, (640, 269), "EVERYONE IS CONNECTED", 29, WHITE, True, anchor="mm")
    if category_stage:
        _label(d, (640, 323), "The word chooser is selecting a category on their phone.", 18,
               (187, 205, 231), anchor="mm")
        _label(d, (640, 360), "Next, they will choose a word from that category.", 17, MUTED, anchor="mm")
        waiting = "PICKING CATEGORY"
    else:
        _label(d, (640, 323), "The word chooser is selecting a word on their phone.", 18,
               (187, 205, 231), anchor="mm")
        _label(d, (640, 360), "Regular players get the word; the imposter gets only the category.", 17,
               MUTED, anchor="mm")
        waiting = "PICKING WORD"
    picker = str(state.get("word_picker") or "").strip()
    if picker:
        _pill(d, (390, 411, 890, 463), "WORD CHOOSER  ·  " + picker[:30],
              (24, 62, 105), WHITE, 15)
        d = ImageDraw.Draw(canvas)
    players = state.get("players") or []
    _label(d, (640, 510), f"{len(players)} PLAYERS  ·  {waiting}", 14,
           (119, 190, 255), True, anchor="mm")
    _imposter_footer(d, "CATEGORY AND WORD ARE SELECTED ON PHONES  ·  BACK: END GAME")
def _draw_imposter_turn(canvas, state):
    round_number = max(1, int(state.get("round_number", 1) or 1))
    turn_index = max(0, int(state.get("turn_index", 0) or 0))
    player_count = max(1, int(state.get("player_count", len(state.get("players") or []) or 1)))
    current_turn = str(state.get("current_turn") or "NEXT PLAYER")
    d = _imposter_header(canvas, f"IMPOSTER  /  CLUE ROUND {round_number:02d}", "PLAYER TURN")
    _label(d, (640, 112), f"PLAYER {min(turn_index + 1, player_count)} OF {player_count}",
           15, (119, 190, 255), True, anchor="mm")
    d.rounded_rectangle((190, 158, 1090, 463), radius=30, fill=(10, 20, 43),
                        outline=(48, 100, 160), width=3)
    _pill(d, (510, 185, 770, 225), "IT'S YOUR TURN", (19, 80, 95), WHITE, 13)
    d = ImageDraw.Draw(canvas)
    name = current_turn.upper()
    name_size = 50 if len(name) <= 15 else 38 if len(name) <= 22 else 29
    _label(d, (640, 294), name, name_size, WHITE, True, anchor="mm")
    _label(d, (640, 356), "GIVE ONE CLUE", 20, (119, 190, 255), True, anchor="mm")
    _label(d, (640, 395), "Don't say the secret word. Keep your role secret.", 16,
           (187, 205, 231), anchor="mm")
    progress_x, progress_y, progress_w = 300, 500, 680
    d.rounded_rectangle((progress_x, progress_y, progress_x+progress_w, progress_y+12),
                        radius=6, fill=(22, 36, 59))
    d.rounded_rectangle((progress_x, progress_y,
                         progress_x + int(progress_w * min(1.0, (turn_index + 1) / player_count)),
                         progress_y+12), radius=6, fill=CYAN)
    _imposter_footer(d, "SELECT / RIGHT  NEXT PLAYER     ·     BACK  PREVIOUS PLAYER")


def _draw_imposter_round_complete(canvas, state):
    round_number = max(1, int(state.get("round_number", 1) or 1))
    choice = int(state.get("choice", 0) or 0)
    d = _imposter_header(canvas, f"IMPOSTER  /  CLUE ROUND {round_number:02d}", "ROUND COMPLETE")
    _label(d, (640, 115), "EVERYONE HAS HAD A TURN", 26, WHITE, True, anchor="mm")
    _label(d, (640, 154), "Repeat clues as often as you like, or vote when ready.", 17,
           MUTED, anchor="mm")
    boxes = [(124, 218, 622, 548), (658, 218, 1156, 548)]
    items = [
        ("ANOTHER CLUE ROUND", "Same roles and secret word.", "clock"),
        ("START THE VOTE", "Lock in suspicions on phones.", "chart-no-axes-combined"),
    ]
    for idx, (box, item) in enumerate(zip(boxes, items)):
        x0, y0, x1, y1 = box
        title, description, icon_name = item
        selected = idx == choice
        d.rounded_rectangle((x0, y0, x1, y1), radius=24,
                            fill=(24, 70, 117) if selected else PANEL,
                            outline=CYAN if selected else (39, 63, 99),
                            width=4 if selected else 1)
        icon = pitv_artwork.load_icon(icon_name, size=62)
        canvas.paste(icon, ((x0+x1-icon.width)//2, 263), icon)
        d = ImageDraw.Draw(canvas)
        _label(d, ((x0+x1)//2, 378), title, 20, WHITE, True, anchor="mm")
        _label(d, ((x0+x1)//2, 428), description, 15, (187, 205, 231), anchor="mm")
        if selected:
            _pill(d, (x0+132, 475, x1-132, 511), "SELECTED", (26, 102, 163), WHITE, 11)
            d = ImageDraw.Draw(canvas)
    _imposter_footer(d, "UP / DOWN  CHOOSE     SELECT  CONFIRM     ·     BACK  PREVIOUS PLAYER")


def _draw_imposter_vote(canvas, state):
    remaining = max(0, int(state.get("seconds_remaining", 0) or 0))
    player_count = max(1, int(state.get("player_count", 1) or 1))
    votes_cast = max(0, int(state.get("votes_cast", 0) or 0))
    d = _imposter_header(canvas, "IMPOSTER  /  FINAL VOTE", "VOTE OPEN")
    d.rounded_rectangle((175, 112, 1105, 600), radius=28, fill=(10, 20, 43),
                        outline=(39, 67, 108), width=2)
    _label(d, (640, 171), "WHO IS THE IMPOSTER?", 25, WHITE, True, anchor="mm")
    _label(d, (640, 243), f"{remaining:02d}", 70, GOLD if remaining <= 5 else CYAN,
           True, anchor="mm")
    _label(d, (640, 288), "SECONDS LEFT", 14, MUTED, True, anchor="mm")
    progress = min(1.0, votes_cast / player_count)
    d.rounded_rectangle((340, 347, 940, 365), radius=9, fill=(22, 36, 59))
    if progress > 0:
        d.rounded_rectangle((340, 347, 340 + int(600*progress), 365), radius=9, fill=CYAN)
    _label(d, (640, 410), f"{votes_cast} OF {player_count} VOTES SUBMITTED", 16,
           (187, 205, 231), True, anchor="mm")
    _label(d, (640, 458), "Choose a suspect on your phone. Votes stay hidden.", 17,
           WHITE, anchor="mm")
    _label(d, (640, 491), "You may change your vote until the timer expires.", 15, MUTED, anchor="mm")
    _imposter_footer(d, "BACK  RETURN TO THE LAST PLAYER'S TURN")


def _draw_imposter_results(canvas, state):
    stage = max(0, min(3, int(state.get("podium_stage", 0) or 0)))
    results = sorted(list(state.get("results") or []),
                     key=lambda item: (-int(item.get("count", 0) or 0),
                                       str(item.get("name", "")).casefold()))
    total_votes = sum(max(0, int(item.get("count", 0) or 0)) for item in results)
    d = _imposter_header(canvas, "IMPOSTER  /  THE VOTE", "PODIUM REVEAL")
    _label(d, (640, 106), "THE MOST-VOTED PLAYERS", 20, WHITE, True, anchor="mm")

    base_y = 602
    podiums = [
        {"rank": 3, "box": (205, 472, 425, base_y), "colour": (33, 65, 102), "stage": 1},
        {"rank": 2, "box": (855, 430, 1075, base_y), "colour": (32, 99, 125), "stage": 2},
        {"rank": 1, "box": (515, 365, 765, base_y), "colour": (138, 96, 31), "stage": 3},
    ]
    for podium in podiums:
        x0, y0, x1, y1 = podium["box"]
        d.rounded_rectangle((x0, y0, x1, y1), radius=16, fill=podium["colour"],
                            outline=(52, 91, 133), width=2)
        d.line((x0+16, y0+8, x1-16, y0+8), fill=(100, 153, 197), width=2)
        _label(d, ((x0+x1)//2, y1-44), str(podium["rank"]), 36, WHITE, True, anchor="mm")
        if stage < podium["stage"]:
            continue
        idx = {3: 2, 2: 1, 1: 0}[podium["rank"]]
        if idx >= len(results):
            continue
        item = results[idx]
        name = str(item.get("name", "")).strip() or "UNKNOWN"
        display_name = name.upper()
        name_size = 24 if len(display_name) <= 13 else 18 if len(display_name) <= 19 else 14
        label_y = y0 - 49
        _label(d, ((x0+x1)//2, label_y), display_name, name_size, WHITE, True, anchor="mm")
        _label(d, ((x0+x1)//2, label_y+24),
               f"{max(0, int(item.get('count', 0) or 0))} VOTES", 13,
               GOLD if podium["rank"] == 1 else CYAN, True, anchor="mm")

    if total_votes == 0:
        _label(d, (640, 178), "NO VOTES CAST", 27, WHITE, True, anchor="mm")
        _label(d, (640, 217), "The actual imposter will be revealed next.", 16, MUTED, anchor="mm")
    elif stage == 0:
        _label(d, (640, 179), "THE VOTES ARE IN", 26, WHITE, True, anchor="mm")
        _label(d, (640, 218), "Revealing the podium from third place to first…", 16, MUTED, anchor="mm")
    elif stage == 1:
        _label(d, (640, 177), "THIRD PLACE REVEALED", 25, WHITE, True, anchor="mm")
    elif stage == 2:
        _label(d, (640, 177), "SECOND PLACE REVEALED", 25, WHITE, True, anchor="mm")
    else:
        tied = (len(results) > 1 and int(results[0].get("count", 0) or 0) ==
                int(results[1].get("count", 0) or 0))
        _label(d, (640, 177), "TIE FOR MOST VOTES" if tied else "MOST VOTES REVEALED",
               25, GOLD if tied else WHITE, True, anchor="mm")
    _imposter_footer(d, "PODIUM PLACES ARE REVEALED ONE AT A TIME")


def _draw_imposter_revealed(canvas, state):
    imposter_name = str(state.get("imposter") or "UNKNOWN").strip()
    word = str(state.get("word") or "").strip()
    category = str(state.get("category") or "").strip()
    results = list(state.get("results") or [])
    total_votes = sum(max(0, int(item.get("count", 0) or 0)) for item in results)
    ranked = sorted(results, key=lambda item: (-int(item.get("count", 0) or 0),
                                                str(item.get("name", "")).casefold()))
    top_names = []
    if ranked and int(ranked[0].get("count", 0) or 0) > 0:
        high = int(ranked[0].get("count", 0) or 0)
        top_names = [str(item.get("name", "")) for item in ranked
                     if int(item.get("count", 0) or 0) == high]
    caught = imposter_name in top_names
    d = _imposter_header(canvas, "IMPOSTER  /  FINAL REVEAL", "GAME REVEALED")
    d.rounded_rectangle((150, 111, 1130, 611), radius=28, fill=(10, 20, 43),
                        outline=(39, 67, 108), width=2)
    tag = "THE GROUP CAUGHT THEM" if caught and total_votes else "THE IMPOSTER ESCAPED" if total_votes else "NO VOTES WERE CAST"
    _pill(d, (380, 129, 900, 170), tag,
          (20, 91, 65) if caught and total_votes else (103, 42, 48) if total_votes else (43, 59, 79),
          WHITE, 14)
    d = ImageDraw.Draw(canvas)
    _label(d, (640, 211), "THE IMPOSTER WAS", 16, (255, 137, 150), True, anchor="mm")
    name_size = 36 if len(imposter_name) <= 14 else 28 if len(imposter_name) <= 22 else 22
    _label(d, (640, 247), imposter_name.upper(), name_size, WHITE, True, anchor="mm")
    _label(d, (640, 302), "THE CATEGORY", 13, (119, 190, 255), True, anchor="mm")
    category_size = 25 if len(category) <= 18 else 20
    _label(d, (640, 334), category.upper() if category else "UNKNOWN", category_size, CYAN, True, anchor="mm")
    _label(d, (640, 386), "THE SECRET WORD", 13, (119, 190, 255), True, anchor="mm")
    word_size = 28 if len(word) <= 18 else 22 if len(word) <= 28 else 18
    _label(d, (640, 421), word.upper() if word else "UNKNOWN", word_size, GOLD, True, anchor="mm")
    _label(d, (640, 486), "SELECT TO DEAL A NEW CATEGORY AND WORD", 14,
           (187, 205, 231), True, anchor="mm")
    _imposter_footer(d, "SELECT  NEW GAME     ·     BACK  END GAME")
def _draw_imposter_error(canvas, state):
    title = str(state.get("error_title") or "IMPOSTER ERROR")
    message = str(state.get("error_message") or "PiTV could not display this screen.")
    url = str(state.get("join_url") or "")
    d = _imposter_header(canvas, "IMPOSTER  /  DISPLAY STATUS", "ATTENTION")
    icon = pitv_artwork.load_icon("bug", size=62)
    canvas.paste(icon, (609, 169), icon)
    d = ImageDraw.Draw(canvas)
    _label(d, (640, 278), title.upper(), 27, WHITE, True, anchor="mm")
    _label(d, (640, 332), message, 17, (187, 205, 231), anchor="mm")
    if url:
        url_text = url if len(url) < 112 else url[:109] + "..."
        _label(d, (640, 385), url_text, 14, CYAN, anchor="mm")
    _imposter_footer(d, "CHECK THE PI TV LOG FOR DETAILS")


def _draw_overlay(canvas, overlay):
    if not overlay:
        return
    shade=Image.new("RGBA",(WIDTH,HEIGHT),(2,5,18,178))
    canvas=Image.alpha_composite(canvas.convert("RGBA"),shade)
    d=ImageDraw.Draw(canvas)
    d.rounded_rectangle((260,160,1020,550),radius=28,fill=(14,27,55,255),
                        outline=(70,177,255,255),width=3)
    if overlay.get("type")=="player_pin":
        _label(d,(640,222),"PLAYER PIN",18,(166,220,255),True,anchor="mm")
        _label(d,(640,277),overlay.get("name",""),24,WHITE,True,anchor="mm")
        code="  ".join(str(overlay.get("pin","")))
        _label(d,(640,362),code,54,WHITE,True,anchor="mm")
        _label(d,(640,438),f"Disappears in {overlay.get('seconds',1)} seconds",15,MUTED,anchor="mm")
    else:
        _label(d,(640,222),"CONNECT A DEVICE",18,(166,220,255),True,anchor="mm")
        _label(d,(640,277),"Enter this code on the PiTV guest page",18,WHITE,anchor="mm")
        _label(d,(640,362),"  ".join(str(overlay.get("code",""))),52,WHITE,True,anchor="mm")
        _label(d,(640,438),f"Code changes in {overlay.get('seconds',1)} seconds",15,MUTED,anchor="mm")
    return canvas.convert("RGB")


def _draw_clock_region(clock_text):
    # Redraw only the clock's small header rectangle using the same background.
    left, top, right, bottom = 1064, 0, 1240, 63
    background = _background().crop((left, top, right, bottom)).convert("RGB")
    d = ImageDraw.Draw(background)
    _label(d, (right-left-10, 29), clock_text, 15, MUTED, anchor="ra")
    d.line((0, 62, right-left, 62), fill=(35, 59, 94), width=1)
    pitv_framebuffer.display_image_region(
        background, (left, top, right, bottom), source_size=(WIDTH, HEIGHT)
    )


def render(state, log_fn=None):
    """Render only changed screens; the home carousel animates in its own region worker."""
    global _LAST_SIGNATURE, _LAST_PREPARED, _LAST_CLOCK_TEXT
    total_started = time.monotonic()
    now = _display_clock_text()
    state = dict(state)
    state["clock_text"] = now
    _sync_carousel_runtime(state)

    # Clock text and carousel position change independently and must not trigger
    # a complete screen render. Their respective workers update small regions.
    signature_state = dict(state)
    signature_state.pop("clock_text", None)
    signature_state.pop("carousel_index", None)
    signature = json.dumps(signature_state, sort_keys=True, ensure_ascii=True, default=str)
    if signature == _LAST_SIGNATURE:
        if now != _LAST_CLOCK_TEXT and str(state.get("view")) in _NAV_CLOCK_VIEWS:
            try:
                with _CAROUSEL_LOCK:
                    _draw_clock_region(now)
            except Exception as exc:
                if log_fn is not None:
                    try:
                        log_fn(f"V2 clock region update failed: {type(exc).__name__}: {exc}")
                    except Exception:
                        pass
            _LAST_CLOCK_TEXT = now
        return False

    draw_started = time.monotonic()
    canvas = _background().copy()
    view = state.get("view")
    if state.get("hard_locked") or view == "LOCKED":
        _draw_lock(canvas, state)
    elif view == "MENU":
        _nav(canvas, "HOME", now, state.get("home_focus") == "nav", state.get("nav_selected", 0))
        _draw_home(canvas, state)
    elif view == "DURATION_SELECT":
        _nav(canvas, "SCREENS", now)
        _draw_duration(canvas, state)
    elif view == "GAMES_MENU":
        _nav(canvas, "GAMES", now)
        _draw_games(canvas, state)
    elif view == "TTT_MODE":
        _nav(canvas, "GAMES", now)
        _draw_ttt(canvas, state)
    elif view == "IMPOSTER_COUNT":
        _nav(canvas, "GAMES", now)
        _draw_imposter_count(canvas, state)
    elif view == "KEYPAD":
        _nav(canvas, "GAMES", now)
        _draw_keypad(canvas, state)
    elif view == "SETTINGS":
        _draw_settings(canvas, state)
    elif view == "SETTINGS_AUTH":
        _draw_settings_auth(canvas, state)
    elif view == "SETTINGS_INPUT":
        _draw_settings_input(canvas, state)
    elif view == "SETTINGS_STATUS":
        _draw_settings(canvas, state)
    elif view == "SETTINGS_CONFIRM":
        _draw_settings(canvas, state)
    elif view == "SETTINGS_LOG":
        _draw_settings_log(canvas, state)
    elif view == "MUSIC":
        _draw_music(canvas, state)
    elif view == "IMPOSTER_WORD":
        _draw_imposter_word(canvas, state)
    elif view == "IMPOSTER_TURN":
        _draw_imposter_turn(canvas, state)
    elif view == "IMPOSTER_ROUND_COMPLETE":
        _draw_imposter_round_complete(canvas, state)
    elif view == "IMPOSTER_VOTE":
        _draw_imposter_vote(canvas, state)
    elif view == "IMPOSTER_RESULTS":
        _draw_imposter_results(canvas, state)
    elif view == "IMPOSTER_REVEALED":
        _draw_imposter_revealed(canvas, state)
    elif view == "IMPOSTER_ERROR":
        _draw_imposter_error(canvas, state)
    elif view in ("CLEAR", "FAKE_OFF"):
        canvas = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
    else:
        return False

    canvas = _draw_overlay(canvas, state.get("overlay")) or canvas
    draw_elapsed = time.monotonic() - draw_started

    scale_started = time.monotonic()
    canvas = _native_resolution(canvas)
    scale_elapsed = time.monotonic() - scale_started

    prepare_started = time.monotonic()
    if os.environ.get("PITV_SAVE_PNG", "").strip() == "1":
        os.makedirs(os.path.dirname(PNG_PATH), mode=0o700, exist_ok=True)
        canvas.save(PNG_PATH, format="PNG", optimize=False)
    prepared = pitv_framebuffer.prepare_image(canvas)
    prepare_elapsed = time.monotonic() - prepare_started

    display_started = time.monotonic()
    # Lock order matches the carousel worker (carousel lock, then framebuffer
    # lock). A stale animation frame cannot land over a newly rendered screen.
    with _CAROUSEL_LOCK:
        pitv_framebuffer.display_prepared_frame(prepared)
    display_elapsed = time.monotonic() - display_started

    _LAST_PREPARED = prepared
    _LAST_SIGNATURE = signature
    _LAST_CLOCK_TEXT = now
    total_elapsed = time.monotonic() - total_started
    if log_fn is not None:
        try:
            log_fn(
                "V2 graphic render {:.2f}s (draw {:.2f}s, scale {:.2f}s, "
                "frame preparation {:.2f}s [{}; {}bpp R{} G{} B{}], "
                "framebuffer write {:.2f}s)".format(
                    total_elapsed, draw_elapsed, scale_elapsed,
                    prepare_elapsed,
                    prepared.get("packing_method", "unknown"),
                    prepared.get("info", {}).get("bits_per_pixel", "?"),
                    prepared.get("info", {}).get("red", "?"),
                    prepared.get("info", {}).get("green", "?"),
                    prepared.get("info", {}).get("blue", "?"),
                    display_elapsed,
                )
            )
        except Exception:
            pass
    return True




def reset_cache():
    """Force the next render, useful after switching display modes."""
    global _LAST_SIGNATURE, _LAST_PREPARED, _LAST_CLOCK_TEXT
    _LAST_SIGNATURE = None
    _LAST_PREPARED = None
    _LAST_CLOCK_TEXT = None
    with _CAROUSEL_LOCK:
        _CAROUSEL["target"] = None
        _CAROUSEL["generation"] += 1
        _CAROUSEL_WAKE.set()
