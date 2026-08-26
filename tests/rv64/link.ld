/* SysCore bare-metal link map.
 *
 * Flat, and everything is RAM: the harness's memory map is sparse and answers
 * any address, so there is no ROM/RAM split to honour. Reset is at 0.
 */

ENTRY(_start)

MEMORY
{
    RAM (rwx) : ORIGIN = 0x00000000, LENGTH = 64M
}

SECTIONS
{
    . = 0x00000000;

    .text : {
        *(.text.init)
        *(.text .text.*)
    } > RAM

    .rodata : { *(.rodata .rodata.*) } > RAM

    . = ALIGN(8);
    .data : { *(.data .data.*) *(.sdata .sdata.*) } > RAM

    . = ALIGN(8);
    .bss : {
        __bss_start = .;
        *(.bss .bss.*) *(.sbss .sbss.*) *(COMMON)
        . = ALIGN(8);
        __bss_end = .;
    } > RAM

    . = ALIGN(16);
    . = . + 0x8000;          /* the stack lives above bss */
    __stack_top = .;

    /DISCARD/ : { *(.comment) *(.note*) *(.eh_frame*) }
}
