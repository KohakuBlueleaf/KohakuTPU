"""The Sv39 walker and the back-to-back access defect translated code lives with."""

import re

import conftest
import memgap

#: The walker as measured on silicon: no A or D update, a PTE seen after
#: `sfence.vma`, cached tables read stale (docs/os/hardware/constraints.md).
WALKER = {
    "load_A0_faults": 1,
    "load_A0_cause": 13,
    "pte_after_load": 0x07,
    "store_D0_faults": 1,
    "store_D0_cause": 15,
    "pte_after_store": 0x47,
    "no_sfence_reads_old_frame": 1,
    "misaligned_gigapage_faults": 1,
    "dram_pte_cached_unflushed_faults": 1,
    "dram_pte_after_flush_faults": 0,
}


def differing(elf):
    out = conftest.core_model("--elf-N", 0, elf, "--max-cycles", 2_000_000)
    found = re.search(r"@ xlat\.cases_differing (\d+)", out)
    assert found, out[-2000:]
    return int(found[1]), out


def test_the_defect_reproduces(gapped):
    n, out = differing(gapped(False))
    assert n == 4, out
    for case in ("dram,spad", "dram,dram,dram", "dram,st spad", "st dram,dram"):
        assert f"DIFF {case} " in out


def test_the_memory_gap_pass_removes_it(gapped):
    assert differing(gapped(True))[0] == 0


def test_the_memory_gap_pass_places_its_nops():
    text = "\tld a0, 0(a1)\n\tsd a0, 8(a1)\n.L1:\n\tld a2, 0(a3)\n\taddi a2, a2, 1\n\tsw a2, 0(a3)\n"
    out, added = memgap.apply(text)
    assert added == 2
    head = [
        "\tld a0, 0(a1)",
        "\tnop",
        "\tsd a0, 8(a1)",
        ".L1:",
        "\tnop",
        "\tld a2, 0(a3)",
    ]
    assert out.split("\n")[:6] == head


def test_the_walker(probe):
    r = probe("core2", "walk", 2_000_000)
    assert r["rc"] == 0 and not r["fails"], r["output"][-2000:]
    got = conftest.measures(r["output"])
    assert {k[len("walk.") :]: v for k, v in got.items()} == WALKER
