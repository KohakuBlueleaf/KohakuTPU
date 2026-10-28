"""Measure vector timing with Rust-assembled L1 probes on one replay model at a time.

Run from the repository root: python scripts/py/measure/vc_timing.py --all.
The fixed-size upload separates instruction delivery and scoreboard reset from
execution. Padding consists of unreachable copies of the encoded HALT word.
"""

import argparse
import json
import sys

from kohakutpu.compiler import _rs
from kohakutpu.simulation.verilator.replay import ROOT, build_model, replay

sys.path.insert(0, str(ROOT / "scripts" / "py"))

import gen_card

KS = (1, 2, 4, 8, 16, 32)
KINDS = (
    "math_math",
    "xbar",
    "xbar_rot",
    "unpack_waw",
    "unpack_ring",
    "pack_ring",
    "cond_sync",
    "cond_setvl",
    "cond_seti",
    "mx7a",
    "mx7b",
)
BENCHES = {
    "math_math": ["math_math"],
    "xbar": ["xbar", "xbar_rot", "math_math"],
    "unpack_math": ["unpack_waw", "unpack_ring"],
    "math_pack": ["pack_ring", "math_math"],
    "cond": ["cond_sync", "cond_setvl", "cond_seti"],
    "seti": ["cond_seti"],
    "mx7_issue_gap": ["mx7a", "mx7b"],
    "mx7_pack_words": ["mx7a", "mx7b"],
}


def core_defines(image):
    """Include image overrides, not just the named VEC_CORE entry."""
    return sorted(
        d for d in gen_card.image_defines(image) if d.startswith(("V2_", "VEC_CORE_"))
    )


def build_key(core, defines):
    skid = next(
        (d.split("=", 1)[1] for d in defines if d.startswith("V2_MQ_SKID=")), "0"
    )
    return f"{core}.mq{skid}"


def program(kind, k, vl, dependent, regs=128, copies=2):
    """Unscheduled image; read-only controls change no producer destination."""
    lines = ["seti s0 = 16"] if kind == "cond_setvl" else []
    for i in range(k):
        dst = 0 if dependent else i % (regs - 8) + 1
        src = dst if dependent else regs - 2
        other = regs - 1
        if kind == "math_math":
            lines.append(f"vadd v{dst}, v{src}, v{other} vl={vl}")
        elif kind in ("xbar", "xbar_rot"):
            x = f"bcast(v{src}, lane=0)" if kind == "xbar" else f"rot(v{src}, 1)"
            lines.append(f"vadd v{dst}, v{other}, {x} vl={vl}")
        elif kind == "unpack_waw":
            lines.append(f"vunpk v{dst} <- l1[0] f16 n={vl // 16}")
        elif kind == "unpack_ring":
            lines.extend(
                [
                    f"vunpk v{dst} <- l1[0] f16 n={vl // 16}",
                    f"vcmplt p1, v{src}, v{other} vl={vl}",
                ]
            )
        elif kind == "pack_ring":
            lines.extend(
                [
                    f"vadd v{dst}, v{regs - 2}, v{other} vl={vl}",
                    f"vpack v{src} -> l1[0] f16 n={vl // 16}",
                ]
            )
        elif kind == "cond_sync":
            lines.append("vsync fe on=math")
        elif kind == "cond_setvl":
            lines.append("setvl s0")
        elif kind == "cond_seti":
            lines.append(f"seti s{i % 16 if not dependent else 0} = {i}")
        elif kind in ("mx7a", "mx7b"):
            lines.append(f"vpack v{src} -> l1[{i * 8}] {kind}")
        else:
            raise ValueError(kind)
    lines.append("halt")
    return f"target ktpu.v9 vregs={regs} vcopies={copies}\nimage bench()\n" + "".join(
        f"    {line}\n" for line in lines
    )


def fit(xs, ys):
    """Least-squares affine fit and every residual; no rounded acceptance."""
    n = len(xs)
    slope = (n * sum(x * y for x, y in zip(xs, ys)) - sum(xs) * sum(ys)) / (
        n * sum(x * x for x in xs) - sum(xs) ** 2
    )
    intercept = (sum(ys) - slope * sum(xs)) / n
    residuals = [y - intercept - slope * x for x, y in zip(xs, ys)]
    return {"slope": slope, "intercept": intercept, "residuals": residuals}


