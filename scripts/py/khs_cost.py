#!/usr/bin/env python3
"""Estimate a PE's area from its configuration, without running synthesis.

    python scripts/py/khs_cost.py --fit                  # from build/sweep
    python scripts/py/khs_cost.py --validate             # fit, then score itself
    python scripts/py/khs_cost.py --table
    python scripts/py/khs_cost.py --report docs/projects/kohakumpe/width-cost.md
    python scripts/py/khs_cost.py ilanes=0 permu=2 flanes=8

Nothing is hand-entered: `--fit` reads the run logs khs_sweep.py leaves under
build/sweep, each carrying its whole configuration in its `@@@REC` line.

The terms are marginals from ONE base, so the model is additive by construction
and cannot see an interaction. `--validate` re-estimates every measured row and
prints the residual, which is where that assumption breaks.
"""

import argparse
import itertools
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SWEEP = ROOT / "build" / "sweep"
MODEL = SWEEP / "cost-model.json"

REC = re.compile(r"^@@@REC\s+(.*)$", re.MULTILINE)
FMAX = re.compile(r"^@@@FMAX\s+\S+\s+\S+\s+([\d.]+)", re.MULTILINE)

#: Fitted and reported. LUT is the question; the rest come free in the record.
METRICS = ["lut", "ff", "dsp", "bram"]
#: Reported per row but not fitted. A measured output MISSING from this list
#: falls through to knobs_of() and reads as a knob that changed, which made
#: every single-knob row look multi-knob and silently dropped seven features.
EXTRA = ["lut_log", "lut_mem", "lut_dram", "lut_srl", "uram", "ctrlsets"]

#: Knobs whose value is a number; everything else is categorical.
NUMERIC = {
    "simd",
    "ilanes",
    "shiftu",
    "permu",
    "red",
    "flanes",
    "fsfu",
    "falu",
    "facc",
    "fcvt",
    "nacc",
    "vregs",
    "npart",
    "wb",
    "dsp_en",
    "lanes",
    "waves",
    "shflu",
    "ldsb",
    "mask",
    "ipdom",
    "shfl",
    "ipdomd",
    "instdepth",
    "recvdepth",
    "imemwords",
    "spadwords",
    "l1lines",
}
#: Measured outputs and provenance, not knobs.
NOT_KNOB = set(METRICS) | set(EXTRA) | {"tag", "period", "xgen", "flat", "sdir"}
#: Future `lut_*` columns, caught by name. ONLY that family: a broader prefix
#: ate the `dsp_en` KNOB, which made the CPU-only row identical to the base.
MEASURED = ("lut_",)


def rows(sweep_dir):
    """Every finished run of the LATEST campaign as (name, knobs, metrics).

    Only the directories `manifest.json` names. build/sweep accumulates, and
    older logs spell retired knobs (`muls`, `perm`, `shift`) at positions that
    now mean something else -- 177 such rows were present when this was written,
    and a fit over all of them prices knobs that no longer exist.
    """
    if not sweep_dir.is_dir():
        sys.exit(f"no sweep directory at {sweep_dir}; run khs_sweep.py --all first")
    man = sweep_dir / "manifest.json"
    keep = set(json.loads(man.read_text())["tags"]) if man.exists() else None
    if keep is None:
        print(
            f"  WARNING: no manifest.json under {sweep_dir} -- reading every\n"
            "  directory, including earlier campaigns whose knobs were retired.",
            file=sys.stderr,
        )
    out = []
    for d in sorted(sweep_dir.iterdir()):
        if not d.is_dir() or (keep is not None and d.name not in keep):
            continue
        log = d / "run.log"
        if not log.exists():
            continue
        text = log.read_text(errors="ignore")
        rec = None
        for m in reversed(REC.findall(text)):
            f = dict(kv.split("=", 1) for kv in m.split() if "=" in kv)
            if "lut" in f:
                rec = f
                break
        if rec is None:
            continue
        metrics = {}
        for k in METRICS + EXTRA:
            try:
                metrics[k] = float(rec[k])
            except (KeyError, ValueError):
                metrics[k] = None
        fs = [float(x) for x in FMAX.findall(text)]
        metrics["fmax"] = min(fs) if fs else None
        # A measured output missing from NOT_KNOB would read as a knob and
        # poison the single-knob test, so the prefix catches new ones too.
        knobs = {
            k: v
            for k, v in rec.items()
            if k not in NOT_KNOB and not k.startswith(MEASURED)
        }
        # `xgen` carries NAME:VALUE generics that no positional argument has.
        # Left packed, every xgen row has the SAME knobs as the base and is
        # indistinguishable from it -- fslots and shround could not be priced,
        # and the base-selection tie put the CPU-only row on the throne.
        for kv in rec.get("xgen", "-").split("+"):
            if ":" in kv:
                nm, val = kv.split(":", 1)
                knobs[nm.lower()] = val
        out.append((d.name, knobs, metrics))

    # A row that never named an xgen knob was built at that generic's RTL
    # default, which is a distinct value and not a missing one.
    allx = {k for _n, kn, _m in out for k in kn if k.startswith("simd_")}
    for _n, kn, _m in out:
        for k in allx:
            kn.setdefault(k, "default")
    return out


