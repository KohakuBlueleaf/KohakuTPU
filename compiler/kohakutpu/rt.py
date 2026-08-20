"""Device-resident tensors on a KohakuTPU, and the device that holds them.

Level 2. A tensor is a logical array plus whichever byte orders it currently
exists in on the card: a kernel declares what order it needs and this
materialises it, once, and remembers.

What a tensor is WORTH -- the operators, and which kernel each one runs -- is
level 5 and lives in :mod:`kohakutpu.api`. Nothing here names a kernel.
"""

import itertools
import weakref

import numpy as np
from kohakuaccel.machinespec import MESH_SHIFT, MachineSpec, MeshSpec
from kohakuaccel.memory import Arena, Buffer, Layout
from kohakuaccel.rt import Runtime
from kohakutpu.isa.vecemit import BATCH_BYTES

FP16 = np.float16

#: High in the 512 MB window, clear of whatever earlier sessions left low down.
ARENA_BASE = 0x1800_0000
ARENA_SIZE = 1 << 26

#: Live arena spans per mesh, keyed by the mesh's control object -- the one
#: thing every Device on a mesh shares whatever views wrap the card.
_ARENAS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _claim_span(anchor, base: int, size: int, owner) -> None:
    """Refuse an arena overlapping one another live Device holds on this mesh.

    Two Devices on ONE mesh otherwise alias silently: both open at the default
    base, each uploads over the other's operands, and every address a tensor
    cached now serves the OTHER device's bytes. Measured on hardware: the
    second dispatch computes on whatever the last device uploaded, identically
    every run, while single-device re-dispatch is exact. The span is released
    when the owning Device is collected.
    """
    spans = _ARENAS.setdefault(anchor, {})
    lo, hi = base, base + size
    for b, e in spans.values():
        if lo < e and b < hi:
            raise ValueError(
                f"this mesh already has a live arena at [{b:#x}, {e:#x}), "
                f"overlapping the requested [{lo:#x}, {hi:#x}). A second "
                f"Device on one mesh shares its DRAM, so give it its own "
                f"base= and size= instead of the default"
            )
    key = id(owner)
    spans[key] = (lo, hi)
    weakref.finalize(owner, spans.pop, key, None)


