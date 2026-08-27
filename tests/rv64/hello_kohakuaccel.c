/* hello_kohakuaccel.c -- the KohakuAccel OS I/O demo.
 *
 * A bare-metal program for the RV64 system node that talks to the outside
 * world both ways: it prints over the console (R_CONSOLE) and reads a line of
 * input over the new stdin queue (R_STDIN). Under Verilator the "outside world"
 * is the harness -- it preloads stdin and captures the console -- which is the
 * point: a software dev platform with real I/O, no card required.
 *
 * The greeting echoes the name it read, so the run cannot pass unless stdin
 * actually delivered the bytes.
 */

#define CTRL_BASE   0x00020000UL
#define R_EXIT      ((volatile unsigned long *)(CTRL_BASE + 0x00))
#define R_CONSOLE   ((volatile unsigned long *)(CTRL_BASE + 0x08))
#define R_STDIN     ((volatile unsigned long *)(CTRL_BASE + 0x30))
#define STDIN_VALID 0x100UL

static void putch(char c) { *R_CONSOLE = (unsigned char)c; }
static void put_str(const char *s) { while (*s) putch(*s++); }

/* Poll the stdin queue until a byte is valid, then pop it. */
static int getch(void)
{
    unsigned long v;
    do { v = *R_STDIN; } while (!(v & STDIN_VALID));
    *R_STDIN = 0;                       /* any write pops one byte */
    return (int)(v & 0xff);
}

static int read_line(char *buf, int max)
{
    int n = 0, c;
    while (n < max - 1) {
        c = getch();
        if (c == '\n' || c == '\r') break;
        buf[n++] = (char)c;
    }
    buf[n] = 0;
    return n;
}

int main(void)
{
    char name[64];

    put_str("Hello from KohakuAccel OS!\n");
    put_str("Running on the RV64 system node, under Verilator.\n");
    put_str("What is your name? ");

    read_line(name, sizeof name);

    put_str("Nice to meet you, ");
    put_str(name);
    put_str("! Welcome to KohakuAccel.\n");

    *R_EXIT = 0;                        /* clean exit; the harness stops here */
    for (;;) { }
}
