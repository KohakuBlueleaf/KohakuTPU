#!/usr/bin/env python3
"""Run single-mesh placement probes and collect what decides the shape.

    python scripts/py/mesh_probe.py wave1
    python scripts/py/mesh_probe.py wave2 --jobs 2

Each probe pins one mesh to one SLR with the real station-bus line and places
it. The comparison is graded, not pass/fail: congestion region, SLL per
boundary, WNS and route completion all matter, because a probe can "pass" while
spilling routing into empty neighbour dies that the full design would not have.
"""

import argparse
import atexit
import os
import pathlib
import random
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from jobguard import JobGuard, avail_gb, run_guarded

ROOT = pathlib.Path(__file__).resolve().parents[2]
VIVADO = pathlib.Path(r"D:\Xilinx\Vivado\2024.2\bin\vivado.bat")
TCL = ROOT / "scripts" / "tcl" / "mesh_probe_bd.tcl"
TCL_IMPL = ROOT / "scripts" / "tcl" / "mesh_probe_impl.tcl"
PROBE_DIR = pathlib.Path(r"C:\Users\apoll\Desktop\vivado\probe")

GUARD = None

# Outside PROBE_DIR: relaunching wipes that tree, which would drop the lock
# while the driver holding it is still alive and still spawning Vivados.
LOCK = PROBE_DIR.parent / ".mesh_probe.lock"


def take_lock(force):
    """Fail unless this is the only driver. Two drivers share probe dirs and
    silently clobber each other's projects."""
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    if force:
        LOCK.unlink(missing_ok=True)
    try:
        fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        sys.exit(
            f"another driver holds {LOCK} (pid "
            f"{LOCK.read_text(errors='ignore').strip()}); "
            f"stop it, or pass --force if it is dead"
        )
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    atexit.register(lambda: LOCK.unlink(missing_ok=True))


