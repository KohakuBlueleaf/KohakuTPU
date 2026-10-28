"""Rust host binder translates package-local handles, including affine sites."""

import json

import pytest

from kohakutpu.language import machine as MF
from kohakutpu.stack import rs


def bind(pkg, handles, translations, **kw):
    machine = (MF.MACHINES / "multimesh_v9t6.json").read_text(encoding="utf-8")
    return rs._rs.package_read(
        rs._rs.bind(pkg, handles, machine, translations=translations, **kw), machine
    )


def package(*, relocate=True, default=0):
    machine = (MF.MACHINES / "multimesh_v9t6.json").read_text(encoding="utf-8")
    calls = (
        [
            ["spans_from", [[default, 32]]],
            ["payload", default + 8, {"addresses": [[[0, 40, 0]]]}],
        ]
        if relocate
        else [["buffer", "x", 32, default], ["payload", 0]]
    )
    return rs._rs.package_build(
        '{"credit": 32}', json.dumps(calls), machine, defaults=False
    )


def test_handle_binding_writes_physical_address_only_at_bind():
    p = package()
    before = p
    out = bind(p, [0x100020], [(0x100000, 0x400000, 128)])
    assert out["payloads"] == [0x400028]
    assert out["buffers"] == out["relocs"] == []
    assert p == before
    machine = (MF.MACHINES / "multimesh_v9t6.json").read_text(encoding="utf-8")
    assert rs._rs.package_read(p, machine)["payloads"] == [0]


@pytest.mark.parametrize(
    "handles,table",
    [
        ([], [(0x100000, 0, 128)]),
        ([0x400000], [(0x100000, 0x400000, 128)]),
        ([0x100070], [(0x100000, 0, 128)]),
        ([0x100000], []),
        ([0x100000], [(0x100000, 0, 128), (0x100040, 4096, 32)]),
        ([0x100000], [(0x100000, (1 << 64) - 32, 128)]),
    ],
)
def test_bad_handle_or_translation_refuses(handles, table):
    with pytest.raises(rs._rs.Refused) as error:
        bind(package(), handles, table)
    assert error.value.clause == "RUL.023"


def test_template_handle_path_matches_ordinary_binding():
    p = package(relocate=False)
    table = {
        "parameters": {"offset": {"lo": 0, "hi": 31}},
        "sites": [
            {
                "name": "address",
                "payload": 0,
                "bit": 0,
                "width": 40,
                "buffer": 0,
                "coefficients": {"offset": 1},
            }
        ],
    }
    got = bind(
        p,
        [0x100020],
        [(0x100000, 0x400000, 128)],
        params={"offset": 8},
        template_json=json.dumps(table),
    )
    assert got["payloads"] == [0x400028]
    p = package(default=99)
    zero = bind(p, [0x100000], [(0x100000, 0, 128)])
    assert zero["payloads"] == [8]
