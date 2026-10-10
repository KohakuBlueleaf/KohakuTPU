"""The data behind the Node VM page's walkthrough: three L3 programs through
every level -- L3 text, the L2 schedule, the L1 program, the L0 package -- with
each L0 payload traced to the L1 op that made it, each op to its L2 item and
each item to its L3 values, and a timeline of what the node and the units do.

    python scripts/py/artifact/ir_walk.py -o OUT.js

The timeline is MODELLED, not measured: the node VM's step semantics
(docs/spec/package-format.md §4: round-robin DISPATCH, credit, AWAIT, BARRIER,
MOVER) over unit durations from the cost model (a cluster's FILL at the shared
fill rate, its sweep at gm*gn*nk/2, its DRAIN at the single-cluster drain rate;
a vector RUN at `vsched`'s cycles for its image; a node send at NODE_SEND).
"""

import argparse
import json
import pathlib
import re

from kohakuaccel.ir.l2.lower import compile as l2_compile
from kohakuaccel.package.format import Op, Package
from kohakutpu.ir import l3
from kohakutpu.ir.l1 import text as l1_text
from kohakutpu.ir.l1.cluster import Drain, Fill, Gemm
from kohakutpu.ir.l1.kernels import vrun as VR
from kohakutpu.ir.l1.mover import Copy, Quantise
from kohakutpu.ir.l1.program import Program
from kohakutpu.ir.l1.vector import Desc, Dims, Image, Run
from kohakutpu.ir.l2 import text as l2_text
from kohakutpu.ir.l2.lowerers import FILL_BYTES_A_CYCLE, LOWERERS, _run_cycles, mover
from kohakutpu.ir.l3 import lower
from kohakutpu.ir.numerics import UnitModel
from kohakutpu.isa.vector import OP_DESC, OP_IMEM, OP_RUN

from kohakutpu import imem

#: Node cycles a DISPATCH step costs: one fetch-port request (MEASURED ~1k on
#: card_v9_1n, kohakuaccel/ir/l1/program.py); the port then streams the words.
NODE_STEP = 1000
#: Cycles between streamed words arriving at a unit (MODEL).
WORD_GAP = 4
#: One cluster's drain, B/cycle (MEASURED, l1.md §7: 18.8 from one cluster).
DRAIN_BYTES_A_CYCLE = 18.8
#: The mover's convert, B/cycle of fp16 read (MODEL).
MOVE_BYTES_A_CYCLE = 30.0
CREDIT = 512

BIAS_SILU = """level l3
# y = silu(x @ w^T + bias): the bias rides the clusters' bias K-block, the
# silu runs on the vector cores over each drained tile.
program linear_silu(x: mx7[M, K], w: mx7[N, K], bias: f16[N])
    tile bm = 32
    tile bn = 32
    y = output                                  : f16[M, N]
    map i in tiles(M, bm), j in tiles(N, bn)
        c = mmt x[i, :], w[j, :]                : f16[bm, bn]
        r = add c, bias[j]                      : f16[bm, bn]
        t = mul r, -1.4426950408889634          : f32[bm, bn]
        e = exp2 t                              : f32[bm, bn]
        d = add e, 1.0                          : f32[bm, bn]
        q = inv d                               : f32[bm, bn]
        z = mul r, q                            : f16[bm, bn]
        store y[i, j] = z
"""

EXAMPLES = [
    {
        "key": "linear",
        "name": "linear + bias → silu",
        "what": "64×128 @ 128×64ᵀ: four 32×32 tiles, one per cluster; the bias in each tile's bias K-block; silu on the two vector cores over each drained tile.",
        "text": BIAS_SILU,
        "program": "linear_silu",
        "shapes": {"x": (64, 128), "w": (64, 128), "bias": (64,)},
    },
    {
        "key": "layernorm",
        "name": "layer norm",
        "what": "32 rows × 128: each vector core takes 16 rows, streams them through two L1 regions, reduces each row with butterflies, gamma and beta resident.",
        "file": "rows",
        "program": "layernorm",
        "shapes": {"x": (32, 128), "g": (128,), "b": (128,)},
    },
    {
        "key": "attention",
        "name": "flash attention",
        "what": "32 queries against 128 keys in two 64-key blocks: per block a cluster sweep for S, the online softmax on a vector core (carries in its L1), the mover quantises P, a second sweep for P·V, the update.",
        "file": "attention",
        "program": "attention",
        "shapes": {"q": (1, 32, 64), "k": (1, 128, 64), "vt": (1, 64, 128)},
    },
]


