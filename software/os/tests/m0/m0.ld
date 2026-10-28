/* M0 probes: text in IMEM, one loop family in DRAM (.dtext, placed by the
 * host), data and stacks in the scratchpad after the boot table. */
ENTRY(_start)

MEMORY
{
    IMEM (rx) : ORIGIN = 0x00000000, LENGTH = 32K
    SPAD (rw) : ORIGIN = 0x00010800, LENGTH = 30K
    DRAM (rx) : ORIGIN = 0x10070000, LENGTH = 64K
}

SECTIONS
{
    .text : { KEEP(*(.text.init)) *(.text .text.*) . = ALIGN(8); } > IMEM
    .dtext : { *(.dtext) } > DRAM
    .rodata : { . = ALIGN(8); *(.rodata .rodata.* .srodata .srodata.*) } > SPAD
    .data : { . = ALIGN(8); *(.data .data.* .sdata .sdata.*) } > SPAD
    .bss : {
        . = ALIGN(8);
        __bss_start = .;
        *(.bss .bss.* .sbss .sbss.* COMMON)
        . = ALIGN(8);
        __bss_end = .;
    } > SPAD
    .stack (NOLOAD) : {
        . = ALIGN(16);
        . = . + 0x1000;
        __stack_top = .;
        . = . + 0x800;
        __trap_top = .;
    } > SPAD
    /DISCARD/ : { *(.comment) *(.note*) *(.eh_frame*) *(.riscv.attributes) }
}
