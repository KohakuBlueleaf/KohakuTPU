"""Fused rows members reading one composed map: two updates of [R, W] states
that read the same transposed gradient run as one package whose bands load
the gradient once for both, with the bytes the two launches write."""

import json
import pathlib
import re
import subprocess

import numpy as np
import pytest
from tinygrad.tensor import Tensor

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels.classes.base import Operand
from kohakutpu.application.ktpugrad.launch import emit
from kohakutpu.application.ktpugrad.launch.config import knobs
from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime import generated
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.language import machine as MF
from kohakutpu.stack import kernel

RNG = np.random.default_rng(5)
#: The MNIST MLP's first weight: [128, 784] states, its gradient a [784, 128]
#: product read transposed.
STATE = (128, 784)
#: One band of rows and two rows past it, several column blocks: fewer row
#: trips than cores.
SMALL = (6, 1024)
SOFTWARE = pathlib.Path(__file__).resolve().parents[2]
PERF = SOFTWARE / "tests" / "perf"


def tensor(x):
    return Tensor(x.astype(np.float16), device="KTPU").realize()


def _spied(monkeypatch) -> list:
    calls = []
    launch = generated.launch

    def spied(dev, generator, arguments, binds, info=None):
        calls.append((generator, arguments))
        return launch(dev, generator, arguments, binds, info=info)

    monkeypatch.setattr(generated, "launch", spied)
    return calls


def _moments(g0, m0, r0):
    """Momentum and root-mean-square updates of Adam16's shape, each in place,
    both reading the gradient transposed."""
    g, m, r = tensor(g0), tensor(m0), tensor(r0)

    def step():
        gt = g.T
        m.assign(m * 0.9 + gt * 0.1)
        r.assign((r * r * 0.95 + gt * gt * 0.05).sqrt())
        Tensor.realize(m, r)

    return m, r, step


def _mapped_loads(dev, call) -> int:
    return generated.source(dev, *call).count('"read_map"')


def _apart_and_fused(monkeypatch, shape) -> tuple:
    """The moments over `shape` states with the fused mapped reads off, then on:
    each run's launches, and (got, want, before) of each state."""
    monkeypatch.setattr(FM, "COMPOSED", "on")
    g0 = RNG.normal(size=shape[::-1]) * 0.05
    m0 = RNG.normal(size=shape) * 0.01
    r0 = np.abs(RNG.normal(size=shape)) * 0.05

    calls = _spied(monkeypatch)
    with knobs(fuse_mapped=False):
        m_ref, r_ref, step = _moments(g0, m0, r0)
        step()
    apart = list(calls)
    calls.clear()
    m, r, step = _moments(g0, m0, r0)
    step()
    states = ((m, m_ref, m0), (r, r_ref, r0))
    return apart, list(calls), states


def _same_bytes(states) -> None:
    for got, want, before in states:
        a, b = got.numpy(), want.numpy()
        assert np.isfinite(a.astype(np.float32)).all()
        assert a.tobytes() == b.tobytes()
        assert a.tobytes() != before.astype(np.float16).tobytes()


def test_two_updates_reading_one_map_share_its_load(dev, monkeypatch):
    apart, fused, states = _apart_and_fused(monkeypatch, STATE)

    assert [g for g, _ in apart] == ["prepared.rows_kernel"] * 2
    assert [g for g, _ in fused] == ["fused.kernel"]
    (rows,) = fused[0][1]["steps"]
    assert (rows["kind"], len(rows["exprs"]), len(rows["made"])) == ("rows", 2, 1)
    # The domain tiles as [128, 784] rows of whole source words: each of the
    # 24 blocks of 32 columns and the 16-column tail loads the gradient once
    # for both members.
    blocks = STATE[1] // 32
    loads = [_mapped_loads(dev, c) for c in (*apart, *fused)]
    assert loads == [blocks + 1, blocks + 1, blocks + 1]
    _same_bytes(states)


