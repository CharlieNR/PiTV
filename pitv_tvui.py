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
import time
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont, ImageOps
import pitv_framebuffer

WIDTH, HEIGHT = 1280, 720
PNG_PATH = "/tmp/pitv-tvui.png"
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


def _font(size: int, bold: bool = False):
    path = _FONT_BOLD if bold else _FONT_NORMAL
    try:
        return ImageFont.truetype(path, size=size)
    except OSError:
        return ImageFont.load_default()


def _mix(a, b, amount):
    return tuple(int(a[i] * (1.0 - amount) + b[i] * amount) for i in range(3))


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
                rng=random.Random(seed if False else 712+_)
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
        rect((.20,.14,.78,.78),(0,0,0,0), (255,229,179), 2, 12)
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
    elif key == "TICTACTOE":
        for i in (1,2):
            d.line((*pt(.24,.15+i*.21),*pt(.76,.15+i*.21)),fill=(145,231,255),width=5)
            d.line((*pt(.24+i*.17,.15),*pt(.24+i*.17,.78)),fill=(145,231,255),width=5)
        ellipse((.28,.20,.39,.35),(0,0,0,0),(255,197,99),5)
        d.line((*pt(.52,.39),*pt(.64,.51)),fill=(255,112,167),width=7)
        d.line((*pt(.64,.39),*pt(.52,.51)),fill=(255,112,167),width=7)
        ellipse((.61,.58,.72,.71),(0,0,0,0),(255,197,99),5)
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

    # A restrained outer glow keeps cover illustrations legible at TV distance.
    return im


def _cover(canvas, box, key, radius=16, image_size=(600, 280)):
    x0, y0, x1, y1 = map(int, box)
    width, height = max(1, x1-x0), max(1, y1-y0)
    art = _artwork(str(key), image_size[0], image_size[1])
    art = ImageOps.fit(art, (width, height), method=Image.Resampling.BILINEAR)
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, width-1, height-1), radius=radius, fill=255)
    canvas.paste(art, (x0, y0), mask)


def _label(draw, xy, text, size=18, colour=WHITE, bold=False, anchor=None):
    draw.text(xy, str(text), font=_font(size, bold), fill=colour, anchor=anchor)


