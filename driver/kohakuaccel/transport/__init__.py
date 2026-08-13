"""Transport backends.

The concrete card backends -- :mod:`kohakuaccel.transport.jtag`,
:mod:`kohakuaccel.transport.xdma` -- are deliberately NOT imported here. Each is
one host's answer to "where is the card", and a package that imports all of them
makes every caller depend on every one.
"""

from kohakuaccel.transport.base import (
    MASK32,
    MASK64,
    WORD_BYTES,
    Transport,
    TransportUnavailable,
    aligned,
    require_words,
    runs,
)
from kohakuaccel.transport.memory import MemoryTransport, RecordingTransport

__all__ = [
    "MASK32",
    "MASK64",
    "WORD_BYTES",
    "MemoryTransport",
    "RecordingTransport",
    "Transport",
    "TransportUnavailable",
    "aligned",
    "require_words",
    "runs",
]
