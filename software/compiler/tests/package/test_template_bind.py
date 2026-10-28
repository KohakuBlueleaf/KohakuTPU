"""Host-only value templates through _rs.bind, with independent wire bytes."""

import json
import struct

import pytest

from kohakutpu.compiler import _rs
from kohakutpu.language import machine as MF


@pytest.fixture(scope="module")
def machine_json():
    return (MF.MACHINES / "multimesh_v9t6.json").read_text(encoding="utf-8")


def package_bytes(buffers, payloads):
    pkg = {
        "buffers": buffers,
        "payloads": [hex(w) for w in payloads],
        "steps": [{"op": "END"}],
        "signature": 42,
        "ack_reserve": 3,
        "checksum": True,
    }
    got = json.loads(
        _rs.spec_run("l0.pkg.bytes", json.dumps({"package": pkg}), "multimesh_v9t6")
    )
    return b"".join(bytes.fromhex(line) for line in got["lines"])


def source():
    return package_bytes(
        [{"name": "a", "size": 256, "default": 4096}], [(1 << 256) - 1, 0]
    )


def payloads(raw):
    h = struct.unpack_from("<12Q", raw)
    return [
        int.from_bytes(raw[h[9] + 32 * i : h[9] + 32 * (i + 1)], "little")
        for i in range(h[3] & 0xFFFFFFFF)
    ]


def table():
    return {
        "parameters": {"n": {"lo": 0, "hi": 256}},
        "sites": [
            {
                "name": "low",
                "payload": 0,
                "bit": 60,
                "width": 8,
                "constant": 4608,
                "coefficients": {"n": 1},
            },
            {
                "name": "high",
                "payload": 0,
                "bit": 200,
                "width": 8,
                "shift": 8,
                "constant": 4608,
                "coefficients": {"n": 1},
            },
            {
                "name": "address",
                "payload": 1,
                "bit": 0,
                "width": 64,
                "coefficients": {"n": 1},
                "buffer": 0,
            },
        ],
    }


@pytest.mark.parametrize("n", [0, 1, 52, 255])
def test_reuse_binds_split_values_and_addresses_to_ordinary_bytes(machine_json, n):
    raw = source()
    meta = json.dumps(table())
    got = _rs.bind(raw, [8192], machine_json, params={"n": n}, template_json=meta)
    expected = (1 << 256) - 1
    expected &= ~((255 << 60) | (255 << 200))
    expected |= n << 60 | 0x12 << 200
    ordinary = package_bytes([], [expected, 8192 + n])
    assert got == ordinary
    assert source() == raw
    assert payloads(got) == [expected, 8192 + n]
    for mutant in ["l0.template/affine", "l0.template/split"]:
        if mutant.endswith("affine") and n == 0:
            continue
        assert mutant in _rs.MUTANTS
        broken = _rs.bind(
            raw,
            [8192],
            machine_json,
            params={"n": n},
            template_json=meta,
            mutant=mutant,
        )
        assert broken != got


@pytest.mark.parametrize(
    "n,clause,mutant,site",
    [
        (257, "L0.BIND.038", "class", "address"),
        (256, "L0.BIND.039", "extent", "address"),
    ],
)
def test_class_and_extent_refusals_name_sites_and_params(
    machine_json, n, clause, mutant, site
):
    meta = table()
    if mutant == "class":
        meta["sites"] = meta["sites"][:2]
        site = "low"
    raw = source()
    with pytest.raises(_rs.Refused) as exc:
        _rs.bind(
            raw, [8192], machine_json, params={"n": n}, template_json=json.dumps(meta)
        )
    assert exc.value.stage == "bind" and exc.value.clause == clause
    assert site in str(exc.value) and str(n) in str(exc.value)
    _rs.bind(
        raw,
        [8192],
        machine_json,
        params={"n": n},
        template_json=json.dumps(meta),
        mutant=f"l0.template/{mutant}",
    )


