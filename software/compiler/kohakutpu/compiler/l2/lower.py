"""L2 -> L1: an `fn NAME.l2` body to L1 `.ktpu` text (docs/spec/l2-program.md
§5), read back by the L1 reader.

Each `par` of units is one stage: every instance's stream is lowered, cut at
its outermost `for` iterations, and sent segment by segment across the
instances (the node interleaves units the way a tile loop does); consecutive
node-level mover ops are one posted move. Stages are then ordered by what
they touch (`sync.schedule`): a stage waits only for the units of earlier
stages it conflicts with. A `par` over nodes gives each node its own
`node N` block.
"""

from dataclasses import dataclass

from kohakutpu.compiler import target as T
from kohakutpu.compiler.l2 import body as B
from kohakutpu.compiler.l2 import cluster, mover, sync, vector
from kohakutpu.language.text import module as M
from kohakutpu.language.text.syntax import Name, View, fmt

IND = "    "
#: The top of a receiving core's L1 that clusters drain tiles into, as a
#: ring; the core's own buffers sit below it.
POOL = 256


@dataclass
class Transfer:
    """A unit-to-unit transfer: into `dst`'s `side` ("l1" words of a core,
    "A" / "B" entries of a cluster's banks) at `at`, `size` long."""

    src: str
    dst: str
    side: str
    at: int
    size: int
    shape: tuple


class Links:
    """One node's transfers: each lands where `send` allocates it (a ring at
    the top of a core's L1; a cluster bank, alternating) and is taken by the
    receiver's `recv`s in order."""

    def __init__(self, fail) -> None:
        self.fail = fail
        self.queues: dict = {}
        self.ring: dict = {}
        self.banks: dict = {}

    def send(self, st, src, dst, side, size, shape) -> Transfer:
        if side == "l1":
            if size > POOL:
                self.fail(st, f"{size} words into {dst}: a core takes {POOL} at a time")
            at = self.ring.get(dst, vector.L1_WORDS - POOL)
            if at + size > vector.L1_WORDS:
                at = vector.L1_WORDS - POOL
            self.ring[dst] = at + size
        else:
            if size > cluster.BANK:
                self.fail(st, f"{size} entries into {dst}: a bank holds {cluster.BANK}")
            bank = self.banks.get((dst, side), 0)
            self.banks[(dst, side)] = 1 - bank
            at = bank * cluster.BANK
        x = Transfer(src, dst, side, at, size, tuple(shape))
        self.queues.setdefault((src, dst, side), []).append(x)
        return x

    def take(self, st, src, dst, side) -> Transfer:
        q = self.queues.get((src, dst, side))
        if not q:
            self.fail(st, f"recv: {src} sent nothing into {dst} ({side}) before this")
        return q.pop(0)

    def check(self, st) -> None:
        for (src, dst, side), q in self.queues.items():
            if q:
                self.fail(st, f"{src} sends into {dst} ({side}) that nothing receives")


def lower(module, name: str, machine=None) -> str:
    """L1 text for `fn name.l2`: the same module's target, `fn name.l1` with
    the L2 body's parameters. `machine` (a card of enough nodes by default)
    answers where units and memory ports are."""
    body = B.read(module, name)
    fail = body.reader.fail
    stages = B.instances(body)
    nodes = sorted({n for _, n, _, _ in stages})
    machine = machine or T.machine(nodes=max(nodes) + 1)
    multi = any(isinstance(s, B.Par) and s.kind == "node" for s in body.stmts)
    land = vector.L1_WORDS - POOL if _drains_to_cores(body.stmts) else None
    out, images = [], []
    for n in nodes:
        links = Links(fail)
        reading: dict = {}
        built = []
        mine = [(k, x, tag) for k, nd, x, tag in stages if nd == n]
        for i, (kind, x, tag) in enumerate(mine):
            if kind == "par":
                lines, units, acc = _par(
                    body, x, fail, machine, images, links, land, reading
                )
                built.append(sync.Stage(lines, units, acc, tag))
                continue
            op, env = x
            if op.op not in mover.OPS:
                fail(op.st, f"no node-level L2 op {op.op!r}: {', '.join(mover.OPS)}")
            got, acc = mover.lower(body, op, env, fail)
            prev = built[-1] if built else None
            # Consecutive moves are one mover step.
            if (
                prev is not None
                and prev.post
                and prev.group == tag
                and mine[i - 1][0] == "op"
            ):
                prev.lines += [IND + ln for ln in got]
                prev.accesses += acc
                continue
            tok = f"mv{len(built)}"
            built.append(
                sync.Stage(
                    [f"{tok} = move posted", *(IND + ln for ln in got)],
                    [],
                    acc,
                    tag,
                    post=tok,
                )
            )
        links.check(body.stmts[-1].st if body.stmts else None)
        lines = sync.schedule(_close(built))
        if multi:
            out.append(f"node {n}")
            out += [IND + ln for ln in lines]
        else:
            out += lines
    src = module.body(name, "l2")
    # Scratch buffers become parameters: the caller places them (zeroed).
    scratch = [(s.target, s.annot) for s in src.stmts if s.op == "buffer"]
    params = ", ".join(f"{p}: {fmt(t)}" for p, t in [*src.params, *scratch])
    head = [f"target {module.target}"] if module.target else []
    text = "\n".join(
        [*head, "", *images, f"fn {name}.l1({params})", *(IND + ln for ln in out)]
    )
    return text + "\n"


