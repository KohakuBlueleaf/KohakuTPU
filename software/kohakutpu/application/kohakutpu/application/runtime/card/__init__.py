"""Node programs on a card: boot once, place arrays, run, time, read back.

    card = VerilatorCard(build, board="multimesh_v9")                         # the Verilated card
    card = SiliconCard("multimesh_v9t4", nodes=(0,))    # the card, over JTAG
    card = SiliconCard("multimesh_v9t6", transport="xdma")  # over PCIe (Linux)
    a = card.put(np.float16 array)      # unit-global byte address
    got = card.run(program)             # node cycles, per-step ends, phases
    card.get(a, nbytes)
    card.close()

    got = card.run({0: p0, 1: p1})      # one package a node, all submitted first

`NodeCard` is the one runtime; the two cards differ only in what they are
handed: the transport, how the host reaches DRAM, how a poll idles, how a
firmware image is placed, and the arena. The machine programs are built for
comes from the board (`target.machine_of`) and is checked against the
enumerated units at boot.

Before a set of programs first runs, the static checker runs on it
(`check="refuse"`: a finding of a `verify.REFUSE` kind raises
`verify.Refused`; "warn" prints the findings and runs; "off").

`cycles` is the package's own count (node clock); with `steps=True` the
firmware prints each step's end (KA_BOOT_F_STEPS), prints excluded. The nodes
share DRAM (the Xache); each node's queue sits in its own staging store.
"""

import json
import os
import pathlib
import re
import sys
import tempfile
import time

import numpy as np

from kohakuaccel.driver.node.boot import BootArgs, NodeBoot, abi
from kohakuaccel.driver.node.boot.args import REGION_DEBUG, REGION_QUEUE, VERSION, bound
from kohakuaccel.driver.node.queue import (
    BootRefused,
    FirmwareFatal,
    OverBudget,
)
from kohakuaccel.driver.node.queue import layout as QL
from kohakuaccel.driver.transport.jtag import JtagTransport
from kohakuaccel.driver.transport.rebase import UnitGlobal
from kohakuaccel.driver.transport.simdram import SimDram
from kohakuaccel.driver.transport.verilator import VerilatorTransport
from kohakuaccel.driver.transport.xdma_linux import XdmaLinux
from kohakutpu.application.runtime import execution
from kohakutpu.application.runtime import packages as B
from kohakutpu.application.runtime import packages as verify
from kohakutpu.application.runtime.card.collection import Collection
from kohakutpu.application.runtime.card.common import (
    _UNSET,
    ARENA,
    DBG_LASTS,
    DEBUG_FIRMWARE,
    DEBUG_RECORDS,
    FIRMWARE,
    FLAGS_ENV,
    KINDS,
    MACHINES,
    Q_SIZE,
    RESIDENT_ENV,
    CardMismatch,
    ClockMismatch,
    ResidentMode,
    enumerated,
    firmware_wait_seconds,
    node_queue,
    parse_resident_mode,
    report,
)
from kohakutpu.application.runtime.card.recording import Recording
from kohakutpu.application.runtime.card.submission import Submission
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.application.runtime.memory import CardMemory
from kohakutpu.application.runtime.native.exchange import NativeExchange
from kohakutpu.application.runtime.native.poll import NativePoll
from kohakutpu.driver import units as _units  # noqa: F401  (names MG and VC)
from kohakutpu.driver.clock.card import BOARDS, CardClocks, load_board
from kohakutpu.driver.host import SCRATCH_RESERVE, Card, board_map
from kohakutpu.language import machine as MF
from kohakutpu.language.config import ENV
from kohakutpu.language.dsl.kernel import _MUTANT
from kohakutpu.stack.rs import _rs

F_TIMING, F_STEPS = 2, 8


