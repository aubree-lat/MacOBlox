/* Kicking threads out of lost Darling waits.
 *
 * Darling sometimes loses the wakeup of a thread that sleeps in darlingserver
 * (psynch mutex and condition waits): the thread sleeps on although the lock
 * is free or the condition was signalled. A signal ends such a wait early. A
 * mutex wait then re-checks the lock and waits again while it is held; a
 * condition wait returns as a spurious wakeup (both checked in Darling with a
 * test program). Two watchdogs kick, both run by the watchdog thread in
 * net_trace.c every 100 ms:
 *  - net_trace.c: a UDP reader thread that leaves queued datagrams unread;
 *  - here: a thread waiting for a mutex (darling_fixes.c) for longer than
 *    250 ms. Such waits are futex waits now, which do not lose wakeups, so
 *    these kicks mostly serve to report where the thread waits; only after
 *    10 s does the wait fall back to Darling's own (psynch).
 *
 * The signal is SIGURG, which nothing else in the client uses. Roblox installs
 * its own handler on SIGUSR2 at startup (the one it also has on SIGTERM,
 * SIGHUP, SIGPIPE...), so the old SIGUSR2 kick reached whichever handler had
 * been installed last. It goes to the thread's Linux id with a direct Linux
 * tgkill, as exit_compat.c calls exit_group: pthread_kill with the pthread_t
 * of a thread that has exited reads freed memory, a stale id only gives ESRCH.
 *
 * When asked, the handler records only the interrupted RIP. It neither
 * dereferences application stack pointers nor enters pthread/loader APIs.
 * The watchdog prints the raw address without taking the dynamic loader lock.
 * MACOBLOX_NO_KICK=1 turns the kicks off (stalls and long mutex waits are
 * still logged, without the location).
 *
 * A kick must only reach a thread that is still in the wait it was meant
 * for: a condition wait that is not lost returns early when a signal comes,
 * and some of Roblox's callers do not cope with that (see the untimed waits
 * in darling_fixes.c). The watchdog checks again right before each kick. */
typedef unsigned long size_t;
struct darwin_sigaction_t { void (*handler)(int, void *, void *); unsigned int mask; int flags; };

extern char *getenv(const char *);
extern int sigaction(int, const struct darwin_sigaction_t *, struct darwin_sigaction_t *);
extern int pthread_key_create(unsigned long *, void (*)(void *));
extern void *pthread_getspecific(unsigned long);
extern int pthread_setspecific(unsigned long, const void *);
extern unsigned long long mach_absolute_time(void);
extern int snprintf(char *, size_t, const char *, ...);
extern long write(int, const void *, size_t);

static long linux_syscall3(long number, long a, long b, long c) {
    long result;
    __asm__ volatile("syscall" : "=a"(result) : "a"(number), "D"(a), "S"(b), "d"(c) : "rcx", "r11", "memory");
    return result;
}
#define LINUX_GETPID 39
#define LINUX_GETTID 186
#define LINUX_TGKILL 234
#define LINUX_SIGURG 23
#define DARWIN_SIGURG 16
#define DARWIN_SA_SIGINFO 0x0040

static int kicks_enabled = 1;
static unsigned long tid_key;
static volatile int tid_key_ready;

/* The Linux id of the calling thread, cached in a pthread TSD slot (not a
 * __thread variable, see darling_fixes.c). */
long macoblox_thread_id(void) {
    if (!tid_key_ready)
        return linux_syscall3(LINUX_GETTID, 0, 0, 0);
    long tid = (long)pthread_getspecific(tid_key);
    if (!tid) {
        tid = linux_syscall3(LINUX_GETTID, 0, 0, 0);
        pthread_setspecific(tid_key, (void *)tid);
    }
    return tid;
}

/* ------------------------------------------------------------- location */

/* Location requests, one per thread asked. Only the watchdog thread asks and
 * reads; the kicked thread's handler fills its slot in. */
#define LOCATION_SLOTS 4
#define LOCATION_EXPIRES_NS 1000000000ULL
enum { LOCATION_FREE, LOCATION_PENDING, LOCATION_WRITING, LOCATION_READY, LOCATION_RESERVED };
static struct {
    volatile unsigned int state;
    volatile long tid;
    unsigned long long asked;
    unsigned long long instruction;
} locations[LOCATION_SLOTS];

