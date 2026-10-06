#include "shim_lock.h"
#include "graphics_context.h"
/* OpenGL Core Profile for Roblox's context.
 *
 * Roblox asks NSOpenGLPixelFormat for NSOpenGLPFAOpenGLProfile = 3.2 Core
 * (as macOS requires for modern OpenGL). Darling's pixel format drops that
 * attribute and CGL creates every context through eglCreateContext without
 * attributes, i.e. a Compatibility Profile context. In that context Roblox
 * reported no multisample-texture support ("Caps: Texture: ... MSAA 0") and
 * never enabled MSAA, whatever the graphics level or fast flags.
 *
 * The pixel format hook records the requested profile per pixel format; the
 * NSOpenGLContext init hook marks the thread; eglCreateContext then adds the
 * Core Profile attributes for that one context. Darling's own contexts (its
 * layer and window compositing use fixed-function GL) stay Compatibility. */

extern char *getenv(const char *);
extern void *eglCreateContext(void *, void *, void *, const int *);
extern int snprintf(char *, unsigned long, const char *, ...);
extern long write(int, const void *, unsigned long);

#define DYLD_INTERPOSE(_replacement, _replacee) \
    __attribute__((used)) static struct { const void *replacement; const void *replacee; } \
    _interpose_##_replacee __attribute__((section("__DATA,__interpose"))) = \
        {(const void *)(unsigned long)&_replacement, (const void *)(unsigned long)&_replacee};

#define NSOpenGLPFAOpenGLProfile 99
#define EGL_NONE 0x3038
#define EGL_CONTEXT_MAJOR_VERSION 0x3098
#define EGL_CONTEXT_MINOR_VERSION 0x30FB
#define EGL_CONTEXT_OPENGL_PROFILE_MASK 0x30FD
#define EGL_CONTEXT_OPENGL_CORE_PROFILE_BIT 0x1

static struct { void *format; unsigned int profile; } formats[32];
static volatile unsigned int formats_lock;
static __thread unsigned int wanted_profile;

static int core_enabled(void) {
    const char *text = getenv("MACOBLOX_GL_COMPAT");
    return !(text && text[0] == '1');
}

/* Attribute lists alternate between flags and "attribute, value" pairs;
 * these are the NSOpenGLPixelFormat attributes that take a value. */
static int takes_value(unsigned int attribute) {
    switch (attribute) {
    case 7: case 8: case 11: case 12: case 13: case 14: case 55: case 56:
    case 70: case 84: case 99: case 128:
        return 1;
    }
    return 0;
}

void macoblox_note_pixel_format(void *format, const unsigned int *attributes) {
    unsigned int profile = 0;
    for (int index = 0; attributes && index < 64 && attributes[index]; index++) {
        if (attributes[index] == NSOpenGLPFAOpenGLProfile)
            profile = attributes[index + 1];
        if (takes_value(attributes[index]))
            index++;
    }
    macoblox_lock(&formats_lock);
    int slot = 0;
    for (int i = 0; i < 32; i++) {
        if (formats[i].format == format || !formats[i].format) { slot = i; break; }
        slot = i;
    }
    formats[slot].format = format;
    formats[slot].profile = profile;
    macoblox_unlock(&formats_lock);
}

void macoblox_prepare_context(void *format) {
    unsigned int profile = 0;
    macoblox_lock(&formats_lock);
    for (int i = 0; i < 32; i++)
        if (format && formats[i].format == format)
            profile = formats[i].profile;
    macoblox_unlock(&formats_lock);
    wanted_profile = core_enabled() ? profile : 0;
}

void macoblox_finish_context(void) {
    wanted_profile = 0;
}

