"""The memory mover's command registers, as a program of 64-bit writes.

Six affine dimensions per walker and no walk limit, dispatched by the HOST --
`mm_mover.v` has a `cfg_*` port and no instruction port, so a compute unit
cannot command it. Three facts from that file decide what it is good for:

* **A TRANSPOSE IS A DESCRIPTOR, not a mode.** Every index permutation is
  affine, so a transpose is `COPY` with a source and destination walked in
  different orders -- see `docs/notes/data-movement-problem.md` §2.4. `MODE_TRANSPOSE`
  is a canned convenience that was never built and is not needed; do not design
  around its absence.
* **Word granular**: `lt_ma = |dst_addr[4:0]` faults `F_ALIGN`, and every beat
  is `awsize` 32 bytes. So it performs a permutation of whole WORDS and nothing
  finer -- `Flat <-> Entry` and no order involving `Tile`.
* **The DESTINATION defines the iteration space** (`desc_next` on `!dst_last`),
  so a source stride of 0 broadcasts and `src_valid` low injects the immediate.

THESE WORDS DO NOT REACH A PRE-FIX BITSTREAM INTACT, and the reason is not in
this file. Every station-bus manager flit-aligns (`sb_nmu.v:134` PACK=1, `:355`
size = FSZ), so a 64-bit write leaves the manager as a 32-BYTE transfer and
arrives at the mesh's 32-bit control port as four 64-bit beats, one of them
strobed. The orchestrator used to pulse `aux_cfg_en` on every beat without
reading `s_axi_wstrb`, and to stride the register address by 8: ONE host write
became FOUR register writes at +0 +8 +16 +24. Register 0x00 is CTRL, so writing
a descriptor also fired GO with stale bytes. The move completed, reported no
fault, and moved wrong bytes.

`noc_orchestrator.v:409` now drops a beat with no strobes, `:414` merges by byte
enable, and `:433` aligns the client offset instead of striding it, so the four
beats land as the one register write they were. `tests/sysnode/mover_cfg32_tb.v`
replays these exact words into `S_AXI_CTRL` at the shipped window (0x800800) in
all four shapes an upsizer can produce -- one packed beat, a +0/+4 pair, a
2-beat burst, and the ship's 32-byte flit -- and checks all 64 moved words.

THE CARD IN THE MACHINE IS PRE-FIX. Until it is rebuilt, one `write64` here
still becomes four register writes on silicon; there is nothing to work around
from this side, because no manager port avoids the flit alignment.
"""

from dataclasses import dataclass, field

#: The `AUX_CFG` window, forwarded out of the orchestrator with the offset
#: WITHIN the window as the client's own register address. Split at 0x80: below
#: is the mover, above is the interlink.
AUX_CFG = 0x0800
AUX_STAT = 0x0078
AUX_CLIENT_SPAN = 0x80

R_CTRL, R_HDR, R_DIM, R_AXIS = 0x00, 0x10, 0x18, 0x20
R_WINDOW, R_INDEX, R_SEED, R_IMM, R_GATHER = 0x28, 0x30, 0x38, 0x40, 0x50

COPY, TRANSPOSE, GATHER, GENERATE, FILL = 0, 1, 2, 3, 4

#: Element widths the datapath carries. 3 faults `F_EWIDTH`.
W8, W16, W32 = 0, 1, 2

#: `flags[3]` coalesces writes into bursts -- worth 7.2x per the RTL header, and
#: safe everywhere since `mag_ilink`'s splitter learned `s_awlen`.
FLAG_WCOAL = 1 << 3

#: The transfer granule, fixed in RTL at `m_awsize = m_arsize = 3'd5`. Not a
#: runtime knob: every address and every stride must be a multiple of it.
WORD_BYTES = 32

#: Loop levels one walker has, and the widths of a dimension's fields.
NDIM = 6
COUNT_BITS, STRIDE_BITS, AXIS_BITS = 16, 32, 16
ADDR_BITS = 40

FAULTS = {
    0: "none",
    1: "index length",
    2: "range",
    3: "AXI",
    4: "mode -- TRANSPOSE is a canned form that was never built; use COPY",
    5: "element width",
    6: "alignment: a destination that is not 32-byte aligned",
}


class MoverError(ValueError):
    """A command this engine cannot execute, and why."""


