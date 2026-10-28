"""Rust findings keep clause identities through the runtime gate."""

import pytest

from kohakutpu.application.runtime import packages
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.stack import kernel

TWO = """target ktpu.v9
image put(at)
    desc a0 = peer(at, count=1) walk=(32, 16)
    vdrain a0 <- l1[0] to=mg[0] buf=0
    halt
fn s.l1(x: mx7a[4, 64] @dram(gm=1, nk=2))
    send vc[0]
        run put(0)
    send vc[1]
        run put(16)
    send mg[0]
        fill A0 recv n=8
"""


def test_a_rust_refusal_names_the_clause_of_each_finding():
    c = kernel.compile(TWO, "s", machine_of("multimesh_v9t6", nodes=1), level="l1")
    params = c.l1.body("s", "l1").params
    binds = {p: 0x0100_0000 * (i + 1) for i, (p, _) in enumerate(params)}
    with pytest.raises(packages.Refused) as e:
        packages.findings(c.programs(binds), "refuse")
    port = [f for f in e.value.findings if f.kind == "port"]
    assert port and {f.clause for f in port} == {"L1.VER.008"}, port
    assert port[0].clause in str(port[0])