def _close(stages: list) -> list:
    """A last mover stage is a plain `move`: the node waits for it and every
    unit, which ends the program."""
    if stages and stages[-1].post:
        last = stages[-1]
        last.lines = ["move", *last.lines[1:]]
        last.post = None
        last.closes = True
    return stages


def _drains_to_cores(stmts) -> bool:
    """Whether any cluster drains a tile into a core's L1."""
    for s in stmts:
        if isinstance(s, B.Op) and s.op == "drain":
            pos = s.st.positional()
            if len(pos) == 3 and isinstance(pos[2], View) and pos[2].name == "vc":
                return True
        elif hasattr(s, "body") and _drains_to_cores(s.body):
            return True
    return False


def _par(body, insts: list, fail, machine, images: list, links, land, reading) -> tuple:
    """``(lines, units, accesses)`` of one `par` stage."""
    kinds = {i.kind for i in insts}
    if len(kinds) != 1:
        fail(insts[0].par.st, "one par runs one unit kind")
    kind = kinds.pop()
    for i in insts:
        if i.index is None:
            fail(i.par.st, f"{kind}[any] is the place pass's to resolve first")
    if kind == "vc":
        lines, units, acc = [], [], []
        for i in insts:
            img = f"{body.name}_n{i.node}_vc{i.index}_{len(images)}"
            text, params, got = vector.lower(body, i.par, i.env, img, fail, links, land)
            images.append(text)
            acc += got
            units.append(f"vc[{i.index}]")
            lines += [f"send vc[{i.index}]", f"{IND}run {img}({', '.join(params)})"]
        return lines, units, acc
    if kind != "mg":
        fail(insts[0].par.st, f"L2 -> L1 has no {kind} lowering yet")
    insts = _merge(insts)
    into = _added_into(body, insts)
    streams = []
    for i in insts:
        unit = f"mg[{i.index}]"
        s = cluster.lower(
            body,
            i,
            fail,
            fuse=i.index not in into,
            links=links,
            reading=reading.get(unit),
        )
        reading[unit] = s.reading
        streams.append((i.index, s))
    units = [s.unit for _, s in streams]
    acc = [a for _, s in streams for a in s.accesses]
    if into:
        return _added(streams, into, fail, insts[0].par.st), units, acc
    if any(ln.share for _, s in streams for ln in s.lines):
        return _shared(insts, streams, machine, fail), units, acc
    return _interleave([(idx, s.lines) for idx, s in streams]), units, acc


def _added_into(body, insts) -> set:
    """Clusters another cluster of the stage adds its tile into
    (`drain ACC -> mg[J] add`)."""
    out = set()
    for i in insts:
        for op, env in i.items:
            if op.op != "drain" or Name("add") not in op.st.positional():
                continue
            to = B.value(body, op.st, op.st.positional()[2], env)
            if isinstance(to, View) and to.name == "mg" and to.slices:
                out.add(getattr(to.slices[0], "value", None))
    return out


def _interleave(streams: list) -> list:
    """Each stream's lines sent segment by segment, the units in turn."""
    segs = max((ln.seg for _, ls in streams for ln in ls), default=-1) + 1
    lines = []
    for seg in range(segs):
        for idx, ls in streams:
            words = [ln.render() for ln in ls if ln.seg == seg]
            if words:
                lines.append(f"send mg[{idx}]")
                lines += [IND + w for w in words]
    return lines


#: Node cycles a cluster's flits may still be in the mesh after its drain
#: retires: no unit reports a peer burst landed (H2, H3).
SETTLE = 2000


