"""The runtime: a machine you can put arrays on and run kernels against.

Level 2 downward. It owns the arena, moves bytes, and turns compiled instances
into dispatched rounds. A project subclasses this to say what a device-resident
value is; the framework allocates results and hands back what :meth:`empty`
returns.
"""

from typing import Protocol, runtime_checkable

from kohakuaccel.dispatch import plan
from kohakuaccel.machinespec import MachineSpec
from kohakuaccel.memory import Arena, Buffer, Layout
from kohakuaccel.package import engine as engine_package
from kohakuaccel.package import mover
from kohakuaccel.package.build import PackageBuilder
from kohakuaccel.package.format import signature
from kohakuaccel.package.lower import compile_stage, machine_units, unit_types


@runtime_checkable
class DeviceValue(Protocol):
    """What a kernel accepts as an operand and returns as a result."""

    shape: tuple

    @property
    def runtime(self) -> "Runtime":
        """The runtime holding this value."""

    def address(self, layout: Layout) -> int:
        """Where these contents are in `layout`, materialising them if needed."""


class Runtime:
    """Memory and dispatch for one attached machine."""

    #: Whether a call may put temps whose lifetimes do not overlap on the same
    #: bytes. Off, because turning it on moves real addresses.
    reuse_temps = False

    #: Whether a call may size an elementwise grid from the LAYOUT rather than
    #: from the traced extent, so idle units take a share. Off: it is a trade.
    regrid = False

    #: The node queue that runs packages (``run(package, bindings)``); None:
    #: the host executes every round.
    node = None
    #: The project backend whose `addresses` reports a word's buffer fields.
    fields = None
    #: Whether every package built is kept in :attr:`packages`.
    keep_packages = False
    #: Whether a package keeps its compiled addresses: it is built per call, and
    #: relocating on the node costs a pass (~11k cycles, 512^3 matmul on v9).
    bind_packages = True
    #: Whether a kick whose words step only in address fields goes as a REPEAT,
    #: which keeps the package -- and the node's copy of it -- from growing with K.
    compress_packages = True
    #: The memory port ``(x, y)`` that streams a unit's words out of the package,
    #: or None to send them through the node. Fetched words are plain, so a
    #: package built for it is neither compressed nor relocated.
    fetch_port = None
    #: Whether a package is lowered for the node's dispatch engine
    #: (package/engine.py): its steps become engine entries the node copies
    #: without decoding. Only a bound package can be; the node must have one.
    engine_packages = False

    def __init__(self, machine: MachineSpec, arena: Arena, transport, ctrl=None):
        self.machine = machine
        self.arena = arena
        #: Addresses the card's memory directly, for operand and result bytes.
        self.transport = transport
        #: Carries the driver's register map, for dispatch. Often the same thing
        #: rebased onto wherever the control agent landed.
        self.ctrl = ctrl if ctrl is not None else transport
        self._constants: dict[str, Buffer] = {}
        #: What has actually crossed the link, per counter name. `relayouts` is
        #: a TRIPWIRE and not a measurement: it counts buffers rewritten through
        #: the host, nothing increments it any more, and a non-zero reading
        #: means that path came back.
        self.counters = dict.fromkeys(
            ("dispatches", "rounds", "flits", "sent", "fetched", "relayouts"), 0
        )
        self._pending: PackageBuilder | None = None
        self.packages: list[bytes] = []
        #: The node's completion for the last package run.
        self.last_completion = None

    # ---------------------------------------------------------------- memory
    def alloc(self, nbytes: int) -> int:
        return self.arena.alloc(nbytes)

    def free(self, addr: int) -> None:
        self.arena.release(addr)

    def scratch(self, nbytes: int) -> int:
        """A block for ONE call's temps, kept between calls.

        Every call's temps are dead when it returns, so the next call reuses
        these bytes rather than allocating and freeing its own -- which two
        kernels in a row otherwise re-pay, and which churns the free list into
        the fragmentation `Arena.fragmentation` reports.

        Grown, never shrunk: a call needing more takes a bigger block and the
        old one goes back. Released by :meth:`empty_cache`.
        """
        held, have = getattr(self, "_scratch", (None, 0))
        if held is not None and have >= nbytes:
            return held
        if held is not None:
            self.arena.release(held)
        made = self.arena.alloc(nbytes)
        self._scratch = (made, nbytes)
        return made

    def write(self, addr: int, blob: bytes) -> None:
        self.flush()
        self.counters["sent"] += len(blob)
        self.transport.write_block(addr, blob)

    def read(self, addr: int, nbytes: int) -> bytes:
        self.flush()
        self.counters["fetched"] += nbytes
        return self.transport.read_block(addr, nbytes)

    def put(self, array, layout: Layout, shape=None) -> Buffer:
        """Pack `array` in `layout`, upload it, and return where it landed."""
        shape = tuple(shape if shape is not None else array.shape)
        blob = layout.pack(array)
        addr = self.alloc(len(blob))
        self.write(addr, blob)
        return Buffer(addr, shape, layout)

    def get(self, buffer: Buffer):
        """Read a buffer back and undo its byte order."""
        return buffer.layout.unpack(self.read(buffer.addr, buffer.nbytes), buffer.shape)

    def convert(self, addr: int, shape: tuple, before: Layout, after: Layout) -> None:
        """Rewrite a buffer from one byte order into another, in place.

        THE FRAMEWORK HAS NO DEFAULT FOR THIS, deliberately. Reading a buffer
        back, repacking it and uploading it again is the one implementation that
        needs no hardware, and it is between two and four orders of magnitude
        more expensive than any on-device walk -- so offering it as a base-class
        courtesy is offering a cliff. A project that reorders memory says how.

        Raises :class:`NotImplementedError`.
        """
        raise NotImplementedError(
            f"{type(self).__name__} was asked to rewrite {before.key} as "
            f"{after.key} and states no way to do it. There is no host round "
            f"trip here: implement `convert` on the runtime, or refuse the "
            f"conversion where the layouts are chosen"
        )

    def constant(self, key: str, array, layout: Layout) -> int:
        """A compiler-materialised buffer, uploaded once and kept under `key`."""
        got = self._constants.get(key)
        if got is None:
            got = self.put(array, layout)
            self._constants[key] = got
        return got.addr

    def empty(self, shape: tuple, layout: Layout, nbytes: int = 0, tier=None):
        """A freshly allocated device value of `shape` in `layout`.

        `nbytes` is a FLOOR on the span, for a buffer something rewrites in a
        WIDER order than the one it ends up in -- a conversion rewrites where it
        lies, so the span outlasts the order it is labelled with.

        `tier` is the memory the buffer asked for. A runtime that has no such
        tier places it wherever it places everything else, so a kernel naming
        one still runs on a machine that does not carry it.

        Raises :class:`NotImplementedError` unless a project overrides it.
        """
        raise NotImplementedError("a runtime must say what a device value is")

    # -------------------------------------------------------------- dispatch
    def dispatch(
        self, payloads: dict, unit: str, name: str = "kernel", nodes=None, acks=None
    ) -> int:
        """Run one compiled kernel's instances on units of type `unit`.

        `nodes` narrows the placement -- to the idle ones, say. `acks` names
        completions owed by nodes this dispatch does not kick, which is how a
        transfer into another unit is waited on. Returns the rounds executed.

        Raises :class:`ImportError` when no driver is installed alongside.
        """
        if self.node is not None:
            return self._gather(payloads, unit, name, nodes, acks)
        # Imported here and not at module scope: the compiler half installs and
        # imports on its own, and only running a round needs the driver half.
        from kohakuaccel.runtime import execute

        rounds = plan(
            payloads,
            tuple(nodes) if nodes is not None else self.machine.coords(unit),
            stage_flits=self.machine.stage_flits,
            name=name,
            acks=acks,
        )
        self.counters["dispatches"] += 1
        self.counters["rounds"] += len(rounds)
        for artifact in rounds:
            self.counters["flits"] += len(artifact.flits)
            execute(artifact.to_dict(), self.ctrl)
        return len(rounds)

    # ------------------------------------------------------- node dispatch
    def live_spans(self) -> dict:
        """``{base: size}`` of every allocation a payload may address."""
        return dict(self.arena.live)

    def _gather(self, payloads, unit, name, nodes, acks) -> int:
        """One stage through the framework pipeline, appended to the open package
        and closed by a barrier."""
        coords = tuple(nodes) if nodes is not None else self.machine.coords(unit)
        result = compile_stage(payloads, unit, coords, self.machine, self.fields, acks)
        b = self._package()
        b.artifact(
            result.artifact,
            addresses=self.fields.addresses if self.fields else None,
            bind=self.bind_packages,
            compress=self.compress_packages and self.fetch_port is None,
        )
        b.barrier()
        self.counters["dispatches"] += 1
        self.counters["rounds"] += result.artifact.rounds
        self.counters["flits"] += len(result.artifact.flits)
        return result.artifact.rounds

    def _package(self) -> PackageBuilder:
        """The open package, opened if need be, its spans current."""
        if self._pending is None:
            units = machine_units(self.machine)
            self._pending = PackageBuilder(
                types=unit_types(self.machine),
                credit=self.machine.inst_depth,
                signature=signature(units),
                mesh=self.machine.default,
                fetch=self.fetch_port if self.bind_packages else None,
            )
        self._pending.spans_from(self.live_spans())
        return self._pending

    def move(self, writes, name: str = "move") -> None:
        """One memory-mover move, as its register writes (`package.mover`): a
        MOVER step and a barrier on a node, else :meth:`host_move`."""
        self.counters["moves"] = self.counters.get("moves", 0) + 1
        if self.node is None:
            self.host_move(list(writes))
            return
        b = self._package()
        b.mover(writes, addresses=None if self.bind_packages else mover.addresses)
        b.barrier()

    def ring(self, mesh: int, tag: int = 0) -> None:
        """Ring mesh `mesh`'s doorbell once this node's mover is idle.
        Node-dispatched only."""
        self._node_only("a doorbell").ring(mesh, tag)

    def wait_bell(self, mesh: int, count: int = 1) -> None:
        """Hold the node until `count` new doorbells from mesh `mesh` arrived.
        Node-dispatched only."""
        b = self._node_only("a doorbell wait")
        b.wait_bell(mesh, count)
        b.barrier()

    def _node_only(self, what: str) -> PackageBuilder:
        if self.node is None:
            raise NotImplementedError(
                f"{what} is a package step; this runtime dispatches from the host"
            )
        return self._package()

    def host_move(self, writes: list) -> None:
        """Issue a move from the host; a project overrides this. Raises
        :class:`NotImplementedError`."""
        raise NotImplementedError(
            f"{type(self).__name__} has no host path to the memory mover; run "
            f"it node-dispatched (Runtime.node), where a move is a MOVER step"
        )

    def flush(self) -> None:
        """Run the open package on the node, if there is one, and wait for it."""
        b, self._pending = self._pending, None
        if b is None or not len(b):
            return
        built = b.build(defaults=False)
        if self.engine_packages:
            built = engine_package.lower(built)
        pkg = built.to_bytes()
        if self.keep_packages:
            self.packages.append(pkg)
        self.counters["packages"] = self.counters.get("packages", 0) + 1
        self.last_completion = self.node.run(pkg, b.bindings())

    # --------------------------------------------------------------- control
    def sync(self) -> None:
        """Wait for outstanding work.

        A host-dispatched round is awaited before :meth:`dispatch` returns; a
        node's open package is run here.
        """
        self.flush()

    def empty_cache(self) -> None:
        """Return memory nothing is using: folded constants, then the free tail.

        LIVE ALLOCATIONS ARE KEPT, as `torch.cuda.empty_cache` keeps them. A
        value something still references still holds its span afterwards, and a
        pinned one is not special because nothing is being taken from it.
        """
        for buf in self._constants.values():
            try:
                self.free(buf.addr)
            except KeyError:
                pass
        self._constants.clear()
        # The scratch holds no live value between calls, so it goes too.
        held, _ = getattr(self, "_scratch", (None, 0))
        if held is not None:
            self.arena.release(held)
            self._scratch = (None, 0)
        self.arena.trim()

    def stats(self) -> dict:
        """Memory in use, and everything that has crossed the link so far."""
        return {**self.arena.stats(), **self.counters}
