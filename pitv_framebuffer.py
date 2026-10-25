#!/usr/bin/env python3
"""Render PNGs directly to a Linux fbdev device without a separate viewer."""
from __future__ import annotations

import fcntl
import mmap
import os
import struct
import sys
from typing import Any

from PIL import Image, ImageMath

FBIOGET_VSCREENINFO = 0x4600
FBIOGET_FSCREENINFO = 0x4602
FBIOPAN_DISPLAY = 0x4606
_VAR_INFO_SIZE = 160
_FIX_INFO_FORMAT = "@16sLIIIIHHHILIIHHH2x"
_FIX_INFO_SIZE = struct.calcsize(_FIX_INFO_FORMAT)


def _component(value: int, offset: int, length: int, msb_right: int, label: str) -> int:
    if length <= 0:
        return 0
    if msb_right:
        raise RuntimeError(f"Unsupported framebuffer {label} bitfield (msb_right={msb_right}).")
    return ((value * ((1 << length) - 1) + 127) // 255) << offset


def _pack_with_numpy(canvas: Image.Image, info: dict[str, Any]):
    """Vectorised fallback for uncommon 16/24/32-bit framebuffer layouts."""
    bits = int(info["bits_per_pixel"])
    if bits not in (16, 24, 32):
        return None
    try:
        import numpy as np
    except ImportError:
        return None

    try:
        pixels = np.asarray(canvas, dtype=np.uint8)
        height, width = pixels.shape[:2]
        packed = np.zeros((height, width), dtype=np.uint32)
        for channel_index, field in enumerate((info["red"], info["green"], info["blue"])):
            offset, length, msb_right = field
            if msb_right:
                return None
            if length <= 0:
                continue
            maximum = (1 << length) - 1
            component = (
                pixels[:, :, channel_index].astype(np.uint32) * maximum + 127
            ) // 255
            packed |= component << offset

        trans_offset, trans_length, trans_msb_right = info["transparency"]
        if trans_msb_right:
            return None
        if trans_length > 0:
            packed |= ((1 << trans_length) - 1) << trans_offset

        if bits == 16:
            raw = packed.astype(np.uint16, copy=False).tobytes()
        elif bits == 32:
            raw = packed.astype(np.uint32, copy=False).tobytes()
        else:
            byte_view = packed.astype(np.uint32, copy=False).view(np.uint8).reshape(
                height, width, 4
            )
            # Remove the unused byte from each 24-bit pixel in native byte order.
            byte_view = byte_view[:, :, :3] if sys.byteorder == "little" else byte_view[:, :, 1:4]
            raw = np.ascontiguousarray(byte_view).tobytes()

        bytes_per_pixel = int(info["bytes_per_pixel"])
        row_bytes = width * bytes_per_pixel
        if len(raw) != row_bytes * height:
            return None
        line_length = int(info["line_length"])
        pixel_start = int(info["xoffset"]) * bytes_per_pixel
        frame = bytearray(line_length * height)
        if pixel_start == 0 and line_length == row_bytes:
            frame[:] = raw
        else:
            for y in range(height):
                src = y * row_bytes
                dst = y * line_length + pixel_start
                frame[dst:dst + row_bytes] = raw[src:src + row_bytes]
        return bytes(frame)
    except (AttributeError, OverflowError, TypeError, ValueError):
        return None


def _pack_with_pillow_math(canvas: Image.Image, info: dict[str, Any]):
    """Pack uncommon 16/32-bit framebuffer layouts in Pillow's native pixel engine."""
    bits = int(info["bits_per_pixel"])
    if bits not in (16, 32):
        return None

    try:
        red_image, green_image, blue_image = (
            channel.convert("I") for channel in canvas.split()
        )
        red, green, blue = info["red"], info["green"], info["blue"]
        transparency = info["transparency"]
        alpha = ((1 << transparency[1]) - 1) << transparency[0] if transparency[1] else 0

        if hasattr(ImageMath, "lambda_eval"):
            def component(source, field):
                offset, length, _msb_right = field
                if length <= 0:
                    return 0
                maximum = (1 << length) - 1
                return (((source * maximum + 127) / 255) << offset)

            def expression(values):
                value = component(values["R"], red)
                value = value | component(values["G"], green)
                value = value | component(values["B"], blue)
                if alpha:
                    value = value | alpha
                return value

            packed_image = ImageMath.lambda_eval(
                expression, R=red_image, G=green_image, B=blue_image
            )
        else:
            def term(name, field):
                offset, length, _msb_right = field
                if length <= 0:
                    return "0"
                maximum = (1 << length) - 1
                return "(((" + name + " * " + str(maximum) + " + 127) / 255) << " + str(offset) + ")"
            terms = [term("R", red), term("G", green), term("B", blue)]
            if alpha:
                terms.append(str(alpha))
            packed_image = ImageMath.eval(
                " | ".join("(" + item + ")" for item in terms),
                R=red_image, G=green_image, B=blue_image,
            )

        if packed_image.mode != "I":
            packed_image = packed_image.convert("I")
        if bits == 16:
            raw = packed_image.convert(
                "I;16L" if sys.byteorder == "little" else "I;16B"
            ).tobytes()
        else:
            # Pillow's mode-I storage uses native-endian 32-bit integers, which
            # matches the framebuffer's native byte order.
            raw = packed_image.tobytes()

        width, height = int(info["width"]), int(info["height"])
        bytes_per_pixel = int(info["bytes_per_pixel"])
        row_bytes = width * bytes_per_pixel
        if len(raw) != row_bytes * height:
            return None

        line_length = int(info["line_length"])
        pixel_start = int(info["xoffset"]) * bytes_per_pixel
        frame = bytearray(line_length * height)
        if pixel_start == 0 and line_length == row_bytes:
            frame[:] = raw
        else:
            for y in range(height):
                src = y * row_bytes
                dst = y * line_length + pixel_start
                frame[dst:dst + row_bytes] = raw[src:src + row_bytes]
        return bytes(frame)
    except (AttributeError, OverflowError, TypeError, ValueError):
        # Keep the established generic conversion as a compatibility fallback.
        return None



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


def prepare_png(path: str, device: str = "/dev/fb0") -> dict[str, Any]:
    """Load a PNG and prepare it for fbdev display."""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"PNG file does not exist: {path}")
    with Image.open(path) as opened:
        image = opened.convert("RGB")
    return prepare_image(image, device)


