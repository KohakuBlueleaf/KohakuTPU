"""Rust-built MG dispatch microcase shared by OS and firmware measurements."""

import json
import struct

from kohakutpu.compiler import _rs


def operand(value):
    """Four packed MX7 rows of 32 equal integers, exact unit scales."""
    word = 0xA0A0A0A0 | sum((value & 127) << (249 - 7 * slot) for slot in range(32))
    return word.to_bytes(32, "little") * 4


def expected_output(value):
    return struct.pack(
        "<16e", float(32 * value * value), *([float(32 * value * value)] * 15)
    )


def mg_package(machine_json, base, rounds, lower=False, arithmetic=False):
    words = []
    for _ in range(rounds):
        for op, sel, off in ((1, 0, 0), (1, 1, 0), (3, 0, 256)):
            addr = base + off
            word = (
                op << 252
                | (addr & ((1 << 34) - 1)) << 218
                | (addr >> 34) << 63
                | 1 << 202
                | sel << 201
                | 1 << 176
                | 40 << 168
            )
            words.append(hex(word))
    if arithmetic:
        words = []
        for _ in range(rounds):
            for op, fields in (
                ("FILL", {"addr": base, "n": 1, "sel": 0}),
                ("FILL", {"addr": base, "n": 1, "sel": 1}),
                ("GEMM", {"gm": 1, "gn": 1, "nk": 1, "anchor": 40}),
                ("DRAIN", {"addr": base + 256, "n": 1}),
            ):
                words.append(hex(_rs.mg_encode(op, fields, machine_json)))
    return _rs.package_build(
        json.dumps({"types": [[1, 1, "MG"]], "credit": 32}),
        json.dumps([["unit", [1, 1]], ["dispatch", 0, words], ["barrier"]]),
        machine_json,
        lower=lower,
    )
