"""Parameterized eager schedules owned by the KTPU seam, never by tinygrad."""

import os

from tinygrad.engine import realize
from tinygrad.uop import Ops
from tinygrad.uop.ops import UOp

from kohakutpu.language.dsl.kernel import _MUTANT

KEPT = 128
ENV = "KTPU_EAGER_SCHEDULE"
ENABLED = False


def refresh():
    global ENABLED
    ENABLED = os.environ.get(ENV, "0") == "1"
    return ENABLED


refresh()


class Schedule:
    """Kernel identities and buffer positions; no live allocation is retained."""

    def __init__(self, sink, linear):
        indices = {b: i for i, b in enumerate(sink.src[1:])}
        self.temporary = []
        calls = []
        for call in linear.src:
            positions = []
            for b in call.src[1:]:
                if b not in indices:
                    indices[b] = len(indices)
                    self.temporary.append((b.device, b.max_numel(), b.dtype))
                positions.append(indices[b])
            calls.append((call.replace(src=()), call.src[0], tuple(positions)))
        self.calls = tuple(calls)

    def instantiate(self, sink):
        buffers = list(sink.src[1:])
        buffers.extend(UOp.new_buffer(*spec) for spec in self.temporary)
        return (
            UOp(
                Ops.LINEAR,
                src=tuple(
                    head.replace(src=(ast, *(buffers[i] for i in positions)))
                    for head, ast, positions in self.calls
                ),
            ),
            {},
        )


class Schedules:
    def __init__(self):
        self.entries = {}
        self.context = None
        self.hits = self.misses = 0

    def clear(self):
        self.entries.clear()
        self.context = None

    def run(self, sink, original, context, compile_schedule=None):
        if _MUTANT.get() == "app.schedule/ignores_context":
            context = None
        if self.context != context:
            self.clear()
            self.context = context
        key = (sink.src[0], tuple((b.shape, b.dtype, b.device) for b in sink.src[1:]))
        if _MUTANT.get() == "app.schedule/ignores_layout":
            key = None
        if (recipe := self.entries.get(key)) is not None:
            self.hits += 1
            return recipe.instantiate(sink)
        self.misses += 1
        linear, values = original(sink)
        if (
            not values
            and all(
                c.op is Ops.CALL and all(b.op is Ops.BUFFER for b in c.src[1:])
                for c in linear.src
            )
            and all(
                b in sink.src[1:] or not b.buffer.is_allocated()
                for c in linear.src
                for b in c.src[1:]
            )
        ):
            # tinygrad's post-cache rewrite creates the remaining unallocated buffers.
            # Initialized non-arguments are external state, never fresh temporaries.
            if len(self.entries) == KEPT:
                del self.entries[next(iter(self.entries))]
            if compile_schedule is not None:
                linear = compile_schedule(linear)
            self.entries[key] = Schedule(sink, linear)
        return linear, values


def enabled(sink):
    return (
        ENABLED
        and not realize.capturing
        and sink.op is Ops.CALL
        and bool(sink.src[1:])
        and all(b.op is Ops.BUFFER and b.device == "KTPU" for b in sink.src[1:])
        and len(set(sink.src[1:])) == len(sink.src) - 1
    )
