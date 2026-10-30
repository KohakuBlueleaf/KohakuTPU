"""Rust-built run packages and checker verdicts carried by the runtime."""

import json
import os
from dataclasses import dataclass
from enum import IntEnum
from functools import lru_cache
from types import SimpleNamespace

from kohakutpu.compiler import _rs
from kohakutpu.stack import rs

ADMISSION_ENV = "KOHAKUTPU_ADMISSION_CONTEXT"
REFUSE = (
    "deadlock",
    "rest",
    "port",
    "race",
    "bounds",
    "route",
    "line",
    "cluster",
    "vector",
)
GATES = ("refuse", "warn", "off")


@dataclass(frozen=True)
class Finding:
    kind: str
    where: str
    text: str
    clause: str = ""

    def __str__(self):
        at = f" ({self.clause})" if self.clause else ""
        return f"[{self.kind}] {self.where}: {self.text}{at}"


class Refused(Exception):
    def __init__(self, findings):
        self.findings = findings
        bad = [f for f in findings if f.kind in REFUSE]
        super().__init__(
            f"the static checker refuses this run ({len(bad)} findings; "
            "check='warn' runs it anyway):\n" + "\n".join(str(f) for f in bad[:20])
        )


@dataclass(frozen=True)
class Package:
    """Immutable wire bytes and read-only Rust-decoded diagnostic tables."""

    _raw: object
    machine_json: str
    mesh: int = 0

    @property
    def raw(self) -> bytes:
        if callable(self._raw):
            object.__setattr__(self, "_raw", self._raw())
        return self._raw

    def to_bytes(self):
        return self.raw

    @property
    def tables(self):
        return _rs.package_read(self.raw, self.machine_json, self.mesh)

    @property
    def steps(self):
        op = IntEnum("Op", _rs.package_format(self.machine_json, self.mesh)["op"])
        return [
            SimpleNamespace(op=op(o), flags=f, unit=u, count=c, arg=a)
            for o, f, u, c, a in self.tables["steps"]
        ]

    @property
    def buffers(self):
        return [SimpleNamespace(**b) for b in self.tables["buffers"]]

    @property
    def payloads(self):
        return self.tables["payloads"]

    def bound(self, bindings=None):
        return _rs.package_read(
            self.raw, self.machine_json, self.mesh, list(bindings or ())
        )["bound"]


@lru_cache(maxsize=512)
def environment_metadata(package, machine, mesh, table):
    metadata = json.loads(table)
    buffers = _rs.package_read(package, machine, mesh)["buffers"]
    sizes = metadata.get("buffer_sizes", [b["size"] for b in buffers])
    return metadata.get("environment"), tuple(
        (b["mesh"], size) for b, size in zip(buffers, sizes, strict=True)
    )


@dataclass(frozen=True)
class Prebuilt:
    package: bytes
    bindings: tuple
    machine_json: str
    mesh: int
    findings: tuple
    refused: bool
    fetch: tuple | None
    translations: tuple | None = None
    images_json: str | None = None
    template_json: str | None = None
    template_params: dict | None = None
    recipe: object = None

    def matches_environment(self, mmu):
        """Compare the certified relation with this submission's physical spans."""
        if self.template_json is None:
            return True
        expected, buffers = environment_metadata(
            self.package, self.machine_json, self.mesh, self.template_json
        )
        if expected is None:
            return False
        addresses = [
            mmu.resolve(h, size)[1]
            for h, (_, size) in zip(self.bindings, buffers, strict=True)
        ]
        actual = []
        for i, (mesh, size) in enumerate(buffers):
            a = addresses[i]
            for j in range(i + 1, len(buffers)):
                other_mesh, other_size = buffers[j]
                b = addresses[j]
                if mesh == other_mesh and (
                    a == b or (a < b + other_size and b < a + size)
                ):
                    actual.append([i, j, b - a])
        return actual == expected

    def bound(self, lower, after=None, cores=None):
        """The bound wire bytes. Without resident cores the result depends
        only on this immutable package, so a second bind (the runtime's
        validation, then the card's build) reuses the first."""
        if cores is not None:
            return self._bind(lower, after, cores)
        memo = self.__dict__.setdefault("_bound", {})
        if (lower, after) not in memo:
            memo[(lower, after)] = self._bind(lower, after, None)
        return memo[(lower, after)]

    def _bind(self, lower, after, cores):
        return rs.bind(
            self.package,
            self.bindings,
            self.machine_json,
            self.mesh,
            lower,
            after,
            translations=self.translations,
            cores=cores,
            images_json=self.images_json,
            template_json=self.template_json,
            params=self.template_params,
        )


