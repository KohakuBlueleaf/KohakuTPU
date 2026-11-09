"""Boot node-local residents for the application's model/performance entry."""

import json
import pathlib
import time

import klog
from boot import MESH_SHIFT, RG_DRAM_SHARED, load, parse_flags, regions, tables
from image import UIMG, segments
from run_card import Card, _close_card, arrays, load_node

from kohakuaccel.driver.device import rv64load
from kohakutpu.application.runtime.card import clocks_at
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.application.runtime.memory import PAINT_CHUNK
from kohakutpu.application.runtime.os import abi as A
from kohakutpu.application.runtime.os.graph import ResidentGraph, task_capacity
from kohakutpu.driver.clock.card import load_board
from kohakutpu.language.config import ENV


class _ReadyObserver:
    """Persist the minimal live boot observation before teardown can lose it.

    STATUS zero is not a stopped core. Do not reset/retry an uncertain boot:
    record its log header while waiting, and distinguish halt from exit.
    After READY this reduces to the ordinary exit check, with no header reads.
    """

    def __init__(self, card, window, node, log_pa, out):
        self.card, self.window, self.node = card, window, node
        self.log_pa = log_pa
        self.path = out / f"boot-ready.node{node}.jsonl"
        self.started = time.monotonic()
        self.next_sample = self.started + 30.0
        self.waiting = True

    def __call__(self):
        if not self.waiting:
            return self.window.exited()
        status = self.window.status()
        now = time.monotonic()
        if now >= self.next_sample or status & 0xC:
            record: dict[str, object] = dict(
                node=self.node,
                elapsed_seconds=now - self.started,
                status=status,
                log_header_pa=self.log_pa,
            )
            try:
                raw = self.card.read(self.log_pa, 64)
                if len(raw) != 64:
                    raise ValueError(f"short boot log header: {len(raw)} bytes")
                words = [
                    int.from_bytes(raw[k : k + 8], "little") for k in range(0, 64, 8)
                ]
                record.update(
                    header_hex=raw.hex(),
                    count=words[klog.COUNT],
                    where=words[klog.WHERE],
                    a=words[klog.A],
                    b=words[klog.B],
                    switches=words[klog.SWITCHES],
                )
            except Exception as error:  # noqa: BLE001
                # Persist a failed read as evidence, never as a zero header.
                record["header_error"] = repr(error)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record) + "\n")
            self.next_sample = now + 30.0
        if status & 8:
            return self.card.t.read64(self.window.base + rv64load.R_EXIT)
        if status & 4:
            raise RuntimeError(
                f"OS node {self.node} halted before READY (status {status:#x});"
                f" live boot observation: {self.path}"
            )
        return None


