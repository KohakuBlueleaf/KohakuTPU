/* EVENT records: gather the two counter streams, then use the page-aware copy.
 * a0 user destination, a1 requested units; return status, clamping to knunits. */
#ifdef KOS_EVENT_ASM
    .section .text.k_event_out, "ax"
    .globl k_event_out
    .type k_event_out, @function
k_event_out:
    la      t0, knunits
    lwu     t0, 0(t0)
    bltu    a1, t0, .Lcount
    mv      a1, t0
.Lcount:
    beqz    a1, .Lempty
    slli    a2, a1, 4
    addi    sp, sp, -16
    sd      s0, 0(sp)
    sd      ra, 8(sp)
    mv      s0, sp
    sub     sp, sp, a2
    mv      t0, sp
    la      t1, k_retired
    la      t2, kunits
.Lgather:
    ld      t3, 0(t1)
    ld      t4, 32(t2)
    sd      t3, 0(t0)
#if defined(KOS_HOT_MUT) && KOS_HOT_MUT == 9
    sd      t3, 8(t0)
#else
    sd      t4, 8(t0)
#endif
    addi    t0, t0, 16
    addi    t1, t1, 8
    addi    t2, t2, 80
    addi    a1, a1, -1
    bnez    a1, .Lgather
    mv      a1, sp
    call    k_copy_out
    mv      sp, s0
    ld      s0, 0(sp)
    ld      ra, 8(sp)
    addi    sp, sp, 16
    ret
.Lempty:
    li      a0, 0
    ret
    .size k_event_out, .-k_event_out
#endif
