#ifndef MACOBLOX_X11_DRAWABLE_CACHE_H
#define MACOBLOX_X11_DRAWABLE_CACHE_H

/* Include after the shim's Objective-C and MacOBloxRect declarations. This
 * removes repeated child-window requests without bypassing NSOpenGLContext's
 * preparation or its main-thread updateViewParameters call. */
extern Method *class_copyMethodList(Class, unsigned int *);
extern unsigned int method_getNumberOfArguments(Method);
extern void method_getArgumentType(Method, unsigned int, char *, unsigned long);
extern void method_getReturnType(Method, char *, unsigned long);
extern const char *ivar_getTypeEncoding(Ivar);
extern unsigned long class_getInstanceSize(Class);
extern id objc_getAssociatedObject(id, const void *);
extern void objc_setAssociatedObject(id, const void *, id, unsigned long);
extern void free(void *);
extern int pthread_main_np(void);

typedef struct {
    void *display;
    unsigned long child, parent_window;
    id parent;
    unsigned long long epoch;
    int x, y;
    unsigned int width, height;
    int frame_known, visibility_known, visible;
} MacOBloxDrawableState;

static Class macoblox_drawable_class, macoblox_drawable_parent_class;
static long macoblox_drawable_display_offset, macoblox_drawable_window_offset;
static long macoblox_drawable_parent_offset;
static SEL macoblox_drawable_convert_selector, macoblox_drawable_handle_selector;
static MacOBloxRect (*macoblox_drawable_convert_original)(id, SEL, MacOBloxRect);
static void (*macoblox_drawable_frame_original)(id, SEL, MacOBloxRect);
static void (*macoblox_drawable_show_original)(id, SEL);
static void (*macoblox_drawable_hide_original)(id, SEL);
static int macoblox_drawable_install_state;
static unsigned int macoblox_drawable_off_main_active;
static unsigned long long macoblox_drawable_off_main_epoch;
static unsigned long long macoblox_drawable_main_calls; /* main thread only */
static char macoblox_drawable_state_key;

static int macoblox_drawable_equal(const char *a, const char *b) {
    if (!a || !b) return 0;
    while (*a && *a == *b) { a++; b++; }
    return *a == *b;
}

/* Ignore struct names, but require exactly two nested pairs of doubles.
 * A size-only check could accidentally accept a different 32-byte ABI. */
static const char *macoblox_drawable_struct_fields(const char *type) {
    if (!type || *type++ != '{') return 0;
    while (*type && *type != '=') {
        if (*type == '{' || *type == '}') return 0;
        type++;
    }
    return *type == '=' ? type + 1 : 0;
}

static int macoblox_drawable_rect_type(const char *type) {
    const char *fields = macoblox_drawable_struct_fields(type);
    if (!fields) return 0;
    for (int pair = 0; pair < 2; pair++) {
        fields = macoblox_drawable_struct_fields(fields);
        if (!fields || fields[0] != 'd' || fields[1] != 'd' || fields[2] != '}') return 0;
        fields += 3;
    }
    return fields[0] == '}' && !fields[1];
}

static int macoblox_drawable_method_abi(Method method, int rect_argument, int rect_return) {
    if (!method || method_getNumberOfArguments(method) != (rect_argument ? 3U : 2U)) return 0;
    char type[256] = {0};
    method_getReturnType(method, type, sizeof type);
    type[sizeof type - 1] = 0;
    if (rect_return ? !macoblox_drawable_rect_type(type) : !macoblox_drawable_equal(type, "v")) return 0;
    method_getArgumentType(method, 0, type, sizeof type);
    type[sizeof type - 1] = 0;
    if (!macoblox_drawable_equal(type, "@")) return 0;
    method_getArgumentType(method, 1, type, sizeof type);
    type[sizeof type - 1] = 0;
    if (!macoblox_drawable_equal(type, ":")) return 0;
    if (rect_argument) {
        method_getArgumentType(method, 2, type, sizeof type);
        type[sizeof type - 1] = 0;
        if (!macoblox_drawable_rect_type(type)) return 0;
    }
    return 1;
}

static int macoblox_drawable_owned(Class cls, Method method) {
    unsigned int count = 0;
    Method *methods = class_copyMethodList(cls, &count);
    int owned = 0;
    for (unsigned int index = 0; methods && index < count; index++)
        if (methods[index] == method) { owned = 1; break; }
    free(methods);
    return owned;
}

