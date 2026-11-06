---
title: Ring egress liveness
summary: Bounded waits and the states that can hold a doorbell.
---
# Ring egress liveness

A ring is complete at the sending node when the interlink's sent count advances,
not when the CPU writes RING. The hardware queue holds four rings. Firmware and
OS compare their issued count to the low 16 bits of the sent count modulo 65536.
The dispatch engine's 12-bit unit counters are a different counter domain.

## Runtime contract

`software/kohakuaccel/os/kernel/msg.c` uses one wait for room and for draining all
issued rings. A room wait requires outstanding rings below `HW_RING_DEPTH`; a
drain requires zero. Each reads the signal word once per poll. A blocked wait
has a total budget of `KOS_DE_TIMEOUT` cycles (default 2^24, defined in
`software/kohakuaccel/os/include/kos/abi.h`), measured by unsigned cycle-counter
subtraction. No ring is issued after that budget expires. The existing kernel
panic path reports `ring egress timeout`, the issued count and the signal word;
the existing where record identifies `KOS_W_RING`. It does not declare work
complete, free outstanding storage, retry rings or reset a link. A progressing
queue still uses the same modulo count across wrap. The budget bounds a blocked
operation, not the lifetime or total ring count of a kernel.

Firmware `software/kohakuaccel/firmware/package/interp.c` already bounds
`ring_room` using `expired(t0, KA_WAIT_RING, sent)` and returns its engine status.
Its slot wait separately uses `KA_WAIT_SLOT`. These paths must retain a nonzero
configured execution timeout when bounded execution is required. OS panic is
node-fatal because a stalled kernel messaging transport cannot reliably deliver
an ordinary remote error reply. A host must treat this as a failed epoch, not
proof of quiescence or permission to reuse its allocations.

Cost: an unblocked ring retains one signal-word read. A blocked OS poll adds a
cycle CSR read, subtraction and comparison, but no additional MMIO or data
memory read. The firmware path is unchanged. The hardware-double host test
holds the sent frontier for each wait and checks that panic precedes any extra
write, then allows progress across the 16-bit wrap.

## Offline hold-state audit

These are possible hold states, not an attribution of the observed persistent
stall. The RTL is unchanged.

* `mag_ilink.v:283-290`: `u_dq` remains full until `ob_db_ack`. A raw engine
  ring can overrun it (F-003); the software room check prevents that input but
  cannot drain a queue whose outbound completion is held.
* `mag_ilink.v:589-597`: a fenced head ring requires `ob_drained`. Any asserted
  `fence_hold`, write state other than `WS_IDLE`, pending `ob_wr_req`, pending
  CPU write `cw_req`, open encapsulation `acc_open`, ready packet `acc_ready`,
  or nonempty encapsulation FIFO holds it. A missing final flit or unfinished
  write can therefore hold a fenced ring even with no link stall counter.
* `mag_ilink.v:673-710`: disabled `enable_r` holds arbitration in `OB_IDLE`;
  missing `ltx_hready` holds `OB_HDR`; missing `ltx_dready` holds `OB_DAT`.
  A packet length/last mismatch can prevent `ob_dat_end`, hence queue pop and
  sent-count advancement. Link backpressure or a leaked link credit can
  manifest as one of those ready signals remaining low. The round-robin arbiter
  does not guarantee progress while a previously selected packet is stuck.
* `dispatch_engine.v:163-182`: a WAIT outside its modular half-window can
  hold the engine (F-004). `t_hold`, mailbox offer/cooldown, mover room, or
  `moving` can hold writes. `moving` depends on mover busy and the signed
  modular `owed` counter. Counter wrap in a long traffic run is a hypothesis
  about these comparisons, not evidence that the 16-bit ring count wrapped.
* `mag.v` connects the interlink reset to system reset (F-007). Processor
  reload does not clear an open packet, queued ring, or link credit state.
  Firmware/OS boot adopts the sent count; it does not repair hardware egress.
* OS `msg.c` also has software message credits and inbox/channel flow control.
  A full inbox can withhold returned message credits; a receiver not polling
  can do the same. Those are not the hardware four-entry ring queue. The ring
  wait does not call the message drain recursively (which can itself ring).

An offline observation of fixed sent count and four outstanding rings cannot
choose among these states. Capture the fenced head bit, `ob_drained` inputs,
`obst/ob_who/ob_left`, link ready/credit state and engine head/owed values at the
stall before attributing a silicon defect. Retuning clears state; it is not a
runtime recovery contract.
