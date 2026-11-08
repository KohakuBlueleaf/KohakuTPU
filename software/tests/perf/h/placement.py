"""The buffers a compiled probe owns, including compiler scratch arenas."""

from kohakutpu.application.tools import l1
from kohakutpu.stack.buffer import Buffer
from kohakutpu.stack.kernel import arena_name


def buffers(compiled, machine_json):
    layout = list(l1.bufs(compiled.l1, compiled.name, machine_json))
    for arena in compiled.scratch or ():
        space = arena["space"]
        node = arena.get("node")
        placement = f"@{space}" + ("" if node is None else f"(node={node})")
        buf = Buffer.of(f"u64[{-(-arena['bytes'] // 8)}] {placement}", machine_json)
        layout.append((arena_name(arena), buf))
    return layout
