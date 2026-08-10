"""A core is not told twice what it already holds, and is told again when it does not.

Elision rests on ONE assumption: nothing between bands resets VL, VMODE or the
scalar file. Codegen emits a single continuous stream per core with no VHALT in
it, so the whole program is one kernel and `prog.rounds` slices it for the
dispatcher rather than restarting the core. If that ever stops being true these
tests still pass and the machine is wrong, so the assumption is written down in
`_emit_vector` as well as here.

The clobber rule is the part with teeth: a reduction writes `S[len(consts)+i]`,
so a VSETI that put a constant there did not survive and the next band must
re-issue it.
"""

import ktpu.dsl as D
from ktpu.codegen import codegen
from ktpu.codegen.cu import _emit_vector
from ktpu.dsl.nn import attention
from ktpu.ir import FP16
from ktpu.ir.program import Opcode
from ktpu.ir.sched import Engine
from ktpu.passes import lower
from ktpu.target import VU13P_8CU as T

L, DH, BLOCK = 128, 64, 64


def spec(*shape):
    return D.TensorSpec(shape, FP16)


def attn(causal=False):
    args = [spec(L, DH)] * 3 + ([spec(BLOCK, BLOCK)] if causal else [])
    return D.trace(
        lambda q, k, v, *r: attention(
            q, k, v, block=BLOCK, causal=causal, tri=r[0] if causal else None
        ),
        *args,
    )


def prog_of(causal=False):
    graph = attn(causal)
    sched = lower(graph, T)
    return codegen(sched, BLOCK if causal else L, DH, DH, T), sched


def replay(prog):
    """What each core holds after each of its instructions, walked in order.

    Returns a list of `(inst, state_before)` for every setup instruction, so a
    test can assert an issue was necessary rather than counting.
    """
    held: dict = {}
    out = []
    for i in prog.insts:
        if i.engine is not Engine.VECTOR:
            continue
        cur = held.setdefault(i.node, {})
        match i.op:
            case Opcode.VSETVL:
                out.append((i, dict(cur)))
                cur["vl"] = i.fields["vl"]
            case Opcode.VSETMD:
                out.append((i, dict(cur)))
                cur["md"] = (i.fields["mode"], i.fields["depth"])
            case Opcode.VSETI:
                out.append((i, dict(cur)))
                cur[("s", i.fields["sd"])] = i.fields["imm"]
            case Opcode.VRED:
                cur.pop(("s", i.fields["vd"]), None)
    return out


def test_no_setup_instruction_re_sets_what_the_core_already_holds():
    for causal in (False, True):
        prog, _ = prog_of(causal)
        for inst, before in replay(prog):
            match inst.op:
                case Opcode.VSETVL:
                    assert before.get("vl") != inst.fields["vl"], f"redundant {inst}"
                case Opcode.VSETMD:
                    want = (inst.fields["mode"], inst.fields["depth"])
                    assert before.get("md") != want, f"redundant {inst}"
                case Opcode.VSETI:
                    key = ("s", inst.fields["sd"])
                    assert before.get(key) != inst.fields["imm"], f"redundant {inst}"


def test_every_vector_instruction_runs_with_a_vl_and_a_mode_that_were_set():
    """The other direction: eliding one the core never received is the bug that
    would make the whole program read the wrong length."""
    for causal in (False, True):
        prog, _ = prog_of(causal)
        held: dict = {}
        for i in prog.insts:
            if i.engine is not Engine.VECTOR:
                continue
            cur = held.setdefault(i.node, {})
            match i.op:
                case Opcode.VSETVL:
                    cur["vl"] = i.fields["vl"]
                case Opcode.VSETMD:
                    cur["md"] = True
                case Opcode.VLD | Opcode.VST | Opcode.VALU | Opcode.VRED:
                    assert "vl" in cur, f"{i} runs before any VSETVL on {i.node}"
                    assert "md" in cur, f"{i} runs before any VSETMD on {i.node}"


