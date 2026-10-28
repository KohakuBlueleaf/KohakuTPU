"""Rust-generated tails preserve an immediately adjacent word sentinel."""

import json

import numpy as np
import pytest

from kohakutpu.language.text import module as M
from kohakutpu.stack import rs
from kohakutpu.stack.kernel import Compiled


@pytest.mark.parametrize("case", ["cast", "reverse"])
def test_tail_sentinel(dev, case):
    n = 144
    call = (
        ("convert.cast_kernel", {"n": n, "src": "f32", "dst": "f16"})
        if case == "cast"
        else (
            "transpose.reverse_kernel",
            {"src_bytes": n * 2, "levels": [[n // 16, 32]], "at": 0, "n": n},
        )
    )
    source = rs._rs.generate(
        call[0], json.dumps(call[1]), rs.machine_json(dev.machine), rs._MUTANT.get()
    )
    graph = json.loads(source)
    name = graph["name"]
    output = rs.compile(source, name, dev.machine)
    compiled = Compiled(
        name,
        source,
        M.read(output["l1"], "tail.l1"),
        dev.machine,
        verdict=output["verdict"],
        packages=output["packages"],
        stack="rust",
        scratch=output["scratch"],
    )
    dtype = np.float32 if case == "cast" else np.float16
    values = np.arange(n, dtype=dtype)
    src = dev.alloc(values.nbytes + 4096)
    dst = dev.alloc(n * 2 + 4096)
    sentinel = bytes([0xA5]) * 4096
    try:
        dev.write(src, values.tobytes() + bytes(4096))
        dev.write(dst, bytes(n * 2) + sentinel)
        bindings = {"in0": src, "out": dst} if case == "cast" else {"x": src, "y": dst}
        dev.run(compiled, bindings)
        expected = (
            values.astype(np.float16)
            if case == "cast"
            else values.reshape(-1, 16)[:, ::-1].flatten()
        )
        assert dev.read(dst, n * 2) == expected.tobytes()
        assert dev.read(dst + n * 2, len(sentinel)) == sentinel
    finally:
        dev.free(src)
        dev.free(dst)


@pytest.mark.parametrize("case", ["cast", "reverse"])
def test_tail_reversal_witness(dev, case):
    mutant = "memory/flat-overrun" if case == "cast" else "memory/reverse-overrun"
    with rs.mutant(mutant), pytest.raises(AssertionError):
        test_tail_sentinel(dev, case)
