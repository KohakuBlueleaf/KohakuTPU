"""Several meshes at once: one Device each, and the values spread across them.

Level 5, ABOVE :class:`kohakutpu.rt.Device`. One Device IS one mesh -- its own
arena, control window and ``MachineSpec.default`` -- so "which mesh" is a
property of the VALUE. Nothing chooses anything here: a split is stated, and a
spec a kernel cannot honour is REFUSED rather than repaired.

:class:`Plan` says which rank runs what and :meth:`MeshGroup.run` says when,
which today is one JTAG kick after another.

Measured 2026-08-13: column-parallel over four meshes is 3.98x compute, a
contraction split over two 1.76x at one fp16 ULP, and wall time worse than one
mesh either way -- all of it transport.
"""

from dataclasses import dataclass, field

import numpy as np
from kohakuaccel.collective import converge
from kohakuaccel.rt import Runtime
from kohakutpu.isa import ISA
from kohakutpu.ops import matmul as _matmul
from kohakutpu.rt import Device, Tensor

WORD_BYTES = 32

#: Sub-tiles one accumulator holds. Per-bitstream and absent from `MachineSpec`:
#: `boards/` ship_3x2 and v2_mesh0 say 512, ship_2x2 and singlemesh_2x2 say 256.
ACC_TILES = 512


# ------------------------------------------------------------------- the spec
@dataclass(frozen=True)
class Spec:
    """Where a logical array's elements are, across a group's ranks.

    `axis` names the axis split one slice per rank; None means every rank holds
    the whole array. `partial` marks a summand instead: every rank holds a
    WHOLE-SHAPED piece of a sum nothing has added up yet, which is what a
    contraction split produces and what a reduce consumes.

    Raises :class:`ValueError` for a spec that is both.
    """

    axis: int | None = None
    partial: bool = False

    def __post_init__(self):
        if self.axis is not None and self.partial:
            raise ValueError(
                "a value cannot be both split and partial: a partial summand is "
                "whole-shaped on every rank by definition"
            )

    @staticmethod
    def on(axis: int) -> "Spec":
        """A value split along `axis`, one slice per rank."""
        return Spec(axis=axis)

    @property
    def split(self) -> bool:
        """Whether this names an axis rather than a copy or a summand."""
        return self.axis is not None

    def local(self, shape: tuple, ranks: int) -> tuple:
        """The shape ONE rank holds, given the logical `shape`.

        Raises :class:`ValueError` for an axis the shape does not have, and for
        one that does not divide: the ranks would hold different shapes and a
        kernel compiled per rank would disagree about its extents silently.
        """
        if not self.split:
            return tuple(shape)
        axis = self.axis
        if not -len(shape) <= axis < len(shape):
            raise ValueError(
                f"axis {axis} does not exist on a value of shape {tuple(shape)}"
            )
        if shape[axis] % ranks:
            raise ValueError(
                f"a value of shape {tuple(shape)} does not split evenly on axis "
                f"{axis} over {ranks} meshes: {shape[axis]} is not a multiple of "
                f"{ranks}. Pad it to {-(-shape[axis] // ranks) * ranks} first"
            )
        out = list(shape)
        out[axis] //= ranks
        return tuple(out)

    def slices(self, array, ranks: int) -> list:
        """`array` cut into one piece per rank, in rank order."""
        self.local(np.shape(array), ranks)
        if not self.split:
            return [array] * ranks
        return list(np.split(np.asarray(array), ranks, axis=self.axis))

    def __str__(self) -> str:
        if self.partial:
            return "a partial sum on every mesh"
        return (
            "copied on every mesh" if not self.split else f"split on axis {self.axis}"
        )


#: Every rank holds the whole value.
COPIED = Spec()

#: Every rank holds a whole-shaped summand. Not readable until it is reduced.
PARTIAL = Spec(partial=True)


