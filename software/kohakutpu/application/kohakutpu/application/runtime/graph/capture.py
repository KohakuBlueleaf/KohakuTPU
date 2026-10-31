"""Semantic eager capture and neighbour fusion over relocatable kernel classes.

Templates own code generation and binding. Capture owns liveness and composition
keys, never instruction generation. Bindings name canonical allocation identities;
views of one allocation use the same identity and byte offsets.
"""

from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from itertools import accumulate
from time import perf_counter


@dataclass(frozen=True)
class Span:
    allocation: Hashable
    offset: int
    nbytes: int

    def overlaps(self, other):
        return (
            self.allocation == other.allocation
            and self.offset < other.offset + other.nbytes
            and other.offset < self.offset + self.nbytes
        )


@dataclass(frozen=True)
class Template:
    """A single-output, pure kernel class; key includes machine and numeric policy.

    The binder takes dynamic parameters and input/output addresses, returning one
    backend launch. Shapes, strides and scalar values are parameters, not keys,
    only where the native builder certifies that parameter domain.
    """

    key: tuple
    family: str
    bind: Callable


@dataclass(frozen=True)
class Launch:
    template: Template
    parameters: object
    inputs: tuple[Span, ...]
    output: Span

    def bind(self, addresses):
        return self.template.bind(
            self.parameters,
            tuple(
                addresses[s.allocation] + s.offset for s in (*self.inputs, self.output)
            ),
        )


@dataclass(frozen=True)
class Rules:
    elementwise: bool = True
    quant: bool = True
    layout: bool = True
    norm: bool = True


DEFAULT_RULES = Rules()


@dataclass(frozen=True)
class Composer:
    """Registered native builder and its dynamic-domain compatibility predicate."""

    key: tuple
    accepts: Callable
    build: Callable


class Library:
    """One composed template per exact class/wiring key, shared across captures."""

    def __init__(self):
        self.composers = {}
        self.templates = {}
        self.builds = 0
        self.hits = 0
        self.build_seconds = 0.0

    def register(self, rule, producer_family, consumer_family, composer):
        self.composers[rule, producer_family, consumer_family] = composer

    def compose(self, rule, left, right, links):
        composer = self.composers.get(
            (rule, left.template.family, right.template.family)
        )
        if composer is None or not composer.accepts(left, right, links):
            return None
        key = (
            "neighbour.v1",
            composer.key,
            rule,
            left.template.key,
            right.template.key,
            len(left.inputs),
            len(right.inputs),
            links,
        )
        template = self.templates.get(key)
        if template is None:
            start = perf_counter()
            template = composer.build(key, left.template, right.template, links)
            self.build_seconds += perf_counter() - start
            self.templates[key] = template
            self.builds += 1
        else:
            self.hits += 1
        return Launch(
            template,
            (left.parameters, right.parameters),
            left.inputs
            + tuple(s for i, s in enumerate(right.inputs) if i not in links),
            right.output,
        )


def candidates(left, right, rules):
    """Priority preserves layout/norm/quant semantics before generic epilogues."""
    a, b = left.template.family, right.template.family
    if rules.layout and a == "layout":
        yield "layout"
    if rules.norm and a == "norm" and b == "linear":
        yield "norm"
    if rules.quant and (a == "quant" or b == "quant"):
        yield "quant"
    if rules.elementwise and b == "elementwise":
        yield "elementwise"


@dataclass(frozen=True)
class Graph:
    launches: tuple[Launch, ...]
    before: int
    fused: tuple[str, ...]
    capture_seconds: float

    def prepare(self, runner, addresses):
        """Bind once and hand one ordered graph to an OS runner adapter.

        The adapter owns package residency, allocation pins and completion. A
        replay calls its prepared object's submit once, not once per micro-op.
        """
        packets = tuple(launch.bind(addresses) for launch in self.launches)
        dependencies = tuple(() if i == 0 else (i - 1,) for i in range(len(packets)))
        return runner.prepare(packets, dependencies)


class Capture:
    """Record semantic launches while executing them eagerly through a submitter.

    The caller keeps captured allocations live and exports every observable
    output (including tensors saved for backward). Host effects split captures.
    """

    def __init__(self):
        self.launches = []

    @classmethod
    def from_recorded(cls, recorded, describe):
        """Adapt Device/KTPUGraph capture using the native library's port contract.

        Host writes/reads are boundaries, not kernels. Split such recordings at
        those effects before describing a pure neighbour-fusion region.
        """
        if any(event[0] != "run" for event in recorded):
            raise ValueError("semantic capture requires a run-only recording")
        if len(recorded.templates) != len(recorded):
            raise ValueError("semantic capture requires one template per run")
        result = cls()
        result.launches = [
            describe(kernel, bindings) for kernel, bindings in recorded.templates
        ]
        return result

    def launch(self, template, parameters, inputs, output, addresses, submit):
        launch = Launch(template, parameters, tuple(inputs), output)
        result = submit(launch.bind(addresses))
        self.launches.append(launch)
        return result

    def finish(self, exports, library, rules=DEFAULT_RULES):
        start = perf_counter()
        # Index all aliases, including writes; external observations keep stores live.
        uses = defaultdict(list)
        for i, launch in enumerate(self.launches):
            for span in (*launch.inputs, launch.output):
                uses[span.allocation].append((i, span))
        for span in exports:
            uses[span.allocation].append((-1, span))
        indexed = {}
        for allocation, entries in uses.items():
            entries.sort(key=lambda entry: entry[1].offset)
            starts = [span.offset for _, span in entries]
            ends = list(
                accumulate((span.offset + span.nbytes for _, span in entries), max)
            )
            indexed[allocation] = (entries, starts, ends)
        groups = []
        fused = []
        for i, right in enumerate(self.launches):
            if groups:
                left, members = groups[-1]
                links = tuple(
                    j for j, span in enumerate(right.inputs) if span == left.output
                )
                entries, starts, ends = indexed[left.output.allocation]
                lo = bisect_right(ends, left.output.offset)
                hi = bisect_left(starts, left.output.offset + left.output.nbytes)
                exclusive = links and all(
                    entries[j][0] in (members[-1], i)
                    for j in range(lo, hi)
                    if entries[j][1].overlaps(left.output)
                )
                aliases = any(left.output.overlaps(s) for s in left.inputs) or any(
                    right.output.overlaps(s) for s in (*left.inputs, *right.inputs)
                )
                # Partial aliases in the consumer must not read the removed store.
                exact = all(
                    not left.output.overlaps(s) or j in links
                    for j, s in enumerate(right.inputs)
                )
                if exclusive and exact and not aliases:
                    for rule in candidates(left, right, rules):
                        merged = library.compose(rule, left, right, links)
                        if merged is not None:
                            groups[-1] = (merged, (*members, i))
                            fused.append(rule)
                            break
                    else:
                        groups.append((right, (i,)))
                    continue
            groups.append((right, (i,)))
        return Graph(
            tuple(launch for launch, _ in groups),
            len(self.launches),
            tuple(fused),
            perf_counter() - start,
        )
