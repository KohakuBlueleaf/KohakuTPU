/* The node's private DRAM span: a page allocator for stacks, mmap and kernel
 * buffers; the console log and the landing zone at its tail. Mappings go
 * through L0 (k_map). The kernel runs untranslated: it names its span by
 * physical address, and user memory through k_user_pa / the copy helpers. */
#include "kernel.h"

#define MAX_FRAMES 8192
static uint64_t bitmap[MAX_FRAMES / 64];
static uint64_t umap[MAX_FRAMES / 64];
static uint64_t nframes, nfree, hint;
static uint64_t mmap_next;

struct knode kn;
uint64_t k_user_satp;
#ifdef KOS_XLAT
static uint64_t map_generation = 1;
extern uint64_t kos_walk(uint64_t va, uint64_t root, uint64_t alias);
static void mappings_changed(void)
{
#if !defined(KOS_HOT_MUT) || KOS_HOT_MUT != 4
    ++map_generation;
#endif
}
#else
#define mappings_changed() ((void)0)
#endif

extern char __u_img_size[];

#ifdef KOS_SMALL_CALL
#define MEM_COLD __attribute__((minsize))
#else
#define MEM_COLD
#endif

MEM_COLD void k_mem_init(void)
{
    kn.land_off = KOS_PRIV_LAND_OFF(kn.priv_size);
    kn.log_off = KOS_PRIV_LOG_OFF(kn.priv_size);
    kn.frames_end = (kn.flags & KOS_F_PT_DRAM) ? KOS_PRIV_PT_OFF(kn.priv_size) : kn.log_off;
    nframes = kn.frames_end >> 12;
    if (nframes > MAX_FRAMES) {
        nframes = MAX_FRAMES;
    }
    nfree = nframes;
    for (uint64_t i = 0; i < ((uint64_t)__u_img_size >> 12); ++i) { /* the user image */
        bitmap[i / 64] |= 1UL << (i % 64);
        --nfree;
    }
    mmap_next = KOS_UV_MMAP + (uint64_t)kn.me * 0x10000000UL;
}

uint64_t k_frames_free(void) { return nfree; }

MEM_COLD uint64_t k_frame(void)
{
    for (uint64_t k = 0; k < nframes; ++k) {
        uint64_t i = (hint + k) % nframes;
        if (!(bitmap[i / 64] >> (i % 64) & 1)) {
            bitmap[i / 64] |= 1UL << (i % 64);
            --nfree;
            hint = i + 1;
            uint64_t pa = kn.priv + (i << 12);
            memset((void *)pa, 0, 4096);
            return pa;
        }
    }
    return 0;
}

MEM_COLD void k_frame_free(uint64_t pa)
{
    uint64_t i = (pa - kn.priv) >> 12;
    if (i < nframes && (bitmap[i / 64] >> (i % 64) & 1)) {
        bitmap[i / 64] &= ~(1UL << (i % 64));
        umap[i / 64] &= ~(1UL << (i % 64));
        ++nfree;
    }
}

MEM_COLD int k_pa_ok(uint64_t pa, uint64_t bytes)
{
    if (!bytes || pa + bytes < pa) {
        return 0;
    }
    uint64_t end = pa + bytes - 1;
    if ((pa - (kn.priv + kn.land_off) < KOS_USER_LAND_SIZE &&
         end - (kn.priv + kn.land_off) < KOS_USER_LAND_SIZE) ||
        (pa - kn.shared < kn.shared_size && end - kn.shared < kn.shared_size)) {
        return 1;
    }
    for (uint64_t p = pa & ~0xfffUL; p <= end; p += 4096) {
        uint64_t i = (p - kn.priv) >> 12;
        if (i >= nframes || !(umap[i / 64] >> (i % 64) & 1)) {
            return 0;
        }
    }
    return 1;
}

int k_map(uint64_t va, uint64_t pa, uint64_t size, unsigned attr)
{
    mappings_changed();
    return (int)k_l0(KOS_M_MAP, va, pa, size, attr).st;
}

int k_user_range(uint64_t va, uint64_t len)
{
    return va >= KOS_UV_TEXT && va + len >= va && va + len <= (1UL << 38);
}

