"""What the visualiser is sent, checked without a simulator.

Everything the page draws -- the tiling grid, the mesh, the L1 spans, the
memory map, the heat maps, the verdict -- is assembled by pure functions in
`sim` and `bench` from a problem the driver already built. None of it needed a
simulator to be wrong, and none of it had a test: the payload could rot under a
green `check.py fast` and the first symptom would be a plausible picture of a
machine that does not exist.

The geometry checks matter most. A tiling that leaves a hole in C, or two
passes claiming one region, still renders as a tidy grid.
"""

import itertools

import numpy as np
import pytest

from kohakutpu import bench, sim, tensor

# Shapes and machines worth covering together: the cluster count divides N and
# sizes the memory image, so the same shape is a different problem at each.
CASES = [
    (2, 256, 256, 256),
    (2, 128, 128, 128),
    (4, 256, 512, 256),
    (8, 512, 1024, 256),
    (1, 64, 128, 128),
    (2, 16, 16, 32),
]


@pytest.fixture(autouse=True)
def _restore_cluster_count():
    """`use_clusters` rebinds module state, so a test must put it back."""
    before = bench.NCLUSTERS
    yield
    bench.use_clusters(before)


# ---------------------------------------------------------------- the preview
@pytest.mark.parametrize("ncl,m,n,k", CASES)
def test_preview_describes_the_problem_that_would_run(ncl, m, n, k):
    """The plan panel must not describe a different machine from the run.

    It is answered without building operands, so the only thing keeping the two
    in step is that they call the same functions -- which is exactly what this
    pins.
    """
    p = bench.preview(m, n, k, ncl)
    assert p["runnable"], p["why"]
    prob = bench.build(m, n, k, ncl=ncl)

    assert (p["padded"]["m"], p["padded"]["n"], p["padded"]["k"]) == (
        prob.m, prob.n, prob.k,
    )
    assert p["tile"]["text"] == str(prob.tile)
    assert p["passes"] == len(prob.kern.passes)
    assert p["rounds"] == len(prob.kern.rounds)
    assert p["flits"] == sum(len(x.flits) for x in prob.kern.passes)
    assert p["band"] == prob.n // prob.ncl
    assert p["grid"]["chunks"] == prob.kern.l1.chunks
    assert p["l1"]["b_resident"] == prob.kern.l1.b_resident
    # The padded problem is the work the machine really does, so a rate quoted
    # against anything else overstates it.
    assert p["macs"] == prob.m * prob.n * prob.k
    assert p["asked_macs"] == m * n * k
    assert p["macs"] >= p["asked_macs"]


def test_preview_refuses_what_check_refuses():
    """Both bench limits, with a reason, and no exception on the way out."""
    p = bench.preview(4096, 4096, 4096, 8)
    assert not p["runnable"]
    assert any("bench RAM" in w for w in p["why"])
    # A cluster count with no mesh, and a shape below one sub-tile: `check`
    # returns early on both, so the preview has nothing to describe and must
    # say so rather than divide by a zero column band. Five has no mesh --
    # the managers do not divide evenly into the left half of the columns.
    bad = next(n for n in range(1, 20) if n not in bench.CLUSTER_COUNTS)
    assert not bench.preview(16, 16, 32, bad)["runnable"]
    assert "padded" not in bench.preview(16, 16, 32, bad)
    assert not bench.preview(2, 2, 8, 2)["runnable"]


def test_build_derives_clusters_from_the_count():
    """`ncl` must size the program, not just label it.

    It used to slice the module global, so building for eight while the global
    held two planned a two-cluster program and reported eight -- with the
    memory map, the mesh and the tiling all drawn for a machine that was not
    the one dispatched to.
    """
    bench.use_clusters(2)
    prob = bench.build(256, 1024, 256, ncl=8)
    assert prob.ncl == 8
    assert len({p.cluster for p in prob.kern.passes}) == 8
    assert prob.kern.clusters == bench.cluster_list(8)