def _fits(name: str, value: int, bits: int, signed: bool = False) -> int:
    span = 1 << bits
    lo, hi = (-(span >> 1), span >> 1) if signed else (0, span)
    if not isinstance(value, int) or not lo <= value < hi:
        raise MoverError(
            f"{name} = {value!r} does not fit {bits} bits "
            f"{'signed' if signed else 'unsigned'}; a value too wide here shifts "
            f"every field above it and still writes a legal-looking command"
        )
    return value & (span - 1)


@dataclass
class Walker:
    """One `mx_tdesc`: a base, up to six nested loops, and two bound axes.

    `dims` is ``(count, stride)`` per level, **OUTERMOST FIRST** -- the opposite
    of `vec_agu`, whose dimension 0 is the fastest. `mx_tdesc` makes the HIGHEST
    live index the innermost one, so a list written the vector core's way walks
    the loops inside out and reads a plausible wrong tensor.

    `axes` is ``(axis, step)`` per level, where axis 0 contributes to neither
    bound; `window` is ``{axis: (base, extent)}`` and an extent of 0 disables
    that axis, which is why a descriptor with no padding needs none.

    A WINDOW SUPPRESSES WRITES; IT DOES NOT SKIP WORK. An out-of-window element
    makes `mx_tdesc`'s `valid` low, which `mm_mover.v:271` turns into K_SKIP --
    no read, no write, but the walk still steps through it. You pay the FULL
    iteration space in cycles and write only the valid part, so a window is
    padding, never an optimisation.
    """

    base: int
    dims: list = field(default_factory=list)
    axes: list = field(default_factory=list)
    window: dict = field(default_factory=dict)

    def program(self, sel: int) -> list:
        """The `(offset, value)` writes that load this walker.

        Checks 32-byte alignment on BOTH walkers. The hardware only faults the
        destination (`lt_ma = |dst_addr[4:0]` -> `F_ALIGN`); a misaligned SOURCE
        is never checked and goes out as an illegal AXI burst, so it is refused
        here or it is refused nowhere.
        """
        if not 1 <= len(self.dims) <= NDIM:
            raise MoverError(f"a walker has 1..{NDIM} dimensions; got {len(self.dims)}")
        if self.base % WORD_BYTES:
            raise MoverError(
                f"base {self.base:#x} is not {WORD_BYTES}-byte aligned. Every "
                f"transfer is one {WORD_BYTES}-byte word; a misaligned "
                f"destination raises F_ALIGN and a misaligned source is not "
                f"checked at all -- it emits an illegal AXI burst instead"
            )
        for at, (_, stride) in enumerate(self.dims):
            if stride % WORD_BYTES:
                raise MoverError(
                    f"dimension {at}'s stride {stride} is not a multiple of "
                    f"{WORD_BYTES}. Every address this walker reaches must be "
                    f"word aligned, and a stride that is not carries the walk "
                    f"off alignment partway through a legal-looking descriptor"
                )
        out = [
            (
                R_HDR,
                sel
                | _fits("base", self.base, ADDR_BITS) << 4
                | _fits("ndim", len(self.dims), 3) << 44,
            )
        ]
        for at, (count, stride) in enumerate(self.dims):
            axis, step = self.axes[at] if at < len(self.axes) else (0, 0)
            # 0x18 stages the dimension and 0x20 LATCHES it: `d_dim_en` is
            # pulsed by the 0x20 write, so 0x18 on its own loads nothing.
            out.append(
                (
                    R_DIM,
                    sel
                    | _fits("dimension", at, 3) << 1
                    | _fits("count", count, COUNT_BITS) << 4
                    | _fits("stride", stride, STRIDE_BITS, signed=True) << 20,
                )
            )
            out.append(
                (
                    R_AXIS,
                    _fits("axis", axis, 2)
                    | _fits("axis step", step, AXIS_BITS, signed=True) << 2,
                )
            )
        for axis in sorted(self.window):
            if axis not in (1, 2):
                raise MoverError(f"a walker has two bound axes, 1 and 2; got {axis}")
        # BOTH axes ALWAYS, extent 0 to disable. `d_aext` is written only here
        # and cleared only by reset (`mx_tdesc.v:102`, `:80`), so an omitted
        # register INHERITS the previous move's bounds -- and an inherited
        # extent makes `valid` low, which `mm_mover.v:271` turns into K_SKIP:
        # the write is suppressed, the walk still completes, and the move
        # reports done with no fault and no bytes moved.
        for axis in (1, 2):
            abase, extent = self.window.get(axis, (0, 0))
            out.append(
                (
                    R_WINDOW,
                    sel
                    | (axis - 1) << 1
                    | _fits("axis base", abase, AXIS_BITS, signed=True) << 2
                    | _fits("axis extent", extent, AXIS_BITS) << 18,
                )
            )
        return out


