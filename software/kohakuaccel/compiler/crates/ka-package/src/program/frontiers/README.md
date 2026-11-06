# Completion frontier lowering

The relational pass retains the existing zero-step dominated waits, constant-delta
AWAITs, and current-sent AWAITs. Small acyclic graphs can retain distinct histories
by specialization. Other outstanding saved targets use private payload scratch.

## FRONTIER (opcode 23)

| Flags | Unit | Count | Argument | Operation |
| --- | --- | --- | --- | --- |
| 0 | package unit | payload index | 0 | Capture this unit's runtime sent count |
| 1 | package unit | payload index | 0 | Await max(previous expected, captured count) |
| 2 | 0 | payload count | first payload | Clear the scratch range |

The first limb of each scratch payload holds the 32-bit firmware sent count and
a high-limb unit tag `0x80000000 | unit`. The remaining limbs are zero. Scratch
payloads are private to FRONTIER; they are not dispatch data, address literals,
relocation targets, or user PATCH targets. Bounds, reserved fields and absent or
wrong-unit saved targets use the existing bad-step status. Package validation uses
L0.PKG.032; the program pass checks mark definition on all incoming paths.

Backward CFG liveness and capture/live interference determine reusable slots.
Only marks needed by a runtime wait receive storage. Captures for those marks
replace their compiler placeholders. The compiler inserts a range clear at normal
exits so a successfully completed package retains its original scratch bytes,
including for checksum-checked reuse. As with user PATCH, abnormal termination
may leave modified package storage; a failed package must be restored before a
checksum-checked resubmission.

Both firmware dispatch modes capture sent counts at submission, without draining
queued work. Await reads the snapshot once and then uses the existing firmware
wait or dispatch-engine WAIT. It adds no loads to completion polling. A capture
costs one instruction, one sent-count load and one scratch write (also mirrored
in the local package copy when present). A saved wait costs one instruction, one
scratch load, a unit-tag comparison and a maximum against expected, followed by
the existing wait mechanism. Clear costs one instruction and one zero write per
allocated slot, plus the existing cache flush operation when cached. Other steps
have no new state reads or per-word work. Each slot occupies one 32-byte payload.

Payload compactors preserve and relocate scratch ranges without breaking aliases.
Residency rewriting retains capture positions in its completion-suffix analysis;
the runtime capture naturally counts the rewritten sends. Straight-line engine
lowering resolves captures to known absolute engine counters. Runtime control
flow retains its existing firmware step execution mode.
