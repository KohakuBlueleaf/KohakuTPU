"""Cold eager planning records an address-parametric launch program."""

import os
from collections import Counter

from kohakutpu.application.ktpugrad import run
from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime import launch_boundary, native_launch
from kohakutpu.application.runtime.storage import Form, StorageView
from kohakutpu.compiler import _rs
from kohakutpu.language.dsl.kernel import _MUTANT

ENV = "KTPU_EAGER_RECIPE"
KEPT = 512
HOST_BYTES = 65536


def host_enabled(buffers, enabled=False):
    return (
        enabled
        and sum(len(st.host) for st in buffers.values() if st.host is not None)
        <= HOST_BYTES
    )


def host_key(buffers):
    if _MUTANT.get() == "app.recipe/no_host_identity":
        return None
    return tuple(
        id(st.host) if st.host is not None else None for st in buffers.values()
    )


def signature(plan, buffers):
    bases = [FM.base_of(b) for b in buffers.values()]
    aliases = tuple(
        next(j for j, (s, _) in enumerate(bases) if s is st) for st, _ in bases
    )
    spans = [(f.addr, f.nbytes) for st, _ in bases for f in st.forms.values()]
    if _MUTANT.get() == "app.recipe/no_overlap_key":
        spans = []
    overlap = tuple(
        (i, j, b - a)
        for i, (a, n) in enumerate(spans)
        for j, (b, m) in enumerate(spans[:i])
        if a < b + m and b < a + n
    )
    return (
        plan.ast,
        tuple(
            (
                slot,
                buf.nbytes,
                off,
                st.dtype,
                st.owned,
                st.host is not None,
                st.host_f16_dtype,
                tuple((k, f.nbytes, f.owned) for k, f in st.forms.items()),
                frozenset(st.valid),
                frozenset(st.zeroed),
                st.site,
            )
            for (slot, buf), (st, off) in zip(buffers.items(), bases, strict=True)
        ),
        aliases,
        overlap,
    )


def state(st):
    return (
        st.addr,
        dict(st.forms),
        st.valid.copy(),
        st.zeroed.copy(),
        st.dtype,
        st.site,
        st.host,
        st.host_f16_dtype,
    )