#: tag -> (mesh module, slr, NoC L2 depth, xdma, MAG URAM[, impl strategy])
#: NoC L2 4096 = 4 URAM per adapter. MAG 64 URAM = 4 banks, 32 = 2 banks.
SSI_CONGEST = "Congestion_SSI_SpreadLogic_high"
WAVES = {
    "wave1": [
        ("m72_l4", "ktpu_ship_2x2_7c2v_1m_pump", 3, 4096, 0, 64),
        ("m72_l8", "ktpu_ship_2x2_7c2v_1m_pump", 3, 8192, 0, 64),
        ("m60_s1", "ktpu_ship_2x1_6c0v_1m_pump", 1, 4096, 1, 64),
        ("m62_s1", "ktpu_ship_2x2_6c2v_1m_pump", 1, 4096, 1, 64),
    ],
    # The real candidates, all at 64+4, rebuilt on the reset/replication fixes.
    # m60_r64 runs alongside these; it is the 6+0 point of the same set.
    "wave15": [
        ("m72_r64", "ktpu_ship_2x2_7c2v_1m_pump", 3, 4096, 0, 64),
        ("m82_r64", "ktpu_ship_2x2_8c2v_1m_pump", 3, 4096, 0, 64),
        ("m62_r64", "ktpu_ship_2x2_6c2v_1m_pump", 1, 4096, 1, 64),
    ],
    "wave15b": [
        ("m72_r64", "ktpu_ship_2x2_7c2v_1m_pump", 3, 4096, 0, 64),
    ],
    # Round 1.6: the ~16k datapath reset pins on top of wave15's three fixes.
    # Same config as m72_l4 (-4.245) and m72_r64, so the three are a clean set.
    # 7+2 was worst pre-fix (-4.245, FIFO-reset path); 6+2 is the SLR1 density
    # ceiling and failed on a CE path instead, so the two probe different modes.
    "wave16": [
        ("m72_r2", "ktpu_ship_2x2_7c2v_1m_pump", 3, 4096, 0, 64),
        ("m62_r2", "ktpu_ship_2x2_6c2v_1m_pump", 1, 4096, 1, 64),
    ],
    "wave16b": [
        ("m62_r2", "ktpu_ship_2x2_6c2v_1m_pump", 1, 4096, 1, 64),
    ],
    # wave16's RTL plus a congestion strategy; the default flow applies none.
    # m60 leads: at 85% CLB it has the free area a spread directive needs.
    "wave17": [
        ("m60_r7", "ktpu_ship_2x1_6c0v_1m_pump", 1, 4096, 1, 64, SSI_CONGEST),
        ("m72_r7", "ktpu_ship_2x2_7c2v_1m_pump", 3, 4096, 0, 64, SSI_CONGEST),
        ("m82_r7", "ktpu_ship_2x2_8c2v_1m_pump", 3, 4096, 0, 64, SSI_CONGEST),
        ("m62_r7", "ktpu_ship_2x2_6c2v_1m_pump", 1, 4096, 1, 64, SSI_CONGEST),
    ],
    # The first wave on correct clocking: no per-CU divider, one ktpu_pumpclk
    # per mesh, UNIT_CDC 1, and the matmul pair its own async clock group.
    "wave20": [
        ("m60_c1", "ktpu_ship_2x1_6c0v_1m_pump", 1, 4096, 1, 64, SSI_CONGEST),
        ("m72_c1", "ktpu_ship_2x2_7c2v_1m_pump", 3, 4096, 0, 64, SSI_CONGEST),
        ("m82_c1", "ktpu_ship_2x2_8c2v_1m_pump", 3, 4096, 0, 64, SSI_CONGEST),
        ("m62_c1", "ktpu_ship_2x2_6c2v_1m_pump", 1, 4096, 1, 64, SSI_CONGEST),
    ],
    # 32+4 against wave17's 64+4, every other variable held: same RTL, same
    # strategy, same L2 depth, same die. Halves the URAM columns the L2 spans.
    "wave19": [
        ("m60_u32", "ktpu_ship_2x1_6c0v_1m_pump", 1, 4096, 1, 32, SSI_CONGEST),
        ("m62_u32", "ktpu_ship_2x2_6c2v_1m_pump", 1, 4096, 1, 32, SSI_CONGEST),
        ("m72_u32", "ktpu_ship_2x2_7c2v_1m_pump", 3, 4096, 0, 32, SSI_CONGEST),
        ("m82_u32", "ktpu_ship_2x2_8c2v_1m_pump", 3, 4096, 0, 32, SSI_CONGEST),
    ],
    # Only if 64+4 still shows URAM-side paths after the fixes.
    "wave2": [
        ("m82_m32", "ktpu_ship_2x2_8c2v_1m_pump", 3, 4096, 0, 32),
        ("m72_m32", "ktpu_ship_2x2_7c2v_1m_pump", 3, 4096, 0, 32),
        ("m62_m32", "ktpu_ship_2x2_6c2v_1m_pump", 1, 4096, 1, 32),
    ],
}


# BD 41-69 / IP_Flow 19-3428 / Ip 78-90 was a virus scanner denying reads of the
# Vivado install, not Vivado concurrency. The retry stays as the backstop.
RETRIES = 8
# Not just the XGUI race: four meshes in OOC synthesis at once took 141 GB of
# free memory to 2.8 GB. Spread the synthesis peak, not only the startup.
IMPL_STAGGER_S = 120
TRANSIENT = (
    "couldn't read file",
    "Failed to create Customization object",
    "find_approot",
    "Error sourcing TCL script",
    "slave interpreter",
    "initialization of Rule object",
    "bd_rule",
    # Feature/licence checkout also loses under many live Vivados.
    "Failed to load feature",
    "Common 17-217",
    # DDR4 phy regeneration works in a shared %TEMP% area and loses
    # its own generated XDC when probes overlap.
    "Mig 66-119",
    "Failed to generate and synthesize debug IP",
)


