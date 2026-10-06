/* Minimal X11 client: hides/shows the cursor with XFixes, and answers which
 * visual a window and the screen use (for picking EGL configs, gl_profile.c).
 *
 * Why: under Xwayland, XWarpPointer moves only the X server's pointer and the
 * next Wayland motion undoes it, so recentering during mouse lock bounced the
 * pointer. Xwayland emulates warps properly (locked pointer + relative motion)
 * while the X cursor is hidden with XFixes, as Wine does. Darling wraps
 * libX11 but not libXfixes, and sending the requests through Xlib internals
 * is unsafe (_XGetRequest/_XReply need Xlib's internal display lock, which
 * XLockDisplay does not take; a test hung in XUnlockDisplay). So this speaks
 * the X protocol directly on its own socket: setup, QueryExtension("XFIXES"),
 * XFixes QueryVersion, then HideCursor/ShowCursor on the root window. The
 * hide is per-client and ends automatically if this connection closes.
 *
 * The connection authenticates like libX11, with the display's
 * MIT-MAGIC-COOKIE-1 from the Xauthority file (Xorg sessions of most display
 * managers accept nothing else); without one it connects without, which
 * servers that allow the local user (Xwayland under GNOME, niri) accept. */

#include "shim_lock.h"

typedef unsigned int socklen_t;
typedef long ssize_t;
typedef unsigned long size_t;
struct darwin_sockaddr_un { unsigned char len, family; char path[104]; };
extern int socket(int, int, int);
extern int connect(int, const void *, socklen_t);
extern ssize_t write(int, const void *, size_t);
extern ssize_t read(int, void *, size_t);
extern int close(int);
extern int open(const char *, int, ...);
extern int fcntl(int, int, ...);
extern int *__error(void);
extern void *malloc(size_t);
extern void free(void *);
extern char *getenv(const char *);
extern int pthread_create(void **, const void *, void *(*)(void *), void *);
extern int pthread_detach(void *);

#define DARWIN_O_RDONLY 0
#define DARWIN_O_CLOEXEC 0x1000000
#define DARWIN_F_SETFD 2
#define DARWIN_FD_CLOEXEC 1
#define DARWIN_F_GETFL 3
#define DARWIN_F_SETFL 4
#define DARWIN_O_NONBLOCK 4
#define DARWIN_EINTR 4
#define DARWIN_EAGAIN 35
#define DARWIN_EINPROGRESS 36
#define DARWIN_EALREADY 37
#define DARWIN_EISCONN 56
#define LINUX_EINTR 4
#define LINUX_EAGAIN 11
#define LINUX_POLLIN 1
#define LINUX_POLLOUT 4
#define X_REPLY_BYTES (1024UL * 1024UL)
#define X_SETUP_MILLISECONDS 250
#define X_TRANSITION_MILLISECONDS 100

static int x_socket = -1;
static unsigned char xfixes_opcode;
static unsigned int root_window;

struct x_linux_timespec { long seconds, nanoseconds; };
struct x_linux_pollfd { int fd; short events, revents; };

static long long x_now(void) {
    struct x_linux_timespec time;
    long result;
    __asm__ volatile("syscall" : "=a"(result)
        : "a"(228L /* Linux clock_gettime */), "D"(1L /* CLOCK_MONOTONIC */), "S"(&time)
        : "rcx", "r11", "cc", "memory");
    if (result < 0 || time.seconds < 0 || time.seconds > 9223372035L ||
        time.nanoseconds < 0 || time.nanoseconds >= 1000000000L)
        return -1;
    return (long long)time.seconds * 1000000000LL + time.nanoseconds;
}

static long long x_deadline(int milliseconds) {
    long long now = x_now();
    return now < 0 || now > 9223372036854775807LL - milliseconds * 1000000LL
        ? -1 : now + milliseconds * 1000000LL;
}

static int x_left(long long deadline) {
    long long now = x_now();
    if (now < 0 || deadline <= now)
        return 0;
    return (int)((deadline - now + 999999LL) / 1000000LL);
}

