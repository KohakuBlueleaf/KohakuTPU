"""Queued tinygrad contracts observed on the Verilated card, never an oracle model."""

import numpy as np
from harness.registry import mutant, patched, runner
from tinygrad import Tensor

from kohakuaccel.driver.node.queue import NodeError
from kohakutpu.application.ktpugrad.device import KTPUDevice
from kohakutpu.application.runtime import device as D
from kohakutpu.stack import kernel as K


@runner("queue.card")
def queued_card(inp, ctx):
    dev = ctx["session"].dev
    old_mode, old_limit, old_bytes = dev.queued, dev.max_inflight, dev.queue_bytes
    dev.queued = True
    try:
        with D.attached(dev):
            raw = np.arange(32, dtype=np.float16)
            x = Tensor(raw, device="KTPU").realize()
            y = (x * 2).contiguous().realize()
            pending = list(dev.inflight)
            lazy = bool(pending)
            if inp["case"] == "depth":
                depth = min(dev.card.q.sq_n, dev.card.q.cq_n)
                for _ in range(depth):
                    y = (y + 1).contiguous().realize()
                bounded = len(dev.inflight) <= depth and pending[0].run is not None
                got = y.numpy()
                return {
                    "bounded": bounded,
                    "values": bool(np.array_equal(got, raw * 2 + depth)),
                }
            if inp["case"] == "replay":
                dev.drain()
                dev.record()
                z = (y + 1).contiguous().realize()
                log = dev.recorded()
                dev.drain()
                runs = dev.replay(log, wait=False)
                lazy = bool(dev.inflight) and all(s.run is None for s in runs)
                log.close()
                got = z.numpy()
                return {"lazy": lazy, "values": bool(np.array_equal(got, raw * 2 + 1))}
            if inp["case"] == "lazy":
                z = (y + 1).contiguous().realize()
                ordered = len(dev.inflight) >= 2
                got = z.numpy()
                return {
                    "lazy": lazy,
                    "ordered": ordered,
                    "values": bool(np.array_equal(got, raw * 2 + 1)),
                    "collected": not dev.inflight,
                }
            if inp["case"] == "free":
                storage = x.uop.buffer._buf
                address = storage.addr
                x.uop.buffer.deallocate()
                held = dev.mmu.entries[address].live
                got = y.numpy()
                return {
                    "held": held,
                    "released": not dev.mmu.entries[address].live,
                    "values": bool(np.array_equal(got, raw * 2)),
                }
            if inp["case"] == "bound":
                # Lower only the host budget, not the live firmware ring geometry.
                dev.queue_bytes = sum(a.live[at] for a, at in pending[0].references)
                z = (y + 1).contiguous().realize()
                bounded = pending[0].run is not None
                got = z.numpy()
                return {
                    "bounded": bounded,
                    "values": bool(np.array_equal(got, raw * 2 + 1)),
                }
            if inp["case"] == "write":
                x.uop.buffer.allocator._copyin(
                    x.uop.buffer._buf, memoryview((raw + 5).tobytes())
                )
                waited = not dev.inflight
                got = y.numpy()
                return {"waited": waited, "values": bool(np.array_equal(got, raw * 2))}
            KTPUDevice.synchronize(type("DeviceView", (), {"rt": dev})())
            return {"lazy": lazy, "collected": not dev.inflight}
    finally:
        dev.drain()
        dev.queued, dev.max_inflight, dev.queue_bytes = old_mode, old_limit, old_bytes


@runner("queue.fault")
def fault_card(inp, ctx):
    dev = ctx["session"].dev
    with D.attached(dev), patched(dev, "queued", True):
        x = Tensor(np.arange(32, dtype=np.float16), device="KTPU").realize()
        y = (x * 2).contiguous().realize()
        template = dev.inflight[-1]
        dev.drain()
        node = dev.card.nodes[0]
        compiled = template.info["compiled"]
        binds = {"in0": x.uop.buffer._buf.addr, "out": y.uop.buffer._buf.addr}

        # A valid wire package that fails during execution, not a malformed
        # header rejected by mandatory pre-publication admission.
        fault = K.compile(
            "target ktpu.v9\nfn queue_fault.l1()\n    r0 = set 1\n    assert_zero r0 code=8\n",
            machine=dev.machine,
            level="l1",
        )

        try:
            with dev.submission_scope("broken-plan"):
                first = dev.submit(fault, {})
            with dev.submission_scope("later-plan"):
                later = dev.submit(compiled, binds)
            exact = discarded = False
            try:
                dev.collect(first)
            except NodeError as error:
                exact = (
                    "broken-plan" in str(error)
                    and fault.name in str(error)
                    and error.completion.tag == first.pending.tags[node][0]
                    and (
                        error.completion.status,
                        error.completion.detail,
                        error.completion.value,
                    )
                    == (0x24, 8, 1)
                )
            try:
                dev.collect(later)
            except RuntimeError as error:
                discarded = "later-plan" in str(error) and "discarded" in str(error)
            return {"exact": exact, "discarded": discarded}
        finally:
            # Restart halts every node before this test releases quarantined pins.
            dev.card.restart()
            for s in dev.inflight:
                for arena, at in s.references:
                    arena.unretain(at)
            dev.inflight.clear()
            dev._failure = None


@mutant("queue/no-fault-attribution")
def no_fault_attribution():
    with patched(D.Device, "collect", lambda self, s: self.card.collect(s.pending)):
        yield


@mutant("queue/replay-waits")
def replay_waits():
    original = D.Device._replay_queued

    def waiting(self, log):
        out = original(self, log)
        self.drain()
        return out

    with patched(D.Device, "_replay_queued", waiting):
        yield


@mutant("queue/no-host-wait")
def no_host_wait():
    with patched(D.Device, "wait_address", lambda *a, **kw: None):
        yield


@mutant("queue/no-admission")
def no_admission():
    with patched(D.Device, "_admit", lambda *a, **kw: None):
        yield


@mutant("queue/no-pins")
def no_pins():
    with patched(D.Device, "_retain", lambda *a, **kw: []):
        yield


@mutant("queue/no-synchronize")
def no_synchronize():
    with patched(KTPUDevice, "synchronize", lambda *a: None):
        yield
