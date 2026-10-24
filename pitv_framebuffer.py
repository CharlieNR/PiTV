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
        # Convert the whole screen into RAM first. The previous implementation
        # wrote each scanline to /dev/fb0 as it converted it, making the image
        # visibly wipe down the TV. One completed buffer is copied to fbdev.
        frame = bytearray(info["memory_length"])
        row_base = xoffset * bytes_per_pixel
        for y in range(height):
            row = bytearray(line_length)
            for x in range(width):
                r, g, b = pixels[x, y]
                packed = red_lut[r] | green_lut[g] | blue_lut[b] | alpha
                start = row_base + x * bytes_per_pixel
                row[start:start + bytes_per_pixel] = packed.to_bytes(bytes_per_pixel, sys.byteorder)
            offset = (y + yoffset) * line_length
            frame[offset:offset + line_length] = row

        with mmap.mmap(
            fb.fileno(), info["memory_length"],
            flags=mmap.MAP_SHARED, prot=mmap.PROT_READ | mmap.PROT_WRITE,
        ) as mapped:
            mapped[:] = frame

    return {
        "device": device,
        "width": info["width"],
        "height": info["height"],
        "bits_per_pixel": info["bits_per_pixel"],
        "stride": info["line_length"],
        "source_width": source_width,
        "source_height": source_height,
    }



def display_png_region(
    path: str,
    device: str = "/dev/fb0",
    region: tuple[int, int, int, int] = (0, 615, 1280, 654),
) -> dict[str, Any]:
    """Update only a rectangle of an already displayed PNG.

    Region uses coordinates in the source PNG. The destination mapping
    mirrors display_png(), including aspect-ratio letterboxing and framebuffer
    offsets, so a status update does not redraw or move the rest of the image.
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"PNG file does not exist: {path}")
    if not os.access(device, os.R_OK | os.W_OK):
        raise PermissionError(f"Framebuffer is not readable and writable: {device}")

    with Image.open(path) as opened:
        image = opened.convert("RGB")
    source_width, source_height = image.size
    left, top, right, bottom = (int(value) for value in region)
    if not (0 <= left < right <= source_width and 0 <= top < bottom <= source_height):
        raise ValueError(
            f"Region {region!r} is outside the source image "
            f"{source_width}x{source_height}."
        )

    with open(device, "r+b", buffering=0) as fb:
        info = _framebuffer_info(fb.fileno())
        width, height = info["width"], info["height"]
        scale = min(width / source_width, height / source_height)
        target_width = max(1, min(width, round(source_width * scale)))
        target_height = max(1, min(height, round(source_height * scale)))
        image_left = (width - target_width) // 2
        image_top = (height - target_height) // 2
        x_scale = target_width / source_width
        y_scale = target_height / source_height

        dest_left = image_left + round(left * x_scale)
        dest_top = image_top + round(top * y_scale)
        dest_right = image_left + round(right * x_scale)
        dest_bottom = image_top + round(bottom * y_scale)
        dest_width = dest_right - dest_left
        dest_height = dest_bottom - dest_top
        if dest_width <= 0 or dest_height <= 0:
            raise RuntimeError(f"Framebuffer region has invalid size: {dest_width}x{dest_height}.")

        # Match the full-screen render exactly by resizing the whole source first,
        # then cropping the correspondingly scaled rectangle. Resizing the crop
        # independently can shift nearest-neighbour sampling at its edges.
        if image.size != (target_width, target_height):
            image = image.resize(
                (target_width, target_height), resample=Image.Resampling.NEAREST
            )
        band = image.crop((
            dest_left - image_left,
            dest_top - image_top,
            dest_right - image_left,
            dest_bottom - image_top,
        ))
        pixels = band.load()

        red, green, blue = info["red"], info["green"], info["blue"]
        transp = info["transparency"]
        bytes_per_pixel = info["bytes_per_pixel"]
        line_length = info["line_length"]
        xoffset, yoffset = info["xoffset"], info["yoffset"]
        alpha = ((1 << transp[1]) - 1) << transp[0] if transp[1] else 0
        red_lut = tuple(_component(v, *red, "red") for v in range(256))
        green_lut = tuple(_component(v, *green, "green") for v in range(256))
        blue_lut = tuple(_component(v, *blue, "blue") for v in range(256))

        # Stage the complete multi-row strip, retaining all bytes outside the
        # actual rectangle, then copy it back in one operation. This avoids a
        # line-by-line visual wipe when the player count changes.
        first_offset = (yoffset + dest_top) * line_length
        last_offset = first_offset + dest_height * line_length
        pixel_start = (xoffset + dest_left) * bytes_per_pixel
        pixel_end = pixel_start + dest_width * bytes_per_pixel
        with mmap.mmap(
            fb.fileno(), info["memory_length"],
            flags=mmap.MAP_SHARED, prot=mmap.PROT_READ | mmap.PROT_WRITE,
        ) as mapped:
            staged = bytearray(mapped[first_offset:last_offset])
            for y in range(dest_height):
                row = bytearray(dest_width * bytes_per_pixel)
                for x in range(dest_width):
                    r, g, b = pixels[x, y]
                    packed = red_lut[r] | green_lut[g] | blue_lut[b] | alpha
                    start = x * bytes_per_pixel
                    row[start:start + bytes_per_pixel] = packed.to_bytes(bytes_per_pixel, sys.byteorder)
                row_start = y * line_length + pixel_start
                staged[row_start:row_start + len(row)] = row
            mapped[first_offset:last_offset] = staged

    return {
        "device": device,
        "region": region,
        "width": dest_width,
        "height": dest_height,
        "left": dest_left,
        "top": dest_top,
        "bits_per_pixel": info["bits_per_pixel"],
    }


def clear_framebuffer(device: str = "/dev/fb0") -> None:
    """Clear the visible framebuffer to opaque black."""
    if not os.access(device, os.R_OK | os.W_OK):
        raise PermissionError(f"Framebuffer is not readable and writable: {device}")

    with open(device, "r+b", buffering=0) as fb:
        info = _framebuffer_info(fb.fileno())
        width, height = info["width"], info["height"]
        bytes_per_pixel = info["bytes_per_pixel"]
        line_length = info["line_length"]
        xoffset, yoffset = info["xoffset"], info["yoffset"]
        transp = info["transparency"]
        alpha = ((1 << transp[1]) - 1) << transp[0] if transp[1] else 0
        black_pixel = alpha.to_bytes(bytes_per_pixel, sys.byteorder)
        black_row = black_pixel * width

        frame = bytearray(info["memory_length"])
        for y in range(height):
            offset = (yoffset + y) * line_length + xoffset * bytes_per_pixel
            frame[offset:offset + len(black_row)] = black_row

        with mmap.mmap(
            fb.fileno(), info["memory_length"],
            flags=mmap.MAP_SHARED, prot=mmap.PROT_READ | mmap.PROT_WRITE,
        ) as mapped:
            mapped[:] = frame