static int wait_on(int fd, short events, long long deadline) {
    for (;;) {
        int milliseconds = x_left(deadline);
        if (!milliseconds)
            return 0;
        struct x_linux_pollfd pollfd = {fd, events, 0};
        long result;
        __asm__ volatile("syscall" : "=a"(result)
            : "a"(7L /* Linux poll */), "D"(&pollfd), "S"(1L), "d"((long)milliseconds)
            : "rcx", "r11", "cc", "memory");
        if (result == -LINUX_EINTR || result == 0)
            continue;
        if (result < 0 || (pollfd.revents & 32 /* POLLNVAL */))
            return 0;
        if (pollfd.revents & (events | 8 /* POLLERR */ | 16 /* POLLHUP */))
            return 1; /* The next operation reports EOF or the socket error. */
    }
}

/* Darling exposes native Linux socket descriptors, but its Darwin send
 * flags drop MSG_NOSIGNAL and SO_NOSIGPIPE is a no-op. Use the kernel ABI
 * for I/O: neither a stopped X server nor a closed peer can block us or
 * deliver SIGPIPE through the translated write() path. */
static long socket_io(long call, int fd, void *data, size_t length, long flags) {
    long result;
    register long fourth __asm__("r10") = flags;
    register long fifth __asm__("r8") = 0;
    register long sixth __asm__("r9") = 0;
    __asm__ volatile("syscall" : "=a"(result)
        : "a"(call), "D"((long)fd), "S"(data), "d"(length),
          "r"(fourth), "r"(fifth), "r"(sixth)
        : "rcx", "r11", "cc", "memory");
    return result;
}

/* The helpers take the socket: the cursor thread keeps one connection open,
 * visual queries open their own. Every helper shares its caller's deadline. */
static int write_all_on(int fd, const void *data, size_t length, long long deadline) {
    const unsigned char *bytes = data;
    while (length) {
        if (!x_left(deadline))
            return 0;
        long written = socket_io(44 /* Linux sendto */, fd, (void *)bytes, length,
                                 0x40 /* MSG_DONTWAIT */ | 0x4000 /* MSG_NOSIGNAL */);
        if (written == -LINUX_EINTR)
            continue;
        if (written == -LINUX_EAGAIN) {
            if (!wait_on(fd, LINUX_POLLOUT, deadline))
                return 0;
            continue;
        }
        if (written <= 0)
            return 0;
        bytes += written;
        length -= (size_t)written;
    }
    return 1;
}

static int read_all_on(int fd, void *data, size_t length, long long deadline) {
    unsigned char *bytes = data;
    while (length) {
        if (!x_left(deadline))
            return 0;
        long got = socket_io(45 /* Linux recvfrom */, fd, bytes, length, 0x40 /* MSG_DONTWAIT */);
        if (got == -LINUX_EINTR)
            continue;
        if (got == -LINUX_EAGAIN) {
            if (!wait_on(fd, LINUX_POLLIN, deadline))
                return 0;
            continue;
        }
        if (got <= 0)
            return 0;
        bytes += got;
        length -= (size_t)got;
    }
    return 1;
}

/* Read the 32-byte reply to the last request, skipping events. */
static unsigned int little_endian32(const unsigned char *bytes) {
    return (unsigned int)bytes[0] | (unsigned int)bytes[1] << 8 |
        (unsigned int)bytes[2] << 16 | (unsigned int)bytes[3] << 24;
}

static int read_reply_on(int fd, unsigned char reply[32], long long deadline) {
    for (int guard = 0; guard < 256; guard++) {
        if (!read_all_on(fd, reply, 32, deadline))
            return 0;
        if (reply[0] == 0)
            return 0; /* X error */
        if (reply[0] == 1 || (reply[0] & 0x7F) == 35 /* GenericEvent */) {
            unsigned int units = little_endian32(reply + 4);
            if (units > (X_REPLY_BYTES - 32) / 4)
                return 0;
            size_t extra = (size_t)units * 4;
            unsigned char skip[256];
            while (extra) {
                size_t chunk = extra < sizeof skip ? extra : sizeof skip;
                if (!read_all_on(fd, skip, chunk, deadline))
                    return 0;
                extra -= chunk;
            }
            if (reply[0] == 1)
                return 1;
        }
    }
    return 0;
}

