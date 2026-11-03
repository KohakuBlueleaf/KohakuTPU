"""One fn on the card, run after run. Every parameter is bound inside a padded
span with `check.GUARD_BYTES` of tag words (a redzone) either side. Each run
writes the fn's fresh inputs, paints its outputs with POISON and its redzones,
runs between two reads of every die's interlink counters, then checks every
output, redzone, the token ledger and the measure records (`check.verdict`).
A mismatched output is read a second time while the card is idle (`reread`),
and a `Spec.provenance` fn's row decodes every retained mismatch.
A `Spec.triple` fn runs serial -> overlap -> serial on the same bindings and
packages, a row a phase. Every row carries the physical intervals after its run
(`intervals`); a `Spec.refused` fn is only compiled, bound and statically checked
(`admission`), never issued."""

import collections
import time
from collections.abc import Iterator

import numpy as np

from h import check
from h import ledger as L
from h.gen import Spec
from kohakutpu.application.runtime import packages as B
from kohakutpu.application.runtime.card import interlink
from kohakutpu.application.runtime.card.measure import Ring
from kohakutpu.application.tools import l1
from kohakutpu.driver.host import board_map
from kohakutpu.stack import kernel

#: A region's padding either side (in DRAM it keeps every alignment up to 64 KiB);
#: the upper redzone starts on the line past the region's end.
PAD = {"dram": 64 << 10, "stage": 2 * check.GUARD_BYTES}
LINE = 64
#: The words of an output without a reference kept in its row.
OBSERVED = 64


