"""An L2 kernel whose Rust build is not relocatable (STACK.RELOC: past
L0.BIND.005's buffer bound a span keeps its absolute addresses) is built at
each binding instead, visibly: `Compiled.bound` says why, `BOUND_BUILDS`
counts the builds, and `BOUND_FALLBACK` off refuses it as before."""

import pytest

from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.language.text.syntax import TextError
from kohakutpu.stack import kernel


def copies(n: int) -> str:
    """`n` one-tile copies, each its own input and output buffer: 2n spans."""
    params = ", ".join(
        [f"x{i}: f16[8, 128] @dram" for i in range(n)]
        + [f"y{i}: f16[8, 128] @dram" for i in range(n)]
    )
    body = "".join(
        "    par c in 0..1 on vc[c]\n"
        "        for t in 0..1\n"
        f"            a{i} = load x{i}[t*8 : +8, :]  : f16[8, 128] @l1\n"
        f"            v{i} = unpack a{i}  : f32[8, 128] @reg\n"
        f"            b{i} = pack v{i}  : f16[8, 128] @l1\n"
        f"            store y{i}[t*8 : +8, :] <- b{i}\n"
        for i in range(n)
    )
    return f"target ktpu.v9\n\nfn many.l2({params})\n{body}"


@pytest.fixture
def rust(monkeypatch):
    monkeypatch.setattr(kernel, "BOUND_BUILDS", {})


def binds(n: int) -> dict:
    names = [f"x{i}" for i in range(n)] + [f"y{i}" for i in range(n)]
    return {p: 0x0100_0000 + k * 0x1000 for k, p in enumerate(names)}


def test_a_relocatable_kernel_is_not_bound(rust):
    c = kernel.compile(copies(8), "many", machine_of("multimesh_v9t6"))
    assert c.bound is None and c.packages
    c.programs(binds(8))
    assert kernel.BOUND_BUILDS == {}


def test_past_256_buffers_the_kernel_is_built_at_each_binding(rust):
    c = kernel.compile(copies(129), "many", machine_of("multimesh_v9t6"))
    assert c.packages == {} and "not relocatable" in c.bound, c.bound
    at = binds(129)
    (p,) = c.programs(at).values()
    assert kernel.BOUND_BUILDS == {"many": 1}
    assert set(p.bindings) <= set(at.values())
    p.bound(True)  # binds and lowers, the L0.DE.040 replay included
    c.programs(at)
    assert kernel.BOUND_BUILDS == {"many": 1}, "a binding seen before is kept"
    c.programs({k: v + 0x10_0000 for k, v in at.items()})
    assert kernel.BOUND_BUILDS == {"many": 2}


def test_the_fallback_off_refuses_it(rust, monkeypatch):
    monkeypatch.setattr(kernel, "BOUND_FALLBACK", False)
    with pytest.raises(TextError, match="STACK.RELOC"):
        kernel.compile(copies(130), "many", machine_of("multimesh_v9t6"))