def program(
    mode: int,
    src: Walker | None = None,
    dst: Walker | None = None,
    *,
    ewidth: int = W16,
    flags: int = FLAG_WCOAL,
    imm: int = 0,
    seed: int = 0,
    index: tuple | None = None,
    gather: tuple | None = None,
) -> list:
    """One move, as `(client offset, 64-bit value)` writes in the order to issue.

    GO is the last write and is a one-cycle pulse, so the descriptors must be
    loaded first; every register here is write-only and status comes back
    through :func:`status`.

    Raises :class:`MoverError` for `TRANSPOSE`, which the RTL refuses before it
    reads a descriptor, and for an element width the datapath faults on.
    """
    if mode == TRANSPOSE:
        raise MoverError(
            "mode 1 is a canned convenience `mm_mover.v` refuses in I_IDLE, and "
            "you do not need it: a transpose IS a descriptor. Every index "
            "permutation is affine, so build it as COPY with the source and the "
            "destination walked in different orders -- six levels each. What the "
            "engine genuinely cannot do is anything FINER than a 32-byte word, "
            "because `lt_ma` faults a destination that is not word aligned"
        )
    if ewidth == 3:
        raise MoverError("element width 3 raises F_EWIDTH; it is 0, 1 or 2")
    out: list = []
    if src is not None:
        out += src.program(0)
    if dst is not None:
        out += dst.program(1)
    if index is not None:
        base, count = index
        out.append(
            (
                R_INDEX,
                _fits("index base", base, ADDR_BITS)
                | _fits("index count", count, 16) << 40,
            )
        )
    if gather is not None:
        pitch, words = gather
        out.append(
            (
                R_GATHER,
                _fits("pitch", pitch, 32) | _fits("gather words", words, 16) << 32,
            )
        )
    if seed:
        out.append((R_SEED, _fits("seed", seed, 64)))
    if imm:
        out.append((R_IMM, _fits("immediate", imm, 32)))
    out.append(
        (
            R_CTRL,
            _fits("mode", mode, 3)
            | _fits("element width", ewidth, 2) << 3
            | _fits("flags", flags, 8) << 8
            | 1 << 16,
        )
    )
    return out


def copy(src: Walker, dst: Walker, **kw) -> list:
    """A word-granular permutation: read `src`'s walk, write `dst`'s.

    The destination defines the iteration space, so `dst` states the element
    count and `src` may repeat itself at stride 0.
    """
    return program(COPY, src, dst, **kw)


def fill(dst: Walker, value: int, **kw) -> list:
    """Write `value` over `dst`'s walk, reading nothing."""
    return program(FILL, None, dst, imm=value, **kw)


def status(word: int) -> dict:
    """`AUX_STAT` decoded. The two traffic counters are free-running 16-bit.

    Read DELTAS from them, never totals: they wrap every 65,536 accesses and
    nothing resets them.
    """
    return {
        "busy": bool(word & 1),
        "fault": FAULTS.get((word >> 4) & 0xF, "unknown"),
        "fault_code": (word >> 4) & 0xF,
        "writes": (word >> 8) & 0xFFFF,
        "reads": (word >> 24) & 0xFFFF,
        "moves": (word >> 40) & 0xFF_FFFF,
    }


def issue(transport, writes: list, base: int) -> None:
    """Send a program to the mover through `base` -- the agent's register window.

    ONE write at a time and in order: GO is a one-cycle pulse and the
    descriptors ahead of it are latched by their own pulses, so a reordered or
    batched issue starts a move against a half-loaded walker.
    """
    for offset, value in writes:
        if not 0 <= offset < AUX_CLIENT_SPAN:
            raise MoverError(
                f"client offset {offset:#x} is outside the mover's half of "
                f"AUX_CFG; {AUX_CLIENT_SPAN:#x} and above is the interlink's"
            )
        transport.write64(base + AUX_CFG + offset, value)
