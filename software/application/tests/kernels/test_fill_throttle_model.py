"""The fill throttle on the card model (L2.VC.027): a valid stream past the
completion queue's wrap limit, against numpy, each input's rows distinct.

    KOHAKUTPU_BOARD=multimesh_v9t6 KOHAKUTPU_SIM_BUILD=build/v9t6/vlt_card_v9t6_1n pytest ...
"""

import numpy as np

from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.stack import kernel as K

VIEWS, ROWS = 7, 32


def wide() -> str:
    params = ", ".join(f"x{i}: f16[{ROWS},128] @dram" for i in range(VIEWS))
    lines = [
        "target ktpu.v9",
        f"fn wide.l2({params}, y: f16[{ROWS},128] @dram)",
        "    par c in 0..1 on vc[c]",
        f"        for t in 0..{ROWS}",
    ]
    for i in range(VIEWS):
        lines.append(f"            a{i} = load x{i}[t:+1, :] : f16[1,128] @l1")
        lines.append(f"            v{i} = unpack a{i} : f32[1,128] @reg")
    last = "v0"
    for i in range(1, VIEWS):
        lines.append(f"            w{i} = add {last}, v{i} : f32[1,128] @reg")
        last = f"w{i}"
    lines.append(f"            q = pack {last} : f16[1,128] @l1")
    lines.append("            store y[t:+1, :] <- q")
    return "\n".join(lines) + "\n"


def test_a_stream_past_the_wrap_limit_reads_every_row(dev):
    machine = machine_of(dev.machine.board, nodes=1)
    c = K.compile(wide(), name="wide", machine=machine)
    rng = np.random.default_rng(VIEWS)
    inputs = {
        f"x{i}": rng.standard_normal((ROWS, 128)).astype(np.float16)
        for i in range(VIEWS)
    }
    binds = {}
    for name, x in inputs.items():
        binds[name] = dev.alloc(x.nbytes)
        dev.write(binds[name], x.tobytes())
    binds["y"] = dev.alloc(ROWS * 128 * 2)
    dev.run(c, binds)
    got = np.frombuffer(dev.read(binds["y"], ROWS * 128 * 2), np.float16)
    want = sum(x.astype(np.float32) for x in inputs.values()).astype(np.float16)
    assert np.allclose(got.reshape(ROWS, 128), want, rtol=2e-3, atol=2e-4)
    for at in binds.values():
        dev.free(at)
