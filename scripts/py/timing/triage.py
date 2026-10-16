"""Sort failing path classes into tool-like and RTL-like signatures.

Reads one census tag (audit/placed_census.tcl output) and, optionally, the
census of the same run's placed checkpoint:

    python scripts/py/timing/triage.py build/multimesh_v9t4_routed \
        [--placed build/multimesh_v9t4_placed]

Writes <tag>_triage.tsv (every replica class with its evidence) and
<tag>_triage.txt (TNS per signature, worst classes per bucket).

A replica class merges the copies of one register-to-register class across
dies (mesh_N, the Xache / station per-die generates) and across identical
units (u_cuK, u_vecK). Its evidence:

    k / R     copies failing / copies that exist (R from the dies and the unit
              count seen in the file; "-" for a crossing between two units)
    spread    worst slack of the best failing copy minus the worst copy
    deep      levels * LEVEL_NS > requirement: depth alone misses the period
    cross     start and end in different blocks (unit, router, sysnode part)
    fanout    a net on the worst path drives >= FANOUT_HI loads (census MAXFO)
    detour    path hops >= DETOUR_X * max(start-end distance, DETOUR_MIN)
    grown     routed worst - placed worst (needs --placed)

    ip        both ends inside vendor IP (DDR4 controller, XDMA, its width converter)

Buckets: rtl = deep, or fanout, or every copy fails alike with no detour;
tool = copies disagree, or detour, or a shallow path that grew in routing;
both / neither as such; ip overrides. The flags are evidence for reading the
paths, not verdicts.
"""

import argparse
import re
from collections import defaultdict

LEVEL_NS = 0.30
CONSISTENT_SPREAD = 0.30
SCATTER_SPREAD = 0.50
FANOUT_HI = 500
DETOUR_X = 3.0
DETOUR_MIN = 20
GROWN_NS = 0.50
FLAGS = ("deep", "fanout", "cross", "consistent", "scattered", "detour", "grown", "ip")
BUCKETS = ("rtl", "tool", "both", "neither", "ip")

ROW = re.compile(
    r"^\s*(-?\d+\.\d+)\s+(\d+)\s+(\d+)\s+(-?\d+\.\d+)\s+(\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+\|\s+(\S+)\s+->\s+(\S+)\s*$"
)
CLK_ROW = re.compile(r"^(\S+)\s+(\d+\.\d+)\s+-?\d+\.\d+")
UNIT = re.compile(r"u_(cu|vec)(\d)")
# Die of a class: the per-die block or clock it names (87_synth_paths' merge set).
DIE_OF = [
    re.compile(r"mesh_?([0-3])(?=_|/|$)"),
    re.compile(
        r"(?:ddr4|rst_sys|rst_bus|rst_ddr4|dwc_ctrl|dclr_mesh)_?([0-3])(?=_|/|$)"
    ),
    re.compile(
        r"(?:g_home|g_m|g_hedge|g_chain\.g_p|g_chain\.g_b|g_stn|g_link)\[([0-3])\]"
    ),
]
MMCM = re.compile(r"^mmcm_clkout0(?:_([123]))?$")
# Physopt / synthesis copies of one register: w_kind_reg[1]_replica_7, ur_q_reg[1]_rep__16.
COPY_SUFFIX = re.compile(
    r"_reg(?:\[(?:\d+|N)\])*(?:__\d+|_rep(?:__?\d+)?|_replica(?:_\d+)?)*(?=/|$)"
)
IP = re.compile(r"/(?:ddr4_\d/inst/(?:u_ddr4_mem_intfc|u_ddr_axi)|xdma_0|dwc_xdma)/")


def census_key(clock, start, end):
    """The census' merged-class name (87_synth_paths.tcl merge, verbatim)."""
    key = f"{clock} | {start} -> {end}"
    key = re.sub(r"mesh_[0-3]", "mesh_N", key)
    key = re.sub(r"mesh[0-3]_0", "meshN_0", key)
    key = re.sub(r"mmcm_clkout([0-9]+)(_[123])?", r"mmcm_clkout\1_N", key)
    key = re.sub(
        r"(ddr4|rst_sys|rst_bus|rst_ddr4|dwc_ctrl|div2_mesh|dclr_mesh|bus_rst_inv|clk_wiz_mesh|c)_?[0-3](_|/|$)",
        r"\1_N\2",
        key,
    )
    return re.sub(r"\[[0-9]+\]", "[N]", key)