def _vivado(wd, log, tcl, tclargs):
    """Run one Vivado batch job in `wd`. Returns the log text, or None."""
    cmd = [
        str(VIVADO),
        "-mode",
        "batch",
        "-notrace",
        "-log",
        log,
        "-journal",
        log.replace(".log", ".jou"),
        "-source",
        str(tcl),
        "-tclargs",
    ] + tclargs
    # Vivado can die with its reason on stderr and nothing in the .log, so keep
    # it: DEVNULL here once cost a whole debugging round.
    with open(wd / (log + ".out"), "wb") as out:
        try:
            rc, _, _ = run_guarded(
                GUARD, cmd, cwd=str(wd), stdout=out, stderr=subprocess.STDOUT
            )
        except Exception as exc:  # noqa: BLE001 - one launch must not kill the batch
            print(f"  {wd.name}: {exc}")
            rc = -1
    if rc:
        print(f"  {wd.name}: vivado exit {rc} ({log})")
    p = wd / log
    return p.read_text(errors="ignore") if p.exists() else None


def build_one(idx_job):
    """Create one probe's project and block design. Returns True on success."""
    _idx, job = idx_job
    tag, mod, slr, l2, xdma, mag = job[:6]
    strat = job[6] if len(job) > 6 else "none"
    wd = PROBE_DIR / tag
    wd.mkdir(parents=True, exist_ok=True)
    args = [tag, mod, str(slr), str(l2), str(xdma), str(mag), strat, "build"]
    for attempt in range(RETRIES):
        if GUARD is not None and GUARD.tripped:
            return tag, False
        text = _vivado(wd, "run.log", TCL, args)
        if text is None:
            return tag, False
        if f"@@@ probe {tag}:" in text:
            return tag, True
        if not any(t in text for t in TRANSIENT):
            return tag, False
        print(f"  {tag}: install-tree contention, retry {attempt + 1}/{RETRIES - 1}")
        # Jittered: probes that collided once back off together and collide
        # again on every identical retry.
        time.sleep(random.uniform(30, 120))
    return tag, False


def impl_one(idx_job):
    """Synthesise and implement one already-built probe project.

    Retries the same startup faults build_one does: the XGUI/rule init race is
    a property of Vivado starting up, so it kills an impl run just as readily,
    and without a retry that probe is simply lost.
    """
    idx, job = idx_job
    tag = job[0]
    wd = PROBE_DIR / tag
    # Offset only the startup window; the hours of synthesis still overlap.
    time.sleep(idx * IMPL_STAGGER_S)
    for attempt in range(RETRIES):
        if GUARD is not None and GUARD.tripped:
            return tag, None
        text = _vivado(wd, "impl.log", TCL_IMPL, [tag])
        if text is None:
            return tag, None
        if f"@@@ probe {tag} done" in text or not any(t in text for t in TRANSIENT):
            return tag, text
        print(
            f"  {tag}: impl lost the startup race, "
            f"retry {attempt + 1}/{RETRIES - 1}",
            flush=True,
        )
        time.sleep(random.uniform(30, 120))
    return tag, text


#: the per-SLR rows worth reading, in report order
SLR_ROWS = ["CLB", "CLB LUTs", "CLB Registers", "Block RAM Tile", "URAM", "DSPs"]


