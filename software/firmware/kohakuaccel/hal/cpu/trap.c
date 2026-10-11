/* The fatal-trap path and the exit store. */
#include <ka/hal/cpu.h>
#include <ka/hal/node.h>
#include <ka/lib/stdio.h>

__attribute__((weak)) void ka_on_fatal(uint64_t code, uint64_t a, uint64_t b)
{
    (void)code;
    (void)a;
    (void)b;
}

void ka_exit(uint64_t code)
{
    ka_ctrl_wr(KA_R_EXIT, code);
    for (;;) {
    }
}

void ka_fatal_trap(uint64_t cause, uint64_t epc, uint64_t tval)
{
    /* The tap too: a ring broken by the fault would swallow the only report. */
    ka_debug("\n[ka] fatal trap\n");
    ka_printf("\n[ka] fatal trap cause=%lx epc=%lx tval=%lx\n", cause, epc, tval);
    ka_on_fatal(KA_EXIT_TRAP | cause, epc, tval);
    ka_exit(KA_EXIT_TRAP | cause);
}
