"""Run a Verilog bench under Vivado xsim.

    python scripts/py/xsim.py cluster_node
    python scripts/py/xsim.py cluster_node --model 0 --keep

The iverilog wrapper elsewhere cannot run these: the memory primitives are XPM
and the DSP model needs the Xilinx libraries. Benches are named here rather
than passed as file lists so that adding a source file is one edit, not one per
caller.
"""

import argparse
import os
import pathlib
import shutil
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
VIVADO = pathlib.Path(r"D:\Xilinx\Vivado\2024.2\bin")

COMMON = [
    "src/kohakuaccel/common/sync_fifo.v",
    "src/kohakuaccel/common/kohaku_sdpram.v",
]

MATMUL = [
    "src/kohakutpu/matmul/mx_mac.v",
    "src/kohakutpu/matmul/mx_tcu.v",
    "src/kohakutpu/matmul/mx_fpacc.v",
    "src/kohakutpu/matmul/mx_acu_fp.v",
    "src/kohakutpu/matmul/mx_cluster_core.v",
    "src/kohakutpu/matmul/mx_cluster_mgr.v",
    "src/kohakutpu/matmul/mx_cluster_node.v",
    # The double-pumped hierarchy. Parsed everywhere, instantiated only where a
    # bench asks for it -- mx_cluster_node_pump_tb, or cluster_data -d MX_CU_PUMP.
    "src/kohakutpu/matmul/mx_acu_fp_pump.v",
    "src/kohakutpu/matmul/mx_cluster_mgr_pump.v",
    "src/kohakutpu/matmul/mx_cluster_node_pump.v",
]
NOC = [
    "src/kohakuaccel/noc/router/noc_inport.v",
    "src/kohakuaccel/noc/router/noc_outport.v",
    "src/kohakuaccel/noc/router/noc_router.v",
    "src/kohakuaccel/noc/ctrl/noc_orchestrator.v",
    "src/kohakuaccel/noc/endpoint/noc_cu_base.v",
    # mm_mesh instantiates the addon slot unconditionally; at L2_CU=0 it
    # generates the straight wire, but it still has to elaborate.
    "src/kohakuaccel/noc/endpoint/noc_l2_adapter.v",
    # Every generated mesh now crosses MAG into the NoC with a pair of these,
    # and noc_local_cdc is only the wrapper -- async_fifo is what it holds.
    "src/kohakuaccel/noc/endpoint/noc_local_cdc.v",
    "src/kohakuaccel/common/async_fifo.v",
]

# The mover and its PRNG. mx_tdesc is the descriptor walker they borrow rather
# than reimplement, and MAG now instantiates all three.
MOVER = [
    "src/kohakuaccel/common/sb_skid.v",
    "src/kohakuaccel/mas/mover/mx_tdesc.v",
    "src/kohakuaccel/mas/mover/mm_prng.v",
    "src/kohakuaccel/mas/mover/mm_mover.v",
    # mag.v instantiates the staging store unconditionally; at STAGE=0 the
    # generate is empty, but it still has to elaborate.
    "src/kohakuaccel/mas/core/mag_stage.v",
]

VECTOR = [
    "src/kohakutpu/vector/vec_dsp.v",
    "src/kohakutpu/vector/vec_delay.v",
    "src/kohakutpu/vector/vec_tables.v",
    "src/kohakutpu/vector/vec_alu.v",
]

# One whole mesh behind one packed master, interlink included: everything
# `ktpu_min_1m` needs. Shared by the 2-mesh and the chain benches.
MESH_1M = (
    COMMON
    + NOC
    + MATMUL
    + MOVER
    + VECTOR
    + [
        "src/kohakutpu/matmul/mx_cluster_cu.v",
        "src/kohakutpu/vector/vec_cvt.v",
        "src/kohakutpu/vector/vec_regfile.v",
        "src/kohakutpu/vector/vec_lanes.v",
        "src/kohakutpu/vector/vec_agu.v",
        "src/kohakutpu/vector/vec_core.v",
        "src/kohakutpu/vector/vec_cu.v",
        "src/kohakutpu/transform/mx_quant.v",
        "src/kohakuaccel/verif/axi_ram.v",
        "src/kohakuaccel/common/sb_skid.v",
        "src/kohakuaccel/mas/core/mag_mem_port.v",
        "src/kohakuaccel/mas/interlink/il_pkt_arb.v",
        "src/kohakuaccel/mas/interlink/mag_link.v",
        "src/kohakuaccel/mas/interlink/mag_link_pipe.v",
        "src/kohakuaccel/mas/interlink/mag_switch.v",
        "src/kohakuaccel/mas/interlink/mag_ilink.v",
        "src/kohakuaccel/mas/core/mag.v",
        "src/kohakuaccel/common/async_fifo.v",
        "src/kohakuaccel/mas/core/mag_stage_port.v",
        "src/kohakuaccel/mas/core/mag_dram_port.v",
        "src/kohakutpu/top/mag_1m.v",
        "src/kohakutpu/top/generated/ktpu_min_1m.v",
    ]
)

MESH_CDC = (
    COMMON
    + NOC
    + MATMUL
    + MOVER
    + VECTOR
    + [
        "src/kohakutpu/matmul/mx_cluster_cu.v",
        "src/kohakutpu/vector/vec_cvt.v",
        "src/kohakutpu/vector/vec_regfile.v",
        "src/kohakutpu/vector/vec_lanes.v",
        "src/kohakutpu/vector/vec_agu.v",
        "src/kohakutpu/vector/vec_core.v",
        "src/kohakutpu/vector/vec_cu.v",
        "src/kohakutpu/transform/mx_quant.v",
        "src/kohakuaccel/verif/axi_ram.v",
        "src/kohakuaccel/common/sb_skid.v",
        "src/kohakuaccel/mas/core/mag_mem_port.v",
        "src/kohakuaccel/mas/core/mag.v",
        "src/kohakuaccel/common/async_fifo.v",
        "src/kohakuaccel/mas/core/mag_stage_port.v",
        "src/kohakuaccel/mas/core/mag_dram_port.v",
        "src/kohakuaccel/noc/endpoint/noc_local_cdc.v",
        "src/kohakutpu/top/mm_mesh.v",
        "tests/mas/mm_mesh_tb.v",
        "tests/mas/mm_mesh_cdc_tb.v",
    ]
)

# xpm_cdc instantiates glbl, so an async FIFO drags it in even at MODEL=1.
NEEDS_GLBL = {
    "axi_n1",
    "mover_chain1",
    "mover_chain2",
    "mover_chain4",
    "mag_dram_port",
    "mag_dram_port_bw",
    "mag_dram_port_r1",
    "mm_mesh_1m",
    "mag_1m_upload",
    "interlink_2mesh_1m",
    "interlink_stage",
    "mag_link_cdc",
    "interlink_cdc_chain",
    "mm_mesh_cdc",
    "mm_mesh_cdc_slow",
    "mm_mesh_peer_cdc",
    "saxpy_mesh",
}

