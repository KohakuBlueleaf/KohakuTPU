"""Balanced gather selector: byte-exact code routing, keys and mutation witnesses."""

import json
from dataclasses import replace

import numpy as np
import pytest

from kohakutpu.application.kernels import relayout as R
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.language import machine as MF
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs


def fixture(slots=2, keep=True, rotations=16):
    hw = R.hw_of(MF.load("vlt_v9t6s_1n"), 1)
    rows = 4
    pairs = tuple(slot * 16 + rot for slot in range(slots) for rot in range(rotations))
    code = np.array([R.KEEP, R.ZERO, *pairs])[
        np.arange(rows * 128) % (len(pairs) + 2)
    ].reshape(rows, 128)
    if not keep:
        code[code == R.KEEP] = R.ZERO
    pull = R.Pull(
        "src",
        "dst",
        0,
        rows * 8,
        rows,
        rows * 8,
        tuple(("walk", ((rows * 8, 32),), k * rows * 256) for k in range(slots)),
        0,
        pairs,
        "dst" if keep else "zero",
        keep,
    )
    scratch = [("po", rows, 128)] + [(f"ps{k}", rows, 128) for k in range(slots)]
    if keep:
        scratch.append(("pd", rows, 128))
    plan = R.Plan(
        (pull,),
        tuple(scratch),
        np.zeros(8, dtype="<f4"),
        code.astype("<f2"),
        slots * rows * 8,
        rows * 8,
    )
    return hw, plan


def selector_bytes(plan, source, previous):
    p = plan.parts[0]
    selectors, valid, zero = p.tree
    leaves = []
    for pair in p.pairs:
        slot, rotation = divmod(pair, 16)
        raw = source[slot * p.rows * 128 : (slot + 1) * p.rows * 128].reshape(-1, 16)
        leaves.append(np.roll(raw, -rotation, axis=1).reshape(p.rows, 128))
    leaves += [np.zeros_like(leaves[0])] * ((1 << len(selectors)) - len(leaves))
    for offset in selectors:
        mask = plan.masks[offset : offset + p.rows].astype(bool)
        leaves = [
            np.where(mask, leaves[i + 1], leaves[i]) for i in range(0, len(leaves), 2)
        ]
    result = leaves[0]
    if valid >= 0:
        result = np.where(
            plan.masks[valid : valid + p.rows],
            result,
            previous.reshape(p.rows, 128) if p.base == "dst" else 0,
        )
    if zero >= 0:
        result = np.where(plan.masks[zero : zero + p.rows], 0, result)
    return result.reshape(-1).astype("<u2")


@pytest.mark.parametrize(
    "slots,keep,rotations", [(1, False, 16), (1, True, 8), (2, False, 4)]
)
def test_tree_bits_route_payload_and_mutation(slots, keep, rotations):
    hw, baseline = fixture(slots, keep, rotations)
    tree = R.tree_pull(baseline, hw)
    source = np.arange(slots * 512, dtype="<u2") * 13
    previous = np.full(512, 0xDEAD, dtype="<u2")
    expected = R.execute(baseline, source, previous, hw)
    np.testing.assert_array_equal(selector_bytes(tree, source, previous), expected)
    masks = tree.masks.copy()
    first = tree.parts[0].tree[0][0]
    masks[first : first + 4] = 1 - masks[first : first + 4]
    broken = replace(tree, masks=masks)
    with pytest.raises(AssertionError):
        np.testing.assert_array_equal(
            selector_bytes(broken, source, previous), expected
        )


def test_tree_memory_view_admission():
    hw, baseline = fixture(2, True)
    transformed = R.tree_pull(baseline, hw)
    assert transformed.shape() == baseline.shape()
    np.testing.assert_array_equal(transformed.masks, baseline.masks)


def test_tree_reduces_compares_and_changes_key():
    hw, baseline = fixture(1, False)
    machine = machine_of("vlt_v9t6s_1n", packing_backend="vector_mx7")
    results = []
    for plan in (baseline, R.tree_pull(baseline, hw)):
        kernel, given = R.relayout_kernel(plan, hw)
        source = json.dumps(kernel.trace(given, machine=K.dsl_machine(machine)).graph)
        compiled = rs._rs.compile(
            source, rs.machine_json(machine), cache_dir="", fetch=True
        )
        assert not compiled["verdict"]["refused"], compiled["verdict"]
        results.append(
            (compiled, json.loads(rs._rs.library_key(source, rs.machine_json(machine))))
        )

    def witness(result):
        assert "vcmpeq " not in result[0]["l1"]
        assert "vsel " in result[0]["l1"]

    witness(results[1])
    assert results[0][1]["class"] != results[1][1]["class"]
    with pytest.raises(AssertionError):
        witness(results[0])
