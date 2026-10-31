"""Native neighbour templates composed before Rust lowering, with stage-local edges.

The existing generators define arithmetic and rounding. Composition namespaces their
IR, removes the private external port, and puts that edge in explicitly allocated
node scratchpad. It does not concatenate already lowered packages.
"""

import json
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from itertools import accumulate

from kohakutpu.application.runtime.class_library import ClassLibrary
from kohakutpu.application.runtime.graph.storage import stage_alignment, stage_graph
from kohakutpu.application.runtime.template_store import TemplateStore
from kohakutpu.language.dsl import TensorSpec
from kohakutpu.stack import rs
from kohakutpu.stack.buffer import Buffer
from kohakutpu.stack.kernel import arena_name


@dataclass
class NativeLaunch:
    graph: dict
    bindings: dict
    kind: str
    members: tuple = ()


def statements(body):
    for statement in body:
        yield statement
        yield from statements(statement.get("body", ()))


def names(graph, prefix):
    result = {
        p["name"]: prefix + p["name"] for p in (*graph["ports"], *graph["scratch"])
    }
    for statement in statements(graph["body"]):
        for field in ("var", "target"):
            if field in statement:
                result[statement[field]] = prefix + statement[field]
    return result


def renamed(value, mapping, field=""):
    if isinstance(value, dict):
        return {k: renamed(v, mapping, k) for k, v in value.items()}
    if isinstance(value, list):
        return [renamed(v, mapping, field) for v in value]
    if isinstance(value, str) and field in (
        "buffer",
        "var",
        "target",
        "value",
        "bcast",
        "src",
        "name",
    ):
        return mapping.get(value, value)
    return value


def port_bytes(port, machine):
    spec = port["spec"]
    layout = {k: next(iter(v.values())) for k, v in spec["layout"]}
    tensor = TensorSpec.make(spec["shape"], spec["dtype"], layout, spec["space"])
    return Buffer.of(tensor.text(), machine).nbytes


def compose(left, right, output, inputs, space="stage"):
    """Caller establishes exclusive ownership and identical edge port types."""
    lm, rm = names(left.graph, "p_"), names(right.graph, "c_")
    internal = lm[output]
    rm.update(dict.fromkeys(inputs, internal))
    a, b = renamed(left.graph, lm), renamed(right.graph, rm)
    intermediate = next(p for p in a["ports"] if p["name"] == internal)
    spec = dict(intermediate["spec"], space=space)
    graph = {
        "format": a["format"],
        "version": a["version"],
        "name": "composed_neighbours",
        "target": a["target"],
        "machine": a["machine"],
        "consts": {},
        "ports": [p for p in (*a["ports"], *b["ports"]) if p["name"] != internal],
        "scratch": [*a["scratch"], {"name": internal, "spec": spec}, *b["scratch"]],
        "body": a["body"] + b["body"],
    }
    lp = {p["name"] for p in left.graph["ports"]}
    rp = {p["name"] for p in right.graph["ports"]}
    bindings = {lm[k]: v for k, v in left.bindings.items() if k in lp and k != output}
    bindings.update(
        {rm[k]: v for k, v in right.bindings.items() if k in rp and k not in inputs}
    )
    return NativeLaunch(graph, bindings, right.kind, left.members + right.members)


def copy_source(launch):
    """An identity read-map over a full pure copy; other layout maps stay explicit."""
    graph = launch.graph
    if graph["scratch"] or len(graph["body"]) != 1 or len(graph["ports"]) != 2:
        return None
    node = graph["body"][0]
    if node.get("op") != "copy" or node.get("flags") or node.get("kw"):
        return None
    if any(axis != "all" for key in ("src", "dst") for axis in node[key]["axes"]):
        return None
    ports = {p["name"]: p for p in graph["ports"]}
    src, dst = ports[node["src"]["buffer"]], ports[node["dst"]["buffer"]]
    if src["role"] != "in" or dst["role"] != "out" or src["spec"] != dst["spec"]:
        return None
    return src["name"]


