"""Queue-visibility counterexamples through real XDMA shaping and SpecFirmware.

No card or tensor arithmetic. Injected timing/faults are labelled, not diagnoses.
"""

import argparse
import json
import time
from pathlib import Path
from types import SimpleNamespace

from kohakuaccel.driver.node.queue import NodeError, NodeQueue
from kohakuaccel.driver.node.queue import layout as L
from kohakuaccel.driver.node.queue.double import SpecFirmware
from kohakuaccel.driver.transport.memory import MemoryTransport
from kohakuaccel.driver.transport.xdma_linux import XdmaLinux
from kohakutpu.application.runtime.card import firmware_wait_seconds

BASE = 1 << 40


class Engine(XdmaLinux):
    """Only the device-file endpoints are replaced; shaping/publication are real."""

    def __init__(self):
        self.memory = MemoryTransport()
        self.before_read = None
        super().__init__(max_xfer=4096)

    def _open(self):
        self._fds = []

    def _h2c_once(self, at, n):
        self.memory.write_block(at, bytes(self._staging[:n]))

    def _c2h_once(self, at, n):
        if self.before_read:
            self.before_read(at)
        self._staging[:n] = self.memory.read_block(at, n)


def setup(head):
    engine, now = Engine(), [0.0]
    q = NodeQueue(engine, BASE, sq_n=64, cq_n=64, clock=lambda: now[0])
    q.cq_bulk, q.head_cache, q.batch_size, q.backoff = False, True, 1, 0
    q.init()
    fw = SpecFirmware(engine.memory, BASE)
    fw.boot()
    q.sq_tail = q.cq_head = q._sq_head_seen = q._tag = head
    for offset in (L.SQ_TAIL, L.SQ_HEAD, L.CQ_TAIL, L.CQ_HEAD):
        engine.memory.write64(BASE + offset, head)
    engine.memory.write64(BASE + L.FW + 16, head)
    engine.memory.write64(BASE + q.geo["cq"] + head % 64 * L.CQ_BYTES, head + 1 - 64)
    return engine, q, fw, now


def step(engine, fw):
    fw.step()
    engine.memory.write64(BASE + L.FW + 16, engine.memory.read64(BASE + L.SQ_HEAD))


def final_diagnostic_race():
    engine, q, fw, now = setup(3887)
    tag = q.submit(None, op=L.OP_NOP)
    polls = []

    def read(at):
        if at == BASE + L.CQ_TAIL:
            polls.append({"seconds": now[0], "tail": fw.rd(L.CQ_TAIL)})
        if at == BASE + L.FW and now[0] > 300:
            engine.before_read = None
            step(engine, fw)

    engine.before_read = read
    q.idle = lambda: now.__setitem__(0, 300.001)
    try:
        q.wait(tag, timeout=300)
    except TimeoutError as exc:
        error = str(exc)
    else:
        raise AssertionError("injected completion must follow final poll")
    observed = q.pointers()
    assert observed["cq_tail"] == 3888 and observed["cq_head"] == 3887
    assert q.wait(tag).ok
    return {
        "injection": "completion between final empty poll and diagnostic state read",
        "polls": polls,
        "error": error,
        "next_wait_consumed": True,
        "rmw": engine.counts["rmw"],
    }


def running_entry():
    engine, q, fw, now = setup(17411)
    tag = q.submit(b"x" * 32)
    observed = {}

    def run(_package, _bindings):
        observed["inside_interpreter"] = True
        observed["state"] = q.state()
        observed["pointers"] = q.pointers()
        q.idle = lambda: now.__setitem__(0, 300.001)
        try:
            q.wait(tag, timeout=300)
        except TimeoutError as exc:
            observed["error"] = str(exc)
        return SimpleNamespace(status=0, sent=0)

    fw.interp = SimpleNamespace(run=run)
    step(engine, fw)
    assert observed["pointers"] == {
        "sq_head": 17411,
        "sq_tail": 17412,
        "cq_tail": 17411,
        "cq_head": 17411,
        "cq_head_tag": 17348,
        "sq_tail_card": 17412,
        "cq_head_card": 17411,
    }
    assert q.wait(tag).ok
    observed["after_execution_consumed"] = True
    observed["injection"] = (
        "host clock expires while RUN interpreter has entered but not returned"
    )
    observed["rmw"] = engine.counts["rmw"]
    return observed


def timeout_budget(requested):
    engine, q, fw, now = setup(17411)
    wait = firmware_wait_seconds(300.0, requested)
    # Firmware wait_until/de_watch followed by ka_engine_quiesce, plus 200 ms
    # of setup/reporting. Timings are model inputs, not measured card phases.
    completion_at = 2 * wait + 0.2
    fw.interp = SimpleNamespace(run=lambda *args: SimpleNamespace(status=0x30, sent=0))
    ticks = [0]

    def idle():
        ticks[0] += 1
        now[0] = ticks[0] / 10
        if now[0] >= completion_at:
            step(engine, fw)

    q.idle = idle
    tag = q.submit(b"x" * 32)
    try:
        q.wait(tag, timeout=300)
    except (NodeError, TimeoutError) as exc:
        return {
            "firmware_wait_seconds": wait,
            "quiesce_seconds": wait,
            "completion_due": completion_at,
            "host_seconds": now[0],
            "outcome": type(exc).__name__,
            "pointers": q.pointers(),
        }
    raise AssertionError("the injected failed RUN must report an error")


def ring_walk(count):
    engine, q, fw, _ = setup(63)
    q.idle = lambda: step(engine, fw)
    started = time.monotonic()
    for _ in range(count):
        tag = q.submit(None, op=L.OP_NOP)
        assert q.wait(tag).tag == tag
    return {
        "count": count,
        "last_tag": tag,
        "seconds": time.monotonic() - started,
        "pointers": q.pointers(),
        "xdma": engine.counts,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=17500)
    parser.add_argument(
        "--output", type=Path, default=Path("../build/seam/visibility.json")
    )
    args = parser.parse_args()
    result = {
        "mx8_compatible": final_diagnostic_race(),
        "mx9_compatible": running_entry(),
        "fault_free_walk": ring_walk(args.count),
        "legacy_budget": timeout_budget(150.0),
        "default_budget": timeout_budget(None),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
