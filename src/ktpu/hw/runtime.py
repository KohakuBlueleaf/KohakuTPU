"""The device surface: buffers you allocate, code you compile, calls you make.

    dev = Device(session)
    a, b, c = dev.alloc(na), dev.alloc(nb), dev.alloc(nc)
    dev.copyin(a, host_a); dev.copyin(b, host_b)
    prog = dev.compile(m, k, n)
    prog(a, b, c); dev.synchronize()
    dev.copyout(out, c)

WHY THIS IS NOT JUST `Session.run`, which builds one problem, lays it out with
`bench.plan` and executes it -- code and memory decided together and discarded
together. A device cannot work that way: a caller allocates and frees on its
own schedule and expects compiled code to run against whichever buffers it is
handed. The seam between them is RELOCATION, and it is what `launch` does.

Shaped like tinygrad's `Compiled`: alloc/free/copyin/copyout is its Allocator,
`compile` its Compiler, `Compiled.__call__` its Program, `synchronize` its
runtime. What a backend would still have to add is in the module's own notes.
"""

import time
from dataclasses import dataclass, field

from ktpu.hw import bench
from ktpu.hw.board import MEM_WORD_BYTES
from ktpu.hw.fpga import HardwareError

# C regions are drained DRAIN_BURST words at a time, so every allocation starts
# on that boundary and a burst can never straddle two buffers.
ALIGN_WORDS = bench.DRAIN_BURST


@dataclass
class Buffer:
    """A span of device memory, in 256-bit words. Not a host array.

    `word` is what the machine addresses and `nbytes` what a caller copies, so
    both are carried rather than derived -- a buffer rounded up to the drain
    alignment is larger than the tensor in it, and confusing the two writes
    past the end of the caller's data.
    """

    word: int
    words: int
    nbytes: int
    device: object = None
    freed: bool = False

    @property
    def byte(self) -> int:
        return self.device.board.mem_byte(self.word)

    def __repr__(self):
        return (
            f"Buffer(word={self.word}, {self.nbytes} B{' FREED' if self.freed else ''})"
        )


