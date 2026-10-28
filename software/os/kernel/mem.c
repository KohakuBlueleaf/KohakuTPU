/* The node's private DRAM span: a page allocator for stacks, mmap and kernel
 * buffers; the console log and the landing zone at its tail. Mappings go
 * through L0 (k_map). The kernel runs untranslated: it names its span by
 * physical address, and user memory through k_user_pa / the copy helpers. */
#include "kernel.h"

#define MAX_FRAMES 8192
static uint64_t bitmap[MAX_FRAMES / 64];
static uint64_t nframes, nfree, hint;
static uint64_t mmap_next;

struct knode kn;
uint64_t k_user_satp;

extern char __u_img_size[];

void k_mem_init(void)
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

uint64_t k_frame(void)
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

void k_frame_free(uint64_t pa)
{
    uint64_t i = (pa - kn.priv) >> 12;
    if (i < nframes && (bitmap[i / 64] >> (i % 64) & 1)) {
        bitmap[i / 64] &= ~(1UL << (i % 64));
        ++nfree;
    }
}

int k_map(uint64_t va, uint64_t pa, uint64_t size, unsigned attr)
{
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

/* Byte-wise through k_user_pa, a page at a time; KOS_EFAULT on a hole. */
int k_copy_in(void *dst, uint64_t uva, uint64_t n)
{
    uint8_t *d = dst;
    while (n) {
        uint64_t pa = k_user_range(uva, 1) ? k_user_pa(uva) : 0, chunk = 4096 - (uva & 0xfff);
        if (!pa) {
            return KOS_EFAULT;
        }
        chunk = chunk < n ? chunk : n;
        memcpy(d, (const void *)pa, chunk);
        d += chunk;
        uva += chunk;
        n -= chunk;
    }
    return KOS_OK;
}

int k_copy_out(uint64_t uva, const void *src, uint64_t n)
{
    const uint8_t *s = src;
    while (n) {
        uint64_t pa = k_user_range(uva, 1) ? k_user_pa(uva) : 0, chunk = 4096 - (uva & 0xfff);
        if (!pa) {
            return KOS_EFAULT;
        }
        chunk = chunk < n ? chunk : n;
        memcpy((void *)pa, s, chunk);
        s += chunk;
        uva += chunk;
        n -= chunk;
    }
    return KOS_OK;
}

/* A user string into dst[max], NUL-terminated; its length or KOS_EFAULT. */
int64_t k_copy_str(char *dst, uint64_t uva, uint64_t max)
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
int k_stack_map(struct thread *t)
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

uint64_t k_user_pa(uint64_t va)
{
    uint64_t pte = k_l0(KOS_M_QUERY, va & ~0xfffUL, 0, 0, 0).val;
    if (!(pte & 1)) {
        return 0;
    }
    return (((pte >> 10) << 12) & ((1UL << 40) - 1)) | (va & 0xfff);
}

void k_stack_unmap(struct thread *t)
{
    uint64_t top = stack_base(t) + KOS_STACK_SPAN;
    for (uint64_t i = 0; i < t->stack_pages; ++i) {
        uint64_t va = top - (i + 1) * 4096;
        uint64_t pa = k_user_pa(va) & ~0xfffUL & ~HW_UNC;
        if (pa) {
            k_l0(KOS_M_UNMAP, va, 4096, 0, 0);
            k_frame_free(pa);
        }
    }
    t->stack_pages = 0;
}

static int used(uint64_t i) { return bitmap[i / 64] >> (i % 64) & 1; }

/* `n` physically contiguous zeroed pages; their physical address, 0 if none. */
static uint64_t frames(uint64_t n)
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
        --nfree;
        memset((void *)(kn.priv + (i << 12)), 0, 4096);
    }
    return kn.priv + (first << 12);
}

void *k_alloc(uint64_t bytes)
{
    uint64_t pa = frames(KOS_ALIGN_UP(bytes, 4096) >> 12);
    if (!pa) {
        k_panic("no %lu bytes for kernel tables", bytes);
    }
    return (void *)pa;
}

/* Private pages for the caller's node, physically contiguous (so the mover
 * and units can take the range whole), mapped cached and zeroed. */
int64_t k_mmap(uint64_t size, unsigned flags)
{
    (void)flags;
    uint64_t n = KOS_ALIGN_UP(size, 4096) >> 12;
    uint64_t pa = frames(n);
    if (!pa) {
        return KOS_ENOMEM;
    }
    uint64_t va = mmap_next;
    if (k_map(va, pa, n << 12, KOS_MAP_R | KOS_MAP_W | KOS_MAP_U)) {
        return KOS_ENOMEM;
    }
    mmap_next += (n + 1) << 12;
    return (int64_t)va;
}