static int macoblox_drawable_ivar(Class cls, const char *name, char kind, long *offset) {
    Ivar ivar = class_getInstanceVariable(cls, name);
    const char *type = ivar ? ivar_getTypeEncoding(ivar) : 0;
    if (!type || type[0] != kind || (kind == 'Q' && type[1])) return 0;
    long position = ivar_getOffset(ivar);
    unsigned long size = class_getInstanceSize(cls);
    if (position < (long)sizeof(void *) || (unsigned long)position % sizeof(void *) ||
        size < sizeof(void *) || (unsigned long)position > size - sizeof(void *)) return 0;
    *offset = position;
    return 1;
}

static int macoblox_drawable_epoch_valid(unsigned long long epoch) {
    return !__atomic_load_n(&macoblox_drawable_off_main_active, __ATOMIC_ACQUIRE) &&
        __atomic_load_n(&macoblox_drawable_off_main_epoch, __ATOMIC_ACQUIRE) == epoch;
}

static void macoblox_drawable_off_main_begin(void) {
    __atomic_add_fetch(&macoblox_drawable_off_main_active, 1, __ATOMIC_ACQ_REL);
    __atomic_add_fetch(&macoblox_drawable_off_main_epoch, 1, __ATOMIC_ACQ_REL);
}

static void macoblox_drawable_off_main_end(void) {
    __atomic_add_fetch(&macoblox_drawable_off_main_epoch, 1, __ATOMIC_RELEASE);
    __atomic_sub_fetch(&macoblox_drawable_off_main_active, 1, __ATOMIC_RELEASE);
}

static int macoblox_drawable_identity(id self, MacOBloxDrawableState *state) {
    if (!self || object_getClass(self) != macoblox_drawable_class) return 0;
    state->display = *(void **)((char *)self + macoblox_drawable_display_offset);
    state->child = *(unsigned long *)((char *)self + macoblox_drawable_window_offset);
    state->parent = *(id *)((char *)self + macoblox_drawable_parent_offset);
    if (!state->display || !state->child || !state->parent ||
        object_getClass(state->parent) != macoblox_drawable_parent_class) return 0;
    state->parent_window = ((unsigned long (*)(id, SEL))objc_msgSend)(
        state->parent, macoblox_drawable_handle_selector);
    return state->parent_window != 0;
}

static int macoblox_drawable_same_identity(const MacOBloxDrawableState *a,
                                           const MacOBloxDrawableState *b) {
    return a->display == b->display && a->child == b->child &&
        a->parent == b->parent && a->parent_window == b->parent_window;
}

/* Only the main thread reads or changes the association. Atomic retain gives
 * the returned immutable NSData its normal safe getter lifetime. */
static void macoblox_drawable_load(id self, MacOBloxDrawableState *state) {
    id data = objc_getAssociatedObject(self, &macoblox_drawable_state_key);
    if (!data || ((unsigned long (*)(id, SEL))objc_msgSend)(data, sel_registerName("length")) != sizeof *state)
        return;
    const unsigned char *bytes = ((const unsigned char *(*)(id, SEL))objc_msgSend)(data, sel_registerName("bytes"));
    if (!bytes) return;
    MacOBloxDrawableState cached;
    for (unsigned long index = 0; index < sizeof cached; index++)
        ((unsigned char *)&cached)[index] = bytes[index];
    if (cached.epoch == state->epoch && macoblox_drawable_same_identity(&cached, state) &&
        macoblox_drawable_epoch_valid(state->epoch))
        *state = cached;
}

static void macoblox_drawable_publish(id self, const MacOBloxDrawableState *state,
                                     unsigned long long call) {
    if (macoblox_drawable_main_calls != call || !macoblox_drawable_epoch_valid(state->epoch)) return;
    MacOBloxDrawableState current = {0};
    if (!macoblox_drawable_identity(self, &current) || !macoblox_drawable_same_identity(state, &current)) return;
    id data = ((id (*)(id, SEL, const void *, unsigned long))objc_msgSend)(
        (id)objc_getClass("NSData"), sel_registerName("dataWithBytes:length:"), state, sizeof *state);
    if (data && macoblox_drawable_main_calls == call && macoblox_drawable_epoch_valid(state->epoch)) {
        objc_setAssociatedObject(self, &macoblox_drawable_state_key, data,
                                 01401UL /* OBJC_ASSOCIATION_RETAIN, atomic */);
        /* A worker may start between the last check and publication. Its
         * changed epoch makes this state unusable; never inspect it there. */
        if (!macoblox_drawable_epoch_valid(state->epoch))
            objc_setAssociatedObject(self, &macoblox_drawable_state_key, 0, 01401UL);
    }
}

