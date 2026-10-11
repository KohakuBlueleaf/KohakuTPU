"""The client half: an RPC connection, and a Transport riding it.

`DaemonTransport` is an ordinary
:class:`kohakuaccel.driver.transport.Transport`, so a Card runs over the
daemon unchanged. It deliberately does NOT
expose `measure_read_skew` or `verify_write_path`: calibration belongs
to the daemon's own transport, done once when the daemon opened the
card, not once per client that connects to it.
"""

import dataclasses
import pathlib
import socket
import threading

from kohakuaccel.driver.daemon.protocol import recv_msg, send_msg
from kohakuaccel.driver.daemon.server import DEFAULT_PORT
from kohakuaccel.driver.node.boot import BootArgs
from kohakuaccel.driver.node.queue import Completion, NodeError
from kohakuaccel.driver.transport.base import Transport, TransportUnavailable


class DaemonError(RuntimeError):
    """The daemon executed the op and reports it failed."""


class DaemonClient:
    """One connection. Thread-safe: calls serialize on a lock, which is
    honest -- they serialize again in the daemon's queue anyway."""

    def __init__(
        self, host: str = "127.0.0.1", port: int = DEFAULT_PORT, timeout: float = 60.0
    ) -> None:
        try:
            self.sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as exc:
            raise TransportUnavailable(
                f"no daemon at {host}:{port} ({exc}); start the project's "
                f"daemon for this card first"
            ) from exc
        self._lock = threading.Lock()
        self._id = 0
        self.hello = self.call("hello")

    def call(self, op: str, **kw):
        with self._lock:
            self._id += 1
            msg = {"id": self._id, "op": op, **kw}
            send_msg(self.sock, msg)
            reply = recv_msg(self.sock)
        if reply is None:
            raise DaemonError("daemon closed the connection")
        if not reply.get("ok"):
            raise DaemonError(reply.get("error", "unknown daemon error"))
        return reply.get("value")

    # Convenience wrappers, exactly the daemon's op surface.
    def run_begin(self, meshes, level: str) -> int:
        return self.call("run_begin", meshes=list(meshes), level=level)

    def run_end(self, token: int) -> None:
        self.call("run_end", token=token)

    def claim(self, mesh: int, base: int, size: int) -> int:
        return self.call("claim", mesh=mesh, base=base, size=size)

    def release(self, lease: int) -> None:
        self.call("release", lease=lease)

    def clocks(self) -> dict:
        return {int(k): v for k, v in self.call("clocks").items()}

    def status(self) -> dict:
        return self.call("status")

    def shutdown(self) -> None:
        self.call("shutdown")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class DaemonTransport(Transport):
    """The card, through whoever owns it."""

    bulk = True

    def __init__(self, client: DaemonClient | None = None, **kw) -> None:
        self.client = client or DaemonClient(**kw)
        bb = self.client.hello.get("beat_bytes")
        if bb:
            self.beat_bytes = bb
        mb = self.client.hello.get("max_block")
        if mb:
            self.max_block = mb

    def run(self, cycles: int) -> None:
        """Advance a simulated card's clock (the daemon's backend must have one)."""
        self.client.call("sim_run", cycles=cycles)

    def status(self) -> dict:
        return self.client.call("sim_status")

    def write64(self, addr: int, data: int) -> None:
        self.client.call("write64", addr=addr, data=data)

    def read64(self, addr: int) -> int:
        return self.client.call("read64", addr=addr)

    def write32(self, addr: int, data: int) -> None:
        self.client.call("write32", addr=addr, data=data)

    def read32(self, addr: int) -> int:
        return self.client.call("read32", addr=addr)

    def write_block(self, addr: int, data: bytes) -> None:
        self.client.call("write_block", addr=addr, data=data.hex())

    def read_block(self, addr: int, nbytes: int) -> bytes:
        return bytes.fromhex(self.client.call("read_block", addr=addr, nbytes=nbytes))

    def close(self) -> None:
        self.client.close()


class RemoteNodeQueue:
    """A node's queue, held by the daemon:
    :class:`kohakuaccel.driver.node.queue.NodeQueue`'s surface, one round trip
    per call -- the daemon polls the completion ring.

        q = RemoteNodeQueue(client, node=0, base=staging_addr)   # attaches, inits
        q.load("build/fw/kohakutpu_node.elf", BootArgs(queue=q.base, mesh=0))
        q.wait_ready()
        q.run(package, bindings=[a, b])
    """

    def __init__(
        self, client: DaemonClient, node: int, base: int, init: bool = True, **geo
    ):
        self.client, self.node = client, node
        got = client.call("node_attach", node=node, base=base, init=init, **geo)
        self.base, self.heap_off, self.size = got["base"], got["heap_off"], got["size"]

    def _call(self, op: str, **kw):
        return self.client.call(op, node=self.node, **kw)

    def _final(self, d: dict, check: bool = True) -> Completion:
        out = d.pop("stdout", "")
        c = Completion(**d)
        if check and not c.ok:
            raise NodeError(c, out)
        return c

    # --------------------------------------------------------------- setup
    def load(self, elf, args: BootArgs, mode: str = "burn") -> dict:
        """Put firmware on the node and start it, daemon-side; the image travels."""
        raw = pathlib.Path(elf).read_bytes()
        return self._call(
            "node_load", elf=raw.hex(), args=dataclasses.asdict(args), mode=mode
        )

    def wait_ready(self, timeout: float = 120.0) -> dict:
        return self._call("node_ready", timeout=timeout)

    def state(self) -> dict:
        return self._call("node_state")

    def units(self) -> list:
        return [tuple(u) for u in self._call("node_units")]

    @property
    def counters(self) -> dict:
        return self._call("node_counters")

    # ------------------------------------------------------------- packages
    def submit(self, package: bytes | None = None, bindings=None, **kw) -> int:
        pkg = package.hex() if package is not None else None
        return self._call(
            "node_submit", package=pkg, bindings=list(bindings or []), **kw
        )

    def wait(self, tag: int, timeout: float = 300.0, check: bool = True) -> Completion:
        return self._final(self._call("node_wait", tag=tag, timeout=timeout), check)

    def run(
        self, package: bytes, bindings=None, timeout: float = 300.0, **kw
    ) -> Completion:
        """Submit and wait in ONE round trip."""
        d = self._call(
            "node_run",
            package=package.hex(),
            bindings=list(bindings or []),
            timeout=timeout,
            **kw,
        )
        return self._final(d)

    def nop(self) -> Completion:
        return self._final(self._call("node_nop"))

    def stop(self, value: int = 0) -> Completion:
        return self._final(self._call("node_stop", value=value))

    # ----------------------------------------------------------- node heaps
    def heap(
        self, region: int, base: int, nbytes: int, granule: int = 64
    ) -> Completion:
        return self._final(
            self._call(
                "node_heap", region=region, base=base, nbytes=nbytes, granule=granule
            )
        )

    def alloc(self, region: int, nbytes: int, align: int = 0, tag: int = 0) -> int:
        d = self._call("node_alloc", region=region, nbytes=nbytes, align=align, tag=tag)
        return self._final(d).value

    def free(self, region: int, addr: int) -> Completion:
        return self._final(self._call("node_free", region=region, addr=addr))

    def heap_stats(self, region: int) -> dict:
        return self._call("node_heap_stats", region=region)

    # ---------------------------------------------------------------- stdio
    def read_stdout(self) -> str:
        return self._call("node_stdout")
