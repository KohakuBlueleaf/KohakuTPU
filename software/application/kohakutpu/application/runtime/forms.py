"""Making the form a kernel reads, and launching kernels.

A kernel reads f16: an f32 buffer has an f16 form (its values rounded
once), made from the host bytes it was uploaded from (packed on the way in)
or on the card from its f32 bytes; a row statistic is those f16 values (16
a word). A cluster operand is read in an MXFP7 packing of a 2-D view
(`kernels.matmul.quant_kernel`), kept for the next call while the buffer is
unwritten. Elements move between layouts by `relayout` (`kernels.lanes`).
"""

import hashlib
import json
import os
import pathlib
import weakref
from collections import OrderedDict
from dataclasses import dataclass, replace

import numpy as np

from kohakutpu.application.kernels import lanes as LN
from kohakutpu.application.kernels import matmul as MM
from kohakutpu.application.kernels import prepared as P
from kohakutpu.application.kernels import relayout as RL
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.runtime import costs, generated
from kohakutpu.application.runtime.storage import FLAT, Storage, StorageView
from kohakutpu.compiler import _rs
from kohakutpu.language.numerics import fp16

F16 = ("f16",)
#: ``("rows", n, np)``: rows of n f16 values held np apart (np whole 32-byte
#: words) -- what a kernel writes when n * 2 bytes is not whole words.
ROWS = "rows"
#: ``("view", src_bytes, levels, at, shape, coef, off)``: a word-walk view of
#: another f16 storage, read through by consumers without a materialising copy.
VIEW = "view"


def rows_key(n: int, np_: int) -> tuple:
    return (ROWS, n, np_)


def view_key(
    src_bytes: int,
    levels: tuple,
    at: int,
    shape: tuple = (),
    coef: tuple = (),
    off: int = 0,
) -> tuple:
    return (
        VIEW,
        int(src_bytes),
        tuple((int(c), int(s)) for c, s in levels),
        int(at),
        tuple(int(d) for d in shape),
        tuple((int(e), int(s)) for e, s in coef),
        int(off),
    )


#: Layout hints by device: producer site -> "rows" (its consumers read its
#: rows padded to whole words: written so) or "flat" (a consumer needed its
#: row-major bytes: written so for good). Learned from the consumers, so
#: one model run makes the same choices offline and on the card.
_hints: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
#: Whether producers follow their consumers' hints (else every one writes
#: its row-major bytes).
LAYOUT_HINTS = True
#: The hints a library holds (`save_hints`), beside its classes: a device
#: with that library starts from them, so a captured model's first step
#: runs the layouts (and library classes) of its steady steps.
HINTS_FILE = "layout_hints.json"


class Hints(dict):
    """Hints by site, a site keyed by its `repr` (what a hints file holds:
    sites are shapes, dtypes and expressions, never addresses)."""

    revision = 0

    def __getitem__(self, site):
        return super().__getitem__(repr(site))

    def __setitem__(self, site, value) -> None:
        key = repr(site)
        if super().get(key) != value:
            self.revision += 1
        super().__setitem__(key, value)

    def __contains__(self, site) -> bool:
        return super().__contains__(repr(site))

    def get(self, site, default=None):
        return super().get(repr(site), default)

    def setdefault(self, site, value):
        key = repr(site)
        if not super().__contains__(key):
            self.revision += 1
        return super().setdefault(key, value)


def hints(dev) -> dict:
    """`dev`'s layout hints (`_hints`), first those of its library's hints
    file (`seed_hints`); a scratch dict when they are off."""
    if not LAYOUT_HINTS:
        return {}
    got = _hints.get(dev)
    if got is None:
        got = _hints[dev] = Hints()
        seed_hints(dev, getattr(dev, "library", None))
    return got


def library_hints(library) -> dict:
    """The hints `library`'s hints file (`HINTS_FILE`) holds, by site text."""
    path = pathlib.Path(library) / HINTS_FILE if library else None
    if path is None or not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def seed_hints(dev, library) -> None:
    """`dev`'s hints start from `library`'s (a capture's from the library it
    adds to, so its kernels are those a run of that library makes)."""
    if LAYOUT_HINTS:
        dict.update(hints(dev), library_hints(library))