# ------------------------------------------------------------------ the value
class Sharded:
    """One logical array, held as one :class:`~kohakutpu.rt.Tensor` per rank.

    `shape` is the LOGICAL shape, whatever any single rank holds, and `spec` is
    how the pieces relate to it. The spec travels with the value because a
    kernel cannot check a split it cannot see.

    Raises :class:`ValueError` for a part count the group does not have.
    """

    def __init__(self, group: "MeshGroup", spec: Spec, parts, shape) -> None:
        self.group = group
        self.spec = spec
        self.parts = tuple(parts)
        self.shape = tuple(shape)
        if len(self.parts) != len(group):
            raise ValueError(
                f"a value on a {len(group)}-mesh group needs {len(group)} parts, "
                f"got {len(self.parts)}"
            )

    @property
    def meshes(self) -> tuple:
        """Which meshes hold the pieces, in rank order."""
        return tuple(p.mesh for p in self.parts)

    @property
    def local(self) -> tuple:
        """The shape one rank holds."""
        return self.spec.local(self.shape, len(self.group))

    def part(self, rank: int) -> Tensor:
        """Rank `rank`'s piece."""
        return self.parts[rank]

    def numpy(self) -> np.ndarray:
        """The whole logical array on the host, its pieces reassembled.

        Raises :class:`ValueError` for a partial sum: nothing has added the
        summands, so concatenating them and picking one are both wrong answers
        that look right.
        """
        if self.spec.partial:
            raise ValueError(
                "this value is a partial sum -- every mesh holds a whole-shaped "
                "summand and nothing has added them. Reduce it with "
                "MeshGroup.matmul_reduce(...) on two adjacent meshes or "
                "matmul_chain_reduce(...) on more, both of which add at full "
                "accumulator precision, or read `.parts` yourself"
            )
        if not self.spec.split:
            return self.parts[0].numpy()
        return np.concatenate([p.numpy() for p in self.parts], axis=self.spec.axis)

    def __len__(self) -> int:
        return len(self.parts)

    def __repr__(self) -> str:
        return f"Sharded{self.shape} [{self.spec}, meshes {list(self.meshes)}]"


# ------------------------------------------------------------------- the plan
@dataclass(frozen=True)
class Step:
    """One kernel call, on one rank, against that rank's own operands.

    `node` pins it to one unit. A step in a reduce must be pinned, because the
    meshes have to agree WHICH cluster holds the accumulator being added into
    and dispatch otherwise deals over whatever is idle.
    """

    rank: int
    kernel: object
    args: tuple
    knobs: dict = field(default_factory=dict)
    node: tuple | None = None


@dataclass(frozen=True)
class Plan:
    """What each rank runs and in what order, and nothing about WHEN.

    `spec` and `shape` describe the result. `into` and `tile` describe a reduce:
    every step but the first drains across the interlink into rank `into`'s
    cluster at `tile`, and THE FIRST STEP IS WHAT OPENS THAT TILE.

    That order is correctness. ``dbuf=2`` is `OP_ADD_PEER` and tile memory has
    no reset, so a burst arriving at a cluster that has run no GEMM is added to
    leftovers and saturates to 65504 (isa/cluster.md §9.4).
    """

    steps: tuple
    spec: Spec
    shape: tuple
    into: int | None = None
    tile: tuple | None = None
    #: Whether this plan's own first step opens `tile`. False for one hop of a
    #: chain reduce, where an earlier plan's GEMM opened every tile in the walk.
    opens: bool = True

    def __repr__(self) -> str:
        where = "" if self.into is None else f" -> rank {self.into}{self.tile}"
        return f"Plan({len(self.steps)} steps, {self.spec}{where})"


