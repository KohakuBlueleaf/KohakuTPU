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
    "A" / "B" entries of a cluster's banks) at `at`, `size` long; `flow` when
    it belongs to a stream."""

    src: str
    dst: str
    side: str
    at: int
    size: int
    shape: tuple
    flow: bool = False
    #: a stream sent a trip at a time: each trip's landing by parity
    slots: tuple = ()
    per: int = 0


#: A stream's ring holds two receiver transfers: the top of a core's L1 (at
#: most `POOL` words) or a cluster side's banks from entry 0 (at most both).
RING_ENTRIES = 2 * cluster.BANK


@dataclass
class Ring:
    """A stream from `src` into `dst`'s `side`: transfers land one after
    another in [lo, lo + size) and wrap; the receiver returns a transfer's
    room as credits once it has read it, the sender's bursts wait for them,
    and the receiver counts what lands."""

    src: str
    dst: str
    side: str
    lo: int
    size: int
    sent: int = 0
    taken: int = 0

    def granules(self) -> int:
        """The ring's room in credits (32-byte granules)."""
        return self.size if self.side == "l1" else 4 * self.size


class Links:
    """One node's transfers: each lands where `send` allocates it (a ring at
    the top of a core's L1; a cluster bank, alternating) and is taken by the
    receiver's `recv`s in order. A pair whose receiver takes inside a loop is
    a stream (`rings`): its transfers follow one another around a ring."""

    def __init__(self, fail, streams=()) -> None:
        self.fail = fail
        self.queues: dict = {}
        self.ring: dict = {}
        self.banks: dict = {}
        self.rings: dict = {}
        for (src, dst, side), size in dict(streams).items():
            lo = vector.L1_WORDS - size if side == "l1" else 0
            self.rings[(src, dst, side)] = Ring(src, dst, side, lo, size)

    def stream(self, src, dst, side) -> Ring | None:
        return self.rings.get((src, dst, side))

    def send(self, st, src, dst, side, size, shape) -> Transfer:
        r = self.stream(src, dst, side)
        if r is not None:
            if r.size % size:
                self.fail(st, f"{size} into {dst}'s ring of {r.size}: whole transfers")
            at = r.lo + r.sent % r.size
            r.sent += size
            x = Transfer(src, dst, side, at, size, tuple(shape), flow=True)
            self.queues.setdefault((src, dst, side), []).append(x)
            return x
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

    def take_stream(self, st, src, dst, side, size) -> Transfer:
        """`size` units of the stream, from the oldest transfers not yet
        taken (several make one take when the sender sends less at a time)."""
        r = self.stream(src, dst, side)
        q = self.queues.get((src, dst, side)) or []
        first, got = None, 0
        while got < size:
            if not q:
                self.fail(st, f"recv: {src} sent {got} of {size} into {dst} ({side})")
            x = q.pop(0)
            if x.at != r.lo + (r.taken + got) % r.size:
                self.fail(st, f"recv: {src}'s transfers into {dst} are out of order")
            first = first or x
            got += x.size
        if got != size:
            self.fail(st, f"recv of {size} splits {src}'s transfer into {dst}")
        r.taken += size
        return Transfer(src, dst, side, first.at, size, first.shape, flow=True)

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
    phases: list = []
    stages = [(*s, ph) for s, ph in zip(B.instances(body, phases=phases), phases)]
    nodes = sorted({n for _, n, _, _, _ in stages})
    machine = machine or T.machine(nodes=max(nodes) + 1)
    # One latency a body, so every instance of a stage shares its image.
    cores = [
        i.par for k, _, x, _, _ in stages if k == "par" for i in x if i.kind == "vc"
    ]
    body.fill = T.mem(
        machine, shared=any(_beside_clusters(body.stmts, p) for p in cores)
    )
    multi = any(isinstance(s, B.Par) and s.kind == "node" for s in body.stmts)
    land = vector.L1_WORDS - POOL if _drains_to_cores(body.stmts) else None
    remote = _remote_adds(body, stages)
    out, images, built_by = [], [], {}
    for n in nodes:
        mine = [(k, x, tag, ph) for k, nd, x, tag, ph in stages if nd == n]
        links = Links(fail, _streams(body, [(k, x, t) for k, x, t, _ in mine]))
        reading: dict = {}
        built = []
        if any(r.dst.startswith("vc") for r in links.rings.values()):
            built.append(_open(body.name, n, links, images))
        for i, (kind, x, tag, ph) in enumerate(mine):
            if kind == "par":
                lines, units, acc, segs = _par(
                    body,
                    x,
                    fail,
                    machine,
                    images,
                    links,
                    land,
                    reading,
                    remote.get((n, _stage_key(body, x)), ()),
                )
                built.append(sync.Stage(lines, units, acc, tag, segs=segs, phase=ph))
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
                    phase=ph,
                )
            )
        links.check(body.stmts[-1].st if body.stmts else None)
        if any(r.dst.startswith("vc") for r in links.rings.values()):
            built.append(_spend(body.name, n, links, images))
        built_by[n] = built
    if len(nodes) > 1:
        _order_nodes(built_by, fail, body.stmts[0].st if body.stmts else None)
    for n in nodes:
        lines = sync.schedule(_close(_grouped(built_by[n])))
        if multi:
            out.append(f"node {n}")
            out += [IND + ln for ln in lines]
        else:
            out += lines
    src = module.body(name, "l2")
    # Scratch buffers become parameters: the caller places them (zeroed).
    scratch = [(s.target, s.annot) for s in src.stmts if s.op == "buffer"]
    params = ", ".join(f"{p}: {fmt(t)}" for p, t in [*src.params, *scratch])
    head = [module.target_line()] if module.target else []
    text = "\n".join(
        [*head, "", *images, f"fn {name}.l1({params})", *(IND + ln for ln in out)]
    )
    return text + "\n"