class Tensor:
    """An array on the card, in whatever byte orders have been asked for.

    `host` is the authoritative contents while they are known; a tensor a kernel
    produced has none until something reads it back. `buffers` maps a layout key
    to where those bytes are.
    """

    def __init__(self, dev: "Device", shape, host=None) -> None:
        self.dev = dev
        self.shape = tuple(shape)
        self.host = None if host is None else np.asarray(host, FP16)
        self.buffers: dict[str, Buffer] = {}
        #: A checkpoint something will read later. Pinned tensors survive
        #: :meth:`release`, so their spans are never handed out again.
        self.keep = False
        #: Which arena epoch these spans came from. See :attr:`stale`.
        self.epoch = dev.arena.epoch

    @property
    def runtime(self) -> "Device":
        return self.dev

    @property
    def device(self) -> "Device":
        """The device these bytes are on, and ONE DEVICE IS ONE MESH.

        The same object :attr:`runtime` answers with, under the name a caller
        above level 2 uses. Anything spreading a value over several meshes
        checks placement through this rather than through the attribute.
        """
        return self.dev

    @property
    def mesh(self) -> int:
        """Which mesh's memory holds these bytes, as an index.

        `Device.mesh` is the driver's object for the same mesh; this is the
        number an instruction's address carries in bits [33:32] and the one a
        collective names a peer by.
        """
        return self.dev.machine.default

    @property
    def stale(self) -> bool:
        """Whether an arena reset has given these spans away since.

        A reset frees live allocations, so a tensor that outlives one holds
        addresses another tensor now owns: reading them returns the other
        tensor's bytes and freeing them takes memory still in use. `pin` does
        not help, because a reset takes no notice of what is live.
        """
        return bool(self.buffers) and self.epoch != self.dev.arena.epoch

    def _adopt(self) -> None:
        """Forget spans an arena reset gave away, and join the current epoch."""
        if self.stale:
            self.buffers.clear()
        self.epoch = self.dev.arena.epoch

    def address(self, layout: Layout) -> int:
        """Where these contents are in `layout`, materialising them if needed.

        Raises :class:`ValueError` for a tensor whose only copy was on the card
        when the arena was reset.
        """
        if self.stale and self.host is None:
            raise ValueError(
                "this tensor's contents were on the card and an arena reset "
                "reclaimed them; read it back before resetting"
            )
        self._adopt()
        got = self.buffers.get(layout.key)
        if got is not None:
            return got.addr
        # Contents that only exist on the card have to come back to be repacked.
        # That is a relayout however it is spelled, so it is counted as one.
        if self.host is None and self.buffers:
            self.dev.counters["relayouts"] += 1
        buf = self.dev.put(self._contents(), layout, self.shape)
        self.buffers[layout.key] = buf
        return buf.addr

    def claim(self, buffer: Buffer) -> "Tensor":
        """Record a span the device allocated for this tensor."""
        self._adopt()
        self.buffers[buffer.layout.key] = buffer
        return self

    def _contents(self) -> np.ndarray:
        if self.host is None:
            self.host = np.asarray(self.numpy(), FP16)
        return self.host

    def numpy(self) -> np.ndarray:
        """The contents on the host, row-major.

        Reads back from whichever order the card holds and undoes it. Raises
        :class:`ValueError` for a tensor that is neither on the host nor on the
        card, and for one whose card copy an arena reset has reclaimed.
        """
        if self.host is not None:
            return np.asarray(self.host)
        if self.stale:
            raise ValueError(
                "this tensor's contents were on the card and an arena reset "
                "reclaimed them; read it back before resetting"
            )
        for buf in self.buffers.values():
            return self.dev.get(buf)
        raise ValueError("this tensor has no contents and was never written")

    def pin(self) -> "Tensor":
        """Keep this tensor's memory even when nothing references it."""
        self.keep = True
        return self

    def unpin(self) -> "Tensor":
        """Stop keeping it, and return its memory now."""
        self.keep = False
        self.release()
        return self

    def reshape(self, *shape) -> "Tensor":
        """The same elements under a different shape, as a NEW tensor.

        Row-major, and it does not share the original's spans, because two
        tensors owning one span free it twice: that costs one upload when the
        contents are already on the host, and a readback as well when they are
        not. Raises :class:`ValueError` on an element mismatch.
        """
        want = shape[0] if len(shape) == 1 and not isinstance(shape[0], int) else shape
        held = self._contents()
        if int(np.prod(want)) != held.size:
            raise ValueError(
                f"cannot see {self.shape} as {tuple(want)}: {held.size} elements "
                f"against {int(np.prod(want))}"
            )
        return type(self)(self.dev, tuple(want), host=held.reshape(want))

    @property
    def ndim(self) -> int:
        return len(self.shape)

    @property
    def size(self) -> int:
        """Elements this tensor holds."""
        return int(np.prod(self.shape)) if self.shape else 1

    @property
    def dtype(self):
        """Always fp16: it is the only element type the units carry."""
        return FP16

    def release(self) -> None:
        """Return every span this tensor owns. Ignored while it is pinned."""
        if self.keep:
            return
        if self.stale:
            self.buffers.clear()
            return
        for buf in self.buffers.values():
            try:
                self.dev.free(buf.addr)
            except KeyError:
                pass
        self.buffers.clear()

    def __del__(self) -> None:
        # A finaliser must not propagate: an arena already torn down at
        # interpreter shutdown would turn a freed span into a crash.
        try:
            self.release()
        except Exception:  # noqa: BLE001, S110
            pass

    def __repr__(self) -> str:
        where = ", ".join(sorted(self.buffers)) or "host"
        return f"Tensor{self.shape} [{where}]"


class Holder:
    """A runtime that hands back device values, whatever class those are.

    `Runtime.empty` asks a project what a device value IS; this is KohakuTPU's
    answer, in one place so no runtime can hand back a different one.
    """

    @property
    def values(self) -> type:
        """The class a device value is, deferred because it is a level ABOVE.

        THE ONLY UPWARD REFERENCE IN THE PACKAGE, and it must stay the only one.
        A SECOND one is the signal to build the registry `software-stack.md` §6
        prescribes for the unit-type decoder -- a project registers against a
        key and the framework asks the registry. One deferred import is a seam;
        a habit of them is a layering failure.
        """
        from kohakutpu.api import Array

        return Array

    def tensor(self, array) -> Tensor:
        """A device tensor holding `array`. Nothing moves until it is used."""
        arr = np.asarray(array, FP16)
        return self.values(self, arr.shape, host=arr)

    def empty(self, shape: tuple, layout: Layout, nbytes: int = 0) -> Tensor:
        """An allocated, unwritten tensor of `shape` in `layout`.

        `nbytes` is a floor on the span, for a buffer held in a WIDER order than
        the one it ends in. See :meth:`kohakuaccel.rt.Runtime.empty`.
        """
        out = self.values(self, shape)
        span = max(int(nbytes), layout.nbytes(shape))
        return out.claim(Buffer(self.alloc(span), tuple(shape), layout))