static int display_number_text(const char *display) {
    int number = 0;
    if (!display)
        return 0;
    while (*display && *display != ':')
        display++;
    if (*display == ':')
        display++;
    while (*display >= '0' && *display <= '9') {
        int digit = *display++ - '0';
        if (number > (2147483647 - digit) / 10)
            return -1;
        number = number * 10 + digit;
    }
    return number;
}

static int display_number(void) { return display_number_text(getenv("DISPLAY")); }

static int socket_error_on(int fd, long long deadline) {
    for (;;) {
        if (!x_left(deadline))
            return 0;
        int error = 0;
        unsigned int length = sizeof error;
        long result;
        register long fourth __asm__("r10") = (long)&error;
        register long fifth __asm__("r8") = (long)&length;
        __asm__ volatile("syscall" : "=a"(result)
            : "a"(55L /* Linux getsockopt */), "D"((long)fd),
              "S"(1L /* SOL_SOCKET */), "d"(4L /* SO_ERROR */), "r"(fourth), "r"(fifth)
            : "rcx", "r11", "cc", "memory");
        if (result == -LINUX_EINTR)
            continue;
        return result == 0 && length == sizeof error && error == 0;
    }
}

static int fcntl_on(int fd, int command, int value, long long deadline) {
    for (;;) {
        if (!x_left(deadline))
            return -1;
        int result = fcntl(fd, command, value);
        if (result < 0 && *__error() == DARWIN_EINTR)
            continue;
        return result;
    }
}

static int connect_display_on(int *out, long long deadline) {
    struct darwin_sockaddr_un address = {0};
    int number = display_number();
    if (number < 0 || !x_left(deadline))
        return 0;
    /* Darling's /tmp is private; the host's X socket is under SystemRoot. */
    const char prefix[] = "/Volumes/SystemRoot/tmp/.X11-unix/X";
    int length = 0;
    while (prefix[length]) {
        address.path[length] = prefix[length];
        length++;
    }
    char digits[12];
    int count = 0;
    do {
        digits[count++] = (char)('0' + number % 10);
        number /= 10;
    } while (number && count < 11);
    while (count)
        address.path[length++] = digits[--count];
    address.family = 1; /* AF_UNIX */
    address.len = (unsigned char)(2 + length + 1);
    int fd;
    do {
        if (!x_left(deadline))
            return 0;
        fd = socket(1, 1 /* SOCK_STREAM */, 0);
    } while (fd < 0 && *__error() == DARWIN_EINTR);
    if (fd < 0)
        return 0;
    /* A child process that inherited the cursor connection would keep the
     * cursor hidden after the game exits. */
    int flags = fcntl_on(fd, DARWIN_F_GETFL, 0, deadline);
    if (flags < 0 || fcntl_on(fd, DARWIN_F_SETFD, DARWIN_FD_CLOEXEC, deadline) < 0 ||
        fcntl_on(fd, DARWIN_F_SETFL, flags | DARWIN_O_NONBLOCK, deadline) < 0) {
        close(fd);
        return 0;
    }
    int connected = 0;
    while (x_left(deadline)) {
        if (connect(fd, &address, sizeof address) == 0) {
            connected = 1;
            break;
        }
        int error = *__error();
        if (error == DARWIN_EINTR)
            continue;
        if (error == DARWIN_EISCONN) {
            connected = 1;
            break;
        }
        if (error == DARWIN_EINPROGRESS || error == DARWIN_EALREADY)
            connected = wait_on(fd, LINUX_POLLOUT, deadline) && socket_error_on(fd, deadline);
        /* AF_UNIX EAGAIN means its listen queue was full, not an initiated
         * connection: never turn a zero SO_ERROR into a false success. */
        break;
    }
    if (!connected) {
        close(fd);
        return 0;
    }
    *out = fd;
    return 1;
}

/* ------------------------------------------------------ authorization */

