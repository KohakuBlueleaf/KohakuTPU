"""An absolute instruction FETCH observes execution memory, not package bytes."""

import json

import pytest

from kohakuaccel.simulation.package import Interpreter
from kohakutpu.compiler import _rs
from kohakutpu.simulation.package import Reader
from kohakutpu.stack import rs

ADDRESS = 0x100000


class FetchWorld:
    """Mailbox completion double with explicitly supplied execution-time memory."""

    def __init__(self, stream):
        self.stream = stream
        self.sent = []
        self.pending = []
        self.reads = []

    def send(self, x, y, payload):
        self.sent.append(payload)
        self.pending.append((x, y, 0, 0))

    def drain(self):
        pending, self.pending = self.pending, []
        return pending

    def memory(self, address, count):
        self.reads.append((address, count))
        assert (address, count) == (ADDRESS, 32)
        return self.stream.to_bytes(32, "little")


def fixture():
    machine = rs.machine_json("vlt_v9t6s_1n")
    facts = json.loads(machine)["machine"]
    x, y = facts["die"]["units"]["vc"][0]
    fetch = facts["die"]["fetch_port"]
    raw = _rs.package_build(
        json.dumps({"types": [[x, y, "VC"]], "fetch": fetch, "credit": 16}),
        json.dumps([["unit", [x, y]], ["fetch_from", 0, ADDRESS, 1], ["barrier"]]),
        machine,
    )
    shift = facts["die"]["noc"]["data_bits"] - 4
    return machine, raw, (1 << shift) | 123, (1 << shift) | 456


def execute(raw, machine, stream):
    world = FetchWorld(stream)
    result = Interpreter(world, reader=Reader(machine), memory=world.memory).run(raw)
    assert result.status == 0
    assert world.reads == [(ADDRESS, 32)]
    return world.sent


def witness(use_stale_snapshot=False):
    machine, raw, first, second = fixture()
    table = _rs.package_read(raw, machine)
    assert table["payloads"] == []
    assert all(op != 18 for op, *_ in table["steps"]), "no PATCH exists"
    one = _rs.bind(raw, [], machine, cores=_rs.Cores(machine))
    two = _rs.bind(raw, [], machine, cores=_rs.Cores(machine))
    assert one == two
    a = execute(one, machine, first)
    b = execute(two, machine, first if use_stale_snapshot else second)
    assert a == [first]
    assert b == [
        second
    ], "execution-time instruction bytes differ from the host snapshot"
    assert a != b


def invalidation_witness(mutant=None):
    machine, raw, first, second = fixture()
    coord = tuple(json.loads(machine)["machine"]["die"]["units"]["vc"][0])
    shift = json.loads(machine)["machine"]["die"]["noc"]["data_bits"] - 4
    cores = _rs.Cores(machine)
    run = 3 << shift
    cores.rewrite(coord, [first, run])
    known = cores.snapshot()
    _rs.bind(raw, [], machine, cores=cores, mutant=mutant)
    assert execute(raw, machine, second) == [second]
    repaired = cores.rewrite(coord, [first, run])
    assert (
        first in repaired
    ), "external FETCH must invalidate the overwritten prediction"
    assert first not in known.rewrite(coord, [first, run])


def test_external_fetch_invalidates_and_predicting_it_is_killed():
    invalidation_witness()
    with pytest.raises(AssertionError, match="must invalidate"):
        invalidation_witness("imr/predict-external")


def test_identical_bind_inputs_can_install_different_instruction_bytes():
    witness()


def test_submit_time_snapshot_mutant_misses_a_queued_producer():
    with pytest.raises(AssertionError, match="execution-time instruction bytes"):
        witness(use_stale_snapshot=True)