@pytest.fixture(scope="module")
def timed():
    cmd = ["cargo", "build", "-q", "--profile", "stage", "-p", "ktpu-perf"]
    subprocess.run(cmd + ["--bin", "timed"], cwd=SOFTWARE, check=True)
    return SOFTWARE / "target" / "stage" / "timed"


def _timed(timed, dev, calls, folder) -> list:
    """The timed model's cycles of each call's kernel (`tests/perf/model`)."""
    folder.mkdir()
    names = []
    for call in calls:
        l2 = dev.kernel(generated.source(dev, *call))
        l1 = kernel.compile(l2.l2_text, l2.name, machine_of(dev.machine.board)).l1_text
        (folder / f"{l2.name}.l1").write_text(l1, "utf-8")
        names.append(l2.name)
    out = folder / "timed.jsonl"
    machine = SOFTWARE.parent / "machines" / f"{dev.machine.board}.json"
    cmd = [str(timed), "--machine", str(machine), "--params", str(PERF / "params.json")]
    subprocess.run([*cmd, "--out", str(out), str(folder)], check=True)
    rows = [json.loads(x) for x in out.read_text("utf-8").splitlines()]
    cycles = {r["name"]: r["cycles"] for r in rows}
    return [cycles[n] for n in names]


def test_a_small_r_package_spreads_over_the_cores(dev, monkeypatch, timed, tmp_path):
    """Two moments of [10, 128] states (one band of rows, four column blocks):
    the package deals its blocks over every core, as each launch it replaces
    does, and the timed model prices it no dearer than those launches."""
    apart, fused, states = _apart_and_fused(monkeypatch, SMALL)

    assert [g for g, _ in apart] == ["prepared.rows_kernel"] * 2
    assert [g for g, _ in fused] == ["fused.kernel"]
    l2 = dev.kernel(generated.source(dev, *fused[0])).l2_text
    par = re.findall(r"^\s*par \w+ in 0\.\.(\d+)", l2, re.MULTILINE)
    regions = [int(n) for n in par]
    cores = MF.load(dev.machine.board).vc.count
    assert regions and set(regions) == {cores}, regions
    (package,) = _timed(timed, dev, fused, tmp_path / "fused")
    parts = _timed(timed, dev, apart, tmp_path / "apart")
    assert package <= sum(parts), (package, parts)
    _same_bytes(states)


def _made_member(src: int, dst: int, state: int) -> dict:
    """`out = in0 + in1` over [1, 256]: in0 a state, in1 a source read
    through a transposing map."""
    col = Operand("col")
    steps = [
        {
            "made_by": {
                "gen": "relayout.plan",
                "args": {
                    "walk": [[16, 1], [16, 16]],
                    "at": 0,
                    "dwalk": [[16, 16], [16, 1]],
                    "dat": 0,
                    "m": 256,
                    "src_n": 256,
                    "keep": False,
                    "z0": 0,
                    "tree": True,
                },
            }
        }
    ]
    return {
        "kind": "rows",
        "R": 1,
        "W": 256,
        "exprs": [X.Op("add", (X.Load(0), X.Load(1)))],
        "ops": [col, col],
        "out_ops": [Operand("full", pitch=256)],
        "ins": [state, src],
        "outs": [dst],
        "made": {1: steps},
    }


@pytest.mark.parametrize(("dst", "loops"), [(0x30000, 1), (0x10040, 2)])
def test_a_member_writing_inside_the_shared_source_keeps_its_own_stage(dev, dst, loops):
    """The second member writing the source the first reads composed, 64 bytes
    on (a shifted in-place write), keeps its own stage; writing elsewhere it
    joins."""
    first = _made_member(0x10000, 0x20000, 0x40000)
    second = _made_member(0x10000, dst, 0x50000)
    stages = [first, second]
    assert len(emit._merge(dev, stages, emit._Loop(dev, stages, set(), 8))) == loops