def _streams(body, stages) -> dict:
    """``{(src, dst, side): ring size}`` of every pair whose receiver takes
    inside a loop of its instance: a stream. Into a core, two of the
    receiver's tiles at the top of its L1; into a cluster, both banks."""
    out = {}
    for kind, x, _ in stages:
        if kind != "par":
            continue
        for inst in x:
            first = inst.cuts[0] if inst.cuts else None
            for i, (op, env) in enumerate(inst.items):
                if not isinstance(op, B.Op) or op.op != "recv" or first is None:
                    continue
                if i < first:
                    continue
                src = B.value(body, op.st, op.st.positional()[0], env)
                if not isinstance(src, View) or len(src.slices) != 1:
                    continue
                me = f"{inst.kind}[{inst.index}]"
                side = "l1" if inst.kind == "vc" else op.type.space
                key = (f"{src.name}[{src.slices[0].value}]", me, side)
                if side == "l1":
                    size = 2 * op.type.shape[0] * op.type.shape[1] // 16
                    if size > POOL:
                        body.reader.fail(
                            op.st, f"two of these tiles exceed {POOL} words"
                        )
                else:
                    # A cluster rejects a burst past its L1 (no wrap): two
                    # slots the sender alternates between.
                    size = 2 * (op.type.shape[0] // 4) * (op.type.shape[1] // 32)
                    if size > RING_ENTRIES:
                        body.reader.fail(
                            op.st, "two of these tiles exceed a side's banks"
                        )
                out.setdefault(key, size)
    return out


#: The largest credit count one POST / VPOST / VWAIT word carries here.
CREDIT_WORD = 512


def _counts(total: int) -> list:
    return [min(CREDIT_WORD, total - k) for k in range(0, total, CREDIT_WORD)]


def _open(name, node, links, images) -> sync.Stage:
    """Each stream into a core: the core hands its whole ring to the sender as
    credits. (A cluster returns its own per stage: `cluster.lower`.)"""
    lines, units, posts = [], [], {}
    for r in links.rings.values():
        if r.dst.startswith("vc"):
            posts.setdefault(r.dst, []).append(r)
    for dst, rings in posts.items():
        img = f"{name}_n{node}_open_{dst.replace('[', '').replace(']', '')}"
        words = [
            f"    vpost {r.src} n={c}" for r in rings for c in _counts(r.granules())
        ]
        images.append("\n".join([f"image {img}()", *words, "    halt"]) + "\n")
        lines += [f"send {dst}", f"{IND}run {img}()"]
        units.append(dst)
    return sync.Stage(lines, units, [])


def _spend(name, node, links, images) -> sync.Stage:
    """Each stream into a core: the sending cluster spends the credits the core
    returned for the ring's last lap, so the next run starts with none, by
    drains into the ring (nothing reads it again)."""
    lines, units, spends = [], [], {}
    for r in links.rings.values():
        if r.dst.startswith("vc"):
            spends.setdefault(r.src, []).append(r)
    for src, rings in spends.items():
        lines.append(f"send {src}")
        for r in rings:
            for c in _counts(r.granules()):
                lines += [
                    f"{IND}drain n={min(c, 128)} to={r.dst} l1={r.lo} credit"
                    for _ in range(0, c, 128)
                ]
        units.append(src)
    return sync.Stage(lines, units, [])


def _rings_of(stage) -> set:
    return {a.key for a in stage.accesses if a.flow}


def _grouped(stages: list) -> list:
    """Consecutive stages joined by a stream as one stage: a unit's queue
    holds a bounded number of words, so a producer's words waiting on credits
    must not fill it before the consumer's are sent. The cores' RUNs go first;
    the clusters' segments follow interleaved by progress (each next from the
    stage least far through its own)."""
    out = []
    for s in stages:
        prev = out[-1] if out else None
        if (
            prev is not None
            and prev.segs
            and s.segs
            and prev.group == s.group
            and _rings_of(prev) & _rings_of(s)
        ):
            prev.members.append(s)
            prev.accesses = prev.accesses + s.accesses
            prev.units = list(dict.fromkeys(prev.units + s.units))
            continue
        s.members = [s]
        out.append(s)
    for s in out:
        if len(s.members) > 1:
            s.lines = _merge_segs([m.segs for m in s.members])
    return out


def _merge_segs(groups: list) -> list:
    runs = [
        g
        for g in groups
        if all(ln.lstrip().startswith(("send vc", "run ")) for seg in g for ln in seg)
    ]
    rest = [g for g in groups if g not in runs]
    lines = [ln for g in runs for seg in g for ln in seg]
    at = [0] * len(rest)
    while any(at[k] < len(g) for k, g in enumerate(rest)):
        k = min(
            (k for k, g in enumerate(rest) if at[k] < len(g)),
            key=lambda k: at[k] / len(rest[k]),
        )
        lines += rest[k][at[k]]
        at[k] += 1
    return lines


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


def _beside_clusters(stmts, par, loop=None) -> bool:
    """Whether vector stage `par` can run while clusters stream: a node loop
    holds it and a cluster stage (the pipeline pass overlaps the iterations),
    or a loop holds it and clusters drain into the cores' L1."""
    for s in stmts:
        if s is par:
            return loop is not None and (
                _has_cluster_par(loop.body) or _drains_to_cores(loop.body)
            )
        if isinstance(s, B.For) and loop is None:
            if _beside_clusters(s.body, par, s):
                return True
        elif hasattr(s, "body") and s.body and _beside_clusters(s.body, par, loop):
            return True
    return False


def _has_cluster_par(stmts) -> bool:
    return any(
        (isinstance(s, B.Par) and s.kind == "mg")
        or (hasattr(s, "body") and s.body and _has_cluster_par(s.body))
        for s in stmts
    )


def _shared_image(images: list, text: str, name: str) -> str:
    """The name of an image already kept whose body is `text`'s (instances
    of one shape differ only in their run's arguments), else `text` kept."""
    head, _, rest = text.partition("\n")
    args = head[head.index("(") :]
    for kept in images:
        h, _, r = kept.partition("\n")
        if r == rest and h.endswith(args):
            return h[len("image ") : h.index("(")]
    images.append(text)
    return name


def _par(
    body, insts: list, fail, machine, images: list, links, land, reading, remote=()
) -> tuple:
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
            if vector.idle(body, i.par, i.env):
                continue
            img = f"{body.name}_n{i.node}_vc{i.index}_{len(images)}"
            text, params, got = vector.lower(body, i.par, i.env, img, fail, links, land)
            img = _shared_image(images, text, img)
            acc += got
            units.append(f"vc[{i.index}]")
            lines += [f"send vc[{i.index}]", f"{IND}run {img}({', '.join(params)})"]
        return lines, units, acc, [lines]
    if kind != "mg":
        fail(insts[0].par.st, f"L2 -> L1 has no {kind} lowering yet")
    insts = _merge(insts)
    into = _added_into(body, insts, remote)
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
    sends_far = any(ln.far is not None for _, s in streams for ln in s.lines)
    if into or sends_far:
        lines = _added(streams, into, fail, insts[0].par.st, remote, insts[0].node)
        return lines, units, acc, [lines]
    if any(ln.share for _, s in streams for ln in s.lines):
        lines = _shared(insts, streams, machine, fail)
        return lines, units, acc, [lines]
    segs = _segments([(idx, s.lines) for idx, s in streams])
    return [ln for seg in segs for ln in seg], units, acc, segs


def _adds(body, insts) -> list:
    """``(source node, source index, target node, target index)`` of every
    accumulator add of the stage (`drain ACC -> mg[J] add [node=M]`)."""
    out = []
    for i in insts:
        if i.kind != "mg":
            continue
        for op, env in i.items:
            if (
                getattr(op, "op", None) != "drain"
                or Name("add") not in op.st.positional()
            ):
                continue
            to = B.value(body, op.st, op.st.positional()[2], env)
            if not (isinstance(to, View) and to.name == "mg" and to.slices):
                continue
            kw = op.st.kwargs()
            far = B.const(body, op.st, kw["node"], env) if "node" in kw else i.node
            out.append((i.node, i.index, far, getattr(to.slices[0], "value", None)))
    return out


def _added_into(body, insts, remote=()) -> set:
    """Clusters of this node another cluster adds its tile into, here or from
    another node (`remote`: ``(source node, target index)``)."""
    out = {t for s, _, n, t in _adds(body, insts) if n == s}
    return out | {t for _, t in remote}


#: Signal slots of the cross-node handshakes: `ORDER + n` counts node n's
#: memory releases, `ADD + n` node n's opened accumulator tiles.
ORDER, ADD = 0, 8


def _node_vars(stmts) -> set:
    out = set()
    for s in stmts:
        if isinstance(s, B.Par) and s.kind == "node":
            out.add(s.var)
        if hasattr(s, "body") and s.body:
            out |= _node_vars(s.body)
    return out


def _stage_key(body, insts) -> tuple:
    """The same `par` run on every node: its statement and the loop values
    around it, the node's own left out."""
    skip = _node_vars(body.stmts) | {insts[0].par.var}
    env = tuple(sorted((k, repr(v)) for k, v in insts[0].env.items() if k not in skip))
    return id(insts[0].par), env


def _remote_adds(body, stages) -> dict:
    """``{(target node, stage key): [(source node, target index), ...]}`` of
    every accumulator add across nodes; refused when the target node runs no
    instance of that stage (its tile would never open)."""
    have = {(n, _stage_key(body, x)) for k, n, x, _, _ in stages if k == "par"}
    out: dict = {}
    for k, n, x, _, _ in stages:
        if k != "par":
            continue
        for s, _, t, j in _adds(body, x):
            if t == s:
                continue
            key = (t, _stage_key(body, x))
            if key not in have:
                body.reader.fail(
                    x[0].par.st,
                    f"node {s} adds into node {t}, which runs no instance of this par",
                )
            out.setdefault(key, []).append((s, j))
    return out


def _order_nodes(built_by: dict, fail, st) -> None:
    """Cross-node order through the shared memory: where a stage reads or
    writes what another node's stage of an earlier phase wrote (or read and
    this one writes), the earlier node waits for those units and rings this
    node's slot `ORDER + itself`; this node waits on it first. Stages of one
    phase run concurrently on their nodes, as the units of one `par` do."""
    mem = {
        n: [[a for a in s.accesses if a.key[0] == "mem"] for s in built]
        for n, built in built_by.items()
    }
    rings: dict = {}  # (node, stage) -> [(to node, accesses)]
    waits: dict = {}  # (node, stage) -> [from node]
    for b, built in built_by.items():
        seen: dict = {}  # source node -> latest stage of it already waited for
        for j, sj in enumerate(built):
            for a, other in built_by.items():
                if a == b:
                    continue
                hit = None
                for i, si in enumerate(other):
                    if si.phase < 0 or sj.phase < 0:
                        continue
                    if si.phase >= sj.phase:
                        continue
                    if any(sync.clash(x, y) for x in mem[a][i] for y in mem[b][j]):
                        hit = i
                if hit is None or seen.get(a, -1) >= hit:
                    continue
                seen[a] = hit
                rings.setdefault((a, hit), []).append((b, mem[a][hit]))
                waits.setdefault((b, j), []).append(a)
    for n, built in built_by.items():
        out = []
        for j, s in enumerate(built):
            got = waits.get((n, j))
            if got:
                lines = [f"wait slot {ORDER + a} n=1 consume" for a in got]
                out.append(sync.Stage(lines, [], [], fence=True, phase=s.phase))
            out.append(s)
            to = rings.get((n, j))
            if to:
                lines = [f"ring mesh={b} slot={ORDER + n}" for b, _ in to]
                acc = [
                    sync.Access(sync.FENCE, True, x.key, x.lo, x.hi)
                    for _, xs in to
                    for x in xs
                ]
                out.append(sync.Stage(lines, [], acc, fence=True, phase=s.phase))
        built_by[n] = out


def _interleave(streams: list) -> list:
    """Each stream's lines sent segment by segment, the units in turn."""
    return [ln for seg in _segments(streams) for ln in seg]


def _segments(streams: list) -> list:
    """The node lines of each segment: every unit's words of it, in turn."""
    count = max((ln.seg for _, ls in streams for ln in ls), default=-1) + 1
    out = []
    for seg in range(count):
        lines = []
        for idx, ls in streams:
            words = [ln.render() for ln in ls if ln.seg == seg]
            if words:
                lines.append(f"send mg[{idx}]")
                lines += [IND + w for w in words]
        out.append(lines)
    return out


def _added(streams, into, fail, st, remote=(), node=0) -> list:
    """Clusters adding their tiles into others' (`drain ACC -> mg[J] add`):
    every cluster runs up to its first drain; each addition goes once its
    target has swept (its tile is open and no sweep runs), followed by a
    COUNTed entry into the target's L1 A0 entry 0, which the target takes
    before the rest of its stream: one source's flits land in order, and no
    unit counts an add landing. Across nodes (`remote`: the sources adding
    into this node's clusters) the target's node rings each source's slot
    `ADD + this node` once the tile is open; the source waits on it."""
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
    marked = {}
    for src_node, j in remote:
        # The tile is open once its sweeps ran: the adding node may go.
        out.append(f"wait mg[{j}]")
        out.append(f"ring mesh={src_node} slot={ADD + node}")
        marked[j] = marked.get(j, 0) + 1
    sent_far = None
    for src, line in adds:
        if line.far is not None:
            if sent_far is not None:
                # One cross-node add in flight at a time (two at once hang).
                out.append(f"wait mg[{sent_far}]")
            out.append(f"wait slot {ADD + line.far} n=1 consume")
            out += [f"send mg[{src}]", IND + line.render()]
            out.append(
                f"{IND}drain n=4 to=mesh{line.far}.mg[{line.into}] buf=0 l1=0 count"
            )
            sent_far = src
            continue
        out.append(f"wait mg[{line.into}]")
        out += [f"send mg[{src}]", IND + line.render()]
        out.append(f"{IND}drain n=4 to=mg[{line.into}] buf=0 l1=0 count")
        marked[line.into] = marked.get(line.into, 0) + 1
    tails = [
        (idx, [_take(marked[idx], ls)] + ls if idx in marked else ls)
        for idx, ls in tails
    ]
    return out + _interleave(tails)


def _take(n: int, lines: list):
    """The added-into cluster takes the markers first, in its first segment."""
    return cluster.Line(f"fill A0 recv n={n}", lines[0].seg if lines else 0)


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