class Traced(Program):
    """An L1 program that remembers the L2 item behind each step."""

    def __init__(self, machine) -> None:
        super().__init__(machine)
        self.item = None
        self.prov: dict = {}

    def note(self, item: int) -> None:
        self.item = item

    def send(self, coord, *ops):
        n = len(self.steps)
        super().send(coord, *ops)
        if len(self.steps) > n:
            self.prov[n] = self.item
        return self

    def move(self, *ops):
        n = len(self.steps)
        super().move(*ops)
        self.prov[n] = self.item
        return self


def unit_name(at, machine) -> str:
    if at is None:
        return "mover"
    kinds = {c: k for k, cs in machine.units.items() for c in cs}
    return f"{kinds.get(tuple(at), '?')} ({at[0]},{at[1]})"


def l3_lines(text: str) -> dict:
    """Value name -> the L3 line (1-based) that binds it; a function's values
    also under every name a call site gives them."""
    out: dict = {}
    for n, line in enumerate(text.splitlines(), 1):
        m = re.match(r"\s*(?:next\s+)?(\w+)\s*=", line)
        if m:
            out.setdefault(m.group(1), n)
    return out


def origin_lines(names, where: dict) -> list:
    lines = set()
    for name in names:
        for part in (name, name.split(".")[-1], name.split(".")[0]):
            if part in where:
                lines.add(where[part])
                break
    return sorted(lines)


def op_kind(op) -> str:
    return {
        Fill: "fill",
        Gemm: "gemm",
        Drain: "drain",
        Image: "image",
        Desc: "desc",
        Dims: "desc",
        Run: "run",
        Quantise: "move",
        Copy: "move",
    }.get(type(op), "op")


def l1_op_lines(text: str) -> list:
    """Per program, per step: the L1 text lines of its ops (1-based)."""
    progs, cur, step = [], None, None
    for n, line in enumerate(text.splitlines(), 1):
        if line.startswith("program "):
            cur = []
            progs.append(cur)
            continue
        if cur is None:
            continue
        if line.startswith(("    send ", "    move")):
            step = [n]
            cur.append(step)
        elif line.startswith("        ") and step is not None:
            step.append(n)
        elif line.startswith("    "):
            cur.append([n])
            step = None
    return progs


def find_buffer(sched, addr):
    for b in sched.buffers:
        if (
            b.base is not None
            and b.space == "mem"
            and b.base <= addr < b.base + b.nbytes
        ):
            return b.name
    return None


def run_cost(item, op, images) -> float:
    """A vector RUN's cycles: its image's model, by the item that sent it."""
    p = item.params
    if item.kind == "vec_prog":
        name = next(n for n, _, pc, _, _ in VR.images(p["prog"]) if pc == op.pc)
        return VR.cycles(p["prog"], name)
    if item.kind == "vec_stream":
        k = images.index(op.pc) if op.pc in images else 1
        if k in (1, 2, 5):
            return _run_cycles(tuple(p["body"]), p["words"], p["runs"])
        return p["words"] * 1.0
    return 1000.0


