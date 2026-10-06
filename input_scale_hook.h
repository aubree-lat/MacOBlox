#ifndef MACOBLOX_INPUT_SCALE_HOOK_H
#define MACOBLOX_INPUT_SCALE_HOOK_H

/* InputCapture caches physical, top-left points for AppKit cursor warps.
 * Its two Mac engine wrappers are the first boundary where only the engine
 * needs those points in the artificial screen-DPI coordinate space. Keep
 * the original effective-UI-scale conversion and raw dx/dy in the engine. */
extern Method *class_copyMethodList(Class, unsigned int *);
extern SEL method_getName(Method);
extern const void *_dyld_get_image_header(unsigned int);
extern int getpagesize(void);
extern int mprotect(void *, unsigned long, int);

static unsigned long long macoblox_input_scale_bits = 0x3ff0000000000000ULL;
static const volatile unsigned char *macoblox_debug_dpi_override;
static void *macoblox_mouse_source;
static void (*macoblox_mouse_move_inner)(void *, const void *, int,
                                         float, float, float, float);
static void (*macoblox_mouse_button_inner)(void *, const void *, int, int,
                                           int, int, int);

static void macoblox_input_scale_set(double scale) {
    union { double value; unsigned long long bits; } factor = { .value = scale };
    __atomic_store_n(&macoblox_input_scale_bits, factor.bits, __ATOMIC_RELEASE);
}

static double macoblox_input_scale_get(void) {
    union { double value; unsigned long long bits; } factor;
    factor.bits = __atomic_load_n(&macoblox_input_scale_bits, __ATOMIC_ACQUIRE);
    return factor.value;
}

static int macoblox_input_scale_allowed(void) {
    return macoblox_debug_dpi_override && *macoblox_debug_dpi_override != 1;
}

static void macoblox_scaled_mouse_move(void *engine, int active,
                                       float x, float y, float dx, float dy) {
    double factor = macoblox_input_scale_get();
    macoblox_mouse_move_inner(engine, macoblox_mouse_source, active,
                              (float)(x / factor), (float)(y / factor), dx, dy);
}

static void macoblox_scaled_mouse_button(void *engine, int x, int y,
                                         int button, int pressed, int click_count_offset) {
    double factor = macoblox_input_scale_get();
    macoblox_mouse_button_inner(engine, macoblox_mouse_source,
                                (int)(x / factor), (int)(y / factor),
                                button, pressed, click_count_offset);
}

static int macoblox_input_string_equal(const char *left, const char *right) {
    if (!left || !right) return 0;
    while (*left && *left == *right) { ++left; ++right; }
    return *left == *right;
}

static Method macoblox_input_owned_method(Class cls, const char *name,
                                           const char *encoding) {
    unsigned int count = 0;
    Method *methods = class_copyMethodList(cls, &count);
    Method found = 0;
    SEL selector = sel_registerName(name);
    if (methods)
        for (unsigned int index = 0; index < count; ++index)
            if (method_getName(methods[index]) == selector &&
                macoblox_input_string_equal(method_getTypeEncoding(methods[index]), encoding)) {
                found = methods[index];
                break;
            }
    free(methods);
    if (!found) {
        write_str("[MacOBlox] UI scale: unsupported InputCapture ");
        write_str(name);
        write_str(" method encoding\n");
    }
    return found;
}

#if defined(__x86_64__)
typedef struct {
    unsigned long begin, size, file_offset, file_size;
    unsigned int protection;
} MacOBloxInputSegment;
typedef struct {
    MacOBloxInputSegment segments[16];
    unsigned int count;
    unsigned long text;
    const unsigned char *starts;
    unsigned long starts_size;
} MacOBloxInputImage;

static unsigned int macoblox_input_u32(const void *pointer) {
    unsigned int value;
    __builtin_memcpy(&value, pointer, sizeof(value));
    return value;
}
static unsigned long macoblox_input_u64(const void *pointer) {
    unsigned long value;
    __builtin_memcpy(&value, pointer, sizeof(value));
    return value;
}
static unsigned long macoblox_input_relative(const unsigned char *displacement) {
    int value;
    __builtin_memcpy(&value, displacement, sizeof(value));
    return (unsigned long)(displacement + 4) + (long)value;
}

