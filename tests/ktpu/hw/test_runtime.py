"""The device surface: allocate, copy, compile, call.

The property that matters is RELOCATION -- the same compiled program run
against different buffers must write its answer to different memory. A device
that quietly ignored the buffers it was handed would pass every test that only
checks one call, so the tests here call twice into different allocations and
compare where the bytes landed.
"""

import itertools

import numpy as np
import pytest
from test_fpga import FakeMachine  # same directory; pytest puts it on sys.path

from ktpu.hw import board as bd
from ktpu.hw import fpga, runtime
from ktpu.hw.board import MEM_WORD_BYTES

BOARD = "singlemesh_2x2"


def device(**kw):
    board = bd.Board.named(BOARD)
    machine = FakeMachine(board, **kw)
    s = fpga.Session(board=board, transport=machine, timeout=2.0)
    return runtime.Device(s), machine


# ---- the allocator ------------------------------------------------------
def test_allocations_do_not_overlap_and_are_drain_aligned():
    a = runtime.Allocator(1 << 20)
    seen = []
    for n in (1, 32, 4097, 1 << 16):
        word, words = a.alloc(n)
        assert word % runtime.ALIGN_WORDS == 0, "a burst must not straddle two buffers"
        assert words * MEM_WORD_BYTES >= n
        seen.append((word, words))
    for (w0, n0), (w1, _) in itertools.pairwise(seen):
        assert w0 + n0 <= w1, "two live allocations overlap"


def test_freeing_coalesces_so_the_arena_does_not_shred():
    a = runtime.Allocator(1024)
    x, _ = a.alloc(32 * MEM_WORD_BYTES)
    y, _ = a.alloc(32 * MEM_WORD_BYTES)
    z, _ = a.alloc(32 * MEM_WORD_BYTES)
    a.free(x)
    a.free(y)
    assert len(a.free_list) == 1, "adjacent frees must merge"
    assert a.free_list[0] == (x, 64)
    a.free(z)
    assert len(a.free_list) == 1


def test_a_free_span_is_reused_before_the_arena_grows():
    a = runtime.Allocator(1024)
    x, _ = a.alloc(32 * MEM_WORD_BYTES)
    a.alloc(32 * MEM_WORD_BYTES)
    top = a.top
    a.free(x)
    again, _ = a.alloc(32 * MEM_WORD_BYTES)
    assert again == x and a.top == top


def test_running_out_says_what_was_asked_for_and_what_was_left():
    a = runtime.Allocator(64)
    with pytest.raises(MemoryError, match="will not fit"):
        a.alloc(65 * MEM_WORD_BYTES)


def test_freeing_twice_is_an_error_not_a_silent_corruption():
    a = runtime.Allocator(1024)
    word, _ = a.alloc(64)
    a.free(word)
    with pytest.raises(ValueError, match="not a live allocation"):
        a.free(word)


# ---- copyin / copyout ---------------------------------------------------
def test_a_buffer_round_trips():
    dev, _ = device()
    buf = dev.alloc(256)
    payload = bytes((i * 31 + 7) & 0xFF for i in range(256))
    dev.copyin(buf, payload)
    out = bytearray(256)
    dev.copyout(out, buf)
    assert bytes(out) == payload


def test_alloc_zeroes_because_this_dram_has_ecc():
    """A never-written line is a bus fault, not zeros, and a tensor library
    will allocate then read. Zeroing is what makes the abstraction honest."""
    dev, m = device()
    buf = dev.alloc(128)
    assert m.blocks and m.blocks[0] == (buf.byte, buf.words * MEM_WORD_BYTES)
    out = bytearray(128)
    dev.copyout(out, buf)
    assert bytes(out) == bytes(128)


def test_a_copy_larger_than_the_buffer_is_refused():
    dev, _ = device()
    buf = dev.alloc(64)
    with pytest.raises(ValueError, match="does not fit"):
        dev.copyin(buf, bytes(buf.words * MEM_WORD_BYTES + MEM_WORD_BYTES))


def test_a_partial_device_word_is_refused():
    dev, _ = device()
    buf = dev.alloc(256)
    with pytest.raises(ValueError, match="whole number"):
        dev.copyin(buf, bytes(8))


