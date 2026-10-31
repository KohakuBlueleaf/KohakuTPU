"""Runtime topology and addresses from the Rust machine view."""

import json
import os
from dataclasses import dataclass, field

from kohakutpu.compiler import _rs
from kohakutpu.language import machine as MF


@dataclass(frozen=True)
class MeshSpec:
    index: int
    units: dict
    mem_ports: tuple
    agent: tuple

    def has(self, kind):
        return bool(self.units.get(kind))


@dataclass(frozen=True)
class MachineSpec:
    name: str
    board: str
    meshes: tuple
    links: tuple
    fetch: tuple
    inst_depth: int
    stage_flits: int
    stage_bytes: int
    params: dict
    machine_json: str = field(repr=False)
    default: int = 0

    def mesh(self, index=None):
        want = self.default if index is None else index
        for mesh in self.meshes:
            if mesh.index == want:
                return mesh
        raise ValueError(f"machine {self.board!r} has no mesh {want}")

    @property
    def units(self):
        return self.mesh().units

    @property
    def mem_ports(self):
        return self.mesh().mem_ports

    @property
    def agent(self):
        return self.mesh().agent

    def count(self, kind, mesh=None):
        return len(self.mesh(mesh).units.get(kind, ()))

    def coords(self, kind, mesh=None):
        found = self.mesh(mesh).units.get(kind, ())
        if not found:
            raise ValueError(f"machine {self.board!r} has no {kind} on mesh {mesh}")
        return found

    def has(self, kind, mesh=None):
        return self.mesh(mesh).has(kind)

    def stage_addr(self, offset, mesh=None):
        index = self.mesh(mesh).index
        return _rs.stage_addr(self.machine_json, offset, index)

    def global_addr(self, base, mesh=None):
        index = self.mesh(mesh).index
        return _rs.global_addr(self.machine_json, base, index)


def machine_of(
    board,
    nodes=None,
    units=None,
    packing_backend=None,
    packing_slots=None,
    packing_cores=None,
    packing_looped=None,
):
    """A named board's schema, optionally restricted to enumerated units/nodes."""
    name = board if isinstance(board, str) else board["name"]
    text = (MF.MACHINES / f"{name}.json").read_text(encoding="utf-8")
    doc = json.loads(text)
    facts = doc["machine"]
    packing_backend = packing_backend or os.environ.get("KTPU_FP16_MX7_BACKEND")
    if packing_backend:
        policy = facts["conversion"]["fp16_mx7"]
        if packing_backend not in policy["allowed_backends"]:
            raise ValueError(f"board excludes FP16-to-MX7 backend {packing_backend!r}")
        policy["preferred_backend"] = packing_backend
        text = json.dumps(doc)
    policy = facts["conversion"]["fp16_mx7"]
    for key, explicit, env in (
        ("vector_slots", packing_slots, "KTPU_MX7_SLOTS"),
        ("vector_cores", packing_cores, "KTPU_MX7_CORES"),
        ("vector_looped", packing_looped, "KTPU_MX7_LOOPED"),
    ):
        value = explicit if explicit is not None else os.environ.get(env)
        if value is not None:
            value = int(value)
            if key == "vector_looped":
                if value not in (0, 1):
                    raise ValueError(f"{env} is 0 or 1")
                value = bool(value)
            policy[key] = value
    text = json.dumps(doc)
    view = _rs.machine_view(text, nodes, units)
    if nodes is not None:
        facts["card"].update(nodes=nodes, chain=list(range(nodes)))
    if units is not None:
        for kind in ("MG", "VC", "SR", "SIMT"):
            coords = units.get(kind, ())
            facts["die"]["units"][kind.lower()] = coords
            facts[kind.lower()]["count"] = len(coords)
    view["meshes"] = tuple(
        MeshSpec(
            m["index"],
            {k: tuple(tuple(c) for c in v) for k, v in m["units"].items()},
            tuple(tuple(c) for c in m["mem_ports"]),
            tuple(m["agent"]),
        )
        for m in view["meshes"]
    )
    view["links"] = tuple(tuple(link) for link in view["links"])
    view["fetch"] = tuple(view["fetch"])
    return MachineSpec(**view, machine_json=json.dumps(doc))