static int macoblox_drawable_geometry(MacOBloxRect rect, MacOBloxDrawableState *state) {
    if (!(rect.origin.x >= -2147483648.0 && rect.origin.x <= 2147483647.0 &&
          rect.origin.y >= -2147483648.0 && rect.origin.y <= 2147483647.0 &&
          rect.size.width >= 1.0 && rect.size.width <= 4294967295.0 &&
          rect.size.height >= 1.0 && rect.size.height <= 4294967295.0)) return 0;
    state->x = (int)rect.origin.x;
    state->y = (int)rect.origin.y;
    state->width = (unsigned int)rect.size.width;
    state->height = (unsigned int)rect.size.height;
    return 1;
}

static int macoblox_drawable_same_geometry(const MacOBloxDrawableState *a,
                                          const MacOBloxDrawableState *b) {
    return a->x == b->x && a->y == b->y && a->width == b->width && a->height == b->height;
}

static void macoblox_drawable_set_frame(id self, SEL cmd, MacOBloxRect frame) {
    if (!pthread_main_np()) {
        macoblox_drawable_off_main_begin();
        @try { macoblox_drawable_frame_original(self, cmd, frame); }
        @finally { macoblox_drawable_off_main_end(); }
        return;
    }
    MacOBloxDrawableState state = {0};
    state.epoch = __atomic_load_n(&macoblox_drawable_off_main_epoch, __ATOMIC_ACQUIRE);
    int supported = __atomic_load_n(&macoblox_drawable_install_state, __ATOMIC_ACQUIRE) == 2 &&
        macoblox_drawable_identity(self, &state) && macoblox_drawable_epoch_valid(state.epoch);
    MacOBloxDrawableState requested = state;
    if (supported) {
        macoblox_drawable_load(self, &state);
        /* Calling the original IMP handles CGRect's structure-return ABI and
         * includes the parent's current frame height and border style. */
        supported = macoblox_drawable_geometry(
            macoblox_drawable_convert_original(self, macoblox_drawable_convert_selector, frame), &requested);
        if (supported && state.frame_known && macoblox_drawable_same_geometry(&state, &requested) &&
            macoblox_drawable_epoch_valid(state.epoch)) return;
    }
    objc_setAssociatedObject(self, &macoblox_drawable_state_key, 0, 01401UL);
    unsigned long long call = ++macoblox_drawable_main_calls;
    macoblox_drawable_frame_original(self, cmd, frame);
    /* Exceptions above propagate with the previous success state removed. */
    if (supported && macoblox_drawable_epoch_valid(state.epoch)) {
        MacOBloxDrawableState after = requested;
        if (macoblox_drawable_geometry(
                macoblox_drawable_convert_original(self, macoblox_drawable_convert_selector, frame), &after) &&
            macoblox_drawable_same_geometry(&requested, &after)) {
            state.x = requested.x; state.y = requested.y;
            state.width = requested.width; state.height = requested.height;
            state.frame_known = 1;
            macoblox_drawable_publish(self, &state, call);
        }
    }
}

static void macoblox_drawable_visibility(id self, SEL cmd, int visible) {
    void (*original)(id, SEL) = visible ? macoblox_drawable_show_original : macoblox_drawable_hide_original;
    if (!pthread_main_np()) {
        macoblox_drawable_off_main_begin();
        @try { original(self, cmd); }
        @finally { macoblox_drawable_off_main_end(); }
        return;
    }
    MacOBloxDrawableState state = {0};
    state.epoch = __atomic_load_n(&macoblox_drawable_off_main_epoch, __ATOMIC_ACQUIRE);
    int supported = __atomic_load_n(&macoblox_drawable_install_state, __ATOMIC_ACQUIRE) == 2 &&
        macoblox_drawable_identity(self, &state) && macoblox_drawable_epoch_valid(state.epoch);
    if (supported) {
        macoblox_drawable_load(self, &state);
        if (state.visibility_known && state.visible == visible && macoblox_drawable_epoch_valid(state.epoch)) return;
    }
    objc_setAssociatedObject(self, &macoblox_drawable_state_key, 0, 01401UL);
    unsigned long long call = ++macoblox_drawable_main_calls;
    original(self, cmd);
    if (supported) {
        state.visibility_known = 1;
        state.visible = visible;
        macoblox_drawable_publish(self, &state, call);
    }
}