# ------------------------------------------------------------------ the group
class MeshGroup:
    """Several devices driven as one, and the values spread across them.

    One device per mesh, in rank order. A rank is a position in this group and
    the mesh index is what the silicon calls the same thing; they agree on
    hardware but not under the models, where every device answers for mesh 0.
    """

    def __init__(self, devices) -> None:
        """Take `devices` as the group's ranks, in order.

        Raises :class:`ValueError` for an empty group, for one device listed
        twice, and for two devices addressing the same bytes -- which on one
        card is two `Device`s opened on one mesh, and is silent corruption
        rather than an error.
        """
        self.devices = tuple(devices)
        if not self.devices:
            raise ValueError("a mesh group needs at least one device")
        for i, a in enumerate(self.devices):
            for j, b in enumerate(self.devices[i + 1 :], i + 1):
                if a is b:
                    raise ValueError(f"ranks {i} and {j} are the same device")
                if _collides(a, b):
                    raise ValueError(
                        f"ranks {i} and {j} address the same bytes -- one "
                        f"transport, arenas overlapping at "
                        f"{max(a.arena.base, b.arena.base):#x}. Two devices on "
                        f"one mesh hand out each other's memory and neither can "
                        f"tell"
                    )

    @classmethod
    def open(cls, card, meshes=None, **kw) -> "MeshGroup":
        """One :class:`~kohakutpu.rt.Device` per mesh of `card`, in order.

        `meshes` is which mesh indices to take, defaulting to every mesh the
        card enumerated; `kw` goes to each `Device`. Returns the group.
        """
        want = [m.index for m in card.meshes] if meshes is None else list(meshes)
        devices = []
        for index in want:
            card.select(index)
            devices.append(Device(card, **kw))
        return cls(devices)

    def __len__(self) -> int:
        return len(self.devices)

    def __getitem__(self, rank: int) -> Device:
        return self.devices[rank]

    def __iter__(self):
        return iter(self.devices)

    @property
    def meshes(self) -> tuple:
        """The mesh index behind each rank, in rank order."""
        return tuple(d.machine.default for d in self.devices)

    # -------------------------------------------------------- putting values on
    def put(self, array, spec: Spec) -> Sharded:
        """Upload `array` across the group under `spec`.

        A split is an ALLOCATION, not a view: each mesh owns its own DRAM, so
        rank `r` gets its own upload of its own slice and nothing is shared.
        """
        pieces = spec.slices(np.asarray(array), len(self))
        parts = [d.tensor(p) for d, p in zip(self.devices, pieces)]
        return Sharded(self, spec, parts, np.shape(array))

    def copy(self, array) -> Sharded:
        """Upload the whole of `array` to every mesh."""
        return self.put(array, COPIED)

    def split(self, array, axis: int) -> Sharded:
        """Upload `array` cut along `axis`, one slice per mesh."""
        return self.put(array, Spec.on(axis))

    def gather(self, value: Sharded) -> np.ndarray:
        """The whole logical array on the host. Refuses a partial sum."""
        self._mine(value, "gather")
        return value.numpy()

    def stats(self) -> list:
        """Each rank's memory and link counters, in rank order."""
        return [d.stats() for d in self.devices]

    # ------------------------------------------------------------------ planning
    def plan_each(self, kernel, *args, spec: Spec | None = None, **knobs) -> Plan:
        """Run `kernel` per mesh, each rank against its own pieces.

        Every operand must carry the SAME spec, which the result then carries
        unless `spec` says otherwise. Nothing crosses a link here, so operands
        split two different ways would need a collective nobody asked for.

        Returns a :class:`Plan`. Raises :class:`ValueError` for disagreeing
        specs, a partial operand, an operand from another group, and a plain
        `Tensor` -- which lives on one mesh, where the other ranks cannot read
        it.
        """
        held = None
        for i, arg in enumerate(args):
            self._mine(arg, kernel_name(kernel), i)
            if held is None:
                held = arg.spec
            elif arg.spec != held:
                raise ValueError(
                    f"{kernel_name(kernel)}: operand {i} is {arg.spec} and an "
                    f"earlier one is {held}. This runs one kernel per mesh and "
                    f"moves nothing between them, so the operands have to be cut "
                    f"the same way; re-state one of them"
                )
        if held is None:
            raise ValueError(f"{kernel_name(kernel)}: nothing to run it against")
        if held.partial:
            raise ValueError(
                f"{kernel_name(kernel)}: an operand is a partial sum, which is "
                f"not the value it stands for. Reduce it first"
            )
        steps = tuple(
            Step(r, kernel, tuple(a.parts[r] for a in args), dict(knobs))
            for r in range(len(self))
        )
        return Plan(steps, spec or held, args[0].shape)

    def plan_matmul(self, a: Sharded, b: Sharded, **tiling) -> Plan:
        """``a @ b.T`` per mesh, with the result's spec read off the operands.

        `b` is [N][K], so its axis 0 is the output columns and its axis 1 the
        contraction: the same axis NUMBER means two different collectives on the
        two operands. Three pairings are legal -- copied against split-0 gives
        split-1, split-0 against copied gives split-0, and split-1 against
        split-1 gives a partial needing a reduce.

        Returns a :class:`Plan`. Raises :class:`ValueError` on any other
        pairing, on operands whose contractions differ, and on one from another
        group.
        """
        self._mine(a, "matmul", 0)
        self._mine(b, "matmul", 1)
        if a.shape[-1] != b.shape[-1]:
            raise ValueError(
                f"matmul contracts a{a.shape} against b{b.shape}, whose last "
                f"axes are {a.shape[-1]} and {b.shape[-1]}"
            )
        spec = pairing(a.spec, b.spec)
        steps = tuple(
            Step(r, _matmul, (a.parts[r], b.parts[r]), dict(tiling))
            for r in range(len(self))
        )
        return Plan(steps, spec, (a.shape[0], b.shape[0]))

    def chain_order(self) -> list:
        """This group's ranks in FABRIC order, an end first.

        A reduction walks this, neighbour into neighbour, so every hop is one
        link -- the invariant that holds on a ring, on a chain, and on a chain
        that forwards. Rank order is not fabric order: the meshes sit
        0, 1, 3, 2 along the SLR stack.

        Raises :class:`ValueError` for a group whose meshes are not one path;
        a fork has no single walk and the caller has to say what it wants.
        """
        rank = {self.devices[r].machine.default: r for r in range(len(self))}
        return [rank[m] for m in self.devices[0].machine.path(rank)]

    def adjacent(self, src: int, dst: int) -> bool:
        """Whether a transfer from rank `src` to rank `dst` crosses ONE link.

        The invariant every plan here must hold. A schedule that only moves
        between neighbours runs on a ring, on a chain, and on a chain that
        forwards -- so it is the one rule that survives the fabric changing.
        A non-adjacent transfer needs forwarding, and where there is none it
        does not arrive slowly, it does not arrive.
        """
        spec = self.devices[src].machine
        here, there = spec.default, self.devices[dst].machine.default
        return spec.mesh_hops(here, there) <= 1

    def plan_reduce(
        self,
        a: Sharded,
        b: Sharded,
        into: int | None = None,
        tile=None,
        nodes=None,
        **tiling,
    ) -> Plan:
        """A contraction split, summed into ONE cluster on rank `into`.

        The partial crosses as the accumulator's own 352-bit float under
        ``dbuf=2``, so the sum costs no fp16 round trip. `into`'s step runs
        FIRST and is what opens the destination tile; `tile` is that cluster and
        `nodes` maps a rank to where it runs, both defaulting to the mesh's
        first cluster.

        Returns a :class:`Plan`. Raises :class:`ValueError` unless both operands
        are split on the contraction, for a group of one, and for a tiling whose
        grid is more than one instance -- the accumulator is ONE cluster's.
        """
        plan = self.plan_matmul(a, b, **tiling)
        if plan.spec != PARTIAL:
            raise ValueError(
                f"a reduce needs a contraction split -- both operands split on "
                f"axis 1 -- but these are {a.spec} and {b.spec}, which give "
                f"{plan.spec} and need no reduce at all"
            )
        if len(self) < 2:
            raise ValueError("a reduce over one mesh crosses no link; drop it")
        if into is None:
            into = 0
        if not 0 <= into < len(self):
            raise ValueError(f"rank {into} is not one of this group's {len(self)}")
        far = [r for r in range(len(self)) if r != into and not self.adjacent(r, into)]
        if far:
            raise ValueError(
                f"ranks {far} are more than one link from rank {into}, and this "
                f"plan has every rank send its partial straight there. Without "
                f"forwarding that transfer never arrives; with it, it crosses a "
                f"mesh that is doing its own work. Use plan_chain_reduce, which "
                f"walks neighbour into neighbour so every hop is one link"
            )
        where = dict(nodes or {})
        where.setdefault(into, tile or self.devices[into].machine.coords("MG")[0])
        pinned = [
            Step(
                s.rank,
                s.kernel,
                s.args,
                s.knobs,
                where.get(s.rank) or self.devices[s.rank].machine.coords("MG")[0],
            )
            for s in plan.steps
        ]
        for step in pinned:
            self._one_instance(step)
            self._fits_the_accumulator(step)
        head = pinned.pop(into)
        return Plan((head, *pinned), PARTIAL, plan.shape, into=into, tile=head.node)

    def plan_chain_reduce(
        self, a: Sharded, b: Sharded, into: int | None = None, nodes=None, **tiling
    ) -> list:
        """A contraction split summed ALONG THE FABRIC, one link per hop.

        What :meth:`plan_reduce` cannot do past two meshes: there every rank
        drains straight to `into`, and on a chain the far ranks are two links
        away. Here the first plan runs every rank's GEMM locally -- which is
        what opens all the tiles -- and each later plan is ONE hop, re-draining
        a cluster's accumulator into its neighbour's under ``dbuf=2``.

        `into` defaults to the far end of the walk. Returns the plans in the
        order :meth:`run` must take them; raises what :meth:`plan_reduce` does.
        """
        plan = self.plan_matmul(a, b, **tiling)
        if plan.spec != PARTIAL:
            raise ValueError(
                f"a chain reduce needs a contraction split -- both operands "
                f"split on axis 1 -- but these are {a.spec} and {b.spec}, which "
                f"give {plan.spec} and need no reduce at all"
            )
        if len(self) < 2:
            raise ValueError("a reduce over one mesh crosses no link; drop it")
        walk = self.chain_order()
        into = walk[-1] if into is None else into
        if not 0 <= into < len(self):
            raise ValueError(f"rank {into} is not one of this group's {len(self)}")
        where = dict(nodes or {})

        def at(rank: int):
            return where.get(rank) or self.devices[rank].machine.coords("MG")[0]

        opened = tuple(
            Step(s.rank, s.kernel, s.args, s.knobs, at(s.rank)) for s in plan.steps
        )
        for step in opened:
            self._one_instance(step)
            self._fits_the_accumulator(step)
        words = int(np.prod(plan.shape)) * 2 // WORD_BYTES
        plans = [Plan(opened, PARTIAL, plan.shape)]
        for src, dst in converge(walk, into):
            hop = Step(src, _forward(self.devices[src], words), (), {}, at(src))
            plans.append(
                Plan((hop,), PARTIAL, plan.shape, into=dst, tile=at(dst), opens=False)
            )
        return plans

    # ----------------------------------------------------------------- running
    def run(self, plan: Plan) -> list:
        """Execute `plan`'s steps in order and return what each returned.

        Sequencing lives here and nowhere else. Every step is awaited before the
        next is kicked, so the meshes run strictly one after another and the
        parallelism a plan expresses cannot be collected: `Program.kick` polls
        over JTAG at ~13 ms against a ~362 us program.

        Raises :class:`ValueError` when a reduce plan does not open its
        destination tile first.
        """
        if plan.into is not None and plan.opens:
            head = plan.steps[0]
            if head.rank != plan.into or head.node != plan.tile:
                raise ValueError(
                    f"a reduce plan must run rank {plan.into} at {plan.tile} "
                    f"FIRST: dbuf=2 ADDS into a resident tile and tile memory "
                    f"has no reset, so a burst arriving before a GEMM opened "
                    f"those sub-tiles lands on leftovers and saturates to 65504"
                )
        out = []
        for step in plan.steps:
            dev = self.devices[step.rank]
            patch = None
            if plan.into is not None and step.rank != plan.into:
                patch = _aimed_at(dev, self.devices[plan.into], plan.tile)
            if step.node is None:
                out.append(step.kernel(*step.args, **step.knobs))
                continue
            with Pin(dev, step.node, patch):
                out.append(step.kernel(*step.args, **step.knobs))
        return out

    def each(self, kernel, *args, spec: Spec | None = None, **knobs) -> Sharded:
        """Run `kernel` per mesh and collect the results. See :meth:`plan_each`."""
        plan = self.plan_each(kernel, *args, spec=spec, **knobs)
        return Sharded(self, plan.spec, self.run(plan), plan.shape)

    def matmul(self, a: Sharded, b: Sharded, **tiling) -> Sharded:
        """``a @ b.T`` per mesh. See :meth:`plan_matmul` for the pairings."""
        plan = self.plan_matmul(a, b, **tiling)
        return Sharded(self, plan.spec, self.run(plan), plan.shape)

    def matmul_reduce(
        self, a: Sharded, b: Sharded, into: int, tile=None, nodes=None, **tiling
    ) -> Tensor:
        """``a @ b.T`` with the contraction split, summed on rank `into`.

        ONE call and not a matmul then a reduce: a partial already drained to
        memory is fp16 and its accumulator is gone, so the drain has to be aimed
        across the link as it is emitted.

        Returns rank `into`'s own result tensor, re-drained once the partials
        arrived, so the sum never leaves the card. Raises :class:`ValueError`
        for :meth:`plan_reduce`'s refusals and for a device with no card behind
        it -- the models decode a node-addressed DRAIN but have no interlink.
        """
        self._on_hardware("a cross-mesh drain")
        plan = self.plan_reduce(a, b, into, tile=tile, nodes=nodes, **tiling)
        held = self.run(plan)[0]
        return _readout(self.devices[into], held, plan.tile, plan.shape)

    def matmul_chain_reduce(
        self, a: Sharded, b: Sharded, into: int | None = None, nodes=None, **tiling
    ) -> Tensor:
        """``a @ b.T`` contraction-split, summed along the fabric. See
        :meth:`plan_chain_reduce`.

        Returns the destination rank's own result tensor, re-drained once every
        hop has landed. Raises :class:`ValueError` for a device with no card
        behind it, as :meth:`matmul_reduce` does.
        """
        self._on_hardware("a chain reduce")
        plans = self.plan_chain_reduce(a, b, into, nodes=nodes, **tiling)
        held = dict(zip((s.rank for s in plans[0].steps), self.run(plans[0])))
        for hop in plans[1:]:
            self.run(hop)
        last = plans[-1]
        return _readout(self.devices[last.into], held[last.into], last.tile, last.shape)

    def _on_hardware(self, what: str) -> None:
        """Refuse a cross-mesh plan on a device with no card behind it.

        Raises :class:`ValueError`: `kohakutpu.model` decodes a node-addressed
        DRAIN but has no interlink, so `dmesh` and `dfin` would be encoded and
        then ignored -- a wrong answer with nothing reporting it.
        """
        for rank, dev in enumerate(self.devices):
            if getattr(dev, "mesh", None) is None:
                raise ValueError(
                    f"{what} is hardware only, and rank {rank} has no card"
                )

    # ------------------------------------------------------------------ checking
    def _mine(self, value, what: str, at: int | None = None) -> None:
        """Refuse a value this group does not hold.

        Raises :class:`TypeError` for anything that is not a :class:`Sharded`,
        and :class:`ValueError` for one belonging to another group.
        """
        where = "" if at is None else f" operand {at}"
        if isinstance(value, Tensor):
            raise TypeError(
                f"{what}:{where} is a plain Tensor on mesh {value.mesh}. It lives "
                f"on one mesh and the other ranks cannot read it -- put it across "
                f"the group with group.copy(...) or group.split(...)"
            )
        if not isinstance(value, Sharded):
            raise TypeError(
                f"{what}:{where} is a {type(value).__name__}, not a Sharded value"
            )
        if value.group is not self:
            raise ValueError(
                f"{what}:{where} belongs to another group, on meshes "
                f"{list(value.meshes)} against this one's {list(self.meshes)}"
            )

    def _one_instance(self, step: Step) -> None:
        """Refuse a step whose grid needs more than one unit.

        Raises :class:`ValueError` naming the grid: a reduce adds into ONE
        cluster's accumulator, so a kernel dealt over several has no single tile
        for the far mesh to aim at.
        """
        grid = step.kernel.plan(*step.args, **step.knobs).grid
        if int(np.prod(grid)) != 1:
            raise ValueError(
                f"a reduce needs a kernel that runs on ONE cluster, but this "
                f"tiling gives a grid of {tuple(grid)} on rank {step.rank}. "
                f"Raise gm/gn until the whole output tile fits one accumulator, "
                f"but no further than gm*gn = {self._tiles(step.rank)} sub-tiles"
            )

    def _fits_the_accumulator(self, step: Step) -> None:
        """Refuse a step whose resident tile is deeper than the accumulator.

        Raises :class:`ValueError` naming both counts. `_one_instance` bounds the
        GRID, which is a different quantity: `gm*gn` past the accumulator's depth
        compiles clean, models clean, and WRAPS the tile address on the card.
        """
        depth = self._tiles(step.rank)
        want = int(step.knobs.get("gm", 8)) * int(step.knobs.get("gn", 8))
        if want > depth:
            raise ValueError(
                f"this tiling asks one cluster to hold gm*gn = {want} sub-tiles "
                f"on rank {step.rank}, where the accumulator holds {depth}. The "
                f"tile address wraps silently past that, so the reduce would add "
                f"into the wrong sub-tiles and report success"
            )

    def _tiles(self, rank: int) -> int:
        """Sub-tiles rank `rank`'s accumulator holds. See :data:`ACC_TILES`."""
        return int(getattr(self.devices[rank].machine, "tiles", ACC_TILES))

    def __repr__(self) -> str:
        return f"MeshGroup(ranks {len(self)}, meshes {list(self.meshes)})"