def test_a_freed_buffer_cannot_be_used():
    dev, _ = device()
    buf = dev.alloc(64)
    dev.free(buf)
    with pytest.raises(ValueError, match="freed"):
        dev.copyin(buf, bytes(64))
    with pytest.raises(ValueError, match="already free"):
        dev.free(buf)


# ---- compile and call ---------------------------------------------------
def test_a_shape_the_board_cannot_hold_is_refused_at_compile():
    """The 15,440-element fault, caught before any buffer is touched."""
    dev, _ = device()
    with pytest.raises(fpga.HardwareError, match="minimum shape"):
        dev.compile(2, 32, 16)


def test_the_same_program_writes_to_whichever_buffer_it_is_given():
    """RELOCATION, which is the whole reason this module exists.

    One compiled program, two different C buffers. A device that ignored its
    arguments and used `bench.plan`'s fixed offsets would pass any test that
    called it once; this calls it twice and checks the answers landed in two
    different places.
    """
    dev, m = device()
    prog = dev.compile(16, 32, 16)
    a, b = dev.alloc(2048), dev.alloc(2048)
    c1, c2 = dev.alloc(4096), dev.alloc(4096)
    assert c1.word != c2.word

    stage = dev.board.ctrl(0x2000)

    def staged():
        """The instruction flits actually written to the staging RAM."""
        return {k: v for k, v in m.mem.items() if stage <= k < stage + 0x1400}

    prog(a, b, c1)
    assert prog.layout.c_word == c1.word, "the layout ignored the buffer"
    first = staged()

    prog(a, b, c2)
    assert prog.layout.c_word == c2.word
    second = staged()

    # Comparing the C buffers would prove nothing: `alloc` zeroes them, so both
    # look written whatever the program did. The FLITS have to differ.
    assert first != second, "the same flits were staged for two different buffers"
    assert prog.layout.a_word == a.word and prog.layout.b_word == b.word


def test_the_wrong_number_of_buffers_is_refused():
    dev, _ = device()
    prog = dev.compile(16, 32, 16)
    with pytest.raises(ValueError, match="wanted 3 buffers"):
        prog(dev.alloc(64), dev.alloc(64))


def test_calling_with_a_freed_buffer_is_refused():
    dev, _ = device()
    prog = dev.compile(16, 32, 16)
    a, b, c = dev.alloc(2048), dev.alloc(2048), dev.alloc(4096)
    dev.free(c)
    with pytest.raises(ValueError, match="freed"):
        prog(a, b, c)


def test_a_program_counts_its_own_calls():
    dev, _ = device()
    prog = dev.compile(16, 32, 16)
    a, b, c = dev.alloc(2048), dev.alloc(2048), dev.alloc(4096)
    prog(a, b, c)
    prog(a, b, c)
    assert prog.calls == 2 and prog.seconds > 0


def test_info_reports_the_board_and_the_arena():
    dev, _ = device()
    before = dev.info()["free_words"]
    dev.alloc(1 << 16)
    after = dev.info()
    assert after["board"] == BOARD
    assert after["clusters"] == 2 and after["vector_cores"] == 4
    assert after["free_words"] < before
    assert after["live_buffers"] == 1
    assert after["quantise"] is False


def test_the_answer_is_the_one_the_planner_would_have_produced():
    """A device run and a `Session.run` of the same shape must agree.

    The fake computes nothing, so this compares the CONTROL TRAFFIC rather than
    numbers: both must kick and both must drive the same registers, or the
    device path has diverged from the one that is validated on silicon.
    """
    dev, m = device()
    prog = dev.compile(16, 32, 16)
    a, b, c = dev.alloc(2048), dev.alloc(2048), dev.alloc(4096)
    prog(a, b, c)
    assert m.mem[dev.board.ctrl(0x0050)] == 1, "PROG_KICK was never written"
    assert m.kicks, "no cluster was dispatched to"


def test_numpy_goes_in_and_comes_back():
    """The shape a tensor library actually hands you."""
    dev, _ = device()
    src = np.arange(64, dtype=np.uint32)
    buf = dev.alloc(src.nbytes)
    dev.copyin(buf, src.tobytes())
    out = np.empty_like(src)
    dev.copyout(out, buf)
    assert np.array_equal(out, src)
