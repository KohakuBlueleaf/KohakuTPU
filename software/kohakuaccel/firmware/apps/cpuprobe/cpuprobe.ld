/* cpuprobe's link map: node.ld cut to the sysnode bench's processor (8 KB
 * instruction window, 4 KB scratchpad, rv64_node_pair PE_IMEM / PE_SPAD), so
 * one image runs on the bench and on every card node. */

ENTRY(_start)

MEMORY
{
    IMEM (rx) : ORIGIN = 0x00000000, LENGTH = 8K
    SPAD (rw) : ORIGIN = 0x00010000, LENGTH = 4K
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
    } > SPAD

    .data : { . = ALIGN(8); *(.data .data.*) *(.sdata .sdata.*) } > SPAD

    .bss : {
        . = ALIGN(8);
        __bss_start = .;
        *(.bss .bss.*) *(.sbss .sbss.*) *(COMMON)
        . = ALIGN(8);
        __bss_end = .;
    } > SPAD

    .noinit (NOLOAD) : {
        . = ALIGN(16);
        *(.noinit .noinit.*)
        . = ALIGN(16);
    } > SPAD

    /* 1 KB of stack: fib(16) is the deepest frame chain. */
    .stack (NOLOAD) : {
        . = ALIGN(16);
        . = . + 0x400;
        __stack_top = .;
    } > SPAD

    /DISCARD/ : { *(.comment) *(.note*) *(.eh_frame*) *(.riscv.attributes) }
}