def pairing(a: Spec, b: Spec) -> Spec:
    """What ``a @ b.T`` produces, given the operands' specs.

    Raises :class:`ValueError` for a pairing with no collective-free reading --
    two output axes split across one set of ranks, or one operand splitting the
    contraction while the other does not.
    """
    if a.partial or b.partial:
        raise ValueError(
            f"matmul takes no partial sum: a is {a} and b is {b}. A partial is "
            f"not the value it stands for; reduce it first"
        )
    if not a.split and not b.split:
        return COPIED
    if not a.split and b.axis == 0:
        return Spec.on(1)
    if a.axis == 0 and not b.split:
        return Spec.on(0)
    if a.axis == 1 and b.axis == 1:
        return PARTIAL
    raise ValueError(
        f"a matmul of a {a} against b {b} is not a split this layer will run. "
        f"`b` is [N][K], so the three that work are: a copied against b split on "
        f"axis 0 (output columns), a split on axis 0 against b copied (rows), and "
        f"both split on axis 1 (the contraction, which then needs a reduce)"
    )


def kernel_name(kernel) -> str:
    """What to call `kernel` in a message."""
    return getattr(kernel, "name", None) or getattr(kernel, "__name__", "kernel")


def _collides(a, b) -> bool:
    """Whether two devices address the same bytes.

    One transport and overlapping arenas, which on a card is two `Device`s
    opened on one mesh. Devices on different meshes share a transport and do not
    overlap, because an arena address carries its mesh in bits [33:32].
    """
    if a.transport is not b.transport:
        return False
    return (
        a.arena.base < b.arena.base + b.arena.size
        and b.arena.base < a.arena.base + a.arena.size
    )


