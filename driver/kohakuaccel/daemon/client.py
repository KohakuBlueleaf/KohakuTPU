"""The client half: an RPC connection, and a Transport riding it.

`DaemonTransport` is an ordinary :class:`kohakuaccel.transport.Transport`,
so Card/Device run over the daemon unchanged. It deliberately does NOT
expose `measure_read_skew` or `verify_write_path`: calibration belongs
to the daemon's own transport, done once when the daemon opened the
card, not once per client that connects to it.
"""

import socket
import threading

from kohakuaccel.daemon.protocol import recv_msg, send_msg
from kohakuaccel.daemon.server import DEFAULT_PORT
from kohakuaccel.transport.base import Transport, TransportUnavailable


class DaemonError(RuntimeError):
    """The daemon executed the op and reports it failed."""


class DaemonClient:
    """One connection. Thread-safe: calls serialize on a lock, which is
    honest -- they serialize again in the daemon's queue anyway."""

    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
                 timeout: float = 60.0) -> None:
        try:
            self.sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as exc:
            raise TransportUnavailable(
                f"no daemon at {host}:{port} ({exc}); start one with "
                f"`python -m kohakuaccel.daemon --board <name>`"
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
        return bytes.fromhex(self.client.call("read_block", addr=addr,
                                              nbytes=nbytes))

    def close(self) -> None:
        self.client.close()
