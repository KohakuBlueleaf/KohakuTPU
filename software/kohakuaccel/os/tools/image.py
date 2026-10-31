"""What a loader writes for a KohakuAccel OS image on one node.

An image's loadable segments with a machine address (below 2**40) go where
they say; segments at UIMG (2**40) and above are the per-node user image,
written at the node's private DRAM base plus their offset from UIMG.
"""

import pathlib
import re
import struct

UIMG = 1 << 40
ABI = pathlib.Path(__file__).resolve().parents[1] / "include/kos/abi.h"


def abi_const(name: str) -> int:
    """A numeric #define of include/kos/abi.h, so the host never restates one."""
    m = re.search(
        rf"^#define {name}\s+(0x[0-9A-Fa-f]+|\d+)", ABI.read_text(), re.MULTILINE
    )
    if not m:
        raise KeyError(f"{name} is not a numeric #define in {ABI}")
    return int(m[1], 0)


def segments(elf: pathlib.Path) -> list[tuple[int, bytes]]:
    """(physical load address, bytes) of every PT_LOAD segment with file bytes."""
    b = elf.read_bytes()
    if b[:4] != b"\x7fELF" or b[4] != 2:
        raise ValueError(f"{elf}: not a 64-bit ELF")
    phoff = struct.unpack_from("<Q", b, 32)[0]
    phes, phnum = struct.unpack_from("<HH", b, 54)
    out = []
    for i in range(phnum):
        p = phoff + i * phes
        ptype, _flags, off, _va, pa, fsz = struct.unpack_from("<IIQQQQ", b, p)
        if ptype == 1 and fsz:
            out.append((pa, b[off : off + fsz]))
    return out


def private_part(elf: pathlib.Path) -> list[tuple[int, bytes]]:
    """(offset in the private span, bytes): the per-node user image."""
    return [(pa - UIMG, data) for pa, data in segments(elf) if pa >= UIMG]
