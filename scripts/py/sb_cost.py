#!/usr/bin/env python3
"""Station-bus resource calculator.

Answers: "given N stations, and each station's manager + subordinate ports
(width + protocol), and the link config -- how many LUT/FF/BRAM?"  It sums
per-component costs measured ONCE in isolation (OOC synth), so any topology's
cost is an itemized bill instead of one opaque synth number.

    python scripts/py/sb_cost.py                    # the ship (sb_bd_line4)
    python scripts/py/sb_cost.py --list             # every named topology

Component costs come from scripts/tcl/ooc_component.tcl (VU13P, FW=256, ost=4):
    vivado -mode batch -source scripts/tcl/ooc_component.tcl \\
        -tclargs sb_nsu 32 256 nsu-32f 4 4
and the hub+station overhead is (line-synth station total) - sum(its NSUs).
Numbers marked None are not yet measured; a topology using one is flagged.
"""

import argparse
import sys

# ============================ measured costs =============================
# (lut, ff, bram) per instance. FW=256, ost=4 (WOST=ROST=4 / TAGW=4), VU13P.
# A subordinate port is an NSU; a manager port is an NMU. "lite" = the _lite
# variant (AXI4-Lite, single-beat), "full" = the full-AXI4 sb_nmu/sb_nsu.
# Every subordinate port is an sb_nsu (full AXI4); a *lite* port is an sb_nsu
# PLUS an sb_axi2lite behind it (that is how the ship builds M02/M03 etc.).
NSU = {
    ("full", 256): (876, 1259, 9.0),  # SDW==FW: no width conversion
    ("full", 32): (813, 1087, 2.5),
}
AXI2LITE = {32: (119, 212, 0.0)}  # added behind a lite NSU
# A manager port is an sb_nmu (full) or sb_nmu (with Lite tie-offs upstream).
NMU = {
    ("full", 64): (1837, 1595, 6.5),  # jtag; MW<FW packs (heaviest logic)
    ("full", 512): (1270, 1273, 13.5),  # xdma; MW>FW splits (wide FIFOs->BRAM)
    ("lite", 32): (1596, 1422, 6.0),
}
# Per station: hub crossbar + skids + reset sync. DERIVED from the ground-truth
# sb_line4 synth (27,680 LUT): (total - sum ports - sum links) / 4 stations.
HUB_OVER = (2017, 2340, 0.0)  # (lut, ff, bram) per station
# Per link, one direction (CDC = sb_link_cdc, the ship's LINK_CDC=1).
LINK = {
    ("cdc", "req"): (299, 2355, 0.0),  # REQ flit 356 b at FW=256
    ("cdc", "rsp"): (251, 1839, 0.0),  # RSP flit 270 b
}


# ============================== topologies ==============================
def _stn(subs, mgrs=None):
    return {"subs": subs, "mgrs": mgrs or []}


_PORTS4 = [("full", 256), ("full", 32), ("lite", 32), ("lite", 32)]
_MGRS3 = [("full", 64), ("full", 512), ("lite", 32)]

TOPOS = {
    # The shipping bus: 4 stations on a line, 3 managers on station 1,
    # 4 subordinate ports/station, LINK_FULL=0 (6 links), LINK_CDC=1.
    "ship": {
        "desc": "sb_bd_line4 (FW=256, LINK_FULL=0, LINK_CDC=1)",
        "stations": [
            _stn(_PORTS4),
            _stn(_PORTS4, _MGRS3),
            _stn(_PORTS4),
            _stn(_PORTS4),
        ],
        "links": [("cdc", "req")] * 3 + [("cdc", "rsp")] * 3,
    },
    # Same line, full-link mode (all 4 directions per boundary => 12 links).
    "ship-full": {
        "desc": "sb_line4 (FW=256, LINK_FULL=1)",
        "stations": [
            _stn(_PORTS4),
            _stn(_PORTS4, _MGRS3),
            _stn(_PORTS4),
            _stn(_PORTS4),
        ],
        "links": [("cdc", "req")] * 6 + [("cdc", "rsp")] * 6,
    },
}


def itemize(topo):
    """-> ({label: [count, (lut,ff,bram)]}, [missing labels])."""
    items, missing = {}, []

    def bump(label, cost):
        if cost is None:
            missing.append(label)
            return
        items.setdefault(label, [0, cost])[0] += 1

    for stn in topo["stations"]:
        for proto, w in stn["subs"]:
            bump(f"NSU full {w}b", NSU.get(("full", w)))  # every sub is an NSU
            if proto == "lite":
                bump(f"axi2lite {w}b", AXI2LITE.get(w))  # + the Lite shim
        for proto, w in stn["mgrs"]:
            bump(f"NMU {proto} {w}b", NMU.get((proto, w)))
        bump("hub+overhead", HUB_OVER)
    for lk in topo["links"]:
        bump(f"link {lk[0]} {lk[1]}", LINK.get(lk))
    return items, missing


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("topo", nargs="?", default="ship")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    if args.list:
        for k, v in TOPOS.items():
            print(f"{k:12} {v['desc']}")
        return
    t = TOPOS.get(args.topo)
    if not t:
        sys.exit(f"unknown topo {args.topo}; --list to see them")
    items, missing = itemize(t)
    print(f"# {args.topo}: {t['desc']}")
    print(f"  {'item':22} {'x':>3} {'LUT':>7} {'FF':>7} {'BRAM':>6}")
    tl = tf = tb = 0.0
    for label in sorted(items):
        n, (l, f, b) = items[label]
        tl += l * n
        tf += f * n
        tb += b * n
        print(f"  {label:22} {n:>3} {l * n:>7} {f * n:>7} {b * n:>6}")
    print(f"  {'TOTAL':22} {'':>3} {tl:>7.0f} {tf:>7.0f} {tb:>6.1f}")
    if missing:
        print(f"  INCOMPLETE -- not measured: {sorted(set(missing))}")


if __name__ == "__main__":
    main()
