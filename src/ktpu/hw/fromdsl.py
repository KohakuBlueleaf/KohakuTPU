"""DSL -> level 3 -> machine code, laid out so the bench can execute it.

`bench` owns the operand image, the upload regions and the control program.
This module produces the other half from the DSL -- the instruction flits a
cluster executes -- relocated onto the layout the bench already uploaded, so
the two are swappable. `test_machine_code.py` compares the result against the
planner's flit for flit and `scripts/py/run_dsl.py` runs it, both through here,
so a divergence cannot be one caller reconciling differently from the other.

Three interface facts have to be reconciled:

  units  the legacy `Layout` counts 256-bit words, a `Program` 32-bit ones.
  preq   `bench.build` pre-quantises into memory, so its FILLs set the bit.
  emit   the legacy GEMM carries C's base, so the sweep hands sub-tiles out as
         it finishes and the DRAIN is a barrier (`fuse`).
"""

import ktpu.dsl as D
from ktpu.codegen import codegen
from ktpu.codegen.encode import flit
from ktpu.ir import FP16
from ktpu.ir.program import Opcode
from ktpu.passes import lower
from ktpu.target import Target

ONE_CU = Target(name="one-cu", clusters=1, vector_cores=0)

_CLUSTER_OPS = (Opcode.FILL, Opcode.GEMM, Opcode.DRAIN)


def program(m, k, n, target=ONE_CU):
    """The level 3 program for `C[m,n] = A[m,k] @ B.T[n,k]`, traced from the DSL."""
    graph = D.trace(
        lambda a, b: a @ b, D.TensorSpec((m, k), FP16), D.TensorSpec((k, n), FP16)
    )
    return codegen(lower(graph, target), m, k, n, target)


def flits(prob, m, k, n, target=ONE_CU):
    """ktpu's machine code for this problem, relocated onto `prob`'s layout.

    ``prob`` is a `bench.Problem` of the same shape -- it supplies where the
    operands were actually uploaded. Returns the flits in the order the staging
    RAM wants them.

    Raises ValueError if the DSL lowered to more than the single pass this
    relocation understands.
    """
    prog = program(m, k, n, target)

    lay = prob.layout
    base = {"v0": lay.a_word * 8, "v1": lay.b_word * 8, "v2": lay.c_word * 8}
    spans = {r.name: (r.word, r.end) for r in prog.memory.regions}

    for inst in prog.insts:
        if "word" in inst.fields:
            for name, (lo, hi) in spans.items():
                if lo <= inst.fields["word"] < hi and name in base:
                    inst.fields["word"] += base[name] - lo
                    break
        match inst.op:
            case Opcode.FILL:
                inst.fields["preq"] = 1
            case Opcode.GEMM:
                inst.fields["word"] = base["v2"]
                inst.fields["emit"] = 1
            case Opcode.DRAIN:
                inst.fields["fuse"] = 1
            case _:
                pass

    cluster = [i for i in prog.insts if i.op in _CLUSTER_OPS]
    if len(cluster) != 4:
        raise ValueError(
            f"{len(cluster)} cluster instructions; this relocation handles the "
            "single-pass case (fill, fill, gemm, drain) only"
        )
    out = [flit(i, last=i.op is Opcode.DRAIN) for i in cluster]
    # The planner fills B before A -- B is the resident operand, so it is loaded
    # once and A streams against it.
    return [out[1], out[0], *out[2:]]


def restage(prob, m, k, n, target=ONE_CU):
    """Replace `prob`'s planner-built machine code with the DSL's, in place.

    Only `Pass.flits` change. The operand image, the upload regions and the
    control program are the bench's own, so what runs afterwards differs from a
    normal run in exactly one thing: which compiler emitted the instructions.

    Raises ValueError if `prob` is not the single-pass shape this handles.
    """
    passes = prob.kern.passes
    if len(passes) != 1:
        raise ValueError(f"{len(passes)} passes; restage handles one")
    mine = flits(prob, m, k, n, target)
    if len(mine) != len(passes[0].flits):
        raise ValueError(
            f"ktpu emitted {len(mine)} flits, the planner staged "
            f"{len(passes[0].flits)}; the control program counts them"
        )
    passes[0].flits[:] = mine
    return prob