#define COOKIE_NAME "MIT-MAGIC-COOKIE-1"
#define COOKIE_NAME_LENGTH 18
#define COOKIE_LENGTH 16
#define FAMILY_LOCAL 256
#define FAMILY_WILD 65535

/* One process lifetime lookup worker: file I/O never runs on a render/input
 * caller, and those callers only wait within their own operation deadline. */
static volatile int cookie_state; /* 0: not started, 1: loading, 2: found, -1: none, -2: unavailable */
static unsigned char cookie[COOKIE_LENGTH];
static char cookie_display[256], cookie_authority[1100], cookie_home[1100];

static int copy_environment(char *out, unsigned long capacity, const char *name) {
    const char *text = getenv(name);
    if (!text) {
        out[0] = 0;
        return 1;
    }
    for (unsigned long index = 0; index < capacity; index++) {
        out[index] = text[index];
        if (!out[index])
            return 1;
    }
    out[capacity - 1] = 0;
    return 0;
}

static int same_environment(const char *name, const char *captured) {
    const char *current = getenv(name);
    if (!current)
        current = "";
    for (unsigned long index = 0;; index++) {
        if (current[index] != captured[index])
            return 0;
        if (!captured[index])
            return 1;
    }
}

static void cookie_publish(int state) {
    __atomic_store_n(&cookie_state, state, __ATOMIC_RELEASE);
    macoblox_lock_futex((volatile unsigned int *)&cookie_state, 129 /* FUTEX_WAKE_PRIVATE */, 2147483647);
}

static int wait_cookie(long long deadline) {
    while (__atomic_load_n(&cookie_state, __ATOMIC_ACQUIRE) == 1) {
        long long now = x_now();
        if (now < 0 || deadline <= now)
            return 0;
        long long left = deadline - now;
        struct x_linux_timespec timeout = {(long)(left / 1000000000LL), (long)(left % 1000000000LL)};
        long result;
        register long fourth __asm__("r10") = (long)&timeout;
        __asm__ volatile("syscall" : "=a"(result)
            : "a"(202L /* Linux futex */), "D"(&cookie_state),
              "S"(128L /* FUTEX_WAIT_PRIVATE */), "d"(1L), "r"(fourth)
            : "rcx", "r11", "cc", "memory");
        if (result < 0 && result != -LINUX_EINTR && result != -LINUX_EAGAIN)
            return 0;
    }
    return 1;
}

static int same_text(const unsigned char *field, unsigned int length, const char *text) {
    unsigned int i = 0;
    for (; i < length && text[i]; i++)
        if (field[i] != (unsigned char)text[i])
            return 0;
    return i == length && !text[i];
}

static unsigned int big_endian16(const unsigned char *bytes) { return (unsigned int)bytes[0] << 8 | bytes[1]; }

/* The best MIT-MAGIC-COOKIE-1 in an Xauthority file for display `number`
 * on `host`: entries are a big-endian 16-bit family, then address, display
 * number, name and data, each a big-endian 16-bit length and the bytes. As
 * in libX11, a local entry for this host is used, or a wildcard one; a local
 * entry for another host name (the name changed since login) comes last. */
static int best_cookie(const unsigned char *file, unsigned long size, const char *number, const char *host,
                       unsigned char out[COOKIE_LENGTH]) {
    int best = 0;
    unsigned long at = 0;
    while (at + 2 <= size) {
        unsigned int family = big_endian16(file + at);
        at += 2;
        const unsigned char *field[4];
        unsigned int length[4];
        for (int i = 0; i < 4; i++) {
            if (at + 2 > size)
                return best;
            length[i] = big_endian16(file + at);
            at += 2;
            if (at + length[i] > size)
                return best;
            field[i] = file + at;
            at += length[i];
        }
        /* field: 0 address, 1 display number, 2 name, 3 data */
        if (!same_text(field[2], length[2], COOKIE_NAME) || length[3] != COOKIE_LENGTH ||
            (length[1] && !same_text(field[1], length[1], number)))
            continue;
        int score = family == FAMILY_LOCAL ? (host[0] && same_text(field[0], length[0], host) ? 3 : 1)
                  : family == FAMILY_WILD ? 2 : 0;
        if (score > best) {
            best = score;
            for (int i = 0; i < COOKIE_LENGTH; i++)
                out[i] = field[3][i];
        }
    }
    return best;
}

