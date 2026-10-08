/* The boot block, placed at the scratchpad's base by node.ld. */
#include <ka/boot/args.h>
#include <ka/hal/cpu.h>
#include <ka/lib/stdio.h>

__attribute__((section(".bootargs"), used)) volatile struct ka_bootargs ka_boot;

uint64_t ka_boot_main_cycle;

int ka_boot_check(void)
{
    ka_boot_main_cycle = ka_cycles();
    if (ka_boot.magic != KA_BOOT_MAGIC) {
        ka_printf("[ka] no boot block (magic %lx)\n", (uint64_t)ka_boot.magic);
        return 1;
    }
    if (ka_boot.version != KA_BOOT_VERSION) {
        ka_printf("[ka] boot block version %lu, firmware speaks %d\n",
                  (uint64_t)ka_boot.version, KA_BOOT_VERSION);
        return 2;
    }
    if (ka_boot.nunits > KA_MAX_UNITS) {
        ka_printf("[ka] %lu units, firmware holds %d\n", (uint64_t)ka_boot.nunits,
                  KA_MAX_UNITS);
        return 3;
    }
    return 0;
}
