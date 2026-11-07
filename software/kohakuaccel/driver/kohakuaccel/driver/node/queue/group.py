"""Retained node-local LISTs with publish-all-before-collect ownership."""

import threading
from contextlib import ExitStack
from dataclasses import dataclass
from typing import Any


@dataclass
class GroupCompletion:
    owner: "PreparedQueueGroup"
    tickets: tuple[tuple[int, int], ...]
    results: dict[int, Any] | None = None

    def wait(self):
        return self.owner.collect(self)


class PreparedQueueGroup:
    """One immutable package per node, retained through every terminal CQ.

    The caller owns allocation and user-slot leases. Preparation does not publish
    any RUN. A failed epoch quarantines every participating queue and its uploads.
    """

    def __init__(self, packages, queues, timeout_cycles=1 << 26):
        if not packages:
            raise ValueError("a queue group needs at least one node part")
        self._lock = threading.RLock()
        self.queues = {node: queues[node] for node in sorted(packages)}
        if len({id(q) for q in self.queues.values()}) != len(self.queues):
            raise ValueError("each node part needs its own queue")
        self.lists: dict[int, Any] = {}
        self.pending: GroupCompletion | None = None
        self.poisoned = self.closed = False
        try:
            for node, queue in self.queues.items():
                package = queue.prepare(packages[node])
                try:
                    self.lists[node] = queue.prepare_list(
                        [(package, (), timeout_cycles, 0, node)]
                    )
                finally:
                    package.close()
        except BaseException:
            for prepared in self.lists.values():
                prepared.close()
            raise

    def _poison(self):
        self.poisoned = True
        for queue in self.queues.values():
            queue._quarantined = True

    def submit(self):
        with self._lock:
            if self.closed or self.poisoned or self.pending is not None:
                raise RuntimeError("queue group is closed, failed or already in flight")
            tickets = []
            publishing = False

            def published():
                nonlocal publishing
                publishing = True

            with ExitStack() as locks:
                for queue in self.queues.values():
                    locks.enter_context(queue._native_lock)
                for node, queue in self.queues.items():
                    queue._require_usable()
                    prepared = self.lists[node]
                    if prepared.closed or prepared.generation != queue._generation:
                        raise ValueError(
                            "queue group belongs to a different queue epoch"
                        )
                    # Admission precedes every publication, including automatic flush.
                    start = queue._start()
                    while not queue._sq_room():
                        queue.poll()
                        waited = queue._waited(start, "a full SQ")
                        if waited > queue.timeout:
                            raise TimeoutError("queue group admission: SQ stayed full")
                        if waited >= queue.spin:
                            queue._idle()
                try:
                    for node, queue in self.queues.items():
                        tickets.append(
                            (
                                node,
                                queue.submit_list(
                                    self.lists[node], publishing=published
                                ),
                            )
                        )
                    for queue in self.queues.values():
                        queue.flush()
                except BaseException:
                    if publishing:
                        self._poison()
                    raise
            self.pending = GroupCompletion(self, tuple(tickets))
            return self.pending

    def collect(self, token: GroupCompletion):
        with self._lock:
            if token.owner is not self:
                raise ValueError("completion belongs to another queue group")
            if token.results is not None:
                return token.results
            if self.pending is not token or self.poisoned:
                raise RuntimeError("no collectable queue-group epoch")
            results = {}
            failure = None
            for node, ticket in token.tickets:
                try:
                    results[node] = self.queues[node].wait(ticket)
                except Exception as error:  # noqa: BLE001
                    if failure is None:
                        failure = error
            if failure is not None:
                self._poison()
                raise failure
            token.results = results
            self.pending = None
            return results

    def run(self):
        return self.submit().wait()

    def close(self):
        with self._lock:
            if self.closed:
                return
            if self.pending is not None or self.poisoned:
                raise RuntimeError(
                    "reset the card before reclaiming an uncertain group"
                )
            for prepared in self.lists.values():
                prepared.close()
            self.closed = True
