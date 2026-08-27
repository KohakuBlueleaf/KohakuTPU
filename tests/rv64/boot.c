/* The imem bootloader: a tiny image that hands off to a program the host placed
 * in DRAM. This is how a program larger than the 8 KB imem runs -- the bootstrap
 * fits in imem, the body lives in DRAM, and the core fetches it through the
 * I-cache. crt0 has already set the stack; we just jump.
 *
 * The entry address is absolute (0x8000_0000), so the compiler emits li+jalr with
 * no relocation, and volatile keeps it from being devirtualised. */

#define CTRL_BASE 0x00020000UL
#define R_CONSOLE ((volatile unsigned long *)(CTRL_BASE + 0x08))

static void putch(char c) { *R_CONSOLE = (unsigned char)c; }
static void put_str(const char *s) { while (*s) { putch(*s++); } }

int main(void)
{
    void (*volatile dram_entry)(void) = (void (*)(void))0x80000000UL;
    put_str("boot: -> dram\n");
    dram_entry();
    for (;;) { }
}
