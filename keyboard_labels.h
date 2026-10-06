#ifndef MACOBLOX_KEYBOARD_LABELS_H
#define MACOBLOX_KEYBOARD_LABELS_H

/* A valid Carbon 'uchr' ANSI fallback resource. Key labels are independent
 * of AppKit's actual text-input events. The former zero-filled layout and
 * successful empty UCKeyTranslate result broke Roblox's key-label queries,
 * including the default ProximityPrompt UI. */
typedef struct {
    unsigned short format, version;
    unsigned int feature_offset, keyboard_count;
    struct {
        unsigned int first, last, modifiers, tables, states, terminators, sequences;
    } keyboard;
    struct {
        unsigned short format, default_table;
        unsigned int count;
        unsigned char table[256];
    } modifiers;
    struct {
        unsigned short format, size;
        unsigned int count, offsets[4];
    } index;
    unsigned short characters[4][128];
} MacOBloxKeyboardLabels;

static unsigned short macoblox_ansi_character(unsigned int key, int shifted, int caps) {
    static const unsigned short plain[128] = {
        [0]='a', [1]='s', [2]='d', [3]='f', [4]='h', [5]='g',
        [6]='z', [7]='x', [8]='c', [9]='v', [10]=0x00a7, [11]='b',
        [12]='q', [13]='w', [14]='e', [15]='r', [16]='y', [17]='t',
        [18]='1', [19]='2', [20]='3', [21]='4', [22]='6', [23]='5',
        [24]='=', [25]='9', [26]='7', [27]='-', [28]='8', [29]='0',
        [30]=']', [31]='o', [32]='u', [33]='[', [34]='i', [35]='p',
        [36]='\r', [37]='l', [38]='j', [39]='\'', [40]='k', [41]=';',
        [42]='\\', [43]=',', [44]='/', [45]='n', [46]='m', [47]='.',
        [48]='\t', [49]=' ', [50]='`', [51]=8, [52]=3, [53]=27,
        [64]=0xf714, [65]='.', [67]='*', [69]='+', [71]=0x001b,
        [75]='/', [76]=3, [78]='-', [79]=0xf715, [80]=0xf716,
        [81]='=', [82]='0', [83]='1', [84]='2', [85]='3', [86]='4',
        [87]='5', [88]='6', [89]='7', [90]=0xf717, [91]='8', [92]='9',
        [93]='\\', [94]='_', [95]=',', [96]=0xf708, [97]=0xf709,
        [98]=0xf70a, [99]=0xf706, [100]=0xf70b, [101]=0xf70c,
        [103]=0xf70e, [105]=0xf710, [106]=0xf713, [107]=0xf711,
        [109]=0xf70d, [111]=0xf70f, [113]=0xf712, [114]=0xf746,
        [115]=0xf729, [116]=0xf72c, [117]=0x007f, [118]=0xf707,
        [119]=0xf72b, [120]=0xf705, [121]=0xf72d, [122]=0xf704,
        [123]=0xf702, [124]=0xf703, [125]=0xf701, [126]=0xf700,
    };
    unsigned short c = plain[key];
    if (c >= 'a' && c <= 'z')
        return shifted != caps ? c - 'a' + 'A' : c;
    if (!shifted || key >= 65) return c;
    switch (c) {
        case '1': return '!'; case '2': return '@'; case '3': return '#';
        case '4': return '$'; case '5': return '%'; case '6': return '^';
        case '7': return '&'; case '8': return '*'; case '9': return '(';
        case '0': return ')'; case '=': return '+'; case '-': return '_';
        case '[': return '{'; case ']': return '}'; case '\\': return '|';
        case ';': return ':'; case '\'': return '"'; case ',': return '<';
        case '.': return '>'; case '/': return '?'; case '`': return '~';
        case 0x00a7: return 0x00b1;
        default: return c;
    }
}

static void macoblox_build_keyboard_labels(MacOBloxKeyboardLabels *layout) {
    *layout = (MacOBloxKeyboardLabels){0};
    layout->format = 0x1002;
    layout->keyboard_count = 1;
    layout->keyboard.last = 0xffffffffU;
    layout->keyboard.modifiers = __builtin_offsetof(MacOBloxKeyboardLabels, modifiers);
    layout->keyboard.tables = __builtin_offsetof(MacOBloxKeyboardLabels, index);
    layout->modifiers.format = 0x3001;
    layout->modifiers.count = 256;
    layout->index.format = 0x4001;
    layout->index.size = 128;
    layout->index.count = 4;
    for (unsigned int modifiers = 0; modifiers < 256; modifiers++) {
        /* Carbon modifiers are already shifted right by eight: Shift=2,
         * Caps Lock=4. Command does not alter a key's display label. */
        layout->modifiers.table[modifiers] = ((modifiers & 2) ? 1 : 0) |
                                            ((modifiers & 4) ? 2 : 0);
    }
    for (unsigned int table = 0; table < 4; table++) {
        layout->index.offsets[table] = __builtin_offsetof(MacOBloxKeyboardLabels, characters) +
                                      table * sizeof layout->characters[0];
        for (unsigned int key = 0; key < 128; key++)
            layout->characters[table][key] = macoblox_ansi_character(key, table & 1, !!(table & 2));
    }
}

/* Recognize the resource shape before interpreting any of its offsets.
 * CFData copies of our resource have the same layout. Other real resources
 * are handled by the underlying Carbon implementation. */
static int macoblox_is_keyboard_labels(const void *data) {
    const MacOBloxKeyboardLabels *layout = data;
    return layout && layout->format == 0x1002 && !layout->version &&
        layout->keyboard_count == 1 && !layout->feature_offset &&
        layout->keyboard.first == 0 && layout->keyboard.last == 0xffffffffU &&
        layout->keyboard.modifiers == __builtin_offsetof(MacOBloxKeyboardLabels, modifiers) &&
        layout->keyboard.tables == __builtin_offsetof(MacOBloxKeyboardLabels, index) &&
        !layout->keyboard.states && !layout->keyboard.terminators && !layout->keyboard.sequences;
}

static int macoblox_translate_keyboard_label(const MacOBloxKeyboardLabels *layout,
        unsigned short key, unsigned short action, unsigned int modifiers,
        unsigned int *dead_state, unsigned long capacity,
        unsigned long *length, unsigned short *output) {
    if (!length) return -50; /* paramErr */
    *length = 0;
    if (!layout || !output || !dead_state || key >= 128 || action > 3) return -50;
    if (layout->modifiers.format != 0x3001 || layout->modifiers.count != 256 ||
        layout->index.format != 0x4001 || layout->index.size != 128 || layout->index.count != 4)
        return -50;
    unsigned int table = layout->modifiers.table[modifiers & 255];
    if (table >= 4 || layout->index.offsets[table] !=
        __builtin_offsetof(MacOBloxKeyboardLabels, characters) + table * sizeof layout->characters[0])
        return -50;
    *dead_state = 0;
    unsigned short character = layout->characters[table][key];
    if (!character) return 0; /* Modifier-only keys have no printable label. */
    if (!capacity) return -25340; /* kUCOutputBufferTooSmall */
    output[0] = character;
    *length = 1;
    return 0;
}
#endif