static void *macoblox_eglCreateContext(void *display, void *config, void *share, const int *attributes) {
    if (!macoblox_bind_desktop_gl())
        return 0;
    unsigned int profile = wanted_profile;
    if (profile >= 0x3200 && !attributes) {
        wanted_profile = 0;
        /* macOS answers a 3.2 Core request with 4.1 Core, and Roblox's Mac
         * shaders expect that. Mesa gives 4.6 for either request, NVIDIA
         * exactly the version asked for (3.2 meant GLSL 1.50). */
        static const int versions[][2] = {{4, 1}, {3, 2}};
        for (int i = 0; i < 2; i++) {
            int core[] = {EGL_CONTEXT_MAJOR_VERSION, versions[i][0], EGL_CONTEXT_MINOR_VERSION, versions[i][1],
                          EGL_CONTEXT_OPENGL_PROFILE_MASK, EGL_CONTEXT_OPENGL_CORE_PROFILE_BIT, EGL_NONE};
            void *context = eglCreateContext(display, config, share, core);
            if (context) {
                macoblox_register_egl_context(context);
                char line[120];
                int length = snprintf(line, sizeof line, "[MacOBlox GL] Core Profile %d.%d context for Roblox: created\n",
                                      versions[i][0], versions[i][1]);
                if (length > 0) write(2, line, (unsigned long)length);
                return context;
            }
        }
        write(2, "[MacOBlox GL] Core Profile context failed, using Compatibility\n", 63);
    }
    void *context = eglCreateContext(display, config, share, attributes);
    if (context)
        macoblox_register_egl_context(context);
    return context;
}
DYLD_INTERPOSE(macoblox_eglCreateContext, eglCreateContext)

/* Roblox turns off multisampled textures (and with them MSAA) when
 * GL_RENDERER contains "AMD", a workaround for Apple's AMD drivers since
 * macOS 10.12. Mesa's radeonsi does not have that bug, so Roblox gets the
 * renderer name without the vendor word. MACOBLOX_GL_COMPAT=1 keeps it. */
extern const unsigned char *glGetString(unsigned int);

extern char *strstr(const char *, const char *);
extern void *eglGetCurrentContext(void);
extern void *malloc(unsigned long);
extern void free(void *);

struct renderer_name {
    struct renderer_name *next;
    void *context;
    const unsigned char *original;
    char text[];
};
static struct renderer_name *renderer_names;
static volatile unsigned int renderer_lock;

void macoblox_forget_gl_context(void *context) {
    macoblox_lock(&renderer_lock);
    struct renderer_name **link = &renderer_names;
    while (*link) {
        struct renderer_name *name = *link;
        if (name->context == context) {
            *link = name->next;
            free(name);
        } else {
            link = &name->next;
        }
    }
    macoblox_unlock(&renderer_lock);
}

static const unsigned char *macoblox_glGetString(unsigned int name) {
    static int reported;
    const unsigned char *value = glGetString(name);
    if (name == 0x1F01 && value && __sync_bool_compare_and_swap(&reported, 0, 1)) {
        /* Once in the log: which driver draws. llvmpipe means no GPU driver
         * reached Darling, the game then crawls at a few frames per second. */
        const unsigned char *version = glGetString(0x1F02);
        const char *text = (const char *)value;
        int software = strstr(text, "llvmpipe") || strstr(text, "softpipe") || strstr(text, "SWR");
        char line[512];
        int length = snprintf(line, sizeof line, "[MacOBlox GL] renderer: %s (OpenGL %s)%s\n", text,
                              version ? (const char *)version : "?",
                              software ? " -- SOFTWARE RENDERING: the GPU driver is not in use" : "");
        if (length > 0) write(2, line, (unsigned long)length < sizeof line ? (unsigned long)length : sizeof line - 1);
    }
    if (name != 0x1F01 || !value || !core_enabled())
        return value;
    const char *text = (const char *)value;
    if (!strstr(text, "AMD"))
        return value;
    void *context = eglGetCurrentContext();
    macoblox_lock(&renderer_lock);
    for (struct renderer_name *cached = renderer_names; cached; cached = cached->next)
        if (cached->context == context && cached->original == value) {
            macoblox_unlock(&renderer_lock);
            return (const unsigned char *)cached->text;
        }
    unsigned long length = 0;
    while (text[length]) length++;
    struct renderer_name *cached = malloc(sizeof(*cached) + length + 1);
    if (!cached) {
        macoblox_unlock(&renderer_lock);
        return value;
    }
    unsigned long out = 0;
    for (unsigned long in = 0; text[in]; in++) {
        if (text[in] == 'A' && text[in + 1] == 'M' && text[in + 2] == 'D') {
            in += text[in + 3] == ' ' ? 3 : 2;
            continue;
        }
        cached->text[out++] = text[in];
    }
    cached->text[out] = 0;
    cached->context = context;
    cached->original = value;
    cached->next = renderer_names;
    renderer_names = cached;
    macoblox_unlock(&renderer_lock);
    return (const unsigned char *)cached->text;
}
DYLD_INTERPOSE(macoblox_glGetString, glGetString)

