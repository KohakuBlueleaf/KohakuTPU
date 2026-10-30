"""Exclusive synchronous LIST exchange; one native publication/collection call.

The retained recording owns packages and bindings. No Python is called by the
native endpoint, including while waiting or acknowledging progress/completion.
Queue object bookkeeping remains at this boundary, not once per list item.
"""

from kohakuaccel.driver.node.queue.client import Completion
from kohakutpu.compiler import _rs


class NativeExchange:
    def __init__(self, queue, transport):
        self.queue = queue
        self.transport = transport
        self.handles = transport._c2h, transport._h2c
        self.generation = transport.fd_generation
        self.endpoint = _rs.QueueExchange(*self.handles, queue.layout)
        self.base = queue.mem._map(queue.base, queue.geo["end"])
        self.geometry = (
            queue.sq_n,
            queue.cq_n,
            queue.geo["cq"],
            queue.geo["rec"],
            queue.geo["so"],
        )

    def submit(self, execution, publishing):
        with self.queue._native_lock, self.transport._native_lock:
            return self._submit(execution, publishing)

    def _submit(self, execution, publishing):
        q = self.queue
        # A transport recovery replaces descriptors. Do not run an old native
        # owner against a different device; duplicate the new handles cold.
        handles = self.transport._c2h, self.transport._h2c
        if self.transport.fd_generation != self.generation:
            self.endpoint = _rs.QueueExchange(*handles, q.layout)
            self.handles = handles
            self.generation = self.transport.fd_generation
        q._tag += 1
        tag = q._tag
        if publishing is not None:
            publishing()
        execution.inflight += 1
        q._list_tags[tag] = execution
        q._outstanding.add(tag)
        try:
            result = self.exchange(execution, tag)
        except (ValueError, TypeError, OverflowError):
            q._list_tags.pop(tag)
            q._outstanding.discard(tag)
            execution.acknowledge()
            raise
        except BaseException:
            q._broken = True
            raise
        return self.finish(execution, tag, result)

    def exchange(self, execution, tag):
        q = self.queue
        return self.endpoint.run(
            self.base,
            self.geometry,
            q.sq_tail,
            q.cq_head,
            q.so_rd,
            q.layout.list_entry(
                tag,
                execution.descriptor.address,
                execution.count,
                execution.generation,
            ),
            q.timeout,
        )

    def finish(self, execution, tag, result):
        q = self.queue
        head, stdout, raw, record, console, polls, published, error = result
        q.counters["polls"] += polls
        q.cq_head, q.so_rd = head, stdout
        if published:
            q.sq_tail += 1
            q.counters["submitted"] += 1
        if error is not None:
            q._broken = True
            if not published:
                q._list_tags.pop(tag)
                q._outstanding.discard(tag)
                execution.acknowledge()
            raise OSError(error)
        completion = Completion.from_bytes(
            raw, q.layout, record, q.record or q.layout.record
        )
        q.console += console.decode("utf-8", "replace")
        q._done[tag] = completion
        q._sq_head_seen = q.sq_tail
        q._list_tags.pop(tag)
        execution.acknowledge()
        return tag
