"""Recorded programs and their replay."""

import time

from kohakutpu.application.runtime.device.common import Parts, Recorded, Run, Submitted
from kohakutpu.application.runtime.graph import recorded as native_recorded
from kohakutpu.language.config import ENV


class Replay(Parts):
    # ----------------------------------------------------- record / replay
    def record(self) -> None:
        """Record on the main heap, pinning the spans each event uses."""
        if self.recording:
            raise RuntimeError("a recording is already active")
        self.card.record()
        self.recording = True
        self._record_pins = []
        self._record_runs = []
        self._record_templates = []

    def recorded(self) -> Recorded:
        """Finish recording; the returned log owns its pins until closed."""
        if not self.recording:
            raise RuntimeError("no recording is active")
        log = Recorded(
            self.card.recorded(),
            self._record_pins,
            self,
            self._record_runs,
            self._record_templates,
        )
        self._record_pins = []
        self._record_runs = []
        self._record_templates = []
        self.recording = False
        if ENV.get("KTPU_GRAPH_FUSE", "0") == "1":
            try:
                log.native = native_recorded.from_environment(log)
            except BaseException:
                log.close()
                raise
        return log

    def private_state(self) -> tuple:
        """Compatibility state; replay lifetimes belong to the log's pins."""
        return tuple(
            (k, tuple(a.free_list), tuple(sorted(a.live.items())))
            for k, a in sorted(self.private.items(), key=str)
        )

    def replay(self, log: list, wait: bool = True) -> list:
        """`log` run again, nothing recompiled or rebuilt: one `Run` a
        package run, its node cycles (no wall time of its own)."""
        if not isinstance(log, Recorded) or log.closed or log.owner is not self:
            raise ValueError("replay requires an open, pinned recording")
        self._healthy()
        if self.closed:
            raise RuntimeError("device is closed")
        if log.native is not None:
            # Native LIST is synchronous even when asynchronous completion is optional.
            self.drain()
            try:
                cycles = log.native.run()
            except BaseException as error:
                if log.native.busy:
                    self._failure = error
                    self._uncertain.append(log)
                raise
            run = Run(
                "native_graph",
                cycles,
                0.0,
                "graph",
                {"launches": log.native.after, "rules": log.native.rules},
            )
            self.runs.append(run)
            self.counters["runs"] += 1
            self.counters["cycles"] += cycles
            return [run]
        if hasattr(self.card, "submit_recorded"):
            pending = self._replay_queued(log)
            return [self.collect_through(s) for s in pending] if wait else pending
        self.drain()
        references = list(log.references)
        for arena, at in references:
            arena.retain_pinned(at)
        publication = getattr(self.card, "publication_epoch", None)
        try:
            cycles = self.card.replay(log)
        except BaseException:
            if publication is None or self.card.publication_epoch != publication:
                self._uncertain.append(
                    Submitted(
                        "replay",
                        "replay",
                        {},
                        time.monotonic(),
                        references=references,
                    )
                )
            else:
                for arena, at in references:
                    arena.unretain(at)
            raise
        for arena, at in references:
            arena.unretain(at)
        out = []
        for c in cycles:
            r = Run("replay", int(c), 0.0, "replay")
            self.runs.append(r)
            self.counters["runs"] += 1
            self.counters["cycles"] += r.cycles
            out.append(r)
        return out

    def prepare_recorded(self, log):
        """Keep a recording's dispatch wrappers and uploads, when selected."""
        if not isinstance(log, Recorded) or log.closed or log.owner is not self:
            raise ValueError("preparation requires an open, pinned recording")
        if log.native is not None:
            return
        if (
            self.prepared_replay
            and hasattr(self.card, "prepare_recorded")
            and log.prepared is None
        ):
            log.prepared = self.card.prepare_recorded(
                log,
                self.replay_package_bytes,
                self.max_inflight,
                fraction=self.replay_package_fraction,
                overcommit=self.replay_overcommit,
            )
            if not hasattr(self.card, "prepare_execution"):
                return
            try:
                events, groups = self.card.prepare_execution(log.prepared)
                log.list_events, log.list_runs = events, []
                for group in groups:
                    runs = [log.runs[i] for i in group]
                    refs = list(dict.fromkeys(r for run in runs for r in run[3]))
                    writes = list(dict.fromkeys(r for run in runs for r in run[4]))
                    nodes = tuple(dict.fromkeys(n for run in runs for n in run[5]))
                    log.list_runs.append(
                        (
                            "replay-list",
                            "replay",
                            {"launches": len(group)},
                            refs,
                            writes,
                            nodes,
                        )
                    )
            except BaseException:
                log.prepared.close()
                log.prepared = None
                raise

    def _replay_queued(self, log):
        if any(ev[0] == "read" for ev in log):
            raise ValueError("a log that reads the card cannot be replayed")
        self.prepare_recorded(log)
        prepared = log.prepared if self.prepared_replay else None
        if prepared is not None and not prepared.matches(log):
            raise ValueError("prepared recording was changed or closed")
        submit = (
            self.card.submit_recorded if prepared is None else self.card.submit_prepared
        )
        metadata = iter(log.runs)
        events = log if prepared is None else prepared
        if prepared is not None and hasattr(self.card, "prepare_execution"):
            events, metadata, submit = (
                log.list_events,
                iter(log.list_runs),
                self.card.submit_execution,
            )
        out = []
        for ev in events:
            if ev[0] == "write":
                # Log addresses are physical; the recording owns their spans.
                refs = [
                    (a, at) for a, at in log.references if at <= ev[1] < at + a.live[at]
                ]
                conflicts = [
                    s for s in self.inflight if any(r in s.references for r in refs)
                ]
                if conflicts:
                    self.collect_through(conflicts[-1])
                self.card.write(ev[1], ev[2])
                continue
            name, kind, info, refs, writes, nodes = next(metadata)
            self._fence(nodes, refs, writes)
            while (
                self.max_inflight is not None
                and len(self.inflight) >= self.max_inflight
            ):
                self.collect(self.inflight[0])
            self._admit(nodes, refs)
            for arena, at in refs:
                arena.retain_pinned(at)
            s = Submitted(
                name,
                kind,
                dict(info),
                time.monotonic(),
                references=list(refs),
                writes=list(writes),
                nodes=nodes,
            )
            publication = getattr(self.card, "publication_epoch", None)
            try:
                selected_submit = (
                    self.card.submit_prepared
                    if prepared is not None and ev[0] == "run"
                    else submit
                )
                s.pending = selected_submit(ev[1])
            except BaseException:
                if publication is None or self.card.publication_epoch != publication:
                    self._uncertain.append(s)
                else:
                    for arena, at in s.references:
                        arena.unretain(at)
                raise
            self.inflight.append(s)
            out.append(s)
        for queue in getattr(self.card, "queues", {}).values():
            if hasattr(queue, "flush"):
                queue.flush()
        return out
