"""Host-only package placement bookkeeping and failure-only readback.

No card read, hash, or byte diff is performed for a successful completion. The
queue retirement hook runs before uploads can be freed/reused, not in main()'s
exception handler after a workload's finally/close. A readback is evidence, not
an automatic attribution: PATCH/FRONTIER can legitimately modify a package.
"""

import hashlib
import json
from collections.abc import Callable
from contextlib import nullcontext
from typing import Concatenate, ParamSpec, TypeVar
from weakref import WeakKeyDictionary

from kohakuaccel.driver.node.queue.client import NodeError, NodeQueue, PreparedPackage
from kohakutpu.application.runtime.eager_batch import Batch, Deferred
from kohakutpu.application.runtime.os.graph import ResidentGraph

Owner = TypeVar("Owner")
Params = ParamSpec("Params")
Result = TypeVar("Result")


class MethodHooks:
    """Recorder-owned method replacement scope, checked against each original.

    Concatenate keeps the receiver positional while ParamSpec checks the rest
    of the method's call contract. Restoration is host-only, including on error.
    """

    def __init__(self):
        self.originals: list[tuple[type, str, object]] = []

    def replace(
        self,
        owner: type[Owner],
        name: str,
        original: Callable[Concatenate[Owner, Params], Result],
        replacement: Callable[Concatenate[Owner, Params], Result],
    ) -> None:
        self.originals.append((owner, name, original))
        setattr(owner, name, replacement)

    def close(self) -> None:
        for owner, name, original in reversed(self.originals):
            setattr(owner, name, original)
        self.originals.clear()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def package_diff(expected, actual, address=0):
    """Exact byte comparison, with package-relative offsets and physical lines.

    Missing/extra bytes are differences too. A final partial line has only the
    bytes supplied by the caller; the recorder supplies the full padded upload.
    """
    offsets, lines, count = [], [], 0
    size = max(len(expected), len(actual))
    start = address - address % 64
    for line_address in range(start, address + size, 64):
        lo = max(0, line_address - address)
        hi = min(size, line_address + 64 - address)
        want, got = expected[lo:hi], actual[lo:hi]
        if want == got:
            continue
        different = [
            lo + i
            for i in range(max(len(want), len(got)))
            if want[i : i + 1] != got[i : i + 1]
        ]
        count += len(different)
        offsets.extend(different[: max(0, 16 - len(offsets))])
        lines.append(
            dict(
                address=line_address,
                offset=lo,
                expected_hex=want.hex(),
                actual_hex=got.hex(),
            )
        )
    return dict(
        equal=not lines,
        expected_bytes=len(expected),
        actual_bytes=len(actual),
        differing_bytes=count,
        first_differing_offsets=offsets,
        lines=lines,
    )


def transport_chain(memory):
    seen = set()
    while memory is not None and id(memory) not in seen:
        seen.add(id(memory))
        yield memory
        memory = getattr(memory, "inner", None)


