"""FIFO lists for an already-observed eager sequence; barriers flush before I/O."""

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from kohakutpu.application.runtime import packages as B


@dataclass
class Deferred:
    owner: "Batch"
    group: object = None
    first: bool = False
    result: dict | None = None
    error: BaseException | None = None
    #: the list this run went out in, and its place among the sequence's lists
    execution: Any = None
    ordinal: int = 0


class Batch:
    """Runs of one recognised sequence, published as one list per host
    barrier. `durations` keeps each list's last run time under (`key`, its
    place in the sequence), so the next step's lists are waited for at their
    predicted end."""

    def __init__(self, card, key=None, durations: dict | None = None):
        self.card = card
        self.pending = []
        self._submitting = False
        self.key = key
        self.durations = {} if durations is None else durations
        self.lists = 0

    def submit(self, raw, debug):
        self._submitting = True
        try:
            return self.card.submit_wire(raw, debug)
        finally:
            self._submitting = False

    def collect(self, pending, *args, **kwargs):
        return self.card.collect(pending, *args, **kwargs)

    def wire(self, raw, debug):
        """Native bytes (`submit_wire`): their plain packages made on demand."""
        plain = {
            n: B.Package(
                lambda r=recipe, h=handles, t=translations: r.plain(h, t),
                self.card.machine_json,
                n,
            )
            for n, (recipe, handles, translations) in debug.items()
        }
        return self.add(raw, plain, lambda: self.submit(raw, debug))

    def add(self, raw, plain, alone):
        """One run on one node joins the list; anything else goes out by
        `alone()` after what is pending."""
        if len(raw) != 1 or (self.pending and self.pending[0][1].keys() != raw.keys()):
            self.flush()
        if len(raw) != 1:
            return alone()
        pending = Deferred(self)
        self.pending.append((pending, raw, plain, alone))
        return pending

    def flush(self):
        if not self.pending:
            return
        batch, self.pending = self.pending, []
        try:
            self.publish(batch)
        except BaseException as error:
            for pending, *_ in batch:
                pending.error = error
            raise

    def publish(self, batch):
        if len(batch) == 1 or not self.card.queues:
            for pending, _, _, alone in batch:
                pending.group = alone()
                pending.first = True
            return
        node = next(iter(batch[0][1]))
        queue = self.card.queues[node]
        uploads, packages, items = [], [], []
        try:
            for index, (_, raw, plain, _) in enumerate(batch):
                data, bindings = raw[node]
                upload = queue.prepare(data)
                uploads.append(upload)
                items.append((upload, bindings, 0, 0, index))
                packages.append((plain[node], ()))
            execution = queue.prepare_list(items)
        finally:
            for upload in uploads:
                upload.close()
        ordinal, self.lists = self.lists, self.lists + 1
        execution.seconds = self.durations.get((self.key, ordinal), 0.0)
        try:
            actual = self.card.submit_execution({node: (execution, tuple(packages))})
            for index, (pending, *_) in enumerate(batch):
                pending.group, pending.first = actual, index == 0
                pending.execution, pending.ordinal = execution, ordinal
        finally:
            execution.close()

    def result(self, pending, *args, **kwargs) -> dict:
        if not isinstance(pending, Deferred):
            return self.collect(pending, *args, **kwargs)
        if pending.error is not None:
            raise pending.error
        self.flush()
        if pending.result is None:
            result = self.collect(pending.group, *args, **kwargs)
            pending.result = result if pending.first else {**result, "cycles": 0}
            if pending.first and pending.execution is not None:
                key = (self.key, pending.ordinal)
                self.durations[key] = pending.execution.seconds
        return pending.result


@contextmanager
def batching(card, key=None, durations: dict | None = None):
    batch = Batch(card, key, durations)
    prev_batch = card._eager_batch
    prev_suspended = card.native_direct_suspended
    card._eager_batch = batch
    card.native_direct_suspended = True
    try:
        yield
    except BaseException as error:
        for pending, *_ in batch.pending:
            pending.error = error
        batch.pending.clear()
        raise
    else:
        batch.flush()
    finally:
        card._eager_batch = prev_batch
        card.native_direct_suspended = prev_suspended