static void macoblox_drawable_show(id self, SEL cmd) { macoblox_drawable_visibility(self, cmd, 1); }
static void macoblox_drawable_hide(id self, SEL cmd) { macoblox_drawable_visibility(self, cmd, 0); }

static void macoblox_install_x11_drawable_cache(Class cls) {
    if (!cls || macoblox_wayland_enabled()) return;
    int expected = 0;
    if (!__atomic_compare_exchange_n(&macoblox_drawable_install_state, &expected, 1, 0,
                                     __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) return;
    SEL convert_selector = sel_registerName("convertFrame:");
    Method convert = class_getInstanceMethod(cls, convert_selector);
    Method frame = class_getInstanceMethod(cls, sel_registerName("setFrame:"));
    Method show = class_getInstanceMethod(cls, sel_registerName("show"));
    Method hide = class_getInstanceMethod(cls, sel_registerName("hide"));
    Class parent_class = objc_getClass("X11Window");
    SEL handle_selector = sel_registerName("windowHandle");
    Method handle = parent_class ? class_getInstanceMethod(parent_class, handle_selector) : 0;
    char type[32] = {0};
    if (handle) method_getReturnType(handle, type, sizeof type);
    int valid = sizeof(MacOBloxRect) == 32 && __alignof__(MacOBloxRect) == 8 && sizeof(void *) == 8 &&
        macoblox_drawable_owned(cls, convert) && macoblox_drawable_owned(cls, frame) &&
        macoblox_drawable_owned(cls, show) && macoblox_drawable_owned(cls, hide) &&
        macoblox_drawable_method_abi(convert, 1, 1) && macoblox_drawable_method_abi(frame, 1, 0) &&
        macoblox_drawable_method_abi(show, 0, 0) && macoblox_drawable_method_abi(hide, 0, 0) &&
        handle && macoblox_drawable_owned(parent_class, handle) &&
        method_getNumberOfArguments(handle) == 2 && macoblox_drawable_equal(type, "Q") &&
        objc_getClass("NSData") &&
        macoblox_drawable_ivar(cls, "_display", '^', &macoblox_drawable_display_offset) &&
        macoblox_drawable_ivar(cls, "_window", 'Q', &macoblox_drawable_window_offset) &&
        macoblox_drawable_ivar(cls, "_parent", '@', &macoblox_drawable_parent_offset) &&
        macoblox_drawable_display_offset != macoblox_drawable_window_offset &&
        macoblox_drawable_display_offset != macoblox_drawable_parent_offset &&
        macoblox_drawable_window_offset != macoblox_drawable_parent_offset;
    if (valid) {
        method_getArgumentType(handle, 0, type, sizeof type);
        valid = macoblox_drawable_equal(type, "@");
        method_getArgumentType(handle, 1, type, sizeof type);
        valid = valid && macoblox_drawable_equal(type, ":");
    }
    if (!valid || !method_getImplementation(convert) || !method_getImplementation(frame) ||
        !method_getImplementation(show) || !method_getImplementation(hide)) {
        __atomic_store_n(&macoblox_drawable_install_state, 3, __ATOMIC_RELEASE);
        write_str("[MacOBlox] X11 drawable cache skipped: unsupported backend ABI\n");
        return;
    }
    macoblox_drawable_class = cls;
    macoblox_drawable_parent_class = parent_class;
    macoblox_drawable_convert_selector = convert_selector;
    macoblox_drawable_handle_selector = handle_selector;
    macoblox_drawable_convert_original = (MacOBloxRect (*)(id, SEL, MacOBloxRect))method_getImplementation(convert);
    macoblox_drawable_frame_original = (void (*)(id, SEL, MacOBloxRect))method_getImplementation(frame);
    macoblox_drawable_show_original = (void (*)(id, SEL))method_getImplementation(show);
    macoblox_drawable_hide_original = (void (*)(id, SEL))method_getImplementation(hide);
    method_setImplementation(frame, (IMP)macoblox_drawable_set_frame);
    method_setImplementation(show, (IMP)macoblox_drawable_show);
    method_setImplementation(hide, (IMP)macoblox_drawable_hide);
    __atomic_store_n(&macoblox_drawable_install_state, 2, __ATOMIC_RELEASE);
    write_str("[MacOBlox] X11 drawable updates skip unchanged geometry and visibility\n");
}
#endif
