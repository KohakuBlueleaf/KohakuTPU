/* The KohakuAccel OS image: identical on every node.
 *
 *  IMEM  0x0000  kernel-side text (L0, L1, their library copy)
 *  SPAD  0x10000 the boot table (host-written, 2 KB)
 *        0x10800 L0's data and stack
 *        0x12000 the kernel's data and stack (the kernel runs untranslated)
 *  DRAM  private span + 0: the user image, loaded per node from load address
 *        UIMG (1 << 40, not a machine address) by tools/image.py:
 *          .utext  text, rodata, the app table, run at KOS_UV_TEXT (U, RX)
 *          .udata  per-node data and bss, run at KOS_UV_DATA (U, RW)
 *  .ushared      KOS_SHARED globals, at KOS_UV_SHARED (shared DRAM, not loaded)
 * User mode touches no scratchpad: every user page is DRAM. */
ENTRY(_start)

UV_TEXT   = 0x10000000;
UV_DATA   = 0x11000000;
UV_SHARED = 0x20000000;
UIMG      = 0x10000000000;

SECTIONS
{
    .text 0x0 : {
        KEEP(*(.text.init))
        KEEP(*(.text.k_entry))
        */k/*(.text .text.*)
    }
    __k_text_end = .;
    ASSERT(__k_text_end <= 0x8000, "the kernel side does not fit the 32 KB instruction window")

    .utext UV_TEXT : AT(UIMG) {
        */u/*(.text .text.*)
        */u/*(.rodata .rodata.* .srodata .srodata.*)
        . = ALIGN(8);
        __kos_apps_start = .;
        KEEP(*(.kos_apps))
        __kos_apps_end = .;
        . = ALIGN(4096);
    }
    __u_data_off = SIZEOF(.utext);

    .udata UV_DATA : AT(UIMG + __u_data_off) {
        */u/*(.data .data.* .sdata .sdata.*)
        . = ALIGN(8);
        __u_bss_start = .;
        */u/*(.bss .bss.* .sbss .sbss.* COMMON)
        . = ALIGN(8);
        __u_bss_end = .;
        . = ALIGN(4096);
    }
    __u_data_size = SIZEOF(.udata);
    __u_img_size = __u_data_off + __u_data_size;

    .kosboot 0x10000 (NOLOAD) : AT(0x10000) { . = . + 0x800; }

    .l0 0x10800 : AT(0x10800) {
        */k/machine__*(.rodata .rodata.* .srodata .srodata.* .data .data.* .sdata .sdata.*)
        . = ALIGN(8);
        __l0_bss_start = .;
        */k/machine__*(.bss .bss.* .sbss .sbss.* COMMON)
        *(.l0bss)
        . = ALIGN(8);
        __l0_bss_end = .;
        . = ALIGN(16);
        . = . + 0x800;
        __m_stack_top = .;
    }
    ASSERT(. <= 0x12000, "L0 outgrew its two scratchpad pages")

    .kdata 0x12000 : AT(0x12000) {
        __k_spad_start = .;
        */k/*(.rodata .rodata.* .srodata .srodata.* .data .data.* .sdata .sdata.*)
        . = ALIGN(8);
        __k_bss_start = .;
        */k/*(.bss .bss.* .sbss .sbss.* COMMON)
        . = ALIGN(8);
        __k_bss_end = .;
        . = ALIGN(16);
        . = . + 0x1000;
        __k_stack_top = .;
        __k_spad_end = .;
    }
    ASSERT(__k_spad_end <= 0x18000, "the kernel's data does not fit the 32 KB scratchpad")

    .ushared UV_SHARED (NOLOAD) : {
        __ushared_start = .;
        */u/*(.ushared)
        . = ALIGN(64);
        __ushared_end = .;
    }

    /DISCARD/ : { *(.comment) *(.note*) *(.eh_frame*) *(.riscv.attributes) }
}
