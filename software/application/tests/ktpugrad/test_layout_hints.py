"""Producers write the layout their consumers read (`forms.hints`).

An MXFP7 packing that pads a producer's rows to whole words records a hint
for that producer site (here the A side's producer, and the B side's
transpose of the weight). The next time the producer runs it writes its rows
zero-padded to whole lines, and the packing reads them in place. Two
producers are covered: a bare gather (a transpose made contiguous) and a
rows kernel (a softmax), whose padding lanes are then stored as zeros.

Offline, launch counts: the second run makes fewer relayouts and the third
as many as the second; with hints off all make the same. On the card model, values: the second
run's product equals numpy. The mutant that claims zero padding without
storing it is caught there.
"""

import numpy as np
import pytest
from tinygrad.tensor import Tensor

from kohakutpu.application.ktpugrad import run as RN
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import forms as FM

RNG = np.random.default_rng(9)
V = RNG.standard_normal((37, 48)).astype(np.float16)
X37 = RNG.standard_normal((48, 37)).astype(np.float16)
WK = (RNG.standard_normal((37, 16)) / 4).astype(np.float16)
X300 = (X37.astype(np.float32) + 300).astype(np.float16)


def T(a):
    return Tensor(a, device="KTPU")


def f(a):
    return np.asarray(a, np.float64)


def softmax(a):
    e = np.exp(a - a.max(-1, keepdims=True))
    return e / e.sum(-1, keepdims=True)


CASES = {
    # A bare gather's rows of 37 the A side of a product (K = 37).
    "transposed": (
        lambda: T(V).T.contiguous().realize() @ T(WK),
        lambda: f(V).T @ f(WK),
    ),
    # A rows kernel's rows of 37 the A side of a product.
    "softmax": (
        lambda: (T(X37) * 2).softmax(-1).realize() @ T(WK),
        lambda: softmax(f(X37) * 2) @ f(WK),
    ),
    # The same of a rows kernel whose padding lanes as computed are large
    # (minus the row's max, about -300, where its input's are 0) beside
    # values near 0: a K block's shared MXFP7 exponent then loses them.
    "shifted": (
        lambda: (T(X300) - T(X300).max(-1, keepdim=True)).realize() @ T(WK),
        lambda: (f(X300) - f(X300).max(-1, keepdims=True)) @ f(WK),
    ),
}
#: The producer kind each case's A side is hinted as.
PRODUCER = {"transposed": "gather", "softmax": "rows", "shifted": "rows"}


def relayouts(monkeypatch, make, runs=2, hinted=None, library=None) -> list:
    """Relayouts each of `runs` runs of `make` makes, on one offline device
    (of `library`, its hints file the device's first hints; `hinted`: a dict
    given the device's hints after)."""
    seen = []
    relayout = FM.relayout

    def counted(*a, **k):
        seen[-1] += 1
        return relayout(*a, **k)

    monkeypatch.setattr(FM, "relayout", counted)
    # The relayouts as their own kernels (not inside the packing's), so
    # each is counted.
    monkeypatch.setattr(FM, "PREPARED", False)
    monkeypatch.setattr(FM, "COMPOSED", "off")
    dev = D.Device(kind="offline", board="multimesh_v9t6", library=library)
    with D.attached(dev):
        for _ in range(runs):
            seen.append(0)
            make().realize()
            dev.drain()
        if hinted is not None:
            hinted.update(FM.hints(dev))
    return seen


@pytest.mark.parametrize("name", sorted(CASES))
def test_second_run_reads_the_producers_rows(monkeypatch, name):
    hinted: dict = {}
    first, second, third = relayouts(monkeypatch, CASES[name][0], 3, hinted)
    # Each packed side's padding relayout gone (the weight's transpose for
    # the B side is one too), and the choice settled.
    assert second < first and third == second, (first, second, third)
    # Sites by their text: "('gather', ...", "('rows', ...".
    kinds = {site.split("'")[1] for site, hint in hinted.items() if hint == "rows"}
    assert PRODUCER[name] in kinds, hinted


@pytest.mark.parametrize("name", sorted(CASES))
def test_library_hints_make_the_first_run_steady(monkeypatch, tmp_path, name):
    """A run's hints saved with a library (`forms.save_hints`, what the
    prebuild does): a device of that library makes the steady run's
    relayouts from its first run on."""
    hinted: dict = {}
    _, steady = relayouts(monkeypatch, CASES[name][0], 2, hinted)
    FM.save_hints(hinted, tmp_path)
    first, second = relayouts(monkeypatch, CASES[name][0], 2, library=tmp_path)
    assert first == second == steady, (first, second, steady)


def test_mutant_library_hints_unread_is_caught(monkeypatch, tmp_path):
    """The hints file not read: the first run lays out as an unhinted one."""
    hinted: dict = {}
    _, steady = relayouts(monkeypatch, CASES["transposed"][0], 2, hinted)
    FM.save_hints(hinted, tmp_path)
    monkeypatch.setattr(FM, "library_hints", lambda library: {})
    first, _ = relayouts(monkeypatch, CASES["transposed"][0], 2, library=tmp_path)
    assert first > steady


def test_save_hints_keeps_a_flat_site_flat(tmp_path):
    """Two captures into one library: a site either hints "flat" is flat."""
    FM.save_hints({"a": "rows", "b": "flat", "c": "rows"}, tmp_path)
    FM.save_hints({"a": "flat", "b": "rows", "d": "rows"}, tmp_path)
    assert FM.library_hints(tmp_path) == {
        "a": "flat",
        "b": "flat",
        "c": "rows",
        "d": "rows",
    }


@pytest.mark.parametrize("name", sorted(CASES))
def test_mutant_hints_off_is_caught(monkeypatch, name):
    """With hints off every producer writes row-major bytes: no fewer."""
    monkeypatch.setattr(FM, "LAYOUT_HINTS", False)
    first, second = relayouts(monkeypatch, CASES[name][0])
    assert second == first


@pytest.mark.parametrize("name", sorted(CASES))
def test_mutant_padding_not_known_zero_is_caught(monkeypatch, name):
    """The packing not trusting the rows' zero padding: it pads them again,
    so the second run makes as many relayouts as the first."""
    held = FM._held_rows

    def unzeroed(*a):
        got = held(*a)
        return None if got is None else (*got[:3], False)

    monkeypatch.setattr(FM, "_held_rows", unzeroed)
    first, second, _ = relayouts(monkeypatch, CASES[name][0], runs=3)
    assert second >= first


@pytest.mark.parametrize("name", sorted(CASES))
def test_hinted_runs_on_the_card(dev, rel, name):
    """Both runs on the card (model) against float64 numpy (MXFP7 product:
    the attention ruling's 0.03); the second reads the producer's rows."""
    make, want = CASES[name]
    for _ in range(2):
        got = make().numpy()
        assert rel(got, want()) < 0.03


def test_mutant_unzeroed_padding_is_caught_on_the_card(dev, rel, monkeypatch):
    """The elementwise kernel's padding lanes left as computed while claimed
    zero: the packing reads them as K columns (B's padding rows are zero, so
    they add nothing), but they set their K block's shared exponent and the
    values beside them are lost. The card device outlives a test, so the
    site may be hinted from the first run on; the second always is."""
    monkeypatch.setattr(RN, "_zero_lanes", lambda expr, k: expr)
    make, want = CASES["shifted"]
    errs = [rel(make().numpy(), want()) for _ in range(2)]
    assert errs[-1] >= 0.03, errs
