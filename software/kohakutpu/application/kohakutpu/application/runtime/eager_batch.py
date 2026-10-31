"""FIFO lists for an already-observed eager sequence; barriers flush before I/O."""

from contextlib import contextmanager
from dataclasses import dataclass

from kohakutpu.application.runtime import packages as B


@dataclass
class Deferred:
    owner: object
    group: object = None
    first: bool = False
    result: object = None
    error: BaseException | None = None


class Batch:
    def __init__(self, card):
        self.card = card
        self.pending = []
        self._submitting = False

    def submit(self, raw, debug):
        self._submitting = True
        try:
            return self.card.submit_wire(raw, debug)
        finally:
            self._submitting = False

    def collect(self, pending, *args, **kwargs):
        return self.card.collect(pending, *args, **kwargs)

    def wire(self, raw, debug):
        if len(raw) != 1 or (self.pending and self.pending[0][1].keys() != raw.keys()):
            self.flush()
        if len(raw) != 1:
            return self.submit(raw, debug)
        pending = Deferred(self)
        self.pending.append((pending, raw, debug))
        return pending

    def flush(self):
        if not self.pending:
            return
        batch, self.pending = self.pending, []
        try:
            self.publish(batch)
        except BaseException as error:
            for pending, _, _ in batch:
                pending.error = error
            raise

    def publish(self, batch):
        if len(batch) == 1 or not self.card.queues:
            for pending, raw, debug in batch:
                pending.group = self.submit(raw, debug)
                pending.first = True
            return
        node = next(iter(batch[0][1]))
        queue = self.card.queues[node]
        uploads, packages, items = [], [], []
        try:
            for index, (_, raw, debug) in enumerate(batch):
                data, bindings = raw[node]
                recipe, handles, translations = debug[node]
                plain = B.Package(
                    lambda r=recipe, h=handles, t=translations: r.plain(h, t),
                    self.card.machine_json,
                    node,
                )
                upload = queue.prepare(data)
                uploads.append(upload)
                items.append((upload, bindings, 0, 0, index))
                packages.append((plain, ()))
            execution = queue.prepare_list(items)
        finally:
            for upload in uploads:
                upload.close()
        try:
            actual = self.card.submit_execution({node: (execution, tuple(packages))})
            for index, (pending, _, _) in enumerate(batch):
                pending.group, pending.first = actual, index == 0
        finally:
            execution.close()

    def result(self, pending, *args, **kwargs):
        if not isinstance(pending, Deferred):
            return self.collect(pending, *args, **kwargs)
        if pending.error is not None:
            raise pending.error
        self.flush()
        if pending.result is None:
            result = self.collect(pending.group, *args, **kwargs)
            pending.result = result if pending.first else {**result, "cycles": 0}
        return pending.result


@contextmanager
def batching(card):
    batch = Batch(card)
    prev_batch = card._eager_batch
    prev_suspended = card.native_direct_suspended
    card._eager_batch = batch
    card.native_direct_suspended = True
    try:
        yield
    except BaseException as error:
        for pending, _, _ in batch.pending:
            pending.error = error
        batch.pending.clear()
        raise
    else:
        batch.flush()
    finally:
        card._eager_batch = prev_batch
        card.native_direct_suspended = prev_suspended
