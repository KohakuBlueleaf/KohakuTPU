"""Level-3 simulator: execute a Program against a device image and get numbers.

Reads a `Program` and nothing above it, so what it runs is what the machine
would be given: the same addresses, the same L1 spans, the same loop counts, the
same operand bindings.

**Not bit-accurate.** It moves values, not bits -- no DSP internals, no barrel
shifter depth, no handshake. What it does model is the part that changes
answers: where every operand comes from, and where each format's rounding and
saturation happen. `xsim` remains the authority on bits.
"""

import numpy as np

from ktpu.ir.program import Opcode, Program
from ktpu.target import Target

#: Explicit significand bits. ACC24 is S1E7M16 and E8M15 is S1E8M15, so both
#: keep 16 stored bits plus the implicit one; FP16 keeps 10.
SIG = {"fp16": 10, "acc24": 16, "e8m15": 15}
FP16_MAX = 65504.0


class MeshFault(RuntimeError):
    """A program that cannot be executed as written."""


def quantise(x: np.ndarray, dtype: str) -> tuple[np.ndarray, int]:
    """Round `x` to `dtype`, returning it and how many elements saturated.

    Only FP16 has a ceiling low enough to reach: at 65504 it is nine orders
    below ACC24's ~2^64, which is the whole argument for finishing a reduction
    on the vector core (vector-core.md s11).
    """
    x = np.asarray(x, dtype=np.float64)
    m, e = np.frexp(x)
    s = float(2 ** SIG[dtype])
    out = np.ldexp(np.round(m * s) / s, e)
    if dtype != "fp16":
        return out, 0
    over = np.abs(out) > FP16_MAX
    return np.where(over, np.sign(out) * FP16_MAX, out), int(over.sum())