class PackageRecorder:
    def __init__(self, trace):
        self.trace = trace
        self.hooks = MethodHooks()
        self.nodes = WeakKeyDictionary()
        self.pending = WeakKeyDictionary()
        self.residents = WeakKeyDictionary()
        self.resident_rows = WeakKeyDictionary()
        self.batch_rows = {}
        self.publishing_rows = None
        self.dead = set()
        self.checks = []
        self.seen_failures = set()

    def close(self):
        self.hooks.close()

    def attach(self, dev):
        # Retirement/teardown observation must not probe a link this diagnostic
        # (or a preceding queue operation) has already lost.
        dev._perf_package_transport_failed = self.transport_failed
        for node, queue in getattr(getattr(dev, "card", None), "queues", {}).items():
            self.nodes[queue] = node
        for node, resident in getattr(
            getattr(dev, "card", None), "sessions", {}
        ).items():
            self.residents[resident] = node

    def current(self):
        return self.trace.open[-1][0] if self.trace.open else None

    def published(self, queue, tag, packages, *, flags=None):
        if queue not in self.nodes:
            return
        rows = self.publishing_rows
        parts = []
        for index, package in enumerate(packages):
            # prepare() returns the immutable key plus the actual upload address.
            # Eager bytes use the same cached (address, length) upload() returned.
            if isinstance(package, PreparedPackage):
                raw, address, size = package.key, package.address, package.nbytes
            else:
                raw = bytes(package)
                address, size = queue._cache[raw]
            row = rows[index] if rows is not None else self.current()
            if row is None:
                continue  # Boot/diagnostic traffic is not a workload launch.
            span = dict(
                node=self.nodes[queue],
                queue_base=queue.base,
                generation=queue._generation,
                tag=tag,
                part=index,
                address=address,
                nbytes=size,
                uploaded_nbytes=size + (-size % queue.layout.line),
            )
            if flags is not None:
                span.update(list_count=len(packages), list_flags=flags)
            row.setdefault("packages", []).append(span)
            parts.append((row["seq"], span, raw))
        # A reset can reuse a tag; never read an old generation's upload.
        self.pending.setdefault(queue, {})[(queue._generation, tag)] = parts

    def unavailable(self, queue):
        # Inspect Python state only. Never probe health by reading a dead link.
        # A fault the driver recovered from leaves a working link; one it did
        # not (no recovery, or the recovery itself failed) leaves it dead.
        for transport in transport_chain(queue.mem):
            if id(transport) in self.dead:
                return "prior transport error"
            faults = getattr(transport, "faults", ())
            if len(faults) > getattr(transport, "counts", {}).get("recovered", 0):
                return "prior unrecovered C2H/H2C fault"
            if hasattr(transport, "_fds") and not transport._fds:
                return "closed transport"
        if queue._broken:
            return "queue transport publication/exchange failed"
        return None

    def transport_failed(self) -> str | None:
        """Why the run's transport is unusable, or None. A queue whose link was
        closed (a device already shut) is not part of the run."""
        if self.dead:
            return "prior transport error"
        for queue, node in self.nodes.items():
            if any(
                hasattr(t, "_fds") and not t._fds for t in transport_chain(queue.mem)
            ):
                continue
            reason = self.unavailable(queue)
            if reason is not None:
                return f"node {node}: {reason}"
        return None

    def mark_dead(self, queue):
        self.dead.update(id(t) for t in transport_chain(queue.mem))
        for peer in self.nodes:
            if any(id(t) in self.dead for t in transport_chain(peer.mem)):
                peer._quarantined = True
                peer._broken = True

    def require_available(self, queue, tag=None):
        """Stop before a queue operation, using only already-known host state."""
        if queue not in self.nodes:
            return
        reason = self.unavailable(queue)
        if reason is None:
            return
        completion = queue._done.get(tag)
        if completion is not None and not completion.ok:
            # A cached, trustworthy CQ can still identify a skipped diagnostic.
            # failed() raises before stdout retrieval or upload reclamation.
            self.failed(queue, NodeError(completion))
        self.mark_dead(queue)
        raise OSError(f"recorded queue access skipped: {reason}")

    def failed(self, queue, error):
        completion = error.completion
        nonfinite = completion.status == 0x24 and completion.detail == 1
        if completion.name != "BAD_STEP" and not nonfinite:
            return
        identity = (id(queue), queue._generation, completion.tag)
        if identity in self.seen_failures or queue not in self.nodes:
            return
        self.seen_failures.add(identity)
        parts = self.pending.get(queue, {}).get((queue._generation, completion.tag), ())
        record = completion.record or {}
        index = record.get("list_index", 0)
        readback_failure = None
        unavailable = self.unavailable(queue)
        # The diagnostic read alone also skips a link that has already needed a
        # C2H recovery: a further reset would destroy the evidence.
        recovered = any(
            f.get("kind") == "c2h"
            for t in transport_chain(queue.mem)
            for f in getattr(t, "faults", ())
        )
        check = dict(
            node=self.nodes[queue],
            queue_base=queue.base,
            generation=queue._generation,
            tag=completion.tag,
            status="NONFINITE_FP16" if nonfinite else completion.name,
            detail=completion.detail,
            step=completion.step,
            list_index=index,
            observation="before package retirement; not a global peer-quiescence proof",
        )
        selected = next((p for p in parts if p[1]["part"] == index), None)
        if selected is None:
            check.update(outcome="unavailable", reason="no exact launch/part placement")
        elif (
            any(p[1].get("list_count", 0) > 1 for p in parts)
            and "list_index" not in record
        ):
            check.update(
                outcome="unavailable", reason="missing failing HOSTL item index"
            )
        else:
            seq, span, raw = selected
            check.update(span, seq=seq)
            if unavailable is not None:
                check.update(outcome="skipped", reason=unavailable)
            elif recovered:
                check.update(outcome="skipped", reason="prior C2H recovery")
            else:
                # The padding was uploaded too; read whole painted 64-byte lines.
                expected = raw + bytes(span["uploaded_nbytes"] - len(raw))
                root = list(transport_chain(queue.mem))[-1]
                lock = getattr(root, "_native_lock", None)
                try:
                    with lock if lock is not None else nullcontext():
                        # A diagnostic must not reset/retry the link and destroy
                        # the evidence when this first read itself times out.
                        recovery = getattr(root, "recover", None)
                        try:
                            if hasattr(root, "recover"):
                                root.recover = None
                            actual = queue.mem.read_block(
                                span["address"], len(expected)
                            )
                        finally:
                            if hasattr(root, "recover"):
                                root.recover = recovery
                    check.update(package_diff(expected, actual, span["address"]))
                    check.update(
                        outcome="read",
                        uploaded_sha256=hashlib.sha256(expected).hexdigest(),
                        readback_sha256=hashlib.sha256(actual).hexdigest(),
                        interpretation="equality supports a read-path fault; differences may include legitimate package patches",
                    )
                except (OSError, TimeoutError, ValueError) as diagnostic:
                    self.mark_dead(queue)
                    readback_failure = diagnostic
                    check.update(outcome="readback_error", error=repr(diagnostic))
                    error.add_note(f"package readback: {diagnostic!r}")
        if unavailable is not None:
            # Skipping only readback is insufficient: wait() would next read
            # stdout, and retirement could unspill/paint the uncertain upload.
            self.mark_dead(queue)
            check["transport_unavailable"] = unavailable
        self.checks.append(check)
        try:
            (self.trace.out / "package-check.json").write_text(
                json.dumps(dict(schema=1, checks=self.checks), indent=2),
                encoding="utf-8",
            )
            self.trace.persist_launches()
        except OSError as diagnostic:
            error.add_note(f"package-check record not saved: {diagnostic!r}")
        if readback_failure is not None or unavailable is not None:
            # Never return to wait()'s stdout retrieval or free an upload after
            # a known transport failure. Keep the accelerator CQ as the cause
            # when the diagnostic itself failed, or as the original exception.
            queue._done.pop(completion.tag, None)
            if readback_failure is not None:
                raise readback_failure from error
            raise error

    def install(self):
        """Recorder-local hooks; driver protocols, uploads and success I/O unchanged."""
        submit, submit_list = NodeQueue.submit, NodeQueue.submit_list
        retire, wait, poll = NodeQueue._retire_package, NodeQueue.wait, NodeQueue.poll
        add, publish = Batch.add, Batch.publish
        resident_submit = ResidentGraph.submit
        recorder = self

        def on_submit(
            queue: NodeQueue,
            package: bytes | PreparedPackage | None = None,
            bindings=None,
            op: int | None = None,
            timeout_cycles: int = 0,
            flags: int = 0,
            arg: int = 0,
            words=None,
            publishing=None,
        ):
            def about_to_publish():
                if package is not None and (op is None or op == queue.layout.op_run):
                    recorder.published(queue, queue._tag, (package,))
                if publishing is not None:
                    publishing()

            try:
                recorder.require_available(queue)
                return submit(
                    queue,
                    package,
                    bindings,
                    op,
                    timeout_cycles,
                    flags,
                    arg,
                    words,
                    about_to_publish,
                )
            except (OSError, TimeoutError):
                recorder.mark_dead(queue)
                raise

        def on_list(queue: NodeQueue, prepared, publishing=None, native=False):
            def about_to_publish():
                recorder.published(
                    queue,
                    queue._tag,
                    prepared.uploads[: prepared.count],
                    flags=prepared.flags,
                )
                if publishing is not None:
                    publishing()

            try:
                recorder.require_available(queue)
                tag = submit_list(queue, prepared, about_to_publish, native)
            except (OSError, TimeoutError):
                recorder.mark_dead(queue)
                raise
            # NativeExchange collects synchronously and bypasses _retire_package.
            # Inspect before the caller can close the list and release its pins.
            completion = queue._done.get(tag)
            if completion is not None and not completion.ok:
                recorder.failed(queue, NodeError(completion))
            recorder.require_available(queue, tag)
            return tag

        def on_retire(queue: NodeQueue, tag: int) -> None:
            completion = queue._done.get(tag)
            if completion is not None and not completion.ok:
                recorder.failed(queue, NodeError(completion))
            recorder.require_available(queue, tag)
            if completion is not None:
                recorder.pending.get(queue, {}).pop((queue._generation, tag), None)
            return retire(queue, tag)

        def on_poll(queue: NodeQueue, *args, **kwargs):
            try:
                recorder.require_available(queue)
                return poll(queue, *args, **kwargs)
            except (OSError, TimeoutError):
                recorder.mark_dead(queue)
                raise

        def on_wait(queue: NodeQueue, tag: int, *args, **kwargs):
            try:
                recorder.require_available(queue, tag)
                return wait(queue, tag, *args, **kwargs)
            except NodeError as error:
                recorder.failed(queue, error)
                raise
            except (OSError, TimeoutError):
                recorder.mark_dead(queue)
                raise
            finally:
                recorder.pending.get(queue, {}).pop((queue._generation, tag), None)

        def on_add(batch: Batch, *args, **kwargs):
            pending = add(batch, *args, **kwargs)
            if isinstance(pending, Deferred):
                recorder.batch_rows[id(pending)] = recorder.current()
            return pending

        def on_publish(owner: Batch, batch):
            previous = recorder.publishing_rows
            recorder.publishing_rows = [
                recorder.batch_rows.pop(id(pending), None) for pending, *_ in batch
            ]
            try:
                return publish(owner, batch)
            finally:
                recorder.publishing_rows = previous

        def on_resident_submit(resident: ResidentGraph, *args, **kwargs):
            if resident in recorder.residents:
                owner = resident._retainer
                index = getattr(owner, "_installed_part", 0)
                # Parts after zero are installed/submitted during collect(),
                # often while another logical launch is open. Remember the
                # original row per resident, refreshing it on each replay.
                if index == 0:
                    recorder.resident_rows[resident] = recorder.current()
                row = recorder.resident_rows.get(resident)
                if row is not None:
                    row.setdefault("packages", []).append(
                        dict(
                            node=recorder.residents[resident],
                            part=index,
                            storage_format="kaos_tasks",
                            package_address=None,
                            address=resident.tasks_pa,
                            nbytes=len(resident._installed_raw),
                            graph_epoch=resident.graph_epoch,
                            sequence=resident.sequence + 1,
                            note="KAPK is translated, not uploaded; this is its resident task/payload span",
                        )
                    )
            return resident_submit(resident, *args, **kwargs)

        hooks = self.hooks
        hooks.replace(ResidentGraph, "submit", resident_submit, on_resident_submit)
        hooks.replace(NodeQueue, "submit", submit, on_submit)
        hooks.replace(NodeQueue, "submit_list", submit_list, on_list)
        hooks.replace(NodeQueue, "_retire_package", retire, on_retire)
        hooks.replace(NodeQueue, "wait", wait, on_wait)
        hooks.replace(NodeQueue, "poll", poll, on_poll)
        hooks.replace(Batch, "add", add, on_add)
        hooks.replace(Batch, "publish", publish, on_publish)
