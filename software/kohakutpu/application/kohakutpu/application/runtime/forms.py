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
import pathlib
import weakref
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Any

import numpy as np

from kohakutpu.application.kernels import matmul as MM
from kohakutpu.application.kernels import relayout as RL
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.runtime import costs, generated
from kohakutpu.application.runtime.memory.storage import (
    FLAT,
    Form,
    Storage,
    StorageView,
)
from kohakutpu.language import machine as MF
from kohakutpu.language.config import ENV
from kohakutpu.language.numerics import fp16
from kohakutpu.stack.rs import _rs

F16 = ("f16",)
#: ``("rows", n, np)``: rows of n f16 values held np apart (np whole 32-byte
#: words) -- what a kernel writes when n * 2 bytes is not whole words.
ROWS = "rows"
#: ``("view", src_bytes, levels, at, shape, coef, off)``: a word-walk view of
#: another f16 storage, read through by consumers without a materialising copy.
VIEW = "view"
#: The f16 values a relayout of another storage's bytes would write, not
#: written: its consumer reads them through the relayout (`defer`).
DEFER = ("deferred",)


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

    def setdefault(self, site, value=None):
        key = repr(site)
        if not super().__contains__(key):
            self.revision += 1
        return super().setdefault(key, value)


def hints(dev) -> Any:
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


#: Consumer packings by device: product site -> the `packed` form keys its
#: consumers read (insertion-ordered), each written by the product's epilogue.
_demands: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def demands(dev) -> dict:
    """`dev`'s consumer packings by product site (`_demands`)."""
    return _demands.setdefault(dev, {})


def mm_site(expr, bt: int, m: int, n: int, k: int) -> tuple:
    """The site of a product `expr` of `bt` [m, k] x [n, k] contractions."""
    return ("mm", repr(expr), bt, m, n, k)


def updated_site(st: Storage) -> tuple:
    """The site of a buffer its writer updates in place (a parameter): the
    buffer itself, each write the next version of the values it holds."""
    return ("w", id(st))


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
    if DEFER in st.valid:
        st.forms[DEFER].make()
    item = np.dtype(dtype).itemsize
    whole = st.nbytes // item
    if (off or n != whole) and np.dtype(dtype) != np.float16:
        return f16(dev, st, dtype, whole) + off // item * 2
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
    # The destination port starts at its line (the words before kept).
    machine = MF.load(dev.machine.board)
    line = max(machine.get("card.dram.line_bytes"), machine.die.word_bytes)
    pad = dst % line // 2
    made = generated.relayout_plan(
        "relayout.plan", n, walk, at, dwalk, dat + pad, m + pad, keep, pad
    )
    rp = generated.planned(dev, made)
    if "unplanned" not in rp:
        binds = {"src": src, "dst": dst - 2 * pad} | tables(dev, rp)
        generated.launch(
            dev, "relayout.relayout_kernel", {"pl": made}, binds, kind="convert"
        )
        return
    made = generated.relayout_plan("lanes.plan", n, walk, at, dwalk, dat, m, keep)
    binds = {"src": src, "dst": dst} | tables(dev, generated.planned(dev, made))
    generated.launch(dev, "lanes.relayout_kernel", {"pl": made}, binds, kind="convert")


def tables(dev, plan: dict) -> dict:
    """A plan's tables uploaded (`table`), by port."""
    return {
        "index": table(dev, plan["index"], np.float32),
        "mask": table(dev, plan["masks"], np.uint16),
    }


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
        #: held by the storage that keeps it (`Storage.keep`): a free waits
        self.view_refs, self._freed, self.derived = 0, False, False

    def made(self) -> Storage:
        """The buffer itself, its steps run as relayouts."""
        if self.storage is None:
            prev: Any = None
            temps = []
            try:
                for i, s in enumerate(self.steps):
                    last = i == len(self.steps) - 1
                    out = Storage(self.dev, self.nbytes if last else max(32, 2 * s.m))
                    try:
                        src = prev.addr if s.src is None else s.src
                        relayout(
                            self.dev,
                            src,
                            s.n,
                            s.walk,
                            s.at,
                            s.dwalk,
                            s.m,
                            out.addr,
                            s.dat,
                        )
                        if prev is not None:
                            temps.append(prev)
                    except BaseException:
                        out.free()
                        raise
                    prev = out
                prev.wrote(FLAT)
                prev.dtype = np.float16
                self.storage = prev
                return prev
            except BaseException:
                if prev is not None:
                    prev.free()
                raise
            finally:
                for t in temps:
                    t.free()
        return self.storage

    def free(self) -> None:
        if self.view_refs > 0:
            self._freed = True
            return
        self._freed = False
        self.holder.free()
        if self.storage is not None:
            self.storage.free()