uint64_t k_shared_unc(uint64_t uva)
{
    if (!kn.shared_size) {
        return 0;
    }
    for (int k = 0; k < 2; ++k) {
        uint64_t base = k ? KOS_UV_SHUNC : KOS_UV_SHARED;
        if (uva >= base && uva < base + kn.shared_size) {
            return (kn.shared + (uva - base)) | HW_UNC;
        }
    }
    return 0;
}

#ifdef KOS_LAUNCH_RING
struct kos_launch_rec k_lring_cur;
#endif

/* Byte-wise through k_user_pa, a page at a time, kernel buffer k to the user
 * when `out`, else the other way; KOS_EFAULT on a hole. */
static __attribute__((noinline)) int copy_user(uint64_t uva, uint8_t *k, uint64_t n, int out)
{
#ifdef KOS_LAUNCH_RING
    uint64_t t0 = hw_cycles();
#endif
    while (n) {
        uint64_t pa = k_user_range(uva, 1) ? k_user_pa(uva) : 0, chunk = 4096 - (uva & 0xfff);
        if (!pa) {
            return KOS_EFAULT;
        }
        chunk = chunk < n ? chunk : n;
        memcpy(out ? (void *)pa : k, out ? k : (const void *)pa, chunk);
        k += chunk;
        uva += chunk;
        n -= chunk;
    }
#ifdef KOS_LAUNCH_RING
    k_lring_cur.cyc_xlat += hw_cycles() - t0;
#endif
    return KOS_OK;
}

int k_copy_in(void *dst, uint64_t uva, uint64_t n) { return copy_user(uva, dst, n, 0); }

int k_copy_out(uint64_t uva, const void *src, uint64_t n) { return copy_user(uva, (uint8_t *)(uintptr_t)src, n, 1); }

/* A user string into dst[max], NUL-terminated; its length or KOS_EFAULT. */
MEM_COLD int64_t k_copy_str(char *dst, uint64_t uva, uint64_t max)
{
    uint64_t i = 0;
    while (i + 1 < max) {
        uint64_t pa = k_user_range(uva + i, 1) ? k_user_pa(uva + i) : 0;
        if (!pa) {
            return KOS_EFAULT;
        }
        for (uint64_t end = i + 4096 - ((uva + i) & 0xfff); i < end && i + 1 < max; ++i, ++pa) {
            dst[i] = *(const char *)pa;
            if (!dst[i]) {
                return (int64_t)i;
            }
        }
    }
    dst[max - 1] = 0;
    return (int64_t)(max - 1);
}

/* The physical address of a user-image address (text, rodata, the app table). */
uint64_t k_uimg(uint64_t va) { return kn.priv + (va - KOS_UV_TEXT); }

static uint64_t stack_base(struct thread *t)
{
    return KOS_UV_STACK + (uint64_t)kn.me * 0x200000UL + (uint64_t)t->slot * KOS_STACK_SPAN;
}

/* KOS_STACK_SIZE of fresh pages under a guard gap; sp at the top. */
MEM_COLD int k_stack_map(struct thread *t)
{
    uint64_t top = stack_base(t) + KOS_STACK_SPAN;
    uint64_t pages = KOS_STACK_SIZE >> 12;
    for (uint64_t i = 0; i < pages; ++i) {
        uint64_t pa = k_frame();
        if (!pa || k_map(top - (i + 1) * 4096, pa, 4096, KOS_MAP_R | KOS_MAP_W | KOS_MAP_U)) {
            t->stack_pages = i + (pa != 0);
            k_stack_unmap(t);
            return KOS_ENOMEM;
        }
    }
    t->stack_pages = pages;
    t->x[2] = top;
    return KOS_OK;
}

