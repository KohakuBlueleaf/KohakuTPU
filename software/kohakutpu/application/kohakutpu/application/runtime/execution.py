"""HOSTL list grouping at replay boundaries, with original failure ownership."""

from kohakuaccel.driver.node.queue.client import NodeError
from kohakutpu.application.runtime.replay_budget import held_bytes, padded


def prepare(card, prepared):
    events, groups, current, indices = [], [], [], []
    run_index = 0

    def finish():
        if not current:
            return
        arena = card.memory.dram
        allocations = [
            size
            for node in current[0]
            for size in (
                padded(
                    sum(len(programs[node][2]) * 8 for programs in current), arena.align
                ),
                padded(
                    card.queues[node].layout.list_descriptor_bytes(len(current)),
                    arena.align,
                ),
            )
            if size
        ]
        held = held_bytes(card)
        if (
            len(current) == 1
            or held + sum(allocations) > prepared.budget
            or not arena.fits(allocations + prepared.stream_workspace)
        ):
            events.extend(("run", programs) for programs in current)
            groups.extend((index,) for index in indices)
            current.clear()
            indices.clear()
            return
        programs = {}
        for node in current[0]:
            entries = [programs[node] for programs in current]
            execution = card.queues[node].prepare_list(
                [
                    (upload, bindings, 0, 0, index)
                    for index, (upload, _, bindings) in zip(
                        indices, entries, strict=True
                    )
                ]
            )
            prepared.execution_lists.append(execution)
            programs[node] = execution, tuple((pkg, binds) for _, pkg, binds in entries)
        events.append(("list", programs))
        groups.append(tuple(indices))
        current.clear()
        indices.clear()

    for event in prepared:
        if event[0] == "write":
            finish()
            events.append(event)
        elif any(isinstance(upload, bytes) for upload, _, _ in event[1].values()):
            finish()
            events.append(event)
            groups.append((run_index,))
            run_index += 1
        else:
            if current and current[0].keys() != event[1].keys():
                finish()
            current.append(event[1])
            indices.append(run_index)
            run_index += 1
            if len(event[1]) > 1:
                finish()
    finish()
    return events, groups


def publish(card, programs):
    card._forget_residents(programs)
    tags = {}
    for node, (execution, packages) in programs.items():
        queue = card.queues[node]
        order = card._order.setdefault(node, [])
        if not order:
            queue.read_stdout()
            card._stdout[node] = ""
        tag = queue.submit_list(
            execution, publishing=card._publishing, native=len(programs) == 1
        )
        order.append(tag)
        tags[node] = tag, packages
    return tags


def collect(card, pending, dump):
    results = {}
    for node in pending.tags:
        card.queues[node].flush()
    for node, (tag, packages) in pending.tags.items():
        peers = [card.queues[n] for n in pending.tags if n != node]

        def service(peers=peers):
            for peer in peers:
                peer.poll()

        try:
            completion = card.queues[node].wait(
                tag, timeout=card.timeout, service=service if peers else None
            )
        except NodeError as error:
            card._forget_residents(pending.tags)
            card._segment(node, tag, error.console)
            if error.completion.status != card.queues[node].layout.bad_arg:
                pkg, bindings = packages[error.completion.record["list_index"]]
                card._keep_failed(pkg, bindings, error, node)
            raise
        card._segment(node, tag)
        results[node] = card._result("", completion, None)
        results[node]["launches"] = len(packages)
    card.runs += max(len(packages) for _, packages in pending.tags.values())
    result = (
        results[card.nodes[0]]
        if pending.single
        else {"cycles": max(r["cycles"] for r in results.values()), "nodes": results}
    )
    if dump is not None:
        result["dump"] = card.dump(dump)
    return result
