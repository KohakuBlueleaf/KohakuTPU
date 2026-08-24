---
title: Address map
summary: The 40-bit address every unit issues, the 43-bit AXI window a host drives, and why the mesh id appears in both.
tags:
  - architecture
  - memory
  - addressing
---

# Address map: outside vs inside

The machine is a **40-bit** machine. Every address a unit issues, every address in
an instruction, and every address a decoder tests is 40 bits:

```
[39]     aperture   1 = special (staging L2, ...), 0 = DRAM
[38]     reserved   must be 0
[37:36]  mesh       0..3
[35:0]   local      64 GB
```

`mm_mover.v`'s header states it, and `mag_stage.v:69-75`,
`mag_stage_port.v:87-88` and `mag_mem_port.v:290-292` all test it
**absolutely** -- an address carries which mesh it belongs to, no matter who
issued it or where it arrives.

`kohakuaccel/machinespec.py:global_addr` builds this form for the compiler, and
raises for a `base` that does not fit one mesh's 64 GB.

## The outside address

A host master (JTAG-AXI, XDMA) does not drive those 40 bits directly. It drives a
**43-bit** AXI address:

```
outside = (mesh + 1) << 40  |  inside
```

| mesh | window base        |
|------|--------------------|
| 0    | `0x100_0000_0000`  |
| 1    | `0x200_0000_0000`  |
| 2    | `0x300_0000_0000`  |
| 3    | `0x400_0000_0000`  |

`0x000_...` is left free so the control space stays below 4 GB and XDMA's
AXI-Lite can reach it.

The top 3 bits are **not address space**. They are a routing prefix: the
interconnect consumes them to choose a mesh's `S_AXI_MEM` port, and the mesh
receives `addr[39:0]` unmodified. The address space is 40 bits; the transport is
43. A mesh cannot tell whether a request came from its own mover or from XDMA.

### Why a prefix exists at all

`S_AXI_MEM` declares 40-bit addressing, so its `reg0` segment is a fixed **1 TB**
and Vivado will only place it on a 1 TB boundary. Four of those cannot tile
inside one 1 TB space. Assigning them at 64 GB spacing does not fail loudly --
Vivado discards the offsets and puts **all four meshes at offset 0**, which is
what `BD 41-1377` reports.

## Worked examples

Mesh 2's DRAM at local offset `L`:

```
0x300_0000_0000   window, mesh + 1 = 3
+ 0x020_0000_0000   2 << 36, the [37:36] mesh field
+ L
```

Mesh 2's staging L2 (aperture, `[39] = 1`):

```
0x300_0000_0000 + 0x080_0000_0000 + 0x020_0000_0000 = 0x3A0_0000_0000
```

The mesh id appears **twice** -- once in the window, once in `[37:36]`. That is
not redundancy to be optimised away; see below.

## Entry point is not destination

Because the low 40 bits survive the prefix, the window chooses where a
transaction *enters* and the address chooses where it *lands*. They are
independent. A write into mesh 2's window carrying `[37:36] = 3` is decoded by
mesh 2 as remote (`mag_ilink.v:6`, `awaddr[37:36] != my_mesh`) and forwarded over
the ilink.

**It does not reach the forwarder, though.** `mag.v:667-671` wires
`mag_ilink`'s AXI slave side to `mv_*` — the **mover's** write channel, and only
that. `S_AXI_MEM` becomes its own requester on MAG's converged path
(`mag.v:742`, `:753`, slot `UP`) and nothing on that path decodes `[37:36]` for
forwarding, so a host write carrying another mesh's id reaches `M_AXI_DRAM` with
its full 40-bit address and lands in **local DRAM above 64 GB**, where nothing
answers.

The same asymmetry covers the aperture: `[39]` set on a host access does not
reach the staging store either. Both are the mover's to issue. A host that wants
bytes in another mesh writes them into that mesh's own window; a host that wants
bytes in staging asks the control processor to move them.

## If the window and the address disagree

Nothing faults. `mine` simply stays low in `mag_stage_port.v:87` and no requester
claims the beat, so the access is never answered -- it presents as a hang, not as
an error. A driver that sets the window but forgets `[37:36]` sees exactly this,
and on JTAG a hung access takes the session with it.

## Capacity

The map gives each mesh 64 GB of local space. Each mesh has **4 GB** of DDR4
behind it (`C0_DDR4_ADDRESS_BLOCK` assigns at `<0x0_0000_0000 [ 4G ]>`), so
addresses from 4 GB to 64 GB within a mesh decode correctly, reach `M_AXI_DRAM`,
and hit nothing. Staying under 4 GB per mesh is a compiler invariant, not
something the hardware checks -- unlike an unimplemented aperture, which does
fault (`mag_stage.v:77`).
