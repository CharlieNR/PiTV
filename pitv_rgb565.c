#include <stdint.h>
#include <stddef.h>

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
