"""Bound-image residency, submission prediction and mutation witnesses."""

import json
from types import SimpleNamespace

import pytest

from kohakutpu.application.runtime import packages as B
from kohakutpu.application.runtime.card import NodeCard
from kohakutpu.compiler import _rs
from kohakutpu.stack import rs


@pytest.fixture
def machine():
    return rs.machine_json("vlt_v9t6s_1n")


def program(machine, value=1):
    source = (
        "target ktpu.v9\nimage image()\n"
        f"    seti s0 = {value}\n    halt\n"
        "fn k.l1()\n    send vc[0]\n        run image()\n    barrier\n"
    )
    compiled = _rs.compile(source, machine, "k", l1=True, fetch=True, cache_dir="")
    raw = compiled["packages"][0]["bytes"]
    return B.Prebuilt(raw, (), machine, 0, (), False, rs.fetch(machine))


def words(raw, machine):
    tables = _rs.package_read(raw, machine)
    return [
        word
        for op, _, _, count, arg in tables["steps"]
        if op == 1
        for word in tables["payloads"][arg : arg + count]
    ]


def installs(raw, machine):
    shift = json.loads(machine)["machine"]["die"]["noc"]["data_bits"] - 4
    return [word for word in words(raw, machine) if word >> shift == 1]


def test_second_bind_omits_image_and_restore_reinstalls(machine):
    p, cores = program(machine), _rs.Cores(machine)
    before = cores.snapshot()
    first = p.bound(False, cores=cores)
    assert installs(first, machine)
    assert not installs(p.bound(False, cores=cores), machine)
    cores.restore(before)
    assert installs(p.bound(False, cores=cores), machine) == installs(first, machine)


def rebound_witness(machine, mutant=None):
    p, other, cores = program(machine, 1), program(machine, 2), _rs.Cores(machine)
    p.bound(False, cores=cores)
    raw = _rs.bind(other.package, [], machine, cores=cores, mutant=mutant)
    unit = next(
        u for u in _rs.package_read(raw, machine)["units"] if u["type"] == 0x5643
    )
    state = cores.state((unit["x"], unit["y"]))
    memory = {addr: int(word, 16) for addr, word in state["mem"]}
    expected = [w & ((1 << 40) - 1) for w in installs(other.package, machine)]
    actual = [memory[state["last"] + i] for i in range(len(expected))]
    assert actual == expected, "bound bytes selected for RUN are stale"
    assert installs(raw, machine), "different bound bytes must install"


def test_key_is_bound_bytes(machine):
    rebound_witness(machine)
    with pytest.raises(AssertionError, match="bound bytes"):
        rebound_witness(machine, "imr/key-identity")


def test_template_hit_binds_without_compilation(machine, monkeypatch):
    p, cores = program(machine), _rs.Cores(machine)
    tables = _rs.package_read(p.package, machine)
    original, other = next(
        (a, b)
        for a, b in zip(
            installs(p.package, machine),
            installs(program(machine, 2).package, machine),
            strict=True,
        )
        if a != b
    )
    payload = tables["payloads"].index(original)
    table = json.dumps(
        {
            "parameters": {"word": {"lo": 0, "hi": (1 << 32) - 1}},
            "sites": [
                {
                    "name": "bound-image",
                    "payload": payload,
                    "bit": 0,
                    "width": 32,
                    "coefficients": {"word": 1},
                }
            ],
        }
    )
    monkeypatch.setattr(_rs, "compile", lambda *a, **kw: pytest.fail("compiled on hit"))
    for value, expected in [
        (original & 0xFFFFFFFF, True),
        (original & 0xFFFFFFFF, False),
        (other & 0xFFFFFFFF, True),
    ]:
        raw = _rs.bind(
            p.package,
            [],
            machine,
            cores=cores,
            template_json=table,
            params={"word": value},
        )
        assert bool(installs(raw, machine)) == expected


def fake_card(machine):
    c = object.__new__(NodeCard)
    c.nodes, c.debug, c.log = (0,), False, None
    c.residents = {0: _rs.Cores(machine)}
    c.machine_json = machine
    c.machine, c.fetch = SimpleNamespace(machine_json=machine), rs.fetch(machine)
    c.verify = lambda progs: None
    c.submitted = []
    c._submit = lambda n, raw, bindings, **kw: c.submitted.append(raw) or len(
        c.submitted
    )
    return c