class NodeCard(Recording, Submission, Collection):
    """The runtime over one card: `t` the transport, `host` the DRAM path the
    host writes through, `idle` what a queue poll does between reads, `boot`
    the firmware placement ("axi" through the load window, "burn" a model's
    arrays), arrays in [ARENA, `arena_end`)."""

    _eager_batch = None
    native_direct = None
    native_direct_suspended = False
    _publication_epoch = 0

    @property
    def resident_mode(self) -> ResidentMode:
        if "_resident_mode" in self.__dict__:
            return self._resident_mode
        return parse_resident_mode(ENV.get(RESIDENT_ENV))

    @resident_mode.setter
    def resident_mode(self, value: str | ResidentMode | None) -> None:
        self._resident_mode = parse_resident_mode(value)

    @property
    def submit_cache(self) -> bool:
        if "_submit_cache" in self.__dict__:
            return self._submit_cache
        return ENV.get("KOHAKUTPU_HOST_SUBMIT_CACHE", "1") == "1"

    @submit_cache.setter
    def submit_cache(self, value: bool) -> None:
        self._submit_cache = value

    @property
    def admission_cached(self) -> bool:
        if "_admission_cached" in self.__dict__:
            return self._admission_cached
        return B.admission_cached()

    @admission_cached.setter
    def admission_cached(self, value: bool) -> None:
        self._admission_cached = value

    def __init__(
        self,
        t,
        host,
        board: str,
        *,
        nodes: tuple[int, ...] = (0,),
        fw=None,
        steps=False,
        machine=None,
        timeout: float = 300.0,
        check: str = "refuse",
        idle=None,
        boot: str = "axi",
        arena_end: int | None = None,
        spin: float = 0.0,
        sys_hz: float | None = None,
        wait_seconds: float | None = None,
        debug: bool = False,
        stop_timeout: float = 5.0,
        clock=time.monotonic,
        alarm: float = 0.0,
        resident_mode: str | None | object = _UNSET,
    ) -> None:
        if check not in verify.GATES:
            raise ValueError(f"check={check!r}: one of {verify.GATES}")
        self.t, self.host = t, host
        #: Seconds a node has to complete STOP (`stop`) before it is restarted.
        self.stop_timeout = stop_timeout
        #: The events since `record`, None while not recording.
        self.log: list | None = None
        self.nodes: tuple[int, ...] = tuple(nodes)
        self.check = check
        #: (node, id(program)) pairs -> (the programs, their findings)
        self.checked: dict = {}
        #: node -> tags submitted (`submit`) whose report text is not yet
        #: read, in order; node -> text read but not yet split; node -> tag
        #: -> its run's text (`_segment`)
        self._order: dict = {}
        self._stdout: dict = {}
        self._segs: dict = {}
        self._eager_batch = None
        self.native_direct = None
        self.native_direct_suspended = False
        self._publication_epoch = 0
        self._resident_mode = parse_resident_mode(
            ENV.get(RESIDENT_ENV) if resident_mode is _UNSET else resident_mode
        )
        self._submit_cache = ENV.get("KOHAKUTPU_HOST_SUBMIT_CACHE", "1") == "1"
        self._admission_cached = B.admission_cached()
        self.timeout = timeout
        #: `clock() -> seconds` of the node's time, which every wait counts.
        self.clock = clock
        self.board = load_board(board)
        mp = board_map(self.board)
        span = max(self.nodes) + 1
        #: The memory port a fetched dispatch reads through, from the board.
        b = self.board
        self.fetch = tuple(b["fetch_port"]) if "fetch_port" in b else None
        self.dram_bytes = mp["size"]
        arena_end = mp["arena_limit"] if arena_end is None else arena_end
        if not ARENA <= arena_end <= mp["arena_limit"]:
            raise ValueError("arena_end must fit below the machine's DRAM scratch")
        self.arena_end = arena_end
        card = Card.from_board(
            board,
            transport=host,
            which=list(self.nodes),
            verify=False,
            reply_at_agent=True,
        )
        #: The machine file: the build's facts, and the names the debug
        #: decoder gives codes.
        self.machine_json = (MACHINES / f"{self.board['name']}.json").read_text(
            encoding="utf-8"
        )
        self.queue_layout = QL.Layout.from_schema(
            json.loads(self.machine_json)["machine"]["fw"]["queue"]
        )
        # Programs are built for the units the card enumerates, which must be
        # its board's machine file's; the board gives addresses and clocks.
        found = enumerated(card.meshes)
        listed = json.loads(self.machine_json)["machine"]["die"]["units"]
        for kind in KINDS:
            want = sorted(tuple(c) for c in listed[kind.lower()])
            if sorted(tuple(c) for c in found[kind]) != want:
                raise CardMismatch(
                    f"the card's {kind} units are {found[kind]}, machine file "
                    f"{self.board['name']}'s {want}"
                )
        self.machine = machine or machine_of(self.board, nodes=span, units=found)
        for mesh in card.meshes:
            self.check_units(mesh)
        self.mem = UnitGlobal(host, mp["dram"], mp["mem"], mp["size"], staging=True)
        #: The host's whole write: a DRAM write is padded to it. Where the
        #: host's reads are wider than the memory endpoint's write granule
        #: (XDMA lines), every line a read may cover is painted first
        #: (`paint`): never-written ECC DRAM answers with an error.
        wide = getattr(host, "line", 0)
        self.line = max(mp["granule"] or 32, wide)
        self.paint = wide > (mp["granule"] or 32)
        #: The one owner of the card's DRAM arrays and every node's staging
        #: store past its queue: arrays, scratch and spilled packages alike.
        self.memory = CardMemory(
            (ARENA, arena_end),
            {
                m.index: (
                    self.machine.stage_addr(0, mesh=m.index),
                    self.machine.stage_addr(0, mesh=m.index) + self.machine.stage_bytes,
                )
                for m in card.meshes
            },
            paint=self.mem.write_block if self.paint else None,
        )
        # Timing comes from run records; PKGT text is opt-in via FLAGS_ENV.
        flags = (F_STEPS if steps else 0) | int(ENV.get(FLAGS_ENV, "0"), 0)
        # The firmware's wait bound, from seconds at the profile's sysnode clock
        # (RUL.033), inside the host's own wait (DBG.SESS.003).
        hz = sys_hz or self.board["profiles"][self.board["idle_profile"]]["sys"] * 1e6
        #: The sysnode clock the firmware counts its waits in.
        self.sys_hz = hz
        seconds = firmware_wait_seconds(self.timeout, wait_seconds)
        #: node -> (base, bytes) of its debug region, assigned once
        self.debug_regions: dict = {}
        #: node -> its queue, in its own staging store
        self.queues = {}
        #: node -> (its loader, its boot block), for :meth:`restart`
        self.boots = {}
        native_run = (
            _rs.QueueRun(self.queue_layout)
            if ENV.get("KTPU_NATIVE_QUEUE", "0") == "1"
            else None
        )
        for mesh in card.meshes:
            i = mesh.index
            sim = t if boot == "burn" else None
            loader = NodeBoot(t, mp["ctrl"][i], i, sim=sim)
            # A firmware left running (a session that never sent STOP) still
            # writes its queue: stop the core before the header is rewritten.
            loader.win.stop()
            queue_base = self.memory.stage(Q_SIZE, i, owner=f"firmware:queue:{i}")
            q = node_queue(
                self.mem,
                queue_base,
                self.memory,
                i,
                idle=idle,
                spin=spin,
                timeout=self.timeout,
                clock=clock,
                alarm=alarm,
                layout=self.queue_layout,
            )
            args = BootArgs(
                queue=q.base,
                mesh=i,
                scan=mesh.caps.grid_hi + 1,
                flags=flags,
                timeout=bound(seconds, hz / 1e6),
            )
            q.native_run = native_run
            self.queues[i] = q
            self.boots[i] = (loader, args)
        self.q = self.queues[self.nodes[0]]
        self.runs = 0
        self._place = boot
        self.load(fw, debug=debug)

    def load(self, fw=None, debug: bool = False) -> None:
        """Every node stopped and booted with image `fw` (by default the
        production image, or the debug image with `debug`): queues
        re-initialised, the vector cores' resident images forgotten. A debug
        image gets a debug region (assigned once per node, kept across loads).
        Raises `abi.AbiMismatch` for an image of another boot block version,
        or a debug load of an image without debug features (DBG.SEL.003)."""
        fw = pathlib.Path(fw or (DEBUG_FIRMWARE if debug else FIRMWARE))
        #: What the image speaks (its .ka_abi words).
        got = abi.check(fw, VERSION, require=True)
        conversion = json.loads(self.machine_json)["machine"]["conversion"]["fp16_mx7"]
        if conversion["version"] >= 2 and not got.get("package_features", 0) & 1:
            raise abi.AbiMismatch(
                f"{fw}: missing package contract/address capability; rebuild firmware"
            )
        if debug and not got["debug"]:
            raise abi.AbiMismatch(f"{fw}: no debug features (DBG.SEL.003)")
        self.abi, self.debug, self.fw = got, debug, fw
        record = self.record_layout(got["queue"])
        for i, (loader, args) in self.boots.items():
            loader.win.stop()
            self.queues[i].speak(got["queue"], record, layout=self.queue_layout)
            self.queues[i].track_ends = True
            self.queues[i].init()
            q = self.queues[i]
            args.regions = [(q.base, q.size, REGION_QUEUE)]
            if debug:
                if i not in self.debug_regions:
                    units = json.loads(self.machine_json)["machine"]["fw"][
                        "max_pkg_units"
                    ]
                    rec = (DBG_LASTS + units * (1 + got["last"]) * 8 + 63) & ~63
                    size = rec + DEBUG_RECORDS * 64
                    self.debug_regions[i] = (
                        self.memory.alloc(size, owner=f"firmware:debug:{i}"),
                        size,
                    )
                base, size = self.debug_regions[i]
                # No header: the firmware writes its own at ready.
                self.mem.write_block(base, bytes(DBG_LASTS))
                args.regions.append((base, size, REGION_DEBUG))
            loader.load(fw, args, self._place)
        for i in self.queues:
            self._wait_ready(i)
            self._forget(i)
        #: Each node's vector cores' instruction memory and descriptors.
        self.residents = {i: _rs.Cores(self.machine_json) for i in self.queues}
        self.resident = self.residents[self.nodes[0]]

    def _wait_ready(self, i: int, timeout: float = 120.0) -> None:
        loader, _ = self.boots[i]
        try:
            self.queues[i].wait_ready(timeout)
        except FirmwareFatal as e:
            console = getattr(loader, "console", None)
            if not e.console and console is not None:
                tap = console()
                if tap:
                    raise type(e)(e.state, tap) from None
            raise

    def record_layout(self, version: int) -> "QL.Record | None":
        """The schema's run record layout, or None before the record-ring version."""
        if version < self.queue_layout.version:
            return None
        return self.queue_layout.record

    @property
    def timeout(self) -> float:
        """Seconds on the queues' clock (the node's time) a package may take
        before its wait raises; every node queue's wait bound."""
        return self._timeout

    @timeout.setter
    def timeout(self, seconds: float) -> None:
        self._timeout = seconds
        for q in getattr(self, "queues", {}).values():
            q.timeout = seconds

    @property
    def alarm(self) -> float:
        """Wall seconds any one wait runs before it raises `OverBudget` (0:
        none); every node queue's."""
        return self.q.alarm

    @alarm.setter
    def alarm(self, seconds: float) -> None:
        for q in self.queues.values():
            q.alarm = seconds

    def restart(self, ready: float = 120.0, nodes=None) -> None:
        """Each node of `nodes` (every node by default) has its firmware
        started again from the image its instruction memory still holds,
        after a reset that kept the arrays (a wizard relock resets every
        sysnode and mesh; DRAM, staging and the placed buffers survive).
        Queues re-initialised; the vector cores' resident images are
        forgotten, so the next run installs them again. Raises
        `TimeoutError` when a node is not ready within `ready` seconds."""
        which = list(self.boots) if nodes is None else list(nodes)
        for i in which:
            loader, args = self.boots[i]
            loader.win.stop()
            self.queues[i].init()
            self._forget(i)
            loader.restart(self.fw, args)
        for i in which:
            self._wait_ready(i, ready)
            self.residents[i] = _rs.Cores(self.machine_json)
        self.resident = self.residents[self.nodes[0]]

    def _forget(self, n: int) -> None:
        """Node `n`'s queue started afresh: no run of it is waited for."""
        for d in (self._order, self._stdout, self._segs):
            d.pop(n, None)

    def check_units(self, mesh) -> None:
        """Raise :class:`CardMismatch` unless the mesh's units are the machine's.
        Programs name units by coordinate, so the scan order does not matter."""
        for kind, want in self.machine.mesh(mesh.index).units.items():
            got = mesh.coords(kind)
            if sorted(got) != sorted(want):
                raise CardMismatch(
                    f"mesh {mesh.index}'s {kind} units are {list(got)}, the "
                    f"machine's {list(want)}: a program built for one runs wrong "
                    f"on the other"
                )

    # ---------------------------------------------------------- memory
    def alloc(self, nbytes: int) -> int:
        return self.memory.alloc(nbytes)

    def free(self, addr: int) -> None:
        self.memory.free(addr)

    def mark(self) -> tuple:
        """What is held now; `release` frees everything placed since."""
        return self.memory.mark()

    def release(self, mark: tuple) -> None:
        self.memory.release(mark)

    def drop_spilled(self) -> None:
        """Every node's packages placed outside its queue's heap given back."""
        for q in self.queues.values():
            q.drop_spilled()

    def stage(self, nbytes: int, node: int | None = None) -> int:
        """`nbytes` of a node's MAG staging store (the card's first by
        default), past its queue."""
        return self.memory.stage(nbytes, self.nodes[0] if node is None else node)

    def place(self, nbytes: int, space: str = "dram", node: int | None = None) -> int:
        """`nbytes` in DRAM or, for ``space == "stage"``, in a node's staging."""
        if space == "stage":
            return self.stage(nbytes, node=node)
        return self.alloc(nbytes)

    def put(self, array, space: str = "dram", node: int | None = None) -> int:
        raw = np.ascontiguousarray(array).tobytes()
        at = self.place(len(raw), space, node)
        self.mem.write_block(at, raw)
        return at

    def write(self, at: int, raw: bytes) -> None:
        if (
            self._eager_batch is not None
            and _MUTANT.get() != "app.batch/write_skips_flush"
        ):
            self._eager_batch.flush()
        if self.log is not None:
            self.log.append(("write", at, bytes(raw)))
        self.mem.write_block(at, raw)

    def get(self, at: int, nbytes: int) -> bytes:
        if self._eager_batch is not None:
            self._eager_batch.flush()
        if self.log is not None:
            self.log.append(("read", at, nbytes))
        return self.mem.read_block(at, nbytes)

    prepare_execution = execution.prepare

    def stop(self) -> None:
        """Ask every node's firmware to leave its loop, each within
        `stop_timeout` seconds. A node that does not complete STOP (a run
        left it busy) is restarted (`restart`, ready within the same bound)
        and asked again; one that still does not is halted at its core
        (`win.stop`). Never waits past those bounds."""
        for i, q in self.queues.items():
            try:
                q.stop(timeout=self.stop_timeout)
                continue
            except (TimeoutError, FirmwareFatal, OverBudget) as e:
                why = f"{type(e).__name__}: {str(e)[:200]}"
            print(
                f"card.stop: node {i} did not complete STOP in {self.stop_timeout}s "
                f"(pending tags {sorted(q._outstanding)}; {why}); restarting it",
                file=sys.stderr,
            )
            try:
                self.restart(ready=self.stop_timeout, nodes=(i,))
                q.stop(timeout=self.stop_timeout)
            except (TimeoutError, RuntimeError) as e:
                print(
                    f"card.stop: node {i} still not stopped after a restart "
                    f"({type(e).__name__}: {str(e)[:200]}); halted at its core",
                    file=sys.stderr,
                )
                self.boots[i][0].win.stop()