def pack_operand(x: np.ndarray, tile: int, chunk: int, lanes: int, kblock: int):
    """Lay a `(rows, k)` operand out tile-major in L1-entry order.

    A block is `tile x chunk`; within it the entries run `g*nk + kb` and each
    entry is `lanes` rows by `kblock` of K (isa/cluster.md s3). Returns a flat
    float64 array. Raises MeshFault if the shape does not divide.
    """
    rows, k = x.shape
    if rows % tile or k % chunk or tile % lanes or chunk % kblock:
        raise MeshFault(f"{x.shape} does not divide into {tile}x{chunk} blocks")
    nk = chunk // kblock
    out = []
    for r0 in range(0, rows, tile):
        for c0 in range(0, k, chunk):
            blk = x[r0 : r0 + tile, c0 : c0 + chunk]
            out.append(
                blk.reshape(tile // lanes, lanes, nk, kblock)
                .transpose(0, 2, 1, 3)
                .reshape(-1)
            )
    return np.concatenate(out).astype(np.float64)


def unpack_operand(flat, rows, k, tile, chunk, lanes, kblock) -> np.ndarray:
    """Inverse of `pack_operand`."""
    nk = chunk // kblock
    out = np.zeros((rows, k))
    n = tile * chunk
    i = 0
    for r0 in range(0, rows, tile):
        for c0 in range(0, k, chunk):
            out[r0 : r0 + tile, c0 : c0 + chunk] = (
                flat[i : i + n]
                .reshape(tile // lanes, nk, lanes, kblock)
                .transpose(0, 2, 1, 3)
                .reshape(tile, chunk)
            )
            i += n
    return out


def pack_tile(arr: np.ndarray, tm: int, tn: int, gn: int, lanes: int) -> np.ndarray:
    """Lay `arr` out the way a DRAIN writes it: tile major, sub-tile order.

    The inverse of `Mesh.result`. An operand a vector core reads element for
    element against a drained tile has to be STORED in the drain's order,
    because the read is sequential and nothing between memory and the ALU can
    permute. `arr` is `(m, n)`; returns a flat array of `m * n`.
    """
    rows, cols = subtile_order(tm, tn, lanes)
    m, n = arr.shape
    out = np.zeros(m * n)
    for t in range((m // tm) * (n // tn)):
        mo, no = divmod(t, gn)
        out[t * tm * tn : (t + 1) * tm * tn] = arr[mo * tm + rows, no * tn + cols]
    return out


def entry_order(rows: int, k: int, lanes: int, kblock: int) -> np.ndarray:
    """Where element `(r, c)` of a `(rows, k)` operand sits in L1-ENTRY order.

    The inverse of `pack_operand`'s permutation, as a `(rows, k)` index array.
    A cluster's FILL reassembles entries of `lanes` rows by `kblock` of K, so a
    tensor a vector core produced in TILE order has to be written in this order
    instead or the GEMM reads the right bytes in the wrong places.
    """
    nk = max(1, k // kblock)
    r = np.arange(rows)[:, None]
    c = np.arange(k)[None, :]
    return ((r // lanes * nk + c // kblock) * lanes + r % lanes) * kblock + c % kblock


def subtile_order(tm: int, tn: int, lanes: int) -> tuple[np.ndarray, np.ndarray]:
    """Row and column of each element of a drained tile, in sub-tile order.

    A DRAIN writes sub-tile `t` to `addr + t*32` in the sweep's own order, row
    group major (isa/cluster.md s5), so element `e` of a tile's span is not
    element `e` of a row-major tile.
    """
    shape = (tm // lanes, lanes, tn // lanes, lanes)
    rows = np.arange(tm).repeat(tn).reshape(shape).transpose(0, 2, 1, 3)
    cols = np.tile(np.arange(tn), tm).reshape(shape).transpose(0, 2, 1, 3)
    return rows.reshape(-1), cols.reshape(-1)


class Mesh:
    """Executes a Program. `image` maps region name to a flat float64 array."""

    def __init__(self, prog: Program, target: Target):
        self.prog = prog
        self.t = target
        self.mem = {
            r.name: np.zeros(max(1, r.elems), dtype=np.float64)
            for r in prog.memory.regions
        }
        self.named: dict[str, np.ndarray] = {}
        self.loaded: set[str] = set()
        self._invs: dict[tuple[int, int], np.ndarray] = {}
        self.saturated = 0
        self.l1: dict[tuple[int, int], dict[str, int]] = {}
        self.tile: dict[tuple[int, int], np.ndarray] = {}
        self.vl: dict[tuple[int, int], int] = {}
        self.sreg: dict[tuple[int, int], dict[int, float]] = {}
        self.vreg: dict[tuple[int, int], dict[int, np.ndarray]] = {}
        self.chain: dict[tuple[int, int], np.ndarray] = {}
        self.counts: dict[str, int] = {}

    def _canon(self, region: str) -> str:
        """Resolve a region name or alias to the region's own name."""
        return self.prog.memory.get(region).name

    def load(self, region: str, data: np.ndarray) -> None:
        """Place `data` at the start of `region`, which may be an alias."""
        flat = np.asarray(data, dtype=np.float64).reshape(-1)
        name = self._canon(region)
        self.mem[name][: flat.size] = flat
        self.loaded.add(name)

    def bind(self, name: str, data: np.ndarray) -> None:
        """Upload a graph input by name into the region allocated for it.

        Raises MeshFault if no input of that name exists, which is what happens
        when a kernel's parameter is renamed and the caller is not.
        """
        if name not in self.prog.inputs:
            raise MeshFault(
                f"nothing bound for tensor operand {name!r}; "
                f"this program's inputs are {sorted(self.prog.inputs)}"
            )
        region = self.prog.inputs[name]
        spec = self.prog.packing.get(self._canon(region))
        if spec:
            self.put(region, data, spec.get("rows", 0), spec.get("k", 0))
        else:
            self.load(region, data)
        self.named[name] = np.asarray(data, dtype=np.float64).reshape(-1)

    def upload(self, band, a: np.ndarray, b: np.ndarray) -> "Mesh":
        """Pack a logical A `(m, k)` and B `(k, n)` into the `a` and `b`
        regions for `band`'s tile. Returns self.

        B is stored transposed, so ordinarily what goes in is `b.T`. When the
        graph already transposed it -- `q @ k.T` -- the region holds the
        orientation the GEMM wants and transposing again would undo it, so
        `Program.packing` says which case this is instead of the caller
        guessing. Use `put` for an operand that is not one of these two.
        """
        self.put("a", a, band.tile.m, band.tile.k)
        self.put("b", b, band.tile.n, band.tile.k)
        return self

    def put(self, region: str, data: np.ndarray, rows: int, k: int) -> None:
        """Pack `data` into `region` in L1-entry order, honouring any transpose
        already folded into the operand's address."""
        arr = np.asarray(data, dtype=np.float64)
        spec = self.prog.packing.get(self._canon(region), {})
        if spec.get("role") == "tile":
            self.load(
                region,
                pack_tile(arr, spec["tm"], spec["tn"], spec["gn"], self.t.lanes),
            )
            return
        flip = spec.get("role") == "b" and not spec.get("flip")

        def one(slab: np.ndarray) -> np.ndarray:
            if flip:
                slab = np.ascontiguousarray(slab.T)
            return pack_operand(slab, rows, k, self.t.lanes, self.t.kblock)

        # `x[h]` is an offset of whole slabs, so each packs on its own --
        # packing the flattened tensor would interleave heads inside one tile.
        if arr.ndim > 2:
            self.load(
                region,
                np.concatenate([one(s) for s in arr.reshape(-1, *arr.shape[-2:])]),
            )
            return
        self.load(region, one(arr))

    def _at(self, word: int, dtype: str) -> tuple[str, int]:
        for r in self.prog.memory.regions:
            if r.word <= word < r.end:
                bits = 32 * (word - r.word)
                if bits % (16 if dtype == "fp16" else 24):
                    raise MeshFault(
                        f"word {word} is not on a {dtype} boundary in {r.name}"
                    )
                return r.name, bits // (16 if dtype == "fp16" else 24)
        raise MeshFault(f"address {word} is outside every region")

    def run(self) -> "Mesh":
        """Execute every instruction in order. Returns self.

        Raises MeshFault on an address outside the map, an unknown opcode, or a
        GEMM whose L1 was never filled -- each of which is a codegen bug that
        would be a silent wrong answer on the machine.
        """
        missing = [
            n for n, r in self.prog.inputs.items() if self._canon(r) not in self.loaded
        ]
        if missing:
            raise MeshFault(
                f"nothing bound for tensor operand(s) {sorted(missing)}; "
                "an unloaded input reads as zeros and the answer would be "
                "plausible and wrong"
            )
        insts = self.prog.insts
        pc = 0
        while pc < len(insts):
            i = insts[pc]
            self.counts[i.op.value] = self.counts.get(i.op.value, 0) + 1
            if i.op is Opcode.VLOOP:
                n = i.fields["body"]
                body = insts[pc + 1 : pc + 1 + n]
                for ch in range(i.fields["count"]):
                    for bi in body:
                        self._vector(bi, ch)
                pc += 1 + n
                continue
            self._step(i)
            pc += 1
        return self

    def _step(self, i) -> None:
        node = i.node
        match i.op:
            case Opcode.FILL:
                region, off = self._at(i.fields["word"], "fp16")
                self.l1.setdefault(node, {})[i.fields["sel"]] = (region, off)
            case Opcode.GEMM:
                self._gemm(i)
            case Opcode.DRAIN:
                dt = i.fields["dtype"]
                region, off = self._at(i.fields["word"], dt)
                flat = self.tile[node].reshape(-1)
                out, sat = quantise(flat, dt)
                self.saturated += sat
                self.mem[region][off : off + out.size] = out
            case Opcode.VSETVL:
                self.vl[node] = i.fields["vl"]
            case Opcode.VSETI:
                self.sreg.setdefault(node, {})[i.fields["sd"]] = i.fields["imm"]
            case Opcode.VSETMD | Opcode.HOST:
                pass
            case _:
                raise MeshFault(f"no level-3 semantics for {i.op.value}")

    def _gemm(self, i) -> None:
        node, f = i.node, i.fields
        spans = self.l1.get(node)
        if not spans or "a" not in spans or "b" not in spans:
            raise MeshFault(f"GEMM at {node} before both operands were filled")
        rows, cols = f["gm"] * self.t.lanes, f["gn"] * self.t.lanes
        k = f["nk"] * self.t.kblock
        ar, ao = spans["a"]
        br, bo = spans["b"]
        a = unpack_operand(
            self.mem[ar][ao : ao + rows * k],
            rows,
            k,
            rows,
            k,
            self.t.lanes,
            self.t.kblock,
        )
        b = unpack_operand(
            self.mem[br][bo : bo + cols * k],
            cols,
            k,
            cols,
            k,
            self.t.lanes,
            self.t.kblock,
        )
        prod = a @ b.T
        rows_i, cols_i = subtile_order(rows, cols, self.t.lanes)
        flat = prod[rows_i, cols_i]
        prev = self.tile.get(node)
        self.tile[node] = flat if not f["acc"] else prev + flat

    def _vector(self, i, chunk: int) -> None:
        node, f = i.node, i.fields
        vl = self.vl[node]
        cur = chunk * vl
        regs = self.vreg.setdefault(node, {})
        match i.op:
            case Opcode.VLD:
                regs[f["vd"]] = self._load(f, cur, vl, chunk)
            case Opcode.VALU | Opcode.VRED:
                srcs = [self._src(node, s, regs) for s in f["srcs"]]
                out, _ = quantise(_apply(f["op"], srcs), "e8m15")
                match f["dst"]:
                    case "chain":
                        self.chain[node] = out
                    case "sreg":
                        self.sreg.setdefault(node, {})[f["vd"]] = out
                    case _:
                        regs[f["vd"]] = out
            case Opcode.VST:
                dt = f["dtype"]
                region, off = self._at(f["word"], dt)
                held = (
                    self.sreg[node][f["vs"]]
                    if f.get("src") == "sreg"
                    else regs[f["vs"]]
                )
                out, sat = quantise(np.atleast_1d(held), dt)
                self.saturated += sat
                n = f.get("n", out.size)
                if f.get("gather") == "row":
                    tl = self._tiling(region)
                    row = f["coff"] // f["tn"] + chunk
                    if f.get("layout") == "entry":
                        rows, kk = f["erows"], f["ek"]
                        blk, local = divmod(row, rows)
                        base = off + blk * rows * kk
                        idx = entry_order(rows, kk, self.t.lanes, self.t.kblock)[local]
                    else:
                        tm = f.get("stm", tl["tm"])
                        tn = f.get("stn", tl["tn"])
                        inv = (
                            tl["inv"]
                            if (tm, tn) == (tl["tm"], tl["tn"])
                            else self._inv(tm, tn)
                        )
                        blk, local = divmod(row, tm)
                        base = off + blk * tm * tn
                        idx = inv[local]
                    self.mem[region][base + idx[:n]] = out[:n]
                elif f.get("layout") == "entry":
                    self.mem[region][off + self._entry_at(f, cur, n)] = out[:n]
                else:
                    at = off + (chunk * n if n < vl else cur)
                    self.mem[region][at : at + out.size] = out[:n]
            case _:
                raise MeshFault(f"{i.op.value} is not a vector-core instruction")

    def _inv(self, tm: int, tn: int) -> np.ndarray:
        """`(row, col) -> offset` within a `tm x tn` tile, in sub-tile order.

        A graph with matmuls of DIFFERENT tile shapes has one of these per
        shape; `Program.tiling` only describes the final output's.
        """
        key = (tm, tn)
        if key not in self._invs:
            r, c = subtile_order(tm, tn, self.t.lanes)
            m = np.zeros((tm, tn), dtype=int)
            m[r, c] = np.arange(tm * tn)
            self._invs[key] = m
        return self._invs[key]

    def _tiling(self, region: str) -> dict:
        """'s own tile geometry, or the program default."""
        return self.prog.tilings.get(region, self.prog.tiling)

    def _entry_at(self, f: dict, cur: int, n: int) -> np.ndarray:
        """Where a contiguous run of a TILE-ordered span lands in entry order.

        A band that reads tile order writes tile order; a cluster's FILL reads
        L1-entry order. This is the permutation between them, so the producer
        writes what the consumer expects rather than the two disagreeing about
        the same bytes.
        """
        tl = self._tiling(self._at(f["word"], f["dtype"])[0])
        rows, k = f["erows"], f["ek"]
        tm, tn, gn = tl["tm"], tl["tn"], max(1, tl["gn"])
        pos = np.arange(f["coff"] + cur, f["coff"] + cur + n)
        # Go through the value's LOGICAL position: the producer's tile and the
        # consumer's fill blocking are independent and need not be the same.
        tile, within = np.divmod(pos, tm * tn)
        row = (tile // gn) * tm + tl["rows"][within]
        col = (tile % gn) * tn + tl["cols"][within]
        nkb = max(1, tl["n"] // k)
        rblk, local = np.divmod(row, rows)
        cblk, within_k = np.divmod(col, k)
        idx = entry_order(rows, k, self.t.lanes, self.t.kblock)
        return (rblk * nkb + cblk) * rows * k + idx[local, within_k]

    def _src(self, node, tag: str, regs) -> np.ndarray:
        if tag == "C":
            return self.chain[node]
        if tag.startswith("v"):
            return regs[int(tag[1:])]
        if tag.startswith("s"):
            return self.sreg[node][int(tag[1:])]
        raise MeshFault(f"unknown operand {tag!r}")

    def _load(self, f, cur: int, vl: int, chunk: int = 0) -> np.ndarray:
        region, off = self._at(f["word"], f["dtype"])
        src = self.mem[region]
        mode = f.get("bcast", "elem")
        tl = self._tiling(region)
        if f.get("gather"):
            row = f["coff"] // f["tn"] + chunk
            if f["gather"] == "rowbcast":
                return np.full(vl, src[off + row % max(1, src.size)])
            if f.get("layout") == "entry":
                rows, kk = f["erows"], f["ek"]
                blk, local = divmod(row, rows)
                idx = entry_order(rows, kk, self.t.lanes, self.t.kblock)[local]
                return src[off + blk * rows * kk + idx][:vl].copy()
            tm, tn = f.get("stm", tl["tm"]), f.get("stn", tl["tn"])
            inv = tl["inv"] if (tm, tn) == (tl["tm"], tl["tn"]) else self._inv(tm, tn)
            tile, local = divmod(row, tm)
            return src[off + tile * tm * tn + inv[local]][:vl].copy()
        if mode == "elem":
            base = off + (f["coff"] + cur if "coff" in f else cur)
            return src[base : base + vl].copy()

        # The positions walked here are in the CONSUMING band's tile, not in
        # the operand's own region, so this geometry is the band's.
        tl = self.prog.tiling
        tm, tn = f.get("stm", tl["tm"]), f.get("stn", tl["tn"])
        rows, cols = (
            (tl["rows"], tl["cols"])
            if (tm, tn) == (tl["tm"], tl["tn"])
            else subtile_order(tm, tn, self.t.lanes)
        )
        gn = tl["gn"] if (tm, tn) == (tl["tm"], tl["tn"]) else 1
        pos = np.arange(f["coff"] + cur, f["coff"] + cur + vl)
        tile, within = np.divmod(pos, tm * tn)
        if mode == "col":
            idx = cols[within] + (tile % gn) * tn
        else:
            idx = rows[within] + (tile // gn) * tm
        if "stride" in f:
            bm, bn = f["bm"], f["bn"]
            idx = (
                self._inv(bm, bn)[idx % bm, f["base"]]
                if bm and bn
                else idx * f["stride"] + f["base"]
            )
        return src[off + idx % max(1, src.size)]

    def read(self, region: str) -> np.ndarray:
        """The region's contents as a flat float64 array."""
        return self.mem[self._canon(region)].copy()

    def result(self, region: str = "c") -> np.ndarray:
        """`region` as a logical `m x n` array, undoing the tile layout."""
        tl = self._tiling(self._canon(region))
        out = np.zeros((tl["m"], tl["n"]))
        tm, tn, gn = tl["tm"], tl["tn"], tl["gn"]
        rows, cols = tl["rows"], tl["cols"]
        flat = self.mem[self._canon(region)]
        for t in range(tl["tiles"]):
            mo, no = divmod(t, gn)
            out[mo * tm + rows, no * tn + cols] = flat[t * tm * tn : (t + 1) * tm * tn]
        return out


_BIN = {
    "add": np.add,
    "sub": np.subtract,
    "mul": np.multiply,
    "div": np.divide,
    "max": np.maximum,
    "min": np.minimum,
    "cmplt": lambda a, b: (a < b).astype(np.float64),
    "cmpgt": lambda a, b: (a > b).astype(np.float64),
    "cmpeq": lambda a, b: (a == b).astype(np.float64),
}
_UN = {
    "neg": np.negative,
    "abs": np.abs,
    "recip": lambda a: 1.0 / a,
    "rsqrt": lambda a: 1.0 / np.sqrt(a),
    "sqrt": np.sqrt,
    "exp2": np.exp2,
    "log2": np.log2,
    "relu": lambda a: np.maximum(a, 0.0),
}


def _apply(op: str, srcs: list) -> np.ndarray:
    if op in _UN:
        return _UN[op](srcs[0])
    if op in _BIN:
        return _BIN[op](srcs[0], srcs[1])
    if op == "fma":
        return srcs[0] * srcs[1] + srcs[2]
    if op == "select":
        return np.where(srcs[0] != 0.0, srcs[1], srcs[2])
    if op in _RED:
        return _RED[op](srcs[0])
    raise MeshFault(f"no level-3 implementation for {op}")


#: TREE mode: 16 elements in, one out, over the whole active vector length.
_RED = {
    "sum": np.sum,
    "rmax": np.max,
    "rmin": np.min,
    "sumsq": lambda a: np.sum(a * a),
}