/* The host's name as libX11 in this process sees it (Linux uname). */
static void host_name(char out[65]) {
    char names[6 * 65];
    long result;
    __asm__ volatile("syscall" : "=a"(result) : "a"(63L /* Linux uname */), "D"(names) : "rcx", "r11", "cc", "memory");
    out[0] = 0;
    if (result == 0)
        for (int i = 0; i < 65; i++)
            if (!(out[i] = names[65 + i])) /* nodename */
                break;
    out[64] = 0;
}

/* Reads up to 64 KB of `path` (a path of the host when `host_path`: it is
 * reached through /Volumes/SystemRoot) into a malloc'ed buffer. */
static unsigned char *read_file(const char *path, const char *suffix, int host_path, unsigned long *size) {
    char full[1100];
    unsigned long used = 0;
    const char *parts[3] = {host_path ? "/Volumes/SystemRoot" : "", path, suffix};
    if (host_path) {
        const char *prefix = "/Volumes/SystemRoot/";
        int already = 1;
        for (int i = 0; prefix[i]; i++)
            if (path[i] != prefix[i]) { already = 0; break; }
        if (already)
            parts[0] = "";
    }
    for (int part = 0; part < 3; part++)
        for (const char *c = parts[part]; *c; c++) {
            if (used + 1 >= sizeof full)
                return 0;
            full[used++] = *c;
        }
    full[used] = 0;
    int fd;
    do {
        fd = open(full, DARWIN_O_RDONLY | DARWIN_O_CLOEXEC | DARWIN_O_NONBLOCK);
    } while (fd < 0 && *__error() == DARWIN_EINTR);
    if (fd < 0)
        return 0;
    unsigned char *data = malloc(1 << 16);
    unsigned long total = 0;
    while (data && total < (1 << 16)) {
        ssize_t got = read(fd, data + total, (1 << 16) - total);
        if (got < 0 && *__error() == DARWIN_EINTR)
            continue;
        if (got <= 0)
            break;
        total += (unsigned long)got;
    }
    close(fd);
    *size = total;
    return data;
}

/* The display's cookie, looked up once: $XAUTHORITY (a host path) or else
 * ~/.Xauthority, as libX11 does; inside Darling HOME is the prefix's
 * /Users/<name>, so both the host's and the prefix's copy are tried. */
static void *lookup_cookie(void *unused) {
    (void)unused;
    char number[12], host[65];
    int value = display_number_text(cookie_display), count = 0;
    if (value < 0) {
        cookie_publish(-2);
        return 0;
    }
    char digits[12];
    do {
        digits[count++] = (char)('0' + value % 10);
        value /= 10;
    } while (value && count < 11);
    for (int i = 0; i < count; i++)
        number[i] = digits[count - 1 - i];
    number[count] = 0;
    host_name(host);
    const char *authority = cookie_authority, *home = cookie_home;
    struct { const char *path, *suffix; int host_path; } files[3] = {{0, 0, 0}, {0, 0, 0}, {0, 0, 0}};
    if (authority[0] == '/')
        files[0].path = authority, files[0].suffix = "", files[0].host_path = 1;
    else if (home[0] == '/') {
        files[0].path = home, files[0].suffix = "/.Xauthority", files[0].host_path = 1;
        files[1].path = home, files[1].suffix = "/.Xauthority", files[1].host_path = 0;
    }
    unsigned char found[COOKIE_LENGTH];
    int state = -1;
    for (int i = 0; i < 3 && files[i].path && state < 0; i++) {
        unsigned long size = 0;
        unsigned char *data = read_file(files[i].path, files[i].suffix, files[i].host_path, &size);
        if (data && best_cookie(data, size, number, host, found))
            state = 2;
        free(data);
    }
    if (state == 2)
        for (int index = 0; index < COOKIE_LENGTH; index++)
            cookie[index] = found[index];
    cookie_publish(state);
    return 0;
}

