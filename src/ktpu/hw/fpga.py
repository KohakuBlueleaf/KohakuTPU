"""A live session against real hardware, shaped like :class:`ktpu.hw.sim.Session`.

    with Session(board="singlemesh_2x2") as s:
        r = s.run(16, 32, 16)      # M, K, N

The signature and the returned payload are the simulator's, so a caller swaps
one for the other and changes nothing else. That is what keeps this module
thin: scoring, error attribution and the verdict all come from
:func:`ktpu.hw.sim.payload`, a pure function of the problem, the answer and a
log. Hardware supplies the first two honestly and writes its own log; what it
cannot supply -- the bench's cycle counters -- is absent rather than invented.
Two scorers that can disagree about one run is the failure `payload` was
centralised to prevent.

No synthesised mesh has a `main_orch`, so THE HOST IS THE ORCHESTRATOR: a round
is `Program.execute`, walking WR/POLL/DONE itself.

NO MODULE STATE IS PATCHED. This once held six of `bench`'s globals under the
session lock to plan for a board -- the layout and five capacities. Every one
of them is an argument now (`board.caps()`, `mesh_layout=`), which is what lets
a hardware session and a simulation run in the same process without describing
each other's machine.
"""

import contextlib
import threading
import time

from ktpu.hw import bench, kernel, sim, tensor, vector
from ktpu.hw import board as _board
from ktpu.hw import device as dev
from ktpu.hw.board import MEM_WORD_BYTES, Board
from ktpu.hw.device import WORD_BYTES

# What `A_CAPS` must read back. FLIT_WIDTH is in its low 16 bits and is 288 on
# every machine we build; 0 and all-ones are what a missing slave returns.
CAPS_FLIT_WIDTH = 288

# BOTH OFF, unlike the bench: without the quantise marker, pre-quantisation is
# a wrong answer rather than a slower path. Shared so check and run agree.
PREQ = (False, False)


class HardwareError(RuntimeError):
    """The machine is reachable and did not do what it was asked."""


@contextlib.contextmanager
def _globals(**kw):
    """Hold `bench` module attributes for the duration, restoring them after.

    THE LAST GLOBAL. Capacities and the mesh layout are arguments now, but
    `bench.mesh()` still falls back to `bench.LAYOUT` on paths this module does
    not own -- `sim.payload` calls it with no layout and would otherwise draw
    the wrong mesh. Every caller holds the session lock and the restore is
    unconditional.
    """
    was = {k: getattr(bench, k) for k in kw}
    for k, v in kw.items():
        setattr(bench, k, v)
    try:
        yield
    finally:
        for k, v in was.items():
            setattr(bench, k, v)


def _layout(lay):
    """Just the layout, for callers with no board in hand."""
    return _globals(LAYOUT=lay)


# What accumulates since `resetn`, and so must be differenced rather than read.
# `compute_cycles`/`memory_cycles` are here only for a cumulative-scope node:
# `vec_cu` clears its kernel counter on every RUN, so differencing there would
# subtract a run that is not in this measurement.
_CUMULATIVE = ("retired", "busy_cycles", "compute_cycles", "memory_cycles")
_ALWAYS_CUMULATIVE = ("retired", "busy_cycles")


def delta_counters(before: dict, after: dict) -> dict:
    """Per-node `after` minus `before`, for the counters that accumulate.

    Both arguments are :meth:`Session.node_counters` results. Fields whose
    `scope` is ``"last-run"`` are taken from `after` unchanged, because they
    were already cleared by the run being measured; so are the CU_STATUS
    fields, which are levels and not counts.

    DIFFERENCES ARE TAKEN MODULO 2**32. The counters are 32 bits at a few
    hundred MHz, so a session longer than about ten seconds wraps one, and a
    plain subtraction would report roughly negative four billion cycles.
    """
    out = {}
    for key, now in after.items():
        was = before.get(key, {})
        # `error` is the unreachable-node sentinel, never a level -- the CU's
        # own flag is `cu_error`. A node absent from `before` says so rather
        # than passing an absolute off as a difference.
        if "error" in now or "error" in was or not was:
            out[key] = {**now, "delta": False}
            continue
        row = dict(now)
        fields = _ALWAYS_CUMULATIVE if now.get("scope") == "last-run" else _CUMULATIVE
        for f in fields:
            if isinstance(now.get(f), int) and isinstance(was.get(f), int):
                row[f] = (now[f] - was[f]) & dev.MASK32
        out[key] = {**_with_dispatch(row), "delta": True}
    return out


