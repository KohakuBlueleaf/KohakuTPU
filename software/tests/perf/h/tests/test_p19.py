"""P19's witness operands keep every shard's y exact in f16, and each fn's triple
groups and owners match the sysnodes its blocks run on."""

import numpy as np

from h.gen import p19

SYSNODES = (0, 1, 2, 3)


def test_every_shard_sum_is_an_integer_f16_holds():
    for shard in p19.SHARDS:
        ins = p19.operands(SYSNODES, shard, "disjoint", "")
        for s in SYSNODES:
            y = ins[f"x{s}"].astype(np.float64) @ ins[f"w{s}"].astype(np.float64).T
            assert np.abs(y).max() < 2**11, (shard, s)


def test_triples_overlap_their_shares_and_own_their_outputs():
    mod = p19.generate(SYSNODES, "multimesh_v9t6")
    for name, spec in mod.specs.items():
        if not spec.triple:
            assert spec.ledger is not None, name
            continue
        p = p19.SHARDS[name.split("_disjoint")[0].split("_shared")[0][4:]][2]
        assert len(spec.overlaps) == (3 if p == 4 else 2), name
        assert all(spec.owners[o] == int(o[1:]) for o in spec.outputs), name
