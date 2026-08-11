# The boundary: what is new, and what is simply absent

**The bitstream in the fab today has no interlink.** Everything in this directory
is generated only when `ILINK=1`, and a driver has to work against both without
being told which it is holding. This page is the contract for that: what appears,
what is missing, how to tell, and what a driver must never assume.

The rule the rest of this page implements: **a single-mesh machine is not a
degraded multi-mesh machine.** It is the same machine with a topology that has
one node in it, and every field the interlink adds is zero there because zero is
the correct value, not because the feature was disabled.

---

## 1. How a driver tells them apart

Read `IL_CAPS` at control-window offset `0x0080`.

```
    0x0000_0000_0000_0000     no interlink -- single mesh
    0x0000_0000_1XXX_494C     interlink present, see the field layout in s3
```

This works on **silicon that predates the interlink entirely**, which is the
property that matters: `noc_orchestrator`'s register read decode ends in
`default: reg_rd = 0`, so an unmapped offset has always returned zero and always
will. There is no version to compare, no capability list to parse, and no risk
of a driver misreading a bitstream older than the question.

`CU_VERSION` does **not** move for the interlink and must not be used to detect
it. The interlink is a topology feature: a compiler emitting local addresses and
`mesh_id = 0` produces identical machine code either way, so the version that
answers "is this the bitstream my compiler targets" has the same answer. Using
`CU_VERSION` here would make a correct single-mesh build look like a wrong one.

---

## 2. What is absent on single-mesh silicon

Absent means *not present in the netlist* -- not disabled, not tied off, not
present-but-idle.

| | single mesh (`ILINK=0`) | multi mesh (`ILINK=1`) |
|---|---|---|
| `LINK*_IN` / `LINK*_OUT` AXI-Stream ports | do not exist on `mag` | `NLINK` of each |
| `mag_switch`, `mag_link` instances | none | one switch, `NLINK` links |
| MAG AXI master channels (`MP1`) | `MEM_PORTS + 2` | `MEM_PORTS + 3` |
| remote address decode | constant false, optimised away | live |
| `IL_*` status registers | read `0` | read state |
| `IL_*` config window (`aux_cfg` `0x80..0xF8`) | writes are discarded | live |
| doorbell counters | read `0` | count |

The AXI master count is the one structural difference a block design sees.
`MP1` is a parameter derived from `MEM_PORTS` and `ILINK`, so a generated top
carries the right number without anyone counting -- but a block design wired for
one cannot be reused for the other, and the address editor changes with it.

---

## 3. The registers, and what they read on old silicon

All are 64-bit, in the MAG **control** window (the same AXI slave the agent
answers on). Reads come back through the aux status window at `0x0080..0x00F8`;
writes go through the aux config window at `0x0880..0x08F8`.

Every one of these **reads as zero on any bitstream without the interlink**,
including the one baking now. A driver may read them unconditionally.

### Read (`0x0080` + 8n)

| offset | name | contents |
|---|---|---|
| `0x0080` | `IL_CAPS` | `[15:0]` magic `0x494C`, `[19:16]` `NLINK`, `[23:20]` this mesh's id, `[27:24]` mesh count, `[31:28]` interlink version (`1`) |
| `0x0088` | `IL_FAULT` | sticky fault bits, and the first fault's detail |
| `0x0090`..`0x00A8` | `IL_DBELL0..3` | doorbells received from mesh *n*: `[31:0]` count, `[47:32]` last `txn` |
| `0x00B0`,`0x00C0` | `IL_TXCNT0/1` | packets `[31:0]` and beats `[63:32]` sent on that link |
| `0x00B8`,`0x00C8` | `IL_RXCNT0/1` | packets and beats received |
| `0x00D0`,`0x00D8` | `IL_STALL0/1` | `[31:0]` cycles stalled for want of credit, `[63:32]` cycles idle |
| `0x00E0` | `IL_FWD` | `[31:0]` packets forwarded, `[63:32]` cycles the forward queue was full |
| `0x00E8` | `IL_CRED` | live credits, for diagnosing a wedge rather than for steady-state use |
| `0x00F0` | `IL_DOOR_TX` | doorbells this MAG has sent |