BENCHES = {
    # N masters onto one slave across two clocks, the SmartConnect replacement.
    # Randomised backpressure on every channel and both clock ratios in one run,
    # because the failure mode is a hang rather than a wrong answer.
    "axi_n1": (
        "axi_n1_tb",
        [
            "src/kohakuaccel/common/sync_fifo.v",
            "src/kohakuaccel/common/async_fifo.v",
            "src/kohakuaccel/axi/simple/axi_n1.v",
            "tests/axi/axi_n1_tb.v",
        ],
    ),
    # The station bus at root_smc's shape -- 3 managers, 9 subordinates, four
    # non-harmonic clocks, widths 32/512. The SmartConnect replacement candidate.
    "sb_root9": (
        "sb_root9_tb",
        [
            "src/kohakuaccel/common/sync_fifo.v",
            "src/kohakuaccel/common/async_fifo.v",
            "src/kohakuaccel/common/sb_skid.v",
            "src/kohakuaccel/axi/station/sb_hub.v",
            "src/kohakuaccel/axi/station/sb_station.v",
            "src/kohakuaccel/axi/station/sb_nmu.v",
            "src/kohakuaccel/axi/station/sb_nsu.v",
            "src/kohakuaccel/axi/topo/sb_root9.v",
            "src/kohakuaccel/verif/axi4_ram.v",
            "tests/axi/sb_axi_check.v",
            "tests/axi/sb_root9_tb.v",
        ],
    ),
    # Two stations over a link. Everything a single station cannot reach: the
    # far hub routing on dport, SRC_PASS, and credit flow control.
    "sb_chain2": (
        "sb_chain2_tb",
        [
            "src/kohakuaccel/common/sync_fifo.v",
            "src/kohakuaccel/common/async_fifo.v",
            "src/kohakuaccel/common/sb_skid.v",
            "src/kohakuaccel/axi/station/sb_hub.v",
            "src/kohakuaccel/axi/station/sb_station.v",
            "src/kohakuaccel/axi/station/sb_nmu.v",
            "src/kohakuaccel/axi/station/sb_nsu.v",
            "src/kohakuaccel/axi/link/sb_link.v",
            "src/kohakuaccel/axi/topo/sb_chain2.v",
            "src/kohakuaccel/verif/axi4_ram.v",
            "tests/axi/sb_chain2_tb.v",
        ],
    ),
    # Width conversion alone: one NMU, one NSU, one clock. -d W_MW/-d W_SDW
    # pick the ratio, so a failure names the width pair not a system symptom.
    "sb_width": (
        "sb_width_tb",
        [
            "src/kohakuaccel/common/sync_fifo.v",
            "src/kohakuaccel/common/async_fifo.v",
            "src/kohakuaccel/axi/station/sb_nmu.v",
            "src/kohakuaccel/axi/station/sb_nsu.v",
            "src/kohakuaccel/verif/axi4_ram.v",
            "tests/axi/sb_width_tb.v",
        ],
    ),
    # Four stations on a LINE, managers on station 1. Station 3 is two hops, so
    # this is the only bench that exercises forwarding THROUGH a station.
    "sb_line4": (
        "sb_line4_tb",
        [
            "src/kohakuaccel/common/sync_fifo.v",
            "src/kohakuaccel/common/async_fifo.v",
            "src/kohakuaccel/common/sb_skid.v",
            "src/kohakuaccel/axi/station/sb_hub.v",
            "src/kohakuaccel/axi/station/sb_nmu.v",
            "src/kohakuaccel/axi/station/sb_nsu.v",
            "src/kohakuaccel/axi/link/sb_link.v",
            "src/kohakuaccel/axi/link/sb_link_cdc.v",
            "src/kohakuaccel/axi/topo/sb_stn_line.v",
            "src/kohakuaccel/axi/topo/sb_line4.v",
            "src/kohakuaccel/verif/axi4_ram.v",
            "tests/axi/sb_axi_check.v",
            "tests/axi/sb_line4_tb.v",
        ],
    ),
    # THE ONLY BENCH where the station bus meets a real mesh. Every other bus
    # bench drives block RAM, so nothing proved the two protocols agree.
    "sb_mesh_e2e": (
        "sb_mesh_e2e_tb",
        COMMON
        + NOC
        + MATMUL
        + MOVER
        + VECTOR
        + [
            "src/kohakutpu/matmul/mx_cluster_cu.v",
            "src/kohakutpu/vector/vec_cvt.v",
            "src/kohakutpu/vector/vec_regfile.v",
            "src/kohakutpu/vector/vec_lanes.v",
            "src/kohakutpu/vector/vec_agu.v",
            "src/kohakutpu/vector/vec_core.v",
            "src/kohakutpu/vector/vec_cu.v",
            "src/kohakutpu/transform/mx_quant.v",
            "src/kohakuaccel/verif/axi_ram.v",
            "src/kohakuaccel/mas/core/mag_mem_port.v",
            "src/kohakuaccel/mas/interlink/il_pkt_arb.v",
            "src/kohakuaccel/mas/interlink/mag_link.v",
            "src/kohakuaccel/mas/interlink/mag_link_pipe.v",
            "src/kohakuaccel/mas/interlink/mag_switch.v",
            "src/kohakuaccel/mas/interlink/mag_ilink.v",
            "src/kohakuaccel/mas/core/mag.v",
            "src/kohakuaccel/common/async_fifo.v",
            "src/kohakuaccel/mas/core/mag_stage_port.v",
            "src/kohakuaccel/mas/core/mag_dram_port.v",
            "src/kohakutpu/top/mag_1m.v",
            "src/kohakutpu/top/generated/ktpu_min_1m.v",
            "src/kohakuaccel/common/sb_skid.v",
            "src/kohakuaccel/axi/station/sb_hub.v",
            "src/kohakuaccel/axi/station/sb_nmu.v",
            "src/kohakuaccel/axi/station/sb_nsu.v",
            "src/kohakuaccel/axi/link/sb_link.v",
            "src/kohakuaccel/axi/link/sb_link_cdc.v",
            "src/kohakuaccel/axi/topo/sb_stn_line.v",
            "src/kohakuaccel/axi/topo/sb_line4.v",
            "tests/axi/sb_mesh_e2e_tb.v",
        ],
    ),
    # Four stations, one per SLR: the validation topology. Two links deep in
    # neither direction -- every leaf hangs off the root, as the BD will wire it.
    "sb_quad": (
        "sb_quad_tb",
        [
            "src/kohakuaccel/common/sync_fifo.v",
            "src/kohakuaccel/common/async_fifo.v",
            "src/kohakuaccel/common/sb_skid.v",
            "src/kohakuaccel/axi/station/sb_hub.v",
            "src/kohakuaccel/axi/station/sb_station.v",
            "src/kohakuaccel/axi/station/sb_nmu.v",
            "src/kohakuaccel/axi/station/sb_nsu.v",
            "src/kohakuaccel/axi/link/sb_link.v",
            "src/kohakuaccel/axi/topo/sb_stn_root.v",
            "src/kohakuaccel/axi/topo/sb_stn_leaf.v",
            "src/kohakuaccel/axi/topo/sb_quad.v",
            "src/kohakuaccel/verif/axi4_ram.v",
            "tests/axi/sb_axi_check.v",
            "tests/axi/sb_quad_tb.v",
        ],
    ),
    # The CU template through the whole port contract, with the convention
    # checker mounted: discovery, batch completions, unknown-flit disposal,
    # hold-until-taken under backpressure.
    # The adapter slot: STAGE=0 transparency, STAGE=1 hold-until-taken under
    # random backpressure, taps counting exactly the transfers.
    "adapter_template": (
        "kh_endpoint_adapter_template_tb",
        [
            "src/kohakuaccel/verif/kh_port_check.v",
            "src/templates/adapter/kh_endpoint_adapter_template.v",
            "src/templates/adapter/kh_endpoint_adapter_template_tb.v",
        ],
    ),
    "cu_template": (
        "kh_cu_template_tb",
        [
            "src/kohakuaccel/common/sync_fifo.v",
            "src/kohakuaccel/noc/endpoint/noc_cu_base.v",
            "src/kohakuaccel/verif/kh_port_check.v",
            "src/templates/cu/kh_cu_template.v",
            "src/templates/cu/kh_cu_template_tb.v",
        ],
    ),
    # The platform proof: the GENERATED saxpy mesh (gen_mesh.py --tokens),
    # kohakuaccel sources + examples/saxpy only, driven host-style through
    # S_AXI_MEM upload, orchestrator dispatch, and readback.
    "saxpy_mesh": (
        "saxpy_mesh_tb",
        COMMON
        + NOC
        + MOVER
        + [
            "src/kohakutpu/transform/mx_quant.v",
            "src/kohakuaccel/verif/axi_ram.v",
            "src/kohakuaccel/mas/core/mag_mem_port.v",
            "src/kohakuaccel/mas/interlink/il_pkt_arb.v",
            "src/kohakuaccel/mas/interlink/mag_link.v",
            "src/kohakuaccel/mas/interlink/mag_link_pipe.v",
            "src/kohakuaccel/mas/interlink/mag_switch.v",
            "src/kohakuaccel/mas/interlink/mag_ilink.v",
            "src/kohakuaccel/mas/core/mag.v",
            "src/kohakuaccel/mas/core/mag_stage_port.v",
            "src/kohakuaccel/mas/core/mag_dram_port.v",
            "src/kohakutpu/top/mag_1m.v",
            "src/examples/saxpy/saxpy_cu.v",
            "src/examples/saxpy/generated/saxpy_mesh.v",
            "src/examples/saxpy/saxpy_mesh_tb.v",
        ],
    ),
    # The example project's RTL half against a memory model: the sw/isa.py
    # encoding, plain reads, a burst write, fault and batch reporting.
    "saxpy_cu": (
        "saxpy_cu_tb",
        [
            "src/kohakuaccel/common/sync_fifo.v",
            "src/kohakuaccel/noc/endpoint/noc_cu_base.v",
            "src/kohakuaccel/verif/kh_port_check.v",
            "src/examples/saxpy/saxpy_cu.v",
            "src/examples/saxpy/saxpy_cu_tb.v",
        ],
    ),
    # The transform-slot template against the three slot rules; the template
    # and its bench ship together (docs/spec/transform-slot.md).
    "transform_template": (
        "kh_transform_template_tb",
        [
            "src/templates/transform/kh_transform_template.v",
            "src/templates/transform/kh_transform_template_tb.v",
        ],
    ),
    # The vector ALU. mx_fpacc.v is here for mx_lead1 alone -- the leading-one
    # search is shared rather than duplicated, and its comment is the reason
    # the search is smear-isolate-encode instead of a loop.
    "vec_alu": (
        "vec_alu_tb",
        ["src/kohakutpu/matmul/mx_fpacc.v"] + VECTOR + ["tests/vector/vec_alu_tb.v"],
    ),
    # The whole core as a NoC endpoint, driven by a simulated NoC stream: the
    # bench is both the agent and the memory, with no mesh in between.
    "vec_cu": (
        "vec_cu_tb",
        COMMON
        + ["src/kohakuaccel/noc/endpoint/noc_cu_base.v", "src/kohakutpu/matmul/mx_fpacc.v"]
        + VECTOR
        + [
            "src/kohakutpu/vector/vec_cvt.v",
            "src/kohakutpu/vector/vec_regfile.v",
            "src/kohakutpu/vector/vec_lanes.v",
            "src/kohakutpu/vector/vec_agu.v",
            "src/kohakutpu/vector/vec_core.v",
            "src/kohakutpu/vector/vec_cu.v",
            "tests/vector/vec_cu_tb.v",
        ],
    ),
    # One register-file slice, then sixteen behind a packed bus.
    "vec_regfile": (
        "vec_regfile_tb",
        COMMON
        + [
            "src/kohakutpu/vector/vec_regfile.v",
            "tests/vector/vec_regfile_tb.v",
        ],
    ),
    # The 16-ALU array and its register file: VMODE topologies and reductions.
    "vec_lanes": (
        "vec_lanes_tb",
        COMMON
        + ["src/kohakutpu/matmul/mx_fpacc.v"]
        + VECTOR
        + [
            "src/kohakutpu/vector/vec_regfile.v",
            "src/kohakutpu/vector/vec_lanes.v",
            "tests/vector/vec_lanes_tb.v",
        ],
    ),
    # The load/store edges. mx_fpacc.v is here for mx_lead1, which normalises an
    # FP16 subnormal on the way in.
    "vec_cvt": (
        "vec_cvt_tb",
        [
            "src/kohakutpu/matmul/mx_fpacc.v",
            "src/kohakutpu/vector/vec_cvt.v",
            "tests/vector/vec_cvt_tb.v",
        ],
    ),
    # Accumulator width -> E8M15, the mesh input a peer transfer arrives on.
    "vec_cvt_acc": (
        "vec_cvt_acc_tb",
        [
            "src/kohakutpu/vector/vec_cvt_acc.v",
            "tests/vector/vec_cvt_acc_tb.v",
        ],
    ),
    # The L2 staging adapter in a local link, both faces played by the bench. A
    # second instance at PASS=1 rides the same stimulus, so bypass is continuous.
    "l2_adapter": (
        "noc_l2_adapter_tb",
        COMMON
        + [
            "src/kohakuaccel/noc/endpoint/noc_l2_adapter.v",
            "tests/noc/noc_l2_adapter_tb.v",
        ],
    ),
    # Agent-side staging: one URAM store behind a reserved range, entry-wide to
    # the fill path and word-wide to the host.
    "mag_stage": (
        "mag_stage_tb",
        COMMON
        + [
            "src/kohakuaccel/mas/core/mag_stage.v",
            "tests/mas/mag_stage_tb.v",
        ],
    ),
    "cluster_node": (
        "mx_cluster_node_tb",
        COMMON + MATMUL + ["tests/matmul/mx_cluster_node_tb.v"],
    ),
    # The 15 tilings against the pumped node. `-d MX_BASE` swaps in the SHIPPING
    # single-clock node at the same 1x period, so ns/sweep is a real speed-up.
    "cluster_node_pump": (
        "mx_cluster_node_pump_tb",
        COMMON + MATMUL + ["tests/matmul/mx_cluster_node_pump_tb.v"],
    ),
    "acu": (
        "mx_acu_fp_tb",
        COMMON + MATMUL + ["tests/matmul/mx_acu_fp_tb.v"],
    ),
    # Another unit writing into a cluster: operands as CU_DATA instead of as
    # memory responses, and a peer sub-tile into the resident tile. The bench
    # is the sender, because nothing in the machine is one yet.
    "cluster_data": (
        "mx_cluster_data_tb",
        COMMON
        + MATMUL
        + [
            "src/kohakuaccel/noc/endpoint/noc_cu_base.v",
            "src/kohakutpu/matmul/mx_cluster_cu.v",
            "src/kohakutpu/matmul/mx_cluster_cu_pump.v",
            "tests/matmul/mx_cluster_data_tb.v",
        ],
    ),
    # The integer-accumulator cluster, superseded by mx_cluster_node's FP
    # accumulator but still built and still tested.
    "cluster": (
        "mx_cluster_tb",
        COMMON
        + MATMUL
        + [
            "src/reference/intcluster/mx_acu.v",
            "src/reference/intcluster/mx_cluster.v",
            "tests/matmul/mx_cluster_tb.v",
        ],
    ),
    "fpacc": (
        "mx_fpacc_tb",
        ["src/kohakutpu/matmul/mx_fpacc.v", "tests/matmul/mx_fpacc_tb.v"],
    ),
    # mx_quant_tb is NOT here: it only dumps what the circuit produced, so
    # scripts/py/run_quant_check.py drives it and does the comparison.
    #
    # The two benches below carry mx_quant.v only because noc_fake_mem
    # instantiates it. mx_matmul_cu never sets the QUANT flag on a read, so
    # these read raw operand words straight out of the memory node.
    "system32": (
        "mx_system32_tb",
        COMMON
        + NOC
        + MATMUL
        + [
            "src/reference/legacy-cu/mx_matmul_cu.v",
            "src/kohakutpu/transform/mx_quant.v",
            "tests/noc/noc_fake_mem.v",
            "tests/noc/mx_system32_tb.v",
        ],
    ),
    "system": (
        "mx_system_tb",
        COMMON
        + NOC
        + MATMUL
        + [
            "src/reference/legacy-cu/mx_matmul_cu.v",
            "src/kohakutpu/transform/mx_quant.v",
            "tests/noc/noc_fake_mem.v",
            "tests/noc/mx_system_tb.v",
        ],
    ),
    "mag_system": (
        "mag_system_tb",
        COMMON
        + NOC
        + MATMUL
        + MOVER
        + [
            "src/kohakutpu/matmul/mx_cluster_cu.v",
            "src/kohakuaccel/axi/simple/axi_xbar2.v",
            "src/kohakuaccel/verif/main_orch.v",
            "src/kohakutpu/transform/mx_quant.v",
            "src/kohakuaccel/verif/axi_ram.v",
            "src/kohakuaccel/common/sb_skid.v",
            "src/kohakuaccel/mas/core/mag_mem_port.v",
            "src/kohakuaccel/mas/core/mag.v",
            "src/kohakuaccel/common/async_fifo.v",
            "src/kohakuaccel/mas/core/mag_stage_port.v",
            "src/kohakuaccel/mas/core/mag_dram_port.v",
            "tests/mas/mag_system_tb.v",
        ],
    ),
    # The minimal machine end to end: MAG, one cluster, one vector core, two
    # routers, with the bench acting as the agent on r11's local port.
    "mm_mesh": (
        "mm_mesh_tb",
        COMMON
        + NOC
        + MATMUL
        + MOVER
        + VECTOR
        + [
            "src/kohakutpu/matmul/mx_cluster_cu.v",
            "src/kohakutpu/vector/vec_cvt.v",
            "src/kohakutpu/vector/vec_regfile.v",
            "src/kohakutpu/vector/vec_lanes.v",
            "src/kohakutpu/vector/vec_agu.v",
            "src/kohakutpu/vector/vec_core.v",
            "src/kohakutpu/vector/vec_cu.v",
            "src/kohakutpu/transform/mx_quant.v",
            "src/kohakuaccel/verif/axi_ram.v",
            "src/kohakuaccel/common/sb_skid.v",
            "src/kohakuaccel/mas/core/mag_mem_port.v",
            "src/kohakuaccel/mas/core/mag.v",
            "src/kohakuaccel/common/async_fifo.v",
            "src/kohakuaccel/mas/core/mag_stage_port.v",
            "src/kohakuaccel/mas/core/mag_dram_port.v",
            "src/kohakutpu/top/mm_mesh.v",
            "tests/mas/mm_mesh_tb.v",
        ],
    ),
    # The same machine with both CUs on an asynchronous unit clock, one arm
    # faster than the mesh and one slower, neither harmonic with it.
    "mm_mesh_cdc": ("mm_mesh_cdc_tb", MESH_CDC),
    "mm_mesh_cdc_slow": ("mm_mesh_cdc_slow_tb", MESH_CDC),
    # Async unit clocks AND the converged L2 together.
    "mm_mesh_cdc_l2": ("mm_mesh_cdc_l2_tb", MESH_CDC),
    "mm_mesh_vfast": ("mm_mesh_vfast_tb", MESH_CDC),
    # MAG behind its own endpoint CDC, and all four component rates at once.
    "mm_mesh_magclk": ("mm_mesh_magclk_tb", MESH_CDC),
    "mm_mesh_5clk": ("mm_mesh_5clk_tb", MESH_CDC),
    "mm_mesh_5clk_l2": ("mm_mesh_5clk_l2_tb", MESH_CDC),
    # The adapter in mm_mesh's cluster link, found by dispatched instructions:
    # CU_CTRL programs it, a real DRAIN is intercepted, DRAM stays clean.
    "mm_mesh_l2": (
        "mm_mesh_l2_tb",
        COMMON
        + NOC
        + MATMUL
        + MOVER
        + VECTOR
        + [
            "src/kohakutpu/matmul/mx_cluster_cu.v",
            "src/kohakutpu/vector/vec_cvt.v",
            "src/kohakutpu/vector/vec_regfile.v",
            "src/kohakutpu/vector/vec_lanes.v",
            "src/kohakutpu/vector/vec_agu.v",
            "src/kohakutpu/vector/vec_core.v",
            "src/kohakutpu/vector/vec_cu.v",
            "src/kohakutpu/transform/mx_quant.v",
            "src/kohakuaccel/verif/axi_ram.v",
            "src/kohakuaccel/common/sb_skid.v",
            "src/kohakuaccel/mas/core/mag_mem_port.v",
            "src/kohakuaccel/mas/core/mag.v",
            "src/kohakuaccel/common/async_fifo.v",
            "src/kohakuaccel/mas/core/mag_stage_port.v",
            "src/kohakuaccel/mas/core/mag_dram_port.v",
            "src/kohakutpu/top/mm_mesh.v",
            "tests/mas/mm_mesh_l2_tb.v",
        ],
    ),
    # MAG staging proved rather than observed: DRAM is poisoned under the staged
    # copy, so a fill that quietly aliased onto DRAM gives a different answer.
    "mm_mesh_stage": (
        "mm_mesh_stage_tb",
        COMMON
        + NOC
        + MATMUL
        + MOVER
        + VECTOR
        + [
            "src/kohakutpu/matmul/mx_cluster_cu.v",
            "src/kohakutpu/vector/vec_cvt.v",
            "src/kohakutpu/vector/vec_regfile.v",
            "src/kohakutpu/vector/vec_lanes.v",
            "src/kohakutpu/vector/vec_agu.v",
            "src/kohakutpu/vector/vec_core.v",
            "src/kohakutpu/vector/vec_cu.v",
            "src/kohakutpu/transform/mx_quant.v",
            "src/kohakuaccel/verif/axi_ram.v",
            "src/kohakuaccel/common/sb_skid.v",
            "src/kohakuaccel/mas/core/mag_mem_port.v",
            "src/kohakuaccel/mas/core/mag.v",
            "src/kohakuaccel/common/async_fifo.v",
            "src/kohakuaccel/mas/core/mag_stage_port.v",
            "src/kohakuaccel/mas/core/mag_dram_port.v",
            "src/kohakutpu/top/mm_mesh.v",
            "tests/mas/mm_mesh_stage_tb.v",
        ],
    ),
    # The same machine, with the cluster draining INTO the vector core rather
    # than into memory: the one path neither cluster_data nor vec_cu joins.
    "mm_mesh_peer": (
        "mm_mesh_peer_tb",
        COMMON
        + NOC
        + MATMUL
        + MOVER
        + VECTOR
        + [
            "src/kohakutpu/matmul/mx_cluster_cu.v",
            "src/kohakutpu/vector/vec_cvt.v",
            "src/kohakutpu/vector/vec_regfile.v",
            "src/kohakutpu/vector/vec_lanes.v",
            "src/kohakutpu/vector/vec_agu.v",
            "src/kohakutpu/vector/vec_core.v",
            "src/kohakutpu/vector/vec_cu.v",
            "src/kohakutpu/transform/mx_quant.v",
            "src/kohakuaccel/verif/axi_ram.v",
            "src/kohakuaccel/common/sb_skid.v",
            "src/kohakuaccel/mas/core/mag_mem_port.v",
            "src/kohakuaccel/mas/core/mag.v",
            "src/kohakuaccel/common/async_fifo.v",
            "src/kohakuaccel/mas/core/mag_stage_port.v",
            "src/kohakuaccel/mas/core/mag_dram_port.v",
            "src/kohakutpu/top/mm_mesh.v",
            "tests/mas/mm_mesh_peer_tb.v",
        ],
    ),
    # The peer drain with both CUs on an asynchronous unit clock: a burst OUT of
    # one unit domain and back IN to the other, through the NoC's.
    "mm_mesh_peer_cdc": (
        "mm_mesh_peer_cdc_tb",
        MESH_CDC
        + [
            "tests/mas/mm_mesh_peer_tb.v",
            "tests/mas/mm_mesh_peer_cdc_tb.v",
        ],
    ),
    # The same machine behind ONE 512-bit master. Proves mag_dram_port carries
    # mover, vector and cluster traffic, not just directed bursts.
    "mm_mesh_1m": (
        "mm_mesh_1m_tb",
        COMMON
        + NOC
        + MATMUL
        + MOVER
        + VECTOR
        + [
            "src/kohakutpu/matmul/mx_cluster_cu.v",
            "src/kohakutpu/vector/vec_cvt.v",
            "src/kohakutpu/vector/vec_regfile.v",
            "src/kohakutpu/vector/vec_lanes.v",
            "src/kohakutpu/vector/vec_agu.v",
            "src/kohakutpu/vector/vec_core.v",
            "src/kohakutpu/vector/vec_cu.v",
            "src/kohakutpu/transform/mx_quant.v",
            "src/kohakuaccel/verif/axi_ram.v",
            "src/kohakuaccel/common/sb_skid.v",
            "src/kohakuaccel/mas/core/mag_mem_port.v",
            "src/kohakuaccel/mas/core/mag.v",
            "src/kohakuaccel/common/async_fifo.v",
            "src/kohakuaccel/mas/core/mag_stage_port.v",
            "src/kohakuaccel/mas/core/mag_dram_port.v",
            "src/kohakuaccel/common/async_fifo.v",
            "src/kohakuaccel/mas/core/mag_stage_port.v",
            "src/kohakuaccel/mas/core/mag_dram_port.v",
            "src/kohakutpu/top/mm_mesh.v",
            "src/kohakutpu/top/mm_mesh_1m.v",
            "tests/mas/mm_mesh_1m_tb.v",
        ],
    ),
    # mag_dram_port alone against an AXI RAM: the partial-beat matrix.
    "mag_dram_port": (
        "mag_dram_port_tb",
        COMMON
        + [
            "src/kohakuaccel/verif/axi_ram.v",
            "src/kohakuaccel/common/async_fifo.v",
            "src/kohakuaccel/mas/core/mag_stage_port.v",
            "src/kohakuaccel/mas/core/mag_dram_port.v",
            "tests/mas/mag_dram_port_tb.v",
        ],
    ),
    # The same bench at R=1, where packing is an identity and RLOG=1 has to be
    # bypassed rather than trusted. A wrapper, because -generic_top splits on =.
    "mag_dram_port_r1": (
        "mag_dram_port_r1_tb",
        COMMON
        + [
            "src/kohakuaccel/verif/axi_ram.v",
            "src/kohakuaccel/common/async_fifo.v",
            "src/kohakuaccel/mas/core/mag_stage_port.v",
            "src/kohakuaccel/mas/core/mag_dram_port.v",
            "tests/mas/mag_dram_port_tb.v",
            "tests/mas/mag_dram_port_r1_tb.v",
        ],
    ),
    # The upload path only: host AXI slave -> MAG -> packed master -> DRAM.
    # mm_mesh discards sm_awready, so this is the only place it is checked.
    "mag_1m_upload": (
        "mag_1m_upload_tb",
        COMMON
        + MOVER
        + [
            "src/kohakuaccel/noc/ctrl/noc_orchestrator.v",
            "src/kohakutpu/transform/mx_quant.v",
            "src/kohakuaccel/common/sb_skid.v",
            "src/kohakuaccel/mas/core/mag_mem_port.v",
            "src/kohakuaccel/mas/interlink/il_pkt_arb.v",
            "src/kohakuaccel/mas/interlink/mag_link.v",
            "src/kohakuaccel/mas/interlink/mag_link_pipe.v",
            "src/kohakuaccel/mas/interlink/mag_switch.v",
            "src/kohakuaccel/mas/interlink/mag_ilink.v",
            "src/kohakuaccel/mas/core/mag.v",
            "src/kohakuaccel/common/async_fifo.v",
            "src/kohakuaccel/mas/core/mag_stage_port.v",
            "src/kohakuaccel/mas/core/mag_dram_port.v",
            "src/kohakuaccel/common/async_fifo.v",
            "src/kohakuaccel/mas/core/mag_stage_port.v",
            "src/kohakuaccel/mas/core/mag_dram_port.v",
            "src/kohakuaccel/verif/axi_ram.v",
            "src/kohakutpu/top/mag_1m.v",
            "tests/mas/mag_1m_upload_tb.v",
        ],
    ),
    # TWO WHOLE MESHES over the interlink, each behind one packed master. A
    # remote write crosses NoC -> MAG -> link -> far MAG -> far DRAM.
    "interlink_2mesh_1m": (
        "interlink_2mesh_1m_tb",
        COMMON
        + NOC
        + MATMUL
        + MOVER
        + VECTOR
        + [
            "src/kohakutpu/matmul/mx_cluster_cu.v",
            "src/kohakutpu/vector/vec_cvt.v",
            "src/kohakutpu/vector/vec_regfile.v",
            "src/kohakutpu/vector/vec_lanes.v",
            "src/kohakutpu/vector/vec_agu.v",
            "src/kohakutpu/vector/vec_core.v",
            "src/kohakutpu/vector/vec_cu.v",
            "src/kohakutpu/transform/mx_quant.v",
            "src/kohakuaccel/verif/axi_ram.v",
            "src/kohakuaccel/common/sb_skid.v",
            "src/kohakuaccel/mas/core/mag_mem_port.v",
            "src/kohakuaccel/mas/interlink/il_pkt_arb.v",
            "src/kohakuaccel/mas/interlink/mag_link.v",
            "src/kohakuaccel/mas/interlink/mag_link_pipe.v",
            "src/kohakuaccel/mas/interlink/mag_switch.v",
            "src/kohakuaccel/mas/interlink/mag_ilink.v",
            "src/kohakuaccel/mas/core/mag.v",
            "src/kohakuaccel/common/async_fifo.v",
            "src/kohakuaccel/mas/core/mag_stage_port.v",
            "src/kohakuaccel/mas/core/mag_dram_port.v",
            "src/kohakuaccel/common/async_fifo.v",
            "src/kohakuaccel/mas/core/mag_stage_port.v",
            "src/kohakuaccel/mas/core/mag_dram_port.v",
            "src/kohakutpu/top/mag_1m.v",
            "src/kohakutpu/top/generated/ktpu_min_1m.v",
            "tests/mas/interlink_2mesh_1m_tb.v",
        ],
    ),
    # Cross-mesh STAGING: mesh 0 drains into mesh 1's aperture 0 over the link,
    # mesh 1 fills from it locally. Mesh 1's DRAM is poison, so an alias shows.
    "interlink_stage": (
        "interlink_stage_tb",
        COMMON
        + MOVER
        + [
            "src/kohakuaccel/noc/ctrl/noc_orchestrator.v",
            "src/kohakutpu/transform/mx_quant.v",
            "src/kohakuaccel/verif/axi_ram.v",
            "src/kohakuaccel/common/sb_skid.v",
            "src/kohakuaccel/mas/core/mag_mem_port.v",
            "src/kohakuaccel/mas/interlink/il_pkt_arb.v",
            "src/kohakuaccel/mas/interlink/mag_link.v",
            "src/kohakuaccel/mas/interlink/mag_link_pipe.v",
            "src/kohakuaccel/mas/interlink/mag_switch.v",
            "src/kohakuaccel/mas/interlink/mag_ilink.v",
            "src/kohakuaccel/mas/core/mag.v",
            "src/kohakuaccel/common/async_fifo.v",
            "src/kohakuaccel/mas/core/mag_stage_port.v",
            "src/kohakuaccel/mas/core/mag_dram_port.v",
            "src/kohakuaccel/common/async_fifo.v",
            "src/kohakuaccel/mas/core/mag_stage_port.v",
            "src/kohakuaccel/mas/core/mag_dram_port.v",
            "src/kohakutpu/top/mag_1m.v",
            "tests/mas/interlink_stage_tb.v",
        ],
    ),
    # The mover across the SLR chain at 1, 2 and 4 MAGs, descriptors written to
    # S_AXI_CTRL the way software writes them. 4 MAGs is mesh0->mesh2, 3 hops.
    "mover_chain1": ("mover_chain1_tb", MESH_1M + ["tests/mas/mover_chain_tb.v"]),
    "mover_chain2": ("mover_chain2_tb", MESH_1M + ["tests/mas/mover_chain_tb.v"]),
    "mover_chain4": ("mover_chain4_tb", MESH_1M + ["tests/mas/mover_chain_tb.v"]),
    # Run with -d MV_L2: the mover moves DRAM -> aperture 0 -> DRAM, the only
    # path that makes it a requester of the MAG store.
    "mover_l2": (
        "mover_chain1_tb",
        MESH_1M + ["src/kohakutpu/top/generated/ktpu_min_1m_l2.v", "tests/mas/mover_chain_tb.v"],
    ),
    # FOUR meshes, a store in each: mesh0 -> mesh3 is three hops through two
    # transit MAGs, so interlink FORWARDING and staging are exercised together.
    "mover_l2_chain": (
        "mover_chain4_tb",
        MESH_1M + ["src/kohakutpu/top/generated/ktpu_min_1m_l2.v", "tests/mas/mover_chain_tb.v"],
    ),
    # Forwarding across two hops, and the ordering that makes a DOORBELL mean
    # "the data ahead of me has landed". Was unregistered, so it never ran.
    "interlink_4mesh": (
        "interlink_4mesh_tb",
        COMMON
        + [
            "src/kohakuaccel/common/async_fifo.v",
            "src/kohakuaccel/mas/interlink/il_pkt_arb.v",
            "src/kohakuaccel/mas/interlink/mag_link.v",
            "src/kohakuaccel/mas/interlink/mag_link_pipe.v",
            "src/kohakuaccel/mas/interlink/mag_switch.v",
            "tests/mas/interlink_4mesh_tb.v",
        ],
    ),
    # interlink_2mesh_tb.v is NOT here: it fails its own checks, superseded by
    # interlink_2mesh_1m. Triage before registering.
    # The link pair alone: credit and framing, no switch above them.
    "mag_link": (
        "mag_link_tb",
        COMMON
        + [
            "src/kohakuaccel/common/async_fifo.v",
            "src/kohakuaccel/mas/interlink/il_pkt_arb.v",
            "src/kohakuaccel/mas/interlink/mag_link.v",
            "src/kohakuaccel/mas/interlink/mag_link_pipe.v",
            "tests/mas/mag_link_tb.v",
        ],
    ),
    # The mover alone against an AXI RAM: no MAG, no NoC, no mesh.
    "mm_mover": (
        "mm_mover_tb",
        COMMON + MOVER + ["src/kohakuaccel/verif/axi_ram.v", "tests/mas/mm_mover_tb.v"],
    ),
    # Workflow bandwidth: conv A', a large copy, a short-run relayout, a fill.
    # mm_mover_v1.v is the pre-burst engine, so before and after are measured.
    "mm_mover_bw": (
        "mm_mover_bw_tb",
        COMMON + MOVER + ["tests/mas/mm_mover_v1.v", "tests/mas/mm_mover_bw_tb.v"],
    ),
    # What the stock mag_dram_port gives one requester against burst length,
    # reads in flight and mesh clock: the ceiling mm_mover_bw is measured against.
    "mag_dram_port_bw": (
        "mag_dram_port_bw_tb",
        COMMON
        + [
            "src/kohakuaccel/common/async_fifo.v",
            "src/kohakuaccel/mas/core/mag_stage_port.v",
            "src/kohakuaccel/mas/core/mag_dram_port.v",
            "tests/mas/mag_dram_port_bw_tb.v",
        ],
    ),
    # The 0-1-3-2 chain at four clocks, swept 1/2/4 deep. Only this bench has
    # TRANSIT: a packet forwarded through a mesh whose rate neither end shares.
    "interlink_cdc_chain": (
        "interlink_cdc_chain_tb",
        [
            "src/kohakuaccel/common/sync_fifo.v",
            "src/kohakuaccel/common/async_fifo.v",
            "src/kohakuaccel/mas/interlink/il_pkt_arb.v",
            "src/kohakuaccel/mas/interlink/mag_link.v",
            "src/kohakuaccel/mas/interlink/mag_switch.v",
            "src/kohakuaccel/mas/interlink/mag_link_cdc.v",
            "tests/mas/interlink_cdc_chain_tb.v",
        ],
    ),
    # One interlink across two mesh clocks. Three ratios, one non-harmonic,
    # and enough beats that credit must recirculate through both crossings.
    "mag_link_cdc": (
        "mag_link_cdc_tb",
        [
            "src/kohakuaccel/common/sync_fifo.v",
            "src/kohakuaccel/common/async_fifo.v",
            "src/kohakuaccel/mas/interlink/mag_link.v",
            "src/kohakuaccel/mas/interlink/mag_link_cdc.v",
            "tests/mas/mag_link_cdc_tb.v",
        ],
    ),
    # Philox-4x32-10 against the published Random123 vectors.
    "mm_prng": (
        "mm_prng_tb",
        ["src/kohakuaccel/mas/mover/mm_prng.v", "tests/mas/mm_prng_tb.v"],
    ),
    # MAG alone, two sources writing concurrently. Cheap, and it covers the
    # write-slot table directly rather than through a whole GEMM.
    "mag_wslot": (
        "mag_wslot_tb",
        COMMON
        + NOC
        + MOVER
        + [
            "src/kohakutpu/transform/mx_quant.v",
            "src/kohakuaccel/verif/axi_ram.v",
            "src/kohakuaccel/common/sb_skid.v",
            "src/kohakuaccel/mas/core/mag_mem_port.v",
            "src/kohakuaccel/mas/core/mag.v",
            "src/kohakuaccel/common/async_fifo.v",
            "src/kohakuaccel/mas/core/mag_stage_port.v",
            "src/kohakuaccel/mas/core/mag_dram_port.v",
            "tests/mas/mag_wslot_tb.v",
        ],
    ),
    # mesh2x2 was DELETED, not disabled. It drove mx_cluster_cu against
    # noc_fake_mem and had fallen a full generation behind on two interfaces at
    # once: it packed the pre-widening instruction layout (8-bit `n`, so every
    # GEMM field landed one byte off), and the stub answers a read with 3'b000
    # where the response word index belongs, so `rword` was always 0 and no L1
    # entry was ever committed. Repairing it meant rewriting it into
    # mag_system_tb / mag_driver_tb NCL=2, which already exist and pass against
    # the real MAG. A bench nobody maintains that grades memory it never waited
    # for is worse than no bench: this one reported wrong ANSWERS for what was
    # actually a hang.
}

