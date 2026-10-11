/* memprobe: what the node processor's memory paths do, measured.
 *
 * The host (software/firmware/tools/memprobe.py) writes a pattern at `queue` and a
 * marker line at queue+0x120, boots this, and reads back what it wrote:
 *   [queue + 0x200] mask of pattern words read back correctly (uncached)
 *   [queue + 0x208] cycles for 64 uncached loads
 *   [queue + 0x210] cycles for 64 uncached stores
 *   [queue + 0x218] cycles for 64 cached loads over 16 fresh lines
 *   [queue + 0x220] mask of pattern words read back correctly (cached)
 * and stores four words into line queue+0x100 and ONE into word 1 of the
 * marker line, so the host can see whether byte strobes survive to DRAM.
 */
#include <ka/boot/args.h>
#include <ka/hal/cpu.h>
#include <ka/hal/mem.h>
#include <ka/lib/stdio.h>

static uint64_t pattern(unsigned i) { return 0x1111000000000000UL * (i + 1) + i; }

int main(void)
{
    if (ka_boot_check()) {
        return 1;
    }
    uint64_t q = ka_boot.queue;
    ka_printf("memprobe q=%lx mesh=%lu\n", q, (uint64_t)ka_boot.mesh);

    uint64_t ok = 0;
    for (unsigned i = 0; i < 8; ++i) {
        if (ka_ld64(q + 8 * i) == pattern(i)) {
            ok |= 1UL << i;
        }
    }

    uint64_t t0 = ka_cycles();
    uint64_t sink = 0;
    for (unsigned i = 0; i < 64; ++i) {
        sink += ka_ld64(q + 8 * (i & 7));
    }
    uint64_t t_ld = ka_cycles() - t0;

    t0 = ka_cycles();
    for (unsigned i = 0; i < 64; ++i) {
        ka_st64(q + 0x400 + 8 * i, i);
    }
    uint64_t t_st = ka_cycles() - t0;

    t0 = ka_cycles();
    for (unsigned i = 0; i < 64; ++i) {
        sink += *(volatile uint64_t *)(uintptr_t)(q + 0x1000 + 8 * i);
    }
    uint64_t t_cl = ka_cycles() - t0;

    uint64_t okc = 0;
    for (unsigned i = 0; i < 8; ++i) {
        if (*(volatile uint64_t *)(uintptr_t)(q + 8 * i) == pattern(i)) {
            okc |= 1UL << i;
        }
    }

    for (unsigned i = 0; i < 4; ++i) {
        ka_st64(q + 0x100 + 8 * i, 0xC0DE000000000000UL | i);
    }
    ka_st64(q + 0x128, 0xBEEF);

    ka_st64(q + 0x200, ok);
    ka_st64(q + 0x208, t_ld);
    ka_st64(q + 0x210, t_st);
    ka_st64(q + 0x218, t_cl);
    ka_st64(q + 0x220, okc);
    ka_printf("ok=%lx okc=%lx ld64x64=%lu st64x64=%lu cached64=%lu (%lx)\n", ok, okc,
              t_ld, t_st, t_cl, sink & 1);
    return 0;
}