static void kick_handler(int signal, void *info, void *context) {
    (void)signal;
    (void)info;
    long self = linux_syscall3(LINUX_GETTID, 0, 0, 0);
    for (int slot = 0; slot < LOCATION_SLOTS; slot++) {
        if (__atomic_load_n(&locations[slot].state, __ATOMIC_ACQUIRE) != LOCATION_PENDING ||
            __atomic_load_n(&locations[slot].tid, __ATOMIC_RELAXED) != self)
            continue;
        unsigned int expected = LOCATION_PENDING;
        if (!__atomic_compare_exchange_n(&locations[slot].state, &expected, LOCATION_WRITING,
                                         0, __ATOMIC_ACQUIRE, __ATOMIC_RELAXED))
            continue;
        // The watchdog may have reassigned an expired pending slot just before
        // this CAS. Verify its new owner while the slot is exclusively ours.
        if (__atomic_load_n(&locations[slot].tid, __ATOMIC_RELAXED) != self) {
            __atomic_store_n(&locations[slot].state, LOCATION_PENDING, __ATOMIC_RELEASE);
            continue;
        }
        unsigned long long instruction = 0;
        if (context) {
            const unsigned long long* uc = context;
            const unsigned long long* mc = (const unsigned long long*)uc[6];
            if (mc && uc[5] >= 19 * sizeof(*mc))
                instruction = mc[18];
        }
        locations[slot].instruction = instruction;
        __atomic_store_n(&locations[slot].state, LOCATION_READY, __ATOMIC_RELEASE);
        return;
    }
}

/* Only the watchdog reserves/expires slots. A signal writer owns its slot
 * until publication, so expiration can never overwrite an in-flight result. */
static int ask_location(long tid, unsigned long long now) {
    for (int slot = 0; slot < LOCATION_SLOTS; slot++) {
        unsigned int state = __atomic_load_n(&locations[slot].state, __ATOMIC_ACQUIRE);
        if (state == LOCATION_FREE || __atomic_load_n(&locations[slot].tid, __ATOMIC_RELAXED) != tid)
            continue;
        if (state == LOCATION_WRITING || state == LOCATION_RESERVED)
            return 1;
        unsigned int expected = state;
        if (!__atomic_compare_exchange_n(&locations[slot].state, &expected, LOCATION_RESERVED,
                                         0, __ATOMIC_ACQUIRE, __ATOMIC_RELAXED))
            return 1;
        // A new locating kick must not consume a ready result from an earlier
        // wait by the same thread, even if that result has not expired yet.
        locations[slot].asked = now;
        locations[slot].instruction = 0;
        __atomic_store_n(&locations[slot].state, LOCATION_PENDING, __ATOMIC_RELEASE);
        return 1;
    }
    for (int slot = 0; slot < LOCATION_SLOTS; slot++) {
        unsigned int state = __atomic_load_n(&locations[slot].state, __ATOMIC_ACQUIRE);
        if (state == LOCATION_WRITING || state == LOCATION_RESERVED ||
            (state != LOCATION_FREE && now - locations[slot].asked <= LOCATION_EXPIRES_NS))
            continue;
        unsigned int expected = state;
        if (!__atomic_compare_exchange_n(&locations[slot].state, &expected, LOCATION_RESERVED,
                                         0, __ATOMIC_ACQUIRE, __ATOMIC_RELAXED))
            continue;
        locations[slot].asked = now;
        locations[slot].instruction = 0;
        __atomic_store_n(&locations[slot].tid, tid, __ATOMIC_RELAXED);
        __atomic_store_n(&locations[slot].state, LOCATION_PENDING, __ATOMIC_RELEASE);
        return 1;
    }
    return 0;
}

__attribute__((constructor)) static void install_kick_handler(void) {
    const char *off = getenv("MACOBLOX_NO_KICK");
    kicks_enabled = !(off && off[0] && off[0] != '0');
    if (pthread_key_create(&tid_key, 0) == 0)
        tid_key_ready = 1;
    /* No SA_RESTART: the point is to interrupt the wait. */
    struct darwin_sigaction_t action = {kick_handler, 0, DARWIN_SA_SIGINFO};
    sigaction(DARWIN_SIGURG, &action, 0);
}

/* Kick `tid`; with `locate`, also ask it to record where it is (see
 * macoblox_located). Called by the watchdog thread only. */
int macoblox_kick(long tid, int locate) {
    if (tid <= 0)
        return -1;
    if (locate)
        ask_location(tid, mach_absolute_time());
    if (!kicks_enabled)
        return 0;
    return (int)linux_syscall3(LINUX_TGKILL, linux_syscall3(LINUX_GETPID, 0, 0, 0), tid, LINUX_SIGURG);
}

int macoblox_kicks_enabled(void) { return kicks_enabled; }

/* Consume a published location without loader locks or arbitrary stack
 * reads. A raw RIP remains useful with the launch log's executable slide. */
