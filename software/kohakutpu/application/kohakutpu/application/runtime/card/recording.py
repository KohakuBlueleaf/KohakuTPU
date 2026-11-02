"""Recorded programs: capture, replay, prepared submission."""

from kohakutpu.application.runtime import execution, replay_budget
from kohakutpu.application.runtime import packages as B
from kohakutpu.application.runtime.card.common import Parts, Pending, PreparedEvents


class Recording(Parts):
    # ------------------------------------------------------ record / replay
    def record(self) -> None:
        """Log every host write, host read and run's packages from now on."""
        self._forget_residents(self.nodes)
        self.log = []

    def recorded(self) -> list:
        """The log since `record`; logging off."""
        log, self.log = self.log or [], None
        return log

    def replay(self, log: list) -> list:
        """Run a `record`ed log again: each write rewritten, each run's
        packages (already in the nodes' heaps) submitted with its bindings.
        Runs on one node go back to back -- the node runs its queue in
        order -- and the host waits only where the next event needs the
        card idle: a write, or a run on another set of nodes. Returns each
        run's node cycles. Raises :class:`ValueError` for a log that reads
        (its later events may depend on what was read)."""
        if any(ev[0] == "read" for ev in log):
            raise ValueError("a log that reads the card cannot be replayed")
        self._forget_residents(self.nodes)
        cycles: list = []
        pending: dict = {}
        last = None
        for ev in log:
            if ev[0] == "write":
                self._drain(pending, cycles)
                self.mem.write_block(ev[1], ev[2])
                continue
            nodes = tuple(sorted(ev[1]))
            if len(nodes) > 1 or nodes != last:
                self._drain(pending, cycles)
            for n, (raw, bindings) in ev[1].items():
                pending.setdefault(n, []).append(self._submit(n, raw, list(bindings)))
            last = nodes
        self._drain(pending, cycles)
        self.runs += sum(1 for ev in log if ev[0] == "run")
        return cycles

    def prepare_recorded(
        self,
        log,
        budget,
        depth=1,
        fraction=replay_budget.DEFAULT_FRACTION,
        overcommit=False,
    ):
        """Decode and pin immutable recorded packages once, within a byte budget."""
        if any(ev[0] not in ("write", "run") for ev in log):
            raise ValueError("only writes and runs can be prepared")
        for ev in log:
            if ev[0] == "run":
                for n, (raw, bindings) in ev[1].items():
                    # The recorded bindings never change: admitted once here,
                    # not at each replay's submission.
                    B.admit(
                        raw,
                        self.machine_json,
                        n,
                        tuple(bindings or ()),
                        cached=self.admission_cached,
                    )
        budget, selected, counts, workspace = replay_budget.select(
            self, log, budget, depth, fraction=fraction, overcommit=overcommit
        )
        prepared = PreparedEvents(log)
        prepared.budget = budget
        prepared.stream_workspace = workspace
        prepared.selection = dict(
            unique_packages=len(counts),
            pinned_packages=len(selected),
            streamed_calls=sum(
                count for key, count in counts.items() if key not in selected
            ),
            unique_bytes=sum(
                replay_budget.padded(len(raw), self.memory.dram.align)
                for _, raw in counts
            ),
        )
        uploads = {}
        try:
            # Match the allocation order used by the free-span admission simulation.
            for n, raw in selected:
                uploads[n, raw] = self.queues[n].prepare(raw)
                prepared.uploads.append(uploads[n, raw])
            for ev in log:
                if ev[0] == "write":
                    prepared.append(ev)
                    continue
                programs = {}
                for n, (raw, bindings) in ev[1].items():
                    key = (n, raw)
                    if key in selected:
                        upload = uploads[key]
                    else:
                        upload = raw
                    programs[n] = (
                        upload,
                        B.Package(raw, self.machine_json, n),
                        tuple(bindings),
                    )
                prepared.append(("run", programs))
        except BaseException:
            prepared.close()
            raise
        return prepared

    def submit_execution(self, programs):
        tags = execution.publish(self, programs)
        return Pending(
            len(tags) == 1 and self.nodes[0] in tags, tags, {}, execution=True
        )

    def submit_prepared(self, programs):
        """Publish admitted entries; pinned uploads bypass transfer, others stream."""
        self._forget_residents(programs)
        tags, plain = {}, {}
        for n, (upload, pkg, bindings) in programs.items():
            tag = self._submit(n, upload, bindings, admitted=True)
            tags[n] = (tag, pkg)
            plain[n] = (pkg, bindings)
        return Pending(len(tags) == 1 and self.nodes[0] in tags, tags, plain)

    def submit_recorded(self, programs: dict) -> Pending:
        """Publish one replay event, retaining its own tags for collection."""
        plain = {
            n: (B.Package(raw, self.machine_json, n), list(b))
            for n, (raw, b) in programs.items()
        }
        self._forget_residents(programs)
        tags = {
            n: (self._submit(n, programs[n][0], b), pkg)
            for n, (pkg, b) in plain.items()
        }
        return Pending(len(tags) == 1 and self.nodes[0] in tags, tags, plain)
