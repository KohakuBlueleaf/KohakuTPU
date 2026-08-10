"""Run a kernel on a VECTOR CORE of the card. Nothing else here ever has.

    python run_vector.py --stage halt          # does the node execute at all?
    python run_vector.py --stage copy          # DRAM -> L1 -> DRAM, no lanes
    python run_vector.py --stage add           # the lanes compute, checked vs numpy
    python run_vector.py --stage add --all     # the same on all four cores

THE STAGES ARE A LADDER, and that is the point. `halt` proves only that a
'VC' node accepts a CU instruction, loads instruction memory and retires a RUN.
`copy` adds the address generator, the memory read path, L1 and the write path,
and still never issues an ALU op. `add` adds the lanes and the FP16 converters.
Whichever one first fails names the layer that is broken, which a single
end-to-end kernel cannot do.

Every stage reports the node's own CYCLE COUNT: `vec_cu.v` returns it as
`exec_result` on the RUN, and the agent mirrors it into NODE_STATUS. That is
the only cycle counter anything on this card exposes.
"""

import argparse
import contextlib
import io
import sys

import numpy as np

from ktpu.hw import device as dev
from ktpu.hw import vector as V
from ktpu.hw.device import TransportUnavailable
from ktpu.hw.fpga import HardwareError, Session

#: Well clear of any matmul image, which starts at word 0.
SRC = 0x0010_0000
DST = 0x0011_0000

#: One VLD/VST at VL=128 walks eight 256-bit words, so an operand is 8 words.
ELEMS = 128
OPERAND_WORDS = ELEMS * 2 // V.WORD_BYTES

MARKER = 0xDEAD_BEEF_DEAD_BEEF

#: The lanes have exp2 and not exp, so softmax scales by log2(e) first.
LOG2E = 1.4426950408889634


def build(stage: str) -> V.Kernel:
    """The kernel for one rung of the ladder.

    A0 walks DRAM lines for the fill; A1/A2 walk L1 words for the two loads,
    A3 for the store, A4 the drain back to DRAM. L1 is addressed in WORDS and
    DRAM in BYTES, which is why the strides differ by 32.
    """
    k = V.Kernel()
    if stage == "halt":
        return k.emit(V.vhalt())

    k.desc(0, SRC, V.dim(V.WORD_BYTES, 2 * OPERAND_WORDS))
    if stage == "copy":
        k.desc(4, DST, V.dim(V.WORD_BYTES, 2 * OPERAND_WORDS))
        return k.emit(V.vfill(0), V.vbar(), V.vdrain(4), V.vhalt())

    k.desc(1, 0, V.dim(1, OPERAND_WORDS))
    k.desc(2, OPERAND_WORDS, V.dim(1, OPERAND_WORDS))
    k.desc(3, 2 * OPERAND_WORDS, V.dim(1, OPERAND_WORDS))
    k.desc(4, DST, V.dim(V.WORD_BYTES, OPERAND_WORDS))
    k.seti(0, ELEMS)
    if stage == "softmax":
        k.seti(0, V.e8m15(LOG2E), to_k=True)
    k.emit(V.vsetvl(0), V.vsetmode(V.FLAT), V.vfill(0), V.vbar(), V.vld(0, 1))

    if stage == "add":
        k.emit(V.vld(1, 2), V.alu("VADD", vd=2, va=0, vb=1, vc=1), V.vst(2, 3))
    elif stage == "reduce":
        k.emit(
            V.vsetmode(V.TREE),
            V.vred(1, 0, "SUM"),
            V.vsetmode(V.FLAT),
            V.vbcast(1, 1),
            V.vst(1, 3),
        )
    else:
        k.emit(
            V.vsetmode(V.TREE),
            V.vred(1, 0, "MAX"),
            V.vsetmode(V.FLAT),
            V.vbcast(1, 1),
            V.alu("VSUB", vd=2, va=0, vc=1),
            V.alu("VMUL", vd=2, va=2, vb=3, sb=V.SRC_K),
            V.alu("VEXP2", vd=3, va=2),
            V.vsetmode(V.TREE),
            V.vred(2, 3, "SUM"),
            V.vsetmode(V.FLAT),
            V.vbcast(4, 2),
            V.alu("VINV", vd=5, va=4),
            V.alu("VMUL", vd=6, va=3, vb=5),
            V.vst(6, 3),
        )
    return k.emit(V.vdrain(4, 2 * OPERAND_WORDS), V.vhalt())


