"""What a firmware image says it speaks: its `.ka_abi` words (the dispatcher's
apps/dispatcher/main.c; a non-loaded section, node.ld), read from the ELF file.

``boot`` is the boot block version the image reads, ``debug`` its debug
feature bits (`DEBUG_*`, ka/package/debug.h), ``last`` the completion words
it keeps per package unit and ``queue`` the queue region version it serves
(3 for an image whose words stop before it)."""

import pathlib
import struct

from kohakuaccel.driver.node.boot.args import MAGIC

#: Debug features (KA_DEBUG_F_*).
DEBUG_STEPS, DEBUG_FAIL, DEBUG_LAST = 1, 2, 4
DEBUG_NAMES = {DEBUG_STEPS: "steps", DEBUG_FAIL: "fail", DEBUG_LAST: "last"}


class AbiMismatch(RuntimeError):
    """The image does not speak what the host writes."""


def read(elf: pathlib.Path | str) -> dict | None:
    """The image's ABI words, or None for an image without them."""
    b = pathlib.Path(elf).read_bytes()
    if b[:4] != b"\x7fELF" or b[4] != 2:
        raise ValueError(f"{elf}: not a 64-bit ELF")
    shoff = struct.unpack_from("<Q", b, 40)[0]
    shentsize, shnum, shstrndx = struct.unpack_from("<HHH", b, 58)

    def header(i: int) -> tuple:
        return struct.unpack_from("<IIQQQQ", b, shoff + i * shentsize)

    strtab = header(shstrndx)[4]
    for i in range(shnum):
        name, _, _, _, off, size = header(i)
        end = b.index(b"\0", strtab + name)
        if b[strtab + name : end] == b".ka_abi" and size >= 32:
            magic, boot, debug, last = struct.unpack_from("<4Q", b, off)
            if magic != MAGIC:
                raise AbiMismatch(f"{elf}: .ka_abi magic {magic:#x}")
            queue = struct.unpack_from("<Q", b, off + 32)[0] if size >= 40 else 3
            return {
                "package_features": (
                    struct.unpack_from("<Q", b, off + 40)[0] if size >= 48 else 0
                ),
                "queue": queue,
                "boot": boot,
                "debug": debug,
                "last": last,
                "features": sorted(n for bit, n in DEBUG_NAMES.items() if debug & bit),
            }
    return None


def check(elf: pathlib.Path | str, boot_version: int, require: bool = False) -> dict:
    """The image's ABI words; raises :class:`AbiMismatch` when it reads another
    boot block version than `boot_version`, or (`require`) has no words."""
    got = read(elf)
    if got is None:
        if require:
            raise AbiMismatch(f"{elf}: no .ka_abi words; build it with this tree")
        return {}
    if got["boot"] != boot_version:
        raise AbiMismatch(
            f"{elf}: the image reads boot block v{got['boot']}, the host writes "
            f"v{boot_version}"
        )
    return got


__all__ = ["DEBUG_FAIL", "DEBUG_LAST", "DEBUG_STEPS", "AbiMismatch", "check", "read"]