/* EGL config for Darling's windows.
 *
 * Darling's CGL picks its EGL config with only "red, green, blue >= 1" and
 * uses the first match for every window surface. Mesa's first config fits
 * any X window, NVIDIA's does not: eglCreateWindowSurface failed, the game
 * window had no surface and stayed black (RTX 3050, driver 615). The chosen
 * config is replaced by one whose native visual is the X screen's default
 * visual, which Darling's windows use; if a window still has another
 * visual, its surface is created with a config for that visual. */
extern unsigned int eglChooseConfig(void *, const int *, void **, int, int *);
extern unsigned int eglGetConfigAttrib(void *, void *, int, int *);
extern void *eglCreateWindowSurface(void *, void *, unsigned long, const int *);
extern int eglGetError(void);
extern int macoblox_raw_x_visuals(unsigned int, unsigned int *, unsigned int *);

#define EGL_BLUE_SIZE 0x3022
#define EGL_GREEN_SIZE 0x3023
#define EGL_RED_SIZE 0x3024
#define EGL_NATIVE_VISUAL_ID 0x302E
#define EGL_SURFACE_TYPE 0x3033
#define EGL_RENDERABLE_TYPE 0x3040
#define EGL_WINDOW_BIT 0x0004
#define EGL_OPENGL_BIT 0x0008

static int darling_default_attributes(const int *list) {
    static const int darling[] = {EGL_RED_SIZE, 1, EGL_GREEN_SIZE, 1, EGL_BLUE_SIZE, 1, EGL_NONE};
    for (int i = 0; list && i < 7; i++)
        if (list[i] != darling[i])
            return 0;
    return list != 0;
}

static int config_visual(void *display, void *config) {
    int visual = 0;
    eglGetConfigAttrib(display, config, EGL_NATIVE_VISUAL_ID, &visual);
    return visual;
}

/* A window-capable desktop OpenGL config for this X visual, or 0. */
static void *config_for_visual(void *display, unsigned int visual) {
    static const int wanted[] = {EGL_SURFACE_TYPE, EGL_WINDOW_BIT, EGL_RENDERABLE_TYPE, EGL_OPENGL_BIT,
                                 EGL_RED_SIZE, 8, EGL_GREEN_SIZE, 8, EGL_BLUE_SIZE, 8, EGL_NONE};
    void *configs[256];
    int count = 0;
    if (!eglChooseConfig(display, wanted, configs, 256, &count))
        return 0;
    for (int i = 0; i < count; i++)
        if ((unsigned int)config_visual(display, configs[i]) == visual)
            return configs[i];
    return 0;
}

static void log_line(const char *text) {
    int length = 0;
    while (text[length]) length++;
    write(2, text, (unsigned long)length);
}