def build(ex: dict) -> dict:
    text = ex.get("text") or (l3.KERNELS / f"{ex['file']}.l3").read_text(
        encoding="utf-8"
    )
    module = l3.read(text, ex.get("file", ex["key"]))
    t = UnitModel()
    comp = lower.compile(module, ex["program"], ex["shapes"], t)
    sched = comp.schedule
    l2 = l2_text.write(sched)
    progs = l2_compile(sched, sched.machine, LOWERERS, mover=mover, program=Traced)
    l1 = l1_text.write(progs, sched.machine)
    where = l3_lines(text)
    item_line = [
        n for n, ln in enumerate(l2.splitlines(), 1) if ln.startswith("    item ")
    ]
    items = [
        {
            "i": i,
            "kind": it.kind,
            "unit": unit_name(it.at, sched.machine),
            "l2": item_line[i],
            "l3": origin_lines(comp.origin.get(i, ()), where),
        }
        for i, it in enumerate(sched.items)
    ]
    lines = l1_op_lines(l1)
    packages = []
    for pi, prog in enumerate(progs):
        pkg = Package.from_bytes(
            prog.build(None, None).build(defaults=False).to_bytes()
        )
        raw = pkg.to_bytes()
        plines = lines[pi] if pi < len(lines) else []
        # each L1 op, with its words; per unit, in stream order
        ops, stream = [], {}
        for k, st in enumerate(prog.steps):
            ln = plines[k] if k < len(plines) else [0]
            item = prog.prov.get(k)
            if st[0] == "send":
                unit = st[1]
                for j, op in enumerate(st[3]):
                    words = op.flits()
                    o = {
                        "id": len(ops),
                        "unit": unit_name(unit, sched.machine),
                        "at": list(unit),
                        "kind": op_kind(op),
                        "l1": ln[j + 1] if j + 1 < len(ln) else ln[0],
                        "item": item,
                        "nwords": len(words),
                        "_op": op,
                    }
                    ops.append(o)
                    stream.setdefault(tuple(unit), []).extend([o["id"]] * len(words))
            elif st[0] == "move":
                for j, op in enumerate(st[2] or ()):
                    ops.append(
                        {
                            "id": len(ops),
                            "unit": "mover",
                            "at": None,
                            "kind": "move",
                            "l1": ln[j + 1] if j + 1 < len(ln) else ln[0],
                            "item": item,
                            "nwords": 0,
                            "_op": op,
                        }
                    )
        timeline = simulate(pkg, ops, stream, sched, comp)
        for o in ops:
            o.pop("_op")
        packages.append(
            {
                "bytes": len(raw),
                "header": [raw[i : i + 16].hex(" ") for i in range(0, 96, 16)],
                "units": [
                    {
                        "type": u.type.to_bytes(2, "big").decode(),
                        "x": u.x,
                        "y": u.y,
                        "credit": u.credit,
                    }
                    for u in pkg.units
                ],
                "steps": [
                    {
                        "op": Op(s.op).name,
                        "unit": s.unit,
                        "count": s.count,
                        "arg": s.arg,
                    }
                    for s in pkg.steps
                ],
                "npayload": len(pkg.payloads),
                "payloads": sample_payloads(pkg),
                "ops": ops,
                **timeline,
            }
        )
    return {
        "key": ex["key"],
        "name": ex["name"],
        "what": ex["what"],
        "l3": text.splitlines(),
        "l2": l2.splitlines(),
        "l1": l1.splitlines(),
        "items": items,
        "buffers": [
            {
                "name": b.name,
                "base": b.base,
                "nbytes": b.nbytes,
                "layout": type(b.layout).__name__ if b.layout else "bytes",
            }
            for b in sched.buffers
            if b.space == "mem"
        ],
        "packages": packages,
    }


def sample_payloads(pkg) -> list:
    """Each payload's hex and what it is, for the L0 view (IMEM words grouped)."""
    out = []
    for i, w in enumerate(pkg.payloads):
        code = imem.op(w)
        if code == OP_IMEM:
            what = "IMEM word"
        elif code == OP_DESC:
            what = "DESC"
        elif code == OP_RUN:
            what = "RUN"
        else:
            what = f"op {code:#x}"
        out.append([f"{w:064x}", what])
    return out


