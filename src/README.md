# src — the frameworknized RTL tree

Reorganized 2026-08-19 into the framework/product split below. Verified at
the reorganization: every file hash-identical to its pre-reorg self, no
duplicate module in the build cone, every consumer (xsim, v6 Tcl, probe
harness) re-pointed and re-proven green.

## The split

Mirrors the software stack exactly (`driver/kohakuaccel` vs
`driver/kohakutpu`, enforced there by `test_isolation.py`):

| tree | is |
|---|---|
| `kohakuaccel/` | the hardware framework — everything ANY accelerator on this platform reuses |
| `kohakutpu/` | this project: the TPU's compute units, its transform plug-in, its tops |
| `templates/` | user/dev extension points, each shipped with its bench |
| `examples/saxpy/` | the second project (RTL twin of `driver/examples/saxpy`) — the platform's acceptance test |
| `reference/` | unused but load-bearing knowledge; permanent |
| `attic/` | candidates for deletion; nothing here is deleted until reviewed |

A new accelerator = new compute units (from `templates/cu`) + a new ISA on the
software side. XDMA ↔ station bus ↔ MAG ↔ NoC is reused untouched.

**Demonstrated, not claimed**: xsim bench `saxpy_mesh` drives the GENERATED
saxpy mesh (`gen_mesh.py --tokens`, kohakuaccel sources + `src/examples/saxpy`
only) host-style — upload, orchestrator dispatch, readback — 14 checks.

## Names, in full

| short | full | is |
|---|---|---|
| **MAS** | Memory Agent System | the library: agent + mover + interlink (`kohakuaccel/mas`) |
| **MAG** | Memory AGent | the module (`mag.v`), one per mesh; prefix `mag_` is the agent proper |
| **MM** | Memory Mover | the descriptor-driven DMA engine (`mm_*`) |
| **IL** | InterLink | the mesh-to-mesh transport (`mag_link*`, `il_*`) |
| **KSB** | Kohaku Station Bus | the station interconnect (today `sb_*`; renames to `ksb_` in the rename phase — `sb` collides with "scoreboard") |
| **NoC** | Network on Chip | the in-mesh fabric (`noc_*`); `noc_pkt.vh` is the flit contract |
| `kh_` | Kohaku HDL | prefix for NEW framework-owned modules (templates, checkers) |

## Library layout

```
kohakuaccel/
  common/        fifos, sdpram, skid, clk/ (div2, pumpclk)      FILES.f
  axi/           station/ link/ topo/ bd/ simple/               FILES.f
  noc/           router/ endpoint/ ctrl/  noc_pkt.vh            FILES.f
  mas/           core/ mover/ interlink/ transform/(slot doc)   FILES.f
  verif/         AXI RAM models, NoC fixtures, probes           FILES.f
kohakutpu/
  matmul/  vector/  transform/(mx_quant)  top/(+generated,maps) FILES.f each
```

`FILES.f` per library is the single source of truth for build scripts; at
cutover `xsim.py`, the v6 Tcl and the probe harness consume these instead of
their fifteen hand-copied lists.

Placement decisions worth naming: `mx_tdesc` lives in `mas/mover` (the mover's
descriptor walker); `mx_quant` lives in `kohakutpu/transform` (a project
plug-in for a framework slot — `docs/integrate/software-stack.md` coupling
#7); `noc_l2_adapter` is a SHIPPED framework utility in `noc/endpoint`, NOT a
template — the template is the generic endpoint-link adapter interface.
