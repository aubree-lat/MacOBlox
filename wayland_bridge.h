/* Private, versioned ABI between the Darwin shim and the Linux window helper. */
#ifndef MACOBLOX_WAYLAND_BRIDGE_H
#define MACOBLOX_WAYLAND_BRIDGE_H
#define MACOBLOX_WAYLAND_ABI 3
enum { MW_MOTION=1, MW_DOWN, MW_UP, MW_SCROLL, MW_KEY_DOWN, MW_KEY_UP,
       MW_TEXT, MW_RESIZE, MW_FOCUS, MW_BLUR, MW_CLOSE, MW_CAPTURE };
enum { MW_SHOW=1, MW_HIDE, MW_TITLE, MW_RESIZE_WINDOW, MW_FULLSCREEN,
       MW_LOCK, MW_WARP, MW_MINIMIZE, MW_CURSOR_VISIBLE, MW_DESTROY };
struct macoblox_wayland_event {
    unsigned int type, window, button, key, modifiers, repeat, clicks;
    double x, y, dx, dy;
    char text[256];
};
struct macoblox_wayland_api {
    unsigned int version;
    void *(*display)(void);
    unsigned int (*create)(int, int);
    void *(*surface)(unsigned int);
    void (*action)(unsigned int, int, double, double, const char *);
    int (*poll)(struct macoblox_wayland_event *);
    void (*screen)(int *, int *, double *);
    /* Custom pixels: premultiplied 0xAARRGGBB words; pitch is bytes per row.
     * The helper copies them before returning. Dimensions/hot spot are in
     * logical surface coordinates, matching the existing 1x render buffers. */
    void (*cursor)(const void *, int, int, int, int, int, const char *);
    const char *(*clipboard)(const char *);
    const char *(*error)(void);
    /* Each AppKit GL view owns a distinct native drawable. Frames use the
     * parent's top-left, logical coordinates; render buffers remain 1x. */
    unsigned int (*create_subwindow)(unsigned int, int, int, int, int);
    void *(*subwindow_surface)(unsigned int);
    void (*subwindow_frame)(unsigned int, int, int, int, int);
    void (*subwindow_visible)(unsigned int, int);
    void (*destroy_subwindow)(unsigned int);
};
#endif