_PREPARED_DEFAULTS = json.loads(_rs.PREPARED_DEFAULTS)
#: Whether a `Pending` operand is made inside the kernel that reads it (else
#: by its own relayouts first).
PREPARED = (
    ENV.get("KTPU_PREPARED", "1" if _PREPARED_DEFAULTS["prepared"] else "0") == "1"
)
#: A pending packing folds only with a measured winning recipe.
PREPARED_QUANT = (
    ENV.get("KTPU_PREPARED_QUANT", "1" if _PREPARED_DEFAULTS["prepared_quant"] else "0")
    == "1"
)
#: When a pending read is composed (`prepared::Composed`): off, on or cost.
COMPOSED = ENV.get("KTPU_COMPOSED", _PREPARED_DEFAULTS["composed"])
#: Whether a whole relayout's output is left to its reader (`defer`), else
#: written when its producer runs.
DEFERRED = ENV.get("KTPU_DEFERRED", "1") == "1"
#: A recorded graph's deferred form past its storage's last call: "drop"
#: ends it unmade (`drop`), "make" makes it with the live ones at the exit.
DEAD_DEFER = ENV.get("KTPU_DEAD_DEFER", "drop")


def defer(dev, dst: Storage, owner: Storage, step: Step) -> None:
    """`dst`'s f16 values are what `step` makes of `owner`'s bytes, not
    written: a packing reads them through the step (`packed`, `deferred`);
    any other read, a write of `dst` and a write of `owner` make them in
    place first (`Storage.settle`), ending the reads kept of them. `owner`
    stays allocated while the form stands."""
    dst.wrote(DEFER)
    f = Form(DEFER, dst.addr, dst.nbytes, False, src_st=owner, steps=(step,))

    def make() -> None:
        _relayouts(dev, f.steps, dst.addr)
        del dst.forms[DEFER]
        f.release()
        dst.release_kept()
        dst.valid.discard(DEFER)
        dst.valid.add(FLAT)

    f.make = make
    owner.readers.append(make)
    owner.view_refs += 1
    dst.forms[DEFER] = f
    dst.dtype = np.float16


def drop(st: Storage) -> None:
    """`st`'s deferred form ended unmade: nothing reads its values again."""
    f = st.forms.pop(DEFER)
    f.release()
    st.valid.discard(DEFER)


def deferred(st: Storage) -> Step | None:
    """The step `st`'s deferred form stands for (`defer`), None without one."""
    return st.forms[DEFER].steps[0] if DEFER in st.valid else None


def _relayouts(dev, steps: tuple, dst: int) -> None:
    """`steps` run as relayouts, the last into `dst`."""
    temps: list[Storage] = []
    try:
        for i, s in enumerate(steps):
            src = temps[-1].addr if s.src is None else s.src
            if i < len(steps) - 1:
                t = Storage(dev, max(32, 2 * s.m))
                try:
                    temps.append(t)
                except BaseException:
                    t.free()
                    raise
            at = temps[-1].addr if i < len(steps) - 1 else dst
            relayout(dev, src, s.n, s.walk, s.at, s.dwalk, s.m, at, s.dat)
    finally:
        for t in temps:
            t.free()


def made_by(s: Step) -> dict:
    """The `relayout.plan` call of `s` a generator call names."""
    args = dict(s.__dict__, src_n=s.n, keep=False, z0=0, tree=RL.PULL_TREE)
    del args["src"], args["n"]
    return {"made_by": {"gen": "relayout.plan", "args": args}}


def relayout_call(s: Step) -> dict:
    return {"gen": "relayout.relayout_kernel", "args": {"pl": made_by(s)}}


@lru_cache(maxsize=4096)
def _decided(mode: str, read: str, launches: str, machine: str, mutant, nodes) -> bool:
    return _rs.read_composed(mode, read, launches, machine, nodes, mutant)


