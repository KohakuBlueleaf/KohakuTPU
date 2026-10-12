"""A logical buffer on the device and the physical forms its bytes are held in.

The primary form, ``flat``, is the row-major bytes a host (or tinygrad)
expects. A kernel may need another form -- a row statistic held replicated
over 32 columns (``rowstat``), an MXFP7 packing of a 2-D view (``mx7a`` /
``mx7b``), f16 copies of a bool / int / f32 buffer -- and the forms a kernel
writes are the valid ones until the next write. A form is made on the card by
a conversion kernel or refused; bytes cross the link only on upload and read
back.
"""

from dataclasses import dataclass
from typing import Any

FLAT = ("flat",)


@dataclass
class Form:
    key: tuple
    addr: int
    nbytes: int
    owned: bool  # a side allocation, freed with the storage


class Storage:
    """`nbytes` of flat bytes at `addr`, plus side forms."""

    def __init__(self, dev, nbytes: int, addr: int | None = None, owned=True) -> None:
        self.dev = dev
        self.nbytes = int(nbytes)
        self.addr = dev.alloc(nbytes) if addr is None else addr
        self.owned = owned
        self.forms: dict = {FLAT: Form(FLAT, self.addr, self.nbytes, False)}
        self.valid: set = {FLAT}
        #: the bytes last uploaded (or folded) while no kernel has written
        #: since: what an f16 form of another element type is packed from
        self.host: bytes | None = None
        #: the element type a kernel last wrote it as (numpy), else None
        self.dtype: Any = None

    # ------------------------------------------------------------- forms
    def has(self, key: tuple) -> bool:
        return key in self.valid

    def form(self, key: tuple, nbytes: int) -> Form:
        """The form `key`, allocated if new or too small (contents then not
        valid)."""
        f = self.forms.get(key)
        if f is not None and f.nbytes < nbytes:
            if f.owned:
                self.dev.free(f.addr)
            self.valid.discard(key)
            f = None
        if f is None:
            f = Form(key, self.dev.alloc(nbytes), int(nbytes), True)
            self.forms[key] = f
        return f

    def wrote(self, key: tuple, keep_host: bool = False) -> None:
        """A kernel wrote form `key`: every other form is stale."""
        self.valid = {key}
        if not keep_host:
            self.host = None

    def made(self, key: tuple) -> None:
        """Form `key` was made from a valid one: both hold the contents."""
        self.valid.add(key)

    def free(self) -> None:
        for f in self.forms.values():
            if f.owned:
                self.dev.free(f.addr)
        if self.owned:
            self.dev.free(self.addr)
        self.forms.clear()
        self.valid.clear()

    def __repr__(self) -> str:
        forms = ", ".join(k[0] for k in sorted(self.valid))
        return f"Storage({self.addr:#x}, {self.nbytes:,} B, valid: {forms})"


class StorageView:
    """`nbytes` of a storage starting `offset` bytes in (a sub-buffer): the
    flat form only."""

    def __init__(self, base: Storage, offset: int, nbytes: int) -> None:
        self.base, self.offset, self.nbytes = base, int(offset), int(nbytes)

    @property
    def addr(self) -> int:
        return self.base.addr + self.offset

    def __repr__(self) -> str:
        return f"StorageView({self.base!r} + {self.offset}, {self.nbytes} B)"


__all__ = ["FLAT", "Form", "Storage", "StorageView"]
