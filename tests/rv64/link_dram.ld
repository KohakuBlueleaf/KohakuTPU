/* The program body, resident in DRAM. .text.start is forced first so
 * dram_start() lands exactly at 0x8000_0000, the address the bootloader jumps to. */
ENTRY(dram_start)

MEMORY
{
    DRAM (rwx) : ORIGIN = 0x80000000, LENGTH = 16M
}

SECTIONS
{
    .text 0x80000000 :
    {
        *(.text.start)
        *(.text .text.*)
        *(.rodata .rodata.*)
    } > DRAM

    .data :
    {
        *(.data .data.*)
        *(.sdata .sdata.*)
    } > DRAM

    .bss :
    {
        *(.bss .bss.*)
        *(.sbss .sbss.*)
        *(COMMON)
    } > DRAM
}