def operands(seed: int, stage: str):
    """Two FP16 arrays, and the exact bytes the fill will read.

    `add` and `reduce` use small integers, which are exact in both FP16 and
    E8M15, so their comparison downstream is an EQUALITY -- a tolerance would
    hide a lane that returned its input. `softmax` runs the transcendentals and
    cannot be exact, so it gets a spread of real values instead.
    """
    rng = np.random.default_rng(seed)
    if stage == "softmax":
        a = rng.uniform(-4, 4, ELEMS).astype(np.float16)
        b = rng.uniform(-4, 4, ELEMS).astype(np.float16)
    elif stage == "reduce":
        a = rng.integers(0, 8, ELEMS).astype(np.float16)
        b = rng.integers(0, 8, ELEMS).astype(np.float16)
    else:
        a = rng.integers(-64, 64, ELEMS).astype(np.float16)
        b = rng.integers(-64, 64, ELEMS).astype(np.float16)
    return a, b, a.tobytes() + b.tobytes()


def prefill(s, addr, nbytes):
    """Mark a region before the run: on this DRAM an unwritten line is SLVERR.

    It is also the false-pass guard. A `copy` that moved nothing would read
    back the marker, not zeros, and zeros are what an unwritten region and a
    correctly-computed 0 look like alike.
    """
    s.raw.write_block(
        s.board.mem_byte(0) + addr,
        MARKER.to_bytes(8, "little") * (nbytes // 8),
    )


def dispatch(s, node, kernel, timeout):
    """Stage the kernel at `node`, kick it, wait, and return NODE_STATUS.

    Raises HardwareError with the node's own signal count when the RUN does not
    retire -- the count says WHICH instruction it stopped on, which is the
    difference between "the kernel hung" and "instruction 7 hung".
    """
    flits = kernel.flits()
    before = node_status(s, node)["signals"]
    try:
        return V.run_kernel(s.link, node, flits, timeout), len(flits)
    except TimeoutError as exc:
        st = node_status(s, node)
        raise HardwareError(
            f"the RUN did not retire: {exc}\n"
            f"  node ({node[0]},{node[1]}) reported {st['signals'] - before} of "
            f"{len(flits)} signals, last code {st['code']:#04x} arg {st['arg']}\n"
            f"  {len(kernel.descs)} descriptors + {len(kernel.words)} words + 1 RUN"
        ) from exc


def node_status(s, node):
    word = s.link.read64(dev.MAG_BASE + dev.A_NODE + dev.node_index(*node) * 8)
    return V.decode_node_status(word)


def report_run(st, nflits, stage):
    """One line for the run itself, and the fault reason when there is one."""
    if st["fault"]:
        print(f"  FAULT  code {st['arg'] & 0xFF}: {st['why']}")
        return False
    print(f"  retired  {st['signals']} of {nflits} staged instructions")
    print(f"  CYCLES   {st['arg']}  <- the vector core's own counter")
    if stage != "halt" and st["arg"] == 0:
        print("  SUSPECT: zero cycles for a kernel that touches memory")
    return True


def check_copy(got, want):
    same = got == want
    print(f"  copied   {len(want)} bytes, byte-exact: {same}")
    if not same:
        first = next(i for i in range(len(want)) if got[i] != want[i])
        print(
            f"    first difference at byte {first}: {got[first]:#04x} "
            f"want {want[first]:#04x}"
        )
    return same


def check_add(raw, a, b):
    """Compare the drained FP16 against numpy, and say what else could pass.

    Reported next to the verdict on purpose: `a + b` equal to `a` is what a
    lane that ignored its second operand produces, and it is not visibly
    different from a correct answer at a glance.
    """
    got = np.frombuffer(raw, dtype=np.float16, count=ELEMS)
    want = (a.astype(np.float32) + b.astype(np.float32)).astype(np.float16)
    bad = int(np.count_nonzero(got != want))
    print(f"  elements {ELEMS}, mismatched {bad}")
    if bad:
        i = int(np.argmax(got != want))
        print(f"    first at {i}: got {got[i]} want {want[i]} " f"(a={a[i]} b={b[i]})")
        return False
    print(f"    not equal to a alone: {not np.array_equal(got, a)}")
    print(f"    not equal to b alone: {not np.array_equal(got, b)}")
    print(f"    not all zero:         {bool(np.any(got != 0))}")
    return True


def check_reduce(raw, a):
    """Every element must be sum(a): VRED into a scalar, broadcast back out.

    The broadcast is what makes this checkable at all -- a scalar register is
    not readable from the host, so the only way to see a reduction's result is
    to put it back in a vector and store it.
    """
    got = np.frombuffer(raw, dtype=np.float16, count=ELEMS)
    want = np.float16(a.astype(np.float64).sum())
    print(
        f"  sum      got {got[0]}  want {want}  uniform: {bool(np.all(got == got[0]))}"
    )
    print(f"    not equal to a:  {not np.array_equal(got, a)}")
    print(f"    not all zero:    {bool(np.any(got != 0))}")
    return bool(np.all(got == want))


def check_softmax(raw, a):
    """Compare against float64 softmax. The transcendentals are approximations.

    `exp2` and `1/x` are seeded LUT operations in E8M15, so this is a tolerance
    and the tolerance is reported rather than merely passed. The sum-to-one
    check is the one that would catch a normalisation that did not happen.
    """
    got = np.frombuffer(raw, dtype=np.float16, count=ELEMS).astype(np.float64)
    x = a.astype(np.float64)
    want = np.exp(x - x.max()) / np.exp(x - x.max()).sum()
    rel = np.abs(got - want) / np.maximum(want, 1e-12)
    print(
        f"  softmax  p50 rel {np.median(rel):.3e}  p99 {np.quantile(rel, 0.99):.3e}"
        f"  max {rel.max():.3e}"
    )
    print(f"    sums to  {got.sum():.6f}  (want 1.0)")
    print(f"    all positive:    {bool(np.all(got > 0))}")
    print(f"    argmax matches:  {int(np.argmax(got)) == int(np.argmax(want))}")
    return rel.max() < 5e-2 and abs(got.sum() - 1.0) < 5e-2


def run_stage(s, node, stage, seed, timeout, quiet=False):
    """One run of one rung, buffering its detail when soaking.

    A soak wants one line per run and the FULL detail of any run that fails, so
    the output is captured and released only when it is worth reading.
    """
    if not quiet:
        return _run_stage(s, node, stage, seed, timeout)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ok = _run_stage(s, node, stage, seed, timeout)
    if not ok:
        print(buf.getvalue())
    return ok


def _run_stage(s, node, stage, seed, timeout):
    print(f"\nnode ({node[0]},{node[1]})  stage {stage!r}  seed {seed:#x}")
    caps = dev.decode_caps(s.ctrl_read(node, dev.CU_CAPS, 2.0))
    if caps["type"] != dev.CU_VECTOR:
        print(f"  ({node[0]},{node[1]}) reports {caps['name']!r}, not a vector core")
        return False
    print(f"  {caps['name']!r} v{caps['version']}  inst_depth {caps['inst_depth']}")

    a, b, payload = operands(seed, stage)
    if stage != "halt":
        prefill(s, SRC, len(payload))
        prefill(s, DST, 2 * OPERAND_WORDS * V.WORD_BYTES)
        s.raw.write_block(s.board.mem_byte(0) + SRC, payload)

    st, nflits = dispatch(s, node, build(stage), timeout)
    if not report_run(st, nflits, stage):
        return False
    if stage == "halt":
        return True

    nbytes = len(payload) if stage == "copy" else ELEMS * 2
    raw = s.raw.read_block(s.board.mem_byte(0) + DST, nbytes)
    if stage == "copy":
        return check_copy(raw, payload)
    if stage == "reduce":
        return check_reduce(raw, a)
    if stage == "softmax":
        return check_softmax(raw, a)
    return check_add(raw, a, b)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--stage",
        default="halt",
        choices=("halt", "copy", "add", "reduce", "softmax"),
    )
    ap.add_argument("--node", help="x,y of one vector core (default: the first)")
    ap.add_argument("--all", action="store_true", help="every vector core in turn")
    ap.add_argument("--board")
    ap.add_argument("--transport", default="jtag")
    ap.add_argument("--seed", type=lambda v: int(v, 0), default=7)
    ap.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="soak: run the stage this many times, seed varying, and report "
        "only the runs that fail. An intermittent fault is invisible to one run",
    )
    ap.add_argument("--timeout", type=float, default=30.0)
    args = ap.parse_args()

    try:
        with Session(
            board=args.board, transport=args.transport, timeout=args.timeout
        ) as s:
            s.handshake()
            s.reset()
            if args.node:
                nodes = [tuple(int(v) for v in args.node.split(","))]
            elif args.all:
                nodes = [tuple(v) for v in s.board.vectors]
            else:
                nodes = [tuple(s.board.vectors[0])]
            if not nodes:
                print("board declares no vector cores", file=sys.stderr)
                return 2

            ok, bad = True, 0
            for node in nodes:
                for i in range(args.repeat):
                    try:
                        got = run_stage(
                            s,
                            node,
                            args.stage,
                            args.seed + i,
                            args.timeout,
                            quiet=args.repeat > 1 and i > 0,
                        )
                    except HardwareError as exc:
                        print(f"  HARDWARE ERROR\n{exc}")
                        got = False
                    ok &= got
                    bad += not got
            if args.repeat > 1:
                print(f"\n  soak: {bad} of {args.repeat * len(nodes)} runs failed")
            print("\nPASS" if ok else "\nFAIL")
            return 0 if ok else 1
    except TransportUnavailable as exc:
        print(f"no machine to run on: {exc}", file=sys.stderr)
        return 3
    except HardwareError as exc:
        print(f"the machine did not answer as expected:\n{exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    sys.exit(main())