def test_width_refusal_is_not_truncation(machine_json):
    meta = table()
    meta["sites"] = [meta["sites"][0]]
    meta["sites"][0]["constant"] = 0
    with pytest.raises(_rs.Refused, match=r"low\[0\].*256.*width 8") as exc:
        _rs.bind(
            source(),
            [],
            machine_json,
            params={"n": 256},
            template_json=json.dumps(meta),
        )
    assert exc.value.clause == "L0.BIND.037"
    raw = _rs.bind(
        source(),
        [],
        machine_json,
        params={"n": 256},
        template_json=json.dumps(meta),
        mutant="l0.template/width",
    )
    assert (payloads(raw)[0] >> 60) & 255 == 0


@pytest.mark.parametrize("path", ["step", "register"])
def test_step_count_paths_and_lowering(machine_json, path):
    def build(count):
        return _rs.package_build(
            json.dumps({"credit": 512}),
            json.dumps(
                [["unit", [0, 0], "VC"], ["dispatch", 0, [0] * count]]
                + [["payload", 0]] * (3 - count)
                + [["end"]]
            ),
            machine_json,
            checksum=True,
        )

    raw = build(1)
    meta = json.dumps(
        {
            "parameters": {"n": {"lo": 1, "hi": 3}},
            "sites": [
                {
                    "name": "words",
                    "step": 0,
                    "bit": 32,
                    "width": 32,
                    "coefficients": {"n": 1},
                }
            ],
        }
    )
    args = dict(params={"n": 2}, template_json=meta, count_path=path)
    bound = _rs.bind(raw, [], machine_json, **args)
    out = _rs.package_read(bound, machine_json)
    lowered = _rs.bind(raw, [], machine_json, lower=True, **args)
    if path == "step":
        assert bound == build(2)
        assert any(s[0] == 11 for s in _rs.package_read(lowered, machine_json)["steps"])
    else:
        assert out["steps"] == [
            (16, 2, 15, 0, 2),
            (18, 0, 15, 2, 32 | (32 << 8) | (1 << 24)),
            (1, 0, 0, 0, 0),
            (0, 0, 0, 0, 0),
        ]
        assert lowered == bound
        assert len(bound) == len(raw) + 32
    assert build(1) == raw


@pytest.mark.parametrize("path", ["step", "register"])
def test_cut_keeps_original_indices_and_no_patches_to_removed_steps(machine_json, path):
    raw = _rs.package_build(
        json.dumps({"credit": 512}),
        json.dumps(
            [
                ["unit", [0, 0], "VC"],
                ["step", 3],
                ["step", 3],
                ["dispatch", 0, [0]],
                ["payload", 0],
            ]
        ),
        machine_json,
    )
    meta = {
        "parameters": {"n": {"lo": 1, "hi": 2}},
        "sites": [
            {
                "name": "later",
                "step": 2,
                "bit": 32,
                "width": 32,
                "coefficients": {"n": 1},
            }
        ],
    }
    args = dict(
        after=0, params={"n": 2}, template_json=json.dumps(meta), count_path=path
    )
    bound = _rs.bind(raw, [], machine_json, **args)
    assert _rs.package_read(bound, machine_json)["steps"] == [(3, 0, 0, 0, 0)]
    with pytest.raises(_rs.Refused, match="invalid step value field"):
        _rs.bind(raw, [], machine_json, mutant="l0.template/cut_sites", **args)


def test_no_table_identity_and_no_parameter_omission(machine_json):
    raw = source()
    expected = package_bytes([], [(1 << 256) - 1, 0])
    assert _rs.bind(raw, [0], machine_json) == expected
    assert _rs.bind(raw, [0], machine_json, params={}, template_json="{}") == expected
    assert _rs.bind(raw, [0], machine_json, mutant="l0.template/identity") != expected
    with pytest.raises(_rs.Refused, match="missing parameter n"):
        _rs.bind(raw, [], machine_json, template_json=json.dumps(table()))
    with pytest.raises(_rs.Refused, match="unknown parameter n"):
        _rs.bind(raw, [], machine_json, params={"n": 1})
