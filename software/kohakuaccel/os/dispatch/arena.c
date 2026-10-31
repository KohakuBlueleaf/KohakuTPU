/* The arena: this node's memory for buffers that units, the mover and CPU
 * tasks share. Chunks are SYS_MMAP ranges (physically contiguous) found by
 * SYS_VTOP once; buffers are extents of a chunk, 64-byte granular (one
 * writer per L1 line), named by their span offset. Free extents stay sorted
 * by (chunk, offset) and merge with their neighbours. */
#include "kds_int.h"

#define GRAIN 64UL

static int add_chunk(struct kds_node *k, uint64_t size)
{
    unsigned c = 0;
    while (c < KDS_CHUNKS && k->chunk[c].size) {
        ++c;
    }
    if (c == KDS_CHUNKS || k->nfree == KDS_EXTENTS) {
        return KOS_ENOMEM;
    }
    size = KOS_ALIGN_UP(size, 4096);
    void *va = kos_mmap_flags(size, (k->flags & KDS_F_UNCACHED) ? KOS_MMAP_UNCACHED : 0);
    if (!va) {
        return KOS_ENOMEM;
    }
    int64_t pa = kos_vtop(va, size);
    if (pa < 0) {
        kos_printf("kds: node %u: chunk %p of %lu bytes not contiguous (%ld)\n", k->me, va, size, pa);
        return KOS_ENOMEM;
    }
    /* size last: a client thread of this node (kds_va) takes a chunk with a size as whole */
    k->chunk[c].va = (uint64_t)va;
    k->chunk[c].off = (uint64_t)pa - k->span[k->me];
    __asm__ volatile("" ::: "memory");
    k->chunk[c].size = size;
    /* chunks are numbered in no order: insert by (chunk, offset) */
    unsigned i = 0;
    while (i < k->nfree && k->fr[i].chunk < c) {
        ++i;
    }
    memmove(&k->fr[i + 1], &k->fr[i], (k->nfree - i) * sizeof k->fr[0]);
    k->fr[i] = (struct kds_extent){c, 0, 0, size};
    ++k->nfree;
    return KOS_OK;
}

int kds_arena_init(struct kds_node *k, uint64_t bytes)
{
    k->chunk_bytes = bytes;
    return add_chunk(k, bytes);
}

static int fit(struct kds_node *k, uint64_t size)
{
    int best = -1;
    for (unsigned i = 0; i < k->nfree; ++i) {
        if (k->fr[i].size < size) {
            continue;
        }
        if (!(k->flags & KDS_F_BESTFIT)) {
            return (int)i;
        }
        if (best < 0 || k->fr[i].size < k->fr[best].size) {
            best = (int)i;
        }
    }
    return best;
}

int64_t kds_arena_alloc(struct kds_node *k, uint64_t bytes, unsigned owner, kds_ref *ref)
{
    uint64_t size = KOS_ALIGN_UP(bytes ? bytes : 1, GRAIN);
    if (k->nused == KDS_EXTENTS) {
        return KOS_ENOMEM;
    }
    int i = fit(k, size);
    if (i < 0) {
        int st = add_chunk(k, size > k->chunk_bytes ? size : k->chunk_bytes);
        if (st) {
            return st;
        }
        i = fit(k, size);
        if (i < 0) {
            return KOS_ENOMEM;
        }
    }
    struct kds_extent *f = &k->fr[i];
    k->used[k->nused++] = (struct kds_extent){f->chunk, owner, f->off, size};
    *ref = KDS_REF(k->me, k->chunk[f->chunk].off + f->off);
    f->off += size;
    f->size -= size;
    if (!f->size) {
        memmove(f, f + 1, (k->nfree - (unsigned)i - 1) * sizeof *f);
        --k->nfree;
    }
    return KOS_OK;
}

