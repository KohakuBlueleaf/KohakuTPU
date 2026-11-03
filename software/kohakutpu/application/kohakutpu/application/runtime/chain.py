"""Two products as one kernel (`KTPU_MM_CHAIN`): inside `chaining`, a
contraction's launch is held; the next contraction that takes the held one's
output as its f16 A (`activation_fp16`) runs with it as one `kernel.chain`
launch, the output a staging scratch where no later call reads it (`dead`),
else still written out. Any other launch that touches what the held one reads
or writes, and any submit outside the generated path, first runs it alone."""

import contextlib
import json

import numpy as np

from kohakutpu.application.runtime import generated, launch_boundary
from kohakutpu.language.config import ENV

MM = "matmul.mm_kernel"
#: Bytes an element of each port dtype takes at most (an MXFP7 entry, under one).
ITEM = {"f16": 2, "f32": 4, "u32": 4, "i32": 4, "u64": 8, "u8": 1, "mx7a": 1, "mx7b": 1}


def enabled() -> bool:
    return ENV.get("KTPU_MM_CHAIN", "0") == "1"


class Held:
    """What a held launch returns: no run yet (`run.execute` drops it)."""

    def __init__(self):
        self.info: dict = {}


HELD = Held()


def ports_of(source: str) -> dict:
    """`{port: (role, nbytes, spec)}` of a generated graph."""
    out = {}
    for p in json.loads(source)["ports"]:
        spec = p["spec"]
        nbytes = int(np.prod(spec["shape"])) * ITEM[spec["dtype"]]
        out[p["name"]] = (p["role"], nbytes, spec)
    return out


def linkable(first: dict, second: dict) -> bool:
    """Whether `kernel.chain` takes `second`'s input `a` from `first`'s output
    `out` (`gen/chain.rs` checks the same): one element type and layout, the
    same row width, and no more rows read than written."""
    if "out" not in first or "a" not in second:
        return False
    (r1, _, d), (r2, _, s) = first["out"], second["a"]
    return (
        r1 == "out"
        and r2 == "in"
        and s["dtype"] == d["dtype"]
        and s["layout"] == d["layout"]
        and len(s["shape"]) == len(d["shape"])
        and s["shape"][1:] == d["shape"][1:]
        and s["shape"][0] <= d["shape"][0]
    )


def _spans(ports: dict, bindings: dict, role: str | None = None) -> list:
    return [
        (int(bindings[n]), int(bindings[n]) + nb)
        for n, (r, nb, _) in ports.items()
        if n in bindings and role in (None, r)
    ]


def _addresses(bindings: dict) -> bool:
    """Whether every binding is a plain address the spans can be read from."""
    return all(isinstance(v, (int, np.integer)) for v in bindings.values())


def _overlap(a: list, b: list) -> bool:
    return any(s < e2 and s2 < e for s, e in a for s2, e2 in b)


class Chain:
    def __init__(self, dev):
        self.dev = dev
        self.held: tuple | None = None
        #: `(start, end)` spans no call after the running group reads.
        self.dead: set = set()
        self.launching = False
        self.chained = 0
        #: frees asked for while a product is held, done once it has run
        self.frees: list = []

    def _now(self, *call):
        self.launching = True
        try:
            return generated.launch_now(self.dev, *call)
        finally:
            self.launching = False
            self._release()

    def _release(self):
        if self.held is None:
            frees, self.frees = self.frees, []
            for addr in frees:
                self.dev.free(addr)

    def flush(self):
        if self.held is not None:
            held, self.held = self.held, None
            self._now(*held[:5])

    def before_submit(self):
        if not self.launching:
            self.flush()

    def access(self, addr: int, nbytes: int):
        if self.held is not None and _overlap(
            [(addr, addr + nbytes)], _spans(self.held[5], self.held[2])
        ):
            self.flush()

    def defer_free(self, addr: int) -> bool:
        if self.held is None:
            return False
        self.frees.append(addr)
        return True

    def _dead(self, span) -> bool:
        return any(s <= span[0] and span[1] <= e for s, e in self.dead)

    def _chain(self, held, generator, arguments, bindings, ports, kind, info):
        """`held` and this product as one launch, else None."""
        g1, a1, b1, _, i1, p1 = held
        if not (
            generator == MM
            and arguments.get("activation_fp16")
            and "a" in bindings
            and bindings["a"] == b1.get("out")
            and linkable(p1, ports)
        ):
            return None
        at = int(b1["out"])
        keep = not self._dead((at, at + p1["out"][1]))
        call = {
            "first": {"gen": g1, "args": generated.prepared(g1, a1)},
            "second": {
                "gen": generator,
                "args": generated.prepared(generator, arguments),
            },
            "from": "a",
            "to": "out",
            "keep": keep,
        }
        self.held = None
        binds = {k: v for k, v in b1.items() if keep or k != "out"}
        binds.update({f"c2_{k}": v for k, v in bindings.items() if k != "a"})
        self.chained += 1
        chained = dict(info or {}, chain=[i1, keep])
        return self._now("kernel.chain", call, binds, kind, chained)

    def launch(self, generator, arguments, bindings, kind, info):
        ports = ports_of(generated.source(self.dev, generator, arguments))
        if self.held is not None:
            run = self._chain(
                self.held, generator, arguments, bindings, ports, kind, info
            )
            if run is not None:
                return run
            _, _, b1, _, _, p1 = self.held
            touched = (
                not _addresses(bindings)
                or _overlap(_spans(ports, bindings), _spans(p1, b1, "out"))
                or _overlap(_spans(ports, bindings, "out"), _spans(p1, b1))
            )
            if touched or generator == MM:
                self.flush()
        # A recipe recording takes each launch as its plan makes it: none is held.
        recording = self.dev._recipe_recording is not None
        if generator == MM and _addresses(bindings) and not recording:
            self.held = (generator, arguments, bindings, kind, info, ports)
            return HELD
        return self._now(generator, arguments, bindings, kind, info)


@contextlib.contextmanager
def chaining(dev):
    """Product chains on `dev` while open (`KTPU_MM_CHAIN`); what is held at
    the end runs alone."""
    if not enabled():
        yield None
        return
    chain = Chain(dev)
    token = launch_boundary.CHAIN.set(chain)
    try:
        yield chain
        chain.flush()
    finally:
        launch_boundary.CHAIN.reset(token)
