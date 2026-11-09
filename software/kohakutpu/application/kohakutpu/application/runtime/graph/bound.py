"""Bind a composed graph once, retaining borrowed eager scratch and aliases."""

import hashlib
import json
from dataclasses import replace

from kohakutpu.application.runtime import packages as B
from kohakutpu.application.runtime.binding import (
    burst_line,
    burst_unaligned,
    cluster_burst,
    unaligned,
)
from kohakutpu.application.runtime.graph.storage import borrow_scratch
from kohakutpu.application.runtime.split import BoundGraphArtifact, NodePart
from kohakutpu.stack import rs
from kohakutpu.stack.kernel import arena_name


def bind(prepared):
    """Backends receive the same verified bytes, physical aliases and leases.

    Scratch is a lease on the eager windows, not a second graph allocation.
    The compiler's lifetime joins make the borrowed offsets safe within the
    graph; backend draining/collection protects them between submissions.
    """
    dev, compiled = prepared.device, prepared.compiled
    full = dict(prepared.bindings)
    references = []
    try:
        physical_ports = {name: dev.mmu.resolve(at)[1] for name, at in full.items()}
        if prepared.environment is not None and physical_ports != prepared.environment:
            raise ValueError(
                "bound graph differs from its lowered physical alias environment"
            )
        full.update(borrow_scratch(dev, compiled, full, prepared.scratch_limits))
        for arena in compiled.scratch or ():
            base = full[arena_name(arena)]
            for name, offset in arena.get("members", ()):
                full.setdefault(name, base + offset)
        # A standalone preparation may have just established a window (zero
        # pins); a recording may instead name released-but-pinned allocations.
        # Extend each physical allocation exactly once using its ownership state.
        retained = set()
        for address in set(full.values()):
            entry, _ = dev.mmu.resolve(address)
            ref = (entry.arena, entry.physical)
            if ref not in retained:
                references.extend(
                    dev._retain(
                        [address], pinned=entry.physical in entry.arena.released
                    )
                )
                retained.add(ref)
        physical = {name: dev.mmu.resolve(address)[1] for name, address in full.items()}
        if unaligned(physical, burst_line(dev.card.machine_json), compiled.line_ports):
            raise ValueError("bound graph violates its mover line-alignment proof")
        if burst_unaligned(
            physical, cluster_burst(dev.card.machine_json), compiled.burst_ports
        ):
            raise ValueError("bound graph violates its cluster burst-alignment proof")
        with rs.compiler_owner(dev):
            programs = compiled.programs(
                physical if compiled.bound is not None else full, dev.home
            )
        if compiled.bound is None:
            programs = {
                node: replace(p, translations=dev.mmu.translations_for(p.bindings))
                for node, p in programs.items()
            }
        programs = B.placed(programs, dev.card.nodes)
        B.findings(programs, "refuse")
        if compiled.bound is None and any(
            not p.matches_environment(dev.mmu) for p in programs.values()
        ):
            raise ValueError("bound graph differs from its certified alias environment")
        parts = []
        for node, program in programs.items():
            raw = program.bound(True)
            B.admit(raw, dev.card.machine_json, node, cached=dev.card.admission_cached)
            parts.append(NodePart(node, raw))
        identity = []
        for name, address in sorted(full.items()):
            entry, actual = dev.mmu.resolve(address)
            identity.append(
                (
                    name,
                    entry.virtual,
                    entry.physical,
                    entry.extent,
                    entry.generation,
                    actual,
                )
            )
        artifact = BoundGraphArtifact(
            hashlib.blake2b(dev.card.machine_json.encode(), digest_size=32).hexdigest(),
            tuple(parts),
            json.dumps(identity).encode(),
            json.dumps(
                {"dependencies": prepared.dependencies, "events": prepared.events},
                sort_keys=True,
            ).encode(),
        )
        prepared.references.extend(references)
        prepared.bound_references = references
        prepared.bound_bindings = full
        return artifact
    except BaseException:
        for arena, address in references:
            arena.unretain(address)
        dev._release_windows()
        raise
