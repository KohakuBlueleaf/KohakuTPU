"""What the compiler needs of a machine: unit coordinates and round bounds.

Constructible from a driver target by duck typing, so neither package imports
the other.

A machine may carry SEVERAL MESHES, and they need not be alike: one board here
has six clusters on every mesh and vector cores on only three of them. So the
unit table is per mesh (:class:`MeshSpec`) and :class:`MachineSpec` forwards to
a default one, which is what keeps ``machine.coords("MG")`` meaning what it
meant when a machine had one mesh.
"""

from dataclasses import dataclass, field, replace

Coord = tuple[int, int]

#: A device address names its mesh in bits [33:32], so an address a unit issues
#: is GLOBAL. One mesh does not decode them, which is why a wrong one aliases.
MESH_SHIFT = 32
MESH_MASK = 0x3


@dataclass(frozen=True)
class MeshSpec:
    """One mesh's units, memory ports and orchestrator.

    A unit type this mesh does not carry is ABSENT from `units`, never present
    and empty: "no vector cores" is a fact about the silicon, and a placement
    that has to refuse should read it off the table rather than off a length.
    """

    index: int = 0
    units: dict[str, tuple[Coord, ...]] = field(default_factory=dict)
    mem_ports: tuple[Coord, ...] = ()
    #: Where the orchestrator sits. A unit-to-unit transfer's completion has to
    #: be aimed here or nothing consumes it and no status register moves.
    agent: Coord | None = None

    def __post_init__(self):
        object.__setattr__(
            self, "units", {k: tuple(v) for k, v in self.units.items() if tuple(v)}
        )
        object.__setattr__(self, "mem_ports", tuple(self.mem_ports))

    def has(self, unit_type: str) -> bool:
        return bool(self.units.get(unit_type))


