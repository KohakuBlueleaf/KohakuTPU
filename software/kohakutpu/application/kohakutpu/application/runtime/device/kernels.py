"""Compiling a kernel for this device, and the compiled-kernel cache."""

import json
from dataclasses import replace

from kohakutpu.application.runtime import generated
from kohakutpu.application.runtime.class_library import ClassLibrary
from kohakutpu.application.runtime.device.common import KERNELS_KEPT, Parts
from kohakutpu.application.runtime.graph.storage import stage_graph
from kohakutpu.language.config import ENV
from kohakutpu.language.dsl.kernel import _const_key
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs


class Kernels(Parts):
    def kernel(self, source, graph=None, traced=None):
        """The `Compiled` of graph `source`, kept by its text: a launch seen
        before reuses it (its bound programs included), so a repeated eager
        step reads no library entry and builds nothing. A library entry's
        reuse still counts as a library hit (class or exact)."""
        if ENV.get("KTPU_LOCAL_SCRATCH", "0") == "1":
            if self.nodes != 1:
                raise ValueError(
                    "local scratch placement requires a single-node device"
                )
            original = json.loads(source) if graph is None else graph
            # Queue/package storage shares staging; reserve a configurable fraction.
            capacity = min(a.hi - a.lo for a in self.memory.staging.values())
            budget = int(ENV.get("KTPU_LOCAL_SCRATCH_BYTES", capacity // 2))
            graph = stage_graph(
                original, rs.machine_json(self.machine), min(budget, capacity)
            )
            if graph is not original:
                source = json.dumps(graph)
                traced = None
        mutant = rs._MUTANT.get()
        key = self._kernel_key(source, mutant, self.classes)
        got = self.kernels.get(key)
        if got is not None:
            self.kernels.move_to_end(key)
            compiled, hit_kind = got
            self.counters["kernel_reuses"] += 1
            if hit_kind == "class":
                self.counters["class_hits"] += 1
                self.counters["library_hits"] += 1
            elif hit_kind == "exact":
                self.counters["exact_hits"] += 1
                self.counters["library_hits"] += 1
            elif hit_kind == "library":
                self.counters["library_hits"] += 1
            return compiled
        c0 = self.counters["class_hits"]
        e0 = self.counters["exact_hits"]
        l0 = self.counters["library_hits"]
        output = self.compile_graph(source)
        if self.counters["class_hits"] > c0:
            hit_kind = "class"
        elif self.counters["exact_hits"] > e0:
            hit_kind = "exact"
        elif self.counters["library_hits"] > l0:
            hit_kind = "library"
        else:
            hit_kind = None
        compiled = generated.from_output(
            self,
            json.loads(source) if graph is None else graph,
            output,
            traced,
        )
        self.counters["f001_split"] += compiled.f001_split
        self.kernels[key] = (compiled, hit_kind)
        while len(self.kernels) > KERNELS_KEPT:
            self.kernels.popitem(last=False)
        return compiled

    def _kernel_key(self, source, mutant, classes):
        return (
            source,
            mutant,
            classes,
            rs.machine_json(self.machine),
            self.library,
            self.library_only,
            rs._ZERO_SIGN.get(),
        )

    def _invalidate_eager(self):
        if rs._MUTANT.get() == "app.device/keeps_eager":
            return
        self.recipe_epoch.invalidate()
        if self.eager_recipes is not None:
            self.eager_recipes.clear()
        if self.eager_schedules is not None:
            self.eager_schedules.clear()
        if self.eager_calls is not None:
            self.eager_calls.clear()
        if self.eager_sequences is not None:
            self.eager_sequences.clear()

    def invalidate_library(self):
        """After replacing entries in the current library, discard compiled views."""
        self._invalidate_eager()
        self.kernels.clear()
        self.specialisations.clear()

    def compile_graph(self, source):
        """Look for a certified class launch before the exact-graph fallback."""
        if hasattr(self.card, "compile_graph"):
            return self.card.compile_graph(source)
        machine = rs.machine_json(self.machine)
        if self.library is not None:
            if self.classes:
                output = ClassLibrary(self.library, machine).lookup(source)
                if output is not None:
                    self.counters["class_hits"] += 1
                    self.counters["library_hits"] += 1
                    return output
            output = rs.library_get(self.library, source, machine, rs._MUTANT.get())
            if output is not None:
                self.counters["exact_hits"] += 1
                self.counters["library_hits"] += 1
                return output
            self.counters["library_misses"] += 1
            key = json.loads(rs.library_key(source, machine))
            if self.library_only:
                raise RuntimeError(
                    f"library miss: class {key['class']}, machine {key['machine']}"
                )
        with rs.compiler_owner(self):
            return rs.compile(source, json.loads(source)["name"], self.machine)

    def specialise(self, kernel, given, **consts):
        cached = self.specialise_cache
        template_call = getattr(kernel, "template_call", None)
        grant = (self.home,) if self.home is not None else tuple(self.card.nodes)
        key = (
            grant,
            (
                generated.split_option(template_call.generator)
                if template_call is not None
                else None
            ),
            (
                tuple(generated.class_schedule().items())
                if template_call is not None
                and "shape_class" in template_call.arguments
                else ()
            ),
            kernel,
            tuple(sorted(given.items())),
            tuple(
                (k, _const_key(v)) for k, v in sorted((kernel.consts | consts).items())
            ),
            self._kernel_key(None, rs._MUTANT.get(), self.classes),
        )
        if cached and key in self.specialisations:
            self.specialisations.move_to_end(key)
            compiled, hit_kind = self.specialisations[key]
            self.counters["kernel_reuses"] += 1
            if hit_kind is not None:
                self.counters["library_hits"] += 1
                self.counters[f"{hit_kind}_hits"] += 1
            return compiled
        before = (self.counters["class_hits"], self.counters["exact_hits"])
        if template_call is not None:
            kernel.specs(given, kernel.constants(consts))
            source, selection = generated.invocation(
                self, template_call.generator, template_call.arguments
            )
            compiled = replace(self.kernel(source), selection=selection)
        elif self.library is None and not hasattr(self.card, "compile_graph"):
            with rs.compiler_owner(self):
                compiled = K.specialise(kernel, given, machine=self.machine, **consts)
        else:
            # The trace compiles its own graph inside its scope.
            made = []
            kernel.trace(
                given,
                machine=K.dsl_machine(self.machine),
                check=lambda t: made.append(self.kernel(t.graph_json, t.graph, t)),
                **consts,
            )
            (compiled,) = made
        if cached:
            hit_kind = (
                "class"
                if self.counters["class_hits"] > before[0]
                else "exact" if self.counters["exact_hits"] > before[1] else None
            )
            self.specialisations[key] = (compiled, hit_kind)
            while len(self.specialisations) > KERNELS_KEPT:
                self.specialisations.popitem(last=False)
        return compiled