static unsigned int macoblox_eglChooseConfig(void *display, const int *attributes, void **configs,
                                             int size, int *count) {
    extern int macoblox_wayland_enabled(void);
    if (macoblox_wayland_enabled() && darling_default_attributes(attributes)) {
        static const int wanted[] = {EGL_SURFACE_TYPE, EGL_WINDOW_BIT,
            EGL_RENDERABLE_TYPE, EGL_OPENGL_BIT, EGL_RED_SIZE, 8,
            EGL_GREEN_SIZE, 8, EGL_BLUE_SIZE, 8, EGL_NONE};
        return eglChooseConfig(display, wanted, configs, size, count);
    }
    unsigned int ok = eglChooseConfig(display, attributes, configs, size, count);
    if (!ok || !configs || size < 1 || !count || *count < 1 || !darling_default_attributes(attributes))
        return ok;
    unsigned int root_visual = 0;
    if (!macoblox_raw_x_visuals(0, &root_visual, 0) || !root_visual)
        return ok;
    int chosen = config_visual(display, configs[0]);
    if ((unsigned int)chosen == root_visual)
        return ok;
    void *better = config_for_visual(display, root_visual);
    char line[160];
    snprintf(line, sizeof line, "[MacOBlox GL] EGL config visual 0x%x, screen visual 0x%x: %s\n",
             chosen, root_visual, better ? "using a config for the screen visual" : "no config for it");
    log_line(line);
    if (better)
        configs[0] = better;
    return ok;
}
DYLD_INTERPOSE(macoblox_eglChooseConfig, eglChooseConfig)

static void forget_configured_surface(void *surface);

static void *macoblox_eglCreateWindowSurface(void *display, void *config, unsigned long window,
                                             const int *attributes) {
    void *surface = eglCreateWindowSurface(display, config, window, attributes);
    if (surface) {
        forget_configured_surface(surface); /* a new surface at an old address */
        const char *trace = getenv("MACOBLOX_TRACE_CGL");
        if (trace && trace[0] == '1') {
            char line[160];
            snprintf(line, sizeof line, "[MacOBlox GL] eglCreateWindowSurface window 0x%lx -> surface %p\n", window, surface);
            log_line(line);
        }
        return surface;
    }
    int error = macoblox_capture_egl_error();
    extern int macoblox_wayland_enabled(void);
    if (macoblox_wayland_enabled()) {
        char line[160];
        snprintf(line, sizeof line, "[MacOBlox Wayland] EGL surface failed: error 0x%x, native %p\n", error, (void *)window);
        log_line(line);
        return 0;
    }
    unsigned int root_visual = 0, window_visual = 0;
    int attributes_available = macoblox_raw_x_visuals((unsigned int)window, &root_visual, &window_visual);
    void *matching = window_visual ? config_for_visual(display, window_visual) : 0;
    const char *retry_status;
    int retry_error = 0;
    if (matching && matching != config) {
        surface = eglCreateWindowSurface(display, matching, window, attributes);
        retry_status = surface ? "with the window's visual succeeded" : "with the window's visual failed";
        /* The original error remains queued by capture_egl_error(). Calls
         * within the shim bypass its eglGetError interposition, so reading
         * the failed retry's driver error does not replace that queued error. */
        if (!surface)
            retry_error = eglGetError();
    } else if (!attributes_available || !window_visual) {
        retry_status = "not attempted (native window attributes unavailable)";
    } else if (!matching) {
        retry_status = "not attempted (no EGL config for the window's visual)";
    } else {
        retry_status = "not attempted (the window's visual already uses this config)";
    }
    char line[320];
    int length = snprintf(line, sizeof line,
             "[MacOBlox GL] eglCreateWindowSurface failed (EGL error 0x%x): native window 0x%lx, "
             "window visual 0x%x, config visual 0x%x, screen visual 0x%x; retry %s",
             error, window, window_visual, config_visual(display, config), root_visual, retry_status);
    if (length >= 0 && (unsigned int)length < sizeof line) {
        if (retry_error)
            snprintf(line + length, sizeof line - (unsigned int)length, " (EGL error 0x%x)\n", retry_error);
        else
            snprintf(line + length, sizeof line - (unsigned int)length, "\n");
    }
    log_line(line);
    if (surface)
        forget_configured_surface(surface);
    return surface;
}
DYLD_INTERPOSE(macoblox_eglCreateWindowSurface, eglCreateWindowSurface)

