/* Raw mouse motion for mouse lock, through XInput 2 (XI_RawMotion).
 *
 * During mouse lock the camera follows the deltas of the mouse events. Until
 * now they came from Darling, computed from pointer positions: after the X
 * server's pointer acceleration, merged by motion compression, and mixed
 * with the warps that keep the hidden pointer inside the window (the "Mouse
 * lock" part of libMacOBloxShims.m had to recognise and drop those). XI2
 * raw events carry the device's own deltas, before acceleration, at the
 * mouse's report rate, independent of where the pointer is, and warps do not
 * generate them. Under Xwayland they come from the compositor's relative
 * pointer motion. The idea and the event layout follow spidercraft's
 * Roblox Mac Linux Port (appkit-gaps.m, RbxRawMotion), used with permission.
 *
 * Darling wraps libX11 but not libXi, so XIQueryVersion and XISelectEvents
 * come from the host's libXi.so.6 through Darling's elfcalls table (host
 * functions with the same calling convention). Darling's X11Display hands
 * every X event to postXEvent:, where the shim asks macoblox_raw_mouse_event
 * whether it is a raw motion. MACOBLOX_RAW_MOUSE=0 uses pointer deltas. */

extern void *dlsym(void *, const char *);
extern int write(int, const void *, unsigned long);
#define RTLD_DEFAULT ((void *)-2)

struct elf_calls_head { /* the start of mldr's struct elf_calls */
    void *(*dlopen)(const char *);
    int (*dlclose)(void *);
    void *(*dlsym)(void *, const char *);
};

/* XGenericEventCookie and XIRawEvent, x86_64 layout. */
struct generic_cookie {
    int type;
    unsigned long serial;
    int send_event;
    void *display;
    int extension, evtype;
    unsigned int cookie;
    void *data;
};
struct raw_event {
    int type;
    unsigned long serial;
    int send_event;
    void *display;
    int extension, evtype;
    unsigned long time;
    int deviceid, sourceid, detail, flags;
    struct { int mask_len; unsigned char *mask; double *values; } valuators;
    double *raw_values;
};
struct xi_event_mask { int deviceid, mask_len; unsigned char *mask; };

#define GENERIC_EVENT 35
#define XI_RAW_MOTION 17
#define XI_ALL_MASTER_DEVICES 1

static int (*xi_query_version)(void *, int *, int *);
static int (*xi_select_events)(void *, unsigned long, struct xi_event_mask *, int);
static int (*x_query_extension)(void *, const char *, int *, int *, int *);
static unsigned long (*x_default_root_window)(void *);
static int (*x_get_event_data)(void *, struct generic_cookie *);
static void (*x_free_event_data)(void *, struct generic_cookie *);
static int (*x_flush)(void *);
static int resolved; /* 1 all found, -1 something missing */
static int xi_opcode = -1;
static void *xi_display;
static unsigned long raw_event_timestamp;

/* Read on the event owner immediately after a decoded raw motion. Core and
 * XI2 events from the same physical report carry the same server timestamp. */
unsigned long macoblox_raw_mouse_event_timestamp(void) {
    return raw_event_timestamp;
}

static void log_line(const char *text) {
    int length = 0;
    while (text[length]) length++;
    write(2, text, (unsigned long)length);
}