#: Sysnode cycles one idle of a Verilated card's wait advances the model.
IDLE_CYCLES = 600
#: Wall seconds a Verilated card's wait runs before `OverBudget`.
WALL_ALARM = 300.0


class VerilatorCard(NodeCard):
    """The Verilated card: host DRAM traffic through the model's arrays
    (`SimDram`), polls advance the model, firmware burnt into its memories."""

    def __init__(
        self, build, board: str | None = None, log=None, args=(), **kw
    ) -> None:
        board = board or ENV.get("KOHAKUTPU_BOARD")
        if not board:
            raise ValueError("RUL.011: name a board with board= or KOHAKUTPU_BOARD")
        MF.load(board)
        b = load_board(board)
        self.build = pathlib.Path(build)
        # A log of its own: cards run in parallel on one build.
        log = log or pathlib.Path(tempfile.mkdtemp(prefix="ktpu_card_")) / "model.log"
        # `args`: harness flags (sim/verilator/docs/card-backend.md).
        t = VerilatorTransport(self.build, log=log, args=tuple(args))
        mp = board_map(b)
        host = SimDram(t, b, mp["mem"], mp["size"])
        # A mismatched model build must not silently wrap allocated DRAM.
        actual = sum(words * width for _, _, words, width in host.dram)
        if ("model" in b and actual != mp["size"]) or not (
            SCRATCH_RESERVE < actual <= mp["size"]
        ):
            t.close()
            raise ValueError(f"model DRAM {actual} != machine DRAM {mp['size']}")
        if actual < mp["size"]:
            kw.setdefault("arena_end", actual - SCRATCH_RESERVE)
        # The firmware's wait bound: 20 ms of the card's sysnode clock
        # (2,000,000 cycles at 100 MHz). The host's waits count the same
        # modelled time (`clock`), so the firmware's TIMEOUT ends a stuck wait
        # before the host's bound at any model speed; `alarm` is wall seconds.
        kw.setdefault("wait_seconds", 0.02)
        kw.setdefault("stop_timeout", 2 * kw["wait_seconds"])
        kw.setdefault("alarm", WALL_ALARM)
        #: Sysnode cycles the host's idles advanced the model: the queues'
        #: clock (host traffic advances it further, uncounted).
        self.idled = 0
        super().__init__(
            t,
            host,
            board,
            idle=self._idle,
            boot="burn",
            clock=self.modelled,
            **kw,
        )

    def _idle(self) -> None:
        self.t.run(IDLE_CYCLES)
        self.idled += IDLE_CYCLES

    def modelled(self) -> float:
        """Seconds of the card's sysnode clock the host's idles modelled."""
        return self.idled / self.sys_hz

    def close(self) -> dict:
        """Stop the model; each vector core's counters from its log."""
        self.stop()
        for q in self.queues.values():
            q.discard_packages()
        self.t.close()
        return counters(self.t.model_log)


