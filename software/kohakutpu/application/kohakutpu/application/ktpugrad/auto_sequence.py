"""Recognize complete eager sequences without speculating past a realization."""

import os
from collections import Counter
from contextlib import nullcontext

from tinygrad.engine import realize
from tinygrad.uop import Ops

from kohakutpu.application.ktpugrad.plans import PLANS
from kohakutpu.application.runtime.eager_batch import batching
from kohakutpu.language.dsl.kernel import _MUTANT

KEPT = 128


class Sequences:
    def __init__(self):
        self.entries = {}
        self.counts = Counter()
        self.previous = None

    def clear(self):
        self.entries.clear()
        self.previous = None

    def observe(self, key):
        if _MUTANT.get() == "app.sequence/ignores_layout":
            key = tuple(ast for ast, _ in key)
        hit = key in self.entries
        self.counts["hit" if hit else "miss"] += 1
        if not hit:
            if len(self.entries) == KEPT:
                self.entries.pop(next(iter(self.entries)))
            self.entries[key] = None
        if self.previous is not None and key != self.previous:
            self.counts["divergence"] += 1
        self.previous = key
        return hit


def scope(rt, linear, inputs):
    enabled = (
        os.environ.get("KTPU_AUTO_SEQUENCE", "0") == "1"
        if _MUTANT.get() == "app.sequence/unlatched"
        else rt.auto_sequence
    )
    if not enabled or realize.capturing or rt.recording:
        return nullcontext()
    calls, bases = [], {}
    for call in linear.src:
        if (
            call.op is not Ops.CALL
            or call.src[0].op is not Ops.PROGRAM
            or call.src[0].src[3].arg not in PLANS
        ):
            return nullcontext()
        plan = PLANS[call.src[0].src[3].arg]
        desc = []
        for uop in realize.resolve_params(call, inputs):
            buffer = uop.buffer
            desc.append(
                (
                    bases.setdefault(buffer.base, len(bases)),
                    buffer.size,
                    buffer.dtype,
                    buffer.offset,
                    buffer.options,
                )
            )
        calls.append((plan.ast, tuple(desc)))
    if rt.eager_sequences is None:
        rt.eager_sequences = Sequences()
    matched = rt.eager_sequences.observe(tuple(calls))
    if not matched or not rt.card.queues:
        return nullcontext()
    return batching(rt.card)