static int resolve(void) {
    if (resolved)
        return resolved > 0;
    resolved = -1;
    struct elf_calls_head **table = dlsym(RTLD_DEFAULT, "_elfcalls");
    void *xi = table && *table && (*table)->dlopen ? (*table)->dlopen("libXi.so.6") : 0;
    if (!xi) {
        log_line("[MacOBlox Input] host libXi.so.6 not found, no raw mouse motion\n");
        return 0;
    }
    xi_query_version = (*table)->dlsym(xi, "XIQueryVersion");
    xi_select_events = (*table)->dlsym(xi, "XISelectEvents");
    /* Resolve the matching host Xlib API alongside libXi. Cookie ownership
     * must use the same Xlib implementation that decoded the XI2 event. */
    void *x11 = (*table)->dlopen("libX11.so.6");
    if (!x11) return 0;
    x_query_extension = (*table)->dlsym(x11, "XQueryExtension");
    x_default_root_window = (*table)->dlsym(x11, "XDefaultRootWindow");
    x_get_event_data = (*table)->dlsym(x11, "XGetEventData");
    x_free_event_data = (*table)->dlsym(x11, "XFreeEventData");
    x_flush = (*table)->dlsym(x11, "XFlush");
    if (!xi_query_version || !xi_select_events || !x_query_extension || !x_default_root_window ||
        !x_get_event_data || !x_free_event_data) {
        log_line("[MacOBlox Input] XInput 2 functions missing, no raw mouse motion\n");
        return 0;
    }
    resolved = 1;
    return 1;
}

/* Select (or deselect) raw motion of all master pointers on this
 * connection's root window. Returns 1 when raw motion is selected. Call it
 * from the thread that reads events from `display`. */
int macoblox_raw_mouse_select(void *display, int enabled) {
    if (!display || !resolve())
        return 0;
    if (xi_opcode < 0 || xi_display != display) {
        int event, error, major = 2, minor = 1; /* 2.1: raw events also during grabs */
        if (!x_query_extension(display, "XInputExtension", &xi_opcode, &event, &error) ||
            xi_query_version(display, &major, &minor) != 0 || major < 2 || (major == 2 && minor < 1)) {
            xi_opcode = -1;
            log_line("[MacOBlox Input] the X server has no XInput 2.1, no raw mouse motion\n");
            return 0;
        }
        xi_display = display;
    }
    unsigned char bits[4] = {0, 0, 0, 0};
    if (enabled)
        bits[XI_RAW_MOTION / 8] |= (unsigned char)(1 << (XI_RAW_MOTION % 8));
    struct xi_event_mask mask = {XI_ALL_MASTER_DEVICES, sizeof bits, bits};
    int status = xi_select_events(display, x_default_root_window(display), &mask, 1);
    if (x_flush)
        x_flush(display);
    return status == 0 && enabled;
}

/* Consume XI2 cookies on the selected connection and release their data.
 * Raw motion stores device deltas (x right, y down); other XI2 events store
 * zero deltas. Events outside this extension return 0 untouched. */
int macoblox_raw_mouse_event(void *display, void *event, double *dx, double *dy) {
    struct generic_cookie *cookie = event;
    if (!display || !event || !dx || !dy || cookie->type != GENERIC_EVENT ||
        xi_opcode < 0 || cookie->extension != xi_opcode ||
        (cookie->display && cookie->display != display))
        return 0;
    *dx = *dy = 0;
    if (!cookie->data && !x_get_event_data(display, cookie)) {
        static unsigned int failures;
        if (failures++ < 3)
            log_line("[MacOBlox Input] XI2 event cookie unavailable; waiting for motion or pointer fallback\n");
        return 1;
    }
    struct raw_event *raw = cookie->data;
    if (cookie->evtype == XI_RAW_MOTION && raw && raw->valuators.mask && raw->valuators.mask_len >= 1) {
        raw_event_timestamp = raw->time;
        /* Values are packed in the order of the set mask bits; valuator 0
         * is x, 1 is y. raw_values are the device's, values accelerated. */
        double *values = raw->raw_values ? raw->raw_values : raw->valuators.values;
        unsigned char bits = raw->valuators.mask[0];
        int at = 0;
        if (values) {
            if (bits & 1) *dx = values[at++];
            if (bits & 2) *dy = values[at];
        }
    }
    if (!__builtin_isfinite(*dx) || !__builtin_isfinite(*dy)) *dx = *dy = 0;
    /* Free every acquired XI2 cookie, including unrelated XI events. */
    x_free_event_data(display, cookie);
    return 1;
}
