/* The kernel's entry on each node. One-shot initialization runs from the
 * private image; halt and steady-state operations remain in IMEM. */
#include "kernel.h"
#include "bootstrap.h"

struct kos_boot k_boot;

extern char __ushared_start[], __ushared_end[], __u_data_off[], __u_bss_start[], __u_bss_end[];
extern char __u_img_size[];

static const struct k_boot_kernel boot = {
    .node = &kn, .boot = &k_boot,
    .memory = &k_boot_memory, .log_init = k_log_init, .log_header = k_log_hdr,
    .units = &k_boot_units, .messages = &k_boot_messages,
    .user_satp = &k_user_satp,
    .data_off = (uint64_t)__u_data_off, .image_size = (uint64_t)__u_img_size,
    .bss_start = __u_bss_start, .bss_end = __u_bss_end,
    .shared_start = __ushared_start, .shared_end = __ushared_end,
    .zero = memset, .alloc = k_alloc, .dcache = hw_dcache, .map = k_map,
    .print = k_printf, .panic = k_panic, .where = k_where, .l0 = k_l0,
    .poll_units = k_units_poll, .find_app = k_find_app, .run_local = k_run_local,
    .schedule = k_schedule, .resume = k_resume,
};

void k_halt(uint64_t code)
{
    k_where(KOS_W_DONE, code, 0);
    k_rings_drain();
    k_l0(KOS_M_EXIT, code, 0, 0, 0);
    for (;;) {
    }
}

#ifdef KOS_SMALL_CALL
__attribute__((minsize))
#endif
void k_main(struct kos_boot *b, uint64_t node)
{
    (void)b;
    (void)node;
    ((k_boot_kernel_fn *)k_uimg((uint64_t)k_boot_kernel))(&boot);
}
