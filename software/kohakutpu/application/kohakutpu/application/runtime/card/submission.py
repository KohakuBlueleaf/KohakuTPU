"""Putting a package on the card's queues."""

from kohakuaccel.driver.node.queue import layout as QL
from kohakuaccel.driver.node.queue.client import PreparedPackage
from kohakutpu.application.runtime import packages as B
from kohakutpu.application.runtime.card.common import Parts, Pending
from kohakutpu.application.runtime.eager_batch import Deferred
from kohakutpu.language.dsl.kernel import _MUTANT


class Submission(Parts):
    # ------------------------------------------------------------- run
    def run(
        self,
        program,
        faults: bool = False,
        trace: bool = False,
        flags: int = 0,
        dump=None,
        after: int | None = None,
    ) -> dict:
        """Run `program` once: ``{"cycles", "steps", "phases", "pkg"}``. Given
        ``{node: Program}`` (None for this card's first node), every node's
        package is submitted before any is waited on; the result is per node
        under ``"nodes"`` and ``"cycles"`` is the longest. With `faults`, a
        node that times out or faults reports ``{"fault": text}`` and the
        other nodes are still waited for. `trace` (the debug image only)
        records each step; `flags` are further run flags (`layout.RUN_F_*`).
        `dump` (``{name: (address, bytes)}``) adds :meth:`dump` of it under
        ``"dump"`` (on a failure, the `NodeError`'s ``dump``); `after` cuts
        each package after that step (RUL.076). `collect(submit(...))`."""
        return self.collect(
            self.submit(program, trace=trace, flags=flags, after=after),
            faults=faults,
            dump=dump,
        )

    def submit(
        self, program, trace: bool = False, flags: int = 0, after: int | None = None
    ) -> "Pending | Deferred":
        """`program` checked, built and queued on its nodes without waiting:
        the node runs its queue in order, so the next package is queued while
        this one runs (L0.NQ.047). :meth:`collect` waits for it. Inside an
        eager batch a plain run on one node joins the batch's list."""
        batch = self._eager_batch
        joins = batch is not None and not (flags or trace) and after is None
        if batch is not None and not joins:
            batch.flush()
        flags |= QL.RUN_F_TRACE if trace else 0
        if flags & (QL.RUN_F_TRACE | QL.RUN_F_NOENGINE) and not self.debug:
            raise RuntimeError(
                f"DBG.SEL.003: {self.fw.name} has no trace; a traced run needs the "
                f"debug image (debug=True)"
            )
        single = not isinstance(program, dict)
        progs = B.placed(program, self.nodes)
        self.verify(progs)
        mode = self.resident_mode
        if mode.forget_all:
            for cores in self.residents.values():
                cores.forget()
        # Unlowered: what the firmware path runs (a NOENGINE run, the debug
        # rerun of a failure); lowered: what the dispatch engine runs.
        # A Rust package's cut is the Rust stack's (`_rs.bind(after=)`).
        off = bool(flags & QL.RUN_F_NOENGINE)
        stateless = self.submit_cache and mode.stateless
        if stateless:
            self._forget_residents(progs)
        predicted = (
            None if stateless else {n: self.residents[n].snapshot() for n in progs}
        )
        both = B.built(
            progs,
            self.machine,
            self.fetch,
            None if off else predicted,
            after,
            resident=mode.resident,
            cached=self.admission_cached,
        )
        plain = {n: (pkg, b) for n, (pkg, _, b) in both.items()}
        lowered = {n: (pkg, b) for n, (_, pkg, b) in both.items()}
        built = plain if off else lowered
        if self.log is not None:
            self.log.append(
                (
                    "run",
                    {n: (pkg.to_bytes(), tuple(b)) for n, (pkg, b) in built.items()},
                )
            )

        def publish() -> Pending:
            tags = {}
            for n, (pkg, bindings) in built.items():
                try:
                    # `B.built` admits lowered bytes; NOENGINE admits its own stream.
                    tag = self._submit(
                        n, pkg.to_bytes(), bindings, flags=flags, admitted=not off
                    )
                except Exception:
                    self._forget_residents((n,))
                    raise
                tags[n] = (tag, pkg)
            return Pending(single, tags, plain)

        # The node runs its queue in order, list or not: IMEM ends as predicted.
        for n in built:
            if off or after is not None:
                self._forget_residents((n,))
            elif predicted is not None:
                self.residents[n].restore(predicted[n])
        if joins and batch is not None:
            raw = {n: (pkg.to_bytes(), b) for n, (pkg, b) in built.items()}
            return batch.add(raw, {n: pkg for n, (pkg, _) in plain.items()}, publish)
        return publish()

    def submit_wire(self, raw, debug):
        """Already admitted native bytes; publication retains ordinary queue ownership."""
        if self._eager_batch is not None and not self._eager_batch._submitting:
            return self._eager_batch.wire(raw, debug)
        tags, plain = {}, {}
        for n, (data, bindings) in raw.items():
            recipe, handles, translations = debug[n]
            plain[n] = (
                B.Package(
                    lambda recipe=recipe, handles=handles, translations=translations: recipe.plain(
                        handles, translations
                    ),
                    self.machine_json,
                    n,
                ),
                (),
            )
            pkg = B.Package(data, self.machine_json, n)
            try:
                tag = self._submit(n, data, bindings, admitted=True)
            except Exception:
                self._forget_residents((n,))
                raise
            tags[n] = tag, pkg
        if self.log is not None:
            self.log.append(("run", raw))
        single = len(tags) == 1 and (
            _MUTANT.get() == "app.card/single_by_count" or self.nodes[0] in tags
        )
        return Pending(single, tags, plain)

    @property
    def publication_epoch(self) -> int:
        """Monotonic count of attempted queue publications, including failed writes."""
        return self._publication_epoch

    def _publishing(self) -> None:
        self._publication_epoch = self.publication_epoch + 1

    def _submit(self, n: int, raw, bindings, admitted: bool = False, **kw) -> int:
        """A RUN of `raw` (package bytes, or an upload `prepare_recorded`
        admitted) queued on node `n`; its tag, in the node's order of
        report text (`_segment`). `admitted`: the caller's build already
        admitted `raw`."""
        if not admitted and not isinstance(raw, PreparedPackage):
            B.admit(
                raw,
                self.machine_json,
                n,
                tuple(bindings or ()),
                cached=self.admission_cached,
            )
        q, order = self.queues[n], self._order.setdefault(n, [])
        if not order:
            # Text of runs nothing here waits for (a replay, a direct
            # `q.run`): not this run's.
            q.read_stdout()
            self._stdout[n] = ""
        tag = q.submit(raw, bindings, publishing=self._publishing, **kw)
        order.append(tag)
        return tag

    def _segment(self, n: int, tag: int, console: str = "") -> str:
        """Split each run's text at its record's stdout count (L0.NQ.052)."""
        q = self.queues[n]
        if getattr(q, "record", None) is None:
            return self._segment_pkgt(n, tag, console)
        order, done = self._order.setdefault(n, []), self._segs.setdefault(n, {})
        buf = self._stdout.pop(n, "") + console
        while tag in order:
            end = q.ends.pop(order[0], None)
            if end is None:
                break
            start = q.console_at - len(buf)
            if end > q.console_at:
                buf += q.read_stdout_to(end)
            take = max(0, end - start)
            done[order.pop(0)], buf = buf[:take], buf[take:]
        self._stdout[n] = buf
        return done.pop(tag, "")

    def _segment_pkgt(self, n: int, tag: int, console: str = "") -> str:
        """The report text node `n` printed for run `tag`. A RUN ends its text
        with one PKGT line (L0.BOOT.010), so the node's text splits into its
        runs in submission order; text read before (`console`, a failure's)
        is taken first."""
        buf = self._stdout.get(n, "") + console
        order, done = self._order.setdefault(n, []), self._segs.setdefault(n, {})
        while True:
            more = self.queues[n].read_stdout()
            buf += more
            while order and order[0] not in done:
                at = buf.find("PKGT ")
                end = buf.find("\n", at)
                if at < 0 or end < 0:
                    break
                done[order.pop(0)] = buf[: end + 1]
                buf = buf[end + 1 :]
            if tag not in order or not more:
                break
        self._stdout[n] = buf
        return done.pop(tag, "")

    def _forget_residents(self, nodes):
        """Invalidate predicted IMEM after any uncertain execution."""
        for n in nodes:
            cores = getattr(self, "residents", {}).get(n)
            if cores is not None:
                cores.forget()