def sequence_choice(dev, read: dict | list, launches: list, mode: str) -> bool:
    """Price whole alternatives under their emitted schedules and physical grant."""
    if mode == "off":
        return False

    def scheduled(call):
        generator = call["gen"]
        return {
            "gen": generator,
            "args": generated.prepared(generator, call["args"]),
            "force": generated.split_option(generator),
        }

    reads = read if isinstance(read, list) else [read]
    nodes = (dev.home,) if dev.home is not None else tuple(dev.card.nodes)
    return _decided(
        mode,
        json.dumps([scheduled(call) for call in reads], sort_keys=True),
        json.dumps([scheduled(call) for call in launches], sort_keys=True),
        generated.rs.machine_json(dev.machine),
        generated.rs._MUTANT.get(),
        nodes,
    )


def composed(dev, read: dict, launches: list) -> bool:
    """Whether the complete composed alternative wins for this device's grant."""
    return sequence_choice(dev, read, launches, COMPOSED)


def plans_of(dev, steps) -> tuple | None:
    """The `relayout.plan` plans of `steps`, None when one is not planned."""
    plans = tuple(generated.planned(dev, s.made_by()) for s in steps)
    return None if any("unplanned" in pl for pl in plans) else plans


def step_binds(dev, plans, prefix: str) -> dict:
    """The tables of `plans` uploaded (`tables`), by `relayout.step_ports` name."""
    out = {}
    for i, pl in enumerate(plans):
        got = tables(dev, pl)
        out[f"{prefix}index{i}"], out[f"{prefix}mask{i}"] = got["index"], got["mask"]
    return out


