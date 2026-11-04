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

Depends on `src/kohakuaccel/common/sync_fifo.v`.

Protocol and packet format: [`docs/arch/noc/README.md`](../../../docs/arch/noc/README.md).
Writing a compute unit: [`docs/arch/noc/compute-unit-port.md`](../../../docs/arch/noc/compute-unit-port.md).
Running the testbenches: [`docs/workflow/simulate.md`](../../../docs/workflow/simulate.md).

```powershell
.\tests\run_noc_sim.ps1
```