def fuse(
    launches,
    exports,
    machine,
    *,
    quant=True,
    elementwise=True,
    norm=False,
    layout=False,
    stage_bytes,
    recursive=False,
    producer_quant=False,
):
    """Bounded pairs; liveness includes partial aliases and all exported spans."""
    align = stage_alignment(machine)
    accesses = []
    for i, launch in enumerate(launches):
        for port in launch.graph["ports"]:
            at = launch.bindings[port["name"]]
            accesses.append((i, at, at + port_bytes(port, machine)))
    accesses.extend((-1, at, at + size) for at, size in exports)
    accesses.sort(key=lambda entry: entry[1])
    starts = [lo for _, lo, _ in accesses]
    ends = list(accumulate((hi for _, _, hi in accesses), max))
    result, rules = [], []
    i = 0
    while i < len(launches):
        left = launches[i]
        if i + 1 < len(launches):
            right = launches[i + 1]
            outputs = [p for p in left.graph["ports"] if p["role"] == "out"]
            rule = None
            source = copy_source(left) if layout else None
            reductions = any(
                s.get("op") in ("rsum", "rmax", "rmin")
                for s in statements(left.graph["body"])
            )
            if source is not None:
                rule = "layout"
            elif norm and reductions and right.kind in ("dsl", "quant", "matmul"):
                rule = "norm"
            elif quant and (
                (left.kind == "quant" and right.kind == "matmul")
                or (producer_quant and right.kind == "quant")
            ):
                rule = "quant"
            elif (
                elementwise
                and right.kind == "dsl"
                and all(
                    st.get("op") not in ("rsum", "rmax", "rmin", "gemm", "quant")
                    for st in statements(right.graph["body"])
                )
            ):
                rule = "elementwise"
            if rule and len(outputs) == 1:
                output = outputs[0]
                at = left.bindings[output["name"]]
                end = at + port_bytes(output, machine)
                linked = [
                    p for p in right.graph["ports"] if right.bindings[p["name"]] == at
                ]
                exclusive = all(
                    owner in (i, i + 1)
                    for owner, lo, hi in accesses[
                        bisect_right(ends, at) : bisect_left(starts, end)
                    ]
                    if at < hi and lo < end
                )
                exact = linked and all(
                    p["role"] == "in" and p["spec"] == output["spec"] for p in linked
                )
                partial = any(
                    at < right.bindings[p["name"]] + port_bytes(p, machine)
                    and right.bindings[p["name"]] < end
                    and p not in linked
                    for p in right.graph["ports"]
                )
                inplace = any(
                    p["role"] == "in"
                    and at < left.bindings[p["name"]] + port_bytes(p, machine)
                    and left.bindings[p["name"]] < end
                    for p in left.graph["ports"]
                )
                stage_need = sum(
                    -(-port_bytes(scratch, machine) // align) * align
                    for scratch in (*left.graph["scratch"], *right.graph["scratch"])
                    if scratch["spec"]["space"] == "stage"
                ) + (0 if rule == "layout" else -(-(end - at) // align) * align)
                if (
                    exclusive
                    and exact
                    and not partial
                    and not inplace
                    and stage_need <= stage_bytes
                ):
                    if rule == "layout":
                        src = left.bindings[source]
                        size = port_bytes(
                            next(p for p in left.graph["ports"] if p["name"] == source),
                            machine,
                        )
                        aliases = any(
                            p["role"] == "out"
                            and src < right.bindings[p["name"]] + port_bytes(p, machine)
                            and right.bindings[p["name"]] < src + size
                            for p in right.graph["ports"]
                        )
                        if aliases:
                            result.append(left)
                            i += 1
                            continue
                        bindings = dict(right.bindings)
                        bindings.update({p["name"]: src for p in linked})
                        result.append(
                            NativeLaunch(
                                right.graph,
                                bindings,
                                right.kind,
                                left.members + right.members,
                            )
                        )
                    else:
                        result.append(
                            compose(
                                left,
                                right,
                                output["name"],
                                tuple(p["name"] for p in linked),
                            )
                        )
                    rules.append(rule)
                    i += 2
                    continue
        result.append(left)
        i += 1
    if recursive and rules:
        result, more = fuse(
            result,
            exports,
            machine,
            quant=quant,
            elementwise=elementwise,
            norm=norm,
            layout=layout,
            stage_bytes=stage_bytes,
            recursive=True,
            producer_quant=producer_quant,
        )
        rules.extend(more)
    return result, rules


def local_scratch(launch, machine, stage_bytes):
    """Place bounded private generator temporaries in node staging, not DRAM.

    No lifetime change and no arithmetic rewrite; larger kernels keep DRAM.
    This is a capacity-gated allocation choice, not a tile pipeline.
    """
    graph = stage_graph(launch.graph, machine, stage_bytes)
    if graph is launch.graph:
        return launch
    return NativeLaunch(graph, launch.bindings, launch.kind, launch.members)


def admit_pair(root, machine, sources, outputs):
    """One certified native binary for a two-member shape class, then exact dispatch.

    The selector is a finite-domain shape parameter, not an extrapolated dynamic
    shape proof. Native relocation is byte-proved against both direct members.
    """
    params = [{"member": i} for i in range(2)]
    derived = rs._rs.template_derive(
        [o["packages"][0]["bytes"] for o in outputs],
        json.dumps(params),
        json.dumps({"member": {"lo": 0, "hi": 1}}),
        machine,
    )
    raw, table = derived["bytes"], derived["template_json"]
    stage_names = {
        name
        for arena in outputs[0]["scratch"]
        if arena["space"] == "stage"
        for name in (arena_name(arena), *(n for n, _ in arena["members"]))
    }
    bindings = [
        (
            rs._rs.stage_addr(machine, 0x100000 + i * 0x10000, 0)
            if name in stage_names
            else 0x10000000 + i * 0x1000000
        )
        for i, name in enumerate(outputs[0]["packages"][0]["params"])
    ]
    store = TemplateStore(root, machine)
    identity = store.admit(sources[0], raw, table, bindings)
    library = ClassLibrary(root, machine)
    for source, output, parameter in zip(sources, outputs, params, strict=True):
        library.admit(
            source,
            output,
            {0: {"source": sources[0], "table": table, "params": parameter}},
            {0: bindings},
        )
    return identity, table
