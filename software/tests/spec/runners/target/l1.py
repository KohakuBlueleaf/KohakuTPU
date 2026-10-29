"""L1 target execution through the Rust seam and machine-bound host buffers."""

import numpy as np
from harness.registry import runner

from kohakutpu.language.text import module as M
from kohakutpu.stack import kernel
from kohakutpu.stack.buffer import Buffer


@runner("l1.card")
def card(inp, ctx):
    dev = ctx["session"].dev
    mod = M.read(inp["text"])
    body = mod.body(inp["fn"], "l1")
    binds, sizes = {}, {}
    try:
        for p, t in body.params:
            n = Buffer.of(t, dev.card.machine_json).nbytes
            binds[p], sizes[p] = dev.alloc(n), n
            raw = np.zeros(n // 4, np.uint32)
            init = inp.get("init", {}).get(p, [])
            if isinstance(init, dict):
                init = np.arange(*init["range"], dtype=np.uint32)
            init = np.asarray(init, np.uint32)
            raw[: len(init)] = init
            dev.write(binds[p], raw.tobytes())
        c = kernel.compile(mod, inp["fn"], dev.machine, level="l1")
        dev.run(c, binds)
        data = {
            p: np.frombuffer(dev.read(binds[p], sizes[p]), np.uint32) for p in binds
        }
    finally:
        for a in binds.values():
            dev.free(a)
    out = {p: [int(v) for v in data[p]] for p in inp.get("read", [])}
    for a, b in inp.get("same", []):
        out[f"{a}=={b}"] = bool(np.array_equal(data[a], data[b]))
    return out