#: Seconds a silicon wait polls back to back before sleeping between polls:
#: MNIST's kernels run ~0.16 ms each at the low profile, against a 20 ms sleep.
SPIN = 0.05

#: Ways to reach the card: JTAG through Vivado's Tcl server (any host that
#: runs Vivado), XDMA on a Linux host whose PCIe slot holds the card.
TRANSPORTS = ("jtag", "xdma")


def open_transport(kind: str = "jtag"):
    """A transport of `kind` (`TRANSPORTS`). XDMA recovers a hung engine by a
    link reset when ``$XDMA_KO`` names the driver module."""
    if kind == "jtag":
        return JtagTransport()
    if kind == "xdma":
        return XdmaLinux(recover="auto" if ENV.get("XDMA_KO") else None)
    raise ValueError(f"transport {kind!r}: one of {TRANSPORTS}")


class SiliconCard(NodeCard):
    """The card itself: `transport` (an instance, or a `TRANSPORTS` name;
    JTAG by default) for everything; a wait polls back to back for `spin`
    seconds, then sleeps `poll` seconds between polls; firmware streamed
    through each node's load window.

    Opens only at `profile` ("idle": the board's idle profile) on every node
    used, else raises :class:`ClockMismatch`: a session never starts at rates
    nobody chose. `profile=None` skips the check (a clock ladder sets them)."""

    def __init__(
        self,
        board: str,
        transport=None,
        poll: float = 0.02,
        profile: str | None = "idle",
        idle=None,
        spin: float = SPIN,
        **kw,
    ) -> None:
        if transport is None or isinstance(transport, str):
            transport = open_transport(transport or "jtag")
        t = transport
        native_poll = ENV.get("KTPU_NATIVE_POLL", "0") == "1"
        native_exchange = ENV.get("KTPU_NATIVE_EXCHANGE", "0") == "1"
        if (native_poll or native_exchange) and not isinstance(t, XdmaLinux):
            t.close()
            raise ValueError(
                "KTPU_NATIVE_POLL / KTPU_NATIVE_EXCHANGE require Linux XDMA"
            )
        b = load_board(board)
        want = b.get("idle_profile", "low") if profile == "idle" else profile
        if want is not None:
            clocks = CardClocks(t, b)
            for n in kw.get("nodes", (0,)):
                got = clocks.mesh(n).read_all()
                rates = b["profiles"][want]
                off = {
                    k: (got[k], v) for k, v in rates.items() if abs(got[k] - v) > 1e-6
                }
                if off:
                    raise ClockMismatch(
                        f"node {n} is not at profile {want!r}: (now, want) {off}; "
                        f"set the clocks first (CardClocks.set_all({want!r}))"
                    )
        idle = idle or (lambda: time.sleep(poll))
        # The sysnode clock the firmware counts its waits in.
        sys_hz = b["profiles"][want]["sys"] * 1e6 if want is not None else None
        super().__init__(
            t, t, board, idle=idle, boot="axi", spin=spin, sys_hz=sys_hz, **kw
        )
        if native_poll:
            for queue in self.queues.values():
                queue.native_poll = NativePoll(queue, t)
        if native_exchange:
            for queue in self.queues.values():
                queue.native_exchange = NativeExchange(queue, t)

    def close(self) -> dict:
        """Stop every node's firmware; the card keeps its clocks."""
        self.stop()
        for q in self.queues.values():
            q.discard_packages()
        return {}


def counters(log: pathlib.Path) -> dict:
    """Per vector core, keyed ``DIE.CORE`` (the instance holding the mesh, then
    the core's): the VRES totals and the V2MATH issue breakdown that
    `v2_core.v` prints at the end of simulation."""
    out: dict = {}
    for ln in log.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"(VRES|V2MATH) (\S+) (.*)", ln)
        if not m:
            continue
        die, _, inner = m[2].partition(".u_mesh0.")
        core = f"{die.rsplit('.', 1)[-1]}.{inner.split('.')[0]}"
        rest = m[3].split()
        fields = {rest[i]: int(rest[i + 1]) for i in range(0, len(rest) - 1, 2)}
        out.setdefault(core, {})["vres" if m[1] == "VRES" else "math"] = fields
    return out


def boards() -> list:
    """Every board file's name."""
    return sorted(json.loads(p.read_text())["name"] for p in BOARDS.glob("*.json"))


__all__ = [
    "TRANSPORTS",
    "BootRefused",
    "CardMismatch",
    "ClockMismatch",
    "NodeCard",
    "SiliconCard",
    "VerilatorCard",
    "boards",
    "counters",
    "node_queue",
    "open_transport",
    "report",
]