# ------------------------------------------------------------- the geometry
@pytest.mark.parametrize("ncl,m,n,k", CASES)
def test_tiling_covers_c_exactly_once(ncl, m, n, k):
    """Every output element claimed by exactly one pass.

    A hole draws as a grey cell and a double-claim draws as whichever pass was
    listed last, so neither is visible in the picture the panel produces.
    """
    prob = bench.build(m, n, k, ncl=ncl)
    t = sim._tiling(prob)
    cover = np.zeros((t["m"], t["n"]), dtype=int)
    for p in t["passes"]:
        cover[p["row0"]:p["row1"], p["col0"]:p["col1"]] += 1
    assert cover.min() == 1, "a region of C is computed by no pass"
    assert cover.max() == 1, "two passes claim the same region of C"
    assert t["band"] == prob.n // prob.ncl
    assert len(t["clusters"]) == ncl


@pytest.mark.parametrize("ncl,m,n,k", CASES)
def test_tiling_rows_and_cols_fill_the_grid_the_page_draws(ncl, m, n, k):
    """The page lays out ceil(m/tile.m) x ceil(n/tile.n) cells and indexes them
    by row0/col0 divided by the tile. Both must land on a boundary, or a pass
    silently lands in the cell next door."""
    prob = bench.build(m, n, k, ncl=ncl)
    t = sim._tiling(prob)
    rows, cols = t["m"] // t["tile"]["m"], t["n"] // t["tile"]["n"]
    seen = set()
    for p in t["passes"]:
        assert p["row0"] % t["tile"]["m"] == 0
        assert p["col0"] % t["tile"]["n"] == 0
        cell = (p["row0"] // t["tile"]["m"], p["col0"] // t["tile"]["n"])
        assert cell not in seen
        seen.add(cell)
    assert len(seen) == rows * cols


@pytest.mark.parametrize("ncl,m,n,k", CASES)
def test_l1_report_matches_the_program(ncl, m, n, k):
    """Residency is READ OFF the flits, so it reports the schedule rather than
    the intent -- which is the one thing residency can silently stop doing."""
    prob = bench.build(m, n, k, ncl=ncl)
    li = sim._l1(prob)
    for side in ("a", "b"):
        s = li[side]
        assert s["fetched"] >= s["unique"], "fewer entries fetched than exist"
        assert sum(sp["entries"] for sp in s["spans"]) <= s["entries"]
        for sp in s["spans"]:
            assert sp["start"] + sp["entries"] <= s["entries"]
    if li["b"]["resident"]:
        # Filled once per column band, so exactly the distinct entries.
        assert li["b"]["fetched"] == li["b"]["unique"]


@pytest.mark.parametrize("ncl,m,n,k", CASES)
def test_memory_regions_do_not_overlap(ncl, m, n, k):
    """Overlapping two regions is silent: the machine computes happily on
    operands another region has overwritten."""
    prob = bench.build(m, n, k, ncl=ncl)
    mm = sim._memory_map(prob)
    spans = sorted((r["start"], r["start"] + r["words"], r["name"])
                   for r in mm["regions"])
    for (_, a1, na), (b0, _, nb) in itertools.pairwise(spans):
        assert a1 <= b0, f"{na} runs into {nb}"
    assert spans[-1][1] <= mm["total"]
    # A is one image every cluster reads; B and C are one region each.
    assert sum(1 for r in mm["regions"] if r["name"] == "A") == 1
    assert sum(1 for r in mm["regions"] if r["name"].startswith("B")) == ncl


@pytest.mark.parametrize("ncl", bench.CLUSTER_COUNTS)
def test_mesh_matches_the_bench_generate_block(ncl):
    """One memory port per row, accumulator directly across from its manager,
    and NO agent node. A picture of a mesh the machine does not have still
    looks like a mesh.

    The agent used to hang off the east edge, which made MAG one module
    attached at both edges of the mesh with every dispatch through a single
    link. It shares the memory ports now and answers at port 0's coordinate, so
    an `agent` key here would draw hardware that no longer exists."""
    M = bench.mesh(ncl)
    assert M["ncl"] == ncl
    assert len(M["clusters"]) == ncl
    assert len(M["ports"]) == M["nrow"]
    assert "agent" not in M, "the agent has no node of its own any more"
    assert M["agent_port"] == [0, 1], "the agent answers on port 0"
    assert M["agent_port"] in M["ports"], "and that must be a real memory port"
    # MANAGERS OUTSIDE, ACCUMULATORS INSIDE. A cluster is one COLUMN of a band,
    # so its two endpoints are on ADJACENT routers -- the property the whole
    # layout exists for. The previous arrangement put them two columns apart
    # with another cluster's router physically between them.
    for c in M["clusters"]:
        assert c["acu"][0] == c["mgr"][0], "endpoints not in the same column"
        assert abs(c["acu"][1] - c["mgr"][1]) == 1, "endpoints not adjacent"
        assert c["mgr"][1] in (1, M["nrow"]), "manager not on an outer row"
        assert 1 <= c["acu"][1] <= M["nrow"]
        # A MAG port attaches to the NoC, not to a cluster, so this only says
        # which row's edge the cluster's memory traffic leaves by -- one of its
        # own two rows, never someone else's band.
        assert c["port"] in ([0, c["mgr"][1]], [0, c["acu"][1]])
        assert c["port"] in M["ports"], "port is not one MAG actually presents"
    # A band need NOT be full: 3, 5, 6 and 7 clusters leave columns empty and
    # are supported, merely not optimal.
    assert len(M["clusters"]) == ncl
    assert bench.cluster_list(ncl) == [tuple(c["mgr"]) for c in M["clusters"]]


# ------------------------------------------------------------- the heat maps
@pytest.mark.parametrize("shape", [(64, 64), (256, 256), (512, 1024),
                                   (300, 700), (1024, 1024)])
def test_heat_fits_the_browser_and_keeps_the_worst_element(shape):
    """The page used to receive full matrices and spread them into Math.max,
    which throws above ~100k arguments -- so every shape past the 256-cube
    failed in the browser. Reducing must not hide the element that mattered."""
    a = np.random.default_rng(0).standard_normal(shape)
    err = np.abs(a)
    h = sim._heat(err, "max")
    assert h["rows"], h["cols"] == shape
    assert len(h["data"]) * len(h["data"][0]) <= sim.HEAT_MAX_CELLS
    # A power of two, so a block boundary cannot fall inside an output tile.
    assert h["block"] & (h["block"] - 1) == 0
    assert max(max(r) for r in h["data"]) == pytest.approx(err.max())

    v = sim._heat(a, "absmax")
    flat = [x for r in v["data"] for x in r]
    assert max(abs(x) for x in flat) == pytest.approx(np.abs(a).max())
    # Signed, so a diverging map keeps both ends rather than folding them.
    if a.min() < 0:
        assert min(flat) < 0


def test_heat_leaves_small_matrices_alone():
    a = np.zeros((16, 16))
    h = sim._heat(a, "max")
    assert h["block"] == 1
    assert len(h["data"]) == 16 and len(h["data"][0]) == 16


def test_worst_is_sorted_and_carries_every_format():
    rng = np.random.default_rng(2)
    want = rng.standard_normal((48, 48))
    got = want + rng.standard_normal((48, 48)) * 1e-3
    refs = {"fp64": want, "mxfp7 model": want * 1.001,
            "mxfp8/fp32": want * 1.002, "fp16/fp32": want * 1.003}
    rel = np.abs(got - want) / np.abs(want)
    rows = sim._worst(got, want, refs, rel)
    assert len(rows) == sim.WORST_ROWS
    assert all(rows[i]["rel"] >= rows[i + 1]["rel"] for i in range(len(rows) - 1))
    assert rows[0]["rel"] == pytest.approx(rel.max())
    for r in rows:
        assert r["hw"] == pytest.approx(got[r["i"], r["j"]])
        assert "fp64" not in r["refs"], "fp64 is the reference, not a row"
        assert set(r["refs"]) == {"mxfp7 model", "mxfp8/fp32", "fp16/fp32"}


def test_worst_handles_a_matrix_smaller_than_the_table():
    got = np.ones((2, 2))
    rows = sim._worst(got, got, {"fp64": got, "mxfp7 model": got},
                      np.zeros((2, 2)))
    assert len(rows) == 4


# --------------------------------------------------------------- the timeline
def test_timeline_reports_truncation():
    """The bench stops recording after a fixed number of windows. Drawn without
    this, a short trace looks like a complete picture of a faster machine."""
    tel = {"trace": {"gap": 64, "cu": {0: [1] * 100}},
           "host_cycles": 1000, "run_cycles": 90000}
    tl = sim._timeline(tel)
    assert tl["covered_cycles"] == 6400
    assert tl["measured_cycles"] == 91000
    assert tl["truncated"]

    full = {"trace": {"gap": 64, "cu": {0: [1] * 100}},
            "host_cycles": 100, "run_cycles": 6000}
    assert not sim._timeline(full)["truncated"]
    assert sim._timeline({}) is None


def test_timeline_summarises_each_cluster():
    """The exact phase counters exist for two clusters only, so this sampled
    summary is the only per-cluster measurement for the ones in between."""
    tel = {"trace": {"gap": 64, "cu": {0: [1, 3, 4, 0], 1: [0, 0, 2, 2]}},
           "host_cycles": 0, "run_cycles": 256}
    c = sim._timeline(tel)["cluster"]
    assert c[0]["fill"] == 2 and c[0]["gemm"] == 1 and c[0]["drain"] == 1
    assert c[0]["idle"] == 1
    assert (c[0]["first"], c[0]["last"]) == (0, 2)
    # Cluster 1 starts two windows later: that difference is the dispatch skew
    # the cooperation panel reports.
    assert (c[1]["first"], c[1]["last"]) == (2, 3)


# ----------------------------------------------------------------- the gate
def _rel(p50=1.7e-4, mx=1.0, over10=1e-5):
    return {"rel": {"p50": p50, "max": mx, "over10": over10,
                    "over10_n": int(over10 * 65536), "n": 65536}}


def test_verdict_passes_a_healthy_run():
    v = sim._verdict(True, _rel(), _rel(p50=3e-3), {})
    assert v["pass"]
    assert all(c["pass"] for c in v["checks"])


@pytest.mark.parametrize(
    "kw,name",
    [
        ({"p50": 2e-3}, "vs mxfp7 model p50"),
        ({"mx": 220.0}, "vs mxfp7 model worst"),
        ({"over10": 2e-3}, "elements over 10%"),
    ],
)
def test_precision_is_reported_and_does_not_fail_the_run(kw, name):
    """A precision figure outside its reference is an ADVISORY, not a failure.

    The thresholds were fitted to one family of shapes. An output that cancels
    against its own partial sums has a large relative error and a correct
    circuit, so failing on it marks good runs bad -- and a verdict that cries
    wolf is one nobody reads. Only a run that did not HAPPEN fails.
    """
    v = sim._verdict(True, _rel(**kw), _rel(p50=3e-3), {})
    assert v["pass"], "a precision miss must not fail an otherwise sound run"
    assert [c["name"] for c in v["checks"] if not c["pass"]] == [name]
    assert v["advisories"] == [name], "and it must still be surfaced"


def test_verdict_fails_when_nothing_came_out():
    """A hang or a dead datapath leaves C unwritten. That is the case the
    precision thresholds used to catch by accident, and it now has its own
    gating check rather than depending on one."""
    nan = float("nan")
    v = sim._verdict(True, _rel(p50=nan), _rel(p50=3e-3), {})
    assert not v["pass"]
    assert any(c["name"] == "produced an answer" and not c["pass"]
               for c in v["checks"])


def test_verdict_fails_on_rtl_assertions_and_on_no_completion():
    """The bench names its own cause; a run that printed one is not a pass
    however good the numbers look."""
    v = sim._verdict(True, _rel(), _rel(p50=3e-3), {"errors": ["ERROR x"]})
    assert not v["pass"]
    assert v["checks"][-1]["name"] == "RTL assertions"
    assert not sim._verdict(False, _rel(), _rel(p50=3e-3), {})["pass"]


def test_verdict_matches_the_documented_thresholds():
    """The gate is quoted in run_matmul.py's docstring and in the page, so the
    numbers are pinned rather than left to drift."""
    limits = {c["name"]: c["limit"]
              for c in sim._verdict(True, _rel(), _rel(p50=3e-3), {})["checks"]}
    assert limits["vs mxfp7 model p50"] == 1e-3
    assert limits["vs mxfp7 model worst"] == 10.0
    assert limits["elements over 10%"] == 5e-4
    assert limits["vs fp64 p50"] == 1e-2


# ------------------------------------------------------------- the sim budget
@pytest.mark.parametrize("ncl,m,n,k", CASES)
def test_timeout_budget_scales_with_the_work(ncl, m, n, k):
    """One flat constant could not cover both ends of the range this session
    runs: 180 s was generous for the default shape and too small for the
    eight-cluster reference, which it failed while calling the design stalled."""
    s = sim.Session()
    prob = bench.build(m, n, k, ncl=ncl)
    assert s._budget(prob) >= sim.SIM_BASE_SECONDS
    # The estimate is an upper bound on a healthy run, so it must exceed the
    # arithmetic floor -- the run cannot beat peak rate.
    floor = prob.m * prob.n * prob.k / (sim.MACS_PER_CLUSTER * prob.ncl)
    assert sim._estimate_cycles(prob) > floor


def test_eight_cluster_reference_gets_more_than_it_needed():
    """The reference case completes inside 480 s, measured. The budget has to
    clear that with room, or the guard fires on healthy work again."""
    s = sim.Session()
    prob = bench.build(512, 1024, 256, ncl=8)
    assert s._budget(prob) > 480
    small = bench.build(16, 16, 32, ncl=2)
    # ... and must not hand a tiny problem the same wait on a wedged simulator.
    assert s._budget(small) < s._budget(prob) / 4


def test_timeout_env_override_wins(monkeypatch):
    monkeypatch.setenv("KOHAKU_SIM_TIMEOUT", "42")
    s = sim.Session()
    assert s._budget(bench.build(512, 1024, 256, ncl=8)) == 42.0


def test_timeout_message_separates_slow_from_silent():
    """The old message asserted the one thing it could not know. The bench
    detects a stalled design itself, so this must not claim to."""
    slow = sim._timeout_message(300.0, 2.0, ["--- 3 rounds ---"])
    quiet = sim._timeout_message(300.0, 300.0, [])
    assert "still working" in slow and "never have started" not in slow
    assert "never have started" in quiet
    for msg in (slow, quiet):
        assert "most likely stalled" not in msg
        assert "KOHAKU_SIM_TIMEOUT" in msg
        assert "ROUND-FAIL" in msg


# ------------------------------------------------------- the bench's own log
# COPIED FROM `mag_driver_tb.v`'s $display calls, formatting and all. The whole
# telemetry path is a text contract between a Verilog bench and a regex, and
# nothing else notices when one side moves: a counter that stops parsing simply
# reads zero, and a zero on a dashboard looks like a measurement.
BENCH_LOG = """
--- upload 3 regions, 8192 beats, 26196 cycles ---
--- 1 rounds ---
    cu0 f=1234 g=5678 d=910  cu1 f=1230 g=5670 d=900  mem rd=4096 wr=2048
    OCCUPANCY cu0 4000..78000  cu1 4200..78100  overlap 17600 cycles
    PROFILE host=1200 run=18701 fill=9000 gemm=26000 drain=3000 idle=1400 \
rd=4096 wr=2048 beat=32
    FETCH lat=31 entries=1024 floor=4
    UPLOAD regions=3 beats=8192 cycles=26196
    NOC in=9000 out=9100 ports=2 chans=3
    MAGSTATE idle=15000 qfill=8000 qwait=900 qemit=7000 rd=4000 wr=2000 host=120
    STALL in_bp=300 out_bp=200 cu_send=150 cu_dry=90
    WHY wack_nob=10 wack_blk=20 wslot_full=30 rfill_dry=40 rwait_emit=50 \
emit_bp=60 cu_dwait=70 cu_gwait=80
    TRACE cu0 gap=64 137f0
    TRACE cu1 gap=64 1370f
  ORCH-OK
"""


def test_telemetry_parses_every_line_the_bench_prints():
    t = sim._telemetry(BENCH_LOG.splitlines())
    assert t["run_cycles"] == 18701
    assert t["host_cycles"] == 1200
    assert (t["fill_cycles"], t["gemm_cycles"]) == (9000, 26000)
    assert (t["rd_beats"], t["wr_beats"], t["beat_bytes"]) == (4096, 2048, 32)
    assert (t["fetch_lat"], t["fetch_entries"], t["fetch_floor"]) == (31, 1024, 4)
    assert (t["upload_beats"], t["upload_cycles"]) == (8192, 26196)
    assert (t["noc_in"], t["noc_out"], t["mem_ports"]) == (9000, 9100, 2)
    assert t["mag_idle"] == 15000 and t["mag_host"] == 120
    assert t["stall_in_bp"] == 300 and t["stall_cu_dry"] == 90
    assert t["why_wack_nob"] == 10 and t["why_cu_gwait"] == 80
    assert (t["cu0_fill"], t["cu1_drain"]) == (1234, 900)
    assert t["occupancy"]["overlap_cycles"] == 17600
    # One nibble per sample window, per cluster, as a MASK.
    assert t["trace"]["gap"] == 64
    assert t["trace"]["cu"][0] == [1, 3, 7, 15, 0]
    assert "errors" not in t
    assert "round_fail" not in t


def test_telemetry_reads_the_whole_profile_into_a_payload():
    """Nothing the page charts may quietly read zero because a line stopped
    matching."""
    t = sim._telemetry(BENCH_LOG.splitlines())
    p = sim._profile(t, 256, 256, 256, 2)
    assert p["cycles"]["run"] == 18701
    assert p["rate"]["peak_macs_per_cycle"] == 1024
    assert p["mem_ports"] == 2
    # Every bucket the page draws must have arrived, not defaulted.
    for group, keys in (
        ("mag", sim.MAG_BASIS), ("stalls", sim.STALL_BASIS), ("why", sim.WHY_BASIS)
    ):
        assert set(p[group]) == set(keys)
        assert all(v["cycles"] > 0 for v in p[group].values())
    assert p["fetch"]["cycles_per_entry"] > 0
    assert all(p["resource"][k] > 0
               for k in ("flops", "mem_rd", "mem_wr", "noc_in", "noc_out"))


def _payload(ncl=2, m=128, n=128, k=128, log=None):
    """A complete payload, built the way a run builds it but with no run.

    The answer stands in as the MXFP7 model's, which is what a correct circuit
    produces -- so the structure is exercised end to end and the error panels
    see a real distribution rather than zeros.
    """
    from kohakutpu import mxfp7

    prob = bench.build(m, n, k, ncl=ncl)
    got = mxfp7.model_matmul(prob.a, prob.bt)
    return sim.payload(
        prob, got, (BENCH_LOG if log is None else log).splitlines(),
        asked={"m": m, "n": n, "k": k}, seed=1, dist="lowrank",
        cold=False, seconds=1.0, budget=300.0,
    )


# Every field the page reaches for. Written out rather than derived, so
# deleting one from the driver fails here instead of rendering as "undefined"
# in a browser nobody has open.
PAGE_FIELDS = [
    "shape", "asked", "seed", "dist", "preq", "tile", "ncl", "frequency",
    "budget_seconds", "cold", "passes", "rounds", "kernel", "seconds", "log",
    "telemetry", "profile", "listing", "dispatches", "ncmd", "memory",
    # `mesh` is the machine; `dispatch` is which of it this program kicked.
    # They differ whenever a subset runs, and the page needs both to draw an
    # idle cluster as idle rather than as absent.
    "tiling", "mesh", "dispatch", "l1", "timeline", "a_peek", "bt_peek", "c_peek",
    "want_peek", "got", "want", "abs_err", "rel_err", "worst", "err_hw",
    "err_vs", "err_total", "ok", "verdict",
]


@pytest.mark.parametrize("ncl", [1, 2, 4, 8])
def test_payload_carries_everything_the_page_draws(ncl):
    d = _payload(ncl=ncl, m=128, n=128 * ncl, k=128)
    assert set(d) == set(PAGE_FIELDS)
    assert d["ncl"] == ncl
    assert d["verdict"]["pass"], "the model's own answer must score as correct"
    assert d["frequency"] == sim.F_TARGET
    # The four maps carry their true shape alongside the reduced grid.
    for key in ("got", "want", "abs_err", "rel_err"):
        h = d[key]
        assert (h["rows"], h["cols"]) == (d["shape"]["m"], d["shape"]["n"])
        assert len(h["data"]) and len(h["data"][0])
    # The panels that used to render "not reported" when a field went missing.
    assert d["profile"] and d["timeline"] and d["l1"] and d["tiling"]
    assert d["mesh"]["ncl"] == ncl
    assert len(d["kernel"]) > 4 and "..." not in d["kernel"][-1]
    assert len(d["dispatches"]) >= 1
    assert d["worst"] and "mxfp7 model" in d["worst"][0]["refs"]


def test_payload_is_json_and_stays_small_at_the_largest_reference():
    """The whole reason for decimating. Ten full matrices of a 512x1024 answer
    were ~100 MB of JSON, which is not a one-second interaction."""
    import json

    d = _payload(ncl=8, m=512, n=1024, k=256)
    raw = json.dumps(d)
    assert len(raw) < 8_000_000, f"{len(raw) / 1e6:.1f} MB payload"
    # And it must still be the full problem that was measured, not a crop.
    assert d["got"]["rows"] == 512 and d["got"]["cols"] == 1024
    assert d["err_hw"]["rel"]["n"] == 512 * 1024


def test_payload_reports_a_failing_run_without_pretending_to_pass():
    d = _payload(log="  ROUND-FAIL round 0 stopped at pc=12\n  ORCH-FAIL")
    assert not d["ok"]
    assert not d["verdict"]["pass"]
    assert "pc 12" in d["verdict"]["checks"][0]["text"]
    # No PROFILE line, so there is nothing to chart and the page must be told
    # rather than shown zeros.
    assert d["profile"] is None
    assert d["timeline"] is None


def test_telemetry_captures_a_failing_round():
    """The bench's diagnosis of a wedge -- which round, which control step --
    reached the payload as nothing at all, so it survived only in the raw log."""
    log = [
        "  ROUND-FAIL round 2 stopped at pc=50",
        "      after 20000 cycles, 37 of 96 commands, 20000 idle",
        "      cu0 f=1 g=2 d=3  cu1 f=4 g=5 d=6",
        "  ERROR host AXI stalled on wready at 1234",
        "  ORCH-FAIL",
    ]
    t = sim._telemetry(log)
    rf = t["round_fail"]
    assert (rf["round"], rf["pc"]) == (2, 50)
    assert (rf["retired"], rf["commands"], rf["cycles"]) == (37, 96, 20000)
    assert t["pc"] == 50
    assert len(t["errors"]) == 1
    assert "ROUND-FAIL round 2 at pc 50" in sim._round_fail_text(t)
    assert "37 of 96 commands" in sim._round_fail_text(t)
    # A healthy run has no pc to report, and must not invent one.
    assert sim._round_fail_text({}) == "no ORCH-OK reported"


# --------------------------------------------------------- units and bases
def test_port_summed_buckets_are_divided_by_the_port_count():
    """The MAG and fabric counters are sums over memory ports. Out of the run
    alone they read past 100% and were charted beside cluster-0 counters as if
    the two were comparable."""
    t = {"mag_idle": 400, "mag_host": 100,
         "stall_in_bp": 200, "stall_cu_dry": 50,
         "why_emit_bp": 400, "why_cu_gwait": 25}
    run, ports = 100, 4
    mag = sim._bucket(t, "mag_", sim.MAG_BASIS, run, ports)
    assert mag["idle"]["frac"] == pytest.approx(1.0)   # 400 / (100 * 4)
    assert mag["idle"]["basis"] == "ports"
    assert mag["host"]["frac"] == pytest.approx(1.0)   # 100 / 100, one unit
    assert mag["host"]["basis"] == "machine"

    st = sim._bucket(t, "stall_", sim.STALL_BASIS, run, ports)
    assert st["in_bp"]["frac"] == pytest.approx(0.5)
    assert st["cu_dry"]["frac"] == pytest.approx(0.5)
    assert st["cu_dry"]["basis"] == "cluster0"

    why = sim._bucket(t, "why_", sim.WHY_BASIS, run, ports)
    assert why["emit_bp"]["frac"] == pytest.approx(1.0)
    assert why["cu_gwait"]["basis"] == "cluster0"
    # Every bar the page draws is now a fraction of a real resource.
    for group in (mag, st, why):
        for v in group.values():
            assert 0.0 <= v["frac"] <= 1.0


def test_profile_fetch_divides_by_the_cluster_count():
    """`fill_cycles` is occupancy over every cluster; `entries` counts L1
    writes on cluster 0. The command line hard-coded the divisor at 2, which
    was wrong by 2x at four clusters and 4x at eight."""
    t = {
        "run_cycles": 1000, "host_cycles": 10, "fill_cycles": 800,
        "gemm_cycles": 0, "drain_cycles": 0, "idle_cycles": 0,
        "rd_beats": 10, "wr_beats": 10, "beat_bytes": 32,
        "fetch_entries": 100, "fetch_lat": 30, "fetch_floor": 4,
        "mem_ports": 4,
    }
    p = sim._profile(t, 64, 64, 64, 8)
    assert p["fetch"]["entries_all"] == 800
    assert p["fetch"]["cycles_per_entry"] == pytest.approx(1.0)
    # The five resource budgets are per port for everything but flops.
    assert p["resource"]["mem_rd"] == pytest.approx(10 / 1000 / 4)


def test_stats_report_counts_next_to_rates():
    """The defect RATE is what held steady across cluster counts while the
    worst element moved; a bare fraction cannot be checked against a run."""
    want = np.ones(1000)
    got = want.copy()
    got[:7] = 2.0  # 7 elements 100% out
    st = sim._stats(got, want)
    assert st["rel"]["over1_n"] == 7
    assert st["rel"]["over10_n"] == 7
    assert st["rel"]["n"] == 1000
    assert st["rel"]["over1"] == pytest.approx(7 / 1000)


def test_host_word_estimate_matches_the_upload_the_bench_reads():
    """The budget's upload term is derived from the same image `check` bounds
    against MAX_HOST, so the two cannot disagree about how big a problem is."""
    prob = bench.build(256, 256, 256, ncl=2)
    words, _ = bench.upload(prob)
    assert sim._host_words(prob) == len(words)
    assert bench._host_words(prob.m, prob.n, prob.k) == len(words)


def test_entry_words_halve_when_prequantised():
    assert tensor.entry_words(True) * 2 == tensor.entry_words(False)
    a = bench.preview(256, 256, 256, 2, (True, True))
    b = bench.preview(256, 256, 256, 2, (False, False))
    assert a["memory"]["words"] < b["memory"]["words"]
    # The host always sends FP16, so the upload does not shrink.
    assert a["host"]["words"] == b["host"]["words"]
