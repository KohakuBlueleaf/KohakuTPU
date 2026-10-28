"""`kernels.relayout`: every form bit-exact against numpy views.

Offline: each plan's parts executed step by step (`relayout.execute`, the
kernel's statements on bit patterns) against numpy over runs (aligned,
misaligned, short, long, kept, line-padded), splats, transposes, spreads
and whole-word walks; each named mutant of the planning is caught; the
kernels compile and pass the static checker (L1.VER.017 line ownership).
On the card model (``dev``): the production path (`forms.relayout`) per
form, bit-exact.
"""

from dataclasses import replace

import numpy as np
import pytest

from kohakutpu.application.kernels import lanes as LN
from kohakutpu.application.kernels import relayout as R
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime.packages import Refused
from kohakutpu.application.runtime.storage import Storage
from kohakutpu.language import machine as MF

BOARD = "multimesh_v9t6"
HW = R.hw_of(MF.load(BOARD), 2)
NEG_ZERO = 0x8000

#: (source elements, walk, at, dwalk, dat, m, keep, z0): one a class.
CASES = {
    "runs_repitch_misaligned": (
        1344,
        ((48, 28), (28, 1)),
        0,
        ((48, 32), (28, 1)),
        0,
        1536,
        False,
        0,
    ),
    "runs_compact_shared_lines": (
        768,
        ((8, 96), (84, 1)),
        0,
        ((8, 84), (84, 1)),
        0,
        672,
        False,
        0,
    ),
    "runs_short_rows": (
        80,
        ((8, 10), (10, 1)),
        0,
        ((8, 16), (10, 1)),
        0,
        128,
        False,
        0,
    ),
    "runs_dense_short": (
        256,
        ((8, 32), (10, 1)),
        0,
        ((8, 10), (10, 1)),
        0,
        80,
        False,
        0,
    ),
    "runs_long": (
        591 * 224,
        ((591, 224), (197, 1)),
        0,
        ((591, 197), (197, 1)),
        0,
        591 * 197,
        False,
        0,
    ),
    "runs_offset_keep": (
        500,
        ((7, 33), (21, 1)),
        5,
        ((7, 40), (21, 1)),
        3,
        288,
        True,
        0,
    ),
    "runs_line_pad": (
        300,
        ((6, 40), (37, 1)),
        2,
        ((6, 41), (37, 1)),
        16,
        272,
        False,
        16,
    ),
    "runs_buffer_ends": (84, ((84, 1),), 0, ((84, 1),), 0, 96, False, 0),
    "runs_negative_shift": (
        200,
        ((3, 50), (40, 1)),
        7,
        ((3, 48), (40, 1)),
        1,
        160,
        False,
        0,
    ),
    "splat_bias": (
        16,
        ((8, 0), (16, 1), (100, 0)),
        0,
        ((8, 1600), (16, 100), (100, 1)),
        0,
        12800,
        False,
        0,
    ),
    "splat_pairs": (
        3200,
        ((640, 5), (2, 0), (5, 1), (2, 0)),
        0,
        ((640, 20), (2, 10), (5, 2), (2, 1)),
        0,
        12800,
        False,
        0,
    ),
    "splat_lane_copies": (
        6272,
        ((8, 784), (28, 28), (28, 1), (16, 0)),
        0,
        ((8, 16384), (28, 512), (28, 16), (16, 1)),
        0,
        131072,
        False,
        0,
    ),
    "splat_offset": (40, ((5, 1), (37, 0)), 19, ((5, 37), (37, 1)), 3, 192, False, 0),
    # A 2 x 2 upsample: words straddling a doubled row take two source words.
    "pull_upsample": (
        42,
        ((6, 7), (2, 0), (7, 1), (2, 0)),
        0,
        ((6, 28), (2, 14), (7, 2), (2, 1)),
        0,
        176,
        False,
        0,
    ),
    "pull_keep": (40, ((5, 1), (3, 0)), 2, ((5, 3), (3, 1)), 7, 48, True, 0),
    # The zero range from element 32, rows past the pulled one filled.
    "pull_zero_range": (40, ((4, 1), (2, 0)), 0, ((4, 2), (2, 1)), 40, 192, False, 32),
    # Pulled rows past whole zero-range rows (filled) and before others.
    "pull_rows_inside": (
        40,
        ((4, 1), (2, 0)),
        0,
        ((4, 2), (2, 1)),
        136,
        512,
        False,
        0,
    ),
    "transpose_odd": (
        37632,
        ((28, 1), (1344, 28)),
        0,
        ((28, 1344), (1344, 1)),
        0,
        37632,
        False,
        0,
    ),
    "transpose_batched": (
        12800,
        ((8, 1600), (16, 1), (100, 16)),
        0,
        ((8, 1600), (16, 100), (100, 1)),
        0,
        12800,
        False,
        0,
    ),
    "transpose_both_odd": (
        10080,
        ((120, 1), (84, 120)),
        0,
        ((120, 84), (84, 1)),
        0,
        10080,
        False,
        0,
    ),
    "transpose_words": (
        48000,
        ((400, 1), (120, 400)),
        0,
        ((400, 120), (120, 1)),
        0,
        48000,
        False,
        0,
    ),
    "transpose_stride2": (
        12800,
        ((2, 1), (6400, 2)),
        0,
        ((2, 6400), (6400, 1)),
        0,
        12800,
        False,
        0,
    ),
    "transpose_window_pad": (
        12800,
        ((8, 1600), (10, 10), (10, 1), (16, 100)),
        0,
        ((8, 5184), (10, 288), (10, 16), (16, 1)),
        1216,
        41472,
        False,
        0,
    ),
    "transpose_keep": (96, ((6, 1), (16, 6)), 0, ((6, 24), (16, 1)), 4, 160, True, 0),
    "spread": (
        150,
        ((2, 75), (3, 25), (5, 5), (5, 1)),
        0,
        ((2, 1200), (3, 400), (5, 80), (5, 16)),
        0,
        2400,
        False,
        0,
    ),
    "strided_gather": (300, ((100, 3),), 1, ((100, 1),), 0, 112, False, 0),
    "transpose_conv_bias": (
        600,
        ((2, 300), (10, 10), (10, 1), (3, 100)),
        0,
        ((2, 2304), (10, 192), (10, 16), (3, 1)),
        208,
        4608,
        False,
        0,
    ),
    "transpose_two_margins": (
        9408,
        ((8, 1176), (14, 14), (14, 1), (6, 196)),
        0,
        ((8, 3136), (14, 224), (14, 16), (6, 1)),
        0,
        25088,
        False,
        0,
    ),
    # A window's channels out of a staged copy with a margin: the margin
    # buffer leaves the exchange's buffers off a GT4 entry unless rounded.
    "transpose_window_channels": (
        2400,
        ((4, 600), (4, 150), (5, 5), (5, 1), (6, 25)),
        0,
        ((4, 1600), (4, 400), (5, 80), (5, 16), (6, 1)),
        0,
        6400,
        False,
        0,
    ),
    # A transpose into zero-padded rows of whole lines (the producers' rows
    # convention): the padding written with the rows, one walk out.
    "transpose_padded_rows": (
        2 * 37 * 32,
        ((2, 37 * 32), (32, 1), (37, 32)),
        0,
        ((2, 32 * 64), (32, 64), (37, 1)),
        0,
        2 * 32 * 64,
        False,
        0,
    ),
    "transpose_long_margin": (
        301056,
        ((28, 1), (10752, 28)),
        0,
        ((28, 10752), (10752, 1)),
        0,
        301056,
        False,
        0,
    ),
    "shuffle_batched_runs_out": (
        288,
        ((3, 96), (2, 1), (48, 2)),
        0,
        ((3, 100), (2, 50), (48, 1)),
        0,
        304,
        False,
        0,
    ),
    "words": (
        4096,
        ((16, 256), (8, 16), (16, 1)),
        0,
        ((16, 128), (8, 16), (16, 1)),
        0,
        2048,
        False,
        0,
    ),
    "words_broadcast": (64, ((4, 0), (64, 1)), 0, ((4, 64), (64, 1)), 0, 256, False, 0),
}


