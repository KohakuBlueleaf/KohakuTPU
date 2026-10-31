/* Kernel console: each byte to the node's console register, and the same
 * bytes into a log ring in private DRAM that a host reads while the node runs
 * or after it stops or hangs: a 64-byte header (abi.h KOS_LOG_*: byte count,
 * what the node is doing, a heartbeat), bytes from +64, wrapping. All of it
 * through the uncached alias, so DRAM holds every byte as soon as it is
 * written. */
#include "kernel.h"
#include <kos/start.h>

static volatile uint64_t *log_hdr;
static volatile uint8_t *log_bytes;
static uint64_t log_cap;
static uint64_t where[3];
#ifdef KOS_LOCAL_LOG
static uint64_t switches;
void k_log_sync(void)
{
    kos_where(log_hdr, where[0], where[1], where[2]);
    log_hdr[KOS_LOG_SWITCHES] = switches;
}
#endif

void k_log_init(void)
{
    log_hdr = (volatile uint64_t *)((kn.priv + kn.log_off) | HW_UNC);
    log_bytes = (volatile uint8_t *)log_hdr + 64;
    log_cap = KOS_LOG_SIZE - 64;
    for (unsigned i = 0; i < 8; ++i) {
        log_hdr[i] = 0;
    }
}

volatile uint64_t *k_log_hdr(void) { return log_hdr; }

void k_where(uint64_t code, uint64_t a, uint64_t b)
{
    where[0] = code;
    where[1] = a;
    where[2] = b;
#ifdef KOS_LOCAL_LOG
    if (code != KOS_W_USER) {
        k_log_sync();
    }
#else
    kos_where(log_hdr, code, a, b);
#endif
}

/* A wait that ends puts back what the node was doing before it. */
void k_where_wait(uint64_t code, uint64_t a, uint64_t b) { kos_where(log_hdr, code, a, b); }
void k_where_back(void) { kos_where(log_hdr, where[0], where[1], where[2]); }

void k_switched(void)
{
#ifdef KOS_LOCAL_LOG
    ++switches;
#else
    static uint64_t n;
    log_hdr[KOS_LOG_SWITCHES] = ++n;
#endif
}

void k_write(const char *s, uint64_t len)
{
    for (uint64_t i = 0; i < len; ++i) {
        hw_wr(HW_R_CONSOLE, (unsigned char)s[i]);
    }
    if (log_hdr) {
#ifdef KOS_LOCAL_LOG
        k_log_sync();
#endif
        uint64_t c = log_hdr[KOS_LOG_COUNT];
        for (uint64_t i = 0; i < len; ++i) {
            log_bytes[(c + i) % log_cap] = (uint8_t)s[i];
        }
        log_hdr[KOS_LOG_COUNT] = c + len;
    }
}

#ifdef KOS_SMALL_CALL
__attribute__((minsize))
#endif
void k_printf(const char *fmt, ...)
{
    char b[192];
    va_list ap;
    va_start(ap, fmt);
    int n = kos_vsnprintf(b, sizeof b, fmt, ap);
    va_end(ap);
    k_write(b, n < (int)sizeof b ? (uint64_t)n : sizeof b - 1);
}