def prebuilt(progs):
    return bool(progs) and all(isinstance(p, Prebuilt) for p in progs.values())


def placed(program, nodes):
    """Each node's program: a bare program, or one keyed ``None`` (unplaced),
    runs on the first node."""
    progs = program if isinstance(program, dict) else {None: program}
    return {nodes[0] if n is None else n: p for n, p in progs.items()}


def admission_cached():
    """`KOHAKUTPU_ADMISSION_CONTEXT` "1" (default): admission reuses the
    receiving board's cached context; any other value admits from scratch.
    Read once by each card at construction (`NodeCard.admission_cached`)."""
    return os.environ.get(ADMISSION_ENV, "1") == "1"


def admit(raw, machine, mesh=0, bindings=(), cached=True):
    """Receiving-board reliability policy cannot be disabled by checker mode."""
    _admit(raw, machine, 0 if mesh is None else mesh, tuple(bindings or ()), cached)


@lru_cache(maxsize=512)
def _admit(raw, machine, mesh, bindings, cached=True):
    # The verdict remains keyed by exact bound bytes, never an unbound entry.
    if cached:
        _rs.package_admit(raw, machine, mesh, list(bindings))
    else:
        _rs.package_admit(raw, machine, mesh, list(bindings), cached_context=False)


def built(
    progs, machine, fetch=None, residents=None, after=None, resident=False, cached=True
):
    """Each node's (plain, lowered, bindings). `resident` False: the node's
    `residents` entry is forgotten and the lowered package carries its
    vector images whole; True: images the cores already hold are omitted.
    `cached` False: admission builds the receiving board's context from
    scratch and no launch recipe reuses a verdict."""
    if not prebuilt(progs):
        raise TypeError("a run contains only Rust Prebuilt packages")
    want = None if fetch is None else tuple(fetch)
    out = {}
    for n, p in progs.items():
        if p.fetch != want:
            raise ValueError(
                f"node {n}: the Rust package was built for fetch port {p.fetch}, the runtime's is {want}"
            )
        cores = None if residents is None else residents.get(n)
        if cores is not None and not resident:
            cores.forget()
            cores = None
        mesh = p.mesh if n is None else n
        # Diagnostics are self-contained; only the submitted package advances state.
        # LaunchRecipe admits with admit_package on cold launch and reuses the verdict
        # only when invariant() proves patches touch only uninspected address bits.
        if p.recipe is not None and after is None and cached:
            if p.machine_json != machine.machine_json or p.mesh != mesh:
                raise ValueError("launch recipe belongs to another receiving machine")
            low = p.recipe.launch(p.bindings, p.translations, cores)
        else:
            low = p.bound(True, after, cores=cores)
            admit(low, machine.machine_json, mesh, cached=cached)
        out[n] = (
            Package(
                lambda p=p, after=after: p.bound(False, after), p.machine_json, p.mesh
            ),
            Package(low, p.machine_json, p.mesh),
            [],
        )
    return out


def findings(progs, mode):
    if mode not in GATES:
        raise ValueError(f"check mode {mode!r}: one of {GATES}")
    if mode == "off":
        return []
    if not prebuilt(progs):
        raise TypeError("checker verdict requires Rust Prebuilt packages")
    first = next(iter(progs.values()))
    found = list(first.findings)
    if mode == "refuse" and first.refused:
        raise Refused(found)
    return found
