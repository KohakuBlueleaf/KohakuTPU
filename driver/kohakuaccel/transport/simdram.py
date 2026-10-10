"""Host DRAM traffic straight into a card model's memory, coherent with its Xache.

The simulated host moves its bytes over the modelled 64-bit AXI, one beat per
bus cycle of a whole-card simulation: a 512 KB tensor takes ~7 s of wall time.
`SimDram` serves every `read_block` / `write_block` that falls inside a mesh's
DRAM window from the DRAM model's own array (`axi_ram.mem`, public through
sim/verilator/card.vlt) and passes everything else to the transport it wraps.

Coherence. The Xache is write-through (docs/projects/kohakuaxi/xbar-cache.md
s2.4): DRAM always holds the newest data, so a read is served from DRAM as it
is. A write also zeroes every Xache row its lines index -- a row is
``{valid, tag, word}`` -- so the next access to them misses and refetches.
Like any host access it assumes nothing is in flight to those bytes: between
packages, as the driver uses memory.

The address map is the Xache's: bit pairs ``(i, i + log2 homes)`` swapped for
``i`` in ``[log2 interleave, home_lsb)``, the home in ``[home_lsb +: log2
homes]``, a 64-byte line indexing ``addr[6 +: log2 sets]`` of the swapped
address, its bank the index's low bits.
"""

from dataclasses import dataclass

from kohakuaccel.transport.base import Transport

LINE = 64


@dataclass(frozen=True)
class DramMap:
    """Where a flat DRAM address lives: home (channel), in-home address."""

    ilv_lg: int  # log2 of the interleave granule in bytes
    home_lsb: int  # first home bit of the swapped address
    nhome_lg: int  # log2 of the number of homes

    @classmethod
    def of_board(cls, board: dict, homes: int) -> "DramMap":
        ilv = board["dram_interleave_kb"] * 1024
        per_home = board["dram_flat_gb"] * (1 << 30) // homes
        return cls(
            ilv.bit_length() - 1, per_home.bit_length() - 1, homes.bit_length() - 1
        )

    def swap(self, addr: int) -> int:
        a = addr
        for i in range(self.ilv_lg, self.home_lsb):
            j = i + self.nhome_lg
            bi, bj = (a >> i) & 1, (a >> j) & 1
            a &= ~((1 << i) | (1 << j))
            a |= (bi << j) | (bj << i)
        return a

    def locate(self, addr: int) -> tuple[int, int]:
        """(home, in-home byte address) of a flat address."""
        a = self.swap(addr)
        return (a >> self.home_lsb) & ((1 << self.nhome_lg) - 1), a & (
            (1 << self.home_lsb) - 1
        )

    def runs(self, addr: int, nbytes: int):
        """(flat offset, home, in-home address, bytes) pieces, each contiguous in
        one home: the map moves only bits at or above the interleave granule."""
        g = 1 << self.ilv_lg
        off = 0
        while off < nbytes:
            a = addr + off
            n = min(nbytes - off, g - (a % g))
            home, inner = self.locate(a)
            yield off, home, inner, n
            off += n


