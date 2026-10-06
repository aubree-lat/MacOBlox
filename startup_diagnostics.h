#ifndef MACOBLOX_STARTUP_DIAGNOSTICS_H
#define MACOBLOX_STARTUP_DIAGNOSTICS_H

/* Included after the shim's Objective-C declarations and in_wt_init.
 * This opt-in trace records nib field names and call boundaries only. The
 * decoded values and object descriptions are deliberately never inspected. */
extern signed char class_addMethod(Class, SEL, IMP, const char*);
extern Method* class_copyMethodList(Class, unsigned int*);
extern unsigned int method_getNumberOfArguments(Method);
extern void method_getArgumentType(Method, unsigned int, char*, unsigned long);
extern void method_getReturnType(Method, char*, unsigned long);
extern void free(void*);

static IMP macoblox_startup_integer_original;
static IMP macoblox_startup_int_original;
static IMP macoblox_startup_double_original;
static IMP macoblox_startup_bool_c_original;
static IMP macoblox_startup_bool_b_original;
static IMP macoblox_startup_main_screen_original;
static volatile unsigned long macoblox_startup_trace_sequence;

typedef struct {
    unsigned long sequence;
    char key[96];
} MacOBloxStartupTrace;

static int macoblox_startup_type_is(const char* type, char kind) {
    return type && type[0] == kind && !type[1];
}

/* Check every ABI component before a function-pointer cast. In particular,
 * Cocoa BOOL ('c') and C99 bool ('B') use separate, exact replacements. */
static int macoblox_startup_method_types(Method method, char result,
                                          unsigned int arguments) {
    if (!method || method_getNumberOfArguments(method) != arguments)
        return 0;
    char type[96] = {0};
    method_getReturnType(method, type, sizeof(type));
    if (!macoblox_startup_type_is(type, result))
        return 0;
    for (unsigned int index = 0; index < arguments; index++) {
        type[0] = 0;
        method_getArgumentType(method, index, type, sizeof(type));
        if (!macoblox_startup_type_is(type, index == 1 ? ':' : '@'))
            return 0;
    }
    return 1;
}

static int macoblox_startup_hook(Class cls, const char* name, char result,
                                  unsigned int arguments, IMP replacement,
                                  IMP* original) {
    if (!cls)
        return 0;
    SEL selector = sel_registerName(name);
    Method method = class_getInstanceMethod(cls, selector);
    if (!macoblox_startup_method_types(method, result, arguments))
        return 0;
    const char* encoding = method_getTypeEncoding(method);
    IMP implementation = method_getImplementation(method);
    if (!encoding || !implementation)
        return 0;
    if (implementation == replacement)
        return *original != 0;
    *original = implementation;
    /* Shadow an inherited method on this class instead of changing NSCoder,
     * NSObject, or any other superclass shared by unrelated callers. */
    if (class_addMethod(cls, selector, replacement, encoding))
        return 1;
    unsigned int count = 0;
    Method* methods = class_copyMethodList(cls, &count);
    int owned = 0;
    for (unsigned int index = 0; methods && index < count; index++)
        if (methods[index] == method)
            owned = 1;
    free(methods);
    if (owned)
        method_setImplementation(method, replacement);
    else
        *original = 0;
    return owned;
}

static void macoblox_startup_key_name(id key, char* output,
                                       unsigned long capacity) {
    output[0] = 0;
    if (!key || capacity < 2)
        return;
    /* A diagnostic getter must not introduce or replace an application
     * exception. Only bounded identifier characters are copied from a key. */
    @try {
        const char* name = ((const char* (*)(id, SEL))objc_msgSend)(
            key, sel_registerName("UTF8String"));
        if (!name)
            return;
        unsigned long index = 0;
        for (; index + 1 < capacity && name[index]; index++) {
            char character = name[index];
            if (!((character >= 'a' && character <= 'z') ||
                  (character >= 'A' && character <= 'Z') ||
                  (character >= '0' && character <= '9') ||
                  character == '_' || character == '-' || character == '.')) {
                output[0] = 0;
                return;
            }
            output[index] = character;
        }
        output[index] = 0;
    } @catch (id exception) {
        (void)exception;
        output[0] = 0;
    }
}