def queued_witness(machine, monkeypatch, advance=True):
    monkeypatch.setenv("KOHAKUTPU_RESIDENT", "1")
    c, p = fake_card(machine), program(machine)
    before = c.residents[0].snapshot()
    first = c.submit(p)
    if not advance:
        c.residents[0].restore(before)
    second = c.submit(p)
    assert first.result is second.result is None
    assert len(c.submitted[1]) < len(
        c.submitted[0]
    ), "state must advance before collect"


def test_two_queued_submissions(machine, monkeypatch):
    queued_witness(machine, monkeypatch)
    with pytest.raises(AssertionError, match="before collect"):
        queued_witness(machine, monkeypatch, advance=False)


def failure_witness(machine, monkeypatch, invalidate=True):
    monkeypatch.setenv("KOHAKUTPU_RESIDENT", "1")
    c, p = fake_card(machine), program(machine)
    pending = c.submit(p)
    if not invalidate:
        c._forget_residents = lambda nodes: None

    def fail(*args):
        raise TimeoutError("failed run")

    c._collect = fail
    with pytest.raises(TimeoutError):
        c.collect(pending)
    assert installs(
        p.bound(False, cores=c.residents[0]), machine
    ), "failure must invalidate"


def test_failed_run_invalidates(machine, monkeypatch):
    failure_witness(machine, monkeypatch)
    with pytest.raises(AssertionError, match="invalidate"):
        failure_witness(machine, monkeypatch, invalidate=False)


def test_off_sends_every_time(machine, monkeypatch):
    monkeypatch.setenv("KOHAKUTPU_RESIDENT", "0")
    c, p = fake_card(machine), program(machine)
    c.submit(p)
    c.submit(p)
    assert c.submitted[0] == c.submitted[1]


def test_submit_failure_does_not_publish_prediction(machine, monkeypatch):
    monkeypatch.setenv("KOHAKUTPU_RESIDENT", "1")
    c, p = fake_card(machine), program(machine)

    def fail(*args, **kwargs):
        raise TimeoutError("submit failed")

    c._submit = fail
    with pytest.raises(TimeoutError):
        c.submit(p)
    assert installs(p.bound(False, cores=c.residents[0]), machine)


def eviction_witness(machine, mutant=None):
    m = json.loads(machine)
    m["machine"]["vc"].update(imem_words=8, run_reach=8)
    small = json.dumps(m)
    cores = _rs.Cores(small)
    ps = [program(small, n) for n in (1, 2, 3)]

    def bind(p):
        return _rs.bind(p.package, [], small, cores=cores, mutant=mutant)

    assert installs(bind(ps[0]), small)
    assert installs(bind(ps[1]), small)
    assert not installs(bind(ps[0]), small)
    assert installs(bind(ps[2]), small)
    assert not installs(bind(ps[0]), small), "recent hit must survive eviction"
    assert installs(bind(ps[1]), small)


def test_record_starts_self_contained_and_raw_replay_invalidates(machine, monkeypatch):
    monkeypatch.setenv("KOHAKUTPU_RESIDENT", "1")
    c, p = fake_card(machine), program(machine)
    c.submit(p)
    assert not installs(p.bound(False, cores=c.residents[0].snapshot()), machine)
    c.record()
    assert installs(p.bound(False, cores=c.residents[0].snapshot()), machine)
    c.submit(p)
    log = c.recorded()
    c.submit_recorded(log[0][1])
    assert installs(p.bound(False, cores=c.residents[0].snapshot()), machine)


def test_bad_machine_and_failed_bind_preserve_state(machine):
    p, cores = program(machine), _rs.Cores(machine)
    p.bound(False, cores=cores)
    before = cores.snapshot()
    with pytest.raises(ValueError):
        _rs.bind(p.package, [], machine, after=999999, cores=cores)
    assert p.bound(False, cores=cores) == p.bound(False, cores=before)
    other = json.loads(machine)
    other["machine"]["vc"].update(imem_words=8, run_reach=8)
    with pytest.raises(ValueError, match="another vector machine"):
        _rs.bind(p.package, [], json.dumps(other), cores=cores)


def test_eviction_and_reinstall(machine):
    eviction_witness(machine)
    with pytest.raises(AssertionError, match="survive eviction"):
        eviction_witness(machine, "l1.lower/hit-evicted-first")
