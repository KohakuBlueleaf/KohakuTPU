# KohakuNoC

2D-mesh network-on-chip for connecting compute units to each other and to memory.

| File | Role |
|---|---|
| `noc_router.v` | 5-port router: north / east / south / west / local |
| `noc_inport.v` | per-input FIFO, route computation, one holding slot |
| `noc_outport.v` | round-robin arbitration across the five inputs; owns the outbound register |
| `noc_orchestrator.v` | AXI4 slave <-> NoC local port: flit mailbox, instruction dispatch, status mirror |
| `noc_cu_base.v` | the part every compute unit needs: instruction FIFO, CU_CTRL, completion signalling |
| `noc_pkt.vh` | packet header layout, single source of truth |

Depends on `src/common/sync_fifo.v`.

Protocol and packet format: [`docs/noc/spec.md`](../../docs/noc/spec.md).
Writing a compute unit: [`docs/noc/cu-framework.md`](../../docs/noc/cu-framework.md).
Running the testbenches: [`docs/simulation.md`](../../docs/simulation.md).

```powershell
.\tests\run_noc_sim.ps1
```
