"""Native recipe relocation safety and byte equivalence, without a card."""

import json

import pytest

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import packages as B
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.compiler import _rs
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs


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


def test_native_recipe_relocates_bytes_and_translates_live_handles():
    machine = machine_of("multimesh_v9t6", nodes=1).machine_json
    raw = mover_package(machine)
    recipe = _rs.LaunchRecipe(raw, machine)
    for address in (0, 64, 4096):
        expected = _rs.bind(raw, [address], machine, lower=True)
        assert recipe.launch([0x100000000], [(0x100000000, address, 32)]) == expected
    assert recipe.admission_invariant
    with pytest.raises(ValueError, match="extent"):
        recipe.launch([0x100000000], [(0x100000000, 4096, 16)])


def test_wire_recipe_patches_lowered_copies_and_refuses_overflow():
    machine = machine_of("multimesh_v9t6", nodes=1).machine_json
    raw = mover_package(machine)
    recipe = _rs.LaunchRecipe(raw, machine, wire=True)
    for address in (0, 64, (1 << 39) + 0x100):
        assert recipe.launch([address]) == _rs.bind(raw, [address], machine, lower=True)
    assert recipe.wire_ready
    with pytest.raises(ValueError, match="RUL.039"):
        recipe.launch([1 << 40])
    inspected_word_witness(
        lambda raw, machine: _rs.LaunchRecipe(raw, machine, wire=True)
    )


def test_native_recipe_resident_bytes_and_state_match_existing_bind():
    machine = machine_of("multimesh_v9t6", nodes=1)
    kernel, given = V.flat_kernel(X.op("add", X.Load(0), 1.0), ("f16",), 128, "f16", 2)
    compiled = K.specialise(kernel, given, machine=machine)
    p = compiled.packages[0]
    recipe = _rs.LaunchRecipe(
        p["bytes"], machine.machine_json, images_json=p.get("images_json")
    )
    old, new = _rs.Cores(machine.machine_json), _rs.Cores(machine.machine_json)
    for offset in (0, 256, 0):
        addresses = [0x100000 + i * 0x10000 + offset for i in range(len(p["params"]))]
        expected = _rs.bind(
            p["bytes"],
            addresses,
            machine.machine_json,
            lower=True,
            cores=old,
            images_json=p.get("images_json"),
        )
        assert recipe.launch(addresses, cores=new) == expected
        for coord in machine.mesh(0).units["VC"]:
            assert new.state(coord) == old.state(coord)


def test_recipe_epoch_revokes_retained_launches():
    dev = D.Device("offline", board="multimesh_v9t6", library="first")
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
            lambda: setattr(dev, "machine", machine_of("multimesh_v9t6", nodes=1)),
        ):
            recipe = fresh()
            recipe.launch([0])
            change()
            with pytest.raises(ValueError, match="invalidated"):
                recipe.launch([0])
        recipe = fresh()
        program = B.Prebuilt(raw, (0,), machine, 0, (), False, None, recipe=recipe)
        other = machine_of("vlt_v9t6s_1n", nodes=1)
        with pytest.raises(ValueError, match="receiving machine"):
            B.built({0: program}, other)
    finally:
        dev.close()


def test_mutant_epoch_invalidation_is_caught():
    assert "app.device/keeps_eager" in rs.mutants()
    with (
        rs.mutant("app.device/keeps_eager"),
        pytest.raises(pytest.fail.Exception, match="DID NOT RAISE"),
    ):
        test_recipe_epoch_revokes_retained_launches()


def inspected_word_witness(factory):
    machine = machine_of("multimesh_v9t6", nodes=1).machine_json
    raw = mover_package(machine, bit=111, width=4, shift=5)
    recipe = factory(raw, machine)
    recipe.launch([0])
    assert not recipe.admission_invariant and not recipe.wire_ready
    try:
        recipe.launch([32])
    except ValueError as error:
        assert "forbids mover_quant GO" in str(error)
    else:
        raise AssertionError("an inspected transform word changed after admission")


def test_native_recipe_rechecks_a_patch_to_an_inspected_word():
    inspected_word_witness(_rs.LaunchRecipe)
    # A template buffer site relocating an inspected word is also rechecked.
    table = json.dumps(
        {
            "parameters": {},
            "sites": [
                {
                    "name": "xf_id",
                    "payload": 0,
                    "bit": 111,
                    "width": 4,
                    "shift": 5,
                    "buffer": 0,
                }
            ],
        }
    )
    inspected_word_witness(
        lambda raw, machine: _rs.LaunchRecipe(
            mover_package(machine), machine, template_json=table
        )
    )
    # LaunchRecipe and package_admit share the same Rust admission check across policies.
    machine = machine_of("multimesh_v9t6", nodes=1).machine_json
    raw = mover_package(machine, bit=111, width=4, shift=5)
    doc = json.loads(machine)
    doc["machine"]["conversion"]["fp16_mx7"].update(
        allowed_backends=["mover_quant", "vector_mx7"], preferred_backend="vector_mx7"
    )
    permissive = json.dumps(doc)
    recipe = _rs.LaunchRecipe(raw, permissive)
    assert recipe.launch([32]) == _rs.bind(raw, [32], permissive, lower=True)
    _rs.package_admit(raw, permissive, bindings=[32])


def test_mutant_unconditional_admission_reuse_is_caught():
    machine = machine_of("multimesh_v9t6", nodes=1)
    raw = mover_package(machine.machine_json, bit=111, width=4, shift=5)

    def built_witness(wire: bool):
        recipe = _rs.LaunchRecipe(raw, machine.machine_json, wire=wire)
        B.built(
            {
                0: B.Prebuilt(
                    raw, (0,), machine.machine_json, 0, (), False, None, recipe=recipe
                )
            },
            machine,
        )
        with pytest.raises(ValueError, match="forbids mover_quant GO"):
            B.built(
                {
                    0: B.Prebuilt(
                        raw,
                        (32,),
                        machine.machine_json,
                        0,
                        (),
                        False,
                        None,
                        recipe=recipe,
                    )
                },
                machine,
            )
        return recipe

    assert "bindings.recipe/unconditional_admission" in rs.mutants()
    for wire in (False, True):
        recipe = built_witness(wire)
        assert not recipe.admission_invariant and not recipe.wire_ready
        with (
            rs.mutant("bindings.recipe/unconditional_admission"),
            pytest.raises(pytest.fail.Exception, match="DID NOT RAISE"),
        ):
            built_witness(wire)
