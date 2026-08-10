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

ROOT = pathlib.Path(__file__).resolve().parents[2]
VIVADO = pathlib.Path(r"D:\Xilinx\Vivado\2024.2\bin")

COMMON = [
    "src/common/sync_fifo.v",
    "src/common/kohaku_sdpram.v",
]
MATMUL = [
    "src/kohakutpu/matmul/mx_mac.v",
    "src/kohakutpu/matmul/mx_tcu.v",
    "src/kohakutpu/matmul/mx_fpacc.v",
    "src/kohakutpu/matmul/mx_acu_fp.v",
    "src/kohakutpu/matmul/mx_cluster_core.v",
    "src/kohakutpu/matmul/mx_cluster_mgr.v",
    "src/kohakutpu/matmul/mx_cluster_node.v",
]
NOC = [
    "src/kohakunoc/noc_inport.v",
    "src/kohakunoc/noc_outport.v",
    "src/kohakunoc/noc_router.v",
    "src/kohakunoc/noc_orchestrator.v",
    "src/kohakunoc/noc_cu_base.v",
]

# The mover and its PRNG. mx_tdesc is the descriptor walker they borrow rather
# than reimplement, and MAG now instantiates all three.
MOVER = [
    "src/kohakutpu/matmul/mx_tdesc.v",
    "src/kohakumas/mm_prng.v",
    "src/kohakumas/mm_mover.v",
]

VECTOR = [
    "src/kohakutpu/vector/vec_dsp.v",
    "src/kohakutpu/vector/vec_delay.v",
    "src/kohakutpu/vector/vec_tables.v",
    "src/kohakutpu/vector/vec_alu.v",
]

# xpm_cdc instantiates glbl, so an async FIFO drags it in even at MODEL=1.
NEEDS_GLBL = {"axi_n1"}

BENCHES = {
    # N masters onto one slave across two clocks, the SmartConnect replacement.
    # Randomised backpressure on every channel and both clock ratios in one run,
    # because the failure mode is a hang rather than a wrong answer.
    "axi_n1": (
        "axi_n1_tb",
        [
            "src/common/sync_fifo.v",
            "src/common/async_fifo.v",
            "src/kohakuaxi/axi_n1.v",
            "tests/axi/axi_n1_tb.v",
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
        + ["src/kohakunoc/noc_cu_base.v", "src/kohakutpu/matmul/mx_fpacc.v"]
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
    "cluster_node": (
        "mx_cluster_node_tb",
        COMMON + MATMUL + ["tests/matmul/mx_cluster_node_tb.v"],
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
            "src/kohakunoc/noc_cu_base.v",
            "src/kohakutpu/matmul/mx_cluster_cu.v",
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
            "src/kohakutpu/matmul/mx_acu.v",
            "src/kohakutpu/matmul/mx_cluster.v",
            "tests/matmul/mx_cluster_tb.v",
        ],
    ),
    "fpacc": (
        "mx_fpacc_tb",
        ["src/kohakutpu/matmul/mx_fpacc.v", "tests/matmul/mx_fpacc_tb.v"],
    ),
    # mx_quant_tb is NOT here. It computes nothing and only dumps what the
    # circuit produced; the expected values come from ktpu.hw.mxfp7, so it is
    # driven by driver/run_quant_check.py, which does the comparison.
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
            "src/kohakutpu/matmul/mx_matmul_cu.v",
            "src/kohakumas/mx_quant.v",
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
            "src/kohakutpu/matmul/mx_matmul_cu.v",
            "src/kohakumas/mx_quant.v",
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
            "src/kohakuaxi/axi_xbar2.v",
            "src/kohakuaxi/main_orch.v",
            "src/kohakumas/mx_quant.v",
            "src/kohakumas/axi_ram.v",
            "src/kohakumas/mag_mem_port.v",
            "src/kohakumas/mag.v",
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
            "src/kohakumas/mx_quant.v",
            "src/kohakumas/axi_ram.v",
            "src/kohakumas/mag_mem_port.v",
            "src/kohakumas/mag.v",
            "src/synth_top/mm_mesh.v",
            "tests/mas/mm_mesh_tb.v",
        ],
    ),
    # The mover alone against an AXI RAM: no MAG, no NoC, no mesh.
    "mm_mover": (
        "mm_mover_tb",
        COMMON + MOVER + ["src/kohakumas/axi_ram.v", "tests/mas/mm_mover_tb.v"],
    ),
    # Philox-4x32-10 against the published Random123 vectors.
    "mm_prng": (
        "mm_prng_tb",
        ["src/kohakumas/mm_prng.v", "tests/mas/mm_prng_tb.v"],
    ),
    # MAG alone, two sources writing concurrently. Cheap, and it covers the
    # write-slot table directly rather than through a whole GEMM.
    "mag_wslot": (
        "mag_wslot_tb",
        COMMON
        + NOC
        + MOVER
        + [
            "src/kohakumas/mx_quant.v",
            "src/kohakumas/axi_ram.v",
            "src/kohakumas/mag_mem_port.v",
            "src/kohakumas/mag.v",
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bench", choices=sorted(BENCHES))
    ap.add_argument("--model", type=int, default=1, help="1 = behavioural, 0 = DSP48")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--define", "-d", action="append", default=[])
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
    def keep(ln):
        return ln.startswith(("---", "    ", "  ", "===")) or "ERROR" in ln

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
        got = []
        for ln in proc.stdout:
            got.append(ln)
            if keep(ln):
                print(ln, end="", flush=True)
        proc.wait()
        out = "".join(got)
        if proc.returncode:
            print(out[-6000:])
            sys.exit(f"failed: {cmd[0]}")
        return out

    files = [str(ROOT / p) for p in srcs]
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
    if args.model == 0 or args.bench in NEEDS_GLBL:
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
    out = run(["xsim.bat", "tb", "-runall"], stream=True)

    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)
    sys.exit(0 if "  PASS" in out else 1)


if __name__ == "__main__":
    main()