def bits(rng, n) -> np.ndarray:
    """Every kind of f16 bit pattern: normal, denormal, +-0, inf, NaN."""
    x = rng.integers(0, 1 << 16, n, dtype=np.uint16)
    x[: min(n, 6)] = [0x0000, 0x8000, 0x7C00, 0xFC00, 0x7E01, 0x0001][: min(n, 6)]
    return x


def want(src, case, before) -> np.ndarray:
    _, walk, at, dwalk, dat, m, keep, z0 = case
    out = before.copy()
    if not keep:
        out[z0:m] = 0
    out[LN.elements(dwalk, dat)] = src[LN.elements(walk, at)]
    return out


def run(case, hw=HW, seed=0) -> tuple:
    n, walk, at, dwalk, dat, m, keep, z0 = case
    rng = np.random.default_rng(seed)
    src = bits(rng, n)
    before = bits(rng, -(-m // 16) * 16)
    pl = R.plan(n, walk, at, dwalk, dat, m, keep, hw, z0)
    return R.execute(pl, src, before, hw)[:m], want(src, case, before)[:m], pl


@pytest.mark.parametrize("name", sorted(CASES))
def test_plan_is_exact(name):
    got, exp, pl = run(CASES[name])
    assert np.array_equal(got, exp), f"{name}: {np.flatnonzero(got != exp)[:8]}"
    assert pl.parts


@pytest.mark.parametrize(
    "name, form",
    [
        ("runs_repitch_misaligned", R.Runs),
        ("splat_bias", R.Splat),
        ("splat_pairs", R.Pull),
        ("splat_lane_copies", R.Pull),
        ("pull_upsample", R.Pull),
        ("pull_keep", R.Pull),
        ("pull_zero_range", R.Pull),
        ("pull_rows_inside", R.Pull),
        ("transpose_odd", R.Exchange),
        ("spread", R.Exchange),
        ("transpose_stride2", R.Shuffle),
        ("shuffle_batched_runs_out", R.Shuffle),
        ("words", R.Words),
    ],
)
def test_each_class_takes_its_form(name, form):
    _, _, pl = run(CASES[name])
    assert any(isinstance(p, form) for p in pl.parts)


def test_random_runs_and_keeps_are_exact():
    rng = np.random.default_rng(7)
    for _ in range(60):
        rows, L = int(rng.integers(1, 9)), int(rng.integers(2, 300))
        sp, dp = L + int(rng.integers(0, 40)), L + int(rng.integers(0, 40))
        at, dat = int(rng.integers(0, 40)), int(rng.integers(0, 40))
        keep, z0 = bool(rng.integers(0, 2)), 16 * int(rng.integers(0, 2))
        dat += z0
        n = at + (rows - 1) * sp + L
        m = -(-(dat + (rows - 1) * dp + L) // 16) * 16
        case = (n, ((rows, sp), (L, 1)), at, ((rows, dp), (L, 1)), dat, m, keep, z0)
        got, exp, _ = run(case, seed=int(rng.integers(1 << 30)))
        assert np.array_equal(got, exp), case


def test_random_transposes_are_exact():
    rng = np.random.default_rng(11)
    for _ in range(30):
        A, B, nb = (
            int(rng.integers(2, 70)),
            int(rng.integers(2, 70)),
            int(rng.integers(1, 4)),
        )
        n = nb * A * B
        case = (
            n,
            ((nb, A * B), (B, 1), (A, B)),
            0,
            ((nb, A * B), (B, A), (A, 1)),
            0,
            -(-n // 16) * 16,
            False,
            0,
        )
        got, exp, _ = run(case, seed=int(rng.integers(1 << 30)))
        assert np.array_equal(got, exp), case


def test_scatters_write_whole_lines_one_layer_a_line():
    """L1.VER.017: every scattered row is whole lines (its line list a
    line's index), and no two rows of one layer write one line."""
    for name, case in CASES.items():
        _, _, pl = run(case)
        for p in pl.parts:
            if not isinstance(p, R.Runs):
                continue
            for g in p.groups:
                assert g.words % HW.line == 0, name
                for lay in g.layers:
                    lines = pl.index[lay.at_d : lay.at_d + lay.rows].astype(np.int64)
                    spans = {
                        (int(q), k)
                        for q in set(lines.tolist())
                        for k in range(g.words // HW.line)
                    }
                    assert len({q + k for q, k in spans}) == len(spans), name


# ------------------------------------------------------------- mutants
def _caught(monkeypatch, attr, value) -> bool:
    """Whether some case's execution differs from numpy with `attr` of
    `kernels.relayout` replaced (plans re-made)."""
    monkeypatch.setattr(R, attr, value)
    monkeypatch.setattr(R, "_plans", type(R._plans)())
    for case in CASES.values():
        try:
            got, exp, _ = run(case)
        except (IndexError, ValueError):
            return True
        if not np.array_equal(got, exp):
            return True
    return False


def test_mutant_rotation_direction_is_caught(monkeypatch):
    orig = R.runs

    def runs(q, rot, *a, **k):
        return orig(q, (-np.asarray(rot)) % HW.lanes, *a, **k)

    assert _caught(monkeypatch, "runs", runs)


def test_mutant_one_layer_is_caught(monkeypatch):
    assert _caught(monkeypatch, "layers", lambda b, n, span: np.zeros(len(b), np.int64))


def test_mutant_no_fill_is_caught(monkeypatch):
    orig = R._done

    def done(parts, *a):
        return orig(
            tuple(p for p in parts if not (isinstance(p, R.Fill) and p.dst == "dst")),
            *a,
        )

    assert _caught(monkeypatch, "_done", done)


def test_mutant_splat_row_is_caught(monkeypatch):
    orig = R.runs

    def runs(q, rot, b, n, *, pitch, **k):
        if pitch > 1:
            q = np.asarray(q) + 1
        return orig(q, rot, b, n, pitch=pitch, **k)

    assert _caught(monkeypatch, "runs", runs)


def test_mutant_no_margin_is_caught(monkeypatch):
    orig = R.runs

    def runs(*a, **k):
        r = orig(*a, **k)
        return (
            R.Runs(r.src, r.src_words, r.pitch, 0, r.dst, r.groups) if r.margin else r
        )

    assert _caught(monkeypatch, "runs", runs)


def test_mutant_shuffle_phase_is_caught(monkeypatch):
    """Every output stream compressed as the first's (its lane phase b
    ignored)."""
    orig = R.compress
    assert _caught(monkeypatch, "compress", lambda B, b, lanes: orig(B, 0, lanes))


def test_mutant_exchange_sides_swapped_is_caught(monkeypatch):
    """The exchange told [nb, B', A'] for [nb, A', B']."""

    class Swapped(R.Exchange):
        def __init__(self, src, dst, nb, A, B, masks):
            super().__init__(src, dst, nb, B, A, masks)

    assert _caught(monkeypatch, "Exchange", Swapped)


def test_padded_rows_are_written_whole():
    """The transpose into zero-padded rows writes them whole: the exchange's
    rows one walk out, no fill of the destination, no runs."""
    _, _, pl = run(CASES["transpose_padded_rows"])
    kinds = [type(p).__name__ for p in pl.parts]
    assert "Runs" not in kinds and kinds[-1] == "Words", kinds
    assert not any(isinstance(p, R.Fill) and p.dst == "dst" for p in pl.parts)


def test_mutant_unfilled_exchange_padding_is_caught(monkeypatch):
    """The out side writing whole rows from an exchange whose padding was
    never filled: the rows' padding is poison, not zero."""
    orig = R._fills
    monkeypatch.setattr(
        R, "_fills", lambda dst, *a: [] if dst == "tx" else orig(dst, *a)
    )
    monkeypatch.setattr(R, "_plans", type(R._plans)())
    got, exp, _ = run(CASES["transpose_padded_rows"])
    assert not np.array_equal(got, exp)


def _pull_mutant(monkeypatch, change) -> None:
    """Every `Pull` part `_pull` plans replaced by `change(part)`."""
    orig = R._pull

    def pull(*a, **k):
        pl = orig(*a, **k)
        parts = tuple(change(p) if isinstance(p, R.Pull) else p for p in pl.parts)
        return replace(pl, parts=parts)

    monkeypatch.setattr(R, "_pull", pull)
    monkeypatch.setattr(R, "_plans", type(R._plans)())


def test_mutant_pull_rotation_reversed_is_caught(monkeypatch):
    assert _caught(monkeypatch, "pull_rotation", lambda s, lane, n: (lane - s) % n)


def test_mutant_pull_second_source_unstaged_is_caught(monkeypatch):
    """The second source word staged as the first: lanes past a straddle
    read the wrong word."""
    _pull_mutant(monkeypatch, lambda p: replace(p, slots=(p.slots[0],) * len(p.slots)))
    got, exp, pl = run(CASES["pull_upsample"])
    assert any(isinstance(p, R.Pull) and len(p.slots) > 1 for p in pl.parts)
    assert not np.array_equal(got, exp)


def test_mutant_pull_kept_lanes_zeroed_is_caught(monkeypatch):
    _pull_mutant(monkeypatch, lambda p: replace(p, base="zero", zeros=False))
    got, exp, _ = run(CASES["pull_keep"])
    assert not np.array_equal(got, exp)


def test_word_levels_finds_walks_and_refuses_the_rest():
    assert R.word_levels([0, 0, 1, 1, 2, 2], 6) == (0, ((3, 1), (2, 0)))
    assert R.word_levels([5, 6, 7, 9, 10, 11], 6) == (5, ((2, 4), (3, 1)))
    assert R.word_levels([3], 6) == (3, ((1, 0),))
    assert R.word_levels([0, 1, 3], 6) is None
    assert R.word_levels(list(range(64)) * 1, 1) == (0, ((64, 1),))
    deep = R.positions(tuple((2, 3**k, 0) for k in range(7)), 0, 1)
    assert R.word_levels(deep, 6) is None and R.word_levels(deep, 7) is not None


def test_compress_moves_each_lane_to_its_place():
    for B in (2, 4, 8):
        for b in range(B):
            lane = np.arange(16)
            for k, m in R.compress(B, b, 16):
                lane = np.where(m, np.roll(lane, -k), lane)
            assert list(lane[: 16 // B]) == [j * B + b for j in range(16 // B)]


def test_mutant_unstaged_copy_rows_are_caught(monkeypatch):
    """Copy rows scattered from the wrong staging (the processed rows')."""
    orig = R.runs

    def runs(*a, **k):
        r = orig(*a, **k)
        groups = []
        for g in r.groups:
            layers = []
            for lay in g.layers:
                cl = tuple(
                    R.Cls(
                        c.rot,
                        c.rmask,
                        c.mask if c.mask >= 0 or c.rot else 0,
                        c.row0,
                        c.rows,
                    )
                    for c in lay.classes
                )
                layers.append(
                    R.Layer(lay.k, lay.base, lay.row0, lay.rows, lay.at_d, cl)
                )
            groups.append(
                R.Group(g.words, g.row0, g.rows, g.at_s, g.at_s2, tuple(layers))
            )
        return R.Runs(r.src, r.src_words, r.pitch, r.margin, r.dst, tuple(groups))

    assert _caught(monkeypatch, "runs", runs)


# ----------------------------------------------------- compiled kernels
@pytest.mark.parametrize(
    "name",
    [
        "runs_compact_shared_lines",
        "splat_bias",
        "splat_pairs",
        "splat_lane_copies",
        "pull_upsample",
        "pull_keep",
        "pull_zero_range",
        "transpose_odd",
        "spread",
        "words",
        "transpose_stride2",
        "shuffle_batched_runs_out",
        "transpose_two_margins",
        "transpose_long_margin",
        "transpose_conv_bias",
        "transpose_window_channels",
    ],
)
def test_kernel_compiles_and_passes_the_checker(name):
    """Offline: compiled, encoded and accepted by the static checker (a
    refusal raises); the plan's tables bound."""
    compile_case(name)


def compile_case(name):
    """The case's kernel compiled offline through `forms.relayout`; its
    `Compiled`."""
    dev = D.Device(kind="offline", board=BOARD)
    n, walk, at, dwalk, dat, m, keep, z0 = CASES[name]
    s, d = Storage(dev, max(32, 2 * n)), Storage(dev, 2 * (-(-m // 32) * 32) + 64)
    got = []
    launch = FM.launch

    def keep_compiled(*a, **k):
        run = launch(*a, **k)
        got.append(run.info["compiled"])
        return run

    FM.launch = keep_compiled
    try:
        FM.relayout(
            dev, s.addr, n, walk, at, dwalk, m - z0, d.addr + 2 * z0, dat - z0, keep
        )
    finally:
        FM.launch = launch
    assert dev.counters["runs"] == 1
    return got[0]


GRAIN_CASES = ["transpose_two_margins", "transpose_window_channels", "spread"]


def misplaced(name) -> list:
    """The scratch buffers of the case's kernel starting off `HW.grain`
    bytes into its scratch arena (the arena itself starts on the runtime's
    allocation grain)."""
    out = []
    for arena in compile_case(name).scratch or ():
        out += [(p, off) for p, off in arena["members"] if off % HW.grain]
    return out


@pytest.mark.parametrize("name", GRAIN_CASES)
def test_scratch_buffers_start_on_the_grain(name):
    assert misplaced(name) == []


def test_mutant_unrounded_scratch_is_caught(monkeypatch):
    """Scratch buffers at their plan sizes: one starts off the grain."""
    monkeypatch.setattr(R, "scratch_rows", lambda rows, width, hw: rows)
    monkeypatch.setattr(R, "_kernels", type(R._kernels)())
    assert any(misplaced(name) for name in GRAIN_CASES)


@pytest.mark.parametrize(
    "name, patches",
    [
        # Both runs parts' staged copies one word into one buffer: two
        # moves write part of its first line.
        (
            "transpose_conv_bias",
            {"margin_name": lambda r: "rm", "margin_words": lambda need, hw: need},
        ),
        # A staged copy one word in: its moves, cut at 16384 words, each
        # write part of the line at a cut.
        ("transpose_long_margin", {"margin_words": lambda need, hw: need}),
    ],
    ids=["shared-staging", "unaligned-staging"],
)
def test_mutant_line_sharing_is_refused(monkeypatch, name, patches):
    """L1.VER.017: the static checker refuses the mutant's kernel."""
    for attr, value in patches.items():
        monkeypatch.setattr(R, attr, value)
    monkeypatch.setattr(R, "_plans", type(R._plans)())
    monkeypatch.setattr(R, "_kernels", type(R._kernels)())
    with pytest.raises(Refused):
        test_kernel_compiles_and_passes_the_checker(name)


# ---------------------------------------------------------- card model
MODEL = [
    "runs_compact_shared_lines",
    "runs_offset_keep",
    "runs_buffer_ends",
    "splat_bias",
    "splat_offset",
    "splat_pairs",
    "pull_upsample",
    "pull_keep",
    "pull_zero_range",
    "pull_rows_inside",
    "transpose_both_odd",
    "transpose_keep",
    "spread",
    "words_broadcast",
    "shuffle_batched_runs_out",
    "transpose_two_margins",
    "transpose_window_channels",
    "transpose_padded_rows",
]


@pytest.mark.parametrize("name", MODEL)
def test_relayout_on_the_card_is_exact(dev, name):
    """The production path on the card (model): bit-exact against numpy,
    every kind of f16 bit pattern but NaN payloads (the vector core keeps
    NaNs NaN, not their payload) and -0, which an element through any
    vector-core math (select, rotation, crossbar) comes back as +0
    (measured on the card model over rows through unpack -> pack,
    `where(mask, v, w)`, `xrot`, `xlane`, `v * 1.0` and `v + 0.0`: only
    unpack -> pack keeps it)."""
    n, walk, at, dwalk, dat, m, keep, z0 = CASES[name]
    rng = np.random.default_rng(3)
    src = bits(rng, n)
    src[np.isnan(src.view(np.float16))] = 0x3C00
    before = bits(rng, -(-m // 16) * 16)
    before[np.isnan(before.view(np.float16))] = 0x4000
    s = Storage(dev, max(32, 2 * n))
    d = Storage(dev, 2 * len(before) + 64)
    try:
        dev.write(s.addr, src.tobytes())
        dev.write(d.addr, before.tobytes())
        FM.relayout(
            dev, s.addr, n, walk, at, dwalk, m - z0, d.addr + 2 * z0, dat - z0, keep
        )
        dev.drain()
        got = np.frombuffer(dev.read(d.addr, 2 * m), np.uint16)
    finally:
        s.free()
        d.free()
    exp = want(src, CASES[name], before)[:m]
    ok = (got == exp) | ((exp == NEG_ZERO) & (got == 0))
    assert ok.all(), f"{name}: {np.flatnonzero(~ok)[:8]}"
