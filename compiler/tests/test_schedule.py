"""isa.schedule.late_drains against hand-written cluster streams."""

from kohakutpu.isa import ISA
from kohakutpu.isa.schedule import late_drains


def _tile(base: int, chunks: int, first_bank: int = 0) -> list[int]:
    """A tile as the backend emits it: FILL A, FILL B, GEMM per chunk, banks
    alternating, the last GEMM emitting, then its fused DRAIN."""
    out = []
    for c in range(chunks):
        bank = (first_bank + c) % 2
        out.append(ISA.fill(base + c * 0x1000, 128, sel=0, fbank=bank))
        out.append(ISA.fill(base + c * 0x1000 + 0x800, 128, sel=1, fbank=bank))
        last = c == chunks - 1
        out.append(
            ISA.gemm(64, 64, 2, acc=int(c > 0), abank=bank, bbank=bank, emit=int(last))
        )
    out.append(ISA.drain(base + 0x100000, 4096, fuse=1))
    return out


def _is(word: int, op: str) -> bool:
    return getattr(ISA, op).matches(word)


def test_a_fused_drain_moves_to_just_before_the_next_emitting_gemm():
    t0, t1 = _tile(0x0, 4), _tile(0x40000, 4)
    got = late_drains(t0 + t1)
    assert sorted(got) == sorted(t0 + t1)
    drain0 = t0[-1]
    at = got.index(drain0)
    # Everything of tile 1 up to its emitting GEMM runs before tile 0's drain.
    assert got[:at] == t0[:-1] + t1[:-2]
    assert got[at + 1] == t1[-2] and ISA.GEMM.decode(t1[-2])["emit"] == 1
    assert got[-1] == t1[-1]


def test_a_drain_stops_at_a_fill_into_the_bank_the_emitting_sweep_reads():
    # Three chunks end on bank 0; tile 1 starts filling bank 0 again.
    t0, t1 = _tile(0x0, 3), _tile(0x40000, 3)
    got = late_drains(t0 + t1)
    at = got.index(t0[-1])
    assert got[at + 1] == t1[0] and _is(t1[0], "FILL")
    assert got[:at] == t0[:-1]


def test_a_drain_with_nothing_after_it_stays_last():
    t0 = _tile(0x0, 2)
    assert late_drains(t0) == t0


def test_an_unfused_drain_is_a_barrier_nothing_moves_past():
    t0 = _tile(0x0, 2)
    plain = ISA.drain(0x200000, 64)
    t1 = _tile(0x40000, 2)
    got = late_drains(t0 + [plain] + t1)
    assert got.index(t0[-1]) < got.index(plain)