def report(tag):
    """Per-SLR resource usage, SLL per boundary, and WNS for one finished run.

    Returns lines of plain text. Resource usage and WNS read together are the
    signal: a probe can route and still have spilled into empty neighbours, and
    the SLL count is what shows it.
    """
    proj = PROBE_DIR / tag / f"mp_{tag}.runs" / "impl_1"
    out = [f"=== {tag}"]
    up = list(proj.glob("*utilization_placed.rpt"))
    if not up:
        return out + ["  no placed utilization -- run did not reach placement"]
    txt = up[0].read_text(errors="ignore")

    for row in SLR_ROWS:
        m = re.search(
            r"^\|\s*" + re.escape(row) + r"\s*\|\s*([\d.]+)\s*\|\s*"
            r"([\d.]+)\s*\|\s*([\d.]+)\s*\|\s*([\d.]+)\s*\|"
            r"\s*([\d.]+)\s*\|\s*([\d.]+)\s*\|\s*([\d.]+)\s*\|"
            r"\s*([\d.]+)\s*\|",
            txt,
            re.MULTILINE,
        )
        if m:
            g = m.groups()
            out.append(
                f"  {row:<16} SLR0 {g[0]:>8} ({g[4]:>5}%)  "
                f"SLR1 {g[1]:>8} ({g[5]:>5}%)  "
                f"SLR2 {g[2]:>8} ({g[6]:>5}%)  "
                f"SLR3 {g[3]:>8} ({g[7]:>5}%)"
            )
    cs = list(proj.glob("*control_sets_placed.rpt"))
    if cs:
        ct = cs[0].read_text(errors="ignore")

        def num(label):
            m = re.search(re.escape(label) + r"\s*\|\s*(\d+)", ct)
            return m.group(1) if m else "?"

        out.append(
            f"  control sets {num('Total control sets')}"
            f" (min {num('Minimum number of control sets')},"
            f" +synth {num('Addition due to synthesis replication')},"
            f" +phys {num('Addition due to physical synthesis replication')})"
            f"  dead FF sites "
            f"{num('Unused register locations in slices containing registers')}"
        )

    sll = re.findall(
        r"\| (SLR\d <-> SLR\d)\s+\|\s+(\d+)\s+\|\s+\|\s+(\d+)\s+\|\s+([\d.]+)", txt
    )
    for name, used, _avail, pct in sll:
        out.append(f"  SLL {name:<14} {used:>6}  {pct}%")

    for rpt in proj.glob("*timing_summary_routed.rpt"):
        for ln in rpt.read_text(errors="ignore").splitlines():
            f = ln.split()
            if len(f) > 6 and re.fullmatch(r"-?\d+\.\d+", f[0]):
                out.append(f"  WNS {f[0]}  TNS {f[1]}  failing {f[2]}")
                break
        break
    rl = proj / "runme.log"
    if rl.exists():
        t = rl.read_text(errors="ignore")
        m = re.search(r"Post Placement Timing Summary WNS=(-?[\d.]+)", t)
        if m:
            out.append(f"  post-place WNS {m.group(1)}")
        out += routability(t)
    out += critical_path(proj)
    return out


def routability(runlog):
    """Whether the router actually succeeded, and how congested it got.

    v5 died on global congestion, so the congestion level is the number that
    decides whether a probe transfers to the full design -- a clean WNS on a
    level-7 route means nothing.
    """
    out = []
    if "Design is not routable" in runlog:
        out.append("  ROUTE FAILED: design is not routable")
    nr = re.findall(r"(\d+) failed nets", runlog)
    if nr and nr[-1] != "0":
        out.append(f"  ROUTE FAILED: {nr[-1]} failed nets")
    if "[Place 46-14]" in runlog:
        out.append("  placer flagged the design as highly congested")
    n = len(re.findall(r"\[Place 30-1229\]", runlog))
    if n:
        out.append(f"  {n} USER_SLL_REG placements ignored by the SLR partitioner")

    # Congestion is reported as a REGION SIZE per direction, not a level, and
    # bigger is worse. The last block is the whole device; per-SLR ones follow.
    blocks = list(re.finditer(r"Estimated Congestion(.{0,1400})", runlog, re.DOTALL))
    if blocks:
        # Four directions and stop: the per-SLR tables follow the device one.
        rows = re.findall(
            r"\|\s*(North|South|East|West)\|\s*(\d+x\d+)\|"
            r"\s*(\d+x\d+)\|\s*(\d+x\d+)\|",
            blocks[0].group(1),
        )[:4]
        if rows:

            def worst(i):
                return max(rows, key=lambda r: int(r[i].split("x")[0]))[i]

            out.append(
                f"  congestion worst  global {worst(1)}  "
                f"long {worst(2)}  short {worst(3)}"
            )
            for d, g, lg, s in rows:
                out.append(f"    {d:<6} global {g:>8}  long {lg:>8}  short {s:>8}")
    if "route_design completed successfully" in runlog:
        out.append("  route_design completed successfully")
    return out