static int macoblox_input_range(const MacOBloxInputImage *image,
                                 unsigned long address, unsigned long length,
                                 unsigned int protection) {
    for (unsigned int index = 0; index < image->count; ++index) {
        const MacOBloxInputSegment *segment = &image->segments[index];
        if ((segment->protection & protection) == protection &&
            address >= segment->begin && address - segment->begin <= segment->size &&
            length <= segment->size - (address - segment->begin))
            return 1;
    }
    return 0;
}

/* Parse the loaded main Mach-O, never the installed client file. dyld owns
 * the header; segment permissions and LC_FUNCTION_STARTS bound every scan. */
static int macoblox_input_image(MacOBloxInputImage *image) {
    const unsigned char *header = _dyld_get_image_header(0);
    if (!header || macoblox_input_u32(header) != 0xfeedfacf ||
        macoblox_input_u32(header + 4) != 0x01000007) return 0;
    unsigned int count = macoblox_input_u32(header + 16);
    unsigned int command_size = macoblox_input_u32(header + 20);
    if (count > 512 || command_size > 1048576) return 0;
    unsigned long slide = (unsigned long)_dyld_get_image_vmaddr_slide(0);
    unsigned int starts_offset = 0, starts_size = 0;
    unsigned int position = 32;
    for (unsigned int index = 0; index < count; ++index) {
        if (position - 32 > command_size || command_size - (position - 32) < 8) return 0;
        const unsigned char *command = header + position;
        unsigned int type = macoblox_input_u32(command);
        unsigned int size = macoblox_input_u32(command + 4);
        if (size < 8 || size > command_size - (position - 32)) return 0;
        if (type == 0x19) { /* LC_SEGMENT_64 */
            if (size < 72 || image->count == 16) return 0;
            MacOBloxInputSegment *segment = &image->segments[image->count++];
            segment->begin = macoblox_input_u64(command + 24) + slide;
            segment->size = macoblox_input_u64(command + 32);
            segment->file_offset = macoblox_input_u64(command + 40);
            segment->file_size = macoblox_input_u64(command + 48);
            segment->protection = macoblox_input_u32(command + 60);
            if (segment->size > ~0UL - segment->begin) return 0;
            if ((segment->protection & 5) == 5 && !segment->file_offset)
                image->text = segment->begin;
        } else if (type == 0x26) { /* LC_FUNCTION_STARTS */
            if (size != 16 || starts_size) return 0;
            starts_offset = macoblox_input_u32(command + 8);
            starts_size = macoblox_input_u32(command + 12);
        }
        position += size;
    }
    if (image->text != (unsigned long)header || !starts_size || starts_size > 2097152) return 0;
    for (unsigned int index = 0; index < image->count; ++index) {
        MacOBloxInputSegment *segment = &image->segments[index];
        if (starts_offset >= segment->file_offset &&
            starts_offset - segment->file_offset <= segment->file_size &&
            starts_size <= segment->file_size - (starts_offset - segment->file_offset)) {
            image->starts = (const unsigned char *)(segment->begin +
                starts_offset - segment->file_offset);
            image->starts_size = starts_size;
            return macoblox_input_range(image, (unsigned long)image->starts, starts_size, 1);
        }
    }
    return 0;
}

static int macoblox_input_next_function(const MacOBloxInputImage *image,
                                         unsigned long *position, unsigned long *address) {
    unsigned long delta = 0;
    unsigned int shift = 0;
    unsigned char byte;
    do {
        if (*position == image->starts_size || shift >= 64) return -1;
        byte = image->starts[(*position)++];
        if (shift == 63 && (byte & 0x7f) > 1) return -1;
        delta |= (unsigned long)(byte & 0x7f) << shift;
        shift += 7;
    } while (byte & 0x80);
    if (!delta) return 0;
    if (delta > ~0UL - *address) return -1;
    *address += delta;
    return 1;
}

static unsigned long macoblox_input_function_size(const MacOBloxInputImage *image,
                                                  const void *function) {
    unsigned long address = image->text, wanted = (unsigned long)function;
    unsigned long position = 0;
    int found = 0;
    while (macoblox_input_next_function(image, &position, &address) > 0) {
        if (found) return address - wanted;
        if (address == wanted) found = 1;
        if (address > wanted && !found) return 0;
    }
    return 0;
}

