"""RUN THIS. Levels 1 and 0 -- instructions and dispatch, written by hand.

THIS IS NOT HOW TO USE THE CARD. It is here so the bottom is inspectable, and so
the levels above have something concrete to be an abstraction OF. Doing it by
hand means owning the two rules that fail as a hang rather than a wrong number.

Level 1 is a typed instruction stream per unit; level 0 is the ISA field table
that makes each one a 256-bit payload, and the artifact saying what to stage,
kick and await.
"""

import os

import numpy as np
from kohakuaccel.dispatch import plan
from kohakuaccel.runtime import execute
from kohakutpu.api import DEFAULT_TARGET, DEVICE_ENV
from kohakutpu.hw import tensor as T
from kohakutpu.isa import ISA

from kohakutpu import layout as LO

M, K, N = 8, 64, 4
GM, GN, NK = 2, 1, 2


def machine(kind: str):
    """``(memory, control, node, base)`` for the card or for the unit models."""
    if kind == "sim":
        from kohakutpu.model import ARENA_BASE, SimDevice

        dev = SimDevice()
        return dev.card, dev.card, dev.machine.coords("MG")[0], ARENA_BASE
    from kohakutpu.host import Card

    card = Card()
    return card.raw, card.ctrl, card.idle("MG")[0], 0x1A00_0000


WHERE = os.environ.get(DEVICE_ENV) or DEFAULT_TARGET
raw, ctrl, node, BASE = machine(WHERE)
print("SimDevice (by hand)" if WHERE == "sim" else "CARD (by hand)")

rng = np.random.default_rng(0)
A = (rng.standard_normal((M, K)) * 0.5).astype(np.float16)
B = (rng.standard_normal((N, K)) * 0.5).astype(np.float16)

# LEVEL 2 -- addresses and byte order, chosen here because nothing below decides.
A_AT, B_AT, C_AT = BASE, BASE + 0x1_0000, BASE + 0x2_0000
a_order, b_order = LO.Entry(GM, NK), LO.Entry(GN, NK)
c_order = LO.Tile(grid=(1, 1), gm=GM, gn=GN)
raw.write_block(A_AT, a_order.pack(A))
raw.write_block(B_AT, b_order.pack(B))

# LEVEL 1 -- one instance's instruction stream, still symbolic.
entry = T.LANES * T.KBLOCK * 2
chunks = -(-(K // T.KBLOCK) // NK)
program = []
for k in range(chunks):
    program.append(("fill", A_AT + k * GM * NK * entry, GM * NK, 0))
    program.append(("fill", B_AT + k * GN * NK * entry, GN * NK, 1))
    program.append(("gemm", GM, GN, NK, 1 if k else 0))
program.append(("drain", C_AT, GM * GN))

print("level 1 -- the instruction stream")
for step in program:
    print(f"  {step}")

# LEVEL 0 -- the ISA field table, then an artifact.
words = []
for step in program:
    match step:
        case ("fill", addr, n, sel):
            words.append(ISA.fill(addr=addr, n=n, sel=sel))
        case ("gemm", gm, gn, nk, acc):
            words.append(ISA.gemm(gm=gm, gn=gn, nk=nk, acc=acc))
        case ("drain", addr, n):
            words.append(ISA.drain(addr=addr, n=n))

# `plan` owns the two rules a hand-written dispatch gets wrong: a round restages
# from slot 0, and a node kicked twice is awaited ONCE for the total.
artifacts = plan({(0, 0): words}, [node], name="by-hand")
print(f"\nlevel 0 -- {len(words)} payloads, {len(artifacts)} round(s) on {node}")
for art in artifacts:
    print(f"  {art.summary()}")
    execute(art.to_dict(), ctrl)

got = c_order.unpack(raw.read_block(C_AT, c_order.nbytes((M, N))), (M, N))
want = A.astype(np.float64) @ B.astype(np.float64).T
err = np.abs(got - want).max() / np.abs(want).max()
print(f"\nerror {err:.3%} of full scale -- the same answer as `matmul(x, w)`")

RULE = "=" * 78
print(f"\n{RULE}\nEVERY PAYLOAD, DECODED\n{RULE}")
print("A 256-bit word is a field table, and it decodes back. This is the whole")
print("of level 0 -- there is nothing below it but the wire.\n")
for word, step in zip(words, program):
    name, fields = ISA.set.decode(word)
    live = {k: v for k, v in fields.items() if v}
    print(f"  {step[0]:<6} -> {name:<6} {live}")

print(f"\n{RULE}\nWHAT THE LEVELS ABOVE DO FOR YOU\n{RULE}")
print(f"  by hand here          {len(program)} instructions, addresses and byte")
print("                        orders chosen above, one instance only")
from kohakutpu.ops import matmul as op_matmul

made = op_matmul.plan(
    *[__import__("kohakutpu.api", fromlist=["api"]).tensor(a) for a in (A, B)]
)
print(
    f"  ops.matmul(a, b)      {len(made.instances)} instances over "
    f"{len(made.stages)} stage(s), grid {made.grid}"
)
print(f"                        layouts {[v.key for v in made.layouts.values()]}")
print("\n  The hand-written version pins ONE instance at ONE address with ONE")
print("  tiling. Everything the compiler adds -- the grid, the byte orders, the")
print("  padding, the rounds -- is what you are opting out of by coming here.")

print(f"\n{RULE}\nTHE TWO RULES THAT HANG RATHER THAN LIE\n{RULE}")
print("  1. A ROUND RESTAGES FROM SLOT 0. An instruction stream longer than the")
print("     instruction memory is several rounds, and each one starts its")
print("     staging at slot 0 again. Continue from where the last stopped and")
print("     the unit runs stale words.")
print("  2. A NODE KICKED TWICE IS AWAITED ONCE, for the TOTAL. Await each kick")
print("     separately and the second wait never returns.")
print("\n  `kohakuaccel.dispatch.plan` owns both, which is why this demo calls it")
print(
    f"  rather than writing the artifact by hand. It made {len(artifacts)} "
    f"round(s) here."
)
