"""All-zero MXFP7 blocks in nk=2 contractions: census and model measurement.

    python run.py census --out FILE            offline, no model
    python run.py model --out FILE [--build B] one model session
    python run.py cost --out FILE [--build B]  one model session
    python run.py fused --out FILE [--build B] one model session

Run from `software/` with it on PYTHONPATH (the operands and the harness are
`application/tests/runtime/zero_block.py`).

Census: per output element and K pair, the pairs where a zero partial's
exponent sum exceeds a live partner's (`mx_acu_fp_pump.v` shifts the partner
by the difference), under the unit-scale zero field (E=20) and E=0. Model: on
a mover-preferring machine, each contraction under both pairings (RUL.099):
`vector` (nk=2, operands quantised on the vector cores) and `unpaired` (nk=1,
mover QUANT); then at nk=2 host-packed operands and the unit-scale encodings
the mutants restore. Every variant reports quant and product cycles, the
packed bytes' hashes against the Python and host packers, and its error
against the exact product of the quantised operands. Cost: on the vector-only
policy, the lift's cycles in each quant and in fused vector packs.
"""

import argparse
import hashlib
import json
import time
from pathlib import Path

import numpy as np
from application.tests.runtime import zero_block as Z
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.language.numerics import mxfp7
from kohakutpu.stack import rs

BOARD = "vlt_v9t6s_1n"
BUILD = "/home/kblueleaf/ktpu/build/v9t6s/vlt_card_v9t6s_1n"
#: The field of an all-zero block lifted to `mxfp7.ZERO_LIFT`: E=0.
_ES, _M8 = mxfp7._scale_field(np.array(mxfp7.ZERO_LIFT))
MIN_FIELD = int((_ES + mxfp7.SBIAS) << 3 | (_M8 - 8))
#: Accumulator significand bits (ACC_MW 14 plus the hidden one).
ACC_BITS = 15
#: The nk=2 operand encodings measured beside the two pairings: label ->
#: (packer, mutant); packer "host" or the generated quant kernel.
ENCODINGS = {
    "host_lifted": ("host", None),
    "host_unit": ("host", "num.mx7/unit-scale-zero"),
    "vector_unit": ("quant", "l2.packing/unit-scale-zero"),
    "mover_nk2": ("quant", "l2.packing/mover-paired"),
}


def fields(x, min_zero):
    """Per (row, block) scale fields of `x` and its zero-block mask."""
    _, es, m8 = mxfp7.quantise_fp16(x)
    field = ((es + mxfp7.SBIAS) << 3) | (m8 - 8)
    zero = (x.reshape(x.shape[0], -1, 32).view(np.uint16) & 0x7FFF).max(-1) == 0
    if min_zero:
        field = np.where(zero, MIN_FIELD, field)
    return field, zero