# The e2e bench with a SPLIT-RESET ktpu_min_1m substituted: the v6.5 per-domain
# reset entry (kh_rst_sync per MAG/mat/vec domain) proven across the bus's
# fifteen non-harmonic clocks, where a reset released in the wrong domain has
# the best odds of showing. Regenerate the substituted top with:
#   python scripts/py/gen_mesh.py src/kohakutpu/top/maps/mesh_1x1_min.txt \
#     -m ktpu_min_1m --ilink --single-master --split-reset \
#     -o build/gen/ktpu_min_1m_split.v
# One MAG memory port's write path against fairness, ordering and integrity:
# the component bench for the slot starvation the PE system bench surfaced.
BENCHES["mag_mem_port"] = (
    "mag_mem_port_tb",
    [
        "src/kohakuaccel/common/sync_fifo.v",
        "src/kohakuaccel/common/sb_skid.v",
        "src/kohakuaccel/common/kohaku_sdpram.v",
        "src/kohakuaccel/mas/core/mag_stage.v",
        "src/kohakutpu/transform/mx_quant.v",
        "src/kohakuaccel/mas/core/mag_mem_port.v",
        "src/kohakuaccel/verif/axi_ram.v",
        "tests/mas/mag_mem_port_tb.v",
    ],
)
NEEDS_GLBL.add("mag_mem_port")

