"""Check the declared KohakuTPU ISA against the encoder that has run on silicon.

``kohakutpu/hw/matmul.py`` hand-packs the same payload and its output is what the
bitstream has actually executed, so it is the authority. This compares the two
bit for bit over a spread of field values -- if the field table drifts, a shifted
field shows up here rather than as traffic that routes plausibly and computes
something else.

Run as ``python -m tests.test_ktpu_isa`` from ``compiler/``.
"""

PAYLOAD = (1 << 256) - 1


def _legacy():
    """The hand-packed encoder, kept as the witness when `src/ktpu` was retired."""
    from kohakutpu.hw import matmul

    return matmul


CASES = [
    ("fill_a", dict(op=1, addr=0x1234_5678, n=64, sel=0)),
    ("fill_b", dict(op=1, addr=0x3_FFFF_FFFF, n=255, sel=1)),
    ("fill_off", dict(op=1, addr=0x40, n=7, sel=0, eoff=200, abank=1, fbank=1)),
    ("gemm", dict(op=2, gm=16, gn=32, nk=25)),
    ("gemm_acc", dict(op=2, gm=4, gn=4, nk=1, acc=True, aoff=8, boff=12, bbank=1)),
    ("gemm_emit", dict(op=2, gm=8, gn=8, nk=3, emit=True, fuse=True)),
    ("drain", dict(op=3, addr=0xDEAD_0000, n=512)),
    (
        "drain_node",
        dict(op=3, addr=0, n=16, dnode=True, dst=(2, 3), dbuf=2, dflags=1, dack=(1, 0)),
    ),
]


def _legacy_payload(matmul, spec: dict) -> int:
    return matmul._flit(**spec) & PAYLOAD


def _declared_payload(spec: dict) -> int:
    from kohakutpu.isa import ISA

    kw = dict(spec)
    op = kw.pop("op")
    dst = kw.pop("dst", (0, 0))
    dack = kw.pop("dack", (0, 0))
    dfin = kw.pop("dfin", (0, 0))
    peers = kw.pop("peers", ())
    kw.update(
        dst_x=dst[0],
        dst_y=dst[1],
        dack_x=dack[0],
        dack_y=dack[1],
        dfin=(dfin[1] & 0xF) << 4 | (dfin[0] & 0xF),
    )
    kw.update(ISA.peers(peers))
    kw.setdefault("anchor", ISA.cfg.anchor)
    fmt = {1: ISA.FILL, 2: ISA.GEMM, 3: ISA.DRAIN}[op]
    kw = {k: int(v) for k, v in kw.items()}
    return fmt.encode(**kw)


def test_declared_isa_matches_the_silicon_encoder() -> None:
    matmul = _legacy()
    for name, spec in CASES:
        want = _legacy_payload(matmul, spec)
        got = _declared_payload(spec)
        assert got == want, (
            f"{name}: declared {got:#068x}\n"
            f"{' ' * len(name)}  legacy   {want:#068x}\n"
            f"{' ' * len(name)}  xor      {got ^ want:#068x}"
        )


VEC_CASES = [
    ("imem_0", "imem_flit", {"addr": 0, "word": 0xDEADBEEF}),
    ("imem_max", "imem_flit", {"addr": 511, "word": 0xFFFFFFFF}),
    ("desc", "desc_flit", {"ad": 3, "fld": 5, "value": 0x1_2345_6789}),
    ("desc_max", "desc_flit", {"ad": 7, "fld": 7, "value": (1 << 34) - 1}),
    ("run_0", "run_flit", {"pc": 0}),
    ("run_pc", "run_flit", {"pc": 300}),
]


def test_declared_vector_isa_matches_the_silicon_encoder() -> None:
    """The vector core's three flit kinds, against the encoder that has run."""
    from kohakutpu.hw import vector
    from kohakutpu.isa.vector import ISA as VEC

    build = {"imem_flit": VEC.imem, "desc_flit": VEC.desc, "run_flit": VEC.run}
    for name, fn, kw in VEC_CASES:
        want = getattr(vector, fn)(**kw) & PAYLOAD
        got = build[fn](**kw)
        assert got == want, (
            f"{name}: declared {got:#068x}\n  legacy {want:#068x}\n"
            f"  xor    {got ^ want:#068x}"
        )


def test_vector_program_ends_in_run() -> None:
    from kohakutpu.isa.vector import ISA as VEC

    flits = VEC.program([0x1111, 0x2222], pc=0)
    assert len(flits) == 3
    assert VEC.set.disasm(flits[-1]).startswith("RUN")


def test_fill_refuses_a_wrapping_count() -> None:
    from kohakutpu.isa import ISA

    ISA.fill(addr=0, n=255)
    try:
        ISA.fill(addr=0, n=256)
    except ValueError:
        return
    raise AssertionError("a 256-entry FILL was accepted; the count wraps to 0")


def test_disassembly_names_the_opcode() -> None:
    from kohakutpu.isa import ISA

    text = ISA.set.disasm(ISA.gemm(gm=4, gn=4, nk=2))
    assert text.startswith("GEMM"), text


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    bad = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception as exc:
            bad += 1
            print(f"  FAIL  {t.__name__}: {exc}")
    print(f"  {len(tests) - bad}/{len(tests)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