def prepare_image(image: Image.Image, device: str = "/dev/fb0") -> dict[str, Any]:
    """Prepare an existing Pillow image directly, avoiding PNG disk I/O."""
    if not os.access(device, os.R_OK | os.W_OK):
        raise PermissionError(f"Framebuffer is not readable and writable: {device}")
    with open(device, "r+b", buffering=0) as fb:
        info = _framebuffer_info(fb.fileno())

    width, height = info["width"], info["height"]
    image = image.convert("RGB")
    source_width, source_height = image.size
    scale = min(width / source_width, height / source_height)
    target_size = (
        max(1, min(width, round(source_width * scale))),
        max(1, min(height, round(source_height * scale))),
    )
    if image.size == (width, height):
        # The V2 renderer already prepares a full native-size canvas. Reuse it
        # instead of allocating and copying a second full-resolution image.
        canvas = image
    else:
        if image.size != target_size:
            image = image.resize(target_size, Image.Resampling.NEAREST)
        canvas = Image.new("RGB", (width, height), (0, 0, 0))
        canvas.paste(image, ((width - image.width) // 2, (height - image.height) // 2))
    red, green, blue = info["red"], info["green"], info["blue"]
    bytes_per_pixel = info["bytes_per_pixel"]
    line_length = info["line_length"]
    xoffset = info["xoffset"]
    transparency = info["transparency"]

    # Native Pillow raw encoders are much faster than converting every pixel
    # in Python. Handle both common byte orders for 24-bit RGB and 32-bit RGBA.
    standard_bgr = (
        sys.byteorder == "little"
        and red == (16, 8, 0)
        and green == (8, 8, 0)
        and blue == (0, 8, 0)
        and xoffset == 0
    )
    standard_rgb = (
        sys.byteorder == "little"
        and red == (0, 8, 0)
        and green == (8, 8, 0)
        and blue == (16, 8, 0)
        and xoffset == 0
    )
    packed = None
    packing_method = None
    if standard_bgr and bytes_per_pixel == 3 and transparency[1] == 0:
        packed = canvas.tobytes("raw", "BGR")
        packing_method = "pillow-raw-bgr24"
    elif (standard_bgr and bytes_per_pixel == 4
          and transparency == (24, 8, 0)):
        packed = canvas.convert("RGBA").tobytes("raw", "BGRA")
        packing_method = "pillow-raw-bgra32"
    elif (standard_bgr and bytes_per_pixel == 4
          and transparency[1] == 0):
        packed = canvas.tobytes("raw", "BGRX")
        packing_method = "pillow-raw-bgrx32"
    elif standard_rgb and bytes_per_pixel == 3 and transparency[1] == 0:
        packed = canvas.tobytes("raw", "RGB")
        packing_method = "pillow-raw-rgb24"
    elif (standard_rgb and bytes_per_pixel == 4
          and transparency == (24, 8, 0)):
        packed = canvas.convert("RGBA").tobytes("raw", "RGBA")
        packing_method = "pillow-raw-rgba32"
    elif (standard_rgb and bytes_per_pixel == 4
          and transparency[1] == 0):
        packed = canvas.tobytes("raw", "RGBX")
        packing_method = "pillow-raw-rgbx32"

    if packed is not None:
        row_bytes = width * bytes_per_pixel
        frame = bytearray(line_length * height)
        if line_length == row_bytes:
            frame[:] = packed
        else:
            for y in range(height):
                src = y * row_bytes
                dst = y * line_length
                frame[dst:dst + row_bytes] = packed[src:src + row_bytes]
        return {
            "frame": bytes(frame),
            "info": info,
            "source_width": source_width,
            "source_height": source_height,
            "packing_method": packing_method,
        }

    # If the format is unusual, NumPy can pack the entire image in compiled
    # array operations. It is optional; no extra PiTV dependency is required.
    packed_frame = _pack_with_numpy(canvas, info)
    if packed_frame is not None:
        return {
            "frame": packed_frame,
            "info": info,
            "source_width": source_width,
            "source_height": source_height,
            "packing_method": "numpy-vectorized",
        }

    packed_frame = _pack_with_pillow_math(canvas, info)
    if packed_frame is not None:
        return {
            "frame": packed_frame,
            "info": info,
            "source_width": source_width,
            "source_height": source_height,
            "packing_method": "pillow-imagemath",
        }

    rgb = canvas.tobytes()
    alpha = ((1 << transparency[1]) - 1) << transparency[0] if transparency[1] else 0
    red_lut = tuple(_component(v, *red, "red") for v in range(256))
    green_lut = tuple(_component(v, *green, "green") for v in range(256))
    blue_lut = tuple(_component(v, *blue, "blue") for v in range(256))

    # Store only one visible page. This buffer is built ahead of time when
    # requested by the Imposter setup thread; showing it is a single memory copy.
    frame = bytearray(line_length * height)
    pixel_start = xoffset * bytes_per_pixel
    colour_cache: dict[int, bytes] = {}
    for y in range(height):
        row_start = y * line_length + pixel_start
        rgb_start = y * width * 3
        for x in range(width):
            pos = rgb_start + x * 3
            key = (rgb[pos] << 16) | (rgb[pos + 1] << 8) | rgb[pos + 2]
            packed_bytes = colour_cache.get(key)
            if packed_bytes is None:
                packed = (red_lut[rgb[pos]] | green_lut[rgb[pos + 1]]
                          | blue_lut[rgb[pos + 2]] | alpha)
                packed_bytes = packed.to_bytes(bytes_per_pixel, sys.byteorder)
                colour_cache[key] = packed_bytes
            start = row_start + x * bytes_per_pixel
            frame[start:start + bytes_per_pixel] = packed_bytes

    return {
        "frame": bytes(frame),
        "info": info,
        "source_width": source_width,
        "source_height": source_height,
        "packing_method": "python-per-pixel",
    }


def _pan_to_page(fd: int, target_yoffset: int) -> None:
    """Ask fbdev to switch the visible scanout page, when the driver supports it."""
    var_buf = bytearray(_VAR_INFO_SIZE)
    fcntl.ioctl(fd, FBIOGET_VSCREENINFO, var_buf, True)
    var = list(struct.unpack("=40I", var_buf))
    var[5] = int(target_yoffset)  # yoffset in struct fb_var_screeninfo
    var_buf[:] = struct.pack("=40I", *var)
    fcntl.ioctl(fd, FBIOPAN_DISPLAY, var_buf, True)


def display_prepared_frame(prepared: dict[str, Any], device: str = "/dev/fb0") -> dict[str, Any]:
    """Display a prepared frame, using a page flip if fbdev exposes two pages."""
    if not os.access(device, os.R_OK | os.W_OK):
        raise PermissionError(f"Framebuffer is not readable and writable: {device}")
    captured = prepared.get("info")
    frame = prepared.get("frame")
    if not isinstance(captured, dict) or not isinstance(frame, bytes):
        raise ValueError("Prepared framebuffer image is malformed.")

    with open(device, "r+b", buffering=0) as fb:
        info = _framebuffer_info(fb.fileno())
        geometry_keys = (
            "width", "height", "virtual_width", "virtual_height",
            "bits_per_pixel", "bytes_per_pixel", "line_length", "memory_length",
            "xoffset", "red", "green", "blue", "transparency",
        )
        if any(info.get(key) != captured.get(key) for key in geometry_keys):
            raise RuntimeError("Framebuffer geometry changed after the image was prepared.")
        height, line_length = info["height"], info["line_length"]
        if len(frame) != height * line_length:
            raise RuntimeError("Prepared framebuffer image has an unexpected size.")

        with mmap.mmap(
            fb.fileno(), info["memory_length"],
            flags=mmap.MAP_SHARED, prot=mmap.PROT_READ | mmap.PROT_WRITE,
        ) as mapped:
            current_y = info["yoffset"]
            if (info["virtual_height"] >= 2 * height
                    and info["memory_length"] >= 2 * height * line_length):
                back_y = 0 if current_y >= height else height
                back_start = back_y * line_length
                mapped[back_start:back_start + len(frame)] = frame
                try:
                    _pan_to_page(fb.fileno(), back_y)
                    return {
                        "device": device, "width": info["width"], "height": info["height"],
                        "bits_per_pixel": info["bits_per_pixel"], "stride": line_length,
                        "source_width": prepared.get("source_width"),
                        "source_height": prepared.get("source_height"),
                        "page_flip": True,
                    }
                except OSError:
                    # Some fbdev drivers report virtual space but do not support
                    # panning. Fall back to the fast single-copy update.
                    pass

            visible_start = current_y * line_length
            mapped[visible_start:visible_start + len(frame)] = frame

    return {
        "device": device,
        "width": info["width"],
        "height": info["height"],
        "bits_per_pixel": info["bits_per_pixel"],
        "stride": info["line_length"],
        "source_width": prepared.get("source_width"),
        "source_height": prepared.get("source_height"),
        "page_flip": False,
    }


def display_png(path: str, device: str = "/dev/fb0") -> dict[str, Any]:
    """Prepare and display a PNG directly through fbdev, without an external viewer."""
    return display_prepared_frame(prepare_png(path, device), device)


def display_png_region(
    path: str,
    device: str = "/dev/fb0",
    region: tuple[int, int, int, int] = (0, 615, 1280, 654),
) -> dict[str, Any]:
    """Snap-update one rectangle, using page flipping when fbdev supports it.

    Region coordinates refer to the source PNG. A complete back page is prepared
    from the current screen and flipped in one operation where possible. Otherwise
    only the requested band is copied into the visible page.
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

        if image.size != (target_width, target_height):
            image = image.resize((target_width, target_height), resample=Image.Resampling.NEAREST)
        band = image.crop((
            dest_left - image_left, dest_top - image_top,
            dest_right - image_left, dest_bottom - image_top,
        ))
        pixels = band.tobytes()
        red, green, blue = info["red"], info["green"], info["blue"]
        alpha = ((1 << info["transparency"][1]) - 1) << info["transparency"][0] if info["transparency"][1] else 0
        bpp = info["bytes_per_pixel"]
        line_length = info["line_length"]
        pixel_start = (info["xoffset"] + dest_left) * bpp
        red_lut = tuple(_component(v, *red, "red") for v in range(256))
        green_lut = tuple(_component(v, *green, "green") for v in range(256))
        blue_lut = tuple(_component(v, *blue, "blue") for v in range(256))
        colour_cache: dict[int, bytes] = {}
        rows = []
        for y in range(dest_height):
            row = bytearray(dest_width * bpp)
            src_start = y * dest_width * 3
            for x in range(dest_width):
                pos = src_start + x * 3
                key = (pixels[pos] << 16) | (pixels[pos + 1] << 8) | pixels[pos + 2]
                packed_bytes = colour_cache.get(key)
                if packed_bytes is None:
                    packed = (red_lut[pixels[pos]] | green_lut[pixels[pos + 1]]
                              | blue_lut[pixels[pos + 2]] | alpha)
                    packed_bytes = packed.to_bytes(bpp, sys.byteorder)
                    colour_cache[key] = packed_bytes
                row_start = x * bpp
                row[row_start:row_start + bpp] = packed_bytes
            rows.append(bytes(row))

        with mmap.mmap(
            fb.fileno(), info["memory_length"],
            flags=mmap.MAP_SHARED, prot=mmap.PROT_READ | mmap.PROT_WRITE,
        ) as mapped:
            current_y = info["yoffset"]
            if (info["virtual_height"] >= 2 * height
                    and info["memory_length"] >= 2 * height * line_length):
                page_start = current_y * line_length
                staged = bytearray(mapped[page_start:page_start + height * line_length])
                for y, row in enumerate(rows):
                    row_start = (dest_top + y) * line_length + pixel_start
                    staged[row_start:row_start + len(row)] = row
                back_y = 0 if current_y >= height else height
                back_start = back_y * line_length
                mapped[back_start:back_start + len(staged)] = staged
                try:
                    _pan_to_page(fb.fileno(), back_y)
                    return {
                        "device": device, "region": region, "width": dest_width,
                        "height": dest_height, "left": dest_left, "top": dest_top,
                        "bits_per_pixel": info["bits_per_pixel"], "page_flip": True,
                    }
                except OSError:
                    pass

            first_offset = (current_y + dest_top) * line_length
            last_offset = first_offset + dest_height * line_length
            staged_band = bytearray(mapped[first_offset:last_offset])
            for y, row in enumerate(rows):
                row_start = y * line_length + pixel_start
                staged_band[row_start:row_start + len(row)] = row
            mapped[first_offset:last_offset] = staged_band

    return {
        "device": device, "region": region, "width": dest_width,
        "height": dest_height, "left": dest_left, "top": dest_top,
        "bits_per_pixel": info["bits_per_pixel"], "page_flip": False,
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
