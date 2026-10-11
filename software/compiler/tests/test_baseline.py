"""Every kernel's bytes, pinned as SHA-256 in `golden/v9.json`: per body level
(`l1` hand-written, `l2` / `l3` compiled) the L1 and planned L2 text and the
cold and warm node packages with their bindings; per kernel the packed inputs,
the L3 reference's rounded and exact outputs, and the stated work.

The hand-L1 packages witness the encoder: their bytes ran on the card. A
compiled level's entries move only with a pipeline change a card run has
justified; ``python software/compiler/tests/test_baseline.py --write`` then
re-pins them.
"""

import functools
import hashlib
import json
import pathlib
import sys

import numpy as np
import pytest
from kohakutpu.compiler import build, imem
from kohakutpu.compiler import target as T
from kohakutpu.compiler.layout.buffer import Buffer
from kohakutpu.language import kernels, pipeline
from kohakutpu.language.l3 import reference
from kohakutpu.language.text.module import read as read_module

GOLDEN = pathlib.Path(__file__).resolve().parent / "golden" / "v9.json"
#: The card runner's arena: DRAM from 1 MB, staging past the node queue.
ARENA, STAGING, QUEUE = 0x0010_0000, 1 << 39, 0x0010_0000
ALIGN = 256
#: The memory port a fetched dispatch reads through.
FETCH = (3, 1)
#: The inputs' generator: standard normal, this seed, the host dtype.
SEED = 2


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def load(name: str):
    return kernels.load(kernels.kernel(name))


class Arena:
    """Bump allocation, `ALIGN`-aligned, in DRAM or in staging."""

    def __init__(self) -> None:
        self.top = {"dram": ARENA, "stage": STAGING + QUEUE}

    def __call__(self, nbytes: int, space: str) -> int:
        key = "stage" if space == "stage" else "dram"
        at = -(-self.top[key] // ALIGN) * ALIGN
        self.top[key] = at + nbytes
        return at


def packages(m, name) -> dict:
    arena = Arena()
    binds = {}
    for p, t in m.body(name, "l1").params:
        b = Buffer.of(t)
        binds[p] = arena(b.nbytes, b.space)
    resident = imem.Cores(imem.IMEM_WORDS)
    out = {"binds": {p: hex(a) for p, a in binds.items()}}
    for run in ("cold", "warm"):
        pkg, bindings = build.package(build.program(m, name, binds), FETCH, resident)
        raw = pkg.to_bytes()
        out[run] = {
            "pkg": sha(raw),
            "bytes": len(raw),
            "bindings": sha(repr(bindings).encode()),
        }
    return out


def level_entry(name: str, level: str, text_of) -> dict:
    m = load(name)
    if level == "l1":
        return packages(m, name)
    text = text_of(name, level)
    out = {"l1_text": sha(text.encode())}
    if level == "l3":
        out["l2_text"] = sha(pipeline.compile_l3(m, name, T.TARGET).encode())
    out.update(packages(read_module(text, f"<{name}.{level}>"), name))
    return out


def numerics(name: str) -> dict:
    m = load(name)
    bufs = [(p, Buffer.of(t)) for p, t in m.body(name, "l1").params]
    n_in = len(m.body(name, "l3").params)
    rng = np.random.default_rng(SEED)
    inputs = [
        rng.standard_normal(b.host_shape).astype(b.host_dtype) for _, b in bufs[:n_in]
    ]
    rounded = np.asarray(reference.run(m, name, inputs), np.float64)
    exact = np.asarray(reference.run(m, name, inputs, exact=True), np.float64)
    return {
        "packed": {
            p: sha(b.pack(x)) for (p, b), x in zip(bufs[:n_in], inputs, strict=True)
        },
        "reference": {"rounded": sha(rounded.tobytes()), "exact": sha(exact.tobytes())},
        "work": reference.work(m, name, [x.shape for x in inputs]),
    }


def entries() -> list:
    """Every ``(kernel, key)`` pinned: each body level, then the numerics."""
    out = []
    for name in kernels.names():
        m = load(name)
        out += [(name, lv) for lv in ("l1", "l2", "l3") if (name, lv) in m.fns]
        out.append((name, "numerics"))
    return out


def snapshot(name: str, key: str, text_of) -> dict:
    """`key`'s entry for kernel `name`; `text_of(kernel, level)` compiles."""
    return numerics(name) if key == "numerics" else level_entry(name, key, text_of)


def golden() -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_every_entry_is_pinned():
    want = golden()
    assert sorted(f"{n}/{k}" for n, k in entries()) == sorted(
        f"{n}/{k}" for n in want for k in want[n]
    )


@pytest.mark.parametrize("name, key", entries(), ids=[f"{n}-{k}" for n, k in entries()])
def test_bytes_are_pinned(name, key, compiled):
    assert snapshot(name, key, compiled) == golden()[name][key]


def write() -> None:
    text_of = functools.cache(lambda n, lv: build.l1_text(load(n), n, lv))
    out: dict = {}
    for name, key in entries():
        out.setdefault(name, {})[key] = snapshot(name, key, text_of)
    GOLDEN.parent.mkdir(exist_ok=True)
    GOLDEN.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n", "utf-8")


if __name__ == "__main__" and sys.argv[1:] == ["--write"]:
    write()