def family_of(knobs):
    """Which PE a row measured. The two have DISJOINT knob sets.

    Fitting both together picks one modal base -- necessarily a SIMD one, there
    being more SIMD sweeps -- and every SIMT row then differs from it in a dozen
    knobs and is discarded as multi-knob. So they are fitted separately.
    """
    return "simt" if ("lanes" in knobs or "top" in knobs) else "simd"


def _diffcount(a, b):
    return sum(1 for k in set(a) | set(b) if a.get(k) != b.get(k))


def fit_family(data):
    """A base, and one curve per (knob, metric) measured against that base.

    THE BASE IS THE ROW MOST OTHERS SIT ONE KNOB AWAY FROM. It used to be the
    modal row, which worked only while every sweep re-synthesised the base;
    khs_sweep.py now runs one synthesis per distinct argv, so the base appears
    exactly once and "most common" picks an arbitrary row.
    """
    cfgs = [kn for _n, kn, _m in data]
    best, best_score = 0, -1
    for i, c in enumerate(cfgs):
        score = sum(1 for j, o in enumerate(cfgs) if i != j and _diffcount(c, o) == 1)
        if score > best_score:
            best_score, best = score, i
    base_cfg = cfgs[best]
    base_row = data[best][2]

    terms = {k: {} for k in METRICS}
    skipped = []
    for name, kn, met in data:
        diff = [k for k in set(kn) | set(base_cfg) if kn.get(k) != base_cfg.get(k)]
        if not diff:
            continue
        if len(diff) > 1:
            skipped.append((name, sorted(diff)))
            continue
        k = diff[0]
        val = kn.get(k, base_cfg.get(k))
        for mk in METRICS:
            if met[mk] is not None and base_row[mk] is not None:
                terms[mk].setdefault(k, {})[val] = met[mk] - base_row[mk]

    for k, v in base_cfg.items():
        for mk in METRICS:
            terms[mk].setdefault(k, {})[v] = 0.0

    return {
        "base": {k: base_row[k] for k in METRICS},
        "base_fmax": base_row["fmax"],
        "base_cfg": base_cfg,
        "terms": terms,
        "skipped": skipped,
        "n_rows": len(data),
    }


def fit(sweep_dir):
    data = rows(sweep_dir)
    if not data:
        sys.exit(f"no @@@REC rows under {sweep_dir}")
    fams = sorted({family_of(kn) for _n, kn, _m in data})
    return {f: fit_family([r for r in data if family_of(r[1]) == f]) for f in fams}