class SimDram(Transport):
    """`inner` with its DRAM windows served from the model's arrays.

    `windows` are the host bases of the meshes' DRAM windows, each `size` bytes,
    every one onto the same flat memory. Raises :class:`RuntimeError` when the
    model has no public DRAM or Xache arrays to serve them from.
    """

    def __init__(self, inner, board: dict, windows, size: int) -> None:
        self.inner = inner
        self.bulk = inner.bulk
        self.windows = sorted(windows)
        self.size = size
        self.dram = sorted(
            (s, v, n, e) for s, v, n, e in inner.arrays(".u_dram") if v == "mem"
        )
        if not self.dram or any(e != LINE for *_, e in self.dram):
            raise RuntimeError(
                "no 512-bit public axi_ram.mem in this model: rebuild it with "
                "sim/verilator/card.vlt"
            )
        self.words = self.dram[0][2]
        self.map = DramMap.of_board(board, len(self.dram))
        # carray banks per home, in bank order: [home][bank] = (scope, rows, bytes)
        cache: dict = {}
        for s, v, n, e in inner.arrays(".u_xache."):
            if v != "mem" or ".g_home[" not in s or ".g_b[" not in s:
                continue
            h = int(s.split(".g_home[")[1].split("]")[0])
            b = int(s.split(".g_b[")[1].split("]")[0])
            cache.setdefault(h, {})[b] = (s, n, e)
        if sorted(cache) != list(range(len(self.dram))):
            raise RuntimeError(
                f"Xache arrays for homes {sorted(cache)}, DRAM has {len(self.dram)}"
            )
        self.banks = [[cache[h][b] for b in sorted(cache[h])] for h in sorted(cache)]
        self.sets = sum(n for _, n, _ in self.banks[0])
        self.served = 0  # bytes served from the arrays, for a caller's report

    def __getattr__(self, name: str):
        """Everything else is the wrapped model's (run, status, peek, ...)."""
        return getattr(self.inner, name)

    # ------------------------------------------------------------ routing
    def _flat(self, addr: int, nbytes: int):
        for w in self.windows:
            if w <= addr and addr + nbytes <= w + self.size:
                return addr - w
        return None

    def _word(self, inner: int) -> int:
        return (inner // LINE) % self.words

    # ---------------------------------------------------------- Transport
    def write64(self, addr: int, data: int) -> None:
        self.inner.write64(addr, data)

    def read64(self, addr: int) -> int:
        return self.inner.read64(addr)

    def read_block(self, addr: int, nbytes: int) -> bytes:
        flat = self._flat(addr, nbytes)
        if flat is None:
            return self.inner.read_block(addr, nbytes)
        out = bytearray(nbytes)
        for off, home, inner, n in self.map.runs(flat, nbytes):
            lo = inner % LINE
            words = (lo + n + LINE - 1) // LINE
            raw = self._peek_words(home, self._word(inner), words)
            out[off : off + n] = raw[lo : lo + n]
        self.served += nbytes
        return bytes(out)

    def write_block(self, addr: int, data: bytes) -> None:
        flat = self._flat(addr, len(data))
        if flat is None:
            self.inner.write_block(addr, data)
            return
        for off, home, inner, n in self.map.runs(flat, len(data)):
            lo = inner % LINE
            words = (lo + n + LINE - 1) // LINE
            w0 = self._word(inner)
            if lo or (lo + n) % LINE:
                buf = bytearray(self._peek_words(home, w0, words))
            else:
                buf = bytearray(words * LINE)
            buf[lo : lo + n] = data[off : off + n]
            self._poke_words(home, w0, bytes(buf))
            self._invalidate(home, inner - lo, words)
        self.served += len(data)

    # --------------------------------------------------------- the arrays
    def _peek_words(self, home: int, word: int, count: int) -> bytes:
        scope = self.dram[home][0]
        out = b""
        while count:  # a run may wrap the (simulated, smaller) channel
            n = min(count, self.words - word)
            out += self.inner.peek(scope, "mem", word, n)
            word, count = 0, count - n
        return out

    def _poke_words(self, home: int, word: int, data: bytes) -> None:
        scope = self.dram[home][0]
        while data:
            n = min(len(data) // LINE, self.words - word)
            self.inner.poke(scope, "mem", word, data[: n * LINE])
            word, data = 0, data[n * LINE :]

    def _invalidate(self, home: int, inner: int, lines: int) -> None:
        """Zero the carray rows of `lines` lines from in-home address `inner`."""
        banks = self.banks[home]
        nb = len(banks)
        idx0 = (inner // LINE) % self.sets
        per = {}
        for k in range(min(lines, self.sets)):
            idx = (idx0 + k) % self.sets
            per.setdefault(idx % nb, []).append(idx // nb)
        for b, rows in per.items():
            scope, _, ent = banks[b]
            rows.sort()
            start = prev = rows[0]
            for r in rows[1:] + [None]:
                if r is not None and r == prev + 1:
                    prev = r
                    continue
                self.inner.poke(scope, "mem", start, bytes(ent * (prev - start + 1)))
                if r is not None:
                    start = prev = r

    def __repr__(self) -> str:
        return (
            f"SimDram({self.inner!r}, {len(self.dram)} homes, {self.served} B served)"
        )
