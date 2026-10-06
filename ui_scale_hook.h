#ifndef MACOBLOX_UI_SCALE_HOOK_H
#define MACOBLOX_UI_SCALE_HOOK_H
#include "ui_scale.h"

extern Method *class_copyMethodList(Class, unsigned int *);
extern SEL method_getName(Method);
extern double macoblox_dpi_scale(void);

static MacOBloxSurfaceSettings (*macoblox_original_surface_settings)(id, SEL);
static double macoblox_requested_surface_scale;
static int macoblox_surface_scale_install_state; /* 0 retry, 1 installing, 2 done */
static int macoblox_surface_scale_invalid_warned;
static int macoblox_surface_scale_applied_logged;
static int macoblox_surface_scale_override_warned;

static MacOBloxSurfaceSettings macoblox_scaled_surface_settings(id self, SEL cmd) {
    MacOBloxSurfaceSettings (*original)(id, SEL) = __atomic_load_n(
        &macoblox_original_surface_settings, __ATOMIC_ACQUIRE);
    /* The original IMP is published before the replacement can be called.
     * Original exceptions propagate normally; this hook adds no catch. */
    MacOBloxSurfaceSettings settings = original(self, cmd);
    float before = settings.scale;
    int allowed = macoblox_input_scale_allowed();
    int changed = settings.surface && settings.enabled && allowed
        ? macoblox_surface_settings_scale(&settings, macoblox_requested_surface_scale) : 0;
    /* Publish the factor actually returned, including float rounding. Until
     * a valid surface is transformed, installed input adapters remain 1x. */
    macoblox_input_scale_set(changed > 0 ? (double)settings.scale / before : 1.0);
    if (!allowed) {
        if (!__atomic_exchange_n(&macoblox_surface_scale_override_warned, 1, __ATOMIC_RELAXED))
            write_str("[MacOBlox] UI scale skipped: client DebugOverrideDPIScale is enabled\n");
    } else if (changed < 0) {
        if (!__atomic_exchange_n(&macoblox_surface_scale_invalid_warned, 1, __ATOMIC_RELAXED))
            write_str("[MacOBlox] UI scale: invalid original surface scale preserved\n");
    } else if (changed > 0 &&
               !__atomic_exchange_n(&macoblox_surface_scale_applied_logged, 1, __ATOMIC_RELAXED)) {
        write_str("[MacOBlox] Roblox UI scale and mouse position compensation applied\n");
    }
    return settings;
}

static void macoblox_install_surface_scale_hook(void) {
    int expected = 0;
    if (!__atomic_compare_exchange_n(&macoblox_surface_scale_install_state, &expected, 1,
                                     0, __ATOMIC_ACQUIRE, __ATOMIC_RELAXED))
        return;
    double requested = macoblox_dpi_scale();
    if (!__builtin_isfinite(requested) || requested <= 1.0 || requested > 4.0) {
        __atomic_store_n(&macoblox_surface_scale_install_state, 2, __ATOMIC_RELEASE);
        return;
    }
    Class cls = objc_getClass("RobloxPlayerAppDelegate");
    if (!cls) {
        /* The client class can load after the injected library constructor. */
        __atomic_store_n(&macoblox_surface_scale_install_state, 0, __ATOMIC_RELEASE);
        return;
    }
    SEL selector = sel_registerName("getSurfaceSettings");
    unsigned int count = 0;
    Method *methods = class_copyMethodList(cls, &count);
    Method method = 0;
    /* class_getInstanceMethod also searches superclasses. Replacing an
     * inherited method would change other classes using that superclass. */
    if (methods)
        for (unsigned int index = 0; index < count; ++index)
            if (method_getName(methods[index]) == selector) {
                method = methods[index];
                break;
            }
    free(methods);
    IMP original = method && macoblox_surface_settings_abi_matches(method_getTypeEncoding(method))
        ? method_getImplementation(method) : 0;
    if (!original || original == (IMP)macoblox_scaled_surface_settings) {
        write_str("[MacOBlox] UI scale skipped: unsupported client surface-settings method\n");
        __atomic_store_n(&macoblox_surface_scale_install_state, 2, __ATOMIC_RELEASE);
        return;
    }
    int input_hooks = macoblox_install_input_scale_hooks();
    if (input_hooks <= 0) {
        if (input_hooks < 0)
            write_str(input_hooks == -2
                ? "[MacOBlox] UI scale skipped: client mouse text protection unavailable\n"
                : "[MacOBlox] UI scale skipped: unsupported client mouse input ABI\n");
        __atomic_store_n(&macoblox_surface_scale_install_state,
                         input_hooks == 0 ? 0 : 2, __ATOMIC_RELEASE);
        return;
    }
    macoblox_requested_surface_scale = requested;
    __atomic_store_n(&macoblox_original_surface_settings,
                     (MacOBloxSurfaceSettings (*)(id, SEL))original, __ATOMIC_RELEASE);
    method_setImplementation(method, (IMP)macoblox_scaled_surface_settings);
    __atomic_store_n(&macoblox_surface_scale_install_state, 2, __ATOMIC_RELEASE);
    write_str("[MacOBlox] UI scale: guarded client mouse adapters installed\n");
}
#endif
