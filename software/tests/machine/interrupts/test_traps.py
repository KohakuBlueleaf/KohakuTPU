"""S<->M and U<->S round trips, an injected S interrupt, the S timer, and the
same loops fetched from IMEM and from DRAM."""


def test_traps_and_interrupts(probe):
    r = probe("core2", "trap", 2_000_000)
    assert r["rc"] == 0 and not r["fails"], r["output"][-2000:]


def test_instruction_fetch_from_dram(probe):
    r = probe("core2", "itext", 4_000_000)
    assert r["rc"] == 0 and not r["fails"], r["output"][-2000:]
