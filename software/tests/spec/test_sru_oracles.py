"""Independent SRU oracle provenance and hand-computed spec witnesses."""

import ast
import sys
from fractions import Fraction
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SRU_SCRIPTS = ROOT / "scripts/py/sru"
if str(SRU_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SRU_SCRIPTS))

import oracle

GENERATORS = (
    "oracle.py",
    "unpack_vectors.py",
    "mx7_vectors.py",
    "red_vectors.py",
    "fma_vectors.py",
    "fn_vectors.py",
    "core_vectors.py",
    "cu_vectors.py",
)

FORBIDDEN = {"core", "red", "fn", "e8"}


def forbidden_imports(source: str) -> list[str]:
    """Return any behavioural module imports found in `source`."""
    bad = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in FORBIDDEN or alias.name.startswith(
                    "kohakutpu.simulation.behave"
                ):
                    bad.append(f"line {node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            top = mod.split(".")[0]
            names = [a.name for a in node.names]
            if (
                top in FORBIDDEN
                or mod.startswith("kohakutpu.simulation.behave")
                or any(n in FORBIDDEN for n in names)
            ):
                bad.append(f"line {node.lineno}: from {mod} import {names}")
    return bad


@pytest.mark.parametrize("filename", GENERATORS)
def test_sru_generator_imports_no_behavioural_module(filename):
    path = SRU_SCRIPTS / filename
    assert path.is_file(), f"missing {path}"
    bad = forbidden_imports(path.read_text(encoding="utf-8"))
    assert not bad, f"{filename} imports behavioural modules: {bad}"


@pytest.mark.parametrize(
    "snippet",
    [
        "import core\n",
        "import red\n",
        "import fn\n",
        "import e8\n",
        "from sru import core\n",
        "import kohakutpu.simulation.behave.vector\n",
    ],
)
def test_behavioural_import_is_rejected(snippet):
    assert forbidden_imports(snippet), f"failed to reject: {snippet!r}"


def _check_unpack_spec(f_ieee, f_acc):
    # NUM.F16.001: normal 1.0 (0x3C00: e=15, m=0) -> E8M15 e=127, m=0 (0x3F8000)
    assert f_ieee(0x3C00) == 0x3F8000
    assert f_acc(0x3C00) == 0x3F8000
    # NUM.F16.001: signed zeros +-0 -> 0x000000 / 0x800000
    assert f_ieee(0x0000) == 0x000000
    assert f_ieee(0x8000) == 0x800000
    assert f_acc(0x0000) == 0x000000
    assert f_acc(0x8000) == 0x800000
    # NUM.F16.002 vs ISA.SR.047 / RUL.057: smallest subnormal 2^-24 (0x0001)
    # IEEE normalises to e = 103 (0x338000); accumulator form flushes e=0 to +0
    assert f_ieee(0x0001) == 0x338000
    assert f_acc(0x0001) == 0x000000
    # NUM.F16.002 vs ISA.SR.047 / RUL.057: exponent 31 (0x7C00)
    # IEEE unpacks to +inf (e=255, 0x7F8000); accumulator form reads binade 2^16 (e=143, 0x478000)
    assert f_ieee(0x7C00) == 0x7F8000
    assert f_acc(0x7C00) == 0x478000


def test_oracle_unpack_and_mutant_witness():
    _check_unpack_spec(oracle.f16_to_e8_ieee, oracle.f16_to_e8_acc)
    with pytest.raises(AssertionError):
        _check_unpack_spec(lambda h: oracle.f16_to_e8_ieee(h) ^ 1, oracle.f16_to_e8_acc)
    with pytest.raises(AssertionError):
        _check_unpack_spec(oracle.f16_to_e8_ieee, oracle.f16_to_e8_ieee)


def _check_e8f16_acc_spec(conv):
    # ISA.SR.047, RUL.056: 1.0 (0x3F8000) -> 0x3C00; tie-to-even at 1 + 2^-11 (0x3F8010 -> 0x3C00)
    # and 1 + 3*2^-11 (0x3F8030 -> 0x3C02)
    assert conv(0x3F8000) == 0x3C00
    assert conv(0x3F8010) == 0x3C00
    assert conv(0x3F8030) == 0x3C02
    # ISA.SR.047: below 2^-14 (e=112, 0x380000 = 2^-15) flushes to signed zero
    assert conv(0x380000) == 0x0000
    assert conv(0xB80000) == 0x8000
    # ISA.SR.047, RUL.057: >= 2^16 (e=143, 0x478000 = 65536) and inf/NaN saturate to +-65504
    assert conv(0x478000) == 0x7BFF
    assert conv(0xC78000) == 0xFBFF
    assert conv(0x7F8000) == 0x7BFF
    assert conv(0x7FC000) == 0x7BFF


def test_oracle_e8f16_acc_and_mutant_witness():
    _check_e8f16_acc_spec(oracle.e8_to_f16_acc)
    # Truncation mutant (fails RNE on 0x3F8030) and IEEE-inf mutant (fails saturation on 0x7F8000)
    with pytest.raises(AssertionError):
        _check_e8f16_acc_spec(lambda w: oracle.e8_to_f16_acc(w) ^ 1)
    with pytest.raises(AssertionError):
        _check_e8f16_acc_spec(oracle.e8_to_f16_ieee)


def _check_mx7_spec(pack_fn):
    # NUM.MX7.002: all-zero 4x32 block has es=0, m8=8 -> scale field 0xA0 on all 4 rows, q=0
    zero_rows = [[0x0000] * 32 for _ in range(4)]
    assert pack_fn(zero_rows, False) == [0xA0A0A0A0] * 4
    # NUM.MX7.001-005: row 0 element k=0 is 1.0 (0x3C00: ep=15, sig=1024 -> m8=9, es=-6,
    # field = ((14 << 3) | 1) = 0x71; prod = 1024*3641 = 3728384, sh = 37 - 6 - 15 = 16,
    # q = (3728384 >> 16) + ((3728384 >> 15) & 1) = 56 + 1 = 57 = 0x39).
    rows = [[0x0000] * 32 for _ in range(4)]
    rows[0][
        1
    ] = 0x3C00  # row 0, kk=1: A layout slot = 0*8 + 1 = 1; B layout slot = 1*4 + 0 = 4
    wa = pack_fn(rows, False)[0]
    wb = pack_fn(rows, True)[0]
    assert (wa & 0xFFFFFFFF) == 0x71A0A0A0
    assert ((wa >> (255 - 1 * 7 - 6)) & 0x7F) == 57
    assert ((wb >> (255 - 4 * 7 - 6)) & 0x7F) == 57
    assert wa != wb


def test_oracle_mx7_and_mutant_witness():
    _check_mx7_spec(oracle.mx7_pack_entry)
    with pytest.raises(AssertionError):
        _check_mx7_spec(lambda r, b: oracle.mx7_pack_entry(r, not b))


def _check_red_max_spec(max_fn):
    # ISA.SR.042, ISA.SR.044, sru.md s5: sign-magnitude key orders -2.0 < -1.0 < -0.0 < +0.0 < +1.0
    wd = [0] * 16
    wd[0] = 0xC00000  # -2.0
    wd[1] = 0x800000  # -0.0
    wd[2] = 0x000000  # +0.0
    wd[3] = 0xBF8000  # -1.0
    assert max_fn([wd], 0) == 0x000000
    wd2 = list(wd)
    wd2[2] = 0xC08000  # -4.0 (all negative, -0.0 wins)
    assert max_fn([wd2], 0) == 0x800000


def test_oracle_red_max_and_mutant_witness():
    _check_red_max_spec(oracle.row_max_e8)
    # Swapped min-for-max mutant fails
    with pytest.raises(AssertionError):
        _check_red_max_spec(
            lambda words, r: min(
                (wd[4 * r + c] for wd in words for c in range(4)),
                key=lambda w: oracle.sign_mag_key(w, 24),
            )
        )


def _oracle_mul(a, b):
    return oracle.fin_alu(oracle.FIN_MUL, a, b, 0)


def _vector_rule_fma(a, b, c, add=True):
    """The vector lane's zero rule (NUM.E8.008, ISA.VC.058) on the SRU oracle:
    a finite zero product is +0 and an addend passes through it unchanged."""
    finite = not any(oracle.e8_is_nan(w) or oracle.e8_is_inf(w) for w in (a, b, c))
    if finite and (oracle.e8_is_zero(a) or oracle.e8_is_zero(b)):
        return c if add else 0x000000
    return oracle.e8_fma(a, b, c, add=add)


def _vector_rule_mul(a, b):
    return _vector_rule_fma(a, b, 0, add=False)


def _check_plane_spec(fma_fn, fn_eval_fn, mul_fn):
    # ISA.SR.043, NUM.E8.001, RUL.056: 1.0 * 1.0 + 3*2^-17 rounds to nearest even (0x3F8001)
    three_2m17 = oracle.e8_encode(Fraction(3, 1 << 17))
    assert fma_fn(0x3F8000, 0x3F8000, three_2m17) == 0x3F8001
    # Exact cancellation gives +0 (0x000000), while (-0)*1 + (-0) gives -0 (0x800000)
    assert fma_fn(0x3F8000, 0x3F8000, 0xBF8000) == 0x000000
    assert fma_fn(0x800000, 0x3F8000, 0x800000) == 0x800000
    # ISA.SR.043: two zero terms are -0 only when both are negative
    assert fma_fn(0x000000, 0x3F8000, 0x800000) == 0x000000
    assert fma_fn(0x800000, 0x3F8000, 0x000000) == 0x000000
    # ISA.SR.043, ISA.SR.044: a zero product carries sa ^ sb, MUL included
    assert mul_fn(0x800000, 0x3F8000) == 0x800000
    # ISA.SR.043: function op on exp2(0) = 1.0 (0x3F8000) and mantissa ROM 1/2.0 = 0.5 (0x3F0000)
    rom = oracle.fn_exp2_table(128)
    assert fn_eval_fn(0x000000, 0x000000, oracle.FN_EXP, 0, rom, rom=True) == 0x3F8000
    assert (
        fn_eval_fn(0x400000, 0, oracle.FN_MANT, 0, None, neg=1, mrom=oracle.MROM_TABLE)
        == 0x3F0000
    )


def test_oracle_plane_and_mutant_witness():
    _check_plane_spec(oracle.e8_fma, oracle.fn_eval, _oracle_mul)
    with pytest.raises(AssertionError):
        _check_plane_spec(
            lambda a, b, c: oracle.e8_fma(a, b, c) ^ 1, oracle.fn_eval, _oracle_mul
        )
    with pytest.raises(AssertionError):
        _check_plane_spec(
            oracle.e8_fma,
            lambda *a, **kw: oracle.fn_eval(*a, **kw) ^ 1,
            _oracle_mul,
        )


def test_vector_zero_rule_fails_the_sru_plane_spec():
    """The vector lane's zero rule differs from the SRU's on (+0) 1 + (-0)
    and on a negative zero product; the plane spec refuses it on each."""
    assert _vector_rule_fma(0x000000, 0x3F8000, 0x800000) == 0x800000
    assert oracle.e8_fma(0x000000, 0x3F8000, 0x800000) == 0x000000
    assert _vector_rule_mul(0x800000, 0x3F8000) == 0x000000
    assert _oracle_mul(0x800000, 0x3F8000) == 0x800000
    with pytest.raises(AssertionError):
        _check_plane_spec(_vector_rule_fma, oracle.fn_eval, _oracle_mul)
    with pytest.raises(AssertionError):
        _check_plane_spec(oracle.e8_fma, oracle.fn_eval, _vector_rule_mul)


def test_disagreement_between_red_py_and_spec_row_sum_is_pinned():
    """RUL.098's hand-computed ROM row distinguishes ideal from hardware sum."""
    # The exact 8 words (4 elements per word) of row 0 in `rom` job `flash` (seed=1):
    words_r0 = [
        [0x379838, 0x3B2AF5, 0x3CD4F4, 0x3BCE48] + [0] * 12,
        [0x3E9766, 0x3F8956, 0x38CE24, 0x3DD6B0] + [0] * 12,
        [0x3ACB71, 0x3A24FC, 0x3AEF8F, 0x3964CB] + [0] * 12,
        [0x3FCAD2, 0x39AD58, 0x3BBA3D, 0x39EBB5] + [0] * 12,
        [0x3ABCC7, 0x3A14A8, 0x3B3B82, 0x3BA13B] + [0] * 12,
        [0x3CA51A, 0x3A2A35, 0x38C346, 0x39747C] + [0] * 12,
        [0x3B8869, 0x3BB258, 0x3A5B3E, 0x3A054E] + [0] * 12,
        [0x3AF190, 0x3AFA26, 0x3C8D7D, 0x3A5FBD] + [0] * 12,
    ]
    exact = sum(oracle.e8_value(x) for wd in words_r0 for x in wd[:4])
    assert exact == Fraction(850256029, 268435456)
    assert oracle.e8_encode(exact) == 0x404AB8  # ideal-sum mutant
    assert oracle.e8_encode(Fraction(6642623, 2097152)) == 0x404AB7
    st2_spec = oracle.row_sum_e8(words_r0, 0)
    assert st2_spec == 0x404AB7
    assert oracle.fin_alu(oracle.FIN_ADD, 0x3F68BD, 0, st2_spec) == 0x408273


def test_rul098_small_tie_and_signed_growth():
    # RUL.098 short example: truncation removes bits that break an even tie.
    words = [[0x3F8000] + [0] * 15, [0x378081] + [0] * 15]
    assert oracle.row_sum_e8(words, 0) == 0x3F8000
    exact = oracle.e8_value(0x3F8000) + oracle.e8_value(0x378081)
    assert oracle.e8_encode(exact) == 0x3F8001
    # A negative tiny total floors to -1 grid unit when E grows, even by >47.
    # The +1 and -1 cancel in word 2; the remaining -2^-23 must not vanish.
    words = [[0x808000] + [0] * 15, [0x3F8000, 0xBF8000] + [0] * 14]
    assert oracle.row_sum_e8(words, 0) == oracle.e8_encode(-Fraction(1, 1 << 23))
    # Element sign follows magnitude truncation: a tiny negative added in word 2
    # instead contributes zero, not -1 grid unit.
    words = [[0x3F8000, 0xBF8000] + [0] * 14, [0x808000] + [0] * 15]
    assert oracle.row_sum_e8(words, 0) == 0