# ---------------------------------------------------------------- RV32 PE
# The controller PE (src/kohakuaccel/pe/rv32, docs/arch/pe). Its benches read
# programs and golden traces from tests/pe/build, which `python
# tests/pe/tools/rv_gen.py` writes -- run that first or the bench reports no
# cases. RTL layout: core/ is the pipeline, mem/ the two L1s, noc/ the
# fabric attach, rv_pe.v the assembly.
PE_RV32 = [
    "src/kohakuaccel/common/sync_fifo.v",
    "src/kohakuaccel/common/kohaku_sdpram.v",
    "src/kohakuaccel/pe/rv32/mem/rv_ram_be.v",
    "src/kohakuaccel/pe/rv32/mem/rv_imem.v",
    "src/kohakuaccel/pe/rv32/mem/rv_spad.v",
    "src/kohakuaccel/pe/rv32/core/rv_regfile.v",
    "src/kohakuaccel/pe/rv32/core/rv_bpred.v",
    "src/kohakuaccel/pe/rv32/core/rv_if.v",
    "src/kohakuaccel/pe/rv32/core/rv_id.v",
    "src/kohakuaccel/pe/rv32/core/rv_ex.v",
    "src/kohakuaccel/pe/rv32/core/rv_mem.v",
    "src/kohakuaccel/pe/rv32/core/rv_wb.v",
    "src/kohakuaccel/pe/rv32/core/rv_core.v",
    "src/kohakuaccel/pe/rv32/mem/rv_l1.v",
    "src/kohakuaccel/pe/rv32/noc/rv_noc_req.v",
    "src/kohakuaccel/pe/rv32/rv_pe.v",
]

