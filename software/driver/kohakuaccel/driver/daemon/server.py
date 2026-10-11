"""The daemon process: one transport owner, one hardware thread, one loop.

An asyncio event loop serves every client concurrently; every hardware
operation runs on a ONE-thread executor. A single-thread executor is a
FIFO queue with a worker attached, so "one hardware op at a time" is the
executor's construction, not a lock anyone has to remember -- and the
loop stays free to accept, read and reply while an op is on the wire.
The transport (a held Tcl session, later an XDMA handle) is touched by
exactly one thread for its lifetime.

A client IS its connection: when the stream closes, its leases and run
tokens are released on the hardware thread like any other op. That
liveness-by-connection is the reason the daemon speaks its own framed
protocol on a plain socket rather than request-scoped HTTP.

The daemon also holds each node's queue (`node_*` ops, node-queue.md §9).
"""

import asyncio
import concurrent.futures
import dataclasses
import pathlib
import tempfile
import threading
import time

from kohakuaccel.driver.daemon.governor import ClockGovernor
from kohakuaccel.driver.daemon.protocol import (
    ProtocolError,
    async_recv_msg,
    async_send_msg,
)
from kohakuaccel.driver.node.boot import BootArgs
from kohakuaccel.driver.node.queue import NodeQueue
from kohakuaccel.driver.node.queue import layout as L
from kohakuaccel.driver.transport.base import MASK32, MASK64

DEFAULT_PORT = 47155
_TICK_SECONDS = 1.0

#: Ops that poll a node until it answers: served on the loop, polling in steps.
_POLLING = {
    "node_ready",
    "node_wait",
    "node_run",
    "node_nop",
    "node_stop",
    "node_heap",
    "node_alloc",
    "node_free",
}


class _Conn:
    def __init__(self, cid: int) -> None:
        self.cid = cid
        self.leases: set[int] = set()
        self.tokens: set[int] = set()


