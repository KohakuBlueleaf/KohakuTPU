"""Capacity-gated private scratch placement shared with TGC."""

import json
from math import lcm

from kohakutpu.language.dsl import TensorSpec
from kohakutpu.stack.buffer import Buffer


def scratch_bytes(port, machine):
    spec = port["spec"]
    layout = {k: next(iter(v.values())) for k, v in spec["layout"]}
    tensor = TensorSpec.make(spec["shape"], spec["dtype"], layout, spec["space"])
    return Buffer.of(tensor.text(), machine).nbytes


def stage_alignment(machine):
    facts = json.loads(machine)["machine"]
    return lcm(
        256,
        facts["card"]["dram"]["line_bytes"],
        facts["mg"]["wburst"] * facts["die"]["word_bytes"],
    )


def stage_graph(graph, machine, budget):
    """Relocate private DRAM scratch only when its whole footprint fits."""
    scratch = graph["scratch"]
    align = stage_alignment(machine)
    need = sum(-(-scratch_bytes(port, machine) // align) * align for port in scratch)
    if need > budget or not any(p["spec"]["space"] == "dram" for p in scratch):
        return graph
    return dict(
        graph,
        scratch=[
            (
                dict(port, spec=dict(port["spec"], space="stage"))
                if port["spec"]["space"] == "dram"
                else port
            )
            for port in scratch
        ],
    )
