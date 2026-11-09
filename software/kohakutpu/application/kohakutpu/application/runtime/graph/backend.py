"""Execute the shared bound artifact through the selected retained backend."""

import time

from kohakutpu.application.runtime import launch_boundary
from kohakutpu.application.runtime import packages as B
from kohakutpu.application.runtime.device.common import Submitted
from kohakutpu.application.runtime.split import SlotLeases
from kohakutpu.language.config import ENV


def submit(prepared):
    dev = prepared.device
    launch_boundary.before_submit(dev)
    dev._healthy()
    dev.drain()
    card = dev.card
    prepare_started = time.monotonic()
    if dev.graph_backend == "kaos":
        backend = card.prepare_split(prepared)
    else:
        if dev._bound_source is not prepared or dev._bound_backend.closed:
            if dev._bound_backend is not None:
                dev._bound_backend.close()
            dev._bound_backend = prepared.prepare_firmware(
                card.queues, dev._bound_leases, timeout_cycles=dev.graph_timeout_cycles
            )
            dev._bound_source = prepared
        backend = dev._bound_backend
    prepare_seconds = time.monotonic() - prepare_started
    artifact = backend.artifact
    references = []
    for arena, address in prepared.references:
        arena.retain_pinned(address)
        references.append((arena, address))
    submitted = Submitted(
        prepared.compiled.name,
        "graph",
        {"slot_leases": tuple(sorted(artifact.slot_leases))},
        time.monotonic(),
        references=references,
        writes=list(references),
        nodes=tuple(part.node for part in artifact.parts),
    )
    card._forget_residents(card.nodes)
    card._publishing()
    try:
        started = time.monotonic()
        token = (
            backend.submit(timeout_cycles=dev.graph_timeout_cycles)
            if dev.graph_backend == "kaos"
            else backend.submit()
        )
        if dev.graph_backend == "firmware":
            for node, ticket in token.tickets:
                card._order.setdefault(node, []).append(ticket)
        results = token.wait()
        seconds = time.monotonic() - started
        if dev.graph_backend == "kaos":
            result = card.result(results, seconds)
        else:
            tickets = dict(token.tickets)
            nodes = {
                part.node: card._result(
                    card._segment(part.node, tickets[part.node]),
                    results[part.node],
                    B.Package(part.package, card.machine_json, part.node),
                )
                for part in artifact.parts
            }
            result = dict(
                cycles=max(done.cycles for done in results.values()),
                nodes=nodes,
                phases=dict(makespan_seconds=seconds),
            )
    except BaseException:
        dev._uncertain.append(submitted)
        raise
    result.setdefault("phases", {}).update(
        prepare_seconds=prepare_seconds,
        prepare_and_execute_seconds=prepare_seconds + seconds,
    )
    submitted.result = result
    card.runs += 1
    dev.inflight.append(submitted)
    return submitted


def configure(dev, backend):
    if backend not in (None, "firmware", "kaos"):
        raise ValueError("graph backend must be firmware or kaos")
    dev.graph_timeout_cycles = int(
        ENV.get("KOHAKUTPU_GRAPH_TIMEOUT_CYCLES", str(1 << 40))
    )
    dev.graph_backend = backend
    dev._bound_backend = dev._bound_source = None
    dev._bound_leases = SlotLeases()