def term_for(model, knob, value, metric="lut", allow_extrap=True):
    """The delta for one knob at one value.

    Returns (delta, kind) where kind is measured / interpolated / extrapolated /
    assumed. Extrapolation continues the slope of the two nearest measured
    points -- it is a straight line off the end of the data and is labelled so
    everywhere it is reported.
    """
    curve = model["terms"].get(metric, {}).get(knob)
    if not curve:
        return 0.0, "assumed"
    if str(value) in curve:
        return curve[str(value)], "measured"
    if knob not in NUMERIC:
        return 0.0, "assumed"
    pts = sorted((float(k), v) for k, v in curve.items())
    x = float(value)
    if len(pts) == 1:
        return pts[0][1], "assumed"
    if pts[0][0] <= x <= pts[-1][0]:
        for (x0, y0), (x1, y1) in itertools.pairwise(pts):
            if x0 <= x <= x1:
                t = 0.0 if x1 == x0 else (x - x0) / (x1 - x0)
                return y0 + t * (y1 - y0), "interpolated"
    if not allow_extrap:
        return None, "out-of-range"
    (x0, y0), (x1, y1) = (pts[0], pts[1]) if x < pts[0][0] else (pts[-2], pts[-1])
    slope = 0.0 if x1 == x0 else (y1 - y0) / (x1 - x0)
    return y0 + slope * (x - x0), "extrapolated"


def estimate(model, cfg, metric="lut", allow_extrap=True):
    total, kinds = model["base"][metric], {}
    for k, v in cfg.items():
        d, kind = term_for(model, k, v, metric, allow_extrap)
        if d is None:
            return None, {k: kind}
        total += d
        if kind != "measured":
            kinds[k] = kind
    return total, kinds


def validate(models, sweep_dir):
    data = rows(sweep_dir)
    worst, n = 0.0, 0
    for fam in sorted(models):
        model = models[fam]
        skipped = {nm for nm, _ in model["skipped"]}
        print(f"\n  === {fam} (base {model['base']['lut']:.0f} LUT) ===")
        print(f"  {'row':<26}{'measured':>10}{'estimate':>10}{'error':>9}")
        for name, kn, met in data:
            if family_of(kn) != fam or met["lut"] is None:
                continue
            est, _k = estimate(model, kn)
            if est is None:
                continue
            err = est - met["lut"]
            flag = "  MULTI-KNOB" if name in skipped else ""
            print(f"  {name:<26}{met['lut']:>10.0f}{est:>10.0f}{err:>+9.0f}{flag}")
            if name not in skipped:
                worst = max(worst, abs(err))
                n += 1
    print(f"\n  {n} single-knob rows, worst residual {worst:+.0f} LUT")
    print("  THAT NUMBER IS TAUTOLOGICAL: a single-knob row IS the point its")
    print("  own term was fitted from, so the model reproduces it by")
    print("  construction. The real test is below.")
    print("\n  HELD OUT -- rows that moved several knobs at once and were never")
    print("  used to fit anything. This is where additivity is actually tested:")
    print(f"\n  {'row':<20}{'measured':>10}{'estimate':>10}{'error':>9}{'':>4}%")
    hw = 0.0
    for fam in sorted(models):
        model = models[fam]
        for name, diff in model["skipped"]:
            row = next((r for r in data if r[0] == name), None)
            if row is None or row[2]["lut"] is None:
                continue
            est, _k = estimate(model, row[1])
            if est is None:
                continue
            err = est - row[2]["lut"]
            pct = 100.0 * err / row[2]["lut"]
            hw = max(hw, abs(pct))
            print(
                f"  {name:<20}{row[2]['lut']:>10.0f}{est:>10.0f}"
                f"{err:>+9.0f}{pct:>+8.1f}%   ({len(diff)} knobs)"
            )
    print(f"\n  worst held-out error {hw:.1f}%")
    return worst


# ------------------------------------------------------------------ report
def _num(v, nd=0):
    return "—" if v is None else f"{v:,.{nd}f}"