/* GL subwindows with the screen's visual.
 *
 * Darling creates its top-level windows with the visual glXChooseVisual
 * returns for RGBA + double buffer + depth, and the OpenGL subwindow
 * (-[X11SubWindow initWithParentWindow:frame:], XCreateSimpleWindow)
 * inherits it. Mesa returns the screen's visual; NVIDIA returns a GLX visual
 * of its own (0x2db, 24 bit, on an RTX 3050) that has no EGL config, so no
 * surface could be created for the game. Such a subwindow is replaced by one with
 * the screen's default visual, the one the EGL config above is chosen for.
 * Called from the X11SubWindow hook in libMacOBloxShims.m; libX11 loads
 * with Darling's X11 backend, after this library, so it is looked up late.
 * MACOBLOX_KEEP_SUBWINDOW_VISUAL=1 keeps Darling's behaviour,
 * MACOBLOX_FORCE_SUBWINDOW_VISUAL=1 replaces every subwindow (to test). */
typedef unsigned long XID;
extern void *dlsym(void *, const char *);
#define X_DEFAULT_HANDLE ((void *)-2)  /* RTLD_DEFAULT */

unsigned long macoblox_replace_gl_subwindow(void *display, unsigned long parent, unsigned long old) {
    const char *keep = getenv("MACOBLOX_KEEP_SUBWINDOW_VISUAL");
    if ((keep && keep[0] == '1') || !display || !parent || !old)
        return old;
    int (*get_attributes)(void *, XID, void *) = dlsym(X_DEFAULT_HANDLE, "XGetWindowAttributes");
    int (*default_screen)(void *) = dlsym(X_DEFAULT_HANDLE, "XDefaultScreen");
    int (*default_depth)(void *, int) = dlsym(X_DEFAULT_HANDLE, "XDefaultDepth");
    void *(*default_visual)(void *, int) = dlsym(X_DEFAULT_HANDLE, "XDefaultVisual");
    XID (*default_colormap)(void *, int) = dlsym(X_DEFAULT_HANDLE, "XDefaultColormap");
    int (*screen_number)(void *) = dlsym(X_DEFAULT_HANDLE, "XScreenNumberOfScreen");
    XID (*create_window)(void *, XID, int, int, unsigned int, unsigned int, unsigned int, int,
                         unsigned int, void *, unsigned long, void *) = dlsym(X_DEFAULT_HANDLE, "XCreateWindow");
    int (*map_window)(void *, XID) = dlsym(X_DEFAULT_HANDLE, "XMapWindow");
    int (*destroy_window)(void *, XID) = dlsym(X_DEFAULT_HANDLE, "XDestroyWindow");
    if (!get_attributes || !default_screen || !default_depth || !default_visual || !default_colormap ||
        !create_window || !map_window || !destroy_window)
        return old;

    /* XWindowAttributes, LP64: x, y, width, height, border_width, depth
     * as ints, then Visual* at 24; map_state at 92. */
    unsigned char parent_attributes[256], old_attributes[256];
    if (!get_attributes(display, parent, parent_attributes) || !get_attributes(display, old, old_attributes))
        return old;
    int screen = screen_number ? screen_number(*(void **)(parent_attributes + 128)) : default_screen(display);
    void *visual = default_visual(display, screen);
    const char *force = getenv("MACOBLOX_FORCE_SUBWINDOW_VISUAL");  /* for testing */
    if (!(force && force[0] == '1') && *(void **)(parent_attributes + 24) == visual)
        return old;

    /* XSetWindowAttributes, LP64: background_pixel at 8, border_pixel at 24,
     * colormap at 96. */
    unsigned char set[112] = {0};
    /* The default visual already has a server-owned colormap. Allocating
     * another one here leaked an X resource every time a drawable changed. */
    *(XID *)(set + 96) = default_colormap(display, screen);
    int *geometry = (int *)old_attributes;
    XID window = create_window(display, parent, geometry[0], geometry[1],
                               geometry[2] > 0 ? (unsigned int)geometry[2] : 1,
                               geometry[3] > 0 ? (unsigned int)geometry[3] : 1, 0,
                               default_depth(display, screen), 1 /* InputOutput */, visual,
                               (1UL << 1) | (1UL << 3) | (1UL << 13), set);
    if (!window)
        return old;
    if (*(int *)(old_attributes + 92) != 0 /* IsUnmapped */)
        map_window(display, window);
    destroy_window(display, old);
    static const char message[] = "[MacOBlox GL] GL subwindow uses the screen visual\n";
    write(2, message, sizeof message - 1);
    return window;
}