# Level 1: the pipeline against the Python model, one instruction at a time.
BENCHES["rv_core"] = ("rv_core_tb", PE_RV32 + ["tests/pe/tb/rv_core_tb.v"])

# Level 2: the memory frontend -- internal L1, external windows and the NoC
# requestor -- against a scripted stub that plays the memory agent.
BENCHES["rv_front"] = (
    "rv_front_tb",
    PE_RV32 + ["src/kohakuaccel/noc/endpoint/noc_cu_base.v",
               "tests/pe/tb/rv_front_tb.v"],
)

# Level 3: ONE PE, one real router, the real MAG, an AXI RAM. Boot is a NoC
# write into the instruction window and the standard kick.
PE_SYS = (
    PE_RV32
    + [
        "src/kohakuaccel/noc/router/noc_inport.v",
        "src/kohakuaccel/noc/router/noc_outport.v",
        "src/kohakuaccel/noc/router/noc_router.v",
        "src/kohakuaccel/noc/ctrl/noc_orchestrator.v",
        "src/kohakuaccel/noc/endpoint/noc_cu_base.v",
        "src/kohakuaccel/common/async_fifo.v",
        "src/kohakuaccel/common/sb_skid.v",
        "src/kohakuaccel/mas/mover/mx_tdesc.v",
        "src/kohakuaccel/mas/mover/mm_prng.v",
        "src/kohakuaccel/mas/mover/mm_mover.v",
        "src/kohakuaccel/mas/core/mag_stage.v",
        "src/kohakutpu/transform/mx_quant.v",
        "src/kohakuaccel/mas/core/mag_mem_port.v",
        "src/kohakuaccel/mas/core/mag.v",
        "src/kohakuaccel/mas/core/mag_stage_port.v",
        "src/kohakuaccel/mas/core/mag_dram_port.v",
        "src/kohakuaccel/verif/axi4_ram.v",
    ]
)