class ModelResidents:
    """One boot owner; all cores stop before any boot allocation is overwritten."""

    def __init__(self, board, nodes, os_board, image, capacity, data_bytes, out):
        spec = load(os_board)
        if spec["card"]["board"] != board or not 1 <= nodes <= spec["nodes"]:
            raise ValueError(
                "OS board must describe the selected card and participants"
            )
        capacity = task_capacity(capacity)
        if not 0 < capacity <= A.KDS_MAXTASKS or data_bytes <= 0:
            raise ValueError(
                "positive arena sizes within the ostpu task limit required"
            )
        spec["nodes"] = nodes
        spec["mesh_of"] = spec["mesh_of"][:nodes]
        if spec["mesh_of"] != list(range(nodes)):
            raise ValueError("model device nodes must match the OS mesh order")
        spec["units"] = [u for u in spec["units"] if u["mesh"] < nodes]
        machine = load_board(board)
        spec["staging"] = machine_of(machine, nodes=nodes).stage_bytes
        private = 3 * capacity * A.OSTPU_TASK_BYTES + (8 << 20) + 4095
        private = private // 4096 * 4096
        spec["dram"] = dict(
            spec["dram"], layout="carved", private=private, shared=data_bytes
        )
        spec["spelling"] = dict(private="own", shared="mesh0")
        # Ordinary DRAM on a flat card is shared across ingress meshes.
        # KOS must accept local-routed movers without widening private/stage access.
        spec["shared_route_mask"] = 0
        if machine.get("dram_flat_gb"):
            if int(machine["dram_mesh_shift"]) != MESH_SHIFT:
                raise ValueError(
                    "flat-card DRAM route differs from the KOS address ABI"
                )
            spec["shared_route_mask"] = 3 << MESH_SHIFT
        self.command = f"ostpu {capacity} 4096 1 1 -e"
        shared = next(r for r in regions(spec, 0) if r[2] == RG_DRAM_SHARED)
        self.arena = (shared[0], shared[0] + shared[1])
        self.image = pathlib.Path(image)
        self.spec, self.out = spec, out
        parts = segments(self.image)
        self.card = Card(spec, None, out / "model.log", True)
        self.windows = []
        self.sessions = {}
        self.closed = False
        try:
            if self.arena[1] > self.card.m["scratch"]:
                raise ValueError("OS arenas exceed the card's allocatable DRAM")
            # Track every selected core before any operation that can fail.
            self.windows = [
                rv64load.LoadWindow(
                    self.card.t, self.card.m["ctrl"][mesh] + rv64load.WINDOW_OFFSET
                )
                for mesh in spec["mesh_of"]
            ]
            for window in self.windows:
                window.stop()
            profile = ENV.get("KOHAKUTPU_PROFILE", "idle")
            if profile == "idle":
                profile = machine["idle_profile"]
            clocks_at(self.card.t, machine, profile, range(nodes))
            self.sys_hz = int(machine["profiles"][profile]["sys"] * 1e6)
            spec["clock_hz"] = self.sys_hz
            painted = set()
            zero = bytes(PAINT_CHUNK)
            for node, table in enumerate(
                tables(spec, parse_flags("shared_phys"), 0, "parallel " + self.command)
            ):
                base, size = regions(spec, node)[0][:2]
                spans = [(base, size)] + [
                    (pa, count)
                    for pa, count, kind, _ in regions(spec, node)
                    if kind == RG_DRAM_SHARED
                ]
                for pa, count in spans:
                    if pa in painted:
                        continue
                    for off in range(0, count, PAINT_CHUNK):
                        self.card.write(pa + off, zero[: min(PAINT_CHUNK, count - off)])
                    painted.add(pa)
                imem, spad, dram = arrays(self.image, table)
                for pa, data in parts:
                    if pa >= UIMG:
                        self.card.write(base + pa - UIMG, data)
                for pa, data in dram:
                    self.card.write(pa | spec["mesh_of"][node] << MESH_SHIFT, data)
                self.windows[node] = load_node(
                    self.card, node, spec["mesh_of"][node], imem, spad, "axi"
                )
            for window in reversed(self.windows):
                window.boot()
            for node, window in enumerate(self.windows):
                base, size = regions(spec, node)[0][:2]
                observer = _ReadyObserver(
                    self.card, window, node, base + klog.offset(size), out
                )
                self.sessions[node] = ResidentGraph(
                    self.card,
                    base + size - A.KOS_LAND_SIZE + A.KDS_LZ_USER,
                    timeout=1800,
                    exited=observer,
                    failure_path=out / f"kds-failures.node{node}.jsonl",
                )
                observer.waiting = False
            self.units = {
                (u["mesh"], u["x"], u["y"]): i for i, u in enumerate(spec["units"])
            }
        except BaseException:
            # A failing close raises with the boot error as its context.
            self.close()
            raise

    def close(self):
        """Each started node's console and kernel log ring go to `klog.json`;
        every selected core gets a stop attempt before the transport closes.

        A CPU stop is not a whole-card reset and does not authorize reclaiming
        an uncertain graph.
        """
        if self.closed:
            return
        self.closed = True
        try:
            nodes = []
            for node, window in enumerate(self.windows):
                base, size = regions(self.spec, node)[0][:2]
                ring = klog.parse(self.card.read(base + klog.offset(size), klog.LOG))
                nodes.append(
                    dict(
                        ring,
                        node=node,
                        status=window.status(),
                        console=window.console(),
                    )
                )
            if nodes:
                (self.out / "klog.json").write_text(json.dumps(nodes, indent=1))
        finally:
            _close_card(self.card, self.windows)
