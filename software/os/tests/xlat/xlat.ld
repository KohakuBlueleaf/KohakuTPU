/* xlat: text in IMEM, everything else in the scratchpad. */
ENTRY(_start)

MEMORY
{
    IMEM (rx) : ORIGIN = 0x00000000, LENGTH = 32K
    SPAD (rw) : ORIGIN = 0x00010000, LENGTH = 32K
}

SECTIONS
{
    .text : { *(.text.init) *(.text .text.*) . = ALIGN(32); } > IMEM
    .rodata : { . = ALIGN(8); *(.rodata .rodata.*) *(.srodata .srodata.*) } > SPAD
    .data : { . = ALIGN(8); *(.data .data.*) *(.sdata .sdata.*) } > SPAD
    .bss : {
        . = ALIGN(8);
        __bss_start = .;
        *(.bss .bss.*) *(.sbss .sbss.*) *(COMMON)
        . = ALIGN(8);
        __bss_end = .;
    } > SPAD
    . = ALIGN(16);
    . = . + 0x1000;
    __stack_top = .;
    /DISCARD/ : { *(.comment) *(.note*) *(.eh_frame*) *(.riscv.attributes) }
}
