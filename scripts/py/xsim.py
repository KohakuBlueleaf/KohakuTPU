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

BENCHES = {
    "cluster_node": (
        "mx_cluster_node_tb",
        COMMON + MATMUL + ["tests/matmul/mx_cluster_node_tb.v"],
    ),
    "acu": (
        "mx_acu_fp_tb",
        COMMON + MATMUL + ["tests/matmul/mx_acu_fp_tb.v"],
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
    # circuit produced; the expected values come from kohakutpu.mxfp7, so it is
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
    # MAG alone, two sources writing concurrently. Cheap, and it covers the
    # write-slot table directly rather than through a whole GEMM.
    "mag_wslot": (
        "mag_wslot_tb",
        COMMON
        + NOC
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
    args = ap.parse_args()

    top, srcs = BENCHES[args.bench]
    work = ROOT / "build" / f"xsim_{args.bench}"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    env = dict(os.environ)
    env["PATH"] = str(VIVADO) + ";" + env["PATH"]

    def run(cmd):
        # Windows will not resolve a .bat through CreateProcess, so name it in
        # full rather than relying on PATH.
        # check=False: a failing tool's output is printed below, which is more
        # useful than a traceback that hides it
        r = subprocess.run(
            [str(VIVADO / cmd[0])] + cmd[1:],
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if r.returncode:
            print(r.stdout[-6000:], r.stderr[-2000:])
            sys.exit(f"failed: {cmd[0]}")
        return r.stdout

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
    if args.model == 0:
        run(["xvlog.bat", "-work", "w", str(VIVADO / ".." / "data/verilog/src/glbl.v")])
        tops.append("w.glbl")
        libs += ["-L", "unisims_ver"]
    run(
        ["xelab.bat", "-debug", "typical", "-timescale", "1ns/1ps"]
        + libs
        + tops
        + ["-s", "tb"]
    )
    out = run(["xsim.bat", "tb", "-runall"])

    # The benches print their own results indented; ERROR lines do NOT match
    # that shape, because `$display("%0t ERROR ...")` starts with the timestamp
    # -- a digit. Filtering on the indent alone therefore discarded every
    # assertion monitor in the project: the NoC's "flit LOST -- sender did not
    # hold", the accumulator's reuse-window check, the manager's L1-overlap
    # check, the drain-queue overflow check. All of them exist to make a failure
    # loud, and all of them were being thrown away before anyone could read one.
    body = [
        ln
        for ln in out.splitlines()
        if ln.startswith(("---", "    ", "  ", "===")) or "ERROR" in ln
    ]
    print("\n".join(body))
    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)
    sys.exit(0 if "  PASS" in out else 1)


if __name__ == "__main__":
    main()
