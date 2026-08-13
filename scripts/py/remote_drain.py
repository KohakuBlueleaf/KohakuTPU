"""Drain one cluster's tile into a cluster on ANOTHER mesh, and check the sum.

    python scripts/py/remote_drain.py                  # mesh_0 -> mesh_2
    python scripts/py/remote_drain.py --src 1 --dst 3

P4, `docs/interlink/paths.md`. The compiler never emits `dmesh`/`dfin`, so a
matmul is compiled normally and only its DRAIN word is rewritten.

`dbuf=2` is `OP_ADD_PEER`: the sub-tile is ADDED to the receiver's resident tile
at full ACU precision, and `isa/cluster.md` s9.4 is explicit that the tile memory
has no reset -- the receiver must have run a GEMM that opened those sub-tiles, or
the burst lands on leftovers and reads back as 65504 everywhere. So the receiver
computes P2 and keeps it, the sender sends P1, and the tile must read P1 + P2.
"""

import argparse

import numpy as np
from kohakuaccel.artifact import Artifact, Await, Kick, SeedCredits
from kohakuaccel.device.registers import MAG_BASE
from kohakuaccel.rt import Runtime
from kohakuaccel.runtime import execute
from kohakutpu.host import Card
from kohakutpu.isa import ISA
from kohakutpu.kernels import matmul
from kohakutpu.rt import Device
from ktpu.hw import interlink as IL

#: Where the far cluster is asked to put what it received.
READOUT = 0x0400_0000

M, K, N = 32, 64, 4
TILING = {"gm": 8, "gn": 1, "nk": 2}
NSUB = TILING["gm"] * TILING["gn"]


class Board:
    """What `ktpu.hw.interlink` wants of a board: a name and `ctrl(offset)`."""

    def __init__(self, mesh) -> None:
        self.name = f"mesh_{mesh.index}"

    @staticmethod
    def ctrl(off: int) -> int:
        return MAG_BASE + off


class Pinned(Device):
    """A device that dispatches to one named node, optionally patching the tail."""

    def __init__(self, card, node, patch=None, **kw) -> None:
        super().__init__(card, **kw)
        self.node, self.patch = node, patch

    def dispatch(self, payloads, unit, name="kernel", nodes=None, acks=None):
        if self.patch is not None:
            payloads = {
                k: list(w[:-1]) + [self.patch(w[-1])] for k, w in payloads.items()
            }
        return Runtime.dispatch(self, payloads, unit, name, [self.node], acks)


def remote(word: int, agent, dst_mesh: int, fin) -> int:
    """One DRAIN word, aimed at `fin` in `dst_mesh` instead of at memory.

    `dst` is this mesh's own MAG port, so the local routers see an ordinary
    local-destination flit and never learn another mesh exists; `dfin` nonzero
    is what makes it remote. The ack MUST be named: across meshes `(0,0)` means
    "answer the sender" and the sender's coordinate exists in the far mesh too.
    """
    name, f = ISA.set.decode(word)
    if name != "DRAIN":
        raise ValueError(f"the stage's last word is a {name}, not a DRAIN")
    f.pop("op")
    f.update(
        addr=0,  # granule 0; buf 2 is a sub-tile pair, so it must be even
        dnode=1,
        dst_x=agent[0],
        dst_y=agent[1],
        dbuf=2,
        dflags=1,
        dack_x=agent[0],
        dack_y=agent[1],
        dmesh=dst_mesh,
        dfin=(fin[1] & 0xF) << 4 | (fin[0] & 0xF),
    )
    return ISA.DRAIN.encode(**f)


def readout(card, mesh, fin) -> np.ndarray:
    """Drain cluster `fin`'s resident tile to its own DRAM and read it back."""
    mesh.mem.write_block(READOUT, bytes(NSUB * 32))
    art = Artifact(
        machine="readout",
        flits=[ISA.drain(addr=mesh.dram_base | READOUT, n=NSUB)],
        steps=[SeedCredits(512), Kick(fin, 0, 1), Await(fin, 1)],
    )
    execute(art.to_dict(), mesh.ctrl, timeout=20.0)
    card.select(mesh.index)
    return card.read_tile(READOUT, TILING["gm"], TILING["gn"])[:M, :N]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=int, default=0, help="sending mesh")
    ap.add_argument("--dst", type=int, default=2, help="receiving mesh")
    args = ap.parse_args()

    card = Card(which=[args.src, args.dst])
    dst = card.select(args.dst)
    src = card.select(args.src)
    print(f"{card}\nroute = {IL.route(args.src, args.dst)}")
    sender, fin = src.coords("MG")[1], dst.coords("MG")[3]
    print(f"sender mesh_{args.src}{sender} -> receiver mesh_{args.dst}{fin}")

    rng = np.random.default_rng(7)
    a1 = rng.normal(0, 0.02, (M, K)).astype(np.float16)
    a2 = rng.normal(0, 0.02, (M, K)).astype(np.float16)
    b = rng.normal(0, 1.00, (N, K)).astype(np.float16)
    P1 = a1.astype(np.float32) @ b.astype(np.float32).T
    P2 = a2.astype(np.float32) @ b.astype(np.float32).T

    send = Pinned(card, sender, patch=lambda w: remote(w, src.agent, args.dst, fin))
    card.select(args.dst)
    recv = Pinned(card, fin)
    card.select(args.src)

    print(f"\n1. mesh_{args.dst} computes P2 and keeps it resident")
    got2 = matmul(recv.tensor(a2), recv.tensor(b), **TILING).numpy()
    print(f"   P2 vs numpy: max {np.abs(got2 - P2).max():.4e}")
    print(
        f"   its own drain left the tile intact: "
        f"{np.abs(readout(card, dst, fin) - got2).max():.4e}"
    )

    print(f"\n2. mesh_{args.src} sends P1 into it")
    sb, db = Board(src), Board(dst)
    before = IL.counters(src.ctrl, sb)["link1"]
    matmul(send.tensor(a1), send.tensor(b), **TILING)
    after = IL.counters(src.ctrl, sb)["link1"]
    print(
        f"   link1 +{after['tx_packets'] - before['tx_packets']} packets, "
        f"+{after['tx_beats'] - before['tx_beats']} beats"
    )
    print(
        f"   faults: mesh_{args.src}={IL.faults(src.ctrl, sb)} "
        f"mesh_{args.dst}={IL.faults(dst.ctrl, db)}"
    )

    print("\n3. the far tile must be P1 + P2, not either alone")
    summed = readout(card, dst, fin)
    for label, ref in (("P1 + P2", P1 + P2), ("P1 alone", P1), ("P2 alone", P2)):
        e = np.abs(summed - ref)
        print(f"   vs {label:9}: max {e.max():.4e}  p50 {np.median(e):.4e}")

    tol = 5e-2 * np.abs(np.stack([P1, P2])).max()
    added = np.abs(summed - (P1 + P2)).max() < tol < np.abs(summed - P1).max()
    print(f"\n   TWO-PARTIAL ADD ACROSS MESHES: {added}")
    return 0 if added else 1


if __name__ == "__main__":
    raise SystemExit(main())
