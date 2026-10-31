"""A prepared graph as one execution list on a node's queue.

All package and descriptor pins outlive the terminal CQ acknowledgement. An
ambiguous submit or wait keeps them owned until the queue is stopped, never
frees live storage.
"""

from dataclasses import dataclass


class FirmwareRunner:
    def __init__(self, queue, timeout_cycles=0):
        self.queue = queue
        self.timeout_cycles = timeout_cycles

    def prepare(self, packets, dependencies):
        """Ordered packets implement the graph's serial predecessor contract."""
        q = self.queue
        uploads = []
        try:
            for packet in packets:
                uploads.append(q.prepare(packet))
            execution = q.prepare_list(
                [
                    (upload, (), self.timeout_cycles, 0, i + 1)
                    for i, upload in enumerate(uploads)
                ]
            )
        finally:
            for upload in uploads:
                upload.close()
        return PreparedList(q, execution)


@dataclass
class PreparedList:
    queue: object
    execution: object
    busy: bool = False

    def submit(self):
        if self.busy:
            raise RuntimeError("execution list has an unresolved submission")
        self.busy = True
        result = self.queue.wait(self.queue.submit_list(self.execution))
        self.busy = False
        return result

    def close(self):
        if not self.busy:
            self.execution.close()
