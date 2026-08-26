/* rv64_syscore link map.
 *
 * Same Harvard split as `link_pe.ld` but sized for a runtime rather than a
 * kernel: IMEM_WORDS=8192 (32 KB) and SPAD_WORDS=4096 (32 KB).
 *
 * NOTHING IS LINKED INTO THE NODE RANGE. Everything at or above NODE_BASE
 * (0x1000_0000) leaves by the fabric port and is reached through pointers, not
 * through a section -- there is no image to place there and the loader does not
 * write it.
 */

ENTRY(_start)

MEMORY
{
    IMEM (rx)  : ORIGIN = 0x00000000, LENGTH = 32K
    SPAD (rw)  : ORIGIN = 0x00010000, LENGTH = 32K
}

SECTIONS
{
    .text : {
        *(.text.init)
        *(.text .text.*)
        . = ALIGN(32);
    } > IMEM

    /* User-mode text, on pages of its own. Supervisor may not fetch from a
     * page marked U, so code the two modes both execute cannot share one --
     * aligning the function alone does not help, because the linker packs the
     * next kernel function in behind it. Empty in programs that have none. */
    .utext : {
        . = ALIGN(4096);
        *(.utext)
        . = ALIGN(4096);
    } > IMEM

    .rodata : { . = ALIGN(8); *(.rodata .rodata.*) } > SPAD
    .data   : { . = ALIGN(8); *(.data .data.*) *(.sdata .sdata.*) } > SPAD

    .bss : {
        . = ALIGN(8);
        __bss_start = .;
        *(.bss .bss.*) *(.sbss .sbss.*) *(COMMON)
        . = ALIGN(8);
        __bss_end = .;
    } > SPAD

    . = ALIGN(16);
    . = . + 0x2000;
    __stack_top = .;

    /DISCARD/ : { *(.comment) *(.note*) *(.eh_frame*) }
}