def _pill(draw, box, text, fill=(29, 71, 126), fg=WHITE, size=13):
    draw.rounded_rectangle(box, radius=(box[3]-box[1])//2, fill=fill)
    draw.text(((box[0]+box[2])//2, (box[1]+box[3])//2), str(text),
              font=_font(size, True), fill=fg, anchor="mm")


def _lock_icon(draw, x, y, scale=1.0, colour=(222, 235, 255)):
    s = scale
    draw.arc((x+5*s, y, x+21*s, y+18*s), 180, 360, fill=colour, width=max(2,int(3*s)))
    draw.rounded_rectangle((x+2*s, y+10*s, x+24*s, y+29*s),
                           radius=max(2,int(3*s)), fill=colour)
    draw.ellipse((x+11*s,y+16*s,x+15*s,y+20*s), fill=(17,32,61))
    draw.rectangle((x+12*s,y+19*s,x+14*s,y+24*s), fill=(17,32,61))


def _nav(canvas, section, clock_text):
    d = ImageDraw.Draw(canvas)
    # Small brand mark, then icon-led navigation rather than a text-heavy header.
    d.ellipse((42, 18, 72, 48), fill=(27, 119, 220), outline=(117, 223, 255), width=2)
    d.arc((49, 25, 65, 41), 25, 325, fill=(245, 252, 255), width=3)
    _label(d, (82, 20), "PiTV", 23, WHITE, True)
    items = [
        ("HOME", "MENU", 192),
        ("SCREENS", "DURATION_SELECT", 294),
        ("GAMES", "GAMES_MENU", 414),
    ]
    for label, active_view, x in items:
        active = ((section == "HOME" and label == "HOME")
                  or (section == "SCREENS" and label == "SCREENS")
                  or (section == "GAMES" and label == "GAMES"))
        if active:
            d.rounded_rectangle((x-13, 15, x+89, 51), radius=16, fill=(25, 71, 130))
            d.rounded_rectangle((x-13, 46, x+89, 50), radius=2, fill=CYAN)
        # compact symbol before label
        ix, iy = x-1, 29
        if label == "HOME":
            d.line((ix,iy,ix+8,iy-7,ix+16,iy),fill=WHITE,width=2)
            d.rectangle((ix+3,iy,ix+13,iy+9),outline=WHITE,width=2)
        elif label == "SCREENS":
            d.rounded_rectangle((ix,iy-7,ix+17,iy+5),radius=2,outline=WHITE,width=2)
            d.line((ix+8,iy+5,ix+8,iy+9),fill=WHITE,width=2)
            d.line((ix+3,iy+9,ix+13,iy+9),fill=WHITE,width=2)
        else:
            d.rounded_rectangle((ix,iy-6,ix+18,iy+7),radius=5,outline=WHITE,width=2)
            d.ellipse((ix+4,iy-2,ix+7,iy+1),fill=WHITE)
            d.ellipse((ix+11,iy-2,ix+14,iy+1),fill=WHITE)
        _label(d, (x+24, 25), label, 14, WHITE if active else MUTED, active)
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


def _draw_home(canvas, state):
    d = ImageDraw.Draw(canvas)
    items = state["home_items"]
    selected = max(0, min(int(state.get("selected_art", 0)), len(items)-1))
    label, key, description = items[selected]
    _label(d, (54, 78), "PITV  /  YOUR SPACE", 14, MUTED, True)

    hero_box = (48, 105, 1232, 352)
    hero = ImageOps.fit(_artwork(key, 840, 360), (hero_box[2]-hero_box[0], hero_box[3]-hero_box[1]),
                        method=Image.Resampling.BILINEAR)
    hero = _with_hero_shade(hero)
    mask = Image.new("L", hero.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0,0,hero.width-1,hero.height-1),radius=22,fill=255)
    canvas.paste(hero, hero_box[:2], mask)
    d = ImageDraw.Draw(canvas)
    _label(d, (78, 226), label, 38, WHITE, True)
    _label(d, (80, 273), description, 18, (221, 231, 247))
    _pill(d, (80, 304, 225, 335), "SELECT TO OPEN", (29, 98, 166), WHITE, 12)
    # A slight blue outline makes it clear this is the featured, focused item.
    d.rounded_rectangle(hero_box, radius=22, outline=(62, 164, 255), width=2)

    _label(d, (55, 380), "EXPLORE", 16, MUTED, True)
    tile_w, tile_h, tile_gap = 216, 128, 18
    total = len(items)*tile_w+(len(items)-1)*tile_gap
    x0 = (WIDTH-total)//2
    for idx, (title, art_key, _desc) in enumerate(items):
        x = x0 + idx*(tile_w+tile_gap)
        y = 408
        selected_tile = idx == selected
        if selected_tile:
            d.rounded_rectangle((x-4,y-4,x+tile_w+4,y+tile_h+40),radius=18,fill=(26,72,125))
        _cover(canvas, (x,y,x+tile_w,y+tile_h), art_key, 13, (500,250))
        d = ImageDraw.Draw(canvas)
        _label(d, (x+tile_w//2, y+tile_h+13), title, 16,
               WHITE if selected_tile else (191,207,231), selected_tile, anchor="mm")
    d = ImageDraw.Draw(canvas)
    _label(d, (56, 636), "Use the arrows to browse", 14, MUTED)
    _label(d, (1224, 636), "SELECT  OPEN     BACK  HOME", 14, MUTED, True, anchor="ra")


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
    cover=ImageOps.fit(_artwork(key,840,360),(hero_box[2]-hero_box[0],hero_box[3]-hero_box[1]),
                       method=Image.Resampling.BILINEAR)
    cover=_with_hero_shade(cover,155)
    mask=Image.new("L",cover.size,0)
    ImageDraw.Draw(mask).rounded_rectangle((0,0,cover.width-1,cover.height-1),radius=22,fill=255)
    canvas.paste(cover,hero_box[:2],mask)
    d=ImageDraw.Draw(canvas)
    _label(d,(79,222),label,34,WHITE,True)
    _label(d,(81,265),"Choose how long it stays on screen",17,(221,231,247))
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
    selected=max(0,min(int(state.get("imposter_count_index",0)),len(options)-1))
    # Big, clear number cards with a simple people motif.
    card_w,card_h,gap=132,168,14
    total=len(options)*card_w+max(0,len(options)-1)*gap
    x0=(WIDTH-total)//2
    y=260
    for idx,count in enumerate(options):
        x=x0+idx*(card_w+gap); chosen=idx==selected
        d.rounded_rectangle((x,y,x+card_w,y+card_h),radius=18,
                            fill=(24,62,113) if chosen else PANEL,
                            outline=CYAN if chosen else (39,63,99),width=3 if chosen else 1)
        # silhouettes
        for person in range(min(count,5)):
            px=x+25+(person%3)*31
            py=y+31+(person//3)*27
            d.ellipse((px,py,px+18,py+18),fill=(95,218,255) if chosen else (107,147,192))
            d.rounded_rectangle((px-4,py+19,px+22,py+49),radius=8,
                                fill=(95,218,255) if chosen else (73,104,151))
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
    d=ImageDraw.Draw(canvas)
    # Large line-art lock and restrained text; this is a safety/administrative screen.
    d.ellipse((530,145,750,365),fill=(12,25,52),outline=(80,154,241),width=4)
    d.arc((582,190,698,304),180,360,fill=(180,215,255),width=13)
    d.rounded_rectangle((568,244,712,340),radius=18,fill=(52,108,177),outline=(201,231,255),width=3)
    d.ellipse((630,274,650,294),fill=(10,25,51))
    d.rounded_rectangle((636,290,644,318),radius=4,fill=(10,25,51))
    _label(d,(640,418),"SYSTEM LOCKED DOWN",30,WHITE,True,anchor="mm")
    _label(d,(640,469),"An administrator must authorise access from SSH.",17,MUTED,anchor="mm")
    _label(d,(640,508),"The TV remains locked until the emergency code is accepted.",15,MUTED,anchor="mm")


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


def render(state):
    """Render only when the visible UI state has changed; return True if drawn."""
    global _LAST_SIGNATURE, _LAST_PREPARED
    now = time.strftime("%H:%M")
    state = dict(state)
    state["clock_text"] = now
    signature = json.dumps(state, sort_keys=True, ensure_ascii=True, default=str)
    if signature == _LAST_SIGNATURE:
        return False

    canvas = _background()
    view = state.get("view")
    if state.get("hard_locked") or view == "LOCKED":
        _draw_lock(canvas, state)
    elif view == "MENU":
        _nav(canvas, "HOME", now)
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
    elif view in ("CLEAR", "FAKE_OFF"):
        canvas = Image.new("RGB", (WIDTH, HEIGHT), (0,0,0))
    else:
        return False

    canvas = _draw_overlay(canvas, state.get("overlay")) or canvas
    canvas.save(PNG_PATH, format="PNG", optimize=False)
    prepared = pitv_framebuffer.prepare_png(PNG_PATH)
    pitv_framebuffer.display_prepared_frame(prepared)
    _LAST_PREPARED = prepared
    _LAST_SIGNATURE = signature
    return True


def reset_cache():
    """Force the next render, useful after switching display modes."""
    global _LAST_SIGNATURE, _LAST_PREPARED
    _LAST_SIGNATURE = None
    _LAST_PREPARED = None
