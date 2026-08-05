# HakuNoC

2D-mesh network-on-chip for connecting compute units to each other and to memory.

| File | Role |
|---|---|
| `noc_router.v` | 5-port router: north / east / south / west / local |
| `noc_inport.v` | per-input FIFO, route computation, per-output holding slots |
| `noc_outport.v` | round-robin arbitration across the five input slots |

Depends on `src/common/uram_fifo.v`.

Protocol and packet format: [`docs/noc/spec.md`](../../docs/noc/spec.md).
Testbenches: [`tests/noc/`](../../tests/noc).
