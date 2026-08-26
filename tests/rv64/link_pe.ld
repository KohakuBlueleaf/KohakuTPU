/* SysCore link map for the NODE, where memory is Harvard.
 *
 * `link.ld` is flat because the standalone harness answers any address. In
 * `rv64_sys_pe` instruction fetch reaches imem and every load and store reaches
 * the spad, so .text must be the ONLY thing in the low region -- rodata
 * included, because rodata is read with loads.
 *
 * Sizes here match the module's defaults: IMEM_WORDS=4096 (16 KB of 32-bit
 * words) and SPAD_WORDS=2048 (16 KB of 64-bit words). Changing one without the
 * other silently truncates the image at the loader's bounds check.
 */

ENTRY(_start)

MEMORY
{
    IMEM (rx)  : ORIGIN = 0x00000000, LENGTH = 16K
    SPAD (rw)  : ORIGIN = 0x00010000, LENGTH = 16K
}

SECTIONS
{
    .text : {
        *(.text.init)
        *(.text .text.*)
        . = ALIGN(32);          /* the loader writes whole 256-bit granules */
    } > IMEM

    .rodata : {
        . = ALIGN(8);
        *(.rodata .rodata.*)
    } > SPAD

    .data : {
        . = ALIGN(8);
        *(.data .data.*) *(.sdata .sdata.*)
    } > SPAD

    .bss : {
        . = ALIGN(8);
        __bss_start = .;
        *(.bss .bss.*) *(.sbss .sbss.*) *(COMMON)
        . = ALIGN(8);
        __bss_end = .;
    } > SPAD

    . = ALIGN(16);
    . = . + 0x1000;             /* the stack lives above bss */
    __stack_top = .;

    /DISCARD/ : { *(.comment) *(.note*) *(.eh_frame*) }
}
