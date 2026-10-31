"""Wire format: 4-byte big-endian length, then one UTF-8 JSON object.

JSON because every message is small control traffic; bulk data rides as
hex strings, which is already generous for a JTAG-paced card (hundreds
of KB/s). The framing exists so a reader never has to guess where a
message ends on a stream socket.
"""

import asyncio
import json
import socket
import struct

#: Refuse absurd frames instead of allocating them: the largest honest
#: message is a block payload, and 64 MiB of hex is far past any burst
#: this driver issues.
MAX_FRAME = 64 << 20

_LEN = struct.Struct(">I")


class ProtocolError(RuntimeError):
    """The peer sent something that is not a frame. The stream is dead:
    after a framing error there is no way back to a message boundary."""


def send_msg(sock: socket.socket, obj: dict) -> None:
    body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_FRAME:
        raise ProtocolError(f"frame of {len(body)} bytes exceeds {MAX_FRAME}")
    sock.sendall(_LEN.pack(len(body)) + body)


def recv_msg(sock: socket.socket) -> dict | None:
    """One message, or None on a clean EOF at a frame boundary."""
    head = _recv_exact(sock, _LEN.size)
    if head is None:
        return None
    (n,) = _LEN.unpack(head)
    if n > MAX_FRAME:
        raise ProtocolError(f"frame of {n} bytes exceeds {MAX_FRAME}")
    body = _recv_exact(sock, n)
    if body is None:
        raise ProtocolError("EOF inside a frame")
    try:
        return json.loads(body.decode("utf-8"))
    except ValueError as exc:
        raise ProtocolError(f"frame is not JSON: {exc}") from exc


def _recv_exact(sock: socket.socket, n: int) -> bytes | None:
    """`n` bytes, None on EOF BEFORE the first byte, error on EOF inside."""
    out = b""
    while len(out) < n:
        chunk = sock.recv(n - len(out))
        if not chunk:
            if not out:
                return None
            raise ProtocolError(f"EOF after {len(out)} of {n} bytes")
        out += chunk
    return out


async def async_send_msg(writer, obj: dict) -> None:
    """The same frame over an asyncio stream; the server side of the pair."""
    body = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_FRAME:
        raise ProtocolError(f"frame of {len(body)} bytes exceeds {MAX_FRAME}")
    writer.write(_LEN.pack(len(body)) + body)
    await writer.drain()


async def async_recv_msg(reader) -> dict | None:
    """One message, or None on a clean EOF at a frame boundary."""
    try:
        head = await reader.readexactly(_LEN.size)
    except asyncio.IncompleteReadError as exc:
        if not exc.partial:
            return None
        raise ProtocolError(f"EOF inside the length prefix: {exc}") from exc
    except (ConnectionError, OSError):
        return None
    (n,) = _LEN.unpack(head)
    if n > MAX_FRAME:
        raise ProtocolError(f"frame of {n} bytes exceeds {MAX_FRAME}")
    try:
        body = await reader.readexactly(n)
    except (asyncio.IncompleteReadError, ConnectionError, OSError) as exc:
        raise ProtocolError(f"EOF inside a frame: {exc}") from exc
    try:
        return json.loads(body.decode("utf-8"))
    except ValueError as exc:
        raise ProtocolError(f"frame is not JSON: {exc}") from exc
