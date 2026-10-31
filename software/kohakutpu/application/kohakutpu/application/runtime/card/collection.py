"""Taking completions back, their checks, and the debug rerun."""

import dataclasses
import json
import os
import pathlib
import re
import sys

from kohakuaccel.driver.node.queue import NodeError
from kohakuaccel.driver.node.queue import layout as QL
from kohakutpu.application.runtime import execution
from kohakutpu.application.runtime import packages as B
from kohakutpu.application.runtime.card.common import (
    CHECKED_KEPT,
    DBG_H_RD,
    DBG_LASTS,
    FAILED_ENV,
    Pending,
)
from kohakutpu.application.runtime.eager_batch import Deferred
from kohakutpu.application.runtime.packing import contract_error
from kohakutpu.stack.rs import _rs


class Collection:
    def _drain(self, pending: dict, cycles: list) -> None:
        for n, tags in pending.items():
            for tag in tags:
                try:
                    cycles.append(self.queues[n].wait(tag, timeout=self.timeout).cycles)
                    self._segment(n, tag)
                except NodeError as error:
                    self._forget_residents((n,))
                    raise contract_error(error) from error
                except Exception:
                    self._forget_residents((n,))
                    raise
        pending.clear()

    def _keep_failed(self, pkg, bindings, error: NodeError, node: int) -> None:
        where = os.environ.get(FAILED_ENV)
        if not where:
            return
        d = pathlib.Path(where)
        d.mkdir(parents=True, exist_ok=True)
        stem = d / f"failed_{self.runs:05d}_n{node}_t{error.completion.tag:x}"
        stem.with_suffix(".pkg").write_bytes(pkg.to_bytes())
        record = {
            "bindings": [int(b) for b in bindings or ()],
            "node": node,
            "completion": dataclasses.asdict(error.completion),
            "snapshot": error.snapshot,
            "snapshot_error": error.snapshot_error,
            "console": error.console,
        }
        stem.with_suffix(".json").write_text(
            json.dumps(record, indent=2), encoding="utf-8"
        )
        print(f"kept the failed package at {stem}.pkg", file=sys.stderr, flush=True)

    # ----------------------------------------------------------- debug
    def debug_report(self, node: int | None = None) -> dict:
        """The debug region's records since the last read, decoded by the Rust
        decoder (`_rs.debug_decode`, RUL.080), and marked read on the node.
        Raises when this card is not a debug session."""
        node = self.nodes[0] if node is None else node
        if not self.debug or node not in self.debug_regions:
            raise RuntimeError(
                "DBG.SEL.003: not a debug session: open the card with debug=True"
            )
        base, size = self.debug_regions[node]
        head = self.mem.read_block(base, DBG_LASTS)
        h = [int.from_bytes(head[i : i + 8], "little") for i in range(0, DBG_LASTS, 8)]
        last, units, rec, cap, wr, rd = h[2], h[3], h[4], h[5], h[6], h[8]
        buf = bytearray(size)
        buf[:DBG_LASTS] = head
        lasts = units * (1 + last) * 8
        buf[DBG_LASTS : DBG_LASTS + lasts] = self.mem.read_block(
            base + DBG_LASTS, lasts
        )
        at = rd
        while cap and at < wr:
            off = at % cap
            n = min(wr - at, cap - off)
            buf[rec + off : rec + off + n] = self.mem.read_block(base + rec + off, n)
            at += n
        got = _rs.debug_decode(bytes(buf), self.machine_json)
        self.mem.write_block(
            base + DBG_H_RD, self.queue_layout.line_bytes(got["consumed"])
        )
        return got

    @staticmethod
    def drained(report: dict | None, console: str = "") -> bool:
        """Whether a failed run's quiesce drained (DBG.DUMP.002): the debug
        image's FAIL records in `report`, else the production image's last
        ``PKGF stray <n> outstanding <n>`` line in `console`."""
        if report is not None:
            return all(
                p["fail"] is None or p["fail"]["drained"] for p in report["packages"]
            )
        left = re.findall(r"PKGF stray \d+ outstanding (\d+)", console)
        return bool(left) and int(left[-1]) == 0

    def dump(self, buffers: dict, stable: bool = True) -> dict:
        """`buffers` (``{name: (address, bytes)}``, the caller's: a bound
        package carries no buffer entries) read by the host after the
        completion (RUL.076): ``{name: {"bytes", "stable"}}`` (DBG.DUMP.001).
        `stable` False marks bytes a failed run left after a quiesce that did
        not drain (DBG.DUMP.002)."""
        return {
            name: {"bytes": self.get(at, nbytes), "stable": stable}
            for name, (at, nbytes) in buffers.items()
        }

    def _debug_rerun(
        self, node: int, raw: bytes, bindings, report: dict, timeout_cycles: int = 0
    ) -> dict:
        """RUL.075: a failed package once more, off the dispatch engine and
        traced, for exact step attribution, under the failed run's bound
        (`timeout_cycles`, 0 the boot block's); the firmware restarted first
        when the failure did not drain (RUL.082)."""
        if not self.drained(report):
            self.restart()
        q = self.queues[node]
        try:
            q.run(
                raw,
                bindings,
                timeout=self.timeout,
                timeout_cycles=timeout_cycles,
                flags=QL.RUN_F_TRACE | QL.RUN_F_NOENGINE,
            )
        except NodeError:
            pass
        again = self.debug_report(node)
        if any(p["fail"] and not p["fail"]["drained"] for p in again["packages"]):
            self.restart()
        return again

    def collect(self, p: "Pending", faults: bool = False, dump=None) -> dict:
        """The result of a :meth:`submit`ted run (as :meth:`run` returns it),
        waiting for it; `faults` and `dump` as :meth:`run`'s. Collected
        twice, the first result."""
        if isinstance(p, Deferred):
            return p.owner.result(p, faults=faults, dump=dump)
        if p.result is None:
            try:
                p.result = self._collect(p, faults, dump)
            except NodeError as error:
                self._forget_residents(p.tags)
                raise contract_error(error) from error
            except Exception:
                self._forget_residents(p.tags)
                raise
        return p.result

    def _collect(self, p: "Pending", faults: bool, dump) -> dict:
        if p.execution:
            return execution.collect(self, p, dump)
        single, tags, plain = p.single, p.tags, p.plain
        # Peers may communicate: publish all of them before waiting on any one.
        if not single:
            for node in tags:
                queue = self.queues[node]
                if hasattr(queue, "flush"):
                    queue.flush()
        if single:
            node = self.nodes[0]
            tag, pkg = tags[node]
            bindings = plain[node][1]
            try:
                done = self.q.wait(tag, timeout=self.timeout)
            except TimeoutError:
                self._order[node].remove(tag)
                raise
            except NodeError as e:
                try:
                    self._segment(node, tag, e.console)
                    self._keep_failed(pkg, bindings, e, node)
                    e.debug = self.debug_report(node) if self.debug else None
                    if dump is not None:
                        # Before the rerun, which writes the same buffers.
                        e.dump = self.dump(dump, self.drained(e.debug, e.console))
                    if e.debug is not None:
                        e.rerun = self._debug_rerun(
                            node, plain[node][0].to_bytes(), bindings, e.debug
                        )
                except Exception as diagnostic:  # noqa: BLE001
                    e.add_note(f"failure diagnostics: {diagnostic!r}")
                raise
            self.runs += 1
            res = self._result(self._segment(node, tag), done, pkg)
            if self.debug:
                res["debug"] = self.debug_report(node)
            if dump is not None:
                res["dump"] = self.dump(dump)
            return res
        out = {}
        #: Every failure so far drained (DBG.DUMP.002).
        stable = True
        for k, (n, (tag, pkg)) in enumerate(tags.items()):
            try:
                done = self.queues[n].wait(tag, timeout=self.timeout)
            except TimeoutError as e:
                self._forget_residents((n,))
                # The node may still be running: nothing read is stable.
                stable = False
                self._order[n].remove(tag)
                if not faults:
                    raise
                out[n] = {"fault": str(e), "debug": None}
                continue
            except NodeError as e:
                e = contract_error(e)
                self._forget_residents((n,))
                try:
                    self._segment(n, tag, e.console)
                    self._keep_failed(pkg, plain[n][1], e, n)
                    e.debug = self.debug_report(n) if self.debug else None
                    stable = stable and self.drained(e.debug, e.console)
                    if not faults and dump is not None:
                        e.dump = self.dump(dump, stable and k == len(tags) - 1)
                except Exception as diagnostic:  # noqa: BLE001
                    stable = False
                    e.add_note(f"failure diagnostics: {diagnostic!r}")
                if not faults:
                    raise
                out[n] = {
                    "fault": str(e),
                    "debug": e.debug,
                    "snapshot": e.snapshot,
                    "snapshot_error": e.snapshot_error,
                }
                continue
            try:
                out[n] = self._result(self._segment(n, tag), done, pkg)
                if self.debug:
                    out[n]["debug"] = self.debug_report(n)
            except ValueError as e:
                self._forget_residents((n,))
                # The node's own report is unreadable: its processor misbehaved.
                if not faults:
                    raise
                out[n] = {"fault": f"garbled node report: {e}"}
        self.runs += 1
        cycles = [r["cycles"] for r in out.values() if "cycles" in r]
        res = {"cycles": max(cycles, default=0), "nodes": out}
        if dump is not None:
            res["dump"] = self.dump(dump, stable)
        return res

    def verify(self, progs: dict) -> list:
        """The checker's findings for `progs` (``{node: Program}``), once per
        set of programs: raises `verify.Refused` under ``check="refuse"``."""
        key = tuple(
            sorted(
                (
                    n,
                    (
                        (id(p.package), id(p.findings), p.refused)
                        if isinstance(p, B.Prebuilt)
                        else id(p)
                    ),
                )
                for n, p in progs.items()
            )
        )
        if key not in self.checked:
            found = B.findings(progs, self.check)
            # The programs (or their package bytes and findings) are held so their ids are not reused.
            held = tuple(
                (p.package, p.findings) if isinstance(p, B.Prebuilt) else p
                for p in progs.values()
            )
            self.checked[key] = (held, found)
            while len(self.checked) > CHECKED_KEPT:
                self.checked.pop(next(iter(self.checked)))
            for f in found:
                print(f"verify: {f}", file=sys.stderr, flush=True)
        return self.checked[key][1]

    def _result(self, text: str, done, pkg) -> dict:
        steps, phases = [], {}
        record = getattr(done, "record", None)
        if record is not None:
            phases = {k: v for k, v in record.items() if k not in ("tag", "so_wr")}
        for ln in text.splitlines():
            if ln.startswith("PKGT "):
                words = ln.split()[1:]
                phases = {
                    words[i]: int(words[i + 1]) for i in range(0, len(words) - 1, 2)
                }
            if ln.startswith("PKGS "):
                first, last, at = (int(v) for v in ln.split()[1:4])
                steps = [] if first == 0 else steps
                what = [
                    f"{s.op.name} u{s.unit} n{s.count}"
                    for s in pkg.steps[first : last + 1]
                ]
                steps.append((first, last, at, ", ".join(what)))
        return {"cycles": done.cycles, "steps": steps, "phases": phases, "pkg": pkg}