class Recording:
    def __init__(self, dev, buffers, native=False, host_recipe=False):
        self.dev = dev
        self.storages = tuple(FM.base_of(b)[0] for b in buffers.values())
        self.before = tuple(state(st) for st in self.storages)
        self.host_effects = host_enabled(buffers, host_recipe)
        self.host_inputs = (
            tuple(st.host for st in self.storages) if self.host_effects else ()
        )
        self.host_bytes = {
            id(value): value for value in self.host_inputs if value is not None
        }
        if self.host_effects and any(
            type(value) is not bytes for value in self.host_bytes.values()
        ):
            raise TypeError("host recipe inputs must be immutable bytes")
        self.addresses = []
        for i, st in enumerate(self.storages):
            for key, form in st.forms.items():
                self.addresses.append((form.addr, form.nbytes, ("form", i, key, 0)))
        self.operations = []
        self.depth = 0
        self.next = 0
        self.unsupported = None
        self.pins = []
        self.external = {}
        self.native = native
        self.program = None
        if self.native and not isinstance(dev.mmu, _rs.NativeMMU):
            raise ValueError("KTPU_NATIVE_PROGRAM requires KTPU_NATIVE_MEMORY=1")

    def ref(self, address):
        for base, size, ref in reversed(self.addresses):
            if base <= address < base + size:
                return (*ref[:3], ref[3] + address - base)
        entry, _ = self.dev.mmu.resolve(address)
        self.external[entry.virtual] = entry
        return ("fixed", entry.virtual, None, address - entry.virtual)

    def on_alloc(self, address, nbytes, space, node, owner):
        if not self.depth:
            index = self.next
            self.next += 1
            self.operations.append(("alloc", index, nbytes, space, node, owner))
            self.addresses.append(
                (address, max(self.dev.line_bytes, nbytes), ("temp", index, None, 0))
            )

    def on_free(self, address):
        if not self.depth:
            ref = self.ref(address)
            if ref[0] == "fixed":
                self.unsupported = "external free"
            self.operations.append(("free", ref))

    def on_write(self, address, data):
        if not self.depth:
            if self.host_effects:
                raw = bytes(data)
                self.host_bytes[id(raw)] = raw
                self.operations.append(("write", self.ref(address), raw))
            else:
                self.unsupported = "host write"

    def on_read(self):
        if not self.depth:
            self.unsupported = "host read"

    def on_submit(self, submit_fn, compiled, bindings, kind, info):
        self.operations.append(("effects", self.effects_of()))
        refs = {k: self.ref(v) for k, v in bindings.items()}
        at = len(self.operations)
        self.operations.append(("run", compiled, refs, kind, info))

        def observed(compiled, full, progs, submitted):
            if not self.native:
                return
            if compiled.bound is not None:
                self.unsupported = "bound compilation"
                return
            native = native_launch.descriptor(
                self.dev, compiled, full, progs, submitted, bindings
            )
            self.operations[at] = ("native", native, tuple(refs.values()))

        prev_capture = self.dev._recipe_capture
        self.dev._recipe_capture = observed
        self.depth += 1
        try:
            return submit_fn(compiled, bindings, kind, info)
        finally:
            self.depth -= 1
            self.dev._recipe_capture = prev_capture

    def capture(self, plan, buffers):
        prev = self.dev._recipe_recording
        self.dev._recipe_recording = self
        try:
            runs = run.execute(self.dev, plan, buffers)
        finally:
            self.dev._recipe_recording = prev
        self.effects = self.effects_of()
        if sum(map(len, self.host_bytes.values())) > HOST_BYTES:
            self.unsupported = "host byte budget"
        return runs

    def effects_of(self):
        effects = []
        for i, (st, before) in enumerate(zip(self.storages, self.before, strict=True)):
            forms = tuple(
                (key, self.ref(f.addr), f.nbytes, f.owned)
                for key, f in st.forms.items()
            )
            if any(f.src_st is not None for f in st.forms.values()):
                self.unsupported = "shared form"
            host = None
            if st.host is not None:
                host = next(
                    (j for j, b in enumerate(self.before) if b[6] is st.host), None
                )
                if host is None:
                    if self.host_effects and type(st.host) is bytes:
                        host = st.host
                        self.host_bytes[id(host)] = st.host
                    else:
                        self.unsupported = "computed host value"
            effects.append(
                (
                    i,
                    self.ref(st.addr),
                    forms,
                    tuple(st.valid),
                    tuple(st.zeroed),
                    st.dtype,
                    st.site,
                    host,
                    st.host_f16_dtype,
                )
            )
        return effects

    def close(self):
        for arena, at in self.pins:
            arena.unretain(at)
        self.pins.clear()

    def launch(self, dev, buffers):
        if self.program is not None:
            launch_boundary.before_submit(dev)
            return self.program.launch(dev, tuple(buffers.values()))
        storages = tuple(FM.base_of(b)[0] for b in buffers.values())
        # Entry addresses precede output replacement, including resized flat forms.
        forms = tuple({k: f.addr for k, f in st.forms.items()} for st in storages)
        hosts = tuple(st.host for st in storages)
        temporary = {}

        def address(ref):
            kind, index, key, offset = ref
            return (
                forms[index][key]
                if kind == "form"
                else temporary[index] if kind == "temp" else index
            ) + offset

        def apply(effects):
            for (
                i,
                addr,
                descriptors,
                valid,
                zeroed,
                dtype,
                site,
                host,
                host_dtype,
            ) in effects:
                st = storages[i]
                st.addr = address(addr)
                st.forms = {
                    key: Form(key, address(ref), size, owned)
                    for key, ref, size, owned in descriptors
                }
                st.valid, st.zeroed = set(valid), set(zeroed)
                st.dtype, st.site = dtype, site
                st.host = (
                    host
                    if isinstance(host, bytes)
                    else None if host is None else hosts[host]
                )
                st.host_f16_dtype = host_dtype

        runs, owned = [], set()
        try:
            for op in self.operations:
                if op[0] == "alloc":
                    _, i, size, space, node, owner = op
                    temporary[i] = dev.alloc(size, space, node, owner)
                    owned.add(temporary[i])
                elif op[0] == "effects":
                    apply(op[1])
                elif op[0] == "write":
                    dev.write(address(op[1]), op[2])
                elif op[0] == "free":
                    at = address(op[1])
                    dev.free(at)
                    owned.discard(at)
                else:
                    _, compiled, bindings, kind, info = op
                    runs.append(
                        dev.run(
                            compiled,
                            {k: address(v) for k, v in bindings.items()},
                            kind,
                            info,
                        )
                    )
            apply(self.effects)
        except BaseException:
            adopted = {st.addr for st in storages if st.owned} | {
                form.addr for st in storages for form in st.forms.values() if form.owned
            }
            for at in owned - adopted:
                dev.free(at)
            raise
        return runs


