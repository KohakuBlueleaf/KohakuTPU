"""The sweep's numbers as Markdown, from whatever records DIR holds.

    python scripts/py/measure/mmeff/report.py DIR

Reads DIR/calls.json (`sweep.py`) and DIR/mover.json (`mover_model.py`); a
record absent is a section absent. Only reads.
"""

import json
import sys
from pathlib import Path


def calls(r):
    print("## Each knob against its base (derived: changed launches, call-weighted)\n")
    print(
        f"workloads {r['workloads']}, pairings {list(r['pairings'])}, "
        f"{r['counts']['captures']} captures, {r['counts']['kernels']} kernels "
        f"measured, {r['counts']['contractions']} contractions; output parity: "
        f"{r['parity_right']}; {r['wall_seconds']:.0f} s ({r['model_seconds']:.0f} s "
        f"on the model) over {r['invocations']} invocations\n"
    )
    print(
        f"coverage {r['coverage']} ({r['left_unmeasured']} kernels of it left"
        f" unmeasured); warm spread {r['warm_spread']:.4f} over "
        f"{len(r['warm_sample'])} kernels\n"
    )
    print("| kind | a (cycles / work) | b | points | rho | rms relative residual |")
    print("|---|---:|---:|---:|---:|---:|")
    for kind, f in r["fits"].items():
        print(
            f"| {kind} | {f['a']:.5g} | {f['b']:.0f} | {f['points']} | {f['rho']} "
            f"| {f['rms_relative']} |"
        )
    print("\nover the per-call cap (derived (proxy fit) for every knob):\n")
    for kid, work, cycles in r["over_cap"]:
        print(f"- {kid} {work}: {cycles:.0f} fitted cycles")
    print(
        "\n| knob | base | value | measured delta | measured noise | derived (proxy fit)"
        " delta | derived noise | covered | over cap | wins | default |"
    )
    print("|---|---|---|---:|---:|---:|---:|---:|---:|---|---|")
    for k in r["knobs"]:
        base = {
            n: v for n, v in k["base"].items() if n in r["requires"].get(k["knob"], {})
        }
        default = (
            f"{k['winner']} (pending owner run)"
            if k["pending_owner_run"]
            else k["winner"]
        )
        for v, row in k["values"].items():
            print(
                f"| {k['knob']} | {base or 'off'} | {v} | {row['measured_delta']:.0f}"
                f" | {row['measured_noise']:.0f} | {row['derived_delta']:.0f}"
                f" | {row['derived_noise']:.0f} | {k['covered_fraction']:.2f} | {k['over_cap_fraction']:.2f}"
                f" | {row['wins']} | {default} |"
            )
    print(f"\ndefaults: {json.dumps(r['defaults'])}\n")


def mover(r):
    print("## Mover timing (cycles = latency * ops + per_word * words)\n")
    print(
        "| op | latency | per word | max abs residual | points (ops, words, cycles) |"
    )
    print("|---|---:|---:|---:|---|")
    for op, t in r["timing"].items():
        print(
            f"| {op} | {t['latency']:.1f} | {t['per_word']:.3f} "
            f"| {t['max_abs_residual']:.1f} | {t['points']} |"
        )
    print(f"\nall right: {r['all_right']}, repeat {r['repeat']}, build {r['build']}\n")


def main():
    d = Path(sys.argv[1])
    if (d / "calls.json").exists():
        calls(json.loads((d / "calls.json").read_text()))
    if (d / "mover.json").exists():
        mover(json.loads((d / "mover.json").read_text()))


if __name__ == "__main__":
    main()
