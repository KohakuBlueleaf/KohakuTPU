# The hardware API: one 64-bit window, four backends

Everything above this page — the DSL, the three IR levels, the round cutter —
ends at a pair of methods. This is what they are, which backends implement them,
and the two facts about the JTAG one that have cost the most time.

Source: [`src/ktpu/hw/device.py`](../../src/ktpu/hw/device.py) (the contract and
the address map), [`jtag.py`](../../src/ktpu/hw/jtag.py),
[`xdma.py`](../../src/ktpu/hw/xdma.py),
[`board.py`](../../src/ktpu/hw/board.py) (which machine, and how to reach it).

---

## 1. The contract is two methods, and the block pair is not a second one

```python
class Transport(abc.ABC):
    bulk = False
    def write64(self, addr: int, data: int) -> None: ...
    def read64(self, addr: int) -> int: ...
```

`write_block`/`read_block` exist, and **a block at `addr` must be
indistinguishable from `len(data) // 8` word accesses at ascending addresses,
little-endian** — which is exactly what the base class does, so a backend
overrides them only when it has a transfer that beats the loop. That equivalence
is what lets one test compare a bulk backend against recorded word writes and
demand the same bytes at the same addresses.

`bulk` says whether the override happened, because **the caller has work to do
either way**: coalescing scattered writes into runs (`device.runs`) is worth the
arithmetic over PCIe, where a run is one descriptor and the loop is one
descriptor per word, and worth nothing over a transport that will only unpack it
again. `stage_flit` walks slots in address order precisely so a round's whole
staging window collapses to one block — one DMA descriptor rather than six
hundred.

A partial word is **refused**, not truncated: it has no sequence of word accesses
to be equivalent to, so there is nothing to fall back to and nothing to compare
against.

`TransportUnavailable` is distinct from an I/O failure on a backend that opened.
Absence is a *configuration* answer — wrong host, driver not installed, card not
enumerated — and a caller that can fall back to another backend has to tell the
two apart without reading a win32 code.

## 2. The four backends

`board.open_transport(spec, board)`, falling back to `$KTPU_TRANSPORT` and then
to the board file's own choice. Each backend module is imported **inside** that
function, one at a time, so a driver does not make every caller depend on every
backend.

| spec | what it is | `bulk` |
|---|---|---|
| `record` | records writes instead of performing them — what makes `check.py fast` need no simulator | no |
| `memory` | a dict standing in for the device | no |
| `xdma`, `xdma:1` | PCIe, ~1.8 GB/s, the real operand path | yes |
| `jtag` | JTAG-to-AXI through a live Vivado Tcl session, ~0.08 MB/s | no |

**JTAG and XDMA are mapped identically and verified byte-exact**, so a pointer
means the same thing on both. That is what makes JTAG a debugger for PCIe-side
work rather than a separate world — and it works before the host has enumerated
the card, which is the reason to keep it at all.

## 3. `jtag.MAX_BLOCK` is a guard, not a hardware cap

```python
MAX_BLOCK = 1 << 18            # 256 KB, about 3 s
JTAG_BYTES_PER_SECOND = 80_000 # measured
```

A block past `max_block` is **refused rather than taking minutes quietly**. A
ceiling in bytes against a measured rate is the only honest way to say "this will
not be quick", and the refusal message says so, ending with *"or raise
max_block"*.

> **It blocked every `K >= 640` measurement for a whole session, and it was never
> the bandwidth.** `res640 conv1`, `attn640 to_q`, `attn1280 to_q` and
> `ff640 geglu` were all recorded as *unrunnable on the card*. 1.6 MB is 20
> seconds at 80 kB/s — slow, entirely affordable for a one-off probe. Setting
> `s.raw.max_block` measured all of them:
>
> | kernel | shape | sw mxint7 p50 / p90 | card p50 / p90 |
> |---|---|---|---|
> | `res640 conv1` | 32x320x640 | 1.739 / 9.90 | 1.716 / 9.80 |
> | `attn640 to_q` | 16x640x640 | 1.630 / 9.84 | 1.662 / 10.30 |
> | `res1280 conv1` | 16x640x1280 | 1.667 / 9.53 | 1.672 / 9.48 |
> | `attn1280 to_q` | 16x1280x1280 | 1.603 / 9.86 | 1.598 / 9.96 |
> | `ff640 geglu` | 16x640x5120 | 1.641 / 9.74 | 1.645 / 10.12 |
>
> The card tracks the software MXFP7 model at every percentile and every shape.
> **Raise `s.raw.max_block` for a probe shape**; the default stays where it is,
> because it is right about the *steady-state* answer — XDMA is the operand path
> and a real operand image over JTAG is a hundred seconds.

The lesson generalises past this knob: a guard whose message names its own escape
hatch is not a limit, and treating one as a limit removes measurements nobody
then knows are missing.

## 4. The board file is where topology lives

`ktpu.hw.board.Board` describes a machine — cluster and vector coordinates, MAG
ports, mesh geometry, capacities, and which transport to open. Two fields
describe where that mesh sits in a mesh-of-meshes:

```python
    mesh_count: int = 1     # 1, 2 or 4 -- the grid is a grid and the id is 2 bits
    mesh_id:    int = 0
```

**One and zero describe every machine that exists today, and describe it
correctly.** A single mesh is not a degraded multi-mesh; it is a grid with one
node in it, which is the same reason every interlink field is reserved-and-zero
rather than absent ([`../interlink/boundary.md`](../interlink/boundary.md)).

`board.global_addr(mesh, word)` builds the `{mesh_id[1:0], local[31:0]}` address
the memory mover takes, and **refuses a mesh this machine does not have** —
because on single-mesh silicon bits `[33:32]` are undecoded, so a remote address
does not fault, it *aliases into local DRAM*. That check has to live in the
driver; there is nothing downstream that can make it.

**This is topology, not version.** `CU_VERSION` does not move for the interlink
and must not be used to detect it: a compiler emitting local addresses and
`mesh_id = 0` produces identical machine code either way, so the version that
answers "is this the bitstream my compiler targets" has the same answer.
Detection is `IL_CAPS` at control offset `0x0080`, which reads zero on any
bitstream that predates the question.

## 5. Related

- [`../isa/orchestrator.md`](../isa/orchestrator.md) — the control program these
  writes carry, and why the link's latency sets the machine's speed
- [`../isa/kernel.md`](../isa/kernel.md) — rounds, and the three limits that cut
  them
- [`../interlink/boundary.md`](../interlink/boundary.md) — the register-level
  contract for telling one machine from four
