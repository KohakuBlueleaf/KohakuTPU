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
from typing import Any, cast

from kohakutpu.application.runtime import generated

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
        #: what reads of these unchanged bytes made, by read: kept until a write
        self.kept: dict = {}
        #: these bytes are such a read's, so what reads them is kept too
        self.derived: bool = False
        #: the kept storage whose bytes and forms this one holds
        self.shared: Storage | None = None
        #: a graph's input slot: the host rewrites these bytes every call
        self.varying: bool = False

    # ------------------------------------------------------------- derived
    @property
    def unchanged(self) -> bool:
        """No kernel wrote these bytes from a value that can change."""
        return not self.varying and (self.host is not None or self.derived)

    def keep(self, read: Any, out: Any) -> None:
        """`out` (a storage, a `forms.Pending`) is what `read` makes of these
        bytes: held past its own free."""
        if out is self:
            return
        if isinstance(out, Storage):
            out = out._holder()
        out.derived = True
        out.view_refs += 1
        self.kept[read] = out
        if self.dev._recipe_recording is not None:
            self.dev._recipe_recording.on_keep()

    def _holder(self) -> "Storage":
        """What keeps these bytes unchanged: this handle shares it, so a write
        through the handle detaches it as it would any other reader."""
        if self.shared is not None:
            return self.shared
        st = Storage(self.dev, self.nbytes, self.addr, self.owned)
        st.forms, st.valid, st.zeroed = self.forms, self.valid, self.zeroed
        st.dtype, st.site = self.dtype, self.site
        st.host, st.host_f16_dtype = self.host, self.host_f16_dtype
        st.kept = self.kept
        # No tensor names it: it goes with the last reference.
        st._freed, st.view_refs = True, 1
        self.owned, self.shared, self.derived = False, st, True
        return st

    def adopt(self, st: "Storage") -> None:
        """Hold kept `st`'s bytes and forms instead of this storage's own."""
        if st is self or st is self.shared:
            return
        if self.shared is None:
            self.release_kept()
            self.dev.free(self.addr)
        else:
            self._leave_shared()
        self.addr, self.owned, self.shared = st.addr, False, st
        self.forms, self.valid, self.zeroed = st.forms, st.valid, st.zeroed
        self.dtype, self.site, self.derived = st.dtype, st.site, True
        self.host, self.host_f16_dtype = st.host, st.host_f16_dtype
        self.kept = st.kept
        st.view_refs += 1

    def unshare(self) -> None:
        """Own bytes again before a write: the kept storage stays as it is,
        this one holds a copy of its current form."""
        st, self.shared = self.shared, None
        if st is None:
            return
        key = FLAT if FLAT in st.valid else next(iter(st.valid))
        held = st.forms[key]
        self.owned, self.derived, self.kept = True, False, {}
        self.host = None
        self.addr = self.dev.alloc(st.forms[FLAT].nbytes)
        self.forms = {FLAT: Form(FLAT, self.addr, st.forms[FLAT].nbytes, False)}
        at = self.form(key, held.nbytes).addr
        generated.copy(self.dev, held.nbytes, {"src": held.addr, "dst": at})
        self.valid, self.zeroed = {key}, {key} & st.zeroed
        self.shared = st
        self._leave_shared()

    def _leave_shared(self) -> None:
        st, self.shared = cast(Storage, self.shared), None
        st.view_refs -= 1
        if st.view_refs <= 0 and st._freed:
            st.free()

    def overwritten(self) -> None:
        """A kernel is about to write these bytes: what reads of them made and
        their standing as a read's bytes end now."""
        self.release_kept()
        self.derived = False

    def release_kept(self) -> None:
        if not self.kept:
            return
        for st in self.kept.values():
            st.view_refs -= 1
            if st.view_refs <= 0 and st._freed:
                st.free()
        self.kept = {}

    # ------------------------------------------------------------- forms
    def has(self, key: tuple) -> bool:
        return key in self.valid

    def form(self, key: tuple, nbytes: int) -> Form:
        """The form `key`, allocated if new or too small (contents then not
        valid)."""
        f = self.forms.get(key)
        if f is not None and (f.nbytes < nbytes or (key != FLAT and not f.owned)):
            self.release_kept()
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
        generated.copy(self.dev, nbytes, {"src": addr, "dst": f.addr})
        return f

    def ensure_flat_bytes(self, min_bytes: int) -> None:
        """Ensure the primary flat allocation has at least `min_bytes` bytes
        while keeping `self.nbytes` as the logical byte size."""
        min_bytes = int(min_bytes)
        f = self.forms.get(FLAT)
        if not self.owned or f is None or f.nbytes >= min_bytes:
            return
        self.release_kept()
        new_addr = self.dev.alloc(min_bytes)
        self.dev.free(self.addr)
        self.addr = new_addr
        self.forms[FLAT] = Form(FLAT, self.addr, min_bytes, False)

    def wrote(self, key: tuple, keep_host: bool = False) -> None:
        """A kernel wrote form `key`: every other form is stale (and no
        padding is known zero until its writer says so)."""
        self.release_kept()
        self.derived = False
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
        if self.shared is not None:
            self.forms, self.valid, self.zeroed, self.kept = {}, set(), set(), {}
            self._leave_shared()
            return
        self.release_kept()
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