def replica_key(clock, start, end):
    """(replica class name, copy id, unit types touched)."""
    copy = set()
    for s in (clock, start, end):
        for rx in DIE_OF:
            copy.update("d" + d for d in rx.findall(s))
    m = MMCM.match(clock)
    if m:
        copy.add("d" + (m.group(1) or "0"))
    units = [t for t, _ in UNIT.findall(start)] + [t for t, _ in UNIT.findall(end)]
    copy.update(t + i for t, i in UNIT.findall(start + " " + end))
    name = census_key(clock, start, end)
    name = UNIT.sub(lambda m: f"u_{m.group(1)}K", name)
    name = " ".join(COPY_SUFFIX.sub("", w) for w in name.split(" "))
    return name, tuple(sorted(copy)), units


def block_of(name):
    parts = [p for p in name.split("/") if p != "inst"]
    parts = parts[1:] if parts and parts[0].endswith("_i") else parts
    if parts and parts[0].startswith("mesh_"):
        return "/".join(
            parts[:3] if len(parts) > 1 and parts[1] == "u_mag" else parts[:2]
        )
    return "/".join(parts[:3])


def read_classes(path):
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            m = ROW.match(line)
            if not m:
                continue
            worst, _n, fail, tns, lvl, _dp, logic, net, clock, start, end = m.groups()
            rows.append(
                {
                    "worst": float(worst),
                    "fail": int(fail),
                    "tns": float(tns),
                    "lvl": int(lvl),
                    "logic": float(logic),
                    "net": float(net),
                    "clock": clock,
                    "start": start,
                    "end": end,
                }
            )
    return rows


def read_req(path):
    req = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            m = CLK_ROW.match(line)
            if m:
                req[m.group(1)] = float(m.group(2))
    return req


def read_place(path):
    place = {}
    try:
        with open(path, encoding="utf-8") as fh:
            head = fh.readline().rstrip("\n").split("\t")
            for line in fh:
                f = dict(zip(head, line.rstrip("\n").split("\t")))
                place[f["CLASS"]] = f
    except FileNotFoundError:
        pass
    return place