def _composed_plan(dev, steps, src, rows, cols, rp, cp, side, g, nk, batches):
    """A composed packing call, without allocating forms or uploading tables."""
    if COMPOSED == "off" or not PREPARED_QUANT:
        return None
    quant_src = src
    if cols % 16 or src.pitch % 16 or src.offset % 16:
        R, dp = batches * rows, -(-cols // 16) * 16
        walk, dwalk = ((R, src.pitch), (cols, 1)), ((R, dp), (cols, 1))
        last = Step(None, steps[-1].m, walk, src.offset, dwalk, 0, R * dp)
        steps = (*steps, last)
        quant_src, cols = Operand("full", pitch=dp, rows=R), dp
    quant = {
        "gen": "matmul.quant_kernel",
        "args": {
            "src": vars(quant_src),
            "rows": rows,
            "cols": cols,
            "rp": rp,
            "cp": cp,
            "side": side,
            "g": g,
            "nk": nk,
            "batches": batches,
        },
    }
    prepared = [made_by(s) for s in steps]
    args = dict(quant["args"], prepared=prepared, composed=[0])
    read = {"gen": "matmul.quant_kernel", "args": args}
    if not composed(dev, read, [*map(relayout_call, steps), quant]):
        return None
    return args


def _prepared_plan(dev, steps, src, rows, cols, rp, cp, side, g, nk, batches):
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
    if plans is None or not plans[-1]["direct"]:
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
    return call, plans


def quant_production(dev, steps, src, rows, cols, rp, cp, side, g, nk, batches):
    """Select composed, prepared or materialized production without device allocation.

    Planning may compile recipes; only execution binds their tables and outputs.
    """
    args = (src, rows, cols, rp, cp, side, g, nk, batches)
    call = _composed_plan(dev, steps, *args)
    if call is not None:
        return "composed", call, None
    prepared = _prepared_plan(dev, steps, *args)
    if prepared is not None:
        call, plans = prepared
        return "prepared", call, plans
    return "materialized", None, None


def _produce_quant(dev, steps, holder, key, size, production):
    mode, call, plans = production
    if key not in holder.valid:
        f = holder.form(key, size)
        binds = {"p_x": steps[0].src, "q": f.addr}
        if mode == "prepared":
            binds.update(step_binds(dev, plans, "p_"))
        generated.launch(dev, "matmul.quant_kernel", call, binds, kind="quant")
        holder.made(key)
    return holder.forms[key].addr


def _stepped(dev, steps, holder, src, rows, cols, rp, cp, side, g, nk, batches):
    """Read the steps into a packed form when selected without materialization."""
    production = quant_production(
        dev, steps, src, rows, cols, rp, cp, side, g, nk, batches
    )
    mode = production[0]
    if mode == "materialized":
        return None
    key = (f"{mode}_mx7", side, src, rows, cols, rp, cp, g, nk, batches)
    return _produce_quant(dev, steps, holder, key, batches * rp * cp, production)


def writer_quant(dev, key: tuple) -> dict | None:
    """A fused writer quant step for whole values (None if it cannot lower).

    This describes writer capability, not the standalone production policy
    selected by `quant_production`.
    """
    _, side, src, rows, cols, rp, cp, g, nk, batches = key
    out = {"side": side, "g": g, "nk": nk, "rp": rp, "cp": cp, "rows": rows}
    if cols % 16 == 0:
        recipe = costs.quant_recipe(src, rows, cols, rp, cp, side, g, nk, batches)
        staged = costs.choose("staged_padding", recipe, dev.machine.board)
        strategy = "staged" if staged else "native"
        return dict(out, cols=cols, src=vars(src), pad_strategy=strategy)
    dp = -(-cols // 16) * 16
    laid = Operand("full", pitch=dp, rows=rows)
    step = Step(
        None,
        rows * cols,
        ((rows, cols), (cols, 1)),
        0,
        ((rows, dp), (cols, 1)),
        0,
        rows * dp,
    )
    composed = [made_by(step)]
    args = _quant_call(laid, rows, dp, rp, cp, side, g, nk, batches, "native")
    read = {
        "gen": "matmul.quant_kernel",
        "args": dict(args, prepared=composed, composed=[0]),
    }
    if not sequence_choice(dev, read, [], "on"):
        return None
    return dict(out, cols=dp, src=vars(laid), composed=composed)


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
    unwritten. Pending inputs and deferred forms (`defer`) are read through
    their steps where the packing takes them, else made first; non-word
    column padding is laid out before packing."""
    args = (src, rows, cols, rp, cp, side, g, nk, batches)
    if isinstance(s, Pending):
        got = _stepped(dev, s.steps, s.holder, *args)
        if got is not None:
            return got
        s, dtype = s.made(), np.float16
    st, off = base_of(s)
    if DEFER in st.valid:
        got = None if off else _stepped(dev, st.forms[DEFER].steps, st, *args)
        if got is not None:
            return got
        st.forms[DEFER].make()
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
        generated.launch(
            dev,
            "matmul.quant_walk_kernel",
            {
                "src_bytes": src_bytes,
                "levels": generated.walks(levels),
                "at": at,
                "rows": rows,
                "cols": cols,
                "rp": rp,
                "cp": cp,
                "side": side,
                "g": g,
                "nk": nk,
                "batches": batches,
            },
            {"x": vbase, "q": f.addr},
            kind="quant",
        )
        st.made(key)
        return f.addr
    n = st.nbytes // item
    # Whole rows of the buffer read (what a producer's row layout serves).
    whole_rows = src.kind == "full" and src.pitch == cols and src.offset % cols == 0
    site = st.site[0] if st.site is not None else None
    if (
        site is not None
        and batches == 1
        and whole_rows
        and src.offset == 0
        and (
            (site[0] == "mm" and (rows, cols, cp) == (site[3], site[4], site[4]))
            or (site[0] == "w" and rows * cols * item == st.nbytes)
        )
    ):
        # A product's or an updated buffer's whole values: packed by their
        # writer from its next write.
        demands(dev).setdefault(site, {})[key] = None
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
            laid = Operand("full", pitch=dp)
            args = (laid, rows, dp, rp, cp, side, g, nk, batches)
            production = quant_production(dev, (step,), *args)
            if production[0] != "materialized":
                return _produce_quant(
                    dev, (step,), st, key, batches * rp * cp, production
                )
            t = Storage(dev, 2 * R * dp)
            try:
                tmps.append(t)
            except BaseException:
                t.free()
                raise
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
        ref = None
        try:
            raw = a.tobytes()
            dev.write(got.addr, raw)
            got.host = raw
            entry, _ = dev.mmu.resolve(got.addr)
            ref = (entry.arena, entry.physical)
            dev.constants.add(ref)
            dev.tables[key] = got
        except BaseException:
            dev.tables.pop(key, None)
            if ref is not None:
                dev.constants.discard(ref)
            got.free()
            raise
    return got.addr


def read(dev, s, dtype, n: int) -> bytes:
    """`s`'s contents as `n` `dtype` values, from whichever form is current:
    row-major bytes, the f16 form, or the padded-rows form."""
    st, off = base_of(s)
    if DEFER in st.valid:
        st.forms[DEFER].make()
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
    "demands",
    "f16",
    "f16_rows",
    "for_port",
    "launch",
    "library_hints",
    "materialize_view",
    "mm_site",
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
    "updated_site",
    "view_key",
    "viewed",
]