def critical_path(proj):
    """The worst routed path: clock, endpoints, logic levels, net/logic split.

    Which clock owns WNS decides what the number means -- a DDR-domain path and
    a mesh-domain path at the same slack call for opposite responses.
    """
    rpt = list(proj.glob("*timing_summary_routed.rpt"))
    if not rpt:
        return []
    txt = rpt[0].read_text(errors="ignore")
    # The report holds one section PER CLOCK GROUP, each sorted internally, so
    # the first violated path is only the first group's -- take the minimum.
    hits = [
        (float(m.group(1)), m.start())
        for m in re.finditer(r"Slack \(VIOLATED\) :\s*(-[\d.]+)ns", txt)
    ]
    if not hits:
        m = re.search(r"Slack \(MET\) :\s*([\d.]+)ns", txt)
        return [f"  no violated path, best slack {m.group(1)}ns"] if m else []
    slack, pos = min(hits)
    blk = txt[pos : pos + 2600]

    def grab(k, default="?"):
        g = re.search(rf"^\s*{k}:\s*(.+?)\s*$", blk, re.MULTILINE)
        return g.group(1) if g else default

    clk = re.findall(r"clocked by (\S+)", blk)
    out = [f"  critical path {slack}ns  group {grab('Path Group')}"]
    if len(clk) >= 2:
        out.append(f"    {clk[0]} -> {clk[1]}   req {grab('Requirement')}")
    out.append(f"    src {grab('Source')}")
    out.append(f"    dst {grab('Destination')}")
    out.append(f"    {grab('Data Path Delay')}")
    out.append(f"    levels {grab('Logic Levels')}  skew {grab('Clock Path Skew')}")
    return out


#: pins that are not data: a violation here delays reset release, not compute
CTRL_PINS = ("R", "S", "CLR", "PRE")
RST_RE = re.compile(r"rst|reset", re.IGNORECASE)


def paths(tag, top=6):
    """Split violated paths into reset/control and real data, worst first.

    A reset arriving late costs placement and routing room but does not limit
    the clock, so the headline WNS is meaningless until the two are separated.
    """
    proj = PROBE_DIR / tag / f"mp_{tag}.runs" / "impl_1"
    rpt = list(proj.glob("*timing_summary_routed.rpt"))
    if not rpt:
        return [f"=== {tag}", "  no routed timing summary"]
    txt = rpt[0].read_text(errors="ignore")

    def grab(blk, k):
        g = re.search(rf"^\s*{k}:\s*(.+?)\s*$", blk, re.MULTILINE)
        return g.group(1) if g else ""

    seen, rst, dat = set(), [], []
    for m in re.finditer(r"Slack \(VIOLATED\) :\s*(-[\d.]+)ns", txt):
        blk = txt[m.start() : m.start() + 1800]
        src, dst = grab(blk, "Source"), grab(blk, "Destination")
        if not dst or (src, dst) in seen:
            continue
        seen.add((src, dst))
        pin = dst.rsplit("/", 1)[-1].rstrip("]0123456789[")
        rec = (
            float(m.group(1)),
            src,
            dst,
            grab(blk, "Data Path Delay"),
            grab(blk, "Logic Levels"),
        )
        # Reset-driven if it ENDS on a set/reset pin or STARTS at a reset flop.
        (rst if pin in CTRL_PINS or RST_RE.search(src) else dat).append(rec)

    def short(p):
        """Drop the wrapper prefix and any bit index, keep the hierarchy."""
        p = re.sub(r"^mp_\w+?_i/(mesh_u/inst/)?", "", p)
        return re.sub(r"\[\d+\]", "[]", p)

    out = [f"=== {tag}"]
    # The class matters more than the worst example: these paths sit within a
    # tenth of a nanosecond of each other, so fixing one alone moves nothing.
    bypin, bymod = {}, {}
    for s, src, dst, _d, _l in dat + rst:
        pin = dst.rsplit("/", 1)[-1].rstrip("]0123456789[")
        bypin[pin] = bypin.get(pin, 0) + 1
        mod = "/".join(short(dst).split("/")[:3])
        bymod[mod] = bymod.get(mod, 0) + 1
    out.append(f"  -- {len(dat) + len(rst)} violated paths by destination pin")
    for k, v in sorted(bypin.items(), key=lambda kv: -kv[1])[:10]:
        out.append(f"     {v:>5}  {k}")
    out.append("  -- by destination module")
    for k, v in sorted(bymod.items(), key=lambda kv: -kv[1])[:10]:
        out.append(f"     {v:>5}  {k}")

    # Control pins are the bulk, and they are fixed at the DRIVER, so the
    # source is what has to be replicated -- not the endpoint.
    bysrc = {}
    for s, src, dst, _d, _l in dat + rst:
        pin = dst.rsplit("/", 1)[-1].rstrip("]0123456789[")
        if pin == "D" or pin.startswith("DIN"):
            continue
        bysrc[short(src)] = bysrc.get(short(src), 0) + 1
    out.append("  -- control-pin paths by DRIVER")
    for k, v in sorted(bysrc.items(), key=lambda kv: -kv[1])[:12]:
        out.append(f"     {v:>5}  {k}")

    for name, group in (("data", dat), ("reset/control", rst)):
        out.append(f"  -- worst {name} paths ({len(group)} of them)")
        shown = set()
        for s, src, dst, dly, lv in sorted(group):
            key = (short(src), short(dst))
            if key in shown:
                continue
            shown.add(key)
            out.append(f"    {s:>8}ns  {dly}  levels {lv}")
            out.append(f"        {short(src)}")
            out.append(f"     -> {short(dst)}")
            if len(shown) >= top:
                break
    return out


