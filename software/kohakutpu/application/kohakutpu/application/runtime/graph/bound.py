"""Bind a composed graph once, retaining its node-private scratch and aliases."""

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
from kohakutpu.application.runtime.split import BoundGraphArtifact, NodePart
from kohakutpu.stack import rs
from kohakutpu.stack.buffer import Buffer
from kohakutpu.stack.kernel import arena_name


def bind(prepared):
    """The caller's prepared graph owns scratch and pins before any upload.

    Backends receive exactly these bytes; neither backend rebinds addresses,
    recompiles node ownership, nor assigns new event slots.
    """
    dev, compiled = prepared.device, prepared.compiled
    full = dict(prepared.bindings)
    arenas = compiled.scratch or ()
    allocated, references = [], []
    try:
        for arena in arenas:
            name = arena_name(arena)
            if name not in full:
                address = dev.alloc(
                    arena["bytes"],
                    arena["space"],
                    arena.get("node"),
                    owner="graph:scratch",
                )
                allocated.append(address)
                full[name] = address
            for name, offset in arena.get("members", ()):
                full.setdefault(name, full[arena_name(arena)] + offset)
        for name, spec in compiled.l1.params(compiled.name, "l1"):
            if name not in full:
                buf = Buffer.of(spec, dev.card.machine_json)
                address = dev.alloc(
                    buf.nbytes, buf.space, buf.layout.get("node"), owner="graph:scratch"
                )
                allocated.append(address)
                full[name] = address
        references = dev._retain(full.values(), pinned=True)
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
        if any(not p.matches_environment(dev.mmu) for p in programs.values()):
            raise ValueError("bound graph differs from its certified alias environment")
        parts = []
        for node, program in programs.items():
            raw = program.bound(True)
            B.admit(raw, dev.card.machine_json, node)
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
        prepared.scratch.extend(allocated)
        return artifact
    except BaseException:
        for arena, address in references:
            arena.unretain(address)
        for address in allocated:
            dev.free(address)
        raise