PE_MESH = PE_SYS + ["tests/pe/tb/rv_mesh.v", "tests/pe/tb/rv_agent.v"]

BENCHES["rv_sys"] = ("rv_sys_tb", PE_MESH + ["tests/pe/tb/rv_sys_tb.v"])

# Level 4: isolation, ping-pong, aggregation and the DRAM hand-off, on ONE NoC
# and ONE MAG. rv_mc1 runs isolation alone, as the other two's floor.
BENCHES["rv_mc1"] = ("rv_mc1_tb", PE_MESH + ["tests/pe/tb/rv_mc_tb.v",
                                             "tests/pe/tb/rv_mc1_tb.v"])
BENCHES["rv_mc2"] = ("rv_mc2_tb", PE_MESH + ["tests/pe/tb/rv_mc_tb.v",
                                             "tests/pe/tb/rv_mc2_tb.v"])
BENCHES["rv_mc4"] = ("rv_mc4_tb", PE_MESH + ["tests/pe/tb/rv_mc_tb.v",
                                             "tests/pe/tb/rv_mc4_tb.v"])

NEEDS_GLBL.update({"rv_core", "rv_front", "rv_sys",
                   "rv_mc1", "rv_mc2", "rv_mc4"})

BENCHES["sb_mesh_e2e_sr"] = (
    "sb_mesh_e2e_tb",
    [
        ("build/gen/ktpu_min_1m_split.v" if s.endswith("/ktpu_min_1m.v") else s)
        for s in BENCHES["sb_mesh_e2e"][1]
    ]
    + ["src/kohakuaccel/common/kh_rst_sync.v"],
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bench", choices=sorted(BENCHES))
    ap.add_argument("--model", type=int, default=1, help="1 = behavioural, 0 = DSP48")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--define", "-d", action="append", default=[])
    # Implies --keep: the VCD lives in the build directory.
    ap.add_argument("--vcd", help="dump this scope, e.g. /mm_mesh_stage_tb/dut")
    ap.add_argument("--vcd-file", default="dump.vcd")
    # BUDGET, not -runall: a hung DUT makes the bench spin its whole SPIN_MAX,
    # so a debug loop should stop at ~1.5x where the answer was due.
    ap.add_argument("--max-time", help="stop after this sim time, e.g. 200us")
    ap.add_argument(
        "--wall",
        type=float,
        default=600.0,
        help="kill the simulation after this many seconds (default 600)",
    )
    # Different benches already own different directories; the SAME bench twice
    # does not, and the second run wipes the first's out from under it.
    ap.add_argument(
        "--build-root",
        default=os.environ.get("KOHAKU_XSIM_BUILD"),
        help="where xsim_<bench> is built (default build/, env KOHAKU_XSIM_BUILD)",
    )
    args = ap.parse_args()

    top, srcs = BENCHES[args.bench]
    work = pathlib.Path(args.build_root or ROOT / "build") / f"xsim_{args.bench}"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    env = dict(os.environ)
    env["PATH"] = str(VIVADO) + ";" + env["PATH"]

    # The benches print their own results indented; ERROR lines do NOT match
    # that shape, because `$display("%0t ERROR ...")` starts with the timestamp
    # -- a digit. Filtering on the indent alone therefore discarded every
    # assertion monitor in the project: the NoC's "flit LOST -- sender did not
    # hold", the accumulator's reuse-window check, the manager's L1-overlap
    # check, the drain-queue overflow check. All of them exist to make a failure
    # loud, and all of them were being thrown away before anyone could read one.
    # Verdict matched STRIPPED: seven benches print `PASS` against the margin, and
    # a two-space test failed them all while this filter hid the line saying so.
    def keep(ln):
        return (
            ln.startswith(("---", "    ", "  ", "==="))
            or "ERROR" in ln
            or ln.strip().startswith(("PASS", "FAIL"))
        )

    def run(cmd, stream=False):
        # Windows will not resolve a .bat through CreateProcess, so name it in
        # full rather than relying on PATH.
        # check=False: a failing tool's output is printed below, which is more
        # useful than a traceback that hides it
        argv = [str(VIVADO / cmd[0])] + cmd[1:]
        if not stream:
            r = subprocess.run(
                argv, cwd=work, env=env, capture_output=True, text=True, check=False
            )
            if r.returncode:
                print(r.stdout[-6000:], r.stderr[-2000:])
                sys.exit(f"failed: {cmd[0]}")
            return r.stdout

        # Streamed line by line. A bench that stalls is then diagnosable by how
        # far it got; captured whole, a run killed on a timeout prints NOTHING,
        # which is indistinguishable from a run that failed to elaborate.
        proc = subprocess.Popen(
            argv,
            cwd=work,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        # WALL CLOCK BOUND. A DUT that hangs makes the bench spin its whole
        # SPIN_MAX, and standalone xsim has no timeout of its own.
        deadline = time.monotonic() + args.wall
        got = []
        for ln in proc.stdout:
            got.append(ln)
            if keep(ln):
                print(ln, end="", flush=True)
            if time.monotonic() > deadline:
                print(f"  STALLED past {args.wall}s -- killing")
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        capture_output=True,
                        check=False,
                    )
                else:
                    proc.kill()
                break
        proc.wait()
        out = "".join(got)
        if proc.returncode:
            print(out[-6000:])
            sys.exit(f"failed: {cmd[0]}")
        return out

    # DEDUPED, order kept: a file listed twice is a duplicate module definition
    # and xvlog rejects the build.
    files = [str(ROOT / p) for p in dict.fromkeys(srcs)]
    libs = ["-L", "xpm"]
    tops = [f"w.{top}"]
    if args.model == 0:
        files.insert(0, str(ROOT / "tests/matmul/mx_model_dsp.v"))

    # Options go through a command file, not the command line: xvlog.bat is a
    # batch script and it splits `-d NAME=VALUE` at the `=`, so the value
    # arrives as a stray filename.
    opts = ["-d " + d for d in args.define + [f"MX_MODEL={args.model}"]]
    (work / "xvlog.f").write_text("\n".join(opts + files) + "\n")

    run(["xvlog.bat", "-sv", "-work", "w", "-f", "xvlog.f"])
    # glbl only where it is needed. Adding it everywhere would hold GSR over
    # every XPM cell for the first 100 ns, which is a behaviour change to benches
    # that pass today -- see the trap in docs/simulation.md s3.
    # An async FIFO is an xpm_cdc, which instantiates glbl. Detected from the
    # source list rather than listed per bench: mag.v now always pulls one in.
    has_cdc = any("async_fifo.v" in f for f in files)
    if args.model == 0 or args.bench in NEEDS_GLBL or has_cdc:
        run(["xvlog.bat", "-work", "w", str(VIVADO / ".." / "data/verilog/src/glbl.v")])
        tops.append("w.glbl")
    if args.model == 0:
        libs += ["-L", "unisims_ver"]
    run(
        ["xelab.bat", "-debug", "typical", "-timescale", "1ns/1ps"]
        + libs
        + tops
        + ["-s", "tb"]
    )
    # --vcd dumps a scope's signals for reading cycle by cycle.
    if args.vcd:
        # COUNTED: a wrong scope returns an empty list and writes a VCD with no
        # $var, which reads as "the signals never moved".
        (work / "dump.tcl").write_text(
            f"set objs [get_objects -r {args.vcd}/*]\n"
            'puts "@@@ VCD [llength $objs] objects"\n'
            'if {[llength $objs] == 0} { error "scope matched nothing" }\n'
            f"open_vcd {args.vcd_file}\n"
            "log_vcd $objs\n"
            "run -all\n"
            "flush_vcd\nclose_vcd\nquit\n"
        )
        out = run(["xsim.bat", "tb", "-t", "dump.tcl"], stream=True)
        print(f"  VCD {work / args.vcd_file}")
    elif args.max_time:
        (work / "budget.tcl").write_text(
            f'run {args.max_time}\nputs "@@@ BUDGET {args.max_time} reached"\nquit\n'
        )
        out = run(["xsim.bat", "tb", "-t", "budget.tcl"], stream=True)
    else:
        out = run(["xsim.bat", "tb", "-runall"], stream=True)

    if not args.keep and not args.vcd:
        shutil.rmtree(work, ignore_errors=True)
    verdicts = [ln.strip() for ln in out.splitlines()]
    passed = any(v.startswith("PASS") for v in verdicts)
    failed = any(v.startswith("FAIL") for v in verdicts)
    sys.exit(0 if passed and not failed else 1)


if __name__ == "__main__":
    main()
