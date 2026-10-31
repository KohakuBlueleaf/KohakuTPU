"""Launch recipes: relocation, admission, keys and the epoch that revokes them."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kohakutpu.application.ktpugrad.captured import recipe as R
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import packages as B
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.application.runtime.memory.storage import FLAT, Form, Storage
from kohakutpu.stack.rs import _rs

BOARD = "multimesh_v9t6"


def mover_package(machine, bit=68, width=40, shift=0):
    transform = json.loads(machine)["machine"]["mover"]["modes"]["transform"]
    calls = [
        ["spans_from", [[0, 32]]],
        [
            "mover",
            [[0x10, 0], [0, (1 << 16) | transform]],
            {"addresses": [[[bit, width, shift]]]},
        ],
        ["end"],
    ]
    return _rs.package_build('{"credit":32}', json.dumps(calls), machine)


def test_a_recipe_relocates_as_bind_does():
    machine = machine_of(BOARD, nodes=1).machine_json
    raw = mover_package(machine)
    recipe = _rs.LaunchRecipe(raw, machine)
    for address in (0, 64, 4096):
        expected = _rs.bind(raw, [address], machine, lower=True)
        assert recipe.launch([0x100000000], [(0x100000000, address, 32)]) == expected
    assert recipe.admission_invariant
    with pytest.raises(ValueError, match="extent"):
        recipe.launch([0x100000000], [(0x100000000, 4096, 16)])


def test_a_wire_recipe_patches_and_refuses_overflow():
    machine = machine_of(BOARD, nodes=1).machine_json
    raw = mover_package(machine)
    recipe = _rs.LaunchRecipe(raw, machine, wire=True)
    for address in (0, 64, (1 << 39) + 0x100):
        assert recipe.launch([address]) == _rs.bind(raw, [address], machine, lower=True)
    assert recipe.wire_ready
    with pytest.raises(ValueError, match="RUL.039"):
        recipe.launch([1 << 40])


@pytest.mark.parametrize("wire", [False, True])
def test_a_patch_to_an_inspected_word_is_rechecked(wire):
    machine = machine_of(BOARD, nodes=1).machine_json
    raw = mover_package(machine, bit=111, width=4, shift=5)
    recipe = _rs.LaunchRecipe(raw, machine, wire=wire)
    recipe.launch([0])
    assert not recipe.admission_invariant and not recipe.wire_ready
    with pytest.raises(ValueError, match="forbids mover_quant GO"):
        recipe.launch([32])


@pytest.mark.parametrize("wire", [False, True])
def test_built_rechecks_an_inspected_word(wire):
    machine = machine_of(BOARD, nodes=1)
    text = machine.machine_json
    raw = mover_package(text, bit=111, width=4, shift=5)
    recipe = _rs.LaunchRecipe(raw, text, wire=wire)
    B.built(
        {0: B.Prebuilt(raw, (0,), text, 0, (), False, None, recipe=recipe)}, machine
    )
    with pytest.raises(ValueError, match="forbids mover_quant GO"):
        B.built(
            {0: B.Prebuilt(raw, (32,), text, 0, (), False, None, recipe=recipe)},
            machine,
        )


def test_the_epoch_revokes_retained_launches():
    dev = D.Device("offline", board=BOARD, library="first")
    machine = dev.card.machine_json
    raw = mover_package(machine)

    def fresh():
        return _rs.LaunchRecipe(
            raw, machine, scope=(dev.recipe_epoch, dev.recipe_epoch.generation)
        )

    try:
        for change in (
            lambda: setattr(dev, "library", "second"),
            dev.invalidate_library,
            lambda: setattr(dev, "machine", machine_of(BOARD, nodes=1)),
        ):
            recipe = fresh()
            recipe.launch([0])
            change()
            with pytest.raises(ValueError, match="invalidated"):
                recipe.launch([0])
    finally:
        dev.close()


def test_a_cancelled_program_frees_its_temporaries(monkeypatch):
    monkeypatch.setenv("KTPU_NATIVE_MEMORY", "1")
    dev = D.Device("offline", board=BOARD, queue_depth=1)
    try:
        before = dev.dram.used
        operations = [
            ("alloc", 0, 256, "dram", None, "temporary"),
            ("alloc", 1, dev.dram.hi, "dram", None, "impossible"),
        ]
        epoch = dev.recipe_epoch, dev.recipe_epoch.generation
        program = _rs.LaunchProgram(operations, [], 0, epoch)
        with pytest.raises(MemoryError):
            program.launch(dev, [])
        assert dev.dram.used == before
    finally:
        dev.close()


def storage(address):
    return SimpleNamespace(
        addr=address,
        nbytes=256,
        dtype=None,
        owned=False,
        host=None,
        host_f16_dtype=None,
        forms={FLAT: Form(FLAT, address, 256, False)},
        valid={FLAT},
        zeroed=set(),
        site=None,
    )


def test_a_key_tells_aliases_and_layouts_not_addresses():
    plan = SimpleNamespace(ast=object())
    a, b = storage(0x100000000), storage(0x100000000)
    aliased = R.signature(plan, {0: a, 1: b})
    b.addr = 0x100001000
    b.forms[FLAT].addr = b.addr
    separate = R.signature(plan, {0: a, 1: b})
    assert aliased != separate
    a.addr += 0x10000
    b.addr += 0x10000
    a.forms[FLAT].addr = a.addr
    b.forms[FLAT].addr = b.addr
    assert separate == R.signature(plan, {0: a, 1: b})
    b.forms[("pad", 16, 32)] = Form(("pad", 16, 32), b.addr + 512, 512, True)
    b.valid = {("pad", 16, 32)}
    assert separate != R.signature(plan, {0: a, 1: b})


def test_a_new_host_input_is_never_replayed_stale(monkeypatch):
    monkeypatch.setenv("KTPU_HOST_RECIPE", "1")
    monkeypatch.setenv("KTPU_NATIVE_PROGRAM", "1")
    monkeypatch.setenv("KTPU_NATIVE_MEMORY", "1")
    dev = D.Device("offline", board=BOARD, queue_depth=1)
    source, dest = Storage(dev, 256), Storage(dev, 256)
    source.host = bytes([3]) * 256
    plan = SimpleNamespace(ast=object(), out_slot=1)
    calls = []

    def execute(dev, plan, buffers):
        calls.append(1)
        raw = bytes((x + 1) % 256 for x in buffers[0].host)
        dev.write(buffers[1].addr, raw)
        buffers[1].host = raw
        return []

    table = R.Recipes()
    try:
        with patch.object(R.run, "execute", execute):
            table.execute(dev, plan, {0: source, 1: dest})
            dest.host = None
            table.execute(dev, plan, {0: source, 1: dest})
            assert len(calls) == 1
            assert dev.read(dest.addr, 256) == bytes([4]) * 256
            source.host = bytes([7]) * 256
            dest.host = None
            table.execute(dev, plan, {0: source, 1: dest})
            assert dev.read(dest.addr, 256) == bytes([8]) * 256
            assert len(calls) == 2
    finally:
        table.clear()
        source.free()
        dest.free()
        dev.close()