/* Frame presentation.
 *
 * Darling presents with eglSwapBuffers at swap interval 1 (vsync). A frame
 * that misses a refresh then waits for the next one, so a game that needs
 * a little more than 16.7 ms drops straight from 60 to 30 FPS; Roblox's own
 * stats showed frames of 30 ms with 9 ms of work and 20 ms idle. The swap
 * interval is set to 0 before the first frame of each window surface (it
 * belongs to the surface, not the context: a context that gets a new surface
 * would get vsync back); Roblox caps the frame rate itself
 * (DFIntTaskSchedulerTargetFps) and the compositor keeps the picture
 * tear-free. MACOBLOX_VSYNC=1 keeps vsync. MACOBLOX_FPS_LOG=1 prints the
 * presented frame rate every 5 s. */
extern unsigned int eglSwapInterval(void *, int);
extern void *eglGetCurrentDisplay(void);
extern void *eglGetCurrentSurface(int);
extern unsigned long long mach_absolute_time(void);
typedef struct { unsigned int numer, denom; } macoblox_timebase;
extern int mach_timebase_info(macoblox_timebase *);

#define EGL_DRAW 0x3059
#define CONFIGURED_SURFACES 16
/* Surfaces whose swap interval is 0 already; a full table overwrites its
 * oldest entry, which then only costs one more eglSwapInterval. */
static struct { void *display, *surface; } configured_surfaces[CONFIGURED_SURFACES];
static int configured_next;
static volatile unsigned int configured_lock;
static struct {
    void *display, *surface;
    unsigned long long start, frames;
} frame_windows[CONFIGURED_SURFACES];
static unsigned int frame_next;
static volatile unsigned int frame_lock;
static macoblox_timebase frame_timebase;

static void lock_configured(void) {
    macoblox_lock(&configured_lock);
}

/* A client can change CGLCPSwapInterval after our first presentation. Darling
 * forwards that setter to EGL for the current draw surface. Forget only the
 * successful nonzero change, so the next presentation reapplies our configured
 * vsync policy without resetting frame statistics or other drawables. */
static unsigned int macoblox_eglSwapInterval(void *display, int interval) {
    unsigned int succeeded = eglSwapInterval(display, interval);
    if (succeeded && interval != 0) {
        void *surface = eglGetCurrentSurface(EGL_DRAW);
        if (surface) {
            lock_configured();
            for (int i = 0; i < CONFIGURED_SURFACES; i++)
                if (configured_surfaces[i].display == display &&
                    configured_surfaces[i].surface == surface)
                    configured_surfaces[i].surface = 0;
            macoblox_unlock(&configured_lock);
        }
    }
    return succeeded;
}
DYLD_INTERPOSE(macoblox_eglSwapInterval, eglSwapInterval)