/* Returns -1 if unavailable within this call's budget, 0 for no cookie,
 * or 1 with the selected key. Cookie bytes are immutable after publication. */
static int display_cookie(unsigned char out[COOKIE_LENGTH], long long deadline) {
    if (!x_left(deadline))
        return -1;
    int expected = 0;
    if (__atomic_compare_exchange_n(&cookie_state, &expected, 1, 0, __ATOMIC_ACQ_REL, __ATOMIC_ACQUIRE)) {
        void *thread;
        if (!copy_environment(cookie_display, sizeof cookie_display, "DISPLAY") ||
            !copy_environment(cookie_authority, sizeof cookie_authority, "XAUTHORITY") ||
            !copy_environment(cookie_home, sizeof cookie_home, "HOME") ||
            pthread_create(&thread, 0, lookup_cookie, 0) != 0) {
            cookie_publish(-2);
        } else {
            pthread_detach(thread);
        }
    }
    if (!wait_cookie(deadline))
        return -1;
    int state = __atomic_load_n(&cookie_state, __ATOMIC_ACQUIRE);
    if (state == -2 || !same_environment("DISPLAY", cookie_display) ||
        !same_environment("XAUTHORITY", cookie_authority) || !same_environment("HOME", cookie_home))
        return -1; /* Never authenticate another display with a stale key. */
    if (state < 0)
        return 0;
    for (int i = 0; i < COOKIE_LENGTH; i++)
        out[i] = cookie[i];
    return 1;
}

/* Connection setup; returns the first screen's root window and visual. */
static int setup_on(int fd, unsigned int *root, unsigned int *visual, long long deadline) {
    /* 'l' = little endian, protocol 11.0, then the authorization name and
     * data (lengths at 6 and 8), each padded to four bytes. */
    unsigned char request[12 + 20 + COOKIE_LENGTH] = {'l', 0, 11, 0, 0, 0, 0, 0, 0, 0, 0, 0};
    unsigned long request_length = 12;
    unsigned char key[COOKIE_LENGTH];
    int cookie_result = display_cookie(key, deadline);
    if (cookie_result < 0)
        return 0;
    if (cookie_result > 0) {
        request[6] = COOKIE_NAME_LENGTH;
        request[8] = COOKIE_LENGTH;
        for (int i = 0; i < COOKIE_NAME_LENGTH; i++)
            request[12 + i] = (unsigned char)COOKIE_NAME[i];
        for (int i = 0; i < COOKIE_LENGTH; i++)
            request[32 + i] = key[i];
        request_length = sizeof request;
    }
    unsigned char header[8];
    if (!write_all_on(fd, request, request_length, deadline) || !read_all_on(fd, header, sizeof header, deadline) ||
        header[0] != 1)
        return 0;
    unsigned int body_length = *(unsigned short *)(header + 6) * 4u;
    /* Not static: the cursor thread and a visual query can set up at once. */
    unsigned char *body = malloc(body_length ? body_length : 1);
    if (!body || !read_all_on(fd, body, body_length, deadline)) {
        free(body);
        return 0;
    }
    int ok = 0;
    unsigned int vendor_length = body_length >= 32 ? *(unsigned short *)(body + 16) : 0;
    unsigned int formats = body_length >= 32 ? body[21] : 0;
    unsigned int screen = 32 + ((vendor_length + 3) & ~3u) + 8 * formats;
    if (body_length >= 32 && body[20] && screen + 36 <= body_length) {
        *root = *(unsigned int *)(body + screen);
        if (visual)
            *visual = *(unsigned int *)(body + screen + 32);
        ok = 1;
    }
    free(body);
    return ok;
}