def _added(streams, into, fail, st) -> list:
    """Clusters adding their tiles into others' (`drain ACC -> mg[J] add`):
    every cluster runs up to its first drain; each addition goes once its
    target has swept (its tile is open and no sweep runs), then a settle
    covers the flits in flight; the rest follows."""
    heads, tails, adds = [], [], []
    for idx, s in streams:
        cut = next((k for k, ln in enumerate(s.lines) if ln.drain), len(s.lines))
        line = s.lines[cut] if cut < len(s.lines) else None
        if line is not None and line.into is not None:
            adds.append((idx, line))
            cut_tail = cut + 1
        else:
            cut_tail = cut
        if idx in into and (line is None or line.into is not None):
            fail(st, f"mg[{idx}] is added into but drains nothing after")
        heads.append((idx, s.lines[:cut]))
        tails.append((idx, s.lines[cut_tail:]))
    out = _interleave(heads)
    for src, line in adds:
        out.append(f"wait mg[{line.into}]")
        out += [f"send mg[{src}]", IND + line.render()]
        out.append(f"wait mg[{src}]")
    out.append(f"settle {SETTLE}")
    return out + _interleave(tails)


def _merge(insts: list) -> list:
    """Instances placed on one unit as one stream, in instance order: the
    unit runs them one after another, its banks carried across."""
    by: dict = {}
    for i in insts:
        if i.index not in by:
            by[i.index] = B.Instance(i.kind, i.index, i.node, i.env, i.par)
        m = by[i.index]
        base = len(m.items)
        m.cuts += [base + c for c in (i.cuts or [0])]
        m.items += i.items
    return list(by.values())


def _steps(lines: list) -> list:
    """A stream cut before each shared fill: step 0 holds what precedes the
    first; a shared fill's runs (consecutive lines of one key) open one step."""
    steps, prev = [[]], None
    for ln in lines:
        if ln.share and ln.key != prev:
            steps.append([])
        prev = ln.key if ln.share else None
        steps[-1].append(ln)
    return steps


def _groups(insts, mode: str, machine, node: int) -> list:
    """Instances sharing one read: ``[(leader, [followers])]``. `port`: the
    clusters on one memory port; `all`: every instance; `cross`: one of each
    port in turn. The leader is the lowest {y, x} (`mx_cluster_cu.v` `lead`)."""
    coords = sorted(machine.coords("MG", node))
    where = {i.index: coords[i.index] for i in insts}
    if mode == "all":
        sets = [[i.index for i in insts]]
    else:
        by_port: dict = {}
        for i in insts:
            by_port.setdefault(machine.nearest_port(where[i.index], node), []).append(
                i.index
            )
        ports = [by_port[p] for p in sorted(by_port)]
        if mode == "port" or len(ports) < 2:
            sets = ports
        else:
            sets = [list(s) for s in zip(*ports, strict=True)]
    out = []
    for s in sets:
        s = sorted(s, key=lambda u: (where[u][1] << 4) | where[u][0])
        out.append((s[0], s[1:]))
    return out


def _shared(insts, streams, machine, fail) -> list:
    """The node as the rendezvous of shared fills: per step, each follower
    gets its own fills of the step, a mark, then the shared fill and the rest;
    each leader, once its followers' marks complete, the shared fill (with
    peers: one read feeds the group) and the rest."""
    node = insts[0].node
    cut = {idx: _steps(s.lines) for idx, s in streams}
    keys = {idx: [st[0].key for st in steps[1:]] for idx, steps in cut.items()}
    first = next(iter(keys.values()))
    if any(k != first for k in keys.values()):
        fail(
            insts[0].par.st,
            "share=: every instance fills the same shared views, in the same order",
        )
    modes = {ln.share for _, s in streams for ln in s.lines if ln.share}
    if len(modes) != 1:
        fail(insts[0].par.st, "share=: one mode per par")
    groups = _groups(insts, modes.pop(), machine, node)
    peers = {}
    for lead, follow in groups:
        members = [lead, *follow]
        for u in members:
            peers[u] = tuple(p for p in members if p != u)
    for idx, steps in cut.items():
        for step in steps[1:]:
            for ln in step:
                if ln.share:
                    ln.peers = peers[idx]
    out = []

    def send(idx, lines):
        if lines:
            out.append(f"send mg[{idx}]")
            out.extend(IND + ln.render() for ln in lines)

    for idx, steps in cut.items():
        send(idx, steps[0])
    for j in range(1, len(first) + 1):
        for lead, follow in groups:
            for f in follow:
                step = cut[f][j]
                # The follower's own fills of the step go first: once they
                # complete it waits in the shared fill (in-order sequencer).
                own = [ln for ln in step if ln.fill and not ln.share]
                rest = [ln for ln in step if ln not in own]
                send(f, own)
                out.append(f"t{f} = mark mg[{f}]")
                send(f, rest)
        for lead, follow in groups:
            for f in follow:
                out.append(f"wait mg[{f}] upto=t{f}")
            send(lead, cut[lead][j])
    return out


def compile_l1(module, name: str):
    """The L1 module `lower` writes, read back (`kohakutpu.language.text.module`)."""
    return M.read(lower(module, name), f"{module.file}:{name}.l1")


__all__ = ["POOL", "Links", "Transfer", "compile_l1", "lower"]
