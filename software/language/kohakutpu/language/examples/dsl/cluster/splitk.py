"""``y = x @ w.T`` for a long K split over the nodes by a cross-node
connection: node n's clusters sum their K slice of every output tile, and
every node but 0 adds its accumulators into node 0's clusters across the
interlink at full accumulator precision (``kt.drain(acc,
kt.node[0].mg[u], add=True)``); node 0 drains the sums. One output tile a
cluster (M one tile of rows, N up to a node's clusters' tiles). On one node
it is the plain product.

The opposite of the Xache-shared examples (`linear`, `mlp`, ...), where the
nodes never address one another: here the kernel names the far unit."""

import kohakutpu.language.dsl as kt
from kohakutpu.language.examples.dsl.cluster.sweep import (
    a_layout,
    b_layout,
    core_blocks,
    nk_for,
)


@kt.kernel
def splitk(
    x=kt.In("M", "K", dtype="mx7a", layout=a_layout("M")),  # one tile of all M rows
    w=kt.In("N", "K", dtype="mx7b", layout=b_layout()),
    y=kt.Out("M", "N"),
    *,
    tn=kt.auto(256, 128, 64, 32),
    rows=kt.auto(8, 4),
):
    M, K = x.shape
    N = w.shape[0]
    m = kt.machine()
    tm = M
    kc = 32 * nk_for(K)
    kn = K // m.nodes
    kt.require(M % 4 == 0 and M <= 256, f"M = {M}: one tile of rows (at most 256)")
    kt.require(N % tn == 0 and N // tn <= m.mg, f"N = {N}: one tile of {tn} a cluster")
    kt.require(
        K % m.nodes == 0 and kn % kc == 0,
        f"K = {K} over {m.nodes} nodes in chunks of {kc}",
    )
    h = kt.buffer((M, N), "f16", tile=(tm // 4, tn // 4))
    for n in kt.par(m.nodes, on=kt.node):
        for u in kt.par(N // tn, on=kt.mg):
            acc = kt.zero(tm, tn)
            for k in kt.range(kn // kc):
                k0 = n * kn + k * kc
                a = kt.fill(x[0:tm, k0 : k0 + kc])
                b = kt.fill(w[u * tn : u * tn + tn, k0 : k0 + kc])
                acc = kt.gemm(a, b, acc)
            if m.nodes == 1:
                kt.drain(acc, h[0:tm, u * tn : u * tn + tn])
            else:
                with kt.when(n == 0):
                    kt.drain(acc, h[0:tm, u * tn : u * tn + tn])
                with kt.when(n != 0):  # into node 0's tile, across the interlink
                    kt.drain(acc, kt.node[0].mg[u], add=True)
    for r0, col, rper in core_blocks(M, N, tn, rows, tm):
        for t in kt.range(rper // rows):
            r = r0 + t * rows
            kt.store(
                y[r : r + rows, col : col + tn],
                kt.load(h[r : r + rows, col : col + tn]),
            )


__all__ = ["splitk"]