class Device(Holder, Runtime):
    """An attached KohakuTPU: its mesh, its memory, and the tensors on it."""

    def __init__(self, card=None, base: int = ARENA_BASE, size: int = ARENA_SIZE):
        """Attach to `card` and open an arena of `size` bytes at `base`.

        Raises :class:`ValueError` when the arena would run past the mesh's
        local space, since the top of it would carry the NEXT mesh's id and
        alias silently into that mesh's memory.
        """
        if base + size > 1 << MESH_SHIFT:
            raise ValueError(
                f"an arena of {size:,} bytes at {base:#x} ends at "
                f"{base + size:#x}, past one mesh's {1 << MESH_SHIFT:,}; bits "
                f"[{MESH_SHIFT + 1}:{MESH_SHIFT}] of the top of it are another "
                f"mesh's id, not address"
            )
        if card is None:
            card = _open_card()
        self.card = card
        # Captured, not read through `card.mesh`: a second Device on another mesh
        # moves that default, and dispatch would then aim at the wrong silicon.
        self.mesh = card.mesh
        _claim_span(self.mesh.ctrl, base, size, self)
        machine = MachineSpec(
            name="kohakutpu",
            meshes=tuple(
                MeshSpec(m.index, _units(m), agent=m.agent) for m in card.meshes
            ),
            default=self.mesh.index,
            inst_depth=512,
            links=_links(card),
        )
        # Aligned to a vector batch, not a word: see vecemit.BATCH_BYTES.
        arena = Arena(
            machine.global_addr(base), size, align=BATCH_BYTES, mesh=machine.default
        )
        # `global_mem` speaks unit-global addresses on boards whose host
        # windows live elsewhere; on the flat boards it IS card.raw.
        super().__init__(
            machine, arena, getattr(card, "global_mem", card.raw), self.mesh.ctrl
        )

    def dispatch(
        self, payloads: dict, unit: str, name: str = "kernel", nodes=None, acks=None
    ):
        """Place on units that are not still running an earlier program.

        A unit left busy never retires what is sent to it, so dispatching there
        waits forever rather than failing. A stage that named its own `nodes`
        overrules this. Raises :class:`RuntimeError` when every unit is busy.
        """
        if nodes is None:
            nodes = self.mesh.idle(unit)
            if not nodes:
                raise RuntimeError(
                    f"every {unit} unit is busy; one is wedged and the mesh needs "
                    f"a bitstream reload"
                )
        return super().dispatch(payloads, unit, name, nodes, acks)

    def __repr__(self) -> str:
        return (
            f"Device(mesh_{self.machine.default}: {self.machine.count('MG')} MG, "
            f"{self.machine.count('VC')} VC, "
            f"{self.arena.used:,}/{self.arena.size:,} bytes used)"
        )


def _units(mesh) -> dict:
    """One mesh's unit table. A type it does not carry is absent, not empty."""
    got = {k: mesh.coords(k) for k in ("MG", "VC")}
    return {k: v for k, v in got.items() if v}


#: Mesh index order along the SLR stack. SLR0=0, SLR1=1, SLR2=3, SLR3=2, and
#: only ADJACENT SLRs carry SLLs -- so the fabric is this line, not a ring.
CHAIN = (0, 1, 3, 2)


def _links(card) -> tuple:
    """The mesh-to-mesh links, as index pairs.

    Read off :data:`CHAIN` rather than from the card: the driver enumerates
    meshes but not the fabric between them, and a link the silicon does not have
    is worse than one it has and we did not use -- a transfer over it never
    arrives. A card carrying meshes outside the chain gets no links at all,
    which reads as fully connected, the answer before topology existed.
    """
    held = {m.index for m in card.meshes}
    if not held <= set(CHAIN):
        return ()
    walk = [m for m in CHAIN if m in held]
    return tuple(itertools.pairwise(walk))


def _open_card():
    """The attached card.

    Imported here, not at module scope: a tensor is an ordinary object and
    nothing but opening a card should need the driver's discovery code.
    """
    from kohakutpu.host import Card

    return Card()