static int macoblox_input_bytes(const unsigned char *pointer,
                                const unsigned char *expected, unsigned long size) {
    for (unsigned long index = 0; index < size; ++index)
        if (pointer[index] != expected[index]) return 0;
    return 1;
}

/* A client debug override can reject a changed Settings.scale. Find its
 * setter predicate and named flag registration in the loaded image. Reading
 * this flag preserves client overrides without patching renderer code. */
static int macoblox_input_find_dpi_override(const MacOBloxInputImage *image) {
    static const unsigned char setter_head[] = {
        0x55,0x48,0x89,0xe5,0x41,0x57,0x41,0x56,0x53,0x50,0x48,0x89,0xfb,0x80,0x3d};
    static const unsigned char predicate[] = {0x01,0x75,0x17,0xf3,0x0f,0x2a,0x0d};
    static const unsigned char comparison[] = {0x0f,0x2e,0xc8,0x0f,0x85};
    static const unsigned char previous_dpi[] = {0xf3,0x0f,0x10,0x8b,0x80,0x0d,0x00,0x00};
    static const unsigned char store_dpi[] = {0xf3,0x0f,0x11,0x83,0x80,0x0d,0x00,0x00};
    static const unsigned char setter_return[] = {0x48,0x83,0xc4,0x08,0x5b,0x41,0x5e,0x41,0x5f,0x5d,0xc3};
    static const unsigned char registration_head[] = {0x55,0x48,0x89,0xe5,0x48,0x8d,0x3d};
    static const unsigned char registration_flag[] = {0x48,0x8d,0x35};
    static const unsigned char registration_tail[] = {0xba,0x02,0x00,0x00,0x00,0x5d,0xe9};
    static const char flag_name[] = "DebugOverrideDPIScale";
    static const char setter_log[] = "[FLog::Graphics] Setting screen DPI scale to %.2f (was %.2f)";
    unsigned long position = 0, address = image->text;
    unsigned long setter_flag = 0, named_flag = 0;
    int next;
    while ((next = macoblox_input_next_function(image, &position, &address)) > 0) {
        const unsigned char *function = (const unsigned char *)address;
        if (!macoblox_input_range(image, address, 29, 5)) continue;
        if (macoblox_input_bytes(function, setter_head, sizeof(setter_head)) &&
            macoblox_input_range(image, address, 344, 5) &&
            macoblox_input_bytes(function + 19, predicate, sizeof(predicate)) &&
            macoblox_input_bytes(function + 30, comparison, sizeof(comparison)) &&
            macoblox_input_bytes(function + 45, previous_dpi, sizeof(previous_dpi)) &&
            macoblox_input_bytes(function + 132, store_dpi, sizeof(store_dpi))) {
            if (setter_flag ||
                macoblox_input_function_size(image, function) < 344 ||
                function[39] != 0x0f || function[40] != 0x8a ||
                macoblox_input_relative(function + 35) != address + 333 ||
                macoblox_input_relative(function + 41) != address + 333 ||
                !macoblox_input_bytes(function + 333, setter_return, sizeof(setter_return)) ||
                function[113] != 0x48 || function[114] != 0x8d || function[115] != 0x15)
                return 0;
            unsigned long log = macoblox_input_relative(function + 116);
            unsigned long override_value = macoblox_input_relative(function + 26);
            /* cmpb has a trailing immediate after its RIP displacement. */
            setter_flag = macoblox_input_relative(function + 15) + 1;
            if (!macoblox_input_range(image, log, sizeof(setter_log), 1) ||
                !macoblox_input_bytes((const unsigned char *)log,
                                       (const unsigned char *)setter_log, sizeof(setter_log)) ||
                (override_value & 3) ||
                !macoblox_input_range(image, override_value, 4, 3) ||
                macoblox_input_range(image, override_value, 4, 4) ||
                !macoblox_input_range(image, setter_flag, 1, 3) ||
                macoblox_input_range(image, setter_flag, 1, 4)) return 0;
        }
        if (macoblox_input_bytes(function, registration_head, sizeof(registration_head)) &&
            macoblox_input_bytes(function + 11, registration_flag, sizeof(registration_flag)) &&
            macoblox_input_bytes(function + 18, registration_tail, sizeof(registration_tail))) {
            unsigned long name = macoblox_input_relative(function + 7);
            if (!macoblox_input_range(image, name, sizeof(flag_name), 1) ||
                !macoblox_input_bytes((const unsigned char *)name,
                                       (const unsigned char *)flag_name, sizeof(flag_name))) continue;
            if (named_flag || macoblox_input_function_size(image, function) < 29 ||
                !macoblox_input_range(image, macoblox_input_relative(function + 25), 16, 5))
                return 0;
            named_flag = macoblox_input_relative(function + 14);
        }
    }
    if (next < 0 || !setter_flag || setter_flag != named_flag) return 0;
    macoblox_debug_dpi_override = (const volatile unsigned char *)setter_flag;
    return 1;
}