def census(a, b, nk, rows, cols):
    """Hazard pairs of A [M, Kp] x B [N, Kp] (outputs [:rows, :cols]) under
    both zero encodings."""
    out = {
        "nk": nk,
        "zero_blocks_a": 0,
        "zero_blocks_b": 0,
        "blocks_a": int(a.size // 32),
        "blocks_b": int(b.size // 32),
    }
    fa, za = fields(a, False)
    fb, zb = fields(b, False)
    out["zero_blocks_a"], out["zero_blocks_b"] = int(za.sum()), int(zb.sum())
    if nk != 2:
        return out
    qa = mxfp7.quantise_fp16(a)[0].reshape(a.shape[0], -1, 32)
    qb = mxfp7.quantise_fp16(b)[0].reshape(b.shape[0], -1, 32)
    qa, qb, fa, fb, za, zb = (
        qa[:rows],
        qb[:cols],
        fa[:rows],
        fb[:cols],
        za[:rows],
        zb[:cols],
    )
    part = np.einsum("rbk,cbk->rcb", qa, qb)
    mm = (8 + (fa & 7))[:, None, :] * (8 + (fb & 7))[None, :, :]
    mag = np.abs(part) * mm
    zero = za[:, None, :] | zb[None, :, :]
    for label, min_zero in (("unit", False), ("min", True)):
        ea = np.where(za, MIN_FIELD >> 3, fa >> 3) if min_zero else fa >> 3
        eb = np.where(zb, MIN_FIELD >> 3, fb >> 3) if min_zero else fb >> 3
        e = ea[:, None, :] + eb[None, :, :]
        e0, e1 = e[..., 0::2], e[..., 1::2]
        z0, z1 = zero[..., 0::2], zero[..., 1::2]
        m0, m1 = mag[..., 0::2], mag[..., 1::2]
        # a zero partial (by its zero block) beside a live one, larger sum
        h01 = z0 & ~z1 & (m1 != 0) & (e0 > e1)
        h10 = z1 & ~z0 & (m0 != 0) & (e1 > e0)
        shift = np.where(h01, e0 - e1, 0) + np.where(h10, e1 - e0, 0)
        live = np.where(h01, m1, 0) + np.where(h10, m0, 0)
        bits = np.zeros_like(live)
        nz = live > 0
        bits[nz] = np.floor(np.log2(live[nz])).astype(np.int64) + 1
        lost = np.maximum(0, shift - np.maximum(0, bits - ACC_BITS))
        hazard = h01 | h10
        out[label] = {
            "hazard_pairs": int(hazard.sum()),
            "outputs_hit": int(hazard.any(-1).sum()),
            "outputs": int(hazard.shape[0] * hazard.shape[1]),
            "pairs_losing_bits": int((hazard & (lost > 0)).sum()),
            "max_shift": int(shift.max()) if hazard.any() else 0,
            "shift_hist": {
                str(s): int(n)
                for s, n in zip(*np.unique(shift[hazard], return_counts=True))
            },
            "max_lost_bits": int(lost.max()) if hazard.any() else 0,
        }
    return out


def cases():
    out = {"minimal": Z.minimal(), "causal_av": Z.causal_av()}
    out.update(Z.lenet_relu_step(Z.mnist8()))
    vit = Z.vit_tiny()
    out.update({k: vit[k] for k in ("vit_qkv", "vit_fc2")})
    return out


def do_census(args):
    result = {}
    sets = cases()
    sets.update(Z.vit_tiny())
    for name, (a, b) in sets.items():
        mt = rs.machine_json(machine_of(BOARD, packing_backend="vector_mx7"))
        mm, ap, bp = Z.operands(a, b, mt, "vector")
        result[name] = dict(
            shape=[a.shape[0], b.shape[0], mm.k],
            plan=vars(mm),
            **census(ap, bp, mm.nk, a.shape[0], b.shape[0]),
        )
        print(name, json.dumps(result[name]), flush=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")


def sha(b):
    return hashlib.sha256(b).hexdigest()[:16]


def do_model(args):
    machine = machine_of(BOARD, packing_backend="mover_quant")
    log = args.out.with_suffix(".log")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    dev = D.Device(kind="sim", board=BOARD, nodes=1, build=Path(args.build), log=log)
    dev.machine = machine
    mt = rs.machine_json(machine)
    started = time.monotonic()
    result = {"board": BOARD, "backend": "mover_quant"}
    try:
        for name, (a0, b0) in cases().items():
            rows, cols = a0.shape[0], b0.shape[0]
            rec, outs = {}, {}
            for pairing in ("vector", "unpaired"):
                mm, ap, bp = Z.operands(a0, b0, mt, pairing)
                ga, gb = mm.tm // 4, mm.tn // 4
                source = Z.mm_source(mm, pairing, mt)
                qa, qca, la = Z.quant(dev, ap, "a", ga, mm.nk)
                qb, qcb, lb = Z.quant(dev, bp, "b", gb, mm.nk)
                got, cycles = Z.product(dev, mm, source, qa, qb)
                outs[pairing] = got
                lifted = (
                    Z.reference(mxfp7.lift(ap, mm.nk), "a", ga, mm.nk),
                    Z.reference(mxfp7.lift(bp, mm.nk), "b", gb, mm.nk),
                )
                plain = (
                    Z.reference(ap, "a", ga, mm.nk),
                    Z.reference(bp, "b", gb, mm.nk),
                )
                hosted = (
                    Z.host(ap, "a", ga, mm.nk, mt),
                    Z.host(bp, "b", gb, mm.nk, mt),
                )
                rec[pairing] = dict(
                    plan=vars(mm),
                    rust_nk=[Z.port_nk(source, "a"), Z.port_nk(source, "b")],
                    quant_backend=[
                        "vector" if "vpack" in t else "mover" for t in (la, lb)
                    ],
                    quant_cycles=[qca, qcb],
                    mm_cycles=cycles,
                    total_cycles=qca + qcb + cycles,
                    sha_device=[sha(qa), sha(qb)],
                    sha_host=[sha(hosted[0]), sha(hosted[1])],
                    sha_python_lifted=[sha(lifted[0]), sha(lifted[1])],
                    sha_python_plain=[sha(plain[0]), sha(plain[1])],
                    **Z.error(got, ap, bp, rows, cols),
                )
                if pairing == "vector":
                    rec["census"] = census(ap, bp, mm.nk, rows, cols)
                    paired = mm, ap, bp, ga, gb, source
            mm, ap, bp, ga, gb, source = paired
            if mm.nk == 2:
                for label, (packer, mutant) in ENCODINGS.items():
                    if packer == "host":
                        xa = Z.host(ap, "a", ga, 2, mt, mutant)
                        xb = Z.host(bp, "b", gb, 2, mt, mutant)
                    else:
                        xa = Z.quant(dev, ap, "a", ga, 2, mutant)[0]
                        xb = Z.quant(dev, bp, "b", gb, 2, mutant)[0]
                    got, cycles = Z.product(dev, mm, source, xa, xb)
                    outs[label] = got
                    rec[label] = dict(
                        mm_cycles=cycles,
                        sha=[sha(xa), sha(xb)],
                        **Z.error(got, ap, bp, rows, cols),
                    )
            for x in outs:
                if x == "vector":
                    continue
                d = np.abs(
                    outs[x][:rows, :cols].astype(np.float64)
                    - outs["vector"][:rows, :cols]
                )
                rec[f"{x}_vs_vector"] = {
                    "max_abs": float(d.max()),
                    "differing": int((d > 0).sum()),
                    "outputs": int(d.size),
                    "bit_exact": bool((d == 0).all()),
                }
            result[name] = rec
            print(name, json.dumps(rec), flush=True)
    finally:
        result["wall_seconds"] = time.monotonic() - started
        args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")
        dev.close()


#: Fused vector packs measured for the lift's cost: (rows, cols, side, groups).
FUSED = [(64, 256, "a", 8), (128, 512, "b", 16)]


def vector_device(args):
    """A model session on the vector-only policy, and its machine JSON."""
    machine = machine_of(BOARD, packing_backend="vector_mx7")
    log = args.out.with_suffix(".log")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    dev = D.Device(kind="sim", board=BOARD, nodes=1, build=Path(args.build), log=log)
    dev.machine = machine
    return dev, rs.machine_json(machine)


def do_cost(args):
    """On the vector-only policy: each case's operands quantised into nk=2
    entries with the lift and under its unit-scale mutant, and into nk=1
    (never lifted). Cycles and hashes."""
    dev, mt = vector_device(args)
    started = time.monotonic()
    result = {"board": BOARD, "backend": "vector_mx7", "quant": {}}
    try:
        for name, (a0, b0) in cases().items():
            mm, ap, bp = Z.operands(a0, b0, mt, "vector")
            rec = {}
            for side, x, g in (("a", ap, mm.tm // 4), ("b", bp, mm.tn // 4)):
                lifted, cl, _ = Z.quant(dev, x, side, g, 2)
                plain, cu, _ = Z.quant(dev, x, side, g, 2, "l2.packing/unit-scale-zero")
                single, c1, _ = Z.quant(dev, x, side, g, 1)
                rec[side] = {
                    "shape": list(x.shape),
                    "lifted_cycles": cl,
                    "unit_cycles": cu,
                    "nk1_cycles": c1,
                    "lift_cost": cl - cu,
                    "lift_cost_rel": (cl - cu) / cu,
                    "bytes_changed": sum(p != q for p, q in zip(lifted, plain)),
                    "sha_lifted": sha(lifted),
                    "sha_python_lifted": sha(Z.reference(mxfp7.lift(x, 2), side, g, 2)),
                    "sha_nk1": sha(single),
                    "sha_python_nk1": sha(Z.reference(x, side, g, 1)),
                }
            result["quant"][name] = rec
            print(name, json.dumps(rec), flush=True)
    finally:
        result["wall_seconds"] = time.monotonic() - started
        args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")
        dev.close()


def do_fused(args):
    """On the vector-only policy: fused vector packs into nk=2 entries with the
    lift and under its unit-scale mutant, and into nk=1. Cycles and hashes."""
    dev, _ = vector_device(args)
    started = time.monotonic()
    result = {"board": BOARD, "backend": "vector_mx7", "fused": {}}
    try:
        rng = np.random.default_rng(9)
        for rows, cols, side, g in FUSED:
            x = rng.standard_normal((rows, cols)).astype(np.float16)
            x.reshape(rows, -1, 32)[::3, ::2] = 0
            key = f"{rows}x{cols}{side}"
            lifted, cl = Z.fused(dev, x, side, g, 2)
            plain, cu = Z.fused(dev, x, side, g, 2, "l2.vector/unit-scale-zero")
            single, c1 = Z.fused(dev, x, side, g, 1)
            result["fused"][key] = {
                "lifted_cycles": cl,
                "unit_cycles": cu,
                "nk1_cycles": c1,
                "lift_cost": cl - cu,
                "lift_cost_rel": (cl - cu) / cu,
                "sha_lifted": sha(lifted),
                "sha_python_lifted": sha(Z.reference(mxfp7.lift(x, 2), side, g, 2)),
                "sha_unit": sha(plain),
                "sha_python_unit": sha(Z.reference(x, side, g, 2)),
                "sha_nk1": sha(single),
                "sha_python_nk1": sha(Z.reference(x, side, g, 1)),
            }
            print(key, json.dumps(result["fused"][key]), flush=True)
    finally:
        result["wall_seconds"] = time.monotonic() - started
        args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")
        dev.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    modes = {"census": do_census, "model": do_model, "cost": do_cost, "fused": do_fused}
    ap.add_argument("mode", choices=tuple(modes))
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--build", default=BUILD)
    args = ap.parse_args()
    modes[args.mode](args)


if __name__ == "__main__":
    main()