# --------------------------------------------------------------- the mechanism
# One level down: pinning a dispatch, and the DRAIN word that leaves the mesh.
class Pin:
    """Dispatch every kernel on one device to ONE unit while this is open.

    `Device.dispatch` deals a kernel over whatever is idle, so two meshes
    cooperating on one accumulator cannot agree which cluster holds it. `patch`
    rewrites the last flit of every instance, which is how an ordinary drain
    becomes a remote one. Shadows the bound method on the instance, so the
    device keeps its one arena and every tensor on it stays valid.
    """

    def __init__(self, dev, node, patch=None) -> None:
        self.dev, self.node, self.patch = dev, node, patch
        self._saved = None

    def __enter__(self):
        self._saved = self.dev.__dict__.get("dispatch")
        self.dev.dispatch = self._dispatch
        return self.dev

    def __exit__(self, *exc) -> bool:
        if self._saved is None:
            del self.dev.dispatch
        else:
            self.dev.dispatch = self._saved
        return False

    def _dispatch(self, payloads, unit, name="kernel", nodes=None, acks=None) -> int:
        """Place every instance on this pin's node, patching the tail first.

        Calls `Runtime.dispatch` rather than the device's, whose narrowing to
        idle units would overrule the pin. Returns the rounds executed.
        """
        if self.patch is not None:
            payloads = {k: [*w[:-1], self.patch(w[-1])] for k, w in payloads.items()}
        return Runtime.dispatch(self.dev, payloads, unit, name, [self.node], acks)