def simulate(pkg, ops, stream, sched, comp) -> dict:
    """The node VM and the units over modelled durations: when each payload is
    sent, when each op runs on its unit, and each step's span on the node."""
    units = [(u.x, u.y) for u in pkg.units]
    sent = {u: 0 for u in units}  # payloads sent so far, per unit
    done_at = {u: [] for u in units}  # completion time of each payload, in order
    expected = {u: 0 for u in units}
    state = {u: {"free": 0.0, "sweep": 0.0} for u in units}
    images: dict = {}  # unit -> pcs of the images it was sent, in order
    t = 0.0
    steps = []
    by_id = {o["id"]: o for o in ops}

    def run_word(u, opid, first: bool, last: bool, arrive: float, p: int) -> float:
        o = by_id[opid]
        op, s = o["_op"], state[u]
        o.setdefault("p0", p)
        kind = o["kind"]
        if kind == "fill":
            start = max(arrive, s["free"])
            end = start + op.n * 128 / FILL_BYTES_A_CYCLE
            s["free"] = end
            done = end
        elif kind == "gemm":
            start = max(arrive, s["free"], s["sweep"])
            s["sweep"] = start + op.gm * op.gn * op.nk / 2
            s["free"] = start + 1
            done, end = start + 1, s["sweep"]
        elif kind == "drain":
            start = max(arrive, s["free"], s["sweep"])
            end = start + op.n * 32 / DRAIN_BYTES_A_CYCLE
            s["free"] = end
            done = end
        elif kind == "run":
            start = max(arrive, s["free"])
            item = sched.items[o["item"]] if o["item"] is not None else None
            cyc = run_cost(item, op, images.get(u, [])) if item is not None else 1000.0
            end = start + cyc
            s["free"] = end
            done = end
        else:  # image / desc words: one cycle each
            start = max(arrive, s["free"])
            end = start + 2
            s["free"] = end
            done = end
            if kind == "image" and first:
                images.setdefault(u, []).append(op.at)
        if first:
            o["start"] = start
        o["end"] = max(o.get("end", 0), end)
        o["sent"] = o.get("sent", arrive)
        o["done"] = done if last else o.get("done", done)
        return done

    k = 0
    while k < len(pkg.steps):
        s = pkg.steps[k]
        op = Op(s.op)
        t0 = t
        if op == Op.DISPATCH:
            # a fetch-port request: issued once the unit has credit for it
            u = units[s.unit]
            need = sent[u] + s.count - CREDIT
            if need > 0:
                t = max(t, done_at[u][need - 1])
            t0 = t
            t += NODE_STEP
            for j in range(s.count):
                pos = sent[u]
                opid = stream[u][pos]
                first = pos == 0 or stream[u][pos - 1] != opid
                last = pos + 1 == len(stream[u]) or stream[u][pos + 1] != opid
                done_at[u].append(
                    run_word(u, opid, first, last, t + WORD_GAP * j, s.arg + j)
                )
                sent[u] += 1
            steps.append(
                {"k": k, "op": "DISPATCH", "unit": s.unit, "start": t0, "end": t}
            )
            k += 1
            continue
        if op == Op.AWAIT:
            u = units[s.unit]
            expected[u] += s.count
            t = max(t, done_at[u][expected[u] - 1])
        elif op == Op.BARRIER:
            t = max([t] + [d[-1] for d in done_at.values() if d])
        elif op == Op.MOVER:
            movers = [o for o in ops if o["kind"] == "move" and "start" not in o]
            t += s.count * 2 * 12
            if movers:
                m = movers[0]
                mo = m["_op"]
                nbytes = mo.entries * 256 if isinstance(mo, Quantise) else mo.nbytes
                m["start"], m["sent"] = t, t
                t += nbytes / MOVE_BYTES_A_CYCLE
                m["end"] = m["done"] = t
        steps.append({"k": k, "op": op.name, "unit": s.unit, "start": t0, "end": t})
        k += 1
    for o in ops:
        o.setdefault("start", t)
        o.setdefault("end", o["start"])
        o.setdefault("sent", o["start"])
        o.setdefault("done", o["end"])
        o["flows"] = flows(o, sched)
        if o["item"] is not None:
            o["l2"] = comp.origin.get(o["item"]) is not None
    steps.sort(key=lambda s: s["k"])
    return {"steps_t": steps, "T": t}


def flows(o, sched) -> list:
    """The data an op moves between memory and its unit: ``[from, to, bytes,
    buffer]``."""
    op = o["_op"]
    u = o["unit"]
    if o["kind"] == "fill":
        return [["mem", u, op.n * 128, find_buffer(sched, op.addr)]]
    if o["kind"] == "drain":
        return [[u, "mem", op.n * 32, find_buffer(sched, op.addr)]]
    if o["kind"] == "move":
        src, dst = op.src, op.dst
        return [
            ["mem", "mover", 0, find_buffer(sched, src)],
            ["mover", "mem", 0, find_buffer(sched, dst)],
        ]
    if o["kind"] == "run" and o["item"] is not None:
        p = sched.items[o["item"]].params
        out = []
        for a in list(p.get("srcs", [])) + [p.get("in_at")]:
            if a:
                out.append(["mem", u, p.get("words", 0) * 32, find_buffer(sched, a)])
        for a in (p.get("dst"), p.get("out_at")):
            if a:
                out.append([u, "mem", p.get("words", 0) * 32, find_buffer(sched, a)])
        return out
    return []


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", required=True)
    a = ap.parse_args(argv)
    data = [build(ex) for ex in EXAMPLES]
    out = pathlib.Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "window.WALK = " + json.dumps(data, separators=(",", ":")) + ";\n",
        encoding="utf-8",
    )
    for d in data:
        p = d["packages"]
        print(
            d["key"],
            "packages",
            len(p),
            "ops",
            [len(x["ops"]) for x in p],
            "payloads",
            [x["npayload"] for x in p],
            "T",
            [round(x["T"]) for x in p],
            out.stat().st_size,
        )
        for o in p[0]["ops"][:: max(1, len(p[0]["ops"]) // 8)]:
            print(
                f"   {o['unit']:10s} {o['kind']:6s} item {o['item']} l1 {o['l1']}: {d['l1'][o['l1'] - 1].strip()[:40]:40s} t {o['start']:.0f}-{o['end']:.0f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