def measure_series(kind, vl, dependent, machine, build, defines):
    vc = json.loads(machine)["machine"]["vc"]
    runs = []
    for k in KS:
        text = program(kind, k, vl, dependent, vc["regs"], vc["rf_copies"])
        words, descs = _rs.image(text, "bench", [], machine, entry="reset")
        if len(words) > 256:
            raise ValueError("probe exceeds fixed upload size")
        words += [words[-1]] * (256 - len(words))
        result = replay(
            _rs.vc_flits(descs, words, 0, machine), b"", build=build, defines=defines
        )
        if result.faults or result.warn or result.completions != result.payloads:
            raise RuntimeError(result.log)
        runs.append(
            {
                "program": text,
                "K": k,
                "vl": vl,
                "dependent": dependent,
                "elapsed": result.elapsed,
                "res": result.res,
                "eng": result.eng,
                "payloads": result.payloads,
                "completions": result.completions,
                "faults": result.faults,
                "warn": result.warn,
            }
        )
    counters = {
        f"{engine}.{name}": fit(KS, [r["eng"][engine][name] for r in runs])
        for engine, fields in runs[0]["eng"].items()
        for name in fields
    }
    return {
        "kind": kind,
        "vl": vl,
        "dependent": dependent,
        "runs": runs,
        "fit": fit(KS, [r["elapsed"] for r in runs]),
        "busy_fit": fit(KS, [r["res"]["busy"] for r in runs]),
        "counter_fits": counters,
    }


def exact(probe, counter=None):
    f = probe["counter_fits"][counter] if counter else probe["fit"]
    if any(abs(x) > 1e-9 for x in f["residuals"]):
        raise ValueError(f"non-affine calibration: {probe['kind']} {f}")
    if not counter and probe["busy_fit"]["slope"] != f["slope"]:
        raise ValueError("elapsed and busy slopes disagree")
    return f["slope"]


def values(probes, names=tuple(BENCHES)):
    """Recover the named edges from independent calibrations."""
    by = {(p["kind"], p["vl"], p["dependent"]): p for p in probes}
    candidates = {name: [] for name in names}
    for vl in (16, 64, 128):
        b = vl // 16
        if set(names) & {"math_math", "math_pack", "xbar"}:
            mm = exact(by["math_math", vl, True]) - b
            if exact(by["math_math", vl, False]) != b:
                raise ValueError(
                    "independent math throughput differs from one beat/cycle"
                )
        if "math_math" in names:
            candidates["math_math"].append(mm)
        if "xbar" in names:
            for kind in ("xbar", "xbar_rot"):
                candidates["xbar"].append(exact(by[kind, vl, True]) - b - mm)
        if "unpack_math" in names:
            candidates["unpack_math"].extend(
                [
                    exact(by["unpack_waw", vl, True]) - b,
                    exact(by["unpack_ring", vl, True]) - 2 * b - 1,
                ]
            )
        if "math_pack" in names:
            candidates["math_pack"].extend(
                [exact(by["pack_ring", vl, True]) - 2 * b - 1, mm]
            )
        if "cond" in names:
            for kind in ("cond_sync", "cond_setvl"):
                candidates["cond"].append(exact(by[kind, vl, True]))
            if exact(by["cond_seti", vl, True]) != candidates["cond"][-1] + 1:
                raise ValueError("SETI immediate-word cost disagrees")
        if "seti" in names:
            for dependent in (True, False):
                candidates["seti"].append(exact(by["cond_seti", vl, dependent]))
        if "mx7_issue_gap" in names:
            for kind in ("mx7a", "mx7b"):
                candidates["mx7_issue_gap"].append(
                    exact(by[kind, vl, True]) - exact(by[kind, vl, True], "V2PACK.act")
                )
        if "mx7_pack_words" in names:
            for kind in ("mx7a", "mx7b"):
                active = exact(by[kind, vl, True], "V2PACK.act")
                candidates["mx7_pack_words"].append(active)
    result = {}
    for name, measured in candidates.items():
        if len(set(measured)) != 1 or measured[0] != int(measured[0]):
            raise ValueError(f"independent calibrations disagree: {name}: {measured}")
        result[name] = {
            "value": int(measured[0]),
            "benches": BENCHES[name],
            "candidates": measured,
        }
    return result


def measure(image, machine_path):
    machine = machine_path.read_text()
    vc = json.loads(machine)["machine"]["vc"]
    defines = core_defines(image)
    key = build_key(vc["build"], defines)
    build = ROOT / "build" / f"r63-{image}"
    build_model(build, defines)
    probes = []
    for kind in KINDS:
        for vl in (16, 64, 128):
            for dependent in (True, False):
                p = measure_series(kind, vl, dependent, machine, build, defines)
                probes.append(p)
        print(image, kind, "measured", flush=True)
    record = {
        "core_build": key,
        "image": image,
        "machine": machine_path.name,
        "vc_build": vc["build"],
        "defines": defines,
        "upload_words": 256,
        "entry": "reset",
        "memwait": 0,
        "bench": "vec2_replay",
        "tool": "scripts/py/measure/vc_timing.py",
        "probes": probes,
        "parameters": values(probes),
    }
    output = ROOT / "machines" / "measured" / f"vc_timing.{key}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(record, indent=2) + "\n")
    print(output, record["parameters"], flush=True)
    return record


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--image", default="v9t6", choices=("v9t6", "v9t4", "v9"))
    args = ap.parse_args()
    images = ("v9t6", "v9t4", "v9") if args.all else (args.image,)
    for image in images:
        measure(image, ROOT / "machines" / f"multimesh_{image}.json")


if __name__ == "__main__":
    main()
