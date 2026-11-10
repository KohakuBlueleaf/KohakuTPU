"""Real queue hooks: exact placement and readback before package reclamation."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import record

from kohakuaccel.driver.node.queue.client import Completion, NodeError, NodeQueue
from kohakuaccel.driver.node.queue.double import SpecFirmware
from kohakuaccel.driver.node.queue.group import PreparedQueueGroup
from kohakuaccel.driver.node.queue.layout import Layout
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime.eager_batch import Batch
from kohakutpu.application.runtime.graph.split import PreparedSplitGraph
from kohakutpu.application.runtime.offline import HostMemory
from kohakutpu.application.runtime.os.graph import ResidentGraph

MACHINE = Path(__file__).resolve().parents[3] / "machines/multimesh_v9t6.json"


class Memory(HostMemory):
    def __init__(self):
        super().__init__()
        self.next_address = 1 << 22
        self.package_reads = []
        self.faults = []
        self.fail_readback = False
        self.readback_dead = False

    def spill(self, size):
        address = self.next_address
        self.next_address += (size + 255) // 256 * 256
        return address

    def read_block(self, addr: int, nbytes: int) -> bytes:
        address, size = addr, nbytes
        assert not self.readback_dead, "read after known C2H timeout"
        if address >= 1 << 22:
            self.package_reads.append((address, size))
            if self.fail_readback and len(self.package_reads) == 2:
                self.readback_dead = True
                raise OSError("diagnostic C2H timeout")
        return super().read_block(address, size)

    def write_block(self, addr: int, data: bytes) -> None:
        assert not self.readback_dead, "write after known C2H timeout"
        return super().write_block(addr, data)

    def unspill(self, address):
        assert not self.readback_dead, "freed an uncertain package after C2H timeout"
        # An intentionally destructive free proves diagnostics run before reuse.
        self.write_block(address, bytes(128))


def install(trace, monkeypatch):
    for owner, names in (
        (D.Device, ("submit", "replay", "compile_graph")),
        (PreparedSplitGraph, ("submit",)),
        (NodeQueue, ("submit", "submit_list", "_retire_package", "wait", "poll")),
        (Batch, ("add", "publish")),
        (ResidentGraph, ("submit",)),
    ):
        for name in names:
            monkeypatch.setattr(owner, name, getattr(owner, name))
    record.recording(trace, None, True, record.Profile(None))


def launch(trace, queues):
    dev = SimpleNamespace(
        card=SimpleNamespace(queues=queues),
        mmu=SimpleNamespace(resolve=lambda at: (None, at)),
        recording=False,
    )
    compiled = SimpleNamespace(
        name="actual_failure", l1_text="", l2_text="", scratch=[]
    )
    trace.launch(dev, compiled, {}, "dsl", None, 0)


@pytest.mark.parametrize(
    "mode", ["success", "equal", "changed", "dead", "nonfinite", "readback_timeout"]
)
def test_recorded_package_is_read_only_on_failure_before_free(
    tmp_path, monkeypatch, mode
):
    trace = record.Trace(tmp_path)
    install(trace, monkeypatch)
    layout = Layout.from_machine(MACHINE)
    memory = Memory()
    memory.fail_readback = mode == "readback_timeout"
    queue = NodeQueue(
        memory,
        0,
        layout=layout,
        record=layout.record,
        spill=memory.spill,
        unspill=memory.unspill,
    )
    queue.init()
    uploaded = bytes(range(128))
    package_address = None

    def execute(raw, bindings):
        assert raw == uploaded
        if mode == "changed":
            assert package_address is not None
            changed = bytearray(raw)
            changed[65] = 0xEE
            memory.write_block(package_address, bytes(changed))
        status = (
            layout.ok if mode == "success" else (0x24 if mode == "nonfinite" else 0x14)
        )
        return SimpleNamespace(status=status, sent=1, time={})

    firmware = SpecFirmware(
        memory,
        0,
        interp=SimpleNamespace(run=execute),
        layout=layout,
        record=layout.record,
    )
    firmware.boot()

    def progress():
        firmware.step()
        if mode == "nonfinite":
            memory.write64(queue.geo["cq"] + layout.cq_status, 0x24 | 1 << 16)

    queue.idle = progress
    launch(trace, {2: queue})
    tag = queue.submit(uploaded)
    package_address = queue._cache[uploaded][0]
    trace.submitted(0)
    # Failure may surface while submitting the NEXT launch, not the failed one.
    trace.opened(dict(name="next_launch", phase="ordinary", kind="dsl"))
    if mode == "dead":
        # A trusted CQ was cached before another operation lost the transport.
        # With no run record, base wait() necessarily reads stdout off the card.
        queue._done[tag] = Completion(tag, 0x14, 0, 0, 0, 0)
        memory.faults.append(dict(kind="c2h", error="C2H timeout"))
        memory.readback_dead = True
    try:
        if mode == "success":
            assert queue.wait(tag).ok
        elif mode == "readback_timeout":
            with pytest.raises(OSError, match="diagnostic C2H timeout") as caught:
                queue.wait(tag)
            assert isinstance(caught.value.__cause__, NodeError)
            assert queue._quarantined and queue._cache
        else:
            with pytest.raises(
                NodeError, match="CONTRACT" if mode == "nonfinite" else "BAD_STEP"
            ):
                queue.wait(tag)
    finally:
        trace.submitted(0)
        trace.save({})
    rows = [
        json.loads(row)
        for row in (tmp_path / "launches.jsonl").read_text().splitlines()
    ]
    span = rows[0]["packages"][0]  # base has no recorded upload placement
    assert (span["node"], span["tag"], span["address"], span["nbytes"]) == (
        2,
        tag,
        package_address,
        len(uploaded),
    )
    assert not rows[1].get("packages")
    if mode == "success":
        assert not (tmp_path / "package-check.json").exists()
        assert memory.package_reads == [(package_address, len(uploaded))]
    else:
        check = json.loads((tmp_path / "package-check.json").read_text())["checks"][0]
        assert (check["seq"], check["node"], check["tag"]) == (0, 2, tag)
        if mode == "readback_timeout":
            assert check["outcome"] == "readback_error" and "C2H" in check["error"]
            assert memory.readback_dead
        elif mode == "dead":
            assert check["outcome"] == "skipped" and "C2H" in check["reason"]
            assert not memory.package_reads
            assert queue._quarantined and queue._cache
        else:
            assert check["equal"] == (mode in ("equal", "nonfinite"))
            assert check["first_differing_offsets"] == (
                [65] if mode == "changed" else []
            )
            assert check["status"] == (
                "NONFINITE_FP16" if mode == "nonfinite" else "BAD_STEP"
            )
            if mode == "changed":
                line = check["lines"][0]
                assert line["address"] == package_address + 64
                assert bytes.fromhex(line["actual_hex"])[1] == 0xEE
    if mode not in ("dead", "readback_timeout"):
        assert HostMemory.read_block(memory, package_address, 128) == bytes(128)
    else:
        # Inspect the double's host storage, not its forbidden transport API.
        assert HostMemory.read_block(memory, package_address, 128) == uploaded


@pytest.mark.parametrize("operation", ["wait", "poll", "submit"])
def test_known_c2h_fault_prevents_queue_entry_without_a_trusted_completion(
    tmp_path, monkeypatch, operation
):
    trace = record.Trace(tmp_path)
    install(trace, monkeypatch)
    layout = Layout.from_machine(MACHINE)
    memory = Memory()
    queue = NodeQueue(memory, 0, layout=layout)
    queue.init()
    launch(trace, {0: queue})
    memory.faults.append(dict(kind="c2h", error="C2H timeout"))
    memory.readback_dead = True
    try:
        with pytest.raises(OSError, match="queue access skipped"):
            if operation == "wait":
                queue.wait(123)
            elif operation == "poll":
                queue.poll()
            else:
                queue.submit(bytes(range(128)))
        assert queue._quarantined and queue._broken
        # No trusted CQ means no invented BAD_STEP attribution or readback.
        assert not (tmp_path / "package-check.json").exists()
    finally:
        trace.submitted(0)
        trace.save({})


def test_retained_failure_uses_middle_part_on_the_failing_sysnode(
    tmp_path, monkeypatch
):
    trace = record.Trace(tmp_path)
    install(trace, monkeypatch)
    layout = Layout.from_machine(MACHINE)
    memories = [Memory(), Memory()]
    queues = {
        node: NodeQueue(
            memory,
            0,
            layout=layout,
            record=layout.record,
            spill=memory.spill,
            unspill=memory.unspill,
        )
        for node, memory in enumerate(memories)
    }
    fail = [False]
    packages = {
        node: tuple(bytes([10 * node + part + 1]) * 128 for part in range(3))
        for node in queues
    }
    for node, queue in queues.items():
        queue.init()

        def execute(raw, binds, node=node, queue=queue):
            status = layout.ok
            if fail[0] and node == 1 and raw == packages[node][1]:
                status = 0x14
                address = queue._cache[raw][0]
                changed = bytearray(raw)
                changed[65] ^= 0xFF
                memories[node].write_block(address, bytes(changed))
            return SimpleNamespace(status=status, sent=1, time={})

        firmware = SpecFirmware(
            memories[node],
            0,
            interp=SimpleNamespace(run=execute),
            layout=layout,
            record=layout.record,
        )
        firmware.boot()
        queue.idle = firmware.step
    prepared = PreparedQueueGroup(packages, queues)
    addresses = {
        node: [queues[node]._cache[raw][0] for raw in parts]
        for node, parts in packages.items()
    }
    for replay in (0, 1):
        fail[0] = bool(replay)
        launch(trace, queues)
        token = prepared.submit()
        trace.submitted(0)
        if replay:
            with pytest.raises(NodeError):
                token.wait()
        else:
            assert all(completion.ok for completion in token.wait().values())
    trace.save({})
    checks = json.loads((tmp_path / "package-check.json").read_text())["checks"]
    assert len(checks) == 1
    check = checks[0]
    assert (check["seq"], check["node"], check["part"], check["address"]) == (
        1,
        1,
        1,
        addresses[1][1],
    )
    assert check["first_differing_offsets"] == [65]
    rows = [
        json.loads(row)
        for row in (tmp_path / "launches.jsonl").read_text().splitlines()
    ]
    for row in rows:
        for node in queues:
            assert [
                p["address"] for p in row["packages"] if p["node"] == node
            ] == addresses[node]
    assert all(queue._quarantined and queue._package_pins for queue in queues.values())