static void macoblox_startup_trace_line(const char* selector, const char* phase,
                                         const MacOBloxStartupTrace* trace) {
    if (!trace->sequence)
        return;
    write_str("[MacOBlox Startup] call=");
    print_num((long long)trace->sequence);
    write_str(" "); write_str(selector);
    if (trace->key[0]) {
        write_str(" key="); write_str(trace->key);
    }
    write_str(" "); write_str(phase); write_str("\n");
}

static MacOBloxStartupTrace macoblox_startup_trace_begin(const char* selector,
                                                          id key) {
    MacOBloxStartupTrace trace = {0, {0}};
    if (!in_wt_init || !macoblox_crash_diagnostics_enabled())
        return trace;
    unsigned long sequence = __sync_add_and_fetch(&macoblox_startup_trace_sequence, 1);
    if (sequence > 1024) {
        if (sequence == 1025)
            write_str("[MacOBlox Startup] trace limit reached\n");
        return trace;
    }
    trace.sequence = sequence;
    macoblox_startup_key_name(key, trace.key, sizeof(trace.key));
    macoblox_startup_trace_line(selector, "entered", &trace);
    return trace;
}

/* The original calls have no catch block: their exceptions propagate with
 * their original identity, and an incomplete call remains visible in the log. */
#define MACOBLOX_STARTUP_DECODE(function, type, original, selector) \
    static type function(id self, SEL command, id key) { \
        MacOBloxStartupTrace trace = macoblox_startup_trace_begin(selector, key); \
        type value = ((type (*)(id, SEL, id))original)(self, command, key); \
        macoblox_startup_trace_line(selector, "completed", &trace); \
        return value; \
    }

MACOBLOX_STARTUP_DECODE(macoblox_startup_decode_integer, long long,
                         macoblox_startup_integer_original, "decodeIntegerForKey:")
MACOBLOX_STARTUP_DECODE(macoblox_startup_decode_int, int,
                         macoblox_startup_int_original, "decodeIntForKey:")
MACOBLOX_STARTUP_DECODE(macoblox_startup_decode_double, double,
                         macoblox_startup_double_original, "decodeDoubleForKey:")
MACOBLOX_STARTUP_DECODE(macoblox_startup_decode_bool_c, signed char,
                         macoblox_startup_bool_c_original, "decodeBoolForKey:")
MACOBLOX_STARTUP_DECODE(macoblox_startup_decode_bool_b, _Bool,
                         macoblox_startup_bool_b_original, "decodeBoolForKey:")
#undef MACOBLOX_STARTUP_DECODE

static id macoblox_startup_main_screen(id self, SEL command) {
    MacOBloxStartupTrace trace = macoblox_startup_trace_begin("+[NSScreen mainScreen]", 0);
    id result = ((id (*)(id, SEL))macoblox_startup_main_screen_original)(self, command);
    macoblox_startup_trace_line("+[NSScreen mainScreen]", "completed", &trace);
    return result;
}

static void macoblox_install_startup_diagnostics(void) {
    if (!macoblox_crash_diagnostics_enabled())
        return;
    unsigned int installed = 0;
    Class unarchiver = objc_getClass("NSKeyedUnarchiver");
    installed += macoblox_startup_hook(unarchiver, "decodeIntegerForKey:", 'q', 3,
        (IMP)macoblox_startup_decode_integer, &macoblox_startup_integer_original);
    installed += macoblox_startup_hook(unarchiver, "decodeIntForKey:", 'i', 3,
        (IMP)macoblox_startup_decode_int, &macoblox_startup_int_original);
    installed += macoblox_startup_hook(unarchiver, "decodeDoubleForKey:", 'd', 3,
        (IMP)macoblox_startup_decode_double, &macoblox_startup_double_original);
    if (!macoblox_startup_hook(unarchiver, "decodeBoolForKey:", 'c', 3,
        (IMP)macoblox_startup_decode_bool_c, &macoblox_startup_bool_c_original))
        installed += macoblox_startup_hook(unarchiver, "decodeBoolForKey:", 'B', 3,
            (IMP)macoblox_startup_decode_bool_b, &macoblox_startup_bool_b_original);
    else
        installed++;
    Class screen = objc_getClass("NSScreen");
    installed += macoblox_startup_hook(screen ? object_getClass((id)screen) : 0,
        "mainScreen", '@', 2, (IMP)macoblox_startup_main_screen,
        &macoblox_startup_main_screen_original);
    write_str("[MacOBlox Startup] diagnostic methods installed=");
    print_num(installed); write_str("/5\n");
}

#endif
