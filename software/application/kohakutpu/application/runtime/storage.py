"""A logical buffer on the device and the physical forms its bytes are held in.

The primary form, ``flat``, is the row-major bytes a host (or tinygrad)
expects. A kernel may need another form -- an MXFP7 packing of a 2-D view
(``mx7a`` / ``mx7b``), f16 copies of a bool / int / f32 buffer, rows padded to
whole words -- and the forms a kernel
writes are the valid ones until the next write. A form is made on the card by
a conversion kernel or refused; bytes cross the link only on upload and read
back.
"""

from dataclasses import dataclass
from typing import Any

from kohakutpu.application.kernels import convert as C

FLAT = ("flat",)


@dataclass
class Form:
    key: tuple
    addr: int
    nbytes: int
    owned: bool  # a side allocation, freed with the storage
    src_st: Any = None

    def release(self) -> None:
        if self.src_st is not None:
            st = self.src_st
            self.src_st = None
            st.view_refs -= 1
            if st.view_refs <= 0 and st._freed:
                st.free()


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
        #: source element type of a host-materialized f16 form
        self.host_f16_dtype: Any = None
        #: the element type a kernel last wrote it as (numpy), else None
        self.dtype: Any = None
        #: valid forms whose padding (the elements a pitch adds) is zero:
        #: claimed by the writer of those elements, cleared by any write
        self.zeroed: set = set()
        #: ``(site, row length)`` of the producer whose layout the current
        #: forms are (`forms.hints`), None when no such producer wrote them
        self.site: Any = None
        self.view_refs: int = 0
        self._freed: bool = False

    # ------------------------------------------------------------- forms
    def has(self, key: tuple) -> bool:
        return key in self.valid

    def form(self, key: tuple, nbytes: int) -> Form:
        """The form `key`, allocated if new or too small (contents then not
        valid)."""
        f = self.forms.get(key)
        if f is not None and (f.nbytes < nbytes or (key != FLAT and not f.owned)):
            f.release()
            if f.owned:
                self.dev.free(f.addr)
            self.valid.discard(key)
            f = None
        if f is None:
            f = Form(key, self.dev.alloc(nbytes), int(nbytes), True)
            self.forms[key] = f
        return f

    def share_form(self, key: tuple, addr: int, nbytes: int, src_st=None) -> Form:
        """Snapshot a view descriptor's backing bytes; mutable storage is never shared."""
        # A contiguous result owns a snapshot: a later in-place source write cannot change it.
        f = self.form(key, nbytes)
        kernel, given = C.copy_kernel(nbytes)
        compiled = self.dev.specialise(kernel, given)
        self.dev.run(compiled, {"src": addr, "dst": f.addr}, kind="copy")
        return f

    def ensure_flat_bytes(self, min_bytes: int) -> None:
        """Ensure the primary flat allocation has at least `min_bytes` bytes
        while keeping `self.nbytes` as the logical byte size."""
        min_bytes = int(min_bytes)
        f = self.forms.get(FLAT)
        if not self.owned or f is None or f.nbytes >= min_bytes:
            return
        new_addr = self.dev.alloc(min_bytes)
        self.dev.free(self.addr)
        self.addr = new_addr
        self.forms[FLAT] = Form(FLAT, self.addr, min_bytes, False)

    def wrote(self, key: tuple, keep_host: bool = False) -> None:
        """A kernel wrote form `key`: every other form is stale (and no
        padding is known zero until its writer says so)."""
        for k, f in list(self.forms.items()):
            if k != key and not f.owned and k != FLAT:
                f.release()
                del self.forms[k]
        self.valid = {key}
        self.zeroed = set()
        self.site = None
        if not keep_host:
            self.host = None

    def made(self, key: tuple) -> None:
        """Form `key` was made from a valid one: both hold the contents."""
        self.valid.add(key)

    def free(self) -> None:
        if self.view_refs > 0:
            self._freed = True
            return
        self._freed = False
        for f in list(self.forms.values()):
            f.release()
            if f.owned:
                self.dev.free(f.addr)
        if self.owned and self.forms:
            self.dev.free(self.addr)
        self.forms.clear()
        self.valid.clear()
        self.zeroed.clear()

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
