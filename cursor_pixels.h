/* Custom cursor pixels shared by the Darwin image decoder and Wayland host.
 * Words are premultiplied 0xAARRGGBB; SDL_CreateColorCursor needs straight
 * alpha instead, while wl_shm ARGB8888 consumes the premultiplied words. */
#ifndef MACOBLOX_CURSOR_PIXELS_H
#define MACOBLOX_CURSOR_PIXELS_H

#ifdef __OBJC__
/* Returns a malloc-owned bitmap, or NULL for unsupported representations. */
unsigned int *macoblox_cursor_pixels_from_image(id image,
    unsigned long *width, unsigned long *height);
#endif

static inline unsigned int macoblox_cursor_straight_argb(unsigned int pixel) {
    unsigned int alpha = pixel >> 24;
    if (!alpha) return 0;
    if (alpha == 255) return pixel;
    unsigned int result = alpha << 24;
    for (unsigned int shift = 0; shift < 24; shift += 8) {
        unsigned int channel = (((pixel >> shift) & 255) * 255 + alpha / 2) / alpha;
        result |= (channel > 255 ? 255 : channel) << shift;
    }
    return result;
}

/* Interpolate premultiplied channels when an image representation has more
 * pixels than its logical cursor size. Transparent RGB cannot bleed into it. */
static inline unsigned int macoblox_cursor_sample_argb(const unsigned int *pixels,
    unsigned long width, unsigned long height, double x, double y) {
    x = x < 0 ? 0 : (x > width - 1 ? width - 1 : x);
    y = y < 0 ? 0 : (y > height - 1 ? height - 1 : y);
    unsigned long x0 = (unsigned long)x, y0 = (unsigned long)y;
    unsigned long x1 = x0 + 1 < width ? x0 + 1 : x0;
    unsigned long y1 = y0 + 1 < height ? y0 + 1 : y0;
    double fx = x - x0, fy = y - y0;
    unsigned int result = 0;
    for (unsigned int shift = 0; shift < 32; shift += 8) {
        double top = ((pixels[y0 * width + x0] >> shift) & 255) * (1 - fx) +
                     ((pixels[y0 * width + x1] >> shift) & 255) * fx;
        double bottom = ((pixels[y1 * width + x0] >> shift) & 255) * (1 - fx) +
                        ((pixels[y1 * width + x1] >> shift) & 255) * fx;
        result |= (unsigned int)(top * (1 - fy) + bottom * fy + 0.5) << shift;
    }
    return result;
}
#endif
