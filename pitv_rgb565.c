#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>

/*
 * Convert 24-bit RGB pixels into the Linux fbdev RGB565 layout.
 * This is the format reported by Charlie's PiTV framebuffer:
 *   red:   offset 11, length 5
 *   green: offset 5,  length 6
 *   blue:  offset 0,  length 5
 *
 * One call packs the entire screen. Row stride and horizontal pixel offset
 * are explicit because fbdev rows are not guaranteed to be tightly packed.
 */
void pitv_pack_rgb565(const uint8_t *src, uint8_t *dst,
                      uint32_t width, uint32_t height,
                      size_t stride, size_t xoffset_bytes,
                      int big_endian) {
    for (uint32_t y = 0; y < height; ++y) {
        const uint8_t *s = src + ((size_t)y * width * 3u);
        uint8_t *d = dst + ((size_t)y * stride) + xoffset_bytes;
        for (uint32_t x = 0; x < width; ++x) {
            const uint16_t pixel =
                (uint16_t)(((uint16_t)(s[0] & 0xF8u) << 8) |
                           ((uint16_t)(s[1] & 0xFCu) << 3) |
                           ((uint16_t)s[2] >> 3));
            if (big_endian) {
                d[0] = (uint8_t)(pixel >> 8);
                d[1] = (uint8_t)pixel;
            } else {
                d[0] = (uint8_t)pixel;
                d[1] = (uint8_t)(pixel >> 8);
            }
            s += 3;
            d += 2;
        }
    }
}


/*
 * Scale an RGB888 image directly into an RGB565 framebuffer. This avoids
 * allocating a full-size Pillow image before packing it. The destination
 * buffer starts black, so letterbox/pillarbox areas need no additional work.
 * Returns 1 on success and 0 if the coordinate map cannot be allocated.
 */
int pitv_scale_pack_rgb565(const uint8_t *src, uint8_t *dst,
                           uint32_t source_width, uint32_t source_height,
                           uint32_t width, uint32_t height,
                           uint32_t content_width, uint32_t content_height,
                           uint32_t offset_x, uint32_t offset_y,
                           size_t stride, size_t xoffset_bytes,
                           int big_endian) {
    if (!src || !dst || source_width == 0 || source_height == 0 ||
        width == 0 || height == 0 || content_width == 0 ||
        content_height == 0 || content_width > width || content_height > height ||
        offset_x > width - content_width || offset_y > height - content_height) {
        return 0;
    }

    uint32_t *xmap = (uint32_t *)malloc((size_t)content_width * sizeof(uint32_t));
    if (!xmap) {
        return 0;
    }

    for (uint32_t x = 0; x < content_width; ++x) {
        uint64_t source_x =
            ((uint64_t)(2u * (uint64_t)x + 1u) * source_width) /
            (2u * (uint64_t)content_width);
        xmap[x] = source_x >= source_width ? source_width - 1u : (uint32_t)source_x;
    }

    for (uint32_t y = 0; y < content_height; ++y) {
        uint64_t source_y =
            ((uint64_t)(2u * (uint64_t)y + 1u) * source_height) /
            (2u * (uint64_t)content_height);
        if (source_y >= source_height) {
            source_y = source_height - 1u;
        }

        const uint8_t *s = src + ((size_t)source_y * source_width * 3u);
        uint8_t *d = dst + ((size_t)(y + offset_y) * stride) +
                     xoffset_bytes + ((size_t)offset_x * 2u);

        for (uint32_t x = 0; x < content_width; ++x) {
            const uint32_t sx = xmap[x];
            const uint8_t *pixel_src = s + ((size_t)sx * 3u);
            const uint16_t pixel =
                (uint16_t)(((uint16_t)(pixel_src[0] & 0xF8u) << 8) |
                           ((uint16_t)(pixel_src[1] & 0xFCu) << 3) |
                           ((uint16_t)pixel_src[2] >> 3));
            if (big_endian) {
                d[0] = (uint8_t)(pixel >> 8);
                d[1] = (uint8_t)pixel;
            } else {
                d[0] = (uint8_t)pixel;
                d[1] = (uint8_t)(pixel >> 8);
            }
            d += 2;
        }
    }

    free(xmap);
    return 1;
}
