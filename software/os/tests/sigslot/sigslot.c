/* The signal-slot read: does the first sig word that names the selected slot
 * carry that slot's count?
 *
 * Node A (mesh 0, -DROLE_A) rings node B's slot 5 three times and stops.
 * Node B (mesh 1) waits for slot 5 to hold 3, then 64 times alternately
 * selects slot 0 (count 0) and slot 5 (count 3), reads the sig word until its
 * slot field names the selection, and counts how often that first matching
 * word's count is wrong, and how often the next word's is. Bare metal, runs
 * on rv64_node_pair (--elf-a sigslot_a.elf --elf-b sigslot_b.elf) and the
 * kos core model (core2). */

#define CTRL    0x00020000UL
#define REG(o)  (*(volatile unsigned long *)(CTRL + (o)))
#define CONSOLE ((volatile unsigned char *)(CTRL + 0x08))
#define EXIT    REG(0x00)
#define IL_CTL  0xC0 /* [0] enable [3] clear slots */
#define IL_RING 0xD0 /* {fence[32], amount[31:16], slot[15:8], mesh[1:0]} */
#define IL_SIG  0xE0 /* read: {.., slot[53:50], .., sent[47:32], count[31:0]} */
#define IL_SEL  0xE8

#define FIELD(w) ((unsigned)(((w) >> 50) & 0xf))
#define COUNT(w) ((unsigned)(w))
#define SENT(w)  ((unsigned)(((w) >> 32) & 0xffff))

static void putch(char c) { *CONSOLE = (unsigned char)c; }
static void put_str(const char *s)
{
    while (*s) {
        putch(*s++);
    }
}
static void put_dec(unsigned long v)
{
    char b[24];
    int n = 0;
    do {
        b[n++] = (char)('0' + v % 10);
        v /= 10;
    } while (v);
    while (n) {
        putch(b[--n]);
    }
}
__attribute__((unused)) static void report(const char *what, unsigned long v)
{
    put_str("@ ");
    put_str(what);
    putch(' ');
    put_dec(v);
    putch('\n');
}

int main(void);

__attribute__((section(".text.init"), naked)) void _start(void)
{
    __asm__ volatile("la sp, __stack_top\n"
                     "call main\n"
                     "j .\n");
}

#ifdef ROLE_A
int main(void)
{
    REG(IL_CTL) = 1;
    for (int i = 0; i < 3; ++i) {
        REG(IL_RING) = (1UL << 16) | (5UL << 8) | 1UL; /* one count on mesh 1's slot 5 */
    }
    while (SENT(REG(IL_SIG)) < 3) {
    }
    put_str("slot a done\n");
    EXIT = 0;
    for (;;) {
    }
}
#else
static unsigned long settled(unsigned slot)
{
    REG(IL_SEL) = slot;
    for (int i = 0; i < 8; ++i) {
        (void)REG(IL_SIG);
    }
    return COUNT(REG(IL_SIG));
}

int main(void)
{
    REG(IL_CTL) = 1;
    while (settled(5) < 3) {
    }
    unsigned long first_wrong = 0, second_wrong = 0, reads = 0;
    for (int i = 0; i < 64; ++i) {
        unsigned slot = (i & 1) ? 5 : 0, want = (i & 1) ? 3 : 0;
        REG(IL_SEL) = slot;
        unsigned long w;
        do {
            w = REG(IL_SIG);
            ++reads;
        } while (FIELD(w) != slot);
        first_wrong += COUNT(w) != want;
        second_wrong += COUNT(REG(IL_SIG)) != want;
    }
    report("slot.selections", 64);
    report("slot.reads_until_field", reads);
    report("slot.first_matching_word_wrong", first_wrong);
    report("slot.next_word_wrong", second_wrong);
    put_str("slot b done\n");
    EXIT = 0;
    for (;;) {
    }
}
#endif
