/* Region heaps (ka/os/heap.h). Plain C over the caller's table: no HAL, so the
 * same file builds natively for the trace test against heap.py. */
#include <ka/os/heap.h>
#include <ka/package/status.h>

static int pow2(uint64_t v) { return v && !(v & (v - 1)); }

int ka_heap_init(struct ka_heap *h, uint64_t base, uint64_t bytes, uint64_t granule,
                 struct ka_block *table, uint32_t cap)
{
    if (!pow2(granule) || granule < 8 || (base & (granule - 1)) || cap < 3) {
        return KA_ST_BAD_ARG;
    }
    bytes &= ~(granule - 1);
    if (!bytes) {
        return KA_ST_BAD_ARG;
    }
    h->base = base;
    h->bytes = bytes;
    h->granule = granule;
    h->b = table;
    h->cap = cap;
    h->n = 1;
    h->used = h->peak = 0;
    h->live = h->fails = 0;
    table[0] = (struct ka_block){.off = 0, .len = bytes, .used = 0, .tag = 0};
    return 0;
}

/* Open `k` entries at index `at`, shifting the rest up. */
static void open_at(struct ka_heap *h, uint32_t at, uint32_t k)
{
    for (uint32_t i = h->n; i-- > at;) {
        h->b[i + k] = h->b[i];
    }
    h->n += k;
}

static void close_at(struct ka_heap *h, uint32_t at)
{
    for (uint32_t i = at; i + 1 < h->n; ++i) {
        h->b[i] = h->b[i + 1];
    }
    --h->n;
}

int ka_heap_alloc(struct ka_heap *h, uint64_t bytes, uint64_t align, uint32_t tag,
                  uint64_t *addr)
{
    if (!bytes || (align && !pow2(align))) {
        return KA_ST_BAD_ARG;
    }
    uint64_t g = h->granule;
    if (bytes > h->bytes) {
        ++h->fails;
        return KA_ST_NO_MEMORY;
    }
    uint64_t len = (bytes + g - 1) & ~(g - 1);
    uint64_t a = align > g ? align : g;
    for (uint32_t i = 0; i < h->n; ++i) {
        struct ka_block *f = &h->b[i];
        if (f->used || f->len < len) {
            continue;
        }
        uint64_t at = ((h->base + f->off + a - 1) & ~(a - 1)) - h->base;
        uint64_t pad = at - f->off;
        if (pad > f->len || f->len - pad < len) {
            continue;
        }
        uint64_t rest = f->len - pad - len;
        uint32_t need = (pad != 0) + (rest != 0);
        if (h->n + need > h->cap) {
            ++h->fails;
            return KA_ST_NO_SLOTS;
        }
        uint64_t off0 = f->off;
        uint32_t u = i + (pad != 0);
        open_at(h, i + 1, need);
        if (pad) {
            h->b[i] = (struct ka_block){.off = off0, .len = pad, .used = 0, .tag = 0};
        }
        h->b[u] = (struct ka_block){.off = at, .len = len, .used = 1, .tag = tag};
        if (rest) {
            h->b[u + 1] = (struct ka_block){.off = at + len, .len = rest, .used = 0, .tag = 0};
        }
        h->used += len;
        ++h->live;
        if (h->used > h->peak) {
            h->peak = h->used;
        }
        *addr = h->base + at;
        return 0;
    }
    ++h->fails;
    return KA_ST_NO_MEMORY;
}

static int index_of(const struct ka_heap *h, uint64_t addr)
{
    if (addr < h->base || addr - h->base >= h->bytes) {
        return -1;
    }
    uint64_t off = addr - h->base;
    uint32_t lo = 0, hi = h->n;
    while (lo < hi) {
        uint32_t mid = (lo + hi) / 2;
        if (h->b[mid].off < off) {
            lo = mid + 1;
        } else {
            hi = mid;
        }
    }
    return (lo < h->n && h->b[lo].off == off && h->b[lo].used) ? (int)lo : -1;
}

int ka_heap_free(struct ka_heap *h, uint64_t addr)
{
    int k = index_of(h, addr);
    if (k < 0) {
        return KA_ST_BAD_FREE;
    }
    uint32_t i = (uint32_t)k;
    h->used -= h->b[i].len;
    --h->live;
    h->b[i].used = 0;
    h->b[i].tag = 0;
    if (i + 1 < h->n && !h->b[i + 1].used) {
        h->b[i].len += h->b[i + 1].len;
        close_at(h, i + 1);
    }
    if (i > 0 && !h->b[i - 1].used) {
        h->b[i - 1].len += h->b[i].len;
        close_at(h, i);
    }
    return 0;
}

const struct ka_block *ka_heap_find(const struct ka_heap *h, uint64_t addr)
{
    int k = index_of(h, addr);
    return k < 0 ? 0 : &h->b[k];
}

void ka_heap_stats(const struct ka_heap *h, struct ka_heap_stats *s)
{
    s->free = h->bytes - h->used;
    s->largest = 0;
    for (uint32_t i = 0; i < h->n; ++i) {
        if (!h->b[i].used && h->b[i].len > s->largest) {
            s->largest = h->b[i].len;
        }
    }
    s->used = h->used;
    s->peak = h->peak;
    s->live = h->live;
    s->blocks = h->n;
    s->fails = h->fails;
}

uint32_t ka_heap_check(const struct ka_heap *h)
{
    uint64_t at = 0, used = 0;
    uint32_t live = 0;
    for (uint32_t i = 0; i < h->n; ++i) {
        const struct ka_block *b = &h->b[i];
        if (b->off != at || !b->len || (b->len & (h->granule - 1)) ||
            (i && !b->used && !h->b[i - 1].used)) {
            return i + 1;
        }
        at += b->len;
        if (b->used) {
            used += b->len;
            ++live;
        }
    }
    if (at != h->bytes || used != h->used || live != h->live || h->n > h->cap) {
        return h->n + 1;
    }
    return 0;
}
