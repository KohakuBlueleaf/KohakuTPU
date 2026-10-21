"""A matmul cluster for values (`mx_cluster_cu.v`; the verifier's
`compiler.verify.cluster` for its order): FILL, GEMM, DRAIN and POST words in
order, and a drain queue sending sub-tiles to their sink as credits allow.

L1 A and B each hold 512 MXFP7 entries (`layout.tensor.pack`: 4 lanes x 32
values and 4 E5M3 scales, A or B packing). A GEMM sweeps gm x gn sub-tiles
over nk K-blocks -- sub-tile (i, j) reads A entry i*nk + k and B entry j*nk +
k from its offsets -- into sub-tile i*gn + j of the accumulator, exactly
(float64), on top of what is there with `acc`. A drain writes sub-tiles as
the clusters' fp16 (rounded, saturating at 65504, no subnormals): one word a
sub-tile, element (r, c) at 4r + c; into another cluster's accumulator two
granules of fp32."""

from collections import deque

import numpy as np

from kohakutpu.compiler.isa.cluster import DFLAG_COUNT, DFLAG_SIGNAL, ISA
from kohakutpu.language.numerics.mxfp7 import SBIAS
from kohakutpu.simulation.behave.vector import COMPLETE, Unmodelled

#: CU_DATA buffers: L1 A, L1 B, the accumulator (an add).
BUF_L1A, BUF_L1B, BUF_PEER = 0, 1, 2
ENTRY = 128
ENTRIES = 512
WORD = 32
FP16_MAX = 65504.0


