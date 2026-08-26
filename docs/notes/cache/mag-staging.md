---
title: Memory-agent staging
summary: One centralised URAM store behind a reserved address range inside the memory agent — almost nothing to design, and one question worth arguing about.
tags:
  - notes
  - memory
  - research
---

# Memory-agent staging: URAM behind a reserved address range

Status: **BUILT and shipping.** `src/kohakuaccel/sysnode/core/mag_stage.v`,
arbitrated by `mag_stage_port.v` on the converged internal path, benched by
`tests/sysnode/mag_stage_tb.v`, and selected with `gen_mesh.py --l2-mag`. It is
optional: a mesh built without the flag has no staging store.

One centralised store inside the memory agent. See [README](README.md) for the
alternatives and for which of the five are built at all.

**Two claims below are corrected by the RTL as built**, and the RTL wins: the
line is **1,024 bits and not 936**, and the memory primitive is *simple* dual
port rather than true dual port, so the port split in §4 is arbitration rather
than two independent ports. The 936-bit arithmetic that follows from the first
is not reproduced here — see §3.

## 1. Almost none of this needs designing

L2 is a reserved range in the existing address map. That settles, with no further
decisions:

- **No new instruction.** `FILL` and `DRAIN` already carry a full 40-bit byte
  address ([projects/kohakutpu/isa.md](../../projects/kohakutpu/isa.md)), and
  `addr[39]` is the aperture bit. Point one at the L2 range and it stages; point
  an operand fetch there and it reads back.
- **Host access is free.** It is in the address map, so the host DMA reaches it
  like any memory -- push weights straight in, read L2 back for debug.
- **No write policy.** Staging is not write-back and never pretends to be. The
  only software obligation: results destined for DRAM must use DRAM addresses, or
  an explicit bulk move.
- **No tags, no associativity, no replacement, no coherence.**

**The one question worth arguing about is line width.**

## 2. Line width

MEASURED, placed multi-mesh run: URAM 120 of 1,280 used (9.38%). One URAM288 is
288 Kb (36 KB), natively 4096 x 72 b; width is built by paralleling URAMs.

An L1 entry is **928 bits** and the cascade consumes exactly one per cycle. A fill
response arrives as **four 256-bit words** which the compute unit assembles.

| line | URAMs | what one read yields |
|---|---|---|
| 256 b | 4 | one response word — matches the mesh flit |
| ~1,024 b | 13–14 | one whole L1 entry. **This is what is built** |
| ~2,048 b | 26–28 | two entries — matches a double-pumped compute unit |

**Wide only pays where the consumer is wide.** The agent's fill path hands
entries to the compute unit and is not limited to one flit per cycle, so one
entry per read is the natural unit here: one read, one entry, no serialisation.
The built line is **1,024 bits**, which covers a 928-bit entry with room.

The double width is worth measuring because a pumped compute unit would consume
two entries per base-clock cycle. Whether the agent can deliver at that rate is
a bandwidth question, not a storage one, and it has not been measured.

**This is exactly where agent staging differs from a mesh adapter.** A local port
is one flit per cycle, so wide lines there buy nothing -- see
[noc-staging](noc-staging.md). The width argument only exists on this side.

## 3. Budget: reach, not capacity

URAM sits in columns spread across the die. A centralised block cannot reach all
~290 free URAMs in an SLR at frequency, and the most crowded SLR is at **95.80%
CLB** (MEASURED), so there is no room to route around it.

**The capacity table this section used to carry is deleted, not corrected.** It
was arithmetic on the 936-bit line, and the built line is 1,024 bits;
recomputing it here would be inventing a number rather than measuring one. **The
shipping configuration is the authority: 4 banks × 16,384 entries = 64 URAM per
memory agent**, set in the build configuration. Take the budget from there and
the capacity from the RTL.

What the deleted table said and what survives it: **capacity was never the
constraint.** A pass's working set is a few hundred kilobytes, and even a
conservative budget of free URAM gives several megabytes per SLR. The reason to
distribute is reach, above, not size.

## 4. Host access shares the store, by arbitration

The intent was two independent ports: one serving the agent's operand path, one
an AXI slave in the address map for the host. **The primitive as instantiated is
*simple* dual port**, so what is built is arbitration between the two rather
than two independent ports. The distinction matters for bandwidth under
concurrent host and operand traffic and it has not been measured.

The alternative — hanging the store off the AXI fabric as an ordinary slave —
would put every operand access through the root interconnect (**43,714 LUT**,
MEASURED) and across SLR boundaries. Keeping the agent's traffic on the store's
own port keeps it local; the host reaches it without touching that path.

## 5. What to measure

1. Does a bank at the built width close at the mesh frequency? Many URAMs in
   parallel is a wide fanout and the output register is optional.
2. One entry per read against two, if a compute unit is ever double-pumped.
3. Fabric cost of the address path. The URAMs are free; the question is entirely
   how much CLB the walker and mux cost in an SLR at 95.80%.
4. What the arbitration in §4 costs when host and operand traffic overlap.