/* Exact wrapper contracts with only RIP-relative displacements decoded.
 * Layout changes in wrappers or inner ABIs disable the paired DPI hook. */
static int macoblox_input_wrapper(const MacOBloxInputImage *image,
                                  const unsigned char *wrapper, int button,
                                  void **source, void **inner) {
    static const unsigned char move_head[] = {0x55,0x48,0x89,0xe5,0x89,0xf2,0x48,0x8d,0x35};
    static const unsigned char move_tail[] = {0x5d,0xe9};
    static const unsigned char button_head[] = {
        0x55,0x48,0x89,0xe5,0x48,0x83,0xec,0x10,0x44,0x89,0xc0,
        0x41,0x89,0xc8,0x89,0xd1,0x89,0xf2,0x44,0x89,0x0c,0x24,0x48,0x8d,0x35};
    static const unsigned char button_call[] = {0x41,0x89,0xc1,0xe8};
    static const unsigned char button_tail[] = {0x48,0x83,0xc4,0x10,0x5d,0xc3};
    unsigned long length = button ? 43 : 19;
    if (macoblox_input_function_size(image, wrapper) < length ||
        !macoblox_input_range(image, (unsigned long)wrapper, length, 5)) return 0;
    if (button) {
        if (!macoblox_input_bytes(wrapper, button_head, sizeof(button_head)) ||
            !macoblox_input_bytes(wrapper + 29, button_call, sizeof(button_call)) ||
            !macoblox_input_bytes(wrapper + 37, button_tail, sizeof(button_tail))) return 0;
        *source = (void *)macoblox_input_relative(wrapper + 25);
        *inner = (void *)macoblox_input_relative(wrapper + 33);
    } else {
        if (!macoblox_input_bytes(wrapper, move_head, sizeof(move_head)) ||
            !macoblox_input_bytes(wrapper + 13, move_tail, sizeof(move_tail))) return 0;
        *source = (void *)macoblox_input_relative(wrapper + 9);
        *inner = (void *)macoblox_input_relative(wrapper + 15);
    }
    return macoblox_input_range(image, (unsigned long)*source, 24, 1) &&
           !macoblox_input_range(image, (unsigned long)*source, 24, 4) &&
           macoblox_input_range(image, (unsigned long)*inner, 96, 5);
}

static const unsigned char *macoblox_input_find_wrapper(const MacOBloxInputImage *image,
                                                        Method method, const char *name, int button) {
    const unsigned char *function = (const unsigned char *)method_getImplementation(method);
    unsigned long length = macoblox_input_function_size(image, function);
    if (length < 5 || length > 4096 ||
        !macoblox_input_range(image, (unsigned long)function, length, 5)) goto unsupported;
    const unsigned char *found = 0;
    for (unsigned long index = 0; index + 5 <= length; ++index) {
        if (function[index] != 0xe8) continue;
        const unsigned char *target = (const unsigned char *)macoblox_input_relative(function + index + 1);
        void *source, *inner;
        if (!macoblox_input_wrapper(image, target, button, &source, &inner)) continue;
        /* Check the direct call's argument setup as well as its target. */
        static const unsigned char move_args[] = {0x48,0x89,0xc7,0xbe,0x01,0x00,0x00,0x00};
        static const unsigned char button_args[] = {0x0f,0xb6,0x4d,0xd0,0x48,0x89,0xc7};
        unsigned long args_size = button ? sizeof(button_args) : sizeof(move_args);
        if (index < args_size || !macoblox_input_bytes(function + index - args_size,
            button ? button_args : move_args, args_size) || found) goto unsupported;
        found = target;
    }
    if (found) return found;
unsupported:
    write_str("[MacOBlox] UI scale: unsupported InputCapture ");
    write_str(name);
    write_str(" mouse handoff signature\n");
    return 0;
}

