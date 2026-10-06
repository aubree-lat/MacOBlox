/* The real X cursor must stay hidden for Xwayland's relative-pointer lock.
 * Display a snapshot in an input-transparent child of the game window so
 * CGAssociateMouseAndMouseCursorPosition(false) freezes a visible cursor,
 * as on macOS. All X calls run on the cursor worker, on its own connection.
 * Host Xlib/Xfixes are loaded through elfcalls, like raw_mouse.c. */
#ifdef __APPLE__
extern void *malloc(unsigned long);
extern void free(void *);
extern void *dlopen(const char *, int);
extern void *dlsym(void *, const char *);
#define RTLD_DEFAULT ((void *)-2)
#else
#include <stdlib.h>
#include <dlfcn.h>
#endif

typedef unsigned long XID;
typedef struct {
    void *visual;
    XID visualid;
    int screen, depth, klass;
    unsigned long red_mask, green_mask, blue_mask;
    int colormap_size, bits_per_rgb;
} CursorVisual;
typedef struct {
    XID background_pixmap, background_pixel, border_pixmap, border_pixel;
    int bit_gravity, win_gravity, backing_store;
    unsigned long backing_planes, backing_pixel;
    int save_under;
    long event_mask, do_not_propagate_mask;
    int override_redirect;
    XID colormap, cursor;
} CursorAttributes;
typedef struct {
    short x, y;
    unsigned short width, height, xhot, yhot;
    unsigned long serial, *pixels, atom;
    const char *name;
} CursorImage;
typedef struct { short x, y; unsigned short width, height; } CursorRectangle;

#define API(result, name, args) static result (*p_##name) args
API(void *, XOpenDisplay, (const char *));
API(int, XCloseDisplay, (void *));
typedef int (*CursorErrorHandler)(void *, void *);
API(CursorErrorHandler, XSetErrorHandler, (CursorErrorHandler));
API(int, XDefaultScreen, (void *));
API(XID, XDefaultRootWindow, (void *));
API(int, XMatchVisualInfo, (void *, int, int, int, CursorVisual *));
API(XID, XCreateColormap, (void *, XID, void *, int));
API(XID, XCreateWindow, (void *, XID, int, int, unsigned int, unsigned int, unsigned int,
                        int, unsigned int, void *, unsigned long, CursorAttributes *));
API(int, XDestroyWindow, (void *, XID));
API(int, XMoveResizeWindow, (void *, XID, int, int, unsigned int, unsigned int));
API(int, XMapRaised, (void *, XID));
API(int, XUnmapWindow, (void *, XID));
API(int, XTranslateCoordinates, (void *, XID, XID, int, int, int *, int *, XID *));
API(XID, XCreatePixmap, (void *, XID, unsigned int, unsigned int, unsigned int));
API(int, XFreePixmap, (void *, XID));
API(void *, XCreateGC, (void *, XID, unsigned long, void *));
API(int, XFreeGC, (void *, void *));
API(void *, XCreateImage, (void *, void *, unsigned int, int, int, char *, unsigned int,
                          unsigned int, int, int));
API(int, XPutPixel, (void *, int, int, unsigned long));
API(int, XDestroyImage, (void *));
API(int, XPutImage, (void *, XID, void *, void *, int, int, int, int, unsigned int, unsigned int));
API(int, XSetWindowBackgroundPixmap, (void *, XID, XID));
API(int, XClearWindow, (void *, XID));
API(int, XFlush, (void *));
API(int, XFree, (void *));
API(int, XFixesQueryExtension, (void *, int *, int *));
API(CursorImage *, XFixesGetCursorImage, (void *));
API(int, XShapeQueryVersion, (void *, int *, int *));
API(void, XShapeCombineRectangles, (void *, XID, int, int, int, CursorRectangle *, int, int, int));
#undef API

static void *display;
static CursorVisual visual;
static XID overlay, parent, colormap;
static int anchor_x, anchor_y;
static unsigned long last_serial;
static unsigned long lock_generation;
static CursorErrorHandler previous_error_handler;
static void *(*host_calloc)(unsigned long, unsigned long);
static void (*host_free)(void *);

static int overlay_error(void *connection, void *error) {
    /* The game can destroy its window while the worker handles an update.
     * Only errors on our private connection are ours to recover from. */
    if (connection == display) {
        overlay = parent = last_serial = 0;
        return 0;
    }
    return previous_error_handler ? previous_error_handler(connection, error) : 0;
}