class Allocator:
    """A bump allocator with a free list over the board's DRAM.

    Deliberately simple and deliberately NOT silent when it runs out: an arena
    this large is not going to be exhausted by the shapes we run, and a
    first-fit list that quietly fragments would be harder to explain than an
    exception naming what was asked for and what was left.
    """

    def __init__(self, words: int, base: int = 0):
        self.base, self.words = base, words
        self.top = base
        self.free_list: list[tuple[int, int]] = []  # (word, words), sorted
        self.live: dict[int, int] = {}

    def alloc(self, nbytes: int) -> tuple[int, int]:
        """Reserve space for `nbytes`; returns (word, words) rounded to ALIGN."""
        if nbytes <= 0:
            raise ValueError(f"cannot allocate {nbytes} bytes")
        need = -(-nbytes // MEM_WORD_BYTES)
        need = -(-need // ALIGN_WORDS) * ALIGN_WORDS
        for i, (word, have) in enumerate(self.free_list):
            if have >= need:
                self.free_list.pop(i)
                if have > need:
                    self._release(word + need, have - need)
                self.live[word] = need
                return word, need
        if self.top + need > self.base + self.words:
            raise MemoryError(
                f"{nbytes} bytes ({need} words) will not fit: {self.available()} "
                f"words free of {self.words}, largest run {self.largest()}"
            )
        word, self.top = self.top, self.top + need
        self.live[word] = need
        return word, need

    def free(self, word: int) -> None:
        if word not in self.live:
            raise ValueError(f"word {word} is not a live allocation")
        self._release(word, self.live.pop(word))

    def _release(self, word, words):
        """Return a span and coalesce it with its neighbours."""
        self.free_list.append((word, words))
        self.free_list.sort()
        merged = []
        for w, n in self.free_list:
            if merged and merged[-1][0] + merged[-1][1] == w:
                merged[-1] = (merged[-1][0], merged[-1][1] + n)
            else:
                merged.append((w, n))
        self.free_list = merged

    def available(self) -> int:
        return (self.base + self.words - self.top) + sum(n for _, n in self.free_list)

    def largest(self) -> int:
        tail = self.base + self.words - self.top
        return max([tail, *(n for _, n in self.free_list)], default=0)


@dataclass
class Compiled:
    """Machine code whose operand addresses are still symbolic.

    THIS IS THE WHOLE POINT OF THE MODULE. A flit carries an absolute word
    address, so code compiled for one set of buffers cannot run against
    another -- which is exactly what a device has to do. The instructions are
    kept unencoded, with the region each address belongs to, and encoded at
    call time against the buffers actually passed.
    """

    m: int
    k: int
    n: int
    args: tuple  # region name per positional buffer, in call order
    device: object = None
    calls: int = 0
    seconds: float = 0.0
    log: list = field(default_factory=list)
    # Where the last call put its operands. Observability, and the only way to
    # check relocation without re-deriving the layout in the test.
    layout: object = None

    def __call__(self, *buffers, wait: bool = True) -> float:
        return self.device.launch(self, buffers, wait=wait)


class Device:
    """One board, presented as something a tensor library can drive.

    Wraps a :class:`~ktpu.hw.fpga.Session`, which keeps the transport, the
    board and the control-program execution. Everything here is about turning
    that into allocate / copy / compile / launch.
    """

    def __init__(self, session, arena_words=None):
        self.session = session
        self.board = session.board
        self.allocator = Allocator(arena_words or self.board.mem_words)
        self.buffers: list[Buffer] = []

    # ---- allocator ------------------------------------------------------
    def alloc(self, nbytes: int, zero: bool = True) -> Buffer:
        """Reserve device memory. ZEROED BY DEFAULT, and that is not politeness.

        This DRAM has ECC, so reading a line that was never written is an
        uncorrectable error and comes back as a bus fault rather than as zeros.
        A caller that allocates and reads before writing -- which any tensor
        library will do -- would get an exception from the transport instead of
        a buffer full of nothing. Pass `zero=False` only when the very next
        thing is a full-width `copyin`.
        """
        word, words = self.allocator.alloc(nbytes)
        buf = Buffer(word=word, words=words, nbytes=nbytes, device=self)
        self.buffers.append(buf)
        if zero:
            self.session.raw.write_block(buf.byte, bytes(words * MEM_WORD_BYTES))
        return buf

    def free(self, buf: Buffer) -> None:
        if buf.freed:
            raise ValueError(f"{buf!r} is already free")
        self.allocator.free(buf.word)
        buf.freed = True

    def copyin(self, buf: Buffer, data) -> int:
        """Host bytes -> device buffer. Returns bytes written.

        Whole words only: the memory port is 256 bits wide and a partial word
        would need a read-modify-write the transport does not offer.
        """
        data = bytes(data)
        self._check(buf, len(data))
        if len(data) % MEM_WORD_BYTES:
            raise ValueError(
                f"{len(data)} bytes is not a whole number of "
                f"{MEM_WORD_BYTES}-byte device words"
            )
        self.session.raw.write_block(buf.byte, data)
        return len(data)

    def copyout(self, dest, buf: Buffer) -> memoryview:
        """Device buffer -> host. `dest` is a writable buffer of the size wanted."""
        view = memoryview(dest).cast("B")
        self._check(buf, len(view))
        if len(view) % MEM_WORD_BYTES:
            raise ValueError(
                f"{len(view)} bytes is not a whole number of "
                f"{MEM_WORD_BYTES}-byte device words"
            )
        view[:] = self.session.raw.read_block(buf.byte, len(view))
        return view

    def _check(self, buf, nbytes):
        if buf.freed:
            raise ValueError(f"{buf!r} has been freed")
        if nbytes > buf.words * MEM_WORD_BYTES:
            raise ValueError(f"{nbytes} bytes does not fit {buf!r} ({buf.words} words)")

    # ---- compiler -------------------------------------------------------
    def compile(self, m, k, n) -> Compiled:
        """Machine code for `C[m,n] = A[m,k] @ B.T[n,k]`, buffers unresolved.

        Refuses a shape the BOARD cannot hold, for the same reason
        `Session.check` does: a tile planned for capacities the bitstream does
        not have overruns the CU silently and the run reports success.
        """
        why = self.session.check(m, k, n)
        if why:
            raise HardwareError("; ".join(why))
        return Compiled(m=m, k=k, n=n, args=("a", "b", "c"), device=self)

    # ---- program --------------------------------------------------------
    def launch(self, prog: Compiled, buffers, wait: bool = True) -> float:
        """Run `prog` against these buffers. Returns seconds.

        RELOCATION HAPPENS HERE. The problem is rebuilt with a layout that
        points at the caller's buffers rather than at `bench.plan`'s offsets,
        so the same `Compiled` runs against different memory -- which is the
        difference between a device and a benchmark harness.
        """
        if len(buffers) != len(prog.args):
            raise ValueError(
                f"{prog.args} wanted {len(prog.args)} buffers, got {len(buffers)}"
            )
        for buf in buffers:
            if buf.freed:
                raise ValueError(f"{buf!r} has been freed")
        a, b, c = buffers
        t0 = time.time()
        prob = self._relocate(prog, a, b, c)
        prog.layout = prob.layout
        for i, control in enumerate(prob.programs):
            self.session._round(i, control, prog.log)
        if wait:
            self.synchronize()
        prog.calls += 1
        prog.seconds += time.time() - t0
        return time.time() - t0

    def _relocate(self, prog, a, b, c):
        """A problem whose operand layout is the caller's buffers.

        `bench.build` chooses the layout itself, so this rebuilds and then
        overwrites it -- the flits are regenerated against the new addresses,
        which is what makes the code position-independent in practice even
        though the ISA has no base register.
        """
        prob = bench.build(
            prog.m,
            prog.k,
            prog.n,
            ncl=self.board.ncl,
            preq=(False, False),
            mesh_layout=self.board.layout,
            caps=self.board.caps(),
        )
        want = bench.matmul.Layout(a_word=a.word, b_word=b.word, c_word=c.word)
        if (prob.layout.a_word, prob.layout.b_word, prob.layout.c_word) != (
            want.a_word,
            want.b_word,
            want.c_word,
        ):
            prob.layout = want
            prob.kern = bench.kernel.plan(
                prob.m,
                prob.k,
                prob.n,
                prob.kern.clusters,
                want,
                prob.tile,
                stage_flits=self.board.stage_flits,
                ncmd=bench.NCMD,
                preq=prob.kern.preq,
                l1_a=self.board.l1_a,
                l1_b=self.board.l1_b,
            )
            prob.programs = bench.kernel.programs(prob.kern)
        return prob

    def synchronize(self) -> None:
        """Block until the machine is quiescent.

        The control program already polls each round to completion, so by the
        time `launch` returns nothing is outstanding. This confirms it rather
        than assuming: a dispatcher still running here means a round retired
        while its cluster had not.
        """
        self.session.reset()

    # ---- what a caller needs to know ------------------------------------
    def info(self) -> dict:
        return {
            "board": self.board.name,
            "clusters": self.board.ncl,
            "vector_cores": len(self.board.vectors),
            "arena_words": self.allocator.words,
            "free_words": self.allocator.available(),
            "live_buffers": sum(1 for b in self.buffers if not b.freed),
            "quantise": self.board.quant_bit is not None,
        }
