/* The dispatcher image's entry: check the boot block, find the units when the
 * host named none, and serve the queue as the run queue's first task. */
#include <ka/boot/args.h>
#include <ka/hal/cpu.h>
#include <ka/os/task.h>
#include <ka/queue/service.h>
#include <ka/unit/enumerate.h>

/* 4 KB: the service's deepest call chain (package run -> engine -> printf). */
static KA_NOINIT uint64_t queue_stack[512] __attribute__((aligned(16)));
static struct ka_task queue_task;
static uint64_t exit_word;

static int serve(void *arg)
{
    (void)arg;
    exit_word = ka_queue_serve();
    return 0;
}

int main(void)
{
    int bad = ka_boot_check();
    if (bad) {
        ka_exit(KA_EXIT_BOOTARGS | (uint64_t)bad);
    }
    if (!ka_boot.nunits && ka_boot.scan) {
        uint64_t units[KA_MAX_UNITS];
        unsigned n = ka_enumerate((unsigned)ka_boot.scan, (unsigned)ka_boot.mesh, units,
                                  0, KA_MAX_UNITS);
        for (unsigned i = 0; i < n; ++i) {
            ka_boot.units[i] = units[i];
        }
        ka_boot.nunits = n;
    }
    ka_task_spawn(&queue_task, "queue", serve, 0, queue_stack, sizeof queue_stack);
    ka_sched_run();
    ka_exit(exit_word);
}