class Daemon:
    """See the package docstring for why this process exists.

    `clock_ctl` is the injected wizard adapter (`nmesh`, `levels`,
    `idle_level`, `apply(mesh, level)`, `read(mesh)`); None runs the
    daemon without clock policy. `allow_program` gates reprogramming --
    a client must not be able to reload the fabric by accident.

    `start()`/`stop()` run the loop on a background thread so callers
    (tests, the CLI) stay synchronous; `run()` is the loop itself for a
    caller that already lives in asyncio.

    `node_mem`, `node_loader`, `poll_idle` and `poll_seconds` serve the node
    queues (node-queue.md §9).
    """

    def __init__(
        self,
        transport,
        board: dict | None = None,
        clock_ctl=None,
        idle_seconds: float = 10.0,
        allow_program: bool = False,
        host: str = "127.0.0.1",
        port: int = DEFAULT_PORT,
        node_mem=None,
        node_loader=None,
        poll_idle=None,
        poll_seconds: float = 0.002,
    ) -> None:
        self.transport = transport
        self.board = board or {}
        self.governor = ClockGovernor(clock_ctl, idle_seconds) if clock_ctl else None
        self.allow_program = allow_program
        self.host, self.port = host, port
        self.node_mem = node_mem
        self.node_loader = node_loader
        self.poll_idle = poll_idle
        self.poll_seconds = poll_seconds
        self._nodes: dict[int, NodeQueue] = {}
        self._conns: dict[int, _Conn] = {}
        self._leases: dict[int, tuple[int, int, int, int]] = {}
        self._next = {"conn": 1, "lease": 1}
        self._hw: concurrent.futures.ThreadPoolExecutor | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopping: asyncio.Event | None = None
        self._ready = threading.Event()
        self._done = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ lifecycle
    def start(self) -> int:
        """Run the loop on a background thread; returns the bound port
        (`port=0` asks the OS, which is what tests want)."""
        self._thread = threading.Thread(
            target=lambda: asyncio.run(self.run()), daemon=True
        )
        self._thread.start()
        self._ready.wait(timeout=10)
        return self.port

    def stop(self) -> None:
        if self._loop is not None and self._stopping is not None:
            try:
                self._loop.call_soon_threadsafe(self._stopping.set)
            except RuntimeError:
                pass  # loop already gone
        if self._thread is not None:
            self._thread.join(timeout=10)
        close = getattr(self.transport, "close", None)
        if close is not None:
            close()

    def serve_forever(self) -> None:
        self._done.wait()

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stopping = asyncio.Event()
        # max_workers=1 IS the op queue: FIFO, strictly serial.
        self._hw = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="hw"
        )
        server = await asyncio.start_server(self._handle, self.host, self.port)
        self.port = server.sockets[0].getsockname()[1]
        if self.governor:
            await self._hw_call(self.governor.to_idle, True)
        tick = asyncio.ensure_future(self._tick_task())
        self._ready.set()
        try:
            await self._stopping.wait()
        finally:
            tick.cancel()
            server.close()
            await server.wait_closed()
            self._hw.shutdown(wait=True)
            self._done.set()

    async def _hw_call(self, fn, *args):
        return await self._loop.run_in_executor(self._hw, fn, *args)

    async def _tick_task(self) -> None:
        # One tick in flight at most: awaiting the executor result means a
        # stalled hardware op cannot pile up stale retunes behind itself.
        while True:
            await asyncio.sleep(_TICK_SECONDS)
            if self.governor:
                await self._hw_call(self.governor.tick)

    # ---------------------------------------------------------- connections
    async def _handle(self, reader, writer) -> None:
        conn = _Conn(self._next["conn"])
        self._next["conn"] += 1
        self._conns[conn.cid] = conn
        try:
            while True:
                try:
                    msg = await async_recv_msg(reader)
                except ProtocolError:
                    break  # no way back to a frame boundary
                if msg is None:
                    break
                try:
                    if msg.get("op") in _POLLING:
                        value = await self._polling(msg)
                    else:
                        value = await self._hw_call(self._dispatch, conn, msg)
                    reply = {"id": msg.get("id"), "ok": True, "value": value}
                except Exception as exc:  # noqa: BLE001 -- op errors reply
                    reply = {
                        "id": msg.get("id"),
                        "ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                try:
                    await async_send_msg(writer, reply)
                except (ConnectionError, OSError):
                    break
        finally:
            await self._hw_call(self._drop_conn, conn)
            writer.close()

    # ----------------------------------------------------------------- ops
    # Everything below executes on the hardware thread, one call at a time.
    def _dispatch(self, conn: _Conn, msg: dict):
        op = msg.get("op")
        fn = getattr(self, f"_op_{op}", None)
        if fn is None:
            raise ValueError(f"unknown op {op!r}")
        return fn(conn, msg)

    def _op_hello(self, conn, msg):
        return {
            "board": self.board.get("name"),
            "meshes": self.board.get("meshes"),
            "backend": type(self.transport).__name__,
            "beat_bytes": getattr(self.transport, "beat_bytes", None),
            "max_block": getattr(self.transport, "max_block", None),
            "governor": self.governor is not None,
            "nodes": self.node_mem is not None,
            "loader": self.node_loader is not None,
        }

    def _op_read64(self, conn, msg):
        return self.transport.read64(msg["addr"]) & MASK64

    def _op_write64(self, conn, msg):
        self.transport.write64(msg["addr"], msg["data"] & MASK64)

    def _op_read32(self, conn, msg):
        native = getattr(self.transport, "read32", None)
        if native is not None:
            return native(msg["addr"]) & MASK32
        word = self.transport.read64(msg["addr"] & ~7)
        return (word >> (32 if msg["addr"] & 4 else 0)) & MASK32

    def _op_write32(self, conn, msg):
        addr, data = msg["addr"], msg["data"] & MASK32
        native = getattr(self.transport, "write32", None)
        if native is not None:
            native(addr, data)
            return
        base = addr & ~7
        word = self.transport.read64(base)
        if addr & 4:
            word = (word & MASK32) | (data << 32)
        else:
            word = (word & (MASK32 << 32)) | data
        self.transport.write64(base, word)

    def _op_read_block(self, conn, msg):
        return self.transport.read_block(msg["addr"], msg["nbytes"]).hex()

    def _op_write_block(self, conn, msg):
        self.transport.write_block(msg["addr"], bytes.fromhex(msg["data"]))

    def _op_clocks(self, conn, msg):
        if self.governor is None:
            raise RuntimeError("daemon runs without clock control")
        ctl = self.governor.ctl
        return {m: ctl.read(m) for m in range(ctl.nmesh)}

    def _op_run_begin(self, conn, msg):
        if self.governor is None:
            raise RuntimeError("daemon runs without clock control")
        token = self.governor.run_begin(msg["meshes"], msg["level"])
        conn.tokens.add(token)
        return token

    def _op_run_end(self, conn, msg):
        self.governor.run_end(msg["token"])
        conn.tokens.discard(msg["token"])

    def _op_claim(self, conn, msg):
        mesh, lo = msg["mesh"], msg["base"]
        hi = lo + msg["size"]
        for cid, m, b, e in self._leases.values():
            if m == mesh and lo < e and b < hi:
                raise ValueError(
                    f"mesh {mesh} already has a live arena [{b:#x}, {e:#x}) "
                    f"held by client {cid}, overlapping [{lo:#x}, {hi:#x})"
                )
        lease = self._next["lease"]
        self._next["lease"] += 1
        self._leases[lease] = (conn.cid, mesh, lo, hi)
        conn.leases.add(lease)
        return lease

    def _op_release(self, conn, msg):
        self._leases.pop(msg["lease"], None)
        conn.leases.discard(msg["lease"])

    def _op_status(self, conn, msg):
        return {
            "clients": len(self._conns),
            "leases": {
                k: {"client": c, "mesh": m, "base": b, "end": e}
                for k, (c, m, b, e) in self._leases.items()
            },
            "governor": self.governor.status() if self.governor else None,
        }

    def _op_program(self, conn, msg):
        if not self.allow_program:
            raise PermissionError("daemon started without --allow-program")
        self.transport.program(msg["bitstream"], msg.get("probes"))
        # BUILT full speed the instant the fabric loads; idle NOW.
        if self.governor:
            self.governor.to_idle(force=True)

    def _op_reset_core(self, conn, msg):
        self.transport.reset_core()

    def _op_shutdown(self, conn, msg):
        self._loop.call_soon_threadsafe(self._stopping.set)

    # ------------------------------------------------------- simulated card
    def _op_sim_run(self, conn, msg):
        run = getattr(self.transport, "run", None)
        if run is None:
            raise RuntimeError(
                f"{type(self.transport).__name__} has no clock to advance"
            )
        run(msg["cycles"])

    def _op_sim_status(self, conn, msg):
        status = getattr(self.transport, "status", None)
        if status is None:
            raise RuntimeError(f"{type(self.transport).__name__} reports no status")
        return status()

    # ---------------------------------------------------------- node queues
    def _node(self, msg) -> NodeQueue:
        q = self._nodes.get(msg["node"])
        if q is None:
            raise KeyError(f"node {msg['node']} has no attached queue (node_attach)")
        return q

    def _op_node_attach(self, conn, msg):
        if self.node_mem is None:
            raise RuntimeError("daemon runs without node memory (node_mem)")
        geo = {
            k: msg[k]
            for k in ("size", "sq_n", "cq_n", "so_bytes", "si_bytes")
            if k in msg
        }
        q = NodeQueue(self.node_mem, msg["base"], **geo)
        if msg.get("init", True):
            q.init()
        self._nodes[msg["node"]] = q
        return {"base": q.base, "heap_off": q.heap_off, "size": q.size}

    def _op_node_load(self, conn, msg):
        if self.node_loader is None:
            raise RuntimeError("daemon runs without a node loader")
        with tempfile.TemporaryDirectory() as tmp:
            elf = pathlib.Path(tmp) / "image.elf"
            elf.write_bytes(bytes.fromhex(msg["elf"]))
            info = self.node_loader(
                msg["node"], elf, BootArgs(**msg["args"]), msg.get("mode", "burn")
            )
        return {k: v for k, v in info.items() if isinstance(v, (int, float, str))}

    def _op_node_submit(self, conn, msg):
        return self._submit(self._node(msg), msg)

    @staticmethod
    def _submit(q: NodeQueue, msg) -> int:
        pkg = msg.get("package")
        return q.submit(
            bytes.fromhex(pkg) if pkg is not None else None,
            msg.get("bindings"),
            op=msg.get("qop", L.OP_RUN),
            timeout_cycles=msg.get("timeout_cycles", 0),
            flags=msg.get("flags", 0),
            arg=msg.get("arg", 0),
            words=msg.get("words"),
        )

    def _op_node_stdout(self, conn, msg):
        return self._node(msg).read_stdout()

    def _op_node_state(self, conn, msg):
        return self._node(msg).state()

    def _op_node_units(self, conn, msg):
        return self._node(msg).units()

    def _op_node_counters(self, conn, msg):
        return dict(self._node(msg).counters)

    def _op_node_heap_stats(self, conn, msg):
        return self._node(msg).heap_stats(msg["region"])

    async def _polling(self, msg):
        """A node op that waits: issue on the hardware thread, then poll in
        steps there, giving the queue to other clients between steps."""
        op, timeout = msg["op"], msg.get("timeout", 300.0)
        q = await self._hw_call(self._node, msg)
        if op == "node_ready":
            return await self._until(lambda: self._ready_step(q), timeout)
        if op == "node_wait":
            tag = msg["tag"]
        else:
            sub = dict(msg)
            if op == "node_nop":
                sub["qop"] = L.OP_NOP
            elif op == "node_stop":
                sub.update(qop=L.OP_STOP, arg=msg.get("value", 0))
            elif op == "node_heap":
                g = msg.get("granule", 64)
                sub.update(
                    qop=L.OP_HEAP, words=[msg["region"], msg["base"], msg["nbytes"], g]
                )
            elif op == "node_alloc":
                w = [
                    msg["region"],
                    msg["nbytes"],
                    msg.get("align", 0),
                    msg.get("tag", 0),
                ]
                sub.update(qop=L.OP_ALLOC, words=w)
            elif op == "node_free":
                sub.update(qop=L.OP_FREE, words=[msg["region"], msg["addr"]])
            tag = await self._hw_call(self._submit, q, sub)
        c = await self._until(lambda: q.take(tag), timeout)
        out = dataclasses.asdict(c)
        if not c.ok:
            out["stdout"] = await self._hw_call(q.read_stdout)
        return out

    @staticmethod
    def _ready_step(q: NodeQueue):
        st = q.state()
        if st["state"] == "fatal":
            raise RuntimeError(f"firmware stopped fatally: {st}\n{q.read_stdout()}")
        return st if st["state"] == "ready" else None

    async def _until(self, step, timeout: float):
        """`step()` on the hardware thread until it returns non-None."""
        t0 = time.monotonic()
        while True:
            got = await self._hw_call(step)
            if got is not None:
                return got
            if time.monotonic() - t0 > timeout:
                raise TimeoutError(f"no answer from the node in {timeout}s")
            if self.poll_idle is not None:
                await self._hw_call(self.poll_idle)
            else:
                await asyncio.sleep(self.poll_seconds)

    # ------------------------------------------------------------- cleanup
    def _drop_conn(self, conn: _Conn) -> None:
        """A dead client releases everything it held; the card is never
        left boosted or leased by a process that no longer exists."""
        for lease in list(conn.leases):
            self._leases.pop(lease, None)
        if self.governor:
            self.governor.drop_runs_of(conn.tokens)
        self._conns.pop(conn.cid, None)