def save_hints(learned: dict, library) -> None:
    """`learned` hints (a `hints` dict's items, by site text) merged into
    `library`'s hints file: a site one model hints "flat" stays flat."""
    held = library_hints(library)
    for site, hint in learned.items():
        held[site] = "flat" if "flat" in (hint, held.get(site)) else hint
    path = pathlib.Path(library) / HINTS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(held, indent=1, sort_keys=True), encoding="utf-8")


def padded(st: Storage) -> tuple | None:
    """The valid padded-rows form's key, when the row-major bytes are not."""
    if FLAT in st.valid:
        return None
    return next((k for k in st.valid if k[0] == ROWS), None)


def viewed(st: Storage) -> tuple | None:
    """The valid word-walk view form's key, when the row-major bytes are not."""
    if FLAT in st.valid:
        return None
    return next((k for k in st.valid if k[0] == VIEW), None)


def materialize_view(dev, st: Storage, key: tuple, item: int = 2) -> int:
    """Materialise a word-walk view form `key` into `st`'s flat (or f16) bytes."""
    _, src_bytes, levels, at, _, _, _ = key
    n = st.nbytes // item
    nbytes = 2 * n
    if item == 2:
        dst_at, made = st.addr, FLAT
    else:
        dst_at, made = st.form(F16, nbytes).addr, F16
    dlevels = ((-(-nbytes // 32), 32),)
    binds = {"src": st.forms[key].addr, "dst": dst_at}
    generated.walk(dev, src_bytes, levels, at, nbytes, dlevels, 0, binds, "move")
    st.made(made)
    return dst_at


class FormError(RuntimeError):
    """A form this machine cannot make from what the buffer holds."""


def launch(
    dev, kernel, given: dict, binds: dict, kind: str = "dsl", info=None, **consts
):
    """Compile `kernel` for `given` (cached) and run it with `binds`."""
    compiled = dev.specialise(kernel, given, **consts)
    run = dev.run(compiled, binds, kind=kind, info=info)
    run.info.setdefault("compiled", compiled)
    return run


def base_of(s):
    """``(storage, byte offset)`` of a storage or a view of one."""
    if isinstance(s, StorageView):
        return s.base, s.offset
    return s, 0


def f16(dev, s, dtype, n: int) -> int:
    """The address of `s`'s contents as f16 (`dtype` its element type, `n`
    its elements; a sub-buffer's inside its base's)."""
    st, off = base_of(s)
    item = np.dtype(dtype).itemsize
    if off and np.dtype(dtype) != np.float16:
        return f16(dev, st, dtype, st.nbytes // item) + off // item * 2
    if np.dtype(dtype) == np.float16:
        if FLAT in st.valid:
            return st.addr + off
        key = padded(st)
        if key is not None:
            compact(dev, st, key)
            return st.addr + off
        vkey = viewed(st)
        if vkey is not None:
            materialize_view(dev, st, vkey)
            return st.addr + off
        raise FormError("an f16 buffer with no current form, read row-major")
    if F16 in st.valid:
        return st.forms[F16].addr
    f = st.form(F16, 2 * n)
    key = padded(st)
    if key is not None:
        return compact(dev, st, key, np.dtype(dtype).itemsize)
    vkey = viewed(st)
    if vkey is not None:
        return materialize_view(dev, st, vkey, np.dtype(dtype).itemsize)
    if st.host is not None:
        st.host_f16_dtype = dtype
        raw = np.frombuffer(st.host, dtype)[:n]
        dev.write(f.addr, fp16.write(raw).tobytes())
    elif FLAT in st.valid and np.dtype(dtype) == np.float32:
        generated.launch(
            dev,
            "convert.cast_kernel",
            {"n": n, "src": "f32", "dst": "f16"},
            {"in0": st.addr, "out": f.addr},
            kind="convert",
        )
    else:
        raise FormError(f"{np.dtype(dtype)} values with no f16 form and no host bytes")
    st.made(F16)
    return f.addr


#: Relayout plans by walk (`kernels.lanes.plan`; their tables are each
#: device's, `table`).
PLANS_KEPT = 1024
_plans: OrderedDict = OrderedDict()


def relayout(
    dev,
    src: int,
    n: int,
    walk: tuple,
    at: int,
    dwalk: tuple,
    m: int,
    dst: int,
    dat: int = 0,
    keep: bool = False,
) -> None:
    """`m` f16 values row-major at `dst` from the `n` row-major f16 values
    at `src`: source element `at` walked by `walk` (``(count, element
    stride)``, outermost first) into destination elements walked by `dwalk`
    from element `dat`; a destination element no walk reaches is 0, or with
    `keep` what `dst` held (`m` values in whole words). One kernel of
    `kernels.relayout`'s forms; a walk pair none takes, `kernels.lanes`."""
    hw = RL.hw_for(dev)
    # The destination port starts at its line (the words before kept).
    pad = dst % (hw.line * hw.word) // 2
    try:
        rp = RL.plan(n, walk, at, dwalk, dat + pad, m + pad, keep, hw, pad)
    except RL.Unplanned:
        rp = None
    if rp is not None:
        binds = {
            "src": src,
            "dst": dst - 2 * pad,
            "index": table(dev, rp.index, np.float32),
            "mask": table(dev, rp.masks),
        }
        pl = generated.relayout_plan(
            "relayout.plan", n, walk, at, dwalk, dat + pad, m + pad, keep, pad
        )
        generated.launch(
            dev, "relayout.relayout_kernel", {"pl": pl}, binds, kind="convert"
        )
        return
    key = (n, tuple(walk), at, tuple(dwalk), dat, m, keep)
    pl = _plans.get(key)
    if pl is None:
        pl = _plans[key] = LN.plan(n, walk, at, dwalk, dat, m, keep)
        if len(_plans) > PLANS_KEPT:
            _plans.popitem(last=False)
    _plans.move_to_end(key)
    binds = {
        "src": src,
        "dst": dst,
        "index": table(dev, pl.index, np.float32),
        "mask": table(dev, pl.masks),
    }
    made = generated.relayout_plan("lanes.plan", n, walk, at, dwalk, dat, m, keep)
    generated.launch(dev, "lanes.relayout_kernel", {"pl": made}, binds, kind="convert")


def repitch(
    dev, src: int, src_pitch: int, dst: int, dst_pitch: int, rows: int, w: int
) -> None:
    """`rows` rows of `w` f16 values from `src_pitch` apart to `dst_pitch`
    apart (both addresses on words): the mover where rows and pitches are
    whole 32-byte words, else `relayout` (exact; the destination between its
    rows 0)."""
    if not ((2 * w) % 32 or (2 * src_pitch) % 32 or (2 * dst_pitch) % 32):
        words = 2 * w // 32
        generated.walk(
            dev,
            ((rows - 1) * src_pitch + w) * 2,
            ((rows, 2 * src_pitch), (words, 32)),
            0,
            ((rows - 1) * dst_pitch + w) * 2,
            ((rows, 2 * dst_pitch), (words, 32)),
            0,
            {"src": src, "dst": dst},
            "convert",
        )
        return
    walk, dwalk = ((rows, src_pitch), (w, 1)), ((rows, dst_pitch), (w, 1))
    n, m = (rows - 1) * src_pitch + w, (rows - 1) * dst_pitch + w
    relayout(dev, src, n, walk, 0, dwalk, m, dst)


def compact(dev, st: Storage, key: tuple, item: int = 2) -> int:
    """A padded-rows form (f16 values) made row-major (`repitch`): the flat
    bytes of an f16 buffer, else (`item`-byte elements) its f16 form.
    Returns that address."""
    _, w, wp = key
    if st.site is not None:
        # A consumer wants the producer's row-major bytes after all.
        hints(dev)[st.site[0]] = "flat"
    rows = st.nbytes // (item * w)
    if item == 2:
        at, made = st.addr, FLAT
    else:
        at, made = st.form(F16, 2 * rows * w).addr, F16
    repitch(dev, st.forms[key].addr, wp, at, w, rows, w)
    st.made(made)
    return at


def mx7(
    dev,
    s,
    dtype,
    src: Operand,
    rows: int,
    cols: int,
    mm: MM.Mm,
    side: str,
    batches: int,
) -> int:
    """The address of an MXFP7 packing of `batches` [rows, cols] blocks of
    `s` for matmul `mm` (A packing for `side` "a", tiles of tm, else B)."""
    g = mm.tm // 4 if side == "a" else mm.tn // 4
    rp = mm.mp if side == "a" else max(rows, mm.n)
    return packed(dev, s, dtype, src, rows, cols, rp, mm.kp, side, g, mm.nk, batches)


def f16_rows(dev, s, dtype, src: Operand, cols: int) -> int | None:
    """The address of the f16 A a contraction kernel reads
    (`activation_fp16`): `src`'s rows of `cols` in `s`, dense from a word,
    or None where they are not."""
    if isinstance(s, Pending):
        s, dtype = s.made(), np.float16
    st, off = base_of(s)
    item = np.dtype(dtype).itemsize
    first = src.offset + off // item
    if src.pitch != cols or first % 16:
        return None
    return f16(dev, st, dtype, st.nbytes // item) + 2 * first


@dataclass(frozen=True)
class Step:
    """One relayout making a `Pending` buffer: `n` f16 values at `src` (None:
    the previous step's result) walked from element `at` by `walk` into the
    `m` destination elements `dwalk` reaches from `dat`, the rest 0."""

    src: int | None
    n: int
    walk: tuple
    at: int
    dwalk: tuple
    dat: int
    m: int

    def made_by(self) -> dict:
        """The generator's plan of the step (`Step::made_by`)."""
        return generated.relayout_plan(
            "relayout.plan",
            self.n,
            self.walk,
            self.at,
            self.dwalk,
            self.dat,
            self.m,
            False,
        )


class Pending:
    """A dense f16 buffer of `nbytes` that one kernel reads, not made yet:
    its `steps` run inside that kernel (a packing, `packed`; a rows kernel,
    `rows.run`), or, when one is a walk pair `kernels.relayout` does not
    plan or the kernel cannot take it, as their own relayouts (`made`)."""

    def __init__(self, dev, nbytes: int, steps: tuple) -> None:
        self.dev, self.nbytes, self.steps = dev, int(nbytes), tuple(steps)
        #: holds the packings made of it, freed with it
        self.holder = Storage(dev, 32)
        self.storage: Storage | None = None

    def made(self) -> Storage:
        """The buffer itself, its steps run as relayouts."""
        if self.storage is None:
            prev, temps = None, []
            for i, s in enumerate(self.steps):
                last = i == len(self.steps) - 1
                out = Storage(self.dev, self.nbytes if last else max(32, 2 * s.m))
                src = prev.addr if s.src is None else s.src
                relayout(
                    self.dev, src, s.n, s.walk, s.at, s.dwalk, s.m, out.addr, s.dat
                )
                if prev is not None:
                    temps.append(prev)
                prev = out
            for t in temps:
                t.free()
            prev.wrote(FLAT)
            prev.dtype = np.float16
            self.storage = prev
        return self.storage

    def free(self) -> None:
        self.holder.free()
        if self.storage is not None:
            self.storage.free()


_PREPARED_DEFAULTS = json.loads(_rs.PREPARED_DEFAULTS)
#: Whether a `Pending` operand is made inside the kernel that reads it (else
#: by its own relayouts first).
PREPARED = (
    os.environ.get("KTPU_PREPARED", "1" if _PREPARED_DEFAULTS["prepared"] else "0")
    == "1"
)
#: A pending packing folds only with a measured winning recipe.
PREPARED_QUANT = (
    os.environ.get(
        "KTPU_PREPARED_QUANT", "1" if _PREPARED_DEFAULTS["prepared_quant"] else "0"
    )
    == "1"
)


def plans_of(dev, steps) -> tuple | None:
    """The `kernels.relayout` plans of `steps`, None when one is not planned."""
    hw = RL.hw_for(dev)
    try:
        return tuple(
            RL.plan(s.n, s.walk, s.at, s.dwalk, s.dat, s.m, False, hw) for s in steps
        )
    except RL.Unplanned:
        return None


def step_binds(dev, plans, prefix: str) -> dict:
    """The tables of `plans` uploaded (`table`), by `relayout.step_ports` name."""
    out = {}
    for i, pl in enumerate(plans):
        out[f"{prefix}index{i}"] = table(dev, pl.index, np.float32)
        out[f"{prefix}mask{i}"] = table(dev, pl.masks)
    return out


def _prepared_quant(dev, steps, src, rows, cols, rp, cp, side, g, nk, batches):
    recipe = (
        costs.prepared_quant(
            steps, src, rows, cols, rp, cp, side, g, nk, batches, dev.machine.board
        )
        if PREPARED_QUANT
        else None
    )
    if recipe is None:
        return None
    qs = recipe["quant"]
    measured = tuple(
        Step(
            steps[0].src if i == 0 else None,
            **{
                **v,
                "walk": tuple(map(tuple, v["walk"])),
                "dwalk": tuple(map(tuple, v["dwalk"])),
            },
        )
        for i, v in enumerate(recipe["steps"])
    )
    plans = plans_of(dev, measured)
    if plans is None or not P.direct_destination(plans):
        return None
    call = _quant_call(
        Operand("full", **qs["src"]),
        rows,
        qs["cols"],
        rp,
        cp,
        side,
        g,
        nk,
        batches,
        recipe.get("pad_strategy", "native"),
        prepared=[s.made_by() for s in measured],
    )
    return call, {"p_x": steps[0].src, **step_binds(dev, plans, "p_")}


def _quant_call(src, rows, cols, rp, cp, side, g, nk, batches, strategy, **more):
    """The `matmul.quant_kernel` arguments of one packing."""
    return dict(
        src=vars(src),
        rows=rows,
        cols=cols,
        rp=rp,
        cp=cp,
        side=side,
        g=g,
        nk=nk,
        batches=batches,
        pad_strategy=strategy,
        **more,
    )


def packed(
    dev, s, dtype, src: Operand, rows, cols, rp, cp, side, g, nk, batches=1
) -> int:
    """The address of `batches` [rows, cols] blocks of `s` quantised into
    [batches * rp, cp] MXFP7 (A packing for `side` "a", else B; tiles of `g`
    lane groups, `nk` K-blocks a chunk), zero-padded; kept while `s` is
    unwritten. Pending inputs and non-word column padding are materialized
    before packing."""
    if isinstance(s, Pending):
        prepared = _prepared_quant(
            dev, s.steps, src, rows, cols, rp, cp, side, g, nk, batches
        )
        if prepared is not None:
            key = ("prepared_mx7", side, src, rows, cols, rp, cp, g, nk, batches)
            if key not in s.holder.valid:
                f = s.holder.form(key, batches * rp * cp)
                call, binds = prepared
                generated.launch(
                    dev,
                    "matmul.quant_kernel",
                    call,
                    dict(binds, q=f.addr),
                    kind="quant",
                )
                s.holder.made(key)
            return s.holder.forms[key].addr
        s, dtype = s.made(), np.float16
    st, off = base_of(s)
    item = np.dtype(dtype).itemsize
    src = replace(src, offset=src.offset + off // item)
    key = ("mx7", side, src, rows, cols, rp, cp, g, nk, batches)
    if key in st.valid:
        return st.forms[key].addr
    vkey = viewed(st) if not off else None
    if (
        vkey is not None
        and src.kind == "full"
        and src.offset == 0
        and src.pitch == cols
        and cols % 16 == 0
        and cols == cp
    ):
        _, src_bytes, levels, at, _, _, _ = vkey
        vbase = st.forms[vkey].addr
        f = st.form(key, batches * rp * cp)
        k, given = MM.quant_walk_kernel(
            src_bytes,
            levels,
            at,
            rows,
            cols,
            rp,
            cp,
            side,
            g,
            nk,
            batches,
        )
        launch(dev, k, given, {"x": vbase, "q": f.addr}, kind="quant")
        st.made(key)
        return f.addr
    n = st.nbytes // item
    # Whole rows of the buffer read (what a producer's row layout serves).
    whole_rows = src.kind == "full" and src.pitch == cols and src.offset % cols == 0
    held = _held_rows(st, src, cols)
    if held is not None:
        # Whole rows of a padded-rows form: read where they are.
        base, n, src, zero = held
    else:
        base, zero = f16(dev, st, dtype, n), False
    f = st.form(key, batches * rp * cp)

    tmps: list = []
    try:
        whole = -(-cols // 16) * 16
        if zero and cols % 16 and whole <= min(src.pitch, cp):
            # The row's padding is zero: its last word read whole is the
            # zero columns the packing pads with.
            cols = whole
        if cols % 16 or src.pitch % 16 or src.offset % 16:
            if st.site is not None and whole_rows and cols == st.site[1]:
                # Its producer's rows, read whole: written zero-padded to
                # whole words next time.
                hints(dev).setdefault(st.site[0], "rows")
            # Rows not of whole words: laid out ceil16(cols) apart first,
            # the extra columns zero (`relayout`).
            R, dp = batches * rows, -(-cols // 16) * 16
            walk = ((R, src.pitch), (cols, 1))
            step = Step(base, n, walk, src.offset, ((R, dp), (cols, 1)), 0, R * dp)
            prepared = _prepared_quant(
                dev,
                (step,),
                Operand("full", pitch=dp),
                rows,
                dp,
                rp,
                cp,
                side,
                g,
                nk,
                batches,
            )
            if prepared is not None:
                call, binds = prepared
                generated.launch(
                    dev,
                    "matmul.quant_kernel",
                    call,
                    dict(binds, q=f.addr),
                    kind="quant",
                )
                st.made(key)
                return f.addr
            t = Storage(dev, 2 * R * dp)
            tmps.append(t)
            relayout(
                dev, base, n, walk, src.offset, ((R, dp), (cols, 1)), R * dp, t.addr
            )
            base, src, cols = t.addr, Operand("full", pitch=dp, rows=R), dp

        recipe = costs.quant_recipe(src, rows, cols, rp, cp, side, g, nk, batches)
        strategy = (
            "staged"
            if costs.choose("staged_padding", recipe, dev.machine.board)
            else "native"
        )
        generated.launch(
            dev,
            "matmul.quant_kernel",
            _quant_call(src, rows, cols, rp, cp, side, g, nk, batches, strategy),
            {"x": base, "q": f.addr},
            kind="quant",
        )
    finally:
        for t in tmps:
            t.free()

    st.made(key)
    return f.addr


def _held_rows(st: Storage, src: Operand, cols: int) -> tuple | None:
    """``(address, f16 values, operand, zero)`` of `src`'s rows (`cols` of
    each) in `st`'s padded-rows form when its row-major bytes are not
    current and each read row is one of its rows -- `zero` when what
    follows the read columns to the row's pitch is zero (whole rows read,
    zero padding); None otherwise."""
    key = padded(st)
    if key is None:
        return None
    _, w, wp = key
    if src.kind != "full" or src.pitch != w or src.offset % w + cols > w:
        return None
    f = st.forms[key]
    at = src.offset // w * wp + src.offset % w
    zero = key in st.zeroed and src.offset % w == 0 and cols == w
    return f.addr, f.nbytes // 2, replace(src, offset=at, pitch=wp), zero


def for_port(dev, s, dtype, shape: tuple, spec) -> int:
    """The address of `s` (`dtype` elements, logical `shape`, row-major) in
    the form a kernel port of `spec` reads: f16 (made if need be), f32 bytes
    as they are, or an MXFP7 packing of its last two axes."""
    st, off = base_of(s)
    n = int(np.prod(shape))
    if spec.dtype in ("mx7a", "mx7b"):
        if len(shape) < 2:
            raise FormError(f"an MXFP7 port takes a 2-D tensor, not {shape}")
        lay = spec.layout_dict
        rows, cols = int(np.prod(shape[:-1])), shape[-1]
        side = "a" if spec.dtype == "mx7a" else "b"
        g = lay["gm"] if side == "a" else lay["gn"]
        src = Operand("full", pitch=cols, rows=rows)
        return packed(
            dev,
            s,
            dtype,
            src,
            rows,
            cols,
            spec.shape[-2],
            spec.shape[-1],
            side,
            g,
            lay["nk"],
        )
    if spec.dtype == "f16":
        return f16(dev, s, dtype, n)
    if spec.dtype == "f32" and np.dtype(dtype) == np.float32 and FLAT in st.valid:
        return st.addr + off
    raise FormError(f"a {np.dtype(dtype)} tensor for a {spec.text()} port")


def table(dev, arr, dtype: type = np.float16) -> int:
    """A constant uploaded once to `dev` as `dtype` (by content:
    `dev.tables`)."""
    a = np.ascontiguousarray(
        fp16.write(arr) if np.dtype(dtype) == np.float16 else arr, dtype
    )
    key = hashlib.sha1(a.tobytes() + repr((a.shape, a.dtype)).encode()).hexdigest()
    got = dev.tables.get(key)
    if got is None:
        got = Storage(dev, max(a.nbytes, 32))
        dev.write(got.addr, a.tobytes())
        dev.tables[key] = got
    return got.addr


def read(dev, s, dtype, n: int) -> bytes:
    """`s`'s contents as `n` `dtype` values, from whichever form is current:
    row-major bytes, the f16 form, or the padded-rows form."""
    st, off = base_of(s)
    dt = np.dtype(dtype)
    if FLAT in st.valid:
        return dev.read(st.addr + off, n * dt.itemsize)
    if off % dt.itemsize:
        raise FormError("a view of a non-flat form must start at an element")
    first = off // dt.itemsize
    if F16 in st.valid:
        vals = np.frombuffer(
            dev.read(st.forms[F16].addr + 2 * first, 2 * n), np.float16
        )
        return vals.astype(dt).tobytes()
    key = padded(st)
    if key is not None:
        _, w, wp = key
        row, col = divmod(first, w)
        rows = -(-(col + n) // w)
        raw = np.frombuffer(
            dev.read(st.forms[key].addr + row * wp * 2, rows * wp * 2), np.float16
        )
        return (
            raw.reshape(rows, wp)[:, :w].reshape(-1)[col : col + n].astype(dt).tobytes()
        )
    vkey = viewed(st)
    if vkey is not None:
        _, src_bytes, levels, at, _, _, _ = vkey
        raw = np.frombuffer(dev.read(st.forms[vkey].addr, src_bytes), np.float16)
        idx = np.array([at // 2], dtype=np.int64)
        for count, byte_stride in levels[:-1]:
            step = (byte_stride // 2) * np.arange(count, dtype=np.int64)
            idx = (idx[:, None] + step[None, :]).reshape(-1)
        offs = np.arange(levels[-1][0] * 16, dtype=np.int64)
        full_idx = (idx[:, None] + offs[None, :]).reshape(-1)[first : first + n]
        return raw[full_idx].astype(dt).tobytes()
    raise FormError("a buffer with no current form")


__all__ = [
    "F16",
    "HINTS_FILE",
    "ROWS",
    "VIEW",
    "FormError",
    "Hints",
    "base_of",
    "compact",
    "f16",
    "f16_rows",
    "for_port",
    "launch",
    "library_hints",
    "materialize_view",
    "mx7",
    "packed",
    "padded",
    "read",
    "relayout",
    "repitch",
    "rows_key",
    "save_hints",
    "seed_hints",
    "table",
    "view_key",
    "viewed",
]
