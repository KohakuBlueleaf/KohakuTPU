"""Device-resident tensors on a KohakuTPU, and the device that holds them.

Level 2. A tensor is a logical array plus whichever byte orders it currently
exists in on the card: a kernel declares what order it needs and this
materialises it, once, and remembers.

What a tensor is WORTH -- the operators, and which kernel each one runs -- is
level 5 and lives in :mod:`kohakutpu.api`. Nothing here names a kernel.
"""

import itertools
import weakref
from dataclasses import replace

import numpy as np
from kohakuaccel.device import mover as DM
from kohakuaccel.dispatch import deal
from kohakuaccel.machinespec import MESH_SHIFT, STAGE_BYTES, MachineSpec, MeshSpec
from kohakuaccel.memory import Arena, Buffer, Layout
from kohakuaccel.package import mover as PM
from kohakuaccel.rt import Runtime
from kohakutpu.isa import relayout as RL
from kohakutpu.isa.fields import FIELDS
from kohakutpu.isa.schedule import late_drains, node_drain
from kohakutpu.isa.vecemit import BATCH_BYTES
from kohakutpu.staging import STAGE_ALIGN

from kohakutpu import imem as IM
from kohakutpu import layout as LO

FP16 = np.float16

#: High in the 512 MB window, clear of whatever earlier sessions left low down.
ARENA_BASE = 0x1800_0000
ARENA_SIZE = 1 << 26

#: The tier name `L.temp(tier=...)` uses for the MAG staging store.
L2 = "l2"

#: The transform bank's slot holding the FP16 -> MXFP7 quantiser.
XFORM_MXFP7 = 1


class RelayoutError(RuntimeError):
    """A byte-order change this machine has no walk for.

    IT IS AN ERROR AND NOT A FALLBACK. Rewriting a buffer through the host is
    the only other way to do it, and the arithmetic forbids it: a granule
    transpose of a 64 KB temp is 26,534 vector cycles, 265 us at 100 MHz, while
    the same buffer through JTAG is a 64 KB read at 41 KB/s plus a 64 KB write
    at 70 KB/s plus ~8 ms an exchange -- about 2.5 s, **four orders of
    magnitude**. There is no shape of the problem where the round trip is the
    right answer, so the path does not exist.
    """


