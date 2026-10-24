#!/usr/bin/env python3
"""Render PNGs directly to a Linux fbdev device without a separate viewer."""
from __future__ import annotations

import fcntl
import mmap
import os
import struct
import sys
from typing import Any

from PIL import Image

FBIOGET_VSCREENINFO = 0x4600
FBIOGET_FSCREENINFO = 0x4602
_VAR_INFO_SIZE = 160
_FIX_INFO_FORMAT = "@16sLIIIIHHHILIIHHH2x"
_FIX_INFO_SIZE = struct.calcsize(_FIX_INFO_FORMAT)


def _component(value: int, offset: int, length: int, msb_right: int, label: str) -> int:
    if length <= 0:
        return 0
    if msb_right:
        raise RuntimeError(f"Unsupported framebuffer {label} bitfield (msb_right={msb_right}).")
    return ((value * ((1 << length) - 1) + 127) // 255) << offset


def _framebuffer_info(fd: int) -> dict[str, Any]:
    var_buf = bytearray(_VAR_INFO_SIZE)
    fcntl.ioctl(fd, FBIOGET_VSCREENINFO, var_buf, True)
    var = struct.unpack("=40I", var_buf)

    xres, yres, xvirt, yvirt, xoff, yoff, bpp = var[:7]
    red = (var[8], var[9], var[10])
    green = (var[11], var[12], var[13])
    blue = (var[14], var[15], var[16])
    transp = (var[17], var[18], var[19])

    if xres <= 0 or yres <= 0 or xvirt < xres or yvirt < yres:
        raise RuntimeError(f"Invalid framebuffer geometry: visible={xres}x{yres}, virtual={xvirt}x{yvirt}.")
    if bpp not in (16, 24, 32):
        raise RuntimeError(f"Unsupported framebuffer depth: {bpp} bits per pixel (expected 16, 24 or 32).")
    for label, field in (("red", red), ("green", green), ("blue", blue), ("transparency", transp)):
        offset, length, msb_right = field
        if offset + length > bpp:
            raise RuntimeError(f"Invalid framebuffer {label} bitfield: offset={offset}, length={length}, bpp={bpp}.")
        if msb_right and length:
            raise RuntimeError(f"Unsupported framebuffer {label} bitfield with msb_right={msb_right}.")
    if not all(field[1] for field in (red, green, blue)):
        raise RuntimeError("Framebuffer does not expose usable red, green and blue pixel fields.")

    fix_buf = bytearray(_FIX_INFO_SIZE)
    fcntl.ioctl(fd, FBIOGET_FSCREENINFO, fix_buf, True)
    fix = struct.unpack(_FIX_INFO_FORMAT, fix_buf)
    line_length = fix[9]
    memory_length = fix[2]
    bytes_per_pixel = bpp // 8
    if line_length < xvirt * bytes_per_pixel:
        raise RuntimeError(
            f"Framebuffer stride is too short: stride={line_length}, "
            f"virtual width={xvirt}, pixel bytes={bytes_per_pixel}."
        )
    if memory_length <= 0:
        memory_length = line_length * yvirt
    required = (yoff + yres) * line_length
    if required > memory_length:
        raise RuntimeError(f"Framebuffer mapping is too small: need {required} bytes, device reports {memory_length}.")

    return {
        "width": xres,
        "height": yres,
        "virtual_width": xvirt,
        "virtual_height": yvirt,
        "xoffset": xoff,
        "yoffset": yoff,
        "bits_per_pixel": bpp,
        "bytes_per_pixel": bytes_per_pixel,
        "line_length": line_length,
        "memory_length": memory_length,
        "red": red,
        "green": green,
        "blue": blue,
        "transparency": transp,
    }


def display_png(path: str, device: str = "/dev/fb0") -> dict[str, Any]:
    """Scale and draw a PNG directly to the framebuffer, returning its geometry.

    No console-switching image viewer is launched. Pixel format and stride are
    obtained from fbdev ioctls, rather than assuming a particular Pi display mode.
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"PNG file does not exist: {path}")
    if not os.access(device, os.R_OK | os.W_OK):
        raise PermissionError(f"Framebuffer is not readable and writable: {device}")

    with open(device, "r+b", buffering=0) as fb:
        info = _framebuffer_info(fb.fileno())
        width, height = info["width"], info["height"]
        with Image.open(path) as opened:
            image = opened.convert("RGB")
        source_width, source_height = image.size
        scale = min(width / source_width, height / source_height)
        target_size = (
            max(1, min(width, round(source_width * scale))),
            max(1, min(height, round(source_height * scale))),
        )
        if image.size != target_size:
            image = image.resize(target_size, Image.Resampling.NEAREST)
        canvas = Image.new("RGB", (width, height), (0, 0, 0))
        canvas.paste(image, ((width - image.width) // 2, (height - image.height) // 2))
        pixels = canvas.load()

        red = info["red"]
        green = info["green"]
        blue = info["blue"]
        transp = info["transparency"]
        bytes_per_pixel = info["bytes_per_pixel"]
        line_length = info["line_length"]
        xoffset, yoffset = info["xoffset"], info["yoffset"]

        alpha = ((1 << transp[1]) - 1) << transp[0] if transp[1] else 0
        red_lut = tuple(_component(v, *red, "red") for v in range(256))
        green_lut = tuple(_component(v, *green, "green") for v in range(256))
        blue_lut = tuple(_component(v, *blue, "blue") for v in range(256))
        row = bytearray(line_length)
        with mmap.mmap(fb.fileno(), info["memory_length"], flags=mmap.MAP_SHARED,
                       prot=mmap.PROT_READ | mmap.PROT_WRITE) as mapped:
            for y in range(height):
                row[:] = b"\\x00" * line_length
                row_base = xoffset * bytes_per_pixel
                for x in range(width):
                    r, g, b = pixels[x, y]
                    packed = red_lut[r] | green_lut[g] | blue_lut[b] | alpha
                    start = row_base + x * bytes_per_pixel
                    row[start:start + bytes_per_pixel] = packed.to_bytes(bytes_per_pixel, sys.byteorder)
                offset = (y + yoffset) * line_length
                mapped[offset:offset + line_length] = row

    return {
        "device": device,
        "width": info["width"],
        "height": info["height"],
        "bits_per_pixel": info["bits_per_pixel"],
        "stride": info["line_length"],
        "source_width": source_width,
        "source_height": source_height,
    }