static int macoblox_input_inner_abi(const MacOBloxInputImage *image,
                                   const unsigned char *move, const unsigned char *button) {
    static const unsigned char move_prefix[] = {
        0x55,0x48,0x89,0xe5,0x41,0x57,0x41,0x56,0x53,0x48,0x83,0xec,0x78,
        0x49,0x89,0xf6,0x48,0x89,0xfb,0xf3,0x0f,0x11,0x45,0xd0,
        0xf3,0x0f,0x11,0x45,0xd4,0xf3,0x0f,0x11,0x4d,0xcc,
        0xf3,0x0f,0x11,0x4d,0xd8,0xf3,0x0f,0x11,0x55,0xc8,
        0xf3,0x0f,0x11,0x55,0xdc,0xf3,0x0f,0x11,0x5d,0xc4,
        0xf3,0x0f,0x11,0x5d,0xe0,0x88,0x55,0xe7,0xe8};
    static const unsigned char button_prefix[] = {
        0x55,0x48,0x89,0xe5,0x41,0x57,0x41,0x56,0x41,0x55,0x41,0x54,0x53,
        0x48,0x83,0xec,0x68,0x45,0x89,0xcc,0x45,0x89,0xc7,0x48,0x89,0xf3,
        0x49,0x89,0xfe,0x48,0x8d,0x75,0xcc,0x89,0x16,0x48,0x8d,0x55,0xd0,
        0x89,0x0a,0x44,0x88,0x7d,0xd6,0x44,0x88,0x65,0xd7,0xe8};
    static const unsigned char helper_prefix[] = {
        0x55,0x48,0x89,0xe5,0x41,0x56,0x53,0x48,0x89,0xd3,0x49,0x89,0xf6,0xe8};
    static const unsigned char move_scale[] = {
        0x48,0x85,0xc0,0x74,0x67,0xf3,0x0f,0x10,0x88,0xb0,0x04,0x00,0x00};
    static const unsigned char helper_scale[] = {
        0x48,0x85,0xc0,0x74,0x53,0xf3,0x0f,0x10,0x88,0xb0,0x04,0x00,0x00};
    if (macoblox_input_function_size(image, move) < 80 ||
        macoblox_input_function_size(image, button) < 80 ||
        !macoblox_input_bytes(move, move_prefix, sizeof(move_prefix)) ||
        !macoblox_input_bytes(button, button_prefix, sizeof(button_prefix)) ||
        !macoblox_input_bytes(move + 67, move_scale, sizeof(move_scale))) return 0;
    const unsigned char *helper = (const unsigned char *)macoblox_input_relative(button + 50);
    unsigned long service = macoblox_input_relative(move + 63);
    return macoblox_input_range(image, service, 16, 5) &&
        macoblox_input_range(image, (unsigned long)helper, 32, 5) &&
        macoblox_input_function_size(image, helper) >= 32 &&
        macoblox_input_bytes(helper, helper_prefix, sizeof(helper_prefix)) &&
        macoblox_input_bytes(helper + 18, helper_scale, sizeof(helper_scale)) &&
        macoblox_input_relative(helper + 14) == service;
}

static void macoblox_input_jump(unsigned char *entry, const void *replacement) {
    static const unsigned char jump[] = {0xff,0x25,0x00,0x00,0x00,0x00};
    __builtin_memcpy(entry, jump, sizeof(jump));
    __builtin_memcpy(entry + sizeof(jump), &replacement, sizeof(replacement));
}

/* Called only during the injected constructor or before finishLaunching
 * enters client code. Entry writes must never run during event dispatch. */
