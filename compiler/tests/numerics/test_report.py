"""The numerics report: every source (L3 interpreter, the hand-written
schedule and the compiled L3 program on the unit models) within its stated
bound of the host MXFP7 reference, and no further from the fp32 reference than
the quantisation itself is."""

import pytest
from kohakutpu.ir import numerics

#: rel_l2 against the MXFP7 reference: an fp16 store's rounding (2^-11), except
#: attention, which stores S and P in fp16 before P is quantised.
BOUND = {"attention": 6e-3}
FP16 = 6e-4


@pytest.fixture(scope="module")
def rows():
    return numerics.report(seed=0)


def test_every_case_has_every_source_against_both_references(rows):
    got = {(r["case"], r["shape"], r["source"], r["ref"]) for r in rows}
    for c in numerics.cases(seed=0):
        assert (c.name, c.shape, "mx-ref", "fp32") in got
        for s in ("interp", "model", "l3"):
            assert {(c.name, c.shape, s, "fp32"), (c.name, c.shape, s, "mx")} <= got


def test_each_source_is_within_its_bound_of_the_mxfp7_reference(rows):
    for r in rows:
        if r["ref"] != "mx":
            continue
        bound = BOUND.get((r["case"], r["source"]), BOUND.get(r["case"], FP16))
        assert r["rel_l2"] < bound, r


def test_no_source_is_further_from_fp32_than_the_quantisation(rows):
    floor = {
        (r["case"], r["shape"]): r["rel_l2"] for r in rows if r["source"] == "mx-ref"
    }
    for r in rows:
        if r["ref"] == "fp32" and r["source"] != "mx-ref":
            assert r["rel_l2"] < floor[(r["case"], r["shape"])] * 1.1 + 1e-3, r