def main():
    # Redirected stdout is block-buffered, so progress is invisible for hours.
    sys.stdout.reconfigure(line_buffering=True)
    ap = argparse.ArgumentParser()
    ap.add_argument("wave", choices=sorted(WAVES) + ["report", "paths"])
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument(
        "--bd-jobs",
        type=int,
        default=1,
        help="concurrent BD builds; >1 fails create_bd_design 4/4",
    )
    ap.add_argument("--tags", default="")
    ap.add_argument(
        "--force",
        action="store_true",
        help="take the driver lock even if one is left behind",
    )
    args = ap.parse_args()

    if args.wave in ("report", "paths"):
        fn = report if args.wave == "report" else paths
        tags = (
            args.tags.split(",")
            if args.tags
            else [t for w in WAVES.values() for t, *_ in w]
        )
        for t in tags:
            print("\n".join(fn(t)))
        return

    take_lock(args.force)
    global GUARD
    GUARD = JobGuard()
    jobs = WAVES[args.wave]
    print(
        f"{args.wave}: {len(jobs)} probes, {args.jobs} impls at a time, "
        f"{avail_gb():.0f} GiB free"
    )

    # Upfront so a monitor sees every probe from the start, not one per turn.
    for tag, *_ in jobs:
        (PROBE_DIR / tag).mkdir(parents=True, exist_ok=True)

    built = []
    with ThreadPoolExecutor(max_workers=args.bd_jobs or args.jobs) as ex:
        for tag, ok in ex.map(build_one, enumerate(jobs)):
            print(f"  bd {tag}: {'ok' if ok else 'FAILED'}")
            if ok:
                built.append(next(j for j in jobs if j[0] == tag))
    if not built:
        print("no block design built; nothing to implement")
        GUARD.close()
        return

    print(f"implementing {len(built)} of {len(jobs)}", flush=True)
    with ThreadPoolExecutor(max_workers=args.jobs) as ex:
        for tag, _ in ex.map(impl_one, enumerate(built)):
            print("\n".join(report(tag)))
    if GUARD.tripped:
        print("jobguard tripped: results above are incomplete")
    GUARD.close()


if __name__ == "__main__":
    main()