def entry_values(raw: bytes, blayout: bool) -> np.ndarray:
    """One L1 entry's [4 lanes, 32] values (`layout.tensor.pack` inverted)."""
    out = np.zeros((4, 32))
    for w in range(4):
        word = int.from_bytes(raw[w * WORD : (w + 1) * WORD], "little")
        fields = [(word >> (24 - 8 * lane)) & 0xFF for lane in range(4)]
        for s in range(32):
            q = (word >> (249 - 7 * s)) & 0x7F
            q = q - 128 if q & 0x40 else q
            lane, kk = (s % 4, s // 4) if blayout else (s // 8, s % 8)
            out[lane, w * 8 + kk] = q
        for lane in range(4):
            es, m8 = (fields[lane] >> 3) - SBIAS, (fields[lane] & 7) + 8
            out[lane, w * 8 : w * 8 + 8] *= (m8 / 8.0) * 2.0**es
    return out


def cluster_fp16(x: np.ndarray) -> bytes:
    """Sub-tile values as the clusters' fp16: rounded, saturating, no
    subnormals."""
    h = np.clip(x, -FP16_MAX, FP16_MAX).astype(np.float16)
    h = np.where(np.abs(h) < np.float16(2.0**-14), np.float16(0), h)
    return h.astype(np.float16).tobytes()


class Cluster:
    """One cluster at `at` (mesh, x, y) on card `net`, over `memory`."""

    def __init__(self, net, at: tuple, memory) -> None:
        self.net, self.at, self.mem = net, at, memory
        self.words: deque = deque()
        self.task = None  # the word in progress
        self.out: list = []
        self.done = 0
        self.l1 = {
            BUF_L1A: bytearray(ENTRIES * ENTRY),
            BUF_L1B: bytearray(ENTRIES * ENTRY),
        }
        self.acc: dict = {}  # sub-tile -> [4, 4]
        self.rx = {BUF_L1A: 0, BUF_L1B: 0}
        self.taken = {BUF_L1A: 0, BUF_L1B: 0}
        self.cr = self.spent = 0
        #: the drain queue: [fields, granules (bytes each) left to send]
        self.queue: deque = deque()

    def deliver(self, payload: int) -> None:
        self.words.append(payload)

    def credit(self, n: int) -> None:
        self.cr += n

    def land(self, buf: int, off: int, data: bytes, count: bool) -> None:
        """A CU_DATA burst of granules from granule `off` of `buf`."""
        if buf in self.l1:
            side = self.l1[buf]
            if (off + len(data) // WORD) * WORD > len(side):
                raise Unmodelled(f"a burst past L1 {'AB'[buf]} (F_CUDATA)")
            side[off * WORD : off * WORD + len(data)] = data
            if count:
                self.rx[buf] += (off + len(data) // WORD) // 4 - off // 4
        elif buf == BUF_PEER:
            vals = np.frombuffer(data, np.float32).astype(np.float64).reshape(-1, 4, 4)
            for i, v in enumerate(vals):
                t = off // 2 + i
                self.acc[t] = self.acc.get(t, np.zeros((4, 4))) + v
        else:
            raise Unmodelled(f"a burst into cluster buffer {buf}")

    # ------------------------------------------------------------- steps
    def step(self) -> bool:
        before = self.done
        self._send()
        while True:
            if self.task is not None:
                try:
                    next(self.task)
                    break
                except StopIteration:
                    self.task = None
                    self.out.append((COMPLETE, 0))
                    self.done += 1
            if not self.words:
                break
            fmt = ISA.set.find(self.words[0])
            if fmt is None:
                raise Unmodelled(
                    f"a word that decodes as no cluster word: {self.words[0]:#x}"
                )
            self.task = self._word(fmt.name, fmt.decode(self.words.popleft()))
        self._send()
        return self.done != before

    def _word(self, name: str, f: dict):
        if name == "FILL":
            side = f["sel"]
            if f["recv"]:
                while self.rx[side] - self.taken[side] < f["n"]:
                    yield
                self.taken[side] += f["n"]
                return
            addr = f["addr"] | f["addr_hi"] << 34
            for i in range(f["n"]):
                e = (f["fbank"] << 8) | ((f["eoff"] + i) & 0xFF)
                self.l1[side][e * ENTRY : (e + 1) * ENTRY] = self.mem.read_block(
                    addr + i * ENTRY, ENTRY
                )
        elif name == "GEMM":
            self._gemm(f)
        elif name == "DRAIN":
            if f["fuse"]:
                while self.queue:  # the emitted sub-tiles leave first
                    yield
                return
            self._push(f, [self.acc.get(t, np.zeros((4, 4))) for t in range(f["n"])])
            while self.queue:
                yield
        elif name == "POST":
            self.net.credit(self.at[0], (f["dst_x"], f["dst_y"]), f["n"])
        return
        yield  # a generator, even where nothing waits

    def _gemm(self, f: dict) -> None:
        gm, gn, nk = f["gm"], f["gn"], f["nk"]

        def side(buf, bank, off, i, k):
            e = (bank << 8) | ((off + i * nk + k) & 0xFF)
            return entry_values(
                bytes(self.l1[buf][e * ENTRY : (e + 1) * ENTRY]), buf == BUF_L1B
            )

        a = [
            [side(BUF_L1A, f["abank"], f["aoff"], i, k) for k in range(nk)]
            for i in range(gm)
        ]
        b = [
            [side(BUF_L1B, f["bbank"], f["boff"], j, k) for k in range(nk)]
            for j in range(gn)
        ]
        tiles = []
        for i in range(gm):
            for j in range(gn):
                t = i * gn + j
                v = sum(a[i][k] @ b[j][k].T for k in range(nk))
                if f["acc"]:
                    v = v + self.acc.get(t, np.zeros((4, 4)))
                self.acc[t] = v
                tiles.append(v)
        if f["emit"]:
            self._push(f, tiles)

    def _push(self, f: dict, tiles: list) -> None:
        """Sub-tiles to their sink: memory at once (one word each, from
        `addr`); onto the drain queue for a node, one granule of fp16 each
        into an L1, two of fp32 into an accumulator."""
        if not f["dnode"]:
            addr = f["addr"] | f["addr_hi"] << 34
            for t, v in enumerate(tiles):
                self.mem.write_block(addr + WORD * t, cluster_fp16(v))
            return
        if f["dfin"]:
            raise Unmodelled("a cluster drain into another mesh")
        if f["dbuf"] == BUF_PEER:
            grans = [
                g
                for v in tiles
                for raw in [v.astype(np.float32).tobytes()]
                for g in (raw[:WORD], raw[WORD:])
            ]
        else:
            grans = [cluster_fp16(v) for v in tiles]
        self.queue.append([f, deque(grans), (f["addr"] >> 5) & 0xFFFF])

    def _send(self) -> None:
        """The drain queue's head item, as far as credits allow."""
        while self.queue:
            f, grans, at = self.queue[0]
            n = len(grans)
            if f["credit"]:
                n = min(n, self.cr - self.spent)
                if n <= 0:
                    return
                self.spent += n
            data = b"".join(grans.popleft() for _ in range(n))
            self.net.burst(
                self.at[0],
                (f["dst_x"], f["dst_y"]),
                f["dbuf"],
                at,
                data,
                count=bool(f["dflags"] & DFLAG_COUNT),
                signal=bool(f["dflags"] & DFLAG_SIGNAL) and not grans,
                ack=(f["dack_x"], f["dack_y"]),
            )
            self.queue[0][2] = at + n
            if not grans:
                self.queue.popleft()


__all__ = ["Cluster", "cluster_fp16", "entry_values"]
