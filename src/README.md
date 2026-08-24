# src — the RTL tree

Split into a framework and the projects built on it. The split is **measured,
not asserted**: `scripts/py/deps.py` reads every instantiation under
`kohakuaccel/` and fails the run on one whose module is defined only under a
project. It is in the standard check suite.

## The split

Mirrors the software stack exactly (`driver/kohakuaccel` vs `driver/kohakutpu`,
enforced there by `test_isolation.py`):

| tree | is |
|---|---|
| `kohakuaccel/` | the hardware framework — everything ANY accelerator on this platform reuses |
| `kohakutpu/` | a project: the TPU's compute units, its transform occupant, its tops |
| `kohakumpe/` | a project: the SIMT PE, and the SIMD unit that fills the framework's `SIMD_EN` slot |
| `templates/` | user/dev extension points, each shipped with its bench |
| `examples/saxpy/` | a second project (RTL twin of `driver/examples/saxpy`) — the platform's acceptance test |
| `reference/` | unused but load-bearing knowledge; permanent |
| `attic/` | candidates for deletion; nothing here is deleted until reviewed |

A project may name the framework and another project. **The framework may name
neither.** A new accelerator is new compute units (from `templates/cu`) plus a
new ISA on the software side; XDMA ↔ station bus ↔ MAG ↔ NoC is reused untouched.

**Demonstrated, not claimed**: xsim bench `saxpy_mesh` drives the GENERATED
saxpy mesh (`gen_mesh.py --tokens`, kohakuaccel sources + `src/examples/saxpy`
only) host-style — upload, orchestrator dispatch, readback — 14 checks.

### The one shape that crosses the line

A **slot**: the framework names a module behind a parameter that is 0 by
default, so a framework-only build never elaborates it and the name need not
resolve. There are three, and `deps.py` lists them with the reason.

| slot | named at | filled by |
|---|---|---|
| `xform_bank` | `mag_xform`, the mover's read return | `kohakutpu/transform/`, or `templates/transform/` |
| `khs_unit` | `rv_core`, at `SIMD_EN` | `kohakumpe/simd/` |
| `khs_scalar_decode` | `rv_id`, at `SIMD_EN` | `kohakumpe/simd/` |

Anything else that reaches from `kohakuaccel/` into a project is a defect, and
the check fails on it rather than reporting it.

## Names, in full

| short | full | is |
|---|---|---|
| **system node** | — | the block: agent + mover + transform slot + control processor + interlink (`kohakuaccel/sysnode`). **Never a plain "node"** — a NoC endpoint is a node, and every compute unit sits on one |
| **MAG** | Memory AGent | the module (`mag.v`), one per mesh; prefix `mag_` is the agent proper |
| **MM** | Memory Mover | the descriptor-driven DMA engine (`mm_*`) |
| **IL** | InterLink | the mesh-to-mesh transport (`mag_link*`, `il_*`) |
| **SB** | Station Bus | the station interconnect (`sb_*`) |
| **NoC** | Network on Chip | the in-mesh fabric (`noc_*`); `noc_pkt.vh` is the flit contract |
| **KHS** | KohakuSIMD | the SIMD extension (`khs_*`), a project's, at the framework's `SIMD_EN` |
| **KHT** | KohakuSIMT | the SIMT PE (`kht_*`), a project's, with its own top |
| `kh_` | Kohaku HDL | prefix for NEW framework-owned modules (templates, checkers) |

## Library layout

```
kohakuaccel/
  common/        fifos, sdpram, skid, clk/ (div2, pumpclk)
  axi/           station/ link/ topo/ bd/ simple/
  noc/           router/ endpoint/ ctrl/  noc_pkt.vh
  sysnode/       core/ (incl. sn_hub) mover/ interlink/ cpu/ + sysnode.v
                 ONE component: `mag` and the control processor are a
                 division of design, not of module, and the hub owns
                 every NoC attachment
  pe/rv32/       core/ mem/ noc/  + rv_pe.v
  verif/         AXI RAM models, NoC fixtures, probes
kohakutpu/
  matmul/  vector/  transform/(mx_quant)  top/(+generated, maps)
kohakumpe/
  simd/(khs_*, generated/)   simt/(kht_*, generated/)
```

**The build list is [`scripts/py/xsim.py`](../scripts/py/xsim.py)**, and only
that: its `BENCHES` dict and the shared source constants above it.

Each library also carries a `FILES.f` — every `.v` and `.vh` under it, sorted,
relative to `src/`. It is an **inventory, not a build list**: nothing elaborates
from one, so adding a file to a manifest by hand changes no build. Generate them
with `scripts/py/filesf.py`; `--check` is in the standard suite. That check
exists because an inventory nobody verifies stops being one — 7 of the 12 had
drifted, always by omission, and `sysnode/FILES.f` had dropped `sysnode.v`, the
library's own top.

Placement decisions worth naming: `mx_tdesc` lives in `sysnode/mover` (the
mover's descriptor walker); `mx_quant` lives in `kohakutpu/transform` (a project
occupant for a framework slot — `docs/integrate/software-stack.md` coupling #7);
`noc_l2_adapter` is a SHIPPED framework utility in `noc/endpoint`, NOT a
template — the template is the generic endpoint-link adapter interface.