### Write (`0x0880` + 8n)

| offset | name | contents |
|---|---|---|
| `0x0880` | `IL_CTRL` | `[0]` enable, `[1]` clear counters, `[2]` clear faults |
| `0x0888` | `IL_MESH` | `[1:0]` this mesh's id. Resets to the `MESH_ID` parameter and is writable, so **one bitstream can occupy any position in the grid** |
| `0x0890` | `IL_DOOR` | ring a doorbell by hand: `[1:0]` destination mesh, `[15:8]` `txn`. A bring-up instrument -- it proves a link end to end with no DMA behind it |

Writes to `0x0880..0x08F8` on old silicon reach the memory mover's config decode,
which ends in `default: ;` and ignores them. They are harmless there, and the
offsets are chosen above everything the mover uses (`0x00`..`0x50`) so they can
never be confused for one.

---

## 4. What a driver must not assume

**Do not assume a remote address works.** With no interlink, an address whose
`[33:32]` is not zero is an address in this mesh's DRAM with two stray high bits.
It does not fault, it does not route anywhere -- it aliases. The check belongs in
the driver, before the descriptor is written:

```
    if board.mesh_count == 1 and (addr >> 32) != 0:  reject
```

**Do not assume a remote drain works.** On single-mesh silicon the CU's drain
sets `NOC_RSVD = 3'b000` because the descriptor's remote fields are zero. If a
driver *does* set them, the flit is built with a remote marker and arrives at a
MAG that has no switch to give it to -- it goes to the agent as an ordinary
control flit and is dropped, with the drop reported. Loud, but the transfer is
gone. The driver must not emit remote drains against a board file whose mesh
count is 1.

**Do not poll a doorbell that can never ring.** `IL_DBELL*` reads zero forever
on single-mesh silicon, which is indistinguishable from "not yet". A wait loop
on it hangs. Gate every doorbell wait on `IL_CAPS != 0`.

**Do not infer topology from the mesh geometry.** `ncol`/`nrow` describe one
mesh's routers. Mesh count, this mesh's id and the link map are separate board
file fields, because they are a different fact -- a machine can be four meshes of
the same geometry, and reading the geometry tells you nothing about how many
there are.

---

## 5. The one thing that is shared

Reserved-and-zero fields exist in both:

| field | single mesh | multi mesh |
|---|---|---|
| `NOC_RSVD[258:256]` | always `3'b000` | `{1'b1, mesh_id[1:0]}` on a remote flit, `3'b000` otherwise |
| `NOC_TXN_ID` on `CU_DATA` | always `8'h00` | `{fin_y, fin_x}` on a remote flit |
| VDRAIN descriptor base `[25:24]` | zero | destination mesh |
| VDRAIN descriptor base `[33:26]` | zero | `{fin_y, fin_x}`, nonzero means remote |
| `CU_INST` DRAIN `[78:77]` (`dmesh`) | zero | destination mesh |
| `CU_INST` DRAIN `[76:69]` (`dfin`) | zero | `{fin_y, fin_x}`, nonzero means remote |

The last four are the same two facts on the two engines, and **only the
semantics are shared, not the bit positions** — the vector core's fit in a
descriptor base and the cluster's in the instruction payload's tail, because the
two instruction words are different shapes
([`../isa/cluster.md`](../isa/cluster.md) §10.3,
[`../isa/vector.md`](../isa/vector.md) §5.1).

A single-mesh program leaves all six at zero, and a multi-mesh build reading
zero behaves exactly as a single-mesh build does. **That is what makes one
compiler serve both**: the remote case is a value in a field that already exists,
not a second encoding.

`(0,0)` is the sentinel for "not remote" in the descriptor because it is a mesh
*corner* -- it touches no router and can never hold an endpoint, which is the
same reason `cud_ack` uses it for "answer the sender".

---

## 6. Reading order

- [README.md](README.md) -- why four meshes, why MAG is the boundary
- [topology.md](topology.md) -- ports, the second routing layer, placement
- [protocol.md](protocol.md) -- packet format, credits, deadlock
- [transfers.md](transfers.md) -- the three kinds and who starts them