int macoblox_located(long tid, char *out, size_t size) {
    if (!size)
        return 0;
    for (int slot = 0; slot < LOCATION_SLOTS; slot++) {
        if (__atomic_load_n(&locations[slot].state, __ATOMIC_ACQUIRE) != LOCATION_READY ||
            __atomic_load_n(&locations[slot].tid, __ATOMIC_RELAXED) != tid)
            continue;
        unsigned int expected = LOCATION_READY;
        if (!__atomic_compare_exchange_n(&locations[slot].state, &expected, LOCATION_RESERVED,
                                         0, __ATOMIC_ACQUIRE, __ATOMIC_RELAXED))
            continue;
        unsigned long long instruction = locations[slot].instruction;
        if (instruction)
            snprintf(out, size, "RIP=0x%llx", instruction);
        else
            snprintf(out, size, "(register context unavailable)");
        __atomic_store_n(&locations[slot].tid, 0, __ATOMIC_RELAXED);
        __atomic_store_n(&locations[slot].state, LOCATION_FREE, __ATOMIC_RELEASE);
        return 1;
    }
    return 0;
}

/* ------------------------------------------------------- mutex waits */

#define MAX_WAITERS 256
#define WAIT_KICK_NS 250000000ULL
#define WAIT_KICK_MAX_NS 1000000000ULL
enum { NOT_REPORTED, LOCATING, REPORTED };
static struct {
    volatile long tid;
    volatile unsigned long long since, next_kick, interval, asked;
    volatile int report;
} waiters[MAX_WAITERS];
static volatile long lock_kicks, lock_waits_seen;

/* darling_fixes.c calls these around a mutex wait that has to sleep. */
int macoblox_wait_begin(void) {
    long tid = macoblox_thread_id();
    for (int i = 0; i < MAX_WAITERS; i++) {
        if (__sync_bool_compare_and_swap(&waiters[i].tid, 0, tid)) {
            unsigned long long now = mach_absolute_time();
            waiters[i].interval = WAIT_KICK_NS;
            waiters[i].next_kick = now + WAIT_KICK_NS;
            waiters[i].report = NOT_REPORTED;
            __sync_synchronize();
            waiters[i].since = now;
            return i;
        }
    }
    return -1;
}

void macoblox_wait_end(int slot) {
    if (slot < 0)
        return;
    waiters[slot].since = 0;
    __sync_synchronize();
    waiters[slot].tid = 0;
}

static void report_wait(long tid, unsigned long long waited, const char *what, const char *where) {
    char line[1400];
    int length = snprintf(line, sizeof line, "[MacOBlox Lock] thread %ld waiting %llu ms for a mutex, %s%s%s\n",
                          tid, waited / 1000000ULL, what, where ? "; at: " : "", where ? where : "");
    if (length > 0)
        write(2, line, (size_t)length < sizeof line ? (size_t)length : sizeof line - 1);
}

/* Called by the watchdog every tick: kick waits longer than 250 ms, then
 * every 0.5 s, 1 s, 1 s ... while they last. The first waits are logged,
 * with the waiting thread's location when it records one within a second. */
void macoblox_kick_stuck_waiters(unsigned long long now) {
    for (int i = 0; i < MAX_WAITERS; i++) {
        long tid = waiters[i].tid;
        unsigned long long since = waiters[i].since;
        if (!tid || !since || now < since)
            continue;
        if (waiters[i].report == LOCATING) {
            char where[1200];
            if (macoblox_located(tid, where, sizeof where)) {
                waiters[i].report = REPORTED;
                report_wait(tid, now - since, "kicked", where);
            } else if (now - waiters[i].asked > LOCATION_EXPIRES_NS) {
                waiters[i].report = REPORTED;
                report_wait(tid, now - since, "kicked", "(not recorded)");
            }
        }
        if (now < waiters[i].next_kick)
            continue;
        if (waiters[i].tid != tid || waiters[i].since != since)
            continue; /* the wait ended during the report above */
        if (waiters[i].report == NOT_REPORTED) {
            long seen = __sync_add_and_fetch(&lock_waits_seen, 1);
            int log = seen <= 20 || seen % 100 == 0;
            if (log && !kicks_enabled)
                report_wait(tid, now - since, "not kicked (MACOBLOX_NO_KICK)", 0);
            waiters[i].report = log && kicks_enabled ? LOCATING : REPORTED;
            waiters[i].asked = now;
        }
        unsigned long long interval = waiters[i].interval * 2;
        waiters[i].interval = interval > WAIT_KICK_MAX_NS ? WAIT_KICK_MAX_NS : interval;
        waiters[i].next_kick = now + waiters[i].interval;
        if (!kicks_enabled)
            continue;
        if (waiters[i].tid != tid || waiters[i].since != since)
            continue;
        __sync_add_and_fetch(&lock_kicks, 1);
        macoblox_kick(tid, waiters[i].report == LOCATING);
    }
}

long macoblox_lock_kick_count(void) { return lock_kicks; }
