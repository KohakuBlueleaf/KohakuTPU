#!/usr/bin/env python3
"""Synthesise a whole generated mesh top out of context.

    python scripts/py/ooc_mesh.py ktpu_ship_2x2_6c2v_1m ktpu_ship_2x2_6c2v_1m_pe

The source list is TAKEN FROM xsim.py's `ctrlpe_mesh` bench rather than written
here: a hand-kept second list drifts, and the first symptom of drift is a top
that synthesises against RTL the benches no longer use. The bench's own top and
testbench are dropped and the requested one substituted.

One run per top, sequentially -- six concurrent Vivado runs is the project's
ceiling and a mesh is far too big to want two of them fighting for memory.
"""

import argparse
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "py"))

import xsim

VIVADO = r"D:\Xilinx\Vivado\2024.2\bin\vivado.bat"
PART = "xcvu13p-fhgb2104-2L-e"

TCL = """
set part {part}
create_project -in_memory -part $part
# `include` is resolved against the fileset's dirs, not the includer's own path,
# so a header one directory down from its user is not found without this.
set_property include_dirs [list {incdirs}] [current_fileset]
{reads}
read_xdc {xdc}
synth_design -top {top} -mode out_of_context -part $part -directive default
if {{[llength [get_clocks -quiet]] == 0}} {{
    error "no clock: ooc_mesh.xdc did not apply, so every figure is unconstrained"
}}
report_utilization -quiet -file {build}/mesh_{top}_util.rpt
report_utilization -hierarchical -hierarchical_depth 3 -quiet \\
    -file {build}/mesh_{top}_hier.rpt
report_timing_summary -quiet -file {build}/mesh_{top}_time.rpt
write_checkpoint -force {build}/mesh_{top}.dcp
set nlut ?; set nff ?; set nbram ?; set ndsp ?; set nuram ?
set u [report_utilization -return_string]
regexp {{CLB LUTs\\D+(\\d+)}}          $u -> nlut
regexp {{CLB Registers\\D+(\\d+)}}     $u -> nff
regexp {{Block RAM Tile\\D+([\\d.]+)}} $u -> nbram
regexp {{URAM\\D+(\\d+)}}              $u -> nuram
regexp {{DSPs\\D+(\\d+)}}              $u -> ndsp
set slk [get_property SLACK [get_timing_paths -max_paths 1 -nworst 1 -setup]]
puts "@@@ MESH {top} LUT=$nlut FF=$nff BRAM=$nbram URAM=$nuram DSP=$ndsp WNS=$slk"
"""


def sources_for(top):
    """The ctrlpe_mesh bench's list, with its own top and TB swapped out."""
    files = xsim.BENCHES["ctrlpe_mesh"][1]
    keep = [
        f
        for f in dict.fromkeys(files)
        if not f.endswith("ctrlpe_mesh_tb.v")
        and "top/generated/" not in f
        and "/verif/" not in f
    ]
    return keep + [f"src/kohakutpu/top/generated/{top}.v"]


def run(top, build):
    reads = "\n".join(
        f"read_verilog {{{(ROOT / p).as_posix()}}}" for p in sources_for(top)
    )
    incdirs = " ".join(
        f"{{{(ROOT / d).as_posix()}}}"
        for d in (
            "src/kohakuaccel/noc",
            "src/kohakumpe/simd",
            "src/kohakumpe/simd/generated",
            "src/kohakumpe/simt",
            "src/kohakumpe/simt/generated",
        )
    )
    body = TCL.format(
        part=PART,
        incdirs=incdirs,
        reads=reads,
        xdc=(ROOT / "scripts" / "xdc" / "ooc_mesh.xdc").as_posix(),
        top=top,
        build=build.as_posix(),
    )
    d = build / f"ooc_mesh_{top}"
    d.mkdir(parents=True, exist_ok=True)
    tcl = d / "run.tcl"
    tcl.write_text(body, encoding="utf-8")
    # check=False deliberately: the caller ORs the return codes so one top's
    # failure does not stop the rest, and the useful diagnosis is the @@@/ERROR
    # lines below rather than a traceback.
    p = subprocess.run(
        [VIVADO, "-mode", "batch", "-nojournal", "-log", "m.log", "-source", str(tcl)],
        cwd=d,
        capture_output=True,
        text=True,
        check=False,
    )
    for line in (p.stdout or "").splitlines():
        if line.startswith(("@@@", "ERROR")):
            print(line, flush=True)
    return p.returncode


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tops", nargs="+")
    args = ap.parse_args()
    build = ROOT / "build"
    rc = 0
    for t in args.tops:
        if not (ROOT / "src/kohakutpu/top/generated" / f"{t}.v").exists():
            sys.exit(f"ooc_mesh: no generated top {t}")
        rc |= run(t, build)
    sys.exit(rc)


if __name__ == "__main__":
    main()
