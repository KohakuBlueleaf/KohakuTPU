"""Offline admission of two native graph members through the existing class store."""

import json

from kohakutpu.application.runtime.class_library import ClassLibrary
from kohakutpu.application.runtime.template_store import TemplateStore
from kohakutpu.stack import rs
from kohakutpu.stack.kernel import arena_name


def admit_pair(root, machine, graphs):
    sources = [json.dumps(g) for g in graphs]
    outputs = [rs._rs.compile(s, machine, cache_dir="", fetch=True) for s in sources]
    derived = rs._rs.template_derive(
        [o["packages"][0]["bytes"] for o in outputs],
        json.dumps([{"member": 0}, {"member": 1}]),
        json.dumps({"member": {"lo": 0, "hi": 1}}),
        machine,
    )
    stages = {
        name
        for a in outputs[0]["scratch"]
        if a["space"] == "stage"
        for name in (arena_name(a), *(n for n, _ in a["members"]))
    }
    bindings = [
        (
            rs._rs.stage_addr(machine, 0x100000 + i * 0x10000, 0)
            if n in stages
            else 0x10000000 + i * 0x1000000
        )
        for i, n in enumerate(outputs[0]["packages"][0]["params"])
    ]
    table = derived["template_json"]
    identity = TemplateStore(root, machine).admit(
        sources[0], derived["bytes"], table, bindings
    )
    library = ClassLibrary(root, machine)
    for i, (source, output) in enumerate(zip(sources, outputs, strict=True)):
        library.admit(
            source,
            output,
            {0: {"source": sources[0], "table": table, "params": {"member": i}}},
            {0: bindings},
        )
    return identity, outputs, derived