@dataclass(frozen=True)
class MachineSpec:
    """Where the units are, and what bounds one dispatch round.

    `meshes` is every mesh; `default` is the index of the one the flat accessors
    answer for. `units`, `mem_ports` and `agent` are that mesh's, and passing
    them instead of `meshes` describes a one-mesh machine -- which is what every
    caller written before there were four is doing.

    `stage_flits` and `ncmd` are the staging RAM and command RAM depths;
    `inst_depth` is a unit's instruction FIFO, a correctness bound on in-flight
    instructions per unit. These bound a dispatch round and are the same on
    every mesh, so they stay here rather than on :class:`MeshSpec`.
    """

    name: str = "generic"
    units: dict[str, tuple[Coord, ...]] = field(default_factory=dict)
    stage_flits: int = 128
    ncmd: int = 128
    inst_depth: int = 32
    mem_ports: tuple[Coord, ...] = ()
    agent: Coord | None = None
    meshes: tuple[MeshSpec, ...] = ()
    #: The mesh INDEX the flat accessors answer for, not a position in `meshes`.
    default: int = 0
    #: Mesh index pairs a link joins, undirected. EMPTY MEANS FULLY CONNECTED,
    #: which is what a machine that never said otherwise has always assumed.
    links: tuple[tuple[int, int], ...] = ()

    def __post_init__(self):
        meshes = tuple(self.meshes) or (
            MeshSpec(self.default, self.units, self.mem_ports, self.agent),
        )
        object.__setattr__(self, "meshes", meshes)
        here = self.mesh()
        object.__setattr__(self, "units", here.units)
        object.__setattr__(self, "mem_ports", here.mem_ports)
        object.__setattr__(self, "agent", here.agent)

    @classmethod
    def from_target(
        cls,
        target,
        units=None,
        mem_ports=(),
        name=None,
        agent=None,
        meshes=(),
        default=0,
    ) -> "MachineSpec":
        """Take the round bounds off a driver target, read by attribute.

        Anything exposing `stage_flits`, `ncmd` and `inst_depth` will do. Give
        `units` for a one-mesh machine or `meshes` for several, not both.
        """
        return cls(
            name=name or getattr(target, "name", "generic"),
            units={k: tuple(v) for k, v in (units or {}).items()},
            stage_flits=getattr(target, "stage_flits", 128),
            ncmd=getattr(target, "ncmd", 128),
            inst_depth=getattr(target, "inst_depth", 32),
            mem_ports=tuple(mem_ports),
            agent=agent,
            meshes=tuple(meshes),
            default=default,
        )

    # ------------------------------------------------------------- the meshes
    def mesh(self, index: int | None = None) -> MeshSpec:
        """The mesh with that index, or the default one. Raises if it is absent."""
        want = self.default if index is None else index
        for m in self.meshes:
            if m.index == want:
                return m
        raise ValueError(
            f"machine {self.name!r} has no mesh {want}; it has "
            f"{[m.index for m in self.meshes]}"
        )

    def mesh_hops(self, src: int, dst: int) -> int:
        """LINKS a transfer from mesh `src` to mesh `dst` crosses.

        Not :meth:`hops`, which is the NoC distance between two coordinates
        inside one mesh. Different fabric, different question.

        Zero to itself, one to a neighbour. With no `links` every mesh is one
        hop from every other, which is what a fully connected fabric is and what
        every caller assumed before a topology could be stated.

        Raises :class:`ValueError` for a pair no chain of links joins -- on a
        fabric that does not forward, that pair cannot talk at all, and treating
        it as far rather than as unreachable hides the fault.
        """
        if src == dst:
            return 0
        if not self.links:
            return 1
        near: dict[int, list[int]] = {}
        for a, b in self.links:
            near.setdefault(a, []).append(b)
            near.setdefault(b, []).append(a)
        seen, edge, far = {src}, [src], 0
        while edge:
            far += 1
            nxt = []
            for at in edge:
                for peer in near.get(at, ()):
                    if peer == dst:
                        return far
                    if peer not in seen:
                        seen.add(peer)
                        nxt.append(peer)
            edge = nxt
        raise ValueError(
            f"machine {self.name!r} has no path from mesh {src} to mesh {dst}; "
            f"its links are {sorted(self.links)}"
        )

    def neighbours(self, mesh: int) -> tuple:
        """The meshes ONE link from `mesh`, in index order.

        The only transfers a schedule may assume: one that crosses two links
        needs forwarding, and a fabric without it drops the packet rather than
        delivering it late.
        """
        if not self.links:
            return tuple(m.index for m in self.meshes if m.index != mesh)
        near = {b for a, b in self.links if a == mesh}
        near |= {a for a, b in self.links if b == mesh}
        return tuple(sorted(near))

    def path(self, only=None) -> tuple:
        """Mesh indices in FABRIC order, an end first. Raises unless one path.

        This card's meshes sit 0, 1, 3, 2 along the SLR stack, so a schedule
        walking INDEX order crosses two links on every other hop. `only`
        narrows to a subset. A fork is refused: it has no single walk. With no
        `links` the fabric is complete and index order comes back.
        """
        want = only if only is not None else [m.index for m in self.meshes]
        held = {self.mesh(m).index for m in want}
        if not self.links:
            return tuple(sorted(held))
        near = {m: [p for p in self.neighbours(m) if p in held] for m in held}
        forks = sorted(m for m, ns in near.items() if len(ns) > 2)
        if forks:
            raise ValueError(
                f"meshes {sorted(held)} are not a path: mesh {forks[0]} has "
                f"{len(near[forks[0]])} neighbours among them, so a walk "
                f"visiting each once has no single answer"
            )
        ends = sorted(m for m, ns in near.items() if len(ns) <= 1)
        walk, seen = [ends[0] if ends else min(held)], set()
        while len(walk) < len(held):
            seen.add(walk[-1])
            nxt = [p for p in near[walk[-1]] if p not in seen]
            if not nxt:
                raise ValueError(
                    f"meshes {sorted(held)} are not connected: no link joins "
                    f"mesh {walk[-1]} to {sorted(held - set(walk))}"
                )
            walk.append(nxt[0])
        return tuple(walk)

    def for_mesh(self, index: int) -> "MachineSpec":
        """This machine seen from mesh `index`: same meshes, different default.

        A kernel is written against ONE mesh, so this is what a placement pass
        hands it -- and it keeps the other meshes visible, which a single-mesh
        copy would throw away.
        """
        self.mesh(index)
        return replace(self, default=index)

    def meshes_with(self, unit_type: str) -> tuple[int, ...]:
        """Indices of the meshes carrying `unit_type`, in order."""
        return tuple(m.index for m in self.meshes if m.has(unit_type))

    def has(self, unit_type: str, mesh: int | None = None) -> bool:
        """Whether that mesh carries `unit_type`. The question :meth:`coords` raises on."""
        return self.mesh(mesh).has(unit_type)

    def count(self, unit_type: str, mesh: int | None = None) -> int:
        """How many `unit_type` that mesh carries. Zero rather than a raise."""
        return len(self.mesh(mesh).units.get(unit_type, ()))

    # -------------------------------------------------------------- the units
    def coords(self, unit_type: str, mesh: int | None = None) -> tuple[Coord, ...]:
        """Coordinates carrying `unit_type` on that mesh. Raises if it has none.

        The message names the meshes that DO carry it, because on a machine
        whose meshes differ the useful answer is almost never "give up" -- it is
        "put this stage somewhere else".
        """
        here = self.mesh(mesh)
        found = here.units.get(unit_type, ())
        if found:
            return found
        elsewhere = self.meshes_with(unit_type)
        where = (
            f"meshes carrying {unit_type!r}: {list(elsewhere)}"
            if elsewhere
            else f"no mesh on this machine carries {unit_type!r}"
        )
        raise ValueError(
            f"machine {self.name!r} mesh_{here.index} has no {unit_type!r} units; "
            f"it has {sorted(here.units) or 'none at all'}, and {where}"
        )

    # ----------------------------------------------------------- the address map
    def global_addr(self, base: int, mesh: int | None = None) -> int:
        """`base` in that mesh's memory, as the address a unit issues.

        Every address in an instruction is global, so a LOCAL one has to say so
        too: bits [33:32] left at zero mean mesh 0, whichever mesh is running.
        That is why an ordinary matmul on mesh_2 raises `IL_F_RD_REMOTE` and is
        right anyway -- the request aliases to local DRAM instead of routing.

        Raises :class:`ValueError` for a mesh this machine does not have, or a
        `base` that does not fit one mesh's 4 GB.
        """
        where = self.mesh(mesh).index
        if base >> MESH_SHIFT:
            raise ValueError(
                f"{base:#x} does not fit one mesh's 4 GB; bits "
                f"[{MESH_SHIFT + 1}:{MESH_SHIFT}] are the mesh id, not address"
            )
        return (where << MESH_SHIFT) | base

    @staticmethod
    def addr_mesh(addr: int) -> int:
        """Which mesh's memory `addr` names."""
        return (addr >> MESH_SHIFT) & MESH_MASK

    @staticmethod
    def hops(a: Coord, b: Coord) -> int:
        """Hop count under XY dimension-order routing."""
        return abs(a[0] - b[0]) + abs(a[1] - b[1])

    def nearest_port(self, coord: Coord, mesh: int | None = None) -> Coord | None:
        """The memory port fewest hops from `coord`, or None if there are none."""
        ports = self.mesh(mesh).mem_ports
        if not ports:
            return None
        return min(ports, key=lambda p: self.hops(coord, p))