def _forward(dev, words: int):
    """A step that re-drains this cluster's accumulator, for :meth:`run` to aim.

    Not a kernel: the sum is in the accumulator and nothing is going to run
    again to carry it, so the step IS one DRAIN word. `Pin` rewrites the last
    word of every payload, the same mechanism that aims a kernel's own final
    drain, so a hop needs no second path through the ISA.
    """

    def hop():
        return dev.dispatch({(0, 0): [ISA.drain(addr=0, n=words)]}, "MG", "reduce:hop")

    return hop


def _aimed_at(src, dst, fin):
    """A patch turning a stage's last DRAIN into one landing in `dst` at `fin`."""
    agent = src.machine.agent
    mesh = dst.machine.default
    return lambda word: _remote(word, agent, mesh, fin)


def _remote(word: int, agent, dst_mesh: int, fin) -> int:
    """One DRAIN word aimed at `fin` in `dst_mesh`. Raises if it is not a DRAIN.

    `dst` is this mesh's own MAG port so the local routers see an ordinary
    local-destination flit; `dfin` nonzero is what makes it remote. The ack must
    be named: across meshes `(0,0)` means "answer the sender", and the sender's
    coordinate exists in the far mesh too.
    """
    name, f = ISA.set.decode(word)
    if name != "DRAIN":
        raise ValueError(f"the stage's last word is a {name}, not a DRAIN")
    f.pop("op")
    f.update(
        addr=0,  # granule 0; buf 2 is a sub-tile pair, so it must be even
        dnode=1,
        dst_x=agent[0],
        dst_y=agent[1],
        dbuf=2,
        dflags=1,
        dack_x=agent[0],
        dack_y=agent[1],
        dmesh=dst_mesh,
        dfin=(fin[1] & 0xF) << 4 | (fin[0] & 0xF),
    )
    return ISA.DRAIN.encode(**f)


