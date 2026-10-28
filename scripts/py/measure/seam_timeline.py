"""Nested host phases and every logical transport call aligned to submissions.

Used by hseam.py --timeline; timestamps are host nanoseconds, not card cycles.
"""

import gc
import time
from contextlib import ExitStack
from functools import wraps
from unittest.mock import patch

from kohakutpu.application.ktpugrad.device import KTPUAllocator, KTPUGraph, KTPUProgram


class HostTimeline:
    def __init__(self, dev):
        self.dev = dev
        self.events = []
        self.stack = ExitStack()
        self.active = False
        self.step = -1
        self.launch = 0
        self.phases = []
        self.gc_start = None

    def start(self, step):
        self.step, self.launch = step, 0
        self.active = True
        self.event("step", time.perf_counter_ns())

    def event(self, phase, start, **fields):
        if self.active:
            self.events.append(
                dict(
                    phase=phase,
                    step=self.step,
                    launch=self.launch,
                    start_ns=start,
                    end_ns=time.perf_counter_ns(),
                    parents=list(self.phases),
                    **fields,
                )
            )

    def wrap(self, target, name, phase=None):
        original = getattr(target, name)
        label = phase or name

        @wraps(original)
        def call(*args, **kwargs):
            start = time.perf_counter_ns()
            self.phases.append(label)
            try:
                return original(*args, **kwargs)
            finally:
                self.phases.pop()
                self.event(label, start)

        self.stack.enter_context(patch.object(target, name, call))

    def gc_event(self, phase, info):
        if phase == "start":
            self.gc_start = time.perf_counter_ns()
        elif self.gc_start is not None:
            self.event("gc", self.gc_start, **info)
            self.gc_start = None

    def __enter__(self):
        gc.callbacks.append(self.gc_event)
        self.stack.callback(gc.callbacks.remove, self.gc_event)
        for target, names in (
            (KTPUGraph, ("__call__", "_fill", "_plan", "_storages")),
            (KTPUAllocator, ("_copyin", "_copyout")),
            (KTPUProgram, ("__call__",)),
            (
                self.dev,
                (
                    "replay",
                    "prepare_recorded",
                    "_admit",
                    "_collect_one",
                    "read",
                    "write",
                    "wait_address",
                ),
            ),
        ):
            for name in names:
                self.wrap(
                    target, name, f"{getattr(target, '__name__', 'runtime')}.{name}"
                )
        card = self.dev.card
        if hasattr(card, "_submit"):
            original_submit = card._submit

            def submit(*args, **kwargs):
                start = time.perf_counter_ns()
                self.event("submit_begin", start)
                try:
                    return original_submit(*args, **kwargs)
                finally:
                    self.event("submit_end", start)
                    if self.active:
                        self.launch += 1

            self.stack.enter_context(patch.object(card, "_submit", submit))
        mem = card.mem
        for name, write in (("read_block", False), ("write_block", True)):
            original = getattr(mem, name)

            def transfer(addr, data, original=original, write=write):
                start = time.perf_counter_ns()
                size = len(data) if write else data
                purpose = mem.purpose(addr, size, write)
                try:
                    return original(addr, data)
                finally:
                    self.event(
                        "transport",
                        start,
                        purpose=purpose,
                        write=write,
                        nbytes=size,
                        address=addr,
                    )

            self.stack.enter_context(patch.object(mem, name, transfer))
        for q in getattr(card, "queues", {}).values():
            for name in ("flush", "poll", "wait"):
                self.wrap(q, name, f"queue.{name}")
        return self

    def __exit__(self, *exc):
        self.active = False
        return self.stack.__exit__(*exc)