static int macoblox_install_input_scale_hooks(void) {
    Class cls = objc_getClass("InputCapture");
    if (!cls) return 0; /* Its image may not have loaded yet. */
    Method move = macoblox_input_owned_method(cls, "handleMouseMoveEvent:", "v24@0:8@16");
    Method entered = macoblox_input_owned_method(cls, "handleMouseEntered:", "v24@0:8@16");
    Method button = macoblox_input_owned_method(cls, "handleMouseButtonEvent:isPressed:", "v28@0:8@16c24");
    Method software = macoblox_input_owned_method(cls, "transitionToSoftwareIconCursor", "i16@0:8");
    if (!move || !entered || !button || !software) return -1;
    MacOBloxInputImage image = {0};
    if (!macoblox_input_image(&image) || !macoblox_input_find_dpi_override(&image)) return -1;
    unsigned char *move_wrapper = (unsigned char *)macoblox_input_find_wrapper(
        &image, move, "handleMouseMoveEvent:", 0);
    unsigned char *button_wrapper = (unsigned char *)macoblox_input_find_wrapper(
        &image, button, "handleMouseButtonEvent:isPressed:", 1);
    if (!move_wrapper || !button_wrapper || move_wrapper == button_wrapper ||
        macoblox_input_find_wrapper(&image, entered, "handleMouseEntered:", 0) != move_wrapper ||
        macoblox_input_find_wrapper(&image, software, "transitionToSoftwareIconCursor", 0) != move_wrapper)
        return -1;
    void *move_source, *button_source, *move_inner, *button_inner;
    if (!macoblox_input_wrapper(&image, move_wrapper, 0, &move_source, &move_inner) ||
        !macoblox_input_wrapper(&image, button_wrapper, 1, &button_source, &button_inner) ||
        move_source != button_source ||
        !macoblox_input_inner_abi(&image, move_inner, button_inner)) return -1;
    int page_size = getpagesize();
    if (page_size < 4096 || page_size > 65536 || (page_size & (page_size - 1))) return -1;
    unsigned long mask = (unsigned long)page_size - 1;
    unsigned long pages[2] = {(unsigned long)move_wrapper & ~mask,
                              (unsigned long)button_wrapper & ~mask};
    if (((unsigned long)move_wrapper & mask) > mask - 13 ||
        ((unsigned long)button_wrapper & mask) > mask - 13 ||
        !macoblox_input_range(&image, pages[0], page_size, 5) ||
        !macoblox_input_range(&image, pages[1], page_size, 5)) return -1;
    unsigned int page_count = pages[0] == pages[1] ? 1 : 2;
    for (unsigned int index = 0; index < page_count; ++index)
        if (mprotect((void *)pages[index], page_size, 7)) {
            for (unsigned int previous = 0; previous < index; ++previous)
                if (mprotect((void *)pages[previous], page_size, 5))
                    write_str("[MacOBlox] UI scale: client text protection restore failed\n");
            return -2;
        }
    macoblox_mouse_source = move_source;
    macoblox_mouse_move_inner = (void (*)(void *, const void *, int, float, float, float, float))move_inner;
    macoblox_mouse_button_inner = (void (*)(void *, const void *, int, int, int, int, int))button_inner;
    __atomic_thread_fence(__ATOMIC_RELEASE);
    macoblox_input_jump(move_wrapper, (const void *)macoblox_scaled_mouse_move);
    macoblox_input_jump(button_wrapper, (const void *)macoblox_scaled_mouse_button);
    __builtin___clear_cache((char *)move_wrapper, (char *)move_wrapper + 14);
    __builtin___clear_cache((char *)button_wrapper, (char *)button_wrapper + 14);
    int restored = 1;
    for (unsigned int index = 0; index < page_count; ++index)
        if (mprotect((void *)pages[index], page_size, 5)) restored = 0;
    /* Both adapters are safe passthroughs at 1x if permission restoration
     * fails. Keep synthetic UI scaling disabled in that failure case. */
    if (!restored)
        write_str("[MacOBlox] UI scale disabled: client text protection restore failed; mouse adapters remain 1x\n");
    return restored ? 1 : -2;
}
#else
static int macoblox_install_input_scale_hooks(void) { return -1; }
#endif
#endif
