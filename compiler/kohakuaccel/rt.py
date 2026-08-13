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

    def __init__(self, machine: MachineSpec, arena: Arena, transport, ctrl=None):
        self.machine = machine
        self.arena = arena
        #: Addresses the card's memory directly, for operand and result bytes.
        self.transport = transport
        #: Carries the driver's register map, for dispatch. Often the same thing
        #: rebased onto wherever the control agent landed.
        self.ctrl = ctrl if ctrl is not None else transport
        self._constants: dict[str, Buffer] = {}
        #: What has actually crossed the link, per counter name.
        self.counters = dict.fromkeys(
            ("dispatches", "rounds", "flits", "sent", "fetched", "relayouts"), 0
        )

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
        self.counters["sent"] += len(blob)
        self.transport.write_block(addr, blob)

    def read(self, addr: int, nbytes: int) -> bytes:
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

        Through the host: this machine has no instruction that reorders memory.
        """
        self.counters["relayouts"] += 1
        raw = self.read(addr, before.nbytes(shape))
        self.write(addr, after.pack(before.unpack(raw, shape)))

    def constant(self, key: str, array, layout: Layout) -> int:
        """A compiler-materialised buffer, uploaded once and kept under `key`."""
        got = self._constants.get(key)
        if got is None:
            got = self.put(array, layout)
            self._constants[key] = got
        return got.addr

    def empty(self, shape: tuple, layout: Layout, nbytes: int = 0):
        """A freshly allocated device value of `shape` in `layout`.

        `nbytes` is a FLOOR on the span, for a buffer something rewrites in a
        WIDER order than the one it ends up in -- a conversion rewrites where it
        lies, so the span outlasts the order it is labelled with.

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

    # --------------------------------------------------------------- control
    def sync(self) -> None:
        """Wait for outstanding work.

        A round is awaited before :meth:`dispatch` returns, so this is already
        true on arrival.
        """

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