def as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tag")
    ap.add_argument("--placed")
    a = ap.parse_args()

    rows = read_classes(a.tag + "_classes.txt")
    req = read_req(a.tag + "_perclock.txt")
    place = read_place(a.tag + "_classes_place.tsv")
    placed_worst = {}
    if a.placed:
        for r in read_classes(a.placed + "_classes.txt"):
            k = census_key(r["clock"], r["start"], r["end"])
            placed_worst[k] = min(placed_worst.get(k, 0.0), r["worst"])

    n_unit = defaultdict(int)
    for r in rows:
        for t, i in UNIT.findall(r["start"] + " " + r["end"]):
            n_unit[t] = max(n_unit[t], int(i) + 1)
    groups = defaultdict(dict)
    sums = defaultdict(lambda: [0.0, 0])
    meta = {}
    dies = set()
    for r in rows:
        name, copy, units = replica_key(r["clock"], r["start"], r["end"])
        dies.update(c for c in copy if c.startswith("d"))
        prev = groups[name].get(copy)
        if prev is None or r["worst"] < prev["worst"]:
            groups[name][copy] = r
        sums[name][0] += r["tns"]
        sums[name][1] += r["fail"]
        if name not in meta:
            meta[name] = units
    n_die = len(dies) or 1

    out = []
    for name, copies in groups.items():
        worst_r = min(copies.values(), key=lambda r: r["worst"])
        worsts = [c["worst"] for c in copies.values()]
        units = meta[name]
        if (
            len(units) == 2
            and units[0] == units[1]
            and UNIT.findall(worst_r["start"]) != UNIT.findall(worst_r["end"])
        ):
            r_all = None
        else:
            has_die = any(c.startswith("d") for c in next(iter(copies)))
            r_all = (n_die if has_die else 1) * (n_unit[units[0]] if units else 1)
        rq = req.get(worst_r["clock"])
        ck = census_key(worst_r["clock"], worst_r["start"], worst_r["end"])
        pl = place.get(ck, {})
        dist, hops, fo = (
            as_int(pl.get("DIST")),
            as_int(pl.get("HOPS")),
            as_int(pl.get("MAXFO")),
        )
        spread = max(worsts) - min(worsts)
        k = len(copies)
        f = {
            "deep": rq is not None and worst_r["lvl"] * LEVEL_NS > rq,
            "cross": block_of(worst_r["start"]) != block_of(worst_r["end"]),
            "fanout": fo is not None and fo >= FANOUT_HI,
            "detour": hops is not None
            and dist is not None
            and hops >= DETOUR_X * max(dist, DETOUR_MIN),
            "consistent": r_all is not None
            and k == r_all
            and spread <= CONSISTENT_SPREAD,
            "scattered": (r_all is not None and r_all > 1 and k <= r_all / 2)
            or spread >= SCATTER_SPREAD,
        }
        grown = None
        if a.placed and ck in placed_worst:
            grown = worst_r["worst"] - placed_worst[ck]
        f["grown"] = grown is not None and grown <= -GROWN_NS and worst_r["lvl"] <= 3
        f["ip"] = bool(IP.search(worst_r["start"]) and IP.search(worst_r["end"]))
        rtl = f["deep"] or f["fanout"] or (f["consistent"] and not f["detour"])
        tool = f["scattered"] or f["detour"] or f["grown"]
        bucket = {
            (True, True): "both",
            (True, False): "rtl",
            (False, True): "tool",
        }.get((rtl, tool), "neither")
        if f["ip"]:
            bucket = "ip"
        out.append(
            {
                "name": name,
                "bucket": bucket,
                "flags": ",".join(x for x in FLAGS if f[x]),
                "worst": worst_r["worst"],
                "tns": sums[name][0],
                "fail": sums[name][1],
                "k": k,
                "R": "-" if r_all is None else r_all,
                "spread": spread,
                "lvl": worst_r["lvl"],
                "logic": worst_r["logic"],
                "net": worst_r["net"],
                "req": "-" if rq is None else rq,
                "dist": "-" if dist is None else dist,
                "hops": "-" if hops is None else hops,
                "maxfo": "-" if fo is None else fo,
                "grown": "-" if grown is None else f"{grown:.3f}",
            }
        )
    out.sort(key=lambda o: o["tns"])

    cols = [
        "bucket",
        "flags",
        "worst",
        "tns",
        "fail",
        "k",
        "R",
        "spread",
        "lvl",
        "logic",
        "net",
        "req",
        "dist",
        "hops",
        "maxfo",
        "grown",
        "name",
    ]
    with open(a.tag + "_triage.tsv", "w", encoding="utf-8") as fh:
        fh.write("\t".join(cols) + "\n")
        fh.writelines(
            "\t".join(
                f"{o[c]:.3f}" if isinstance(o[c], float) else str(o[c]) for c in cols
            )
            + "\n"
            for o in out
        )

    total = sum(o["tns"] for o in out)
    with open(a.tag + "_triage.txt", "w", encoding="utf-8") as fh:
        fh.write(
            f"replica classes: {len(out)}; TNS {total:.0f}; dies {n_die}; units {dict(n_unit)}\n"
        )
        fh.write(
            f"thresholds: level {LEVEL_NS} ns, spread {CONSISTENT_SPREAD}/{SCATTER_SPREAD} ns, fanout {FANOUT_HI}, detour {DETOUR_X}x (min {DETOUR_MIN}), grown {GROWN_NS} ns\n"
        )
        fh.write(
            f"path walk present for {sum(1 for o in out if o['hops'] != '-')} classes; placed census {'joined' if a.placed else 'absent'}\n\n"
        )
        fh.write(f"{'bucket':8s} {'classes':>8s} {'TNS':>12s} {'share':>6s}\n")
        by = defaultdict(lambda: [0, 0.0])
        for o in out:
            by[o["bucket"]][0] += 1
            by[o["bucket"]][1] += o["tns"]
        for b in BUCKETS:
            n, t = by[b]
            fh.write(
                f"{b:8s} {n:8d} {t:12.0f} {100 * t / total if total else 0:5.1f}%\n"
            )
        fh.write(f"\n{'flag':10s} {'classes':>8s} {'TNS':>12s}\n")
        for x in FLAGS:
            sel = [o for o in out if x in o["flags"].split(",")]
            fh.write(f"{x:10s} {len(sel):8d} {sum(o['tns'] for o in sel):12.0f}\n")
        for b in BUCKETS:
            fh.write(f"\n== {b}: worst 30 by TNS ==\n")
            for o in [o for o in out if o["bucket"] == b][:30]:
                fh.write(
                    f"{o['tns']:10.1f} {o['worst']:7.3f} k={o['k']}/{o['R']} sp={o['spread']:.2f} "
                    f"lvl={o['lvl']} net={o['net']:.2f} d={o['dist']} h={o['hops']} fo={o['maxfo']} "
                    f"g={o['grown']} [{o['flags']}]  {o['name']}\n"
                )
    print(f"{len(out)} replica classes -> {a.tag}_triage.tsv / _triage.txt")


if __name__ == "__main__":
    main()
