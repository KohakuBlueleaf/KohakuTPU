"""Receiver-scoped doorbell resources used by immutable run packages."""

from functools import lru_cache

from kohakutpu.stack.rs import _rs


@lru_cache(maxsize=4096)
def package_slots(raw, machine, node):
    """A ring and its consume own the same receiver/slot resource until completion."""
    opcodes = _rs.package_format(machine, node)["op"]
    result = set()
    for op, _flags, unit, count, _arg in _rs.package_read(raw, machine, node)["steps"]:
        if op == opcodes["RING"]:
            result.add((unit, count))
        elif op == opcodes["WAIT_SLOT"]:
            result.add((node, unit))
    return frozenset(result)


def launch_slots(programs, nodes):
    return frozenset(
        resource
        for node, program in programs.items()
        for resource in package_slots(
            program.package, program.machine_json, nodes[0] if node is None else node
        )
    )