static void forget_configured_surface(void *surface) {
    lock_configured();
    for (int i = 0; i < CONFIGURED_SURFACES; i++)
        if (configured_surfaces[i].surface == surface)
            configured_surfaces[i].surface = 0;
    macoblox_unlock(&configured_lock);
    macoblox_lock(&frame_lock);
    for (int i = 0; i < CONFIGURED_SURFACES; i++)
        if (frame_windows[i].surface == surface)
            frame_windows[i].surface = 0;
    macoblox_unlock(&frame_lock);
}

static void swap_interval_zero(void) {
    static volatile long logged;
    void *surface = eglGetCurrentSurface(EGL_DRAW);
    void *display = eglGetCurrentDisplay();
    if (!surface || !display)
        return;
    int known = 0;
    lock_configured();
    for (int i = 0; i < CONFIGURED_SURFACES && !known; i++)
        known = configured_surfaces[i].surface == surface && configured_surfaces[i].display == display;
    macoblox_unlock(&configured_lock);
    if (known)
        return;
    if (!eglSwapInterval(display, 0))
        return; /* tried again next frame */
    lock_configured();
    configured_surfaces[configured_next].surface = surface;
    configured_surfaces[configured_next].display = display;
    configured_next = (configured_next + 1) % CONFIGURED_SURFACES;
    macoblox_unlock(&configured_lock);
    long count = __sync_add_and_fetch(&logged, 1);
    if (count <= 8 || count % 100 == 0)
        write(2, "[MacOBlox GL] vsync off (swap interval 0)\n", 42);
}

static int environment_flag(const char *name, int *cache) {
    int cached = __atomic_load_n(cache, __ATOMIC_ACQUIRE);
    if (cached >= 0)
        return cached;
    const char *value = getenv(name);
    int enabled = value && value[0] == '1';
    int expected = -1;
    __atomic_compare_exchange_n(cache, &expected, enabled, 0, __ATOMIC_RELEASE, __ATOMIC_RELAXED);
    return enabled;
}

void macoblox_frame_presenting(void *cgl_context) {
    static int vsync = -1;
    (void)cgl_context; /* the interval goes with the current draw surface */
    if (!environment_flag("MACOBLOX_VSYNC", &vsync))
        swap_interval_zero();
}

void macoblox_frame_presented(void *display, void *surface, unsigned int succeeded) {
    static int fps_log = -1;
    if (!succeeded || !surface || !environment_flag("MACOBLOX_FPS_LOG", &fps_log))
        return;
    unsigned long long now = mach_absolute_time();
    double fps = 0;
    int report = 0;
    macoblox_lock(&frame_lock);
    if (!frame_timebase.denom && (mach_timebase_info(&frame_timebase) || !frame_timebase.denom))
        frame_timebase.numer = frame_timebase.denom = 1;
    unsigned int slot;
    for (slot = 0; slot < CONFIGURED_SURFACES; slot++)
        if (frame_windows[slot].display == display && frame_windows[slot].surface == surface)
            break;
    if (slot == CONFIGURED_SURFACES) {
        slot = frame_next;
        frame_next = (frame_next + 1) % CONFIGURED_SURFACES;
        frame_windows[slot].display = display;
        frame_windows[slot].surface = surface;
        frame_windows[slot].start = now;
        frame_windows[slot].frames = 0;
    } else {
        frame_windows[slot].frames++;
        double nanoseconds = (double)(now - frame_windows[slot].start) * frame_timebase.numer / frame_timebase.denom;
        if (nanoseconds >= 5000000000.0) {
            fps = frame_windows[slot].frames * 1e9 / nanoseconds;
            frame_windows[slot].start = now;
            frame_windows[slot].frames = 0;
            report = 1;
        }
    }
    macoblox_unlock(&frame_lock);
    if (report) {
        char line[96];
        int length = snprintf(line, sizeof line, "[MacOBlox FPS] %.1f surface=%p\n", fps, surface);
        if (length > 0) write(2, line, (unsigned long)length < sizeof line ? (unsigned long)length : sizeof line - 1);
    }
}
