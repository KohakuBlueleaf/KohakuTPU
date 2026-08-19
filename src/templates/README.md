# templates — the extension points

Each template is a working skeleton plus its self-checking bench. A template
without a bench is a trap; none ships without one. New framework-owned files
use the `kh_` prefix.

| dir | slot | contract | status |
|---|---|---|---|
| `cu/` | a compute unit on the NoC | `docs/spec/compute-unit-port.md`, `flit-format.md`; caps/CU_VERSION rules in `noc_cu_base` | **DONE** — `kh_cu_template.v` + `_tb.v`, xsim bench `cu_template`, 13 checks. Worked example project: `examples/saxpy` (bench `saxpy_cu`, 20 checks). |
| `transform/` | the memory-port transform stage (per-request, flag-selected) | `docs/spec/transform-slot.md` (extracted from `mag_mem_port` while building this) | **DONE** — `kh_transform_template.v` + `_tb.v`, xsim bench `transform_template`, 12 checks. |
| `adapter/` | the endpoint-link adapter (observe/intercept between a router local port and its endpoint; pass-through = straight wire) | interface shape from `reference/poc`; `noc_l2_adapter` is the shipped PRODUCTION example of the slot, not a template | **DONE** — `kh_endpoint_adapter_template.v` + `_tb.v`, xsim bench `adapter_template`, 4 checks + STAGE=0 transparency assert. |

Shipped alongside: `kohakuaccel/verif/kh_port_check.v` — a bindable
simulation checker for the FORCED conventions (hold-until-taken data
stability, valid-drop, busy-wedge watchdog). Every template bench and the
saxpy bench mount it; `report`/`violations` fold into each bench's PASS line.