static int resolve(void) {
    static int tried;
    if (tried) return display != 0;
    tried = 1;
    void *(*host_open)(const char *, int) = dlopen;
    void *(*host_symbol)(void *, const char *) = dlsym;
#ifdef __APPLE__
    struct elf_head {
        void *(*open)(const char *, int);
        int (*close)(void *);
        void *(*symbol)(void *, const char *);
    } **table = dlsym(RTLD_DEFAULT, "_elfcalls");
    if (!table || !*table) return 0;
    host_open = (*table)->open;
    host_symbol = (*table)->symbol;
#endif
    void *x11 = host_open("libX11.so.6", 2);
    void *fixes = host_open("libXfixes.so.3", 2);
    void *shape = host_open("libXext.so.6", 2);
    if (!x11 || !fixes || !shape) return 0;
    void *libc = host_open("libc.so.6", 2);
    if (!libc) return 0;
    host_calloc = host_symbol(libc, "calloc");
    host_free = host_symbol(libc, "free");
    if (!host_calloc || !host_free) return 0;
#define LOAD(lib, name) do { p_##name = host_symbol(lib, #name); if (!p_##name) return 0; } while (0)
    LOAD(x11, XOpenDisplay); LOAD(x11, XDefaultScreen); LOAD(x11, XDefaultRootWindow);
    LOAD(x11, XCloseDisplay); LOAD(x11, XSetErrorHandler);
    LOAD(x11, XMatchVisualInfo); LOAD(x11, XCreateColormap); LOAD(x11, XCreateWindow);
    LOAD(x11, XDestroyWindow); LOAD(x11, XMoveResizeWindow); LOAD(x11, XMapRaised);
    LOAD(x11, XUnmapWindow); LOAD(x11, XTranslateCoordinates); LOAD(x11, XCreatePixmap);
    LOAD(x11, XFreePixmap); LOAD(x11, XCreateGC); LOAD(x11, XFreeGC);
    LOAD(x11, XCreateImage); LOAD(x11, XPutPixel); LOAD(x11, XDestroyImage);
    LOAD(x11, XPutImage); LOAD(x11, XSetWindowBackgroundPixmap); LOAD(x11, XClearWindow);
    LOAD(x11, XFlush); LOAD(x11, XFree);
    LOAD(fixes, XFixesQueryExtension); LOAD(fixes, XFixesGetCursorImage);
    LOAD(shape, XShapeQueryVersion); LOAD(shape, XShapeCombineRectangles);
#undef LOAD
    void *connection = p_XOpenDisplay(0);
    int major, minor, event, error;
    if (!connection || !p_XFixesQueryExtension(connection, &event, &error) ||
        !p_XShapeQueryVersion(connection, &major, &minor) || major * 10 + minor < 11 ||
        !p_XMatchVisualInfo(connection, p_XDefaultScreen(connection), 32, 4 /* TrueColor */, &visual)) {
        if (connection) p_XCloseDisplay(connection);
        return 0;
    }
    display = connection;
    previous_error_handler = p_XSetErrorHandler(overlay_error);
    colormap = p_XCreateColormap(display, p_XDefaultRootWindow(display), visual.visual, 0);
    return colormap != 0;
}

/* Called before XFixesHideCursor on lock entry, and again on cursor changes.
 * A transparent cursor stays transparent; a game's custom cursor is kept.
 * Production calls pass the native position sampled at capture entry. The
 * worker may run after motion/recentering, so cursor->x/y are not that point. */
