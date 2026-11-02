"""HOSTL resident FIFO lists, retained through the terminal CQ acknowledgment."""

import os
import struct
from dataclasses import dataclass, field

from kohakuaccel.driver.node.queue.layout import LIST_F_CHAIN


@dataclass
class ExecutionList:
    queue: object
    generation: int
    count: int
    uploads: list = field(default_factory=list)
    inflight: int = 0
    closed: bool = False
    #: Seconds of its last run, by the node's clock: when the next one is due.
    seconds: float = 0.0
    #: `LIST_F_*` of the list entry.
    flags: int = 0

    @property
    def descriptor(self):
        return self.uploads[-1]

    def close(self):
        self.closed = True
        self.release()

    def release(self):
        if self.closed and not self.inflight:
            for upload in self.uploads:
                upload.close()

    def acknowledge(self):
        self.inflight -= 1
        self.release()


def prepare(queue, items):
    """HOSTL ABI 1: (prepared package, bindings, timeout, flags, item tag).

    `KOHAKUTPU_LIST_CHAIN=1` chains the items (L0.NQ.057); serial by default.
    """
    with queue._native_lock:
        layout = queue.layout
        if (
            queue.version != layout.version
            or queue.record is None
            or queue.mem.read64(queue.base + layout.list_abi) != layout.list_version
        ):
            raise ValueError("firmware does not advertise execution-list ABI 1")
        if not items:
            raise ValueError("an execution list needs at least one item")
        result = ExecutionList(queue, queue._generation, len(items))
        if os.environ.get("KOHAKUTPU_LIST_CHAIN", "0") == "1":
            result.flags = LIST_F_CHAIN
        try:
            bindings, entries = bytearray(), []
            for package, binds, timeout, flags, tag in items:
                if (
                    package.queue is not queue
                    or package.closed
                    or package.generation != queue._generation
                ):
                    raise ValueError(
                        "list item is closed or belongs to another queue epoch"
                    )
                if len(binds) > queue.binding_limit:
                    raise ValueError("too many list item bindings")
                owned = queue.prepare(package.key)
                result.uploads.append(owned)
                entries.append((owned, len(bindings), len(binds), timeout, flags, tag))
                bindings.extend(struct.pack(f"<{len(binds)}Q", *binds))
            binding_base = 0
            if bindings:
                bindings.extend(bytes(-len(bindings) % layout.line))
                upload = queue.prepare(bytes(bindings))
                result.uploads.append(upload)
                binding_base = upload.address
            raw = b"".join(
                layout.entry_bytes(
                    *layout.run_entry(
                        tag,
                        p.address,
                        p.nbytes,
                        binding_base + offset if nbind else 0,
                        nbind,
                        timeout,
                        flags,
                    )
                )
                for p, offset, nbind, timeout, flags, tag in entries
            )
            result.uploads.append(queue.prepare(raw))
        except BaseException:
            result.close()
            raise
        return result