def test_a_scalar_slot_a_reduction_clobbered_is_never_read_as_a_constant():
    """A band may read a reduction's scalar -- that is what the fused softmax
    does -- but only its OWN. Reading a slot a previous band's reduction wrote,
    because the VSETI that should have refreshed it was elided, is a plausible
    wrong answer rather than a crash, so it is checked per band.
    """
    cases = [prog_of(False), prog_of(True)]
    assert any(
        sum(1 for o in b.ops if o.kind.value in ("sum", "rmax")) >= 2
        for b in cases[0][1].bands
        if b.engine is Engine.VECTOR
    ), "the fused softmax band should hold two reductions"
    sched = lower(D.trace(_clobber, spec(128, 64)), T, reorder=False)
    cases.append((codegen(sched, 128, 64, 1, T), sched))

    for prog, sched in cases:
        vid_band = {o.out: b.name for b in sched.bands for o in b.ops}
        wrote: dict = {}
        for i in prog.insts:
            if i.engine is not Engine.VECTOR:
                continue
            last = wrote.setdefault(i.node, {})
            band = vid_band.get(i.fields.get("vid"))
            match i.op:
                case Opcode.VSETI:
                    last[i.fields["sd"]] = ("vseti", None)
                case Opcode.VRED:
                    last[i.fields["vd"]] = ("vred", band)
                    _read(i, last, band)
                case Opcode.VALU:
                    _read(i, last, band)


def _read(inst, last, band):
    for s in inst.fields["srcs"]:
        if not s.startswith("s"):
            continue
        kind, owner = last.get(int(s[1:]), ("nothing", None))
        assert kind == "vseti" or owner == band, (
            f"{inst} in {band} reads S{s[1:]}, last written by {kind} in "
            f"{owner}; the constant that belongs there was elided"
        )


def _clobber(x):
    """Bands that put 1.0 in S0, let a reduction overwrite S0, then want 1.0 again.

    Attention never reaches this, so it is built rather than hoped for: the
    middle band has no constants, so its `rmax` lands on S0 and the `+ 1.0`
    after it would otherwise read the row max as its constant. Lowered with
    `reorder=False`, because what is under test is codegen's clobber rule and
    the reorder would regroup the very bands the case is made of.
    """
    a = (x * 5.0).sum(axis=-1, keepdim=True) + 1.0
    b = x.max(axis=-1, keepdim=True) + 1.0
    return a + b + (x * 5.0).sum(axis=-1, keepdim=True)


def test_a_reduction_that_lands_on_a_constant_slot_forces_a_re_issue():
    """Without the invalidation this elides the second `VSETI S0, 1.0` and the
    add multiplies against a row max. Nothing else in the suite notices."""
    sched = lower(D.trace(_clobber, spec(128, 64)), T, reorder=False)
    kinds = [
        (b.name, [v for v, (k, _) in b.ext.items() if k == "const"])
        for b in sched.bands
        if b.engine is Engine.VECTOR
    ]
    assert any(not c for _, c in kinds), "no band without constants to clobber with"

    prog = codegen(sched, 128, 64, 1, T)
    mine = [i for i in prog.insts if i.node == (1, 1)]
    clobber = next(
        n for n, i in enumerate(mine) if i.op is Opcode.VRED and i.fields["vd"] == 0
    )
    after = mine[clobber + 1 :]
    read = next(
        n
        for n, i in enumerate(after)
        if i.op is Opcode.VALU and "s0" in i.fields["srcs"]
    )
    restored = [
        i
        for i in after[:read]
        if i.op is Opcode.VSETI and i.fields["sd"] == 0 and i.fields["imm"] == 1.0
    ]
    assert restored, (
        f"{after[read]} reads S0 after a reduction overwrote it, with no VSETI "
        "in between: it is multiplying against a row max"
    )


def test_passing_no_state_issues_the_setup_unconditionally():
    """The escape hatch has to work, or there is no way back if the assumption
    about state surviving a band boundary turns out to be false."""
    graph = attn(False)
    sched = lower(graph, T)
    full = codegen(sched, L, DH, DH, T)

    from ktpu.codegen.cu import allocate, assign, exports_of

    memory, reg = allocate(sched, T)
    counts = []
    for keep in (None, {}):
        prog = type(full)(name="probe")
        prog.memory = memory
        where = assign(sched, T)
        exports = exports_of(sched)
        for b in sched.bands:
            if b.engine is Engine.VECTOR:
                _emit_vector(
                    prog, b, where[b.name], T, reg, None, exports[b.name], {}, {}, keep
                )
        counts.append(sum(1 for i in prog.insts if i.op is Opcode.VSETVL))
    assert counts[0] > counts[1], "held=None must not elide anything"
