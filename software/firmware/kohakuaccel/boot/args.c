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
        return KA_BOOT_ERR_MAGIC;
    }
    if (ka_boot.version != KA_BOOT_VERSION) {
        ka_printf("[ka] boot block version %lu, firmware speaks %d\n",
                  (uint64_t)ka_boot.version, KA_BOOT_VERSION);
        return KA_BOOT_ERR_VERSION;
    }
    if (ka_boot.nunits > KA_MAX_UNITS) {
        ka_printf("[ka] %lu units, firmware holds %d\n", (uint64_t)ka_boot.nunits,
                  KA_MAX_UNITS);
        return KA_BOOT_ERR_UNITS;
    }
    for (unsigned i = 0; i < KA_BOOT_REGIONS; ++i) {
        uint64_t base = ka_boot.regions[i].base, size = ka_boot.regions[i].size;
        uint64_t use = ka_boot.regions[i].use;
        if (!use) continue;
        if (!size || base + size < base) return KA_BOOT_ERR_REGIONS;
        for (unsigned j = 0; j < i; ++j) {
            if (!ka_boot.regions[j].use) continue;
            uint64_t other = ka_boot.regions[j].base, bytes = ka_boot.regions[j].size;
            if (use == ka_boot.regions[j].use || (base < other + bytes && other < base + size))
                return KA_BOOT_ERR_REGIONS;
        }
    }
    return 0;
}

uint64_t ka_boot_region(uint64_t use, uint64_t *size)
{
    for (unsigned i = 0; i < KA_BOOT_REGIONS; ++i) {
        if (ka_boot.regions[i].use == use && ka_boot.regions[i].size) {
            *size = ka_boot.regions[i].size;
            return ka_boot.regions[i].base;
        }
    }
    return 0;
}