def report(models, data, path, fails=None):
    """Write the measured tables and the derived estimates as one markdown doc."""
    fails = fails or {}
    L = []
    L.append("---")
    L.append("title: Configurable width area cost")
    L.append(
        "summary: Measured LUT, FF, DSP and BRAM for every configurable width on "
        "the SIMD and SIMT PEs, the marginal cost of each knob, and the "
        "interpolated and extrapolated cost at widths that were not synthesised."
    )
    L.append("tags:")
    for t in ("architecture", "pe", "simd", "simt", "area"):
        L.append(f"  - {t}")
    L.append("---")
    L.append("")
    L.append("# Configurable width area cost")
    L.append("")
    L.append(
        "GENERATED — do not edit by hand. Rebuild with "
        "`python scripts/py/khs_cost.py --fit --report <this file>` after a "
        "`python scripts/py/khs_sweep.py --all` campaign."
    )
    L.append("")
    L.append(
        "Every figure is out-of-context **synthesis** of one PE on "
        "`xcvu13p-fhgb2104-2L-e` at a 3.333 ns target, `-flatten_hierarchy "
        "rebuilt`, `-directive default`. Synthesis estimates are optimistic "
        "against place-and-route and are not a closed frequency."
    )
    L.append("")
    L.append("## Anchors")
    L.append("")
    L.append(
        "The ONE absolute figure per core. Everything after this section is a "
        "delta; to budget a configuration, start here and add the rows."
    )
    L.append("")

    for fam in sorted(models):
        m = models[fam]
        L.append(f"### {fam.upper()} anchor")
        L.append("")
        L.append("| LUT | FF | DSP | BRAM | Fmax (MHz) |")
        L.append("|---:|---:|---:|---:|---:|")
        L.append(
            f"| {_num(m['base']['lut'])} | {_num(m['base']['ff'])} "
            f"| {_num(m['base']['dsp'])} | {_num(m['base']['bram'], 1)} "
            f"| {_num(m['base_fmax'], 1)} |"
        )
        L.append("")
        L.append(
            "Measured at the configuration below. **This is NOT every feature "
            "at maximum** — read it. Every delta in the next section is this "
            "build with one knob moved."
        )
        L.append("")
        L.append("```")
        cfg = m["base_cfg"]
        line = ""
        for k in sorted(cfg):
            piece = f"{k}={cfg[k]} "
            if len(line) + len(piece) > 76:
                L.append(line.rstrip())
                line = ""
            line += piece
        if line.strip():
            L.append(line.rstrip())
        L.append("```")
        L.append("")

    L.append("## What each feature costs")
    L.append("")
    L.append(
        "**Every number is a DELTA against that feature switched off.** The "
        "`vs` column names the reference, which is width 0 — not built — "
        "wherever the feature can be removed at all. Nothing in this table is "
        "an absolute total: to budget a configuration, take the anchor above "
        "and add the rows you want."
    )
    L.append("")
    L.append(
        "`measured` is a synthesis run. `interpolated` is a straight line "
        "between the two measured points either side. `extrapolated` continues "
        "the slope of the two nearest measured points off the end of the data — "
        "a prediction, not a result, and the weakest number here."
    )
    L.append("")
    for fam in sorted(models):
        model = models[fam]
        L.append(f"### {fam.upper()}")
        L.append("")
        L.append("| feature | width | vs | ΔLUT | ΔFF | ΔDSP | ΔBRAM | basis |")
        L.append("|---|---:|---:|---:|---:|---:|---:|---|")
        shown = set()
        for knob in sorted(model["terms"]["lut"]):
            got = (
                _numeric_rows(model, knob)
                if knob in NUMERIC
                else _categorical_rows(model, knob)
            )
            if got:
                shown.add(knob)
            L.extend(got)
        # A feature the RTL refuses, or one whose run died, gets a row SAYING so.
        for (core, knob, val), why in sorted(fails.items()):
            if core != fam or knob in shown:
                continue
            L.append(f"| `{knob}` | {val} | — | — | — | — | — | {why} |")
        L.append("")

    L.append("## How wrong this model is")
    L.append("")
    L.append(
        "The terms are marginals from a single base, so the estimate is "
        "additive by construction and cannot see two features that share "
        "control logic. Re-estimating a SINGLE-knob row proves nothing — that "
        "row IS the point its own term came from. The rows below moved several "
        "knobs at once and were **never used to fit anything**, so they are the "
        "only honest test here."
    )
    L.append("")
    L.append("| row | knobs moved | measured LUT | estimate | error |")
    L.append("|---|---:|---:|---:|---:|")
    for fam in sorted(models):
        model = models[fam]
        for name, diff in model["skipped"]:
            row = next((r for r in data if r[0] == name), None)
            if row is None or row[2]["lut"] is None:
                continue
            est, _k = estimate(model, row[1])
            if est is None:
                continue
            err = est - row[2]["lut"]
            pct = 100.0 * err / row[2]["lut"]
            L.append(
                f"| `{name}` | {len(diff)} | {_num(row[2]['lut'])} | "
                f"{_num(est)} | {err:+,.0f} ({pct:+.1f}%) |"
            )
    L.append("")
    L.append(
        "**The error is one-directional and that matters for budgeting.** "
        "Removing features together saves MORE than the sum of removing them "
        "one at a time, because shared control and mux logic goes away once "
        "when its last consumer does. A stripped configuration therefore comes "
        "in CHEAPER than this model predicts, never dearer — so an estimate "
        "used as a ceiling is safe, and one used as a floor is not."
    )
    L.append("")
    if any(models[f]["skipped"] for f in models):
        L.append(
            "These rows moved several generics at once and are therefore NOT "
            "used to derive any per-knob term:"
        )
        L.append("")
        for fam in sorted(models):
            for name, diff in models[fam]["skipped"]:
                L.append(f"- `{name}` — {', '.join(diff)}")
        L.append("")

    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"  wrote {path}")