def _readout(dev, out: Tensor, tile, shape) -> Tensor:
    """Drain `tile`'s resident accumulator back over `out`'s own buffer.

    The sum is in the accumulator, not in memory: `out` holds only the
    destination's own partial, drained before the others arrived. Re-draining
    writes the total where the kernel already wrote once, in the layout `out`
    was allocated in, so nothing crosses the host. Returns `out`.

    Raises :class:`ValueError` when the buffer is not exactly the logical
    result, which means the layout padded it and the drain would land
    misaligned, and when the tensor is held in more than one order, where
    picking either would be a guess.
    """
    if len(out.buffers) != 1:
        raise ValueError(
            f"the result is held in {len(out.buffers)} byte orders "
            f"({sorted(out.buffers)}); a re-drain has to name one and either "
            f"choice would be a guess"
        )
    buf = next(iter(out.buffers.values()))
    want = int(np.prod(shape)) * 2
    if buf.nbytes != want:
        raise ValueError(
            f"the result buffer is {buf.nbytes} bytes where {tuple(shape)} in "
            f"fp16 is {want}: the output does not exactly fill one accumulator, "
            f"so re-draining it would land misaligned"
        )
    out.host = None
    dev.dispatch(
        {(0, 0): [ISA.drain(addr=buf.addr, n=buf.nbytes // WORD_BYTES)]},
        "MG",
        "reduce:readout",
        [tile],
    )
    return out