def _with_dispatch(row: dict) -> dict:
    """Add `dispatch_cycles`: the busy time neither datapath counter explains.

    Measured against the LARGER of compute and memory rather than their sum,
    because on a cluster the two overlap -- a fill of one buffer runs while the
    array works on the other -- so the sum would over-subtract and report an
    overhead that is negative. This is therefore an upper bound on dispatch and
    idle time, and it is not clamped: a negative value means a datapath counter
    ran while `noc_cu_base` did not consider the CU busy, which is a finding
    about the RTL and not a rounding error.
    """
    known = [
        c
        for c in (row.get("compute_cycles"), row.get("memory_cycles"))
        if isinstance(c, int)
    ]
    row["dispatch_cycles"] = row["busy_cycles"] - max(known) if known else None
    return row


class Session:
    """One board, one transport, one problem at a time.

    ``board`` is a :class:`~ktpu.hw.board.Board`, a name in ``boards/``, or
    None to take ``$KTPU_BOARD``. ``transport`` is a spec string
    (:func:`~ktpu.hw.board.open_transport`) or an already-open transport, which
    is what lets a test drive the whole session against a fake.
    """

    def __init__(self, board=None, transport=None, timeout: float = 30.0):
        self.board = board if isinstance(board, Board) else Board.select(board)
        if self.board is None:
            raise ValueError(
                "no board: pass one, name one in boards/, or set $KTPU_BOARD"
            )
        self.timeout = timeout
        self.lock = threading.Lock()
        self._owned = False
        # `link` rebases -- control programs are written in the bench's address
        # space. `raw` does not: mem_byte and upload_addr are already absolute.
        self.link = self._open(transport)
        self.raw = self.link.inner
        # CU_CAPS per node, read once: an endpoint's type cannot change under a
        # live bitstream, and index 3 cannot be decoded without it.
        self._caps: dict = {}

    # ---- lifecycle ------------------------------------------------------
    def close(self):
        if self._owned and hasattr(self.raw, "close"):
            self.raw.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _ctrl(self, off):
        return dev.MAG_BASE + off

    # ---- the machine ----------------------------------------------------
    def caps(self) -> dict:
        """The agent's capability word, decoded. The cheapest proof of life."""
        raw = self.link.read64(self._ctrl(dev.A_CAPS))
        return {
            "raw": raw,
            "flit_width": raw & 0xFFFF,
            "pos_width": (raw >> 16) & 0xFF,
            "grid_lo": (raw >> 24) & 0xFF,
            "grid_hi": (raw >> 32) & 0xFF,
        }

    def handshake(self) -> dict:
        """Confirm something answering is the machine this board describes.

        A_CAPS is constant and known in advance, so a wrong value separates
        "the card is not there" from "the run went wrong" -- and that is the
        distinction every later failure otherwise hides. The grid check catches
        a board file pointed at the wrong bitstream, which is the mistake that
        would put flits on nodes that do not exist.
        """
        c = self.caps()
        if c["flit_width"] != CAPS_FLIT_WIDTH:
            raise HardwareError(
                f"A_CAPS at {self.board.ctrl(dev.A_CAPS):#x} reads {c['raw']:#018x}, "
                f"whose FLIT_WIDTH is {c['flit_width']} and not {CAPS_FLIT_WIDTH}. "
                "Nothing is answering there, or it is not the agent."
            )
        want = max(self.board.ncol, self.board.nrow)
        if c["grid_hi"] != want:
            raise HardwareError(
                f"the machine reports a {c['grid_hi']}-wide grid and board "
                f"{self.board.name!r} describes {want}; the board file does not "
                "match the bitstream"
            )
        return c

    def reset(self) -> None:
        """Clear the completion counter and wait for the dispatcher to be idle.

        AS MUCH RESET AS THERE IS. `axi_aresetn` is a block-design pin the host
        cannot drive, and the agent has no soft-reset bit, so a genuinely wedged
        machine needs the bitstream reloading -- which this says rather than
        appearing to fix.
        """
        self.link.write64(self._ctrl(dev.A_SIG_DONE), 0)
        deadline = time.monotonic() + self.timeout
        while self.link.read64(self._ctrl(dev.A_PROG_STAT)) & 1:
            if time.monotonic() > deadline:
                raise HardwareError(
                    f"the dispatcher is still running after {self.timeout:g}s. "
                    "There is no soft reset; reload the bitstream."
                )

    # ---- the mailbox: asking a node what it is --------------------------
    def ctrl_read(self, node, idx: int = dev.CU_CAPS, timeout=None) -> int:
        """Read control register `idx` from the endpoint at `node`.

        Sends a raw CU_CTRL flit through the agent's mailbox and waits for the
        reply. THIS IS THE ONLY QUESTION THE MACHINE ANSWERS ABOUT ITSELF --
        every other register describes the agent, not the nodes -- so it is
        also the only way to tell a mesh that enumerated from one that did not.

        Raises :class:`HardwareError` if nothing replies, or if the reply comes
        from the wrong node or carries the wrong transaction tag.
        """
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        self._drain_rx(deadline)
        txn = self._next_txn()
        flit = dev.ctrl_request(tuple(node), tuple(self.board.agent), idx, txn)
        for w in range(dev.FLIT_WORDS):
            self.link.write64(
                self._ctrl(dev.A_TX_FLIT0 + w * 8), (flit >> (w * 64)) & dev.MASK64
            )
        self.link.write64(self._ctrl(dev.A_TX_KICK), 1)

        reply = dev.ctrl_reply(self._pop_rx(node, idx, deadline))
        # Addressing the agent itself loops the request back: source and tag are
        # ours, so only the opcode tells a reply from an echo. Found by sweeping.
        if reply["op"] != dev.CTRL_READ_RESP:
            raise HardwareError(
                f"({node[0]},{node[1]}) returned opcode {reply['op']} and not a read "
                f"response ({dev.CTRL_READ_RESP}). Addressing the agent itself looks "
                "exactly like this: the request comes back rather than being answered."
            )
        if reply["txn"] != txn or reply["src"] != tuple(node):
            raise HardwareError(
                f"asked ({node[0]},{node[1]}) for register {idx} with tag {txn} and "
                f"got tag {reply['txn']} from {reply['src']}"
            )
        return reply["value"]

    def _next_txn(self):
        self._txn = (getattr(self, "_txn", 0) + 1) & 0xFF or 1
        return self._txn

    def _drain_rx(self, deadline):
        """Empty the mailbox before asking, so a stale reply cannot answer."""
        while not self.link.read64(self._ctrl(dev.A_RX_STATUS)) & (1 << 16):
            self.link.write64(self._ctrl(dev.A_RX_POP), 1)
            if time.monotonic() > deadline:
                raise HardwareError("the reply mailbox will not empty")

    def _pop_rx(self, node, idx, deadline):
        """Block for one reply flit and pop it."""
        while self.link.read64(self._ctrl(dev.A_RX_STATUS)) & (1 << 16):
            if time.monotonic() > deadline:
                raise HardwareError(
                    f"({node[0]},{node[1]}) did not answer a CU_CTRL read of "
                    f"register {idx}. Either nothing is at that coordinate or the "
                    "flit did not route -- both look identical from here."
                )
        flit = 0
        for w in range(dev.FLIT_WORDS):
            flit |= self.link.read64(self._ctrl(dev.A_RX_FLIT0 + w * 8)) << (w * 64)
        self.link.write64(self._ctrl(dev.A_RX_POP), 1)
        return flit

    def enumerate_nodes(self, coords=None) -> dict:
        """Ask every node what it is. The inventory, from the machine's own mouth.

        Returns coordinate -> decoded CU_CAPS for everything that answers, and
        the reason for everything that does not. Defaults to the board's
        clusters and vector cores; pass `coords` to sweep somewhere else.
        """
        want = coords or [*self.board.clusters, *self.board.vectors]
        out = {}
        for node in want:
            key = f"{node[0]},{node[1]}"
            try:
                out[key] = dev.decode_caps(self.ctrl_read(node, dev.CU_CAPS, 2.0))
            except HardwareError as exc:
                out[key] = {"error": str(exc).split(".")[0]}
        return out

    def sweep(self, timeout: float = 0.5) -> dict:
        """Ask EVERY coordinate in the grid, not only the ones the board claims.

        `enumerate_nodes` can only confirm that what the file declares is
        present. It cannot notice a node the file omits, and a GENERATED board
        file looks authoritative enough that nobody would think to. This walks
        the whole grid including the edge ring, so "the board file is complete"
        becomes a measurement in both directions.

        A flit to a coordinate with no endpoint is dropped by the tie-off, so
        an absent node costs one timeout and never hangs.
        """
        b = self.board
        grid = [(x, y) for y in range(b.nrow + 2) for x in range(b.ncol + 2)]
        answered, declared = {}, {tuple(c) for c in (*b.clusters, *b.vectors)}
        for node in grid:
            try:
                caps = dev.decode_caps(self.ctrl_read(node, dev.CU_CAPS, timeout))
            except HardwareError:
                continue
            answered[node] = caps
        return {
            "grid": [b.ncol + 2, b.nrow + 2],
            "answered": {f"{x},{y}": c for (x, y), c in answered.items()},
            # The two directions the file can be wrong in, named separately.
            "undeclared": sorted(set(answered) - declared),
            "missing": sorted(declared - set(answered)),
        }

    def check_nodes(self) -> list[str]:
        """How the machine's own account of itself differs from the board file.

        The version field now carries its own weight: the RTL is at 0x03 (the
        vector datapath rebuild, then CU_DATA), so a card still holding the
        shipped v1 bitstream is REFUSED here rather than running a compiler's
        newer ISA against silicon that does not implement it. That refusal is
        this check working. Reading type and version off every node is the
        other half, and catches a board file aimed at the wrong bitstream.
        """
        why = []
        found = self.enumerate_nodes()
        for node, kind in (
            *[(c, dev.CU_MATMUL) for c in self.board.clusters],
            *[(v, dev.CU_VECTOR) for v in self.board.vectors],
        ):
            key = f"{node[0]},{node[1]}"
            got = found.get(key, {})
            if "error" in got:
                why.append(f"({key}) did not answer: {got['error']}")
            elif got["type"] != kind:
                why.append(
                    f"({key}) reports {got['name']!r} (0x{got['type']:04X}) and the "
                    f"board expects 0x{kind:04X}"
                )
            elif got["version"] != self.board.cu_version:
                why.append(
                    f"({key}) reports CU_VERSION {got['version']} and board "
                    f"{self.board.name!r} was written for {self.board.cu_version}. "
                    "THIS IS THE VERSION GATE DOING ITS JOB, not a fault: the "
                    "card is running a DIFFERENT BITSTREAM from the one this "
                    "board file describes, and its capacities and ISA may have "
                    "moved. Reprogram the card with the matching bitstream, or "
                    "regenerate the board file from that build's synthesis log "
                    "(scripts/py/gen_board.py). Do not override it."
                )
        return why

    def counters(self, deep: bool = False) -> dict:
        """Everything the agent will tell you about the run it just did.

        Cheap by default: agent registers only, one transport read each. `deep`
        additionally asks every node for its own counters over CU_CTRL, which
        is :meth:`node_counters` and costs what that costs.
        """
        stat = self.link.read64(self._ctrl(dev.A_PROG_STAT))
        nodes = {}
        for x, y in (*self.board.clusters, *self.board.vectors):
            v = self.link.read64(self._ctrl(dev.A_NODE + dev.node_index(x, y) * 8))
            nodes[f"{x},{y}"] = {"valid": v & 1, "signals": (v >> 8) & 0xFFFF}
        if deep:
            for key, row in self.node_counters().items():
                nodes.setdefault(key, {}).update(row)
        return {
            "sig_done": self.link.read64(self._ctrl(dev.A_SIG_DONE)) & 0xFFFF,
            "prog_run": stat & 1,
            "prog_left": (stat >> 1) & 0xFFFF,
            "prog_credit": (stat >> 17) & 0xFFFF,
            "nodes": nodes,
        }

    def node_counters(self, nodes=None, timeout=None) -> dict:
        """Compute, memory and dispatch cycles, per node, from the nodes themselves.

        Returns coordinate -> a row carrying `retired`, `busy_cycles`,
        `compute_cycles`, `memory_cycles`, `dispatch_cycles`, `scope` and the
        CU_STATUS levels; or `{"error": ...}` for a node that does not answer,
        so one dead endpoint does not lose the others. `nodes` defaults to the
        board's clusters and vector cores.

        THIS IS THE ONLY HONEST CYCLE MEASUREMENT AVAILABLE. Wall clock cannot
        substitute: one JTAG access is ~32 ms against ~14 us of compute, so
        transport dominates the interval by three orders of magnitude and
        subtracting an estimate of it has already produced a negative residue.

        THREE CU_CTRL ROUND TRIPS PER NODE, each a mailbox write, a kick and a
        poll -- tens of transport accesses. Take it around a run, not inside
        one. The counters accumulate and cannot be cleared, so a run is
        measured as :func:`delta_counters` of a read before and a read after.
        """
        want = (
            nodes if nodes is not None else [*self.board.clusters, *self.board.vectors]
        )
        out = {}
        for node in want:
            key = f"{node[0]},{node[1]}"
            try:
                caps = self._node_kind(node, timeout)
                row = {"kind": caps["name"]}
                row.update(
                    dev.decode_counters(
                        self.ctrl_read(tuple(node), dev.CU_COUNTERS, timeout)
                    )
                )
                row.update(
                    dev.decode_dbg(
                        self.ctrl_read(tuple(node), dev.CU_DBG, timeout), caps["type"]
                    )
                )
                row.update(
                    dev.decode_status(
                        self.ctrl_read(tuple(node), dev.CU_STATUS, timeout)
                    )
                )
            except HardwareError as exc:
                out[key] = {"error": str(exc).split(".")[0]}
                continue
            out[key] = _with_dispatch(row)
        return out

    def _node_kind(self, node, timeout=None) -> dict:
        """Decoded CU_CAPS for `node`, read once and remembered."""
        key = tuple(node)
        if key not in self._caps:
            self._caps[key] = dev.decode_caps(self.ctrl_read(key, dev.CU_CAPS, timeout))
        return self._caps[key]

    # ---- unit-to-unit transfers -----------------------------------------
    def push_l1(self, node, words, buf_id=0, offset=0, timeout=None) -> dict:
        """Write `words` straight into a CU's L1 over the mesh, and confirm it.

        The host-originated half of `CU_DATA`: a descriptor and one flit per
        256-bit word, dispatched to `node`, waited on until that node reports
        `SIG_DATA_RECEIVED`. Returns its decoded NODE_STATUS. Raises
        :class:`HardwareError` if the burst does not fit the staging RAM, and
        :class:`TimeoutError` via `Program.execute` if the node never reports.

        THE ACK IS OBSERVABLE ONLY BECAUSE THE HOST IS THE SENDER. The receiver
        answers the DESCRIPTOR'S SOURCE, and the dispatcher stamps that with
        the orchestrator's own coordinates, so the signal comes back to us and
        lands in NODE_STATUS. A CU-to-CU transfer answers the SENDING CU
        instead and is not visible here at all -- see `push_l1` in the module
        docstring of `ktpu.hw.vector` for what that costs.

        CREDIT IS SEEDED TO THE FLIT COUNT, not to `INST_DEPTH`. Credit is
        consumed per flit and refilled only by `SIG_INST_COMPLETE`, which a
        data flit never produces, so a burst seeded at 32 stalls forever on its
        33rd flit. Sizing it to the burst is exact, and it is safe above
        `INST_DEPTH` here because these land in the receive queue and not in
        the instruction FIFO that bound is about.
        """
        flits = dev.cu_data_flits(words, buf_id=buf_id, offset=offset, signal=True)
        stage = self.board.stage_flits
        if len(flits) > stage:
            raise HardwareError(
                f"a {len(flits)}-flit burst does not fit the {stage}-flit staging "
                f"RAM on board {self.board.name!r}; split it"
            )
        node = tuple(node)
        with self.lock:
            before = self._signals(node)
            prog = dev.Program()
            for slot, flit in enumerate(flits):
                prog.stage_flit(slot, flit)
            prog.seed_credits(len(flits))
            prog.kick(node[0], node[1], base=0, nflits=len(flits))
            prog.await_signal(
                node[0],
                node[1],
                dev.SIG_DATA_RECEIVED,
                (before + 1) & 0xFFFF,
                arg=buf_id,
            )
            prog.done(0xDA7A)
            prog.execute(
                self.link, timeout=self.timeout if timeout is None else timeout
            )
        return self.node_status(node)

    def node_status(self, node) -> dict:
        """One node's NODE_STATUS mirror, decoded."""
        return vector.decode_node_status(
            self.link.read64(dev.node_status_addr(*tuple(node)))
        )

    def _signals(self, node) -> int:
        return self.node_status(node)["signals"]

    # ---- operands and results -------------------------------------------
    def upload(self, prob) -> int:
        """Push the FP16 operand image through MAG's memory window. Returns bytes.

        THE OPERANDS GO THROUGH THE HARDWARE, exactly as in the bench: the host
        writes FP16 and MAG stores it, verbatim or quantised as the marker on
        the write says. One `write_block` per region, so an operand is one DMA
        rather than a quarter of a million register writes.
        """
        words, regions = bench.upload(prob)
        sent = 0
        for src, dst, nwords, quant, blayout in regions:
            if quant and nwords % 8:
                raise HardwareError(
                    f"a quantising upload of {nwords} source words is not a whole "
                    "number of L1 entries; mag.v would wait for beats that never come"
                )
            payload = b"".join(
                w.to_bytes(MEM_WORD_BYTES, "little") for w in words[src : src + nwords]
            )
            self.raw.write_block(
                self.board.upload_addr(dst, bool(quant), blayout), payload
            )
            sent += len(payload)
        return sent

    def prefill(self, prob, word: int = 0xDEAD_BEEF_DEAD_BEEF) -> int:
        """Write a marker over the whole device image. Returns bytes written.

        THE DRAM HAS ECC, so a read of a never-written line is an uncorrectable
        error and comes back SLVERR. Without this, a machine that computed
        nothing fails the RESULT READBACK with a bus error rather than handing
        back a recognisable pattern -- which is the difference between
        diagnosing a hang and guessing at one. Worth the seconds on a card that
        has just been programmed or power-cycled.
        """
        _, end = bench.plan(prob.m, prob.k, prob.n, prob.tile, prob.kern.preq)
        payload = word.to_bytes(WORD_BYTES, "little") * (MEM_WORD_BYTES // WORD_BYTES)
        self.raw.write_block(self.board.mem_byte(0), payload * end)
        return end * MEM_WORD_BYTES

    def read_c(self, prob) -> list:
        """Device memory words covering C, indexed from the base of memory.

        `bench.decode_result` walks tiles from the base, so the C region is
        read and the untouched prefix is padded rather than transferred: A and
        B are already known and reading them back would double the run.
        """
        layout, end = bench.plan(prob.m, prob.k, prob.n, prob.tile, prob.kern.preq)
        try:
            raw = self.raw.read_block(
                self.board.mem_byte(layout.c_word),
                (end - layout.c_word) * MEM_WORD_BYTES,
            )
        except OSError as exc:  # SLVERR from ECC, or a dead link
            raise HardwareError(
                f"reading C at {self.board.mem_byte(layout.c_word):#x} failed: {exc}\n"
                "On this DRAM a never-written line reads as an uncorrectable ECC "
                "error, so this is what a machine that drained nothing looks like. "
                "Call Session.prefill(prob) before the run to tell the two apart."
            ) from exc
        step = MEM_WORD_BYTES
        tail = [
            int.from_bytes(raw[i : i + step], "little")
            for i in range(0, len(raw), step)
        ]
        return [0] * layout.c_word + tail

    # ---- the whole thing ------------------------------------------------
    def check(self, m, k, n, preq=PREQ, use=None) -> list[str]:
        """Every reason this shape will not run HERE. Empty means go.

        Deliberately not `bench.check`: two of its limits are the bench's file
        sizes, and refusing a shape that fits 4 GB of DDR because it would not
        fit an 8 MB `axi_ram` would be describing the wrong machine. The board's
        own capacity and its marker reachability replace them.
        """
        b = self.board
        why = []
        use = b.ncl if use is None else int(use)
        if not 1 <= use <= b.ncl:
            why.append(f"cannot dispatch to {use} of board {b.name!r}'s {b.ncl}")
        if m < tensor.LANES or n < tensor.LANES or k < tensor.KBLOCK:
            why.append(
                f"minimum shape is {tensor.LANES}x{tensor.KBLOCK}x{tensor.LANES}"
            )
        if why:
            return why
        if any(preq) and b.quant_bit is None:
            why.append(
                f"board {b.name!r} cannot mark an upload for quantisation, so "
                f"preq={tuple(bool(p) for p in preq)} would write FP16 into a "
                "region sized for MXFP7. Use preq=(False, False)."
            )
        # `bench.tile_for` rather than a copy of it: a second tile chooser would
        # describe a different machine from the one that runs.
        tile = bench.tile_for(m, k, n, b.caps())
        mp, kp, np_ = bench.padded_shape(m, k, n, tile)
        _, end = bench.plan(mp, kp, np_, tile, preq)
        if end > b.mem_words:
            why.append(
                f"padded to {mp}x{kp}x{np_} the image needs {end} words and board "
                f"{b.name!r} has {b.mem_words}"
            )
        return why

    def run(
        self,
        m,
        k,
        n,
        seed=0xC0FFEE,
        dist="lowrank",
        preq=PREQ,
        use=None,
        packing="spread",
        transform=None,
        profile=False,
        strict_wait=True,
    ):
        """Build this shape, run it on the card, and score it like a simulation.

        Returns :func:`ktpu.hw.sim.payload`, plus the board, the agent's
        counters and the transport. Raises :class:`HardwareError` if the shape
        cannot run here, if the machine does not answer, or if a round does not
        retire; :class:`ValueError` on a bad distribution.

        `profile` reads every node's own counters before and after and attaches
        the difference as ``payload["node_counters"]`` -- the per-node compute,
        memory and dispatch cycles, and the only basis on which GFLOP/s here
        means anything. It costs several CU_CTRL round trips per node at both
        ends, so it is off by default and the timing of the run itself is
        unaffected either way: the reads bracket the round, they do not
        interleave with it.

        `strict_wait` waits per cluster rather than on the shared completion
        counter (:meth:`_strict`), which is the fix for a round being released
        early by somebody else's signal. On by default, because the alternative
        is a known data-corrupting race; pass False to reproduce the old
        behaviour, which is how the two are told apart on the card.

        `ncl` is absent on purpose: the mesh is compiled into the bitstream, so
        the count is the board's and not a parameter.
        """
        if dist not in bench.DISTRIBUTIONS:
            raise ValueError(f"unknown distribution {dist!r}")
        b = self.board
        with self.lock:
            why = self.check(m, k, n, preq, use)
            if why:
                raise HardwareError("; ".join(why))
            use = b.ncl if use is None else int(use)

            # The same caps `check` measured against; building under any
            # others would validate one shape and run a different one.
            prob = bench.build(
                m,
                k,
                n,
                seed,
                ncl=b.ncl,
                dist=dist,
                preq=preq,
                use=use,
                packing=packing,
                mesh_layout=b.layout,
                caps=b.caps(),
            )
            if transform is not None:
                transform(prob)

            log = [f"BOARD {b.name} layout={b.layout} ncl={b.ncl} use={use}"]
            # OUTSIDE the timed region, both of them: these are tens of JTAG
            # accesses and would swamp the `seconds` they bracket. Answering a
            # CU_CTRL touches neither counter -- it is not an instruction and
            # does not raise `busy` -- so reading does not perturb the reading.
            was = self.node_counters() if profile else None
            t0 = time.time()
            self.handshake()
            self.reset()
            log.append(f"UPLOAD bytes={self.upload(prob)}")
            rounds = prob.kern.rounds
            if strict_wait and len(rounds) != len(prob.programs):
                raise HardwareError(
                    f"{len(prob.programs)} programs against {len(rounds)} rounds; "
                    "a per-cluster wait pairs them by index and would wait for "
                    "the wrong round's passes"
                )
            for i, prog in enumerate(prob.programs):
                code = self._round(
                    i, prog, log, rounds[i].passes if strict_wait else None
                )
                log.append(f"ROUND {i} code={code:#x} cmds={len(prog.cmds)}")
            got = bench.decode_result(self.read_c(prob), prob)
            elapsed = time.time() - t0
            per_node = delta_counters(was, self.node_counters()) if profile else None

            tel = self.counters()
            log.append(f"SIGDONE {tel['sig_done']}")
            log.append("ORCH-OK")
            payload = sim.payload(
                prob,
                got,
                log,
                asked={"m": m, "k": k, "n": n},
                seed=seed,
                dist=dist,
                cold=False,
                seconds=elapsed,
                budget=self.timeout,
                mesh_layout=b.layout,
            )
        payload["board"] = b.to_dict()
        payload["counters"] = tel
        payload["node_counters"] = per_node
        payload["transport"] = repr(self.raw)
        return payload

    def _open(self, transport):
        if isinstance(transport, dev.Transport):
            self._owned = False
            return self.board.view(transport)
        self._owned = True
        return _board.open_transport(transport, self.board)

    def _round(self, i, prog, log, passes=None):
        """One round, with the machine's own state attached to a failure.

        A poll that never matches says which register did not move; the node
        counters say which CLUSTER did not report, which is the difference
        between "it hung" and "cluster 3 hung".

        `passes` rebuilds the round's wait to be per cluster; see
        :meth:`_strict`. The STAGED FLITS are untouched either way -- only the
        control commands differ, so what the machine computes cannot change.
        """
        if passes is not None:
            prog = self._strict(prog, passes)
        try:
            return prog.execute(self.link, timeout=self.timeout)
        except TimeoutError as exc:
            raise HardwareError(
                f"round {i} did not retire: {exc}\ncounters: {self.counters()}\n"
                + "\n".join(log)
            ) from exc

    def _strict(self, prog, passes):
        """This round's program, waiting per cluster instead of on `A_SIG_DONE`.

        `A_SIG_DONE` counts completions from EVERY node. In a run where a
        vector core also signals -- a kernel retiring, or `SIG_DATA_RECEIVED`
        from a CU_DATA burst -- a wait sized for this dispatch is satisfied by
        somebody else's traffic, the round is released early, and the next
        `upload` overwrites the staging RAM under a dispatcher still streaming
        out of it. That corrupts exactly the passes in flight.

        HOST-ONLY, AND NECESSARILY SO. `node_status` is cumulative and nothing
        clears it, so the target is a baseline read plus this round's flits --
        a read-then-compute that WR/POLL/DONE cannot express. `await_all` is
        portable to `main_orch` because `clear_done` makes it absolute; this is
        not, and no synthesised mesh has a `main_orch` to run it on.

        The baseline is re-read every round rather than tracked, so an
        unexpected signal -- a fault, a peer's ack -- resynchronises instead of
        leaving every later round waiting for a count that has moved.
        """
        base = {}
        for p in passes:
            c = tuple(p.cluster)
            if c not in base:
                base[c] = self._signals(c)
        staged = dev.Program(setup=list(prog.setup))
        return kernel.control(passes, prog=staged, baseline=base)