def _no_walk(held, layout) -> str:
    """Why this tensor cannot be put in `layout`, in the author's own terms."""
    have = ", ".join(sorted(held.buffers)) or "nothing"
    return (
        f"a {held.shape} tensor is held as {have} and something asks for "
        f"{layout.key}, which is not a walk this machine has -- four AGU "
        f"dimensions, 256 words, and whole 32-byte words unless a `Tile` order "
        f"puts the disagreement at the 8-byte granule. The bytes are ON THE "
        f"CARD, so the only other way to reorder them is a host round trip, "
        f"which costs about four orders of magnitude more than the walk and is "
        f"not available. Give the two kernels a layout they share, or add the "
        f"walk to `isa/relayout.py`"
    )


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
        mx = LO.mx_inner(layout) is not None
        if mx and self.host is not None:
            # MXFP7 is made on the card from the FP16 entries (relayout.md §13).
            self.address(LO.mx_source(layout))
        if self.host is None or mx:
            made = self._reorder(layout)
            if made is not None:
                return made
            raise RelayoutError(_no_walk(self, layout))
        # Still on the host, so this is an UPLOAD and not a round trip: the
        # bytes have to cross once either way and they are packed on the way.
        buf = self.dev.put(self._contents(), layout, self.shape)
        self.buffers[layout.key] = buf
        return buf.addr

    def upload(self, layout: Layout) -> int:
        """Put the host copy up in `layout` now, packed by the host, MXFP7 included."""
        self._adopt()
        buf = self.dev.put(self._contents(), layout, self.shape)
        self.buffers[layout.key] = buf
        return buf.addr

    def prepare(self, layout: Layout) -> None:
        """Upload what `layout` will be made from on the card, if anything.

        The upload half of :meth:`address`.
        """
        if LO.mx_inner(layout) is not None and self.host is not None:
            self._adopt()
            if layout.key not in self.buffers:
                self.address(LO.mx_source(layout))

    def _reorder(self, layout: Layout) -> int | None:
        """This tensor in `layout`, rewritten on the card from an order it has.

        THE CROSS-KERNEL CASE: one kernel drained this in `Tile` order and the
        next fills it as `Entry`, and the two cannot be made to agree -- a
        cluster drains sub-tiles and fills entries, which is silicon. It is the
        one relayout `Compiled.conversions` never sees, because it is not INSIDE
        a kernel.

        None when there is nothing to rewrite FROM, or when the machine has no
        walk for it; the caller then refuses, because the only other way to do
        it is through the host.
        """
        if not self.buffers:
            return None
        held = next(iter(self.buffers.values()))
        tier = None
        if LO.mx_inner(layout) is not None:
            # From the FP16 entries when they are held.
            held = self.buffers.get(LO.mx_source(layout).key, held)
            tier = self.dev.mx_tier(layout.nbytes(self.shape), self.host is None)
        out = self.dev.empty(self.shape, layout, tier=tier)
        buf = out.buffers[layout.key]
        if not self.dev.reorder(held.addr, buf.addr, self.shape, held.layout, layout):
            out.release()
            return None
        self.buffers[layout.key] = buf
        out.buffers.clear()
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

    def flush(self) -> None:
        """Refuse to run a package that holds a fused cluster stage without its
        epilogue: its tiles would drain into cores nothing ever runs."""
        held = self.__dict__.get("_fused")
        if held is not None:
            raise RuntimeError(f"{held[3]}: a fused cluster stage has no epilogue")
        super().flush()

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

    def empty(self, shape: tuple, layout: Layout, nbytes: int = 0, tier=None) -> Tensor:
        """An allocated, unwritten tensor of `shape` in `layout`.

        `nbytes` is a floor on the span, for a buffer held in a WIDER order than
        the one it ends in. See :meth:`kohakuaccel.rt.Runtime.empty`.
        """
        out = self.values(self, shape)
        span = max(int(nbytes), layout.nbytes(shape))
        return out.claim(Buffer(self.alloc(span, tier), tuple(shape), layout))

    # ---------------------------------------------------------------- tiers
    #: Where a converted (MXFP7) operand lives, the `mx_tier` policy
    #: (relayout.md §13): in the staging store whenever there is one and it fits.
    mx_staging_max = STAGE_BYTES
    mx_uploads_to_staging = True

    def mx_tier(self, nbytes: int, produced: bool):
        """The tier a converted operand of `nbytes` is placed in: `L2` or None (DRAM)."""
        if self.staging is None or nbytes > self.mx_staging_max:
            return None
        return L2 if produced or self.mx_uploads_to_staging else None

    def alloc(self, nbytes: int, tier=None) -> int:
        """A span in `tier`, or in DRAM when this machine cannot give it one.

        A tier is a PLACEMENT and not a semantic: `L.temp` asks for the store
        that suits the access pattern, and a machine without one -- or with one
        that is full -- still has to run the kernel. Both fall back, and the
        second is counted, because a store that quietly stopped being used is a
        performance cliff nobody would see.
        """
        if tier == L2 and self.staging is not None:
            if self.staging.largest_free >= nbytes:
                return self.staging.alloc(nbytes)
            self.counters["staging_full"] = self.counters.get("staging_full", 0) + 1
        return self.arena.alloc(nbytes)

    def free(self, addr: int) -> None:
        """Return a span to whichever arena handed it out, read off the address."""
        if self.staging is not None and MachineSpec.addr_aperture(addr) is not None:
            self.staging.release(addr)
            return
        self.arena.release(addr)

    def live_spans(self) -> dict:
        """Every allocation a payload may address: DRAM and staging alike."""
        spans = dict(self.arena.live)
        if self.staging is not None:
            spans.update(self.staging.live)
        return spans

    # ------------------------------------------------------------- relayouts
    #: An arena over this mesh's MAG staging store, when one has been attached.
    #: `kohakutpu.staging.attach` sets it; without it a relayout stages in DRAM.
    staging: Arena | None = None

    #: Whether the OUT-OF-PLACE walk may run on a vector core. It gates
    #: `reorder` only: `convert` is on-card unconditionally and raises when it
    #: has no walk, because there is no host path left to fall back to.
    device_relayout = True

    #: Whether a cluster's tiles go as one stream with their fused DRAINs moved
    #: late (isa.schedule.late_drains). False sends each tile as compiled.
    drain_late = True

    def convert(self, addr: int, shape: tuple, before: Layout, after: Layout) -> None:
        """Rewrite a buffer from one byte order into another, in place.

        ON A VECTOR CORE, ALWAYS. There is no host path: `TpuBackend.validate`
        refuses a kernel whose conversion this machine cannot walk, so reaching
        the refusal here means a caller assembled one the compiler never saw.

        Into :class:`LO.MxEntry` it is the mover's quantiser, in place: entry `e`
        writes 128 B at `128e` after reading 256 B at `256e` (relayout.md §13).
        """
        mx = LO.mx_inner(after)
        if mx is not None:
            if not LO.quantises(before, after):
                self.convert(addr, shape, before, LO.mx_source(after))
            elif getattr(before, "stride", 0) != getattr(after, "stride", 0):
                raise RelayoutError(
                    f"{before.key} -> {after.key} in place needs one stride; "
                    f"{getattr(before, 'stride', 0)} != {getattr(after, 'stride', 0)}"
                )
            count, stride, entries = LO.mx_runs(after, shape)
            for i in range(count):
                at = addr + i * stride
                self.move(
                    PM.convert(at, at, entries, XFORM_MXFP7, mx.blayout), "quantise"
                )
            self.counters["quantised"] = self.counters.get("quantised", 0) + 1
            return
        made = RL.for_conversion(before, after, shape)
        plan, stride, wide, count = made if made else (None, 0, 0, 0)
        # In place has ONE buffer to step, so the two orders must agree on what
        # a batch element costs; out of place does not care.
        if made is None or stride != wide:
            raise RelayoutError(
                f"{before.key} -> {after.key} over {shape} is not a walk this "
                f"machine has, and the host path does not exist. See "
                f"`isa/relayout.py` for the four dimensions, 256 words and "
                f"whole-word bounds a conversion has to fit"
            )
        tier = self._route(plan, addr)
        addrs = [addr + i * stride for i in range(count)]
        span, held = 0, count
        if tier:
            # One span per batch element where they fit, so a batch is TWO
            # dispatches; otherwise one span serves them in turn and element `i`
            # must be home before `i+1` overwrites it.
            held = count if self._room(tier) >= count * plan.nbytes else 1
            span = self.alloc(held * plan.nbytes, tier)
        try:
            for at in range(0, count, held):
                block = addrs[at : at + held]
                spans = [span + i * plan.nbytes for i in range(len(block))]
                self._relayout(plan, block, spans if span else None)
        finally:
            if span:
                self.free(span)
        self.counters["relayouts_device"] = self.counters.get("relayouts_device", 0) + 1

    def _relayout(self, plan, addrs: list, spans: list | None) -> None:
        """One batch of buffers, walked in place or through `spans`.

        One dispatch per pass whatever the batch is: the image and the dimension
        fields do not depend on the base, so a batch restates two bases per run
        rather than a whole image per element.
        """
        mask = 0
        if plan.transpose:
            mask = self.constant("relayout:lane-groups", RL.lane_groups(), LO.Flat())
        if spans is None:
            self._run(RL.build(plan).program([(at, at) for at in addrs], mask))
            return
        pairs = list(zip(addrs, spans, strict=True))
        self._run(RL.build(plan).program(pairs, mask))
        self._run(RL.Copy(plan.total).program([(s, a) for a, s in pairs]))

    def _room(self, tier: str) -> int:
        """Bytes the tier this conversion routed to can still hand out."""
        where = self.staging if tier == L2 and self.staging is not None else self.arena
        return where.largest_free

    def reorder(self, src: int, dst: int, shape: tuple, before, after) -> bool:
        """Rewrite `before`-ordered bytes at `src` into `after` order at `dst`.

        ONE pass and no staging: the two buffers are different memory, so a run
        can never write a word a later run still has to read. That is what makes
        the CROSS-KERNEL case -- a tensor one kernel drained and another fills in
        a different order -- cheaper than the in-place one, not harder.

        False when this machine has no walk for it. The caller then REFUSES --
        `Tensor.address` raises `RelayoutError` -- because the only other way to
        do it is through the host and there is no host path.
        """
        if LO.mx_inner(after) is not None:
            return self._quantise(src, dst, shape, before, after)
        if isinstance(after, LO.PadHWC):
            return self._pad_image(src, dst, shape, before, after)
        ready = self.device_relayout and self.machine.has("VC")
        made = RL.for_conversion(before, after, shape) if ready else None
        if made is None:
            return False
        plan, from_stride, to_stride, count = made
        mask = 0
        if plan.transpose:
            mask = self.constant("relayout:lane-groups", RL.lane_groups(), LO.Flat())
        pairs = [(src + i * from_stride, dst + i * to_stride) for i in range(count)]
        self._run(RL.build(plan).program(pairs, mask))
        self.counters["relayouts_device"] = self.counters.get("relayouts_device", 0) + 1
        return True

    def _quantise(self, src: int, dst: int, shape: tuple, before, after) -> bool:
        """`before`-ordered bytes at `src` as MXFP7 entries at `dst`, by the mover.

        False when either step has no way (relayout.md §13).
        """
        fp = LO.mx_source(after)
        held = None
        if LO.quantises(before, after):
            # The source's own stride: its pad may differ from the target's.
            fp = before
        else:
            held = self.empty(shape, fp)
            at = held.buffers[fp.key].addr
            if not self.reorder(src, at, shape, before, fp):
                held.release()
                return False
            src = at
        count, to_stride, entries = LO.mx_runs(after, shape)
        from_stride = getattr(fp, "stride", 0)
        blayout = LO.mx_inner(after).blayout
        for i in range(count):
            self.move(
                PM.convert(
                    src + i * from_stride,
                    dst + i * to_stride,
                    entries,
                    XFORM_MXFP7,
                    blayout,
                ),
                "quantise",
            )
        if held is not None:
            held.release()
        self.counters["quantised"] = self.counters.get("quantised", 0) + 1
        return True

    def _pad_image(self, src: int, dst: int, shape: tuple, before, after) -> bool:
        """An ``[H][W][C]`` activation into :class:`LO.PadHWC`, by the mover.

        `C` must be a multiple of 16; False otherwise (conv2d.md §2, §6).
        """
        if len(shape) != 3 or (shape[2] * 2) % PM.WORD_BYTES:
            return False
        h, w, c = shape
        flat, held = LO.Flat(), None
        if before.key != flat.key:
            held = self.empty(shape, flat)
            at = held.buffers[flat.key].addr
            if not self.reorder(src, at, shape, before, flat):
                held.release()
                return False
            src = at
        px = after.cp * 2
        row = after.wp * px
        self.move(
            PM.fill((dst, [(after.hp, row), (row // PM.WORD_BYTES, PM.WORD_BYTES)])),
            "pad",
        )
        per = c * 2 // PM.WORD_BYTES
        origin = dst + (after.pad * after.wp + after.pad) * px
        self.move(
            PM.move(
                PM.COPY,
                (src, [(h, w * c * 2), (w, c * 2), (per, PM.WORD_BYTES)]),
                (origin, [(h, after.wp * px), (w, px), (per, PM.WORD_BYTES)]),
            ),
            "pad",
        )
        if held is not None:
            held.release()
        return True

    def _run(self, flits: list) -> None:
        self.dispatch({(0,): list(flits)}, "VC", "relayout")

    def dispatch(
        self, payloads: dict, unit: str, name: str = "kernel", nodes=None, acks=None
    ):
        """Node-dispatched, a vector core gets only the IMEM and DESC words that
        change what it holds, and a RUN (imem.rewrite). A cluster gets its
        instances as one stream, each tile's results draining under the next
        tile's work (isa.schedule.late_drains). A cluster stage draining into
        vector cores (`acks`) is held until its epilogue stage arrives, and the
        two go out as one schedule (:meth:`_stream_fused`)."""
        held = self.__dict__.get("_fused")
        if held is not None and unit == "VC" and nodes is not None:
            self._fused = None
            return self._stream_fused(held, payloads, nodes, name)
        if unit == "MG" and acks is not None and self.node is not None:
            if held is not None:
                raise RuntimeError(
                    f"{name}: a fused cluster stage arrived while {held[3]} still "
                    f"waits for its epilogue"
                )
            self._fused = (payloads, nodes, acks, name)
            return 1
        if held is not None and unit == "MG":
            raise RuntimeError(
                f"{name}: a cluster stage arrived while {held[3]} still waits for "
                f"its epilogue; only a conversion may run in between"
            )
        if unit == "MG" and acks is None and self.drain_late and self.node is not None:
            coords = tuple(nodes) if nodes is not None else self.machine.coords(unit)
            placed = deal(payloads, coords)
            per: dict = {}
            for key in sorted(payloads):
                per.setdefault(placed[key], []).extend(payloads[key])
            order = [c for c in coords if c in per]
            payloads = {(i,): late_drains(per[c]) for i, c in enumerate(order)}
            nodes = order
        if unit != "VC" or self.node is None:
            return super().dispatch(payloads, unit, name, nodes, acks)
        coords = tuple(nodes) if nodes is not None else self.machine.coords(unit)
        placed = deal(payloads, coords)
        # Per device, made on first use: Holder has no __init__ to declare it in.
        held = self.__dict__.setdefault("_imem", {})
        cores = {
            key: held.setdefault(tuple(placed[key]), IM.Resident()) for key in payloads
        }
        payloads = {
            key: IM.rewrite(list(words), cores[key]) for key, words in payloads.items()
        }
        try:
            return super().dispatch(payloads, unit, name, nodes, acks)
        except Exception:
            for core in cores.values():
                core.forget()
            raise

    def _stream_fused(self, held, vc_payloads: dict, vc_nodes, name: str) -> int:
        """A fused cluster stage and its epilogue as ONE gated schedule.

        A vector core holds one open receive stream, so the tiles that drain
        into it take turns: tile `q` on core `r` lands in L1 region `q % 2`
        (backend `_rank`/`_buffers`), and before the step loop releases tile
        `q+1`'s drain and tile `q`'s RUN it awaits on `r` exactly the acks of
        drains `0..q` and the completions of RUNs `0..q-1` -- at that point
        nothing else from `r` can be in flight, so the cumulative count means
        exactly that. A cluster's words up to its first node drain go out as
        soon as its previous tile's drain did, so it computes the next tile
        while the gate holds the drain. Ends with exact AWAITs on every unit,
        so later stages' counts stay true.

        Raises :class:`RuntimeError` when the dealing does not keep each
        cluster's tiles on one core in order -- the schedule assumes it.
        """
        mg_payloads, mg_nodes, acks, mg_name = held
        coords = tuple(mg_nodes) if mg_nodes is not None else self.machine.coords("MG")
        cluster = deal(mg_payloads, coords)
        core = deal(vc_payloads, tuple(vc_nodes))
        keys = sorted(mg_payloads)
        if sorted(vc_payloads) != keys or any(
            tuple(core[k]) != tuple(acks[k][0]) for k in keys
        ):
            raise RuntimeError(
                f"{mg_name}: the epilogue's instances are not the drains' receivers"
            )
        mine = self.__dict__.setdefault("_imem", {})
        used = {tuple(core[k]) for k in keys}
        try:
            run = {
                k: IM.rewrite(
                    list(vc_payloads[k]), mine.setdefault(tuple(core[k]), IM.Resident())
                )
                for k in keys
            }
        except Exception:
            for c in used:
                mine[c].forget()
            raise
        by_cluster: dict = {}
        by_core: dict = {}
        for k in keys:
            by_cluster.setdefault(tuple(cluster[k]), []).append(k)
            by_core.setdefault(tuple(core[k]), []).append(k)
        for ks in by_cluster.values():
            if len({tuple(core[k]) for k in ks}) != 1:
                raise RuntimeError(
                    f"{mg_name}: a cluster's tiles drain into several cores; the "
                    f"streamed schedule needs clusters a whole multiple of cores"
                )

        def cut(words):
            at = next((i for i, w in enumerate(words) if node_drain(w)), len(words))
            return list(words[:at]), list(words[at:])

        pre = {k: cut(mg_payloads[k])[0] for k in keys}
        post = {k: cut(mg_payloads[k])[1] for k in keys}
        b = self._package()
        reloc = (
            None
            if self.bind_packages
            else (self.fields.addresses if self.fields else None)
        )
        sent: dict = {}
        awaited: dict = {}
        #: Each tile's cluster position just past its last node drain: a unit
        #: retires in order, so `received` reaching it means the drain retired.
        landed: dict = {}

        def send(coord, words) -> None:
            if words:
                b.dispatch(b.unit(coord), words, reloc)
                sent[coord] = sent.get(coord, 0) + len(words)

        def wait(coord, upto: int) -> None:
            n = upto - awaited.get(coord, 0)
            if n > 0:
                b.await_(b.unit(coord), n)
                awaited[coord] = upto

        def release(k) -> None:
            c = tuple(cluster[k])
            last = max(i for i, w in enumerate(post[k]) if node_drain(w))
            landed[k] = sent.get(c, 0) + last + 1
            send(c, post[k])
            line = by_cluster[c]
            at = line.index(k) + 1
            if at < len(line):
                send(c, pre[line[at]])

        acked = self.machine.transfer_acks
        for c, ks in sorted(by_cluster.items()):
            send(c, pre[ks[0]])
        for ks in by_core.values():
            release(ks[0])
        for q in range(max(len(ks) for ks in by_core.values())):
            for r, ks in sorted(by_core.items()):
                if q >= len(ks):
                    continue
                prev = len(run[ks[q - 1]]) if q else 0
                if acked:
                    # The receiver's own acks: exactly the data landed.
                    b.await_(b.unit(r), acks[ks[q]][1] + prev)
                    awaited[r] = awaited.get(r, 0) + acks[ks[q]][1] + prev
                else:
                    # The sender retiring the drain: the data has LEFT the
                    # cluster; the RUN still crosses node, fetch port and DRAM
                    # before it reaches the core (MachineSpec.transfer_acks).
                    wait(tuple(cluster[ks[q]]), landed[ks[q]])
                    b.await_(b.unit(r), prev)
                    awaited[r] = awaited.get(r, 0) + prev
                send(r, run[ks[q]])
                if q + 1 < len(ks):
                    release(ks[q + 1])
        for r, ks in sorted(by_core.items()):
            b.await_(b.unit(r), len(run[ks[-1]]))
        for c in sorted(by_cluster):
            wait(c, sent.get(c, 0))
        b.barrier()
        if acked:
            # Acks are completions no word was sent for: one stream's per core.
            b.ack_reserve = max(
                b.ack_reserve,
                sum(max(acks[k][1] for k in ks) for ks in by_core.values()),
            )
        self.counters["dispatches"] += 2
        self.counters["rounds"] += 1
        self.counters["streamed_tiles"] = self.counters.get("streamed_tiles", 0) + len(
            keys
        )
        return 1

    def _route(self, plan, addr: int) -> str | None:
        """The tier this conversion walks into: `L2`, DRAM, or None for in place.

        Priced in `docs/notes/data-movement-problem.md` §5 credits, which is not
        the same as the fewest passes -- see :func:`kohakutpu.cost.route_for`.
        Where the BUFFER lives is read off its address rather than its name: a
        buffer already in the staging store has no ragged access to avoid, so it
        is walked in place and staging it again would buy a pass for nothing.

        Imported here and not at module scope: only performing a conversion
        needs the cost model, and the cost model reads this module's plans.
        """
        from kohakutpu import cost

        room = self.staging.largest_free if self.staging is not None else 0
        home = "S" if MachineSpec.addr_aperture(addr) is not None else "M"
        return {"S": L2, "M": "dram"}.get(cost.route_for(plan, room, home))


class Device(Holder, Runtime):
    """An attached KohakuTPU: its mesh, its memory, and the tensors on it."""

    def __init__(
        self,
        card=None,
        base: int = ARENA_BASE,
        size: int = ARENA_SIZE,
        node=None,
    ):
        """Attach to `card` and open an arena of `size` bytes at `base`.

        `node` is the mesh's node queue (`kohakuaccel.node.queue.NodeQueue`);
        given, the node dispatches every kernel, else the host does.

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
        self.node = node
        self.fields = FIELDS
        if node is not None:
            self.staging = _staging_above(machine, node)
            # The node's package sequences a fused epilogue tile by tile; the
            # transfers' acks cannot reach it (MachineSpec.transfer_acks).
            self.machine = replace(
                self.machine, stream_epilogue=True, transfer_acks=False
            )
        # The board names the memory port that streams instructions, if its
        # memory ports take INST fetches (mag_mem_port flag [5]).
        port = (getattr(card, "board", None) or {}).get("fetch_port")
        if port is not None:
            self.fetch_port = tuple(port)
            self.machine = replace(self.machine, fetched=self.bind_packages)

    def dispatch(
        self, payloads: dict, unit: str, name: str = "kernel", nodes=None, acks=None
    ):
        """Place on units that are not still running an earlier program.

        A unit left busy never retires what is sent to it, so dispatching there
        waits forever rather than failing. A stage that named its own `nodes`
        overrules this. Raises :class:`RuntimeError` when every unit is busy.
        A node-dispatched device leaves unit choice to the node.
        """
        if nodes is None and self.node is None:
            nodes = self.mesh.idle(unit)
            if not nodes:
                raise RuntimeError(
                    f"every {unit} unit is busy; one is wedged and the mesh needs "
                    f"a bitstream reload"
                )
        return super().dispatch(payloads, unit, name, nodes, acks)

    #: Polls of the mover's status a host-issued move may take before failing.
    HOST_MOVE_POLLS = 2000

    def host_move(self, writes: list) -> None:
        """A move issued by the host through this mesh's agent window, awaited.

        Raises :class:`RuntimeError` on a mover fault or a move that never goes
        idle.
        """
        DM.issue(self.mesh.ctrl, list(writes), 0)
        for _ in range(self.HOST_MOVE_POLLS):
            st = DM.status(self.mesh.ctrl.read64(DM.AUX_STAT))
            if st["fault_code"]:
                raise RuntimeError(f"the mover faulted: {st['fault']}")
            if not st["busy"]:
                return
        raise RuntimeError(
            f"the mover was still busy after {self.HOST_MOVE_POLLS} polls"
        )

    def __repr__(self) -> str:
        return (
            f"Device(mesh_{self.machine.default}: {self.machine.count('MG')} MG, "
            f"{self.machine.count('VC')} VC, "
            f"{self.arena.used:,}/{self.arena.size:,} bytes used)"
        )


def _staging_above(machine, node) -> Arena | None:
    """The mesh's staging store, above the node's queue region if it lives there.

    None when the queue leaves none of it free. A queue in DRAM leaves it all.
    """
    base = machine.stage_addr(0)
    start = getattr(node, "base", None)
    if start is None:
        return None
    if base <= start < base + STAGE_BYTES:
        start = -(-(start + node.size) // STAGE_ALIGN) * STAGE_ALIGN
    else:
        start = base
    if start >= base + STAGE_BYTES:
        return None
    return Arena(
        start, base + STAGE_BYTES - start, align=STAGE_ALIGN, mesh=machine.default
    )


def _units(mesh) -> dict:
    """One mesh's unit table. A type it does not carry is absent, not empty."""
    got = {k: mesh.coords(k) for k in ("MG", "VC")}
    return {k: v for k, v in got.items() if v}


#: Mesh index order along the SLR stack: mesh i is in SLR i, and only ADJACENT
#: SLRs carry SLLs -- so the fabric is this line, not a ring. Mirrors
#: mag_switch.v CH_SEQ.
CHAIN = (0, 1, 2, 3)


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