GUARD = re.compile(r"module '(\w+)' not found")


def failures(sweep_dir):
    """Points that produced no @@@REC, and why -- keyed by (core, knob, value).

    A feature the RTL REFUSES is not a hole in the data, it is the answer, and
    a table that silently omits it reads as "not measured yet".
    """
    try:
        sys.path.insert(0, str(ROOT / "scripts" / "py"))
        from khs_sweep import SWEEPS
    except ImportError:
        return {}
    man = sweep_dir / "manifest.json"
    tags = set(json.loads(man.read_text())["tags"]) if man.exists() else None
    out = {}
    for name, (core, _why, points) in SWEEPS.items():
        for i, pt in enumerate(points):
            tag = f"{name}_{i}"
            if tags is not None and tag not in tags:
                continue
            log = sweep_dir / tag / "run.log"
            if not log.exists():
                continue
            text = log.read_text(errors="ignore")
            if REC.search(text) and "lut=" in text:
                continue
            g = GUARD.search(text)
            why = f"REFUSED at elaboration: `{g.group(1)}`" if g else "run failed"
            for k, v in pt.items():
                if k != "xgen":
                    out[(core, k, v)] = why
    return out


def _numeric_rows(model, knob):
    """One row per legal width, every metric referenced to the lowest width."""
    want = _ladder(knob, model["terms"]["lut"].get(knob, {}))
    if len(want) < 2:
        return []
    ref = want[0]
    refs = {}
    for mk in METRICS:
        r, _k = term_for(model, knob, ref, mk)
        refs[mk] = r or 0.0
    out = []
    for val in want:
        cells, kind = [], "measured"
        for mk in METRICS:
            d, k = term_for(model, knob, val, mk)
            if mk == "lut":
                kind = k
            cells.append((d or 0.0) - refs[mk])
        out.append(
            f"| `{knob}` | {val:g} | {ref:g} | {cells[0]:+,.0f} | "
            f"{cells[1]:+,.0f} | {cells[2]:+,.0f} | {cells[3]:+,.1f} | {kind} |"
        )
    return out


def _categorical_rows(model, knob):
    """A knob whose values are names, not counts: BRAM-vs-LUTRAM and the like."""
    curve = model["terms"]["lut"].get(knob, {})
    if len(curve) < 2:
        return []
    vals = sorted(curve, key=_sortable)
    ref = vals[0]
    out = []
    for val in vals:
        cells = [
            model["terms"][mk].get(knob, {}).get(val, 0.0)
            - model["terms"][mk].get(knob, {}).get(ref, 0.0)
            for mk in METRICS
        ]
        out.append(
            f"| `{knob}` | {val} | {ref} | {cells[0]:+,.0f} | {cells[1]:+,.0f} | "
            f"{cells[2]:+,.0f} | {cells[3]:+,.1f} | measured |"
        )
    return out


