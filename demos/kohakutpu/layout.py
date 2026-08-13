"""RUN THIS. The byte orders, what each costs, and the one asymmetry.

Same numbers, different order, and the machine CANNOT TELL THEM APART. An
operand packed for the wrong tiling is the right bytes in the wrong places and
reads back as noise -- which is why layout is a level of its own.
"""

import numpy as np

from kohakutpu import layout as LO

RULE = "=" * 78
rng = np.random.default_rng(0)


def head(title: str) -> None:
    print(f"\n{RULE}\n{title}\n{RULE}")


X = (rng.standard_normal((8, 64)) * 0.5).astype(np.float16)

head("EVERY ORDER, AND WHAT IT IS FOR")
print(f"  {'layout':<22} {'bytes':>7}  what it is")
for lay, what in (
    (LO.Flat(), "row-major fp16; the only order whose rows are rows"),
    (LO.Entry(2, 2), "L1 entries of 4x32, tile-major; what FILL streams"),
    (LO.Entry(1, 1), "the same entries, one group and one block per run"),
    (LO.Entry(8, 2), "the shipped default for a gm=8 nk=2 operand"),
):
    raw = lay.pack(X)
    assert np.array_equal(lay.unpack(raw, X.shape), X.astype(np.float64))
    print(f"  {lay.key:<22} {len(raw):>7}  {what}")

C = (rng.standard_normal((8, 8)) * 0.5).astype(np.float16)
tile = LO.Tile(grid=(1, 2), gm=2, gn=1)
raw = tile.pack(C)
print(f"  {tile.key:<22} {len(raw):>7}  4x4 sub-tiles, blocked per instance")
print("\n  Every one holds the SAME numbers and round-trips exactly. They differ")
print("  only in which byte a given element is at.")

head("THE PERMUTATION, MADE VISIBLE")
print("Row 0 of an 8x8 result, three ways:\n")
print("  row-major  ", np.round(C[0].astype(float), 3))
print("  as stored  ", np.round(np.frombuffer(raw, np.float16)[:8].astype(float), 3))
print("  unpacked   ", np.round(tile.unpack(raw, (8, 8))[0], 3))
print("\n  The middle line is what a DRAIN really wrote. Read it as row 0 and")
print("  every number is wrong, and nothing faults.")

head("THE ASYMMETRY -- why layout cannot be an afterthought")
back = tile.unpack(raw, (8, 8))
stored = np.frombuffer(raw, np.float16).astype(float)
print("  ELEMENTWISE work commutes with a permutation:")
print(f"    sum over the drained bytes  {stored.sum():.4f}")
print(f"    sum over the row-major      {C.astype(np.float64).sum():.4f}")
print("\n  A ROW REDUCTION does not -- 'row 0' is not row 0 until unpacked:")
print(f"    max of the first 8 stored   {stored[:8].max():.4f}")
print(f"    max of the real row 0       {back[0].max():.4f}")
print("\n  So a fused matmul-then-silu needs NO relayout and a softmax does.")
print("  That single fact is why `Compiled.conversions` exists, and why a")
print("  relayout is counted: this machine has no instruction that reorders")
print("  memory, so every one is a round trip THROUGH THE HOST.")

head("CHANNELBIAS -- the order that made a per-channel bias possible")
print("A bias is `N` values that every row reads. Stored flat and broadcast to")
print("`M x N`, it costs M times the memory to hold N distinct numbers.\n")
n = 64
bias = np.arange(n, dtype=np.float16)
chan = LO.ChannelBias(8)
print(f"  {'order':<22} {'bytes':>8}  for a {n}-wide bias against 64 rows")
print(f"  {'Flat, broadcast to MxN':<22} {64 * n * 2:>8}")
print(f"  {chan.key:<22} {chan.nbytes((n,)):>8}")
print(f"\n  {64 * n * 2 / chan.nbytes((n,)):.0f}x smaller, and no pass of its own:")
print("  the epilogue reads it at stride 0 beside the resident tile.")

words = np.frombuffer(chan.pack(bias), np.float16).reshape(-1, LO.LANES, LO.LANES)
print("\n  One 4x4 sub-tile is ONE word, so a column group's bias sits in all")
print("  four rows of it. Word 0 of the packed bias:\n")
for row in words[0]:
    print(f"    {np.round(row.astype(float), 1)}")
print("\n  That repeat down the rows is a stride-0 dimension in the descriptor,")
print("  not four copies in memory.")

head("PACKING IS EXACT, AND CHECKED")
print(f"  {'layout':<22} {'round trips':>12}")
for lay in (LO.Flat(), LO.Entry(2, 2), LO.Entry(8, 2), tile):
    shape = (8, 8) if isinstance(lay, LO.Tile) else (8, 64)
    src = C if shape == (8, 8) else X
    ok = np.array_equal(lay.unpack(lay.pack(src), shape), src.astype(np.float64))
    print(f"  {lay.key:<22} {'exact' if ok else 'LOSSY':>12}")
print("\n  fp16 in, fp16 out, bit for bit. A layout moves bytes and never")
print("  rounds -- every loss on this machine is arithmetic, never packing.")
