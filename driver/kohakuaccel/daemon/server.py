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
"""

import asyncio
import concurrent.futures
import threading

from kohakuaccel.daemon.governor import ClockGovernor
from kohakuaccel.daemon.protocol import (
    ProtocolError,
    async_recv_msg,
    async_send_msg,
)
from kohakuaccel.transport.base import MASK32, MASK64

DEFAULT_PORT = 47155
_TICK_SECONDS = 1.0


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
    ) -> None:
        self.transport = transport
        self.board = board or {}
        self.governor = (
            ClockGovernor(clock_ctl, idle_seconds) if clock_ctl else None
        )
        self.allow_program = allow_program
        self.host, self.port = host, port
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
                    value = await self._hw_call(self._dispatch, conn, msg)
                    reply = {"id": msg.get("id"), "ok": True, "value": value}
                except Exception as exc:  # noqa: BLE001 -- op errors reply
                    reply = {"id": msg.get("id"), "ok": False,
                             "error": f"{type(exc).__name__}: {exc}"}
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
            "governor": self.governor is not None,
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

    # ------------------------------------------------------------- cleanup
    def _drop_conn(self, conn: _Conn) -> None:
        """A dead client releases everything it held; the card is never
        left boosted or leased by a process that no longer exists."""
        for lease in list(conn.leases):
            self._leases.pop(lease, None)
        if self.governor:
            self.governor.drop_runs_of(conn.tokens)
        self._conns.pop(conn.cid, None)