static int cursor_overlay_update(int locked, XID game_window, int visible,
                                 int saved_position, int saved_x, int saved_y,
                                 unsigned long generation) {
    if (!locked || !game_window) {
        if (display && overlay) {
            p_XUnmapWindow(display, overlay);
            p_XFlush(display);
        }
        parent = 0;
        last_serial = 0;
        lock_generation = 0;
        return 1;
    }
    if (!resolve()) return 0;
    if (!visible) {
        if (overlay) p_XUnmapWindow(display, overlay);
        last_serial = 0;
        p_XFlush(display);
        return 1;
    }
    CursorImage *cursor = p_XFixesGetCursorImage(display);
    if (!cursor) return 0;
    int anchor_changed = saved_position &&
        (lock_generation != generation || anchor_x != saved_x || anchor_y != saved_y);
    if (parent == game_window && last_serial == cursor->serial && !anchor_changed) {
        p_XFree(cursor);
        return 1;
    }
    unsigned int width = cursor->width, height = cursor->height;
    if (!width || !height || width > 256 || height > 256) {
        p_XFree(cursor);
        return 0;
    }
    if (saved_position) {
        anchor_x = saved_x;
        anchor_y = saved_y;
        lock_generation = generation;
    } else if (parent != game_window) {
        XID child;
        if (!p_XTranslateCoordinates(display, p_XDefaultRootWindow(display), game_window,
                                     cursor->x, cursor->y, &anchor_x, &anchor_y, &child)) {
            p_XFree(cursor);
            return 0;
        }
    }
    if (parent != game_window) {
        if (overlay) p_XDestroyWindow(display, overlay);
        CursorAttributes attributes = {0};
        attributes.colormap = colormap;
        overlay = p_XCreateWindow(display, game_window, 0, 0, width, height, 0, 32,
                                  1 /* InputOutput */, visual.visual,
                                  (1UL << 1) | (1UL << 3) | (1UL << 13), &attributes);
        parent = game_window;
        /* No input interception, including at the cursor's opaque pixels. */
        p_XShapeCombineRectangles(display, overlay, 2 /* ShapeInput */, 0, 0, 0, 0, 0, 0);
    }
    CursorRectangle *rects = malloc(width * height * sizeof *rects);
    /* XDestroyImage must free a host allocation, not Darling's malloc. Let
     * Xlib allocate its image structure and populate pixels via XPutPixel;
     * the data uses the host allocator resolved below. */
    char *pixels = host_calloc ? host_calloc(width * height, 4) : 0;
    void *image = pixels ? p_XCreateImage(display, visual.visual, 32, 2 /* ZPixmap */, 0,
                                         pixels, width, height, 32, 0) : 0;
    if (!rects || !image) {
        if (image) p_XDestroyImage(image);
        else if (pixels) host_free(pixels);
        free(rects);
        p_XFree(cursor);
        return 0;
    }
    int count = 0;
    for (unsigned int y = 0; y < height; y++) {
        for (unsigned int x = 0; x < width; x++)
            p_XPutPixel(image, x, y, cursor->pixels[y * width + x]);
        for (unsigned int x = 0; x < width;) {
            if (!(cursor->pixels[y * width + x] >> 24)) { x++; continue; }
            unsigned int start = x++;
            while (x < width && (cursor->pixels[y * width + x] >> 24)) x++;
            rects[count++] = (CursorRectangle){start, y, x - start, 1};
        }
    }
    XID pixmap = p_XCreatePixmap(display, overlay, width, height, 32);
    void *gc = p_XCreateGC(display, pixmap, 0, 0);
    p_XPutImage(display, pixmap, gc, image, 0, 0, 0, 0, width, height);
    p_XSetWindowBackgroundPixmap(display, overlay, pixmap);
    p_XMoveResizeWindow(display, overlay, anchor_x - cursor->xhot, anchor_y - cursor->yhot, width, height);
    /* Bounding shape also supports X servers without a compositor. */
    p_XShapeCombineRectangles(display, overlay, 0 /* ShapeBounding */, 0, 0, rects, count, 0, 0);
    p_XClearWindow(display, overlay);
    if (count) p_XMapRaised(display, overlay);
    else p_XUnmapWindow(display, overlay);
    p_XFreeGC(display, gc);
    p_XFreePixmap(display, pixmap);
    p_XDestroyImage(image);
    last_serial = cursor->serial;
    p_XFree(cursor);
    free(rects);
    p_XFlush(display);
    return 1;
}

int macoblox_cursor_overlay_update_at(int locked, XID game_window, int visible,
                                      int saved_x, int saved_y, unsigned long generation) {
    return cursor_overlay_update(locked, game_window, visible, 1, saved_x, saved_y, generation);
}

/* Keep the standalone helper interface for callers without a capture
 * snapshot. The game shim always uses update_at with the saved position. */
int macoblox_cursor_overlay_update(int locked, XID game_window, int visible) {
    return cursor_overlay_update(locked, game_window, visible, 0, 0, 0, 0);
}
