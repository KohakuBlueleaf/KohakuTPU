/* The node processor's link map: .text in IMEM (32 KB at 0x0), everything else
 * in SPAD (32 KB at 0x1_0000) with .bootargs first. */

ENTRY(_start)

MEMORY
{
    IMEM (rx) : ORIGIN = 0x00000000, LENGTH = 32K
    SPAD (rw) : ORIGIN = 0x00010000, LENGTH = 32K
}

SECTIONS
{
    .text : {
        KEEP(*(.text.init))
        *(.text .text.*)
        . = ALIGN(32);
    } > IMEM

    .bootargs : {
        KEEP(*(.bootargs))
        . = ALIGN(32);
    } > SPAD

    .rodata : {
        . = ALIGN(8);
        *(.rodata .rodata.*) *(.srodata .srodata.*)
        . = ALIGN(8);
        __ka_units_start = .;
        KEEP(*(.ka_units))
        __ka_units_end = .;
    } > SPAD

    .data : { . = ALIGN(8); *(.data .data.*) *(.sdata .sdata.*) } > SPAD

    .bss : {
        . = ALIGN(8);
        __bss_start = .;
        *(.bss .bss.*) *(.sbss .sbss.*) *(COMMON)
        . = ALIGN(8);
        __bss_end = .;
    } > SPAD

    /* KA_NOINIT (ka/hal/cpu.h): reserved, never zeroed by crt0. */
    .noinit (NOLOAD) : {
        . = ALIGN(16);
        *(.noinit .noinit.*)
        . = ALIGN(16);
    } > SPAD

    /* 6 KB of stack, counted in the image's SPAD figure. */
    .stack (NOLOAD) : {
        . = ALIGN(16);
        . = . + 0x1800;
        __stack_top = .;
    } > SPAD

    /DISCARD/ : { *(.comment) *(.note*) *(.eh_frame*) *(.riscv.attributes) }
}