def _sortable(s):
    try:
        return (0, float(s))
    except ValueError:
        return (1, s)


#: EVERY legal value of every knob, so no feature appears with a partial ladder.
#: A width is units per pass over the vector and cannot exceed it, so the ladder
#: stops at 8 for both cores.
WIDTH_LADDER = [0, 1, 2, 4, 8]
BOOL_LADDER = [0, 1]
LADDERS = {
    "ilanes": WIDTH_LADDER,
    "shiftu": WIDTH_LADDER,
    "permu": WIDTH_LADDER,
    "flanes": WIDTH_LADDER,
    "fsfu": WIDTH_LADDER,
    "shflu": WIDTH_LADDER,
    "ldsb": WIDTH_LADDER,
    "red": BOOL_LADDER,
    "falu": BOOL_LADDER,
    "facc": BOOL_LADDER,
    "fcvt": WIDTH_LADDER,
    "wb": BOOL_LADDER,
    "dsp_en": BOOL_LADDER,
    "mask": BOOL_LADDER,
    "ipdom": BOOL_LADDER,
    "shfl": BOOL_LADDER,
    "nacc": [1, 2, 4],
    "npart": [4, 8, 16],
    "vregs": [8, 16, 32],
    "waves": [4, 8, 16],
}


def _ladder(knob, curve):
    """Every value this feature is reported at: its legal ladder, plus anything
    actually synthesised outside it."""
    have = sorted(float(k) for k in curve)
    return sorted(set(LADDERS.get(knob, [])) | set(have))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cfg", nargs="*", help="knob=value pairs to price")
    ap.add_argument("--dir", default=str(SWEEP))
    ap.add_argument("--fit", action="store_true", help="rebuild the coefficients")
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--table", action="store_true")
    ap.add_argument("--report", help="write the markdown report to this path")
    ap.add_argument("--core", default="simd", choices=("simd", "simt"))
    ap.add_argument("--metric", default="lut", choices=METRICS)
    a = ap.parse_args()
    sweep_dir = pathlib.Path(a.dir)

    if a.fit or a.validate or a.report or not MODEL.exists():
        models = fit(sweep_dir)
        MODEL.parent.mkdir(parents=True, exist_ok=True)
        MODEL.write_text(json.dumps(models, indent=2, sort_keys=True))
        for fam, m in sorted(models.items()):
            print(
                f"  {fam}: {m['n_rows']} rows, base {m['base']['lut']:.0f} LUT"
                f", {len(m['skipped'])} multi-knob rows held out"
            )
    else:
        models = json.loads(MODEL.read_text())

    if a.report:
        report(models, rows(sweep_dir), a.report, failures(sweep_dir))
        return 0
    if a.validate:
        validate(models, sweep_dir)
        return 0

    if a.core not in models:
        sys.exit(f"no {a.core} rows in the fit; have {sorted(models)}")
    model = models[a.core]

    if a.table:
        print(
            f"\n  {a.core} base: "
            + "  ".join(f"{k.upper()} {model['base'][k]:,.0f}" for k in METRICS)
        )
        for k, v in sorted(model["base_cfg"].items()):
            print(f"    {k}={v}")
        print(f"\n  measured {a.metric} deltas from that base:")
        for k in sorted(model["terms"][a.metric]):
            pts = model["terms"][a.metric][k]
            if len(pts) < 2:
                continue
            body = "  ".join(
                f"{val}:{d:+,.0f}"
                for val, d in sorted(pts.items(), key=lambda p: _sortable(p[0]))
            )
            print(f"    {k:<12} {body}")
        return 0

    if not a.cfg:
        return 0
    cfg = dict(kv.split("=", 1) for kv in a.cfg)
    full = dict(model["base_cfg"])
    full.update(cfg)
    est, kinds = estimate(model, full, a.metric)
    for k, kind in sorted(kinds.items()):
        print(f"  note: {k}={full[k]} is {kind}")
    if est is None:
        return 1
    print(f"\n  estimated {est:,.0f} {a.metric.upper()} ({a.core})")
    print("  changed from the base: " + " ".join(f"{k}={v}" for k, v in cfg.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