class Recipes:
    def __init__(self):
        self.entries = {}
        self.counts = Counter()
        self.native_program = os.environ.get("KTPU_NATIVE_PROGRAM", "0") == "1"
        self.host_recipe = os.environ.get("KTPU_HOST_RECIPE", "0") == "1"
        self.native = _rs.RecipeTable(KEPT) if self.native_program else None

    def clear(self):
        if self.native is not None:
            self.native.clear()
        for entry in self.entries.values():
            entry.close()
        self.entries.clear()

    def execute(self, dev, plan, buffers):
        if any(isinstance(b, StorageView) for b in buffers.values()):
            self.counts["views"] += 1
            return run.execute(dev, plan, buffers)
        hints = FM.hints(dev)
        revision = hints.revision if FM.LAYOUT_HINTS else 0
        context = (
            revision,
            dev.resident_mode,
            host_key(buffers) if host_enabled(buffers, self.host_recipe) else None,
        )
        if self.native is not None:
            launch_boundary.before_submit(dev)
            key = self.native.launch(dev, plan.ast, buffers, context)
            if not isinstance(key, _rs.RecipeMiss):
                self.counts["hit"] += 1
                return key
        else:
            key = (signature(plan, buffers), context)
            if (entry := self.entries.get(key)) is not None:
                self.counts["hit"] += 1
                return entry.launch(dev, buffers)
        self.counts["miss"] += 1
        entry = Recording(
            dev, buffers, native=self.native_program, host_recipe=self.host_recipe
        )
        try:
            runs = entry.capture(plan, buffers)
        except BaseException:
            entry.close()
            raise
        if FM.LAYOUT_HINTS and hints.revision != revision:
            entry.unsupported = "hint change"
        if entry.unsupported:
            self.counts[entry.unsupported] += 1
            entry.close()
        else:
            # A recipe owns descriptors and constant pins, not the cold call's tensors.
            entry.before = entry.storages = ()
            entry.pins = dev._retain(entry.external)
            if entry.native:
                entry.program = _rs.LaunchProgram(
                    entry.operations,
                    entry.effects,
                    tuple(buffers).index(plan.out_slot),
                    (dev.recipe_epoch, dev.recipe_epoch.generation),
                )
            entry.dev = None
            if self.native is not None:
                self.native.insert(key, entry)
            else:
                if len(self.entries) == KEPT:
                    self.entries.pop(next(iter(self.entries))).close()
                self.entries[key] = entry
        return runs


def execute(dev, plan, buffers):
    if (
        not dev.eager_recipe_enabled
        or run._active_scope(dev) is not None
        or dev.recording
        or dev._placement is not None
    ):
        return run.execute(dev, plan, buffers)
    if dev.eager_recipes is None:
        dev.eager_recipes = Recipes()
    return dev.eager_recipes.execute(dev, plan, buffers)