def bind(card, layout) -> tuple[dict, dict]:
    """Each parameter placed inside its padding: its address, and its redzones'
    (region, below, above) addresses."""
    binds, zones = {}, {}
    for region, (p, b) in enumerate(layout):
        pad = PAD[b.space]
        at = card.place(b.nbytes + 2 * pad, b.space, b.layout["node"]) + pad
        binds[p] = at
        zones[p] = (region, at - check.GUARD_BYTES, at - (-b.nbytes // LINE) * LINE)
    return binds, zones


def intervals(card, binds: dict, layout) -> dict:
    """The card memory's every held span ``[lo, hi, owner]`` (queues, packages,
    firmware regions, padded parameters), and each parameter's
    ``[lo, hi, line_lo, line_hi, space]``: its bytes and its 64-byte write
    footprint."""
    held = [
        [at, at + size, arena.owners[at]]
        for arena in card.memory.arenas()
        for at, size in sorted(arena.live.items())
    ]
    params = {}
    for p, b in layout:
        lo, hi = binds[p], binds[p] + b.nbytes
        params[p] = [lo, hi, lo // LINE * LINE, -(-hi // LINE) * LINE, b.space]
    return dict(held=held, params=params)


def admission(card, module, name: str, level: str, spec: Spec, session: str) -> dict:
    """A fn that must be refused before issue: compiled, bound and statically
    checked, never run. Its row: the refusal, and EXPECTED when it names
    `spec.refused`."""
    held = card.mark()
    refused = ""
    try:
        compiled = kernel.compile(module, name, card.machine, level=level)
        binds, _ = bind(card, l1.bufs(compiled.l1, compiled.name, card.machine_json))
        B.findings(compiled.programs(binds), "refuse")
    except Exception as error:  # noqa: BLE001 -- the refusal is the outcome
        refused = repr(error)[:600]
    finally:
        card.release(held)
    ok = bool(refused) and spec.refused in refused
    return dict(
        session=session,
        name=name,
        rep=0,
        phase="admission",
        refused=refused,
        verdict=dict(
            structural="EXPECTED" if ok else "FAIL",
            precision="NONE",
            reasons=[] if ok else [f"not refused with {spec.refused}"],
            timing="INVALID",
        ),
    )


def judge(b, raw: bytes, ref, exact: bool) -> dict:
    """One output read back: its digest (an index), and its exact or numerical
    check against `ref` (none: its leading words, observed)."""
    got: dict = dict(digest=l1.digest({"bytes": np.frombuffer(raw, np.uint8)}))
    if ref is None:
        got["observed"] = check.words(raw[: 8 * OBSERVED]).tolist()
    elif exact:
        want = (
            ref
            if isinstance(ref, bytes)
            else b.pack(np.asarray(ref, b.host_dtype).reshape(b.host_shape))
        )
        got |= check.exact(raw, want)
    else:
        got |= check.numeric(np.asarray(b.unpack(raw)), ref)
    return got


def reread(card, at: int, b, first: bytes, ref) -> dict:
    """A mismatched output read again once every sysnode is idle: its check, and
    the words that differ from the first read (`check.KEPT` of them as
    ``[word, first, second]``), so an observation-path fault shows as disagreement."""
    raw = card.get(at, b.nbytes)
    a, z = check.words(first), check.words(raw)
    moved = np.flatnonzero(a != z)
    return judge(b, raw, ref, True) | dict(
        changed=int(moved.size),
        changes=[[int(i), int(a[i]), int(z[i])] for i in moved[: check.KEPT]],
    )


def phases(spec: Spec, prog) -> list[tuple[str, tuple | None]]:
    """(phase, the sysnodes run; None: every one) of each run of a rep."""
    if spec.issued:
        return [("issued:" + "".join(map(str, spec.issued)), spec.issued)]
    if not spec.triple or not isinstance(prog, dict):
        return [("all", None)]
    out = []
    for g in spec.overlaps or (tuple(sorted(prog)),):
        tag = "".join(map(str, g))
        alone = [(n,) for n in g]
        out += [(f"before:{tag}", r) for r in alone] + [(f"overlap:{tag}", g)]
        out += [(f"after:{tag}", r) for r in alone]
    return out


def runs(
    card,
    compiled,
    spec: Spec,
    seed: int,
    reps: int,
    faults: bool,
    ring: Ring | None,
    session: str,
) -> Iterator[tuple[dict, list[dict]]]:
    """`reps` runs of `compiled` on `card` (epoch = rep + 1), each phase's row and
    its measure records (one a sysnode run; none without `ring`)."""
    layout = l1.bufs(compiled.l1, compiled.name, card.machine_json)
    bufs = dict(layout)
    rng = np.random.default_rng(seed)
    given = spec.inputs(0) if spec.inputs else {}
    ins = {
        p: l1.seeded(b, rng)
        for p, b in layout
        if p not in spec.outputs and p not in given
    }
    if not spec.fresh:
        ins |= {p: np.reshape(v, bufs[p].host_shape) for p, v in given.items()}
    ctrls = board_map(card.board)["ctrl"]
    held = card.mark()
    try:
        binds, zones = bind(card, layout)
        for p, b in layout:
            if p not in spec.outputs:
                card.write(binds[p], l1.raw(b, ins[p]) if p in ins else bytes(b.nbytes))
        progs = compiled.programs(binds)
        prog = progs[None] if list(progs) == [None] else progs
        want = {}
        for rep in range(reps):
            epoch = rep + 1
            if spec.inputs and spec.fresh:
                now = spec.inputs(epoch)
                for p, v in now.items():
                    b = bufs[p]
                    card.write(binds[p], l1.raw(b, np.reshape(v, b.host_shape)))
                ins |= now
            if spec.reference and (rep == 0 or spec.fresh):
                want = spec.reference(ins | {"epoch": epoch})
            for phase, ran in phases(spec, prog):
                row = dict(
                    session=session,
                    name=compiled.name,
                    rep=rep,
                    epoch=epoch,
                    phase=phase,
                )
                yield once(
                    card,
                    ctrls,
                    prog,
                    ran,
                    binds,
                    zones,
                    layout,
                    want,
                    spec,
                    faults,
                    ring,
                    row,
                )
    finally:
        card.release(held)


def once(card, ctrls, prog, ran, binds, zones, layout, want, spec, faults, ring, row):
    """One run of `prog` (of its sysnodes `ran`): `row` completed, and its
    measure records keyed like it."""
    epoch = row["epoch"]
    outs = [(p, b) for p, b in layout if p in spec.outputs]
    l1.paint(card, binds, outs)
    for region, lo, hi in zones.values():
        z = check.guard(epoch, region)
        card.write(lo, z[: check.GUARD_BYTES])
        card.write(hi, z[check.GUARD_BYTES :])
    sub = prog if ran is None else {n: prog[n] for n in ran}

    def timed() -> tuple[dict, float]:
        t0 = time.perf_counter()
        got = card.run(sub, faults=faults)
        return got, time.perf_counter() - t0

    (res, wall), links = interlink.around(card.t, ctrls, timed)
    raw = {p: card.get(binds[p], b.nbytes) for p, b in outs}
    outputs, ledger, refs = {}, [], {}
    for p, b in outs:
        if spec.ledger and p == "slots":
            outputs[p] = dict(observed=check.words(raw[p]).tolist())
            if ran is None:
                ledger = L.check(spec.ledger, links, check.words(raw[p]))
            continue
        refs[p] = want.get(p)
        if ran is not None and spec.owners.get(p) not in ran:
            refs[p] = bytes([l1.POISON]) * b.nbytes
        outputs[p] = judge(b, raw[p], refs[p], p not in spec.numeric)
    for p, b in outs:
        if outputs[p].get("mismatched"):
            outputs[p]["reread"] = reread(card, binds[p], b, raw[p], refs[p])
    hits = {}
    for p, (region, lo, hi) in zones.items():
        got = card.get(lo, check.GUARD_BYTES) + card.get(hi, check.GUARD_BYTES)
        hits[p] = check.hit(got, check.guard(epoch, region))
    sysnodes = l1.sysnodes(res, card.nodes[0])
    errs = {n: r["fault"] for n, r in sysnodes.items() if "fault" in r}
    verdict = check.verdict(outputs, hits, errs, ledger, spec.diagnostic, spec.expected)
    units = ring.rows() if ring is not None else []
    done = sorted(sub) if isinstance(sub, dict) else [card.nodes[0]]
    records = collections.Counter(u["node"] for u in units)
    measure = dict(
        records={n: records[n] for n in done},
        lost=dict(ring.lost) if ring is not None else {},
        valid=ring is None
        or (all(records[n] == 1 for n in done) and not any(ring.lost.values())),
    )
    verdict["timing"] = (
        "VALID"
        if verdict["structural"] == "PASS" and measure["valid"] and not spec.diagnostic
        else "INVALID"
    )
    row |= dict(
        ran=done,
        wall=wall,
        cycles=res["cycles"],
        binds={p: int(a) for p, a in binds.items()},
        redzones={p: [lo, hi] for p, (_, lo, hi) in zones.items()},
        intervals=intervals(card, binds, layout),
        sysnodes=sysnodes,
        outputs=outputs,
        ledger=ledger,
        interlink=links,
        measure=measure,
        verdict=verdict,
    )
    if spec.provenance:
        row["provenance"] = spec.provenance(outputs, row["binds"], epoch)
    keys = {k: row[k] for k in ("session", "name", "rep", "epoch", "phase")}
    return row, [dict(u, **keys) for u in units]
