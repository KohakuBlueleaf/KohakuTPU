"""Host driver for KohakuTPU.

The driver is ordinary software. Its only hardware dependency is a 64-bit AXI
window -- ``write64`` and ``read64`` -- so the same code runs against a
simulator, a JTAG-AXI master, or XDMA by swapping the transport.

Software never sees MXFP7. Operands are uploaded as FP16 and quantised in
hardware on the way out of MAG; :mod:`ktpu.hw.mxfp7` exists only so tests can
predict what the hardware will produce.
"""

from ktpu.hw.device import (
    MAG_BASE,
    ORC_BASE,
    Program,
    RecordingTransport,
    Transport,
    node_index,
)

__all__ = [
    "MAG_BASE",
    "ORC_BASE",
    "Program",
    "RecordingTransport",
    "Transport",
    "node_index",
]
