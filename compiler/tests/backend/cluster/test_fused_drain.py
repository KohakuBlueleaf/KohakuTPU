"""Compiler output must respect the LOAD/ADD_EMIT issue boundary."""

import pytest
from kohakutpu.isa import ISA
from tests.test_band_witness import BACKEND, digest, flits_of, operands

from tests import fixtures as F


def assert_memory_drain_pair(words, fused):
    gemms = [ISA.GEMM.decode(w) for w in words if w >> 252 == 2]
    drains = [ISA.DRAIN.decode(w) for w in words if w >> 252 == 3]
    assert gemms and drains
    assert bool(gemms[-1]["emit"]) == fused
    assert bool(drains[-1]["fuse"]) == fused
    assert all(
        not g["emit"] or g["acc"] or max(1, g["nk"]) > BACKEND.k_blocks_per_issue
        for g in gemms
    )
    if fused:
        assert words[-2] >> 252 == 2 and words[-1] >> 252 == 3
        gemm_addr = gemms[-1]["addr"] | gemms[-1]["addr_hi"] << ISA.cfg.addr_bits
        drain_addr = drains[-1]["addr"] | drains[-1]["addr_hi"] << ISA.cfg.addr_bits
        assert gemm_addr == drain_addr


@pytest.mark.parametrize(
    "k,nk,fused",
    [(32, 1, False), (64, 2, False), (96, 3, True), (128, 4, True), (128, 2, True)],
)
def test_memory_drain_emits_only_after_an_accumulator_load(k, nk, fused):
    words, _ = flits_of(F.mm, operands((32, k), (32, k)), nk=nk)
    assert_memory_drain_pair(words, fused)


@pytest.mark.parametrize("k,nk,fused", [(32, 1, False), (64, 2, True)])
def test_unpumped_backend_respects_the_single_k_block_issue(k, nk, fused, monkeypatch):
    monkeypatch.setattr(BACKEND, "k_blocks_per_issue", 1)
    words, _ = flits_of(F.mm, operands((32, k), (32, k)), nk=nk)
    assert_memory_drain_pair(words, fused)


@pytest.mark.parametrize(
    "nk,witness", [(4, "4:b89760472d241ce7"), (2, "7:f5fdb45171a48d38")]
)
def test_legal_fused_program_bytes_are_unchanged(nk, witness):
    assert digest(F.mm, operands((32, 128), (32, 128)), nk=nk) == witness
