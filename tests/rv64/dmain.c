/* The program body, linked to live in DRAM at 0x8000_0000. The bootloader in
 * imem jumps here; every instruction is fetched from DRAM through the I-cache.
 * It computes purely in registers (no DRAM data access) and returns the result
 * as the exit word, so the proof is a clean fetch-from-DRAM. */

#define CTRL_BASE 0x00020000UL
#define R_EXIT    ((volatile unsigned long *)(CTRL_BASE + 0x00))

void __attribute__((section(".text.start"))) dram_start(void)
{
    unsigned long acc = 0;
    unsigned i;
    for (i = 0; i < 256; ++i) { acc += i * 3 + 1; }   /* = 98176 */
    *R_EXIT = acc;
    for (;;) { }
}
