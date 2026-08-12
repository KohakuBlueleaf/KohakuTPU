---
title: Memory-agent staging
summary: One centralised URAM store behind a reserved address range inside the memory agent — almost nothing to design, and one question worth arguing about.
tags:
  - notes
  - memory
  - research
---

# Memory-agent staging: URAM behind a reserved address range

One centralised store inside the memory agent. See [README](README.md) for the
alternatives.

Status: discussion. Nothing built.

## 1. Almost none of this needs designing

L2 is a reserved range in the existing address map. That settles, with no further
decisions:

- **No new instruction.** `FILL` and `DRAIN` already carry a 34-bit byte address
  ([projects/kohakutpu/isa.md](../../projects/kohakutpu/isa.md)). Point one at the
  L2 range and it stages; point an operand fetch there and it reads back.
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
| 256 b | 4 | one response word -- matches the mesh flit |
| 936 b | 13 | one whole L1 entry |
| 1,872 b | 26 | two entries -- matches a double-pumped cluster |

**Wide only pays where the consumer is wide.** The agent's fill path hands entries
to the compute unit and is not limited to one flit per cycle, so 936 b is the
natural unit here: one read, one entry, no serialisation.

1,872 b is worth measuring because a pumped cluster would eat two K blocks per
base-clock cycle. Whether the agent can deliver at that rate is a bandwidth
question, not a storage one.

**This is exactly where agent staging differs from a mesh adapter.** A local port
is one flit per cycle, so wide lines there buy nothing -- see
[noc-staging](noc-staging.md). The width argument only exists on this side.

## 3. Budget: reach, not capacity

URAM sits in columns spread across the die. A centralised block cannot reach all
~290 free URAMs in an SLR at frequency, and the most crowded SLR is at **95.80%
CLB** (MEASURED), so there is no room to route around it.

At 13 URAMs per 936-bit bank:

| URAMs | banks | capacity |
|---|---|---|
| 91 | 7 | 3.3 MB |
| 104 | 8 | 3.7 MB |
| 130 | 10 | 4.7 MB |

**~91-104 URAMs is the right ballpark** -- 8 banks of 936 b covers a mesh of six
clusters and two vector cores at one bank per cluster, and keeps the store in the
columns nearest the agent. Capacity was never the constraint; a pass's working set
is a few hundred KB.

## 4. Host access uses the second URAM port

URAM288 is dual-port. Port A serves the agent's operand path; port B is an AXI
slave in the address map for the host.

The alternative -- hanging L2 off the AXI fabric as an ordinary slave -- would put
every operand access through the root interconnect (**43,714 LUT**, MEASURED) and
across SLR boundaries. Port A keeps the agent's traffic local; port B gives the
host access without touching it.

## 5. What to measure

1. Does a 936-bit bank close at the mesh frequency? 13 URAMs in parallel is a wide
   fanout and the output register is optional.
2. 936 vs 1,872, against a pumped cluster's two-entries-per-cycle appetite.
3. Fabric cost of the address path. The URAMs are free; the question is entirely
   how much CLB the walker and mux cost in an SLR at 95.80%.