static int query_xfixes(long long deadline) {
    unsigned char request[16] = {98 /* QueryExtension */, 0, 4, 0, 6, 0, 0, 0,
                                 'X', 'F', 'I', 'X', 'E', 'S', 0, 0};
    unsigned char reply[32];
    if (!write_all_on(x_socket, request, sizeof request, deadline) || !read_reply_on(x_socket, reply, deadline) || !reply[8])
        return 0;
    xfixes_opcode = reply[9];
    unsigned char version[12] = {xfixes_opcode, 0 /* QueryVersion */, 3, 0, 5, 0, 0, 0, 0, 0, 0, 0};
    return write_all_on(x_socket, version, sizeof version, deadline) && read_reply_on(x_socket, reply, deadline);
}

int macoblox_raw_xfixes_open(void) {
    if (x_socket >= 0)
        return 1;
    long long deadline = x_deadline(X_SETUP_MILLISECONDS);
    if (!connect_display_on(&x_socket, deadline))
        return 0;
    if (!setup_on(x_socket, &root_window, 0, deadline) || !query_xfixes(deadline)) {
        close(x_socket);
        x_socket = -1;
        return 0;
    }
    return 1;
}

int macoblox_raw_xfixes_set_hidden(int hidden) {
    if (x_socket < 0)
        return 0;
    long long deadline = x_deadline(X_TRANSITION_MILLISECONDS);
    unsigned char request[8] = {xfixes_opcode, hidden ? 29 /* HideCursor */ : 30 /* ShowCursor */,
                                2, 0};
    *(unsigned int *)(request + 4) = root_window;
    /* The game and cursor worker use different X connections. A successful
     * socket write only queues the hide; acknowledge it after the server has
     * processed this connection's preceding requests, before recentering on
     * the game's connection. This round trip happens only on transitions. */
    unsigned char barrier[4] = {43 /* GetInputFocus */, 0, 1, 0};
    unsigned char reply[32];
    if (write_all_on(x_socket, request, sizeof request, deadline) &&
        write_all_on(x_socket, barrier, sizeof barrier, deadline) && read_reply_on(x_socket, reply, deadline))
        return 1;
    // Closing this per-client connection also ends any outstanding hide.
    close(x_socket);
    x_socket = -1;
    return 0;
}

/* Sample the physical cursor once at a CGAssociate lock transition. This
 * connection selects no events and owns no grab. It avoids accessing the
 * event owner's Xlib connection or asking AppKit to create a platform window
 * from the game thread. Root coordinates are converted on the owner thread. */
int macoblox_raw_x_pointer_snapshot(unsigned int *pointer_root, int *x, int *y) {
    int fd;
    unsigned int root;
    long long deadline = x_deadline(X_TRANSITION_MILLISECONDS);
    if (!pointer_root || !x || !y || !connect_display_on(&fd, deadline)) return 0;
    int ok = setup_on(fd, &root, 0, deadline);
    if (ok) {
        unsigned char request[8] = {38 /* QueryPointer */, 0, 2, 0};
        unsigned char reply[32];
        *(unsigned int *)(request + 4) = root;
        ok = write_all_on(fd, request, sizeof request, deadline) && read_reply_on(fd, reply, deadline);
        if (ok) {
            *pointer_root = *(unsigned int *)(reply + 8);
            *x = *(short *)(reply + 16);
            *y = *(short *)(reply + 18);
        }
    }
    close(fd);
    return ok;
}

/* The screen's default visual and, if `window` is not 0, that window's
 * visual (GetWindowAttributes). Opens and closes its own connection.
 * Returns 0 when the X server cannot be reached. */
int macoblox_raw_x_visuals(unsigned int window, unsigned int *root_visual, unsigned int *window_visual) {
    int fd;
    unsigned int root;
    long long deadline = x_deadline(X_SETUP_MILLISECONDS);
    if (!connect_display_on(&fd, deadline))
        return 0;
    int ok = setup_on(fd, &root, root_visual, deadline);
    if (ok && window && window_visual) {
        unsigned char request[8] = {3 /* GetWindowAttributes */, 0, 2, 0};
        unsigned char reply[32];
        *(unsigned int *)(request + 4) = window;
        ok = write_all_on(fd, request, sizeof request, deadline) && read_reply_on(fd, reply, deadline);
        if (ok)
            *window_visual = *(unsigned int *)(reply + 8);
    }
    close(fd);
    return ok;
}