/* Give extent e back: in order, merged with the free neighbours in its chunk. */
static int give_back(struct kds_node *k, struct kds_extent e)
{
    unsigned i = 0;
    while (i < k->nfree && (k->fr[i].chunk < e.chunk || (k->fr[i].chunk == e.chunk && k->fr[i].off < e.off))) {
        ++i;
    }
    int left = KDS_MUT != 3 && i > 0 && k->fr[i - 1].chunk == e.chunk && k->fr[i - 1].off + k->fr[i - 1].size == e.off;
    int right = KDS_MUT != 3 && i < k->nfree && k->fr[i].chunk == e.chunk && e.off + e.size == k->fr[i].off;
    if (left && right) {
        k->fr[i - 1].size += e.size + k->fr[i].size;
        memmove(&k->fr[i], &k->fr[i + 1], (k->nfree - i - 1) * sizeof k->fr[0]);
        --k->nfree;
    } else if (left) {
        k->fr[i - 1].size += e.size;
    } else if (right) {
        k->fr[i].off = e.off;
        k->fr[i].size += e.size;
    } else {
        if (k->nfree == KDS_EXTENTS) {
            return KOS_ENOMEM;
        }
        memmove(&k->fr[i + 1], &k->fr[i], (k->nfree - i) * sizeof k->fr[0]);
        k->fr[i] = e;
        ++k->nfree;
    }
    return KOS_OK;
}

static int release_used(struct kds_node *k, unsigned u)
{
    struct kds_extent e = k->used[u];
    k->used[u] = k->used[--k->nused];
    return give_back(k, e);
}

int kds_arena_free(struct kds_node *k, kds_ref ref)
{
    if (KDS_REF_NODE(ref) != k->me) {
        return KOS_EINVAL;
    }
    for (unsigned u = 0; u < k->nused; ++u) {
        struct kds_extent *e = &k->used[u];
        if (k->chunk[e->chunk].off + e->off == KDS_REF_OFF(ref)) {
            return release_used(k, u);
        }
    }
    return KOS_ENOENT;
}

int64_t kds_arena_free_owner(struct kds_node *k, unsigned owner)
{
    int64_t n = 0;
    for (unsigned u = 0; u < k->nused;) {
        if (k->used[u].owner == owner) {
            release_used(k, u);
            ++n;
        } else {
            ++u;
        }
    }
    return n;
}

int64_t kds_arena_trim(struct kds_node *k)
{
    int64_t bytes = 0;
    for (unsigned i = 0; i < k->nfree;) {
        struct kds_extent *f = &k->fr[i];
        struct kds_chunk *c = &k->chunk[f->chunk];
        uint64_t size = c->size;
        if (f->off || f->size != size) {
            ++i;
            continue;
        }
        c->size = 0; /* gone for kds_va before the pages go */
        __asm__ volatile("" ::: "memory");
        if (kos_munmap((void *)c->va, size)) {
            c->size = size;
            ++i;
            continue;
        }
        bytes += (int64_t)size;
        memmove(f, f + 1, (k->nfree - i - 1) * sizeof *f);
        --k->nfree;
    }
    return bytes;
}

void kds_arena_stat_of(struct kds_node *k, struct kds_arena_stat *s)
{
    memset(s, 0, sizeof *s);
    for (unsigned c = 0; c < KDS_CHUNKS; ++c) {
        s->chunks += k->chunk[c].size != 0;
        s->bytes += k->chunk[c].size;
    }
    for (unsigned i = 0; i < k->nfree; ++i) {
        s->free += k->fr[i].size;
        s->largest = k->fr[i].size > s->largest ? k->fr[i].size : s->largest;
    }
    s->used_extents = k->nused;
    s->free_extents = k->nfree;
}

/* A reference of this node: its user address in a chunk or the landing zone. */
void *kds_arena_va(struct kds_node *k, kds_ref ref)
{
    uint64_t off = KDS_REF_OFF(ref);
    if (KDS_REF_NODE(ref) != k->me) {
        return 0;
    }
    for (unsigned c = 0; c < KDS_CHUNKS; ++c) {
        struct kds_chunk *ch = &k->chunk[c];
        if (ch->size && off >= ch->off && off < ch->off + ch->size) {
            return (void *)(ch->va + (off - ch->off));
        }
    }
    uint64_t land = k->spansz[k->me] - KOS_LAND_SIZE;
    if (off >= land && off < k->spansz[k->me]) {
        return (void *)(KOS_UV_LAND + (off - land));
    }
    return 0;
}