#ifdef KOS_COMPACT_XLAT
__attribute__((noinline))
#endif
uint64_t k_user_pa(uint64_t va)
{
#ifdef KOS_XLAT
    uint64_t page = va & ~0xfffUL;
    if (kcur && kcur->tlb_gen == map_generation && kcur->tlb_va == page) {
        return kcur->tlb_pa | (va & 0xfff);
    }
    uint64_t root = (k_user_satp << 20) >> 8;
    uint64_t pte = kos_walk(va, root, kn.flags & KOS_F_PT_DRAM ? HW_UNC : 0);
#else
    uint64_t pte = k_l0(KOS_M_QUERY, va & ~0xfffUL, 0, 0, 0).val;
#endif
    if (!(pte & 1)) {
        return 0;
    }
    uint64_t pa = ((pte >> 10) << 12) & ((1UL << 40) - 1);
#ifdef KOS_XLAT
    if (kcur) {
        kcur->tlb_va = page;
        kcur->tlb_pa = pa;
        kcur->tlb_gen = map_generation;
    }
#endif
    return pa | (va & 0xfff);
}

MEM_COLD void k_stack_unmap(struct thread *t)
{
    uint64_t top = stack_base(t) + KOS_STACK_SPAN;
    for (uint64_t i = 0; i < t->stack_pages; ++i) {
        uint64_t va = top - (i + 1) * 4096;
        uint64_t pa = k_user_pa(va) & ~0xfffUL & ~HW_UNC;
        if (pa) {
            mappings_changed();
            k_l0(KOS_M_UNMAP, va, 4096, 0, 0);
            k_frame_free(pa);
        }
    }
    t->stack_pages = 0;
}

static int used(uint64_t i) { return bitmap[i / 64] >> (i % 64) & 1; }

/* `n` physically contiguous zeroed pages; their physical address, 0 if none. */
static MEM_COLD uint64_t frames(uint64_t n, int user)
{
    uint64_t first = 0, run = 0;
    for (uint64_t i = 0; i < nframes && run < n; ++i) {
        run = used(i) ? 0 : run + 1;
        first = run == 1 ? i : first;
    }
    if (!n || run < n) {
        return 0;
    }
    for (uint64_t k = 0; k < n; ++k) {
        uint64_t i = first + k;
        bitmap[i / 64] |= 1UL << (i % 64);
        if (user) {
            umap[i / 64] |= 1UL << (i % 64);
        }
        --nfree;
        memset((void *)(kn.priv + (i << 12)), 0, 4096);
    }
    return kn.priv + (first << 12);
}

MEM_COLD void *k_alloc(uint64_t bytes)
{
    uint64_t pa = frames(KOS_ALIGN_UP(bytes, 4096) >> 12, 0);
    if (!pa) {
        k_panic("no %lu bytes for kernel tables", bytes);
    }
    return (void *)pa;
}

/* Private pages for the caller's node, physically contiguous (so the mover
 * and units can take the range whole), zeroed, mapped cached or (flags
 * KOS_MMAP_UNCACHED) through the uncached alias. */
MEM_COLD int64_t k_mmap(uint64_t size, unsigned flags)
{
    uint64_t n = KOS_ALIGN_UP(size, 4096) >> 12;
    uint64_t pa = frames(n, 1);
    if (!pa) {
        return KOS_ENOMEM;
    }
    uint64_t va = mmap_next;
    unsigned attr = KOS_MAP_R | KOS_MAP_W | KOS_MAP_U | ((flags & KOS_MMAP_UNCACHED) ? KOS_MAP_UNCACHED : 0);
    if (k_map(va, pa, n << 12, attr)) {
        return KOS_ENOMEM;
    }
    mmap_next += (n + 1) << 12;
    return (int64_t)va;
}

/* Unmap [va, va + size) of the mmap area, physically contiguous from pa, and
 * free its frames. */
MEM_COLD int64_t k_munmap(uint64_t va, uint64_t pa, uint64_t size)
{
    uint64_t lo = KOS_UV_MMAP + (uint64_t)kn.me * 0x10000000UL;
    if (!pa || (va | size) & 0xfff || va < lo || va + size > mmap_next) {
        return KOS_EINVAL;
    }
    mappings_changed();
    int64_t st = k_l0(KOS_M_UNMAP, va, size, 0, 0).st;
    for (uint64_t off = 0; !st && off < size; off += 4096) {
        k_frame_free(pa + off);
    }
    return st;
}
