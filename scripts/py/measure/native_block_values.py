"""Bounded eager encoder block, immutable host packages and model comparison."""

import argparse
import json
import re
import shutil
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import numpy as np
from examples.native.block import block, vit_forward
from kohakuaccel.driver.device import (
    CU_COUNTERS,
    CU_DBG,
    control_read,
    decode_counters,
)
from kohakuaccel.driver.transport.rebase import Rebased
from kohakutpu.application import api as kp
from kohakutpu.application.api import native
from kohakutpu.application.kernels import matmul as MM
from kohakutpu.application.kernels import transpose as T
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime import generated as G
from kohakutpu.driver.units import matmul_dbg
from kohakutpu.stack import rs
from native_values import decode_bytes, encode_bytes


def parse_v2_log(log_path: Path) -> dict:
    if not log_path.exists():
        return {}
    counters = {}
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = re.match(
            r"(VRES|V2MATH|V2UNPACK|V2PACK|V2DMA|V2DISPATCH) (\S+) (.*)", line
        )
        if match:
            fields = match[3].split()
            counters.setdefault(match[2], {})[match[1]] = {
                fields[i]: int(fields[i + 1]) for i in range(0, len(fields) - 1, 2)
            }
    return counters


def unit_snapshot(dev, ctrl):
    reply = dev.machine.agent
    snap = {}
    for kind in ("MG", "VC"):
        for idx, coord in enumerate(dev.machine.coords(kind)):
            name = f"{kind.lower()}[{idx}]"
            cnt = decode_counters(control_read(ctrl, coord, CU_COUNTERS, reply=reply))
            entry = {
                "busy_cycles": cnt["busy_cycles"],
                "retired": cnt["retired"],
            }
            if kind == "MG":
                dbg = matmul_dbg(control_read(ctrl, coord, CU_DBG, reply=reply))
                entry["compute_cycles"] = dbg["compute_cycles"]
                entry["memory_cycles"] = dbg["memory_cycles"]
            snap[name] = entry
    return snap


def unit_delta(before, after):
    return {
        u: {k: (after[u][k] - before[u][k]) & 0xFFFFFFFF for k in before[u]}
        for u in before
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prebuild", "sim"))
    parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tokens", type=int, default=16)
    parser.add_argument("--batch", type=int, default=1)
    parser.add_argument("--width", type=int, default=192)
    parser.add_argument("--ring-depth", type=int, default=2)
    parser.add_argument("--query-blocked", action="store_true")
    parser.add_argument("--mlp-space", choices=("stage", "dram"), default="stage")
    parser.add_argument("--attn-space", choices=("stage", "dram"), default="dram")
    parser.add_argument("--warm-weights-only", action="store_true")
    parser.add_argument("--full-vit", action="store_true")
    parser.add_argument("--blocks", type=int, default=1)
    parser.add_argument("--board", default="vlt_v9t6s_1n")
    parser.add_argument(
        "--quant-backend", choices=("mover_quant", "vector_mx7"), default="mover_quant"
    )
    parser.add_argument("--no-mover-overlap", action="store_true")
    parser.add_argument("--no-fast-attn", action="store_true")
    parser.add_argument("--no-compact-mlp", action="store_true")
    parser.add_argument("--no-balance-norm", action="store_true")
    parser.add_argument("--attn-only", action="store_true")
    parser.add_argument("--ablation", action="store_true")
    parser.add_argument("--ablation-set", type=int, choices=(0, 1, 2), default=0)
    parser.add_argument("--select-variants", type=str, default="")
    args = parser.parse_args()
    if args.mode == "sim":
        shutil.unpack_archive(str(args.archive), str(args.library), "zip")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    log_path = args.output.with_suffix(".log")
    dev = D.Device(
        "offline" if args.mode == "prebuild" else "sim",
        board=args.board,
        nodes=1,
        queued=False,
        library=args.library,
        library_only=True,
        log=log_path if args.mode == "sim" else None,
    )
    ctrl = (
        Rebased(dev.card.host, int(dev.card.board["ctrl_base"], 16))
        if args.mode == "sim"
        else None
    )
    run_units = []
    last_snap = unit_snapshot(dev, ctrl) if ctrl is not None else None
    orig_collect_one = dev._collect_one

    def collect_one_with_units(s):
        nonlocal last_snap
        already = s.run is not None
        r = orig_collect_one(s)
        if not already and ctrl is not None:
            cur = unit_snapshot(dev, ctrl)
            run_units.append(unit_delta(last_snap, cur))
            last_snap = cur
        return r

    captured, sources = {}, {}
    if (args.library / "graphs.json").exists():
        sources = json.loads((args.library / "graphs.json").read_text())
    if args.mode == "sim":
        captured = json.loads(
            (args.library / "recorded.json").read_text(), object_hook=decode_bytes
        )
    original, original_graph = dev.compile_graph, G.graph

    def lookup(source):
        machine = rs.machine_json(dev.machine)
        key = json.loads(rs._rs.library_key(source, machine))["class"]
        if args.mode == "prebuild":
            rs._rs.library_prebuild(str(args.library), source, machine)
            captured[key] = original(source)
        output = captured[key]
        output["packages"] = {int(k): v for k, v in output["packages"].items()}
        return output

    def generate(generator, arguments, machine, mutant):
        if generator == "matmul.mm_kernel":
            parsed = json.loads(arguments)
            m_args = parsed["mm"]["made_by"]["args"]
            m_args.setdefault("pad_m", False)
            m_args.setdefault("dense_n", False)
            arguments = json.dumps(parsed, sort_keys=True)
        key = json.dumps([generator, arguments, machine, mutant])
        if args.mode == "prebuild" and key not in sources:
            sources[key] = original_graph(generator, arguments, machine, mutant)
        return sources[key]

    def forbidden(*a, **kw):
        raise AssertionError("runtime compiler/generator reached")

    rng = np.random.default_rng(29)
    w = args.width
    shapes = [
        (w,),
        (w,),
        (3 * w, w),
        (3 * w,),
        (w, w),
        (w,),
        (w,),
        (w,),
        (4 * w, w),
        (4 * w,),
        (w, 4 * w),
        (w,),
    ]
    arrays = [rng.normal(0, 0.02, s).astype(np.float16) for s in shapes]
    arrays[0] += 1
    arrays[6] += 1
    input_array = rng.normal(0, 0.2, (args.batch, args.tokens, w)).astype(np.float16)
    patch_in_arr = np.zeros((args.batch, args.tokens, 768), np.float16)
    patch_in_arr[:, 1:, :] = rng.normal(
        0, 0.2, (args.batch, args.tokens - 1, 768)
    ).astype(np.float16)
    pos_cls_arr = rng.normal(0, 0.02, (args.batch, args.tokens, w)).astype(np.float16)
    patch_w_arr = rng.normal(0, 0.02, (w, 768)).astype(np.float16)
    patch_b_arr = rng.normal(0, 0.02, (w,)).astype(np.float16)
    head_scale_arr = (1 + rng.normal(0, 0.02, (w,))).astype(np.float16)
    head_shift_arr = rng.normal(0, 0.02, (w,)).astype(np.float16)
    head_w_arr = np.zeros((1024, w), np.float16)
    head_w_arr[:1000, :] = rng.normal(0, 0.02, (1000, w)).astype(np.float16)
    head_b_arr = np.zeros((1024,), np.float16)
    head_b_arr[:1000] = rng.normal(0, 0.02, (1000,)).astype(np.float16)
    report = {
        "shape": [args.batch, args.tokens, w],
        "mode": args.mode,
        "package_replay": args.mode == "sim",
        "full_vit": args.full_vit,
        "blocks": args.blocks,
        "variants": [],
        "ring_depth": args.ring_depth,
        "query_blocked": args.query_blocked,
        "mlp_space": args.mlp_space,
        "attn_space": args.attn_space,
    }
    values = []
    with (
        D.attached(dev),
        patch.object(dev, "_collect_one", collect_one_with_units),
        patch.object(dev, "compile_graph", lookup),
        patch.object(G, "graph", generate),
        patch.object(
            rs._rs, "compile", rs._rs.compile if args.mode == "prebuild" else forbidden
        ),
    ):
        weights = [kp.tensor(a) for a in arrays]
        patch_weights = (kp.tensor(patch_w_arr), kp.tensor(patch_b_arr))
        head_weights = (
            kp.tensor(head_scale_arr),
            kp.tensor(head_shift_arr),
            kp.tensor(head_w_arr),
            kp.tensor(head_b_arr),
        )
        block_list = [weights for _ in range(args.blocks)]
        mask = kp.tensor(T.masks())
        ablation_1 = [
            (
                "unfused",
                False,
                {
                    "fast_attn": False,
                    "compact_mlp": False,
                    "balance_norm": False,
                    "mover_overlap": False,
                    "quant_backend": "mover_quant",
                },
            ),
            (
                "loop9_baseline",
                True,
                {
                    "fast_attn": False,
                    "compact_mlp": False,
                    "balance_norm": False,
                    "mover_overlap": False,
                    "quant_backend": "mover_quant",
                },
            ),
            (
                "mover_overlap_only",
                True,
                {
                    "fast_attn": False,
                    "compact_mlp": False,
                    "balance_norm": False,
                    "mover_overlap": True,
                    "quant_backend": "mover_quant",
                },
            ),
            (
                "vector_mx7_quant",
                True,
                {
                    "fast_attn": False,
                    "compact_mlp": False,
                    "balance_norm": False,
                    "mover_overlap": False,
                    "quant_backend": "vector_mx7",
                },
            ),
        ]
        ablation_2 = [
            (
                "unfused",
                False,
                {
                    "fast_attn": False,
                    "compact_mlp": False,
                    "balance_norm": False,
                    "mover_overlap": False,
                    "quant_backend": "mover_quant",
                },
            ),
            (
                "fast_attn_only",
                True,
                {
                    "fast_attn": True,
                    "compact_mlp": False,
                    "balance_norm": False,
                    "mover_overlap": False,
                    "quant_backend": "mover_quant",
                },
            ),
            (
                "compact_mlp_only",
                True,
                {
                    "fast_attn": False,
                    "compact_mlp": True,
                    "balance_norm": False,
                    "mover_overlap": False,
                    "quant_backend": "mover_quant",
                },
            ),
            (
                "balance_norm_only",
                True,
                {
                    "fast_attn": False,
                    "compact_mlp": False,
                    "balance_norm": True,
                    "mover_overlap": False,
                    "quant_backend": "mover_quant",
                },
            ),
            (
                "all_loop10",
                True,
                {
                    "fast_attn": True,
                    "compact_mlp": True,
                    "balance_norm": True,
                    "mover_overlap": True,
                    "quant_backend": "mover_quant",
                },
            ),
        ]
        variant_specs = (
            (
                ablation_1
                if args.ablation_set == 1
                else (
                    ablation_2
                    if args.ablation_set == 2
                    else ablation_1 + ablation_2[1:]
                )
            )
            if args.ablation
            else [
                (
                    "unfused",
                    False,
                    {
                        "fast_attn": not args.no_fast_attn,
                        "compact_mlp": not args.no_compact_mlp,
                        "balance_norm": not args.no_balance_norm,
                        "mover_overlap": not args.no_mover_overlap,
                        "quant_backend": args.quant_backend,
                    },
                ),
                (
                    "fused",
                    True,
                    {
                        "fast_attn": not args.no_fast_attn,
                        "compact_mlp": not args.no_compact_mlp,
                        "balance_norm": not args.no_balance_norm,
                        "mover_overlap": not args.no_mover_overlap,
                        "quant_backend": args.quant_backend,
                    },
                ),
            ]
        )
        if args.select_variants:
            keep = set(args.select_variants.split(","))
            variant_specs = [v for v in variant_specs if v[0] in keep]
        need_unfused = not args.attn_only and any(not f for _, f, _ in variant_specs)
        need_fused = not args.attn_only and any(f for _, f, _ in variant_specs)
        need_compact = not args.attn_only and any(
            f and vk.get("compact_mlp", False) for _, f, vk in variant_specs
        )
        need_noncompact = not args.attn_only and any(
            f and not vk.get("compact_mlp", False) for _, f, vk in variant_specs
        )
        if args.warm_weights_only and not args.attn_only:
            mp = -(-(args.batch * args.tokens) // 4) * 4
            to_warm = [weights[2], weights[4], weights[8], weights[10]]
            if args.full_vit:
                to_warm.extend([patch_weights[0], head_weights[2]])
            for w_tensor in to_warm:
                plan = MM.plan(1, mp, w_tensor.shape[0], w_tensor.shape[1])
                n_cols = w_tensor.shape[0]
                clusters = dev.machine.count("MG")
                dense_tn = next(
                    x * 32
                    for x in range(min(n_cols, 256) // 32, 0, -1)
                    if n_cols % (x * 32) == 0
                    and (plan.tm // 4) * (x * 8) <= 4096
                    and (x * 8) * plan.nk <= 256
                )
                cluster_tn = next(
                    (
                        x * 32
                        for x in range(min(n_cols, 256) // 32, 0, -1)
                        if n_cols % (x * 32) == 0
                        and (n_cols // (x * 32)) % clusters == 0
                        and (plan.tm // 4) * (x * 8) <= 4096
                        and (x * 8) * plan.nk <= 256
                    ),
                    dense_tn,
                )
                if args.mode == "prebuild":
                    warm_pairs = [
                        (p_var, r_rows)
                        for p_var in {
                            plan,
                            replace(plan, tn=dense_tn),
                            replace(plan, tn=cluster_tn),
                        }
                        for r_rows in (0, n_cols)
                    ]
                else:
                    warm_pairs = []
                    if need_unfused:
                        warm_pairs.append((plan, 0))
                    if need_fused:
                        if n_cols == 3072 and clusters > 1:
                            if need_noncompact:
                                warm_pairs.append(
                                    (replace(plan, tn=cluster_tn), n_cols)
                                )
                            if need_compact:
                                warm_pairs.append((replace(plan, tn=192), n_cols))
                        else:
                            warm_pairs.append((replace(plan, tn=cluster_tn), n_cols))
                for p_var, r_rows in warm_pairs:
                    FM.mx7(
                        dev,
                        w_tensor.st,
                        np.float16,
                        Operand("full", pitch=w_tensor.shape[1], rows=r_rows),
                        n_cols,
                        w_tensor.shape[1],
                        p_var,
                        "b",
                        1,
                    )
                if (
                    args.mode == "prebuild"
                    and not args.no_compact_mlp
                    and (not args.ablation or args.ablation_set != 1)
                    and n_cols == 3072
                ):
                    FM.mx7(
                        dev,
                        w_tensor.st,
                        np.float16,
                        Operand("full", pitch=w_tensor.shape[1], rows=n_cols),
                        n_cols,
                        w_tensor.shape[1],
                        replace(plan, tn=192),
                        "b",
                        1,
                    )
            dev.drain()
        if args.attn_only:
            qkv_arr = rng.normal(0, 0.2, (args.batch, args.tokens, 3 * w)).astype(
                np.float16
            )
            qkv_in = kp.tensor(qkv_arr)
            dev.drain()
            v_snap0 = unit_snapshot(dev, ctrl) if ctrl is not None else None
            if v_snap0 is not None:
                last_snap = v_snap0
            mark, u_mark, start = len(dev.runs), len(run_units), time.perf_counter()
            out_attn = native.projection_attention(
                qkv_in,
                mask,
                heads=w // 64,
                dim=64,
                fused=True,
                query_blocked=args.query_blocked,
                space=args.attn_space,
                fast_attn=not args.no_fast_attn,
                mover_overlap=not args.no_mover_overlap,
                quant_backend=args.quant_backend,
            )
            dev.drain()
            v_snap1 = unit_snapshot(dev, ctrl) if ctrl is not None else None
            runs = dev.runs[mark:]
            u_runs = run_units[u_mark:]
            recs = []
            for idx, r in enumerate(runs):
                u_d = u_runs[idx] if idx < len(u_runs) else {}
                max_u = max((u["busy_cycles"] for u in u_d.values()), default=0)
                recs.append(
                    {
                        "name": r.name,
                        "cycles": r.cycles,
                        "idle_all_units_cycles": max(0, r.cycles - max_u),
                        "kind": r.kind,
                        **({"units": u_d} if u_d else {}),
                    }
                )
            report["variants"].append(
                {
                    "label": "attn_only",
                    "fast_attn": not args.no_fast_attn,
                    "mover_overlap": not args.no_mover_overlap,
                    "quant_backend": args.quant_backend,
                    "cycles": sum(r.cycles for r in runs),
                    "idle_all_units_cycles": sum(
                        r["idle_all_units_cycles"] for r in recs
                    ),
                    "launches": len(runs),
                    "seconds": time.perf_counter() - start,
                    "units": (
                        unit_delta(v_snap0, v_snap1) if v_snap0 is not None else {}
                    ),
                    "records": recs,
                }
            )
            if args.mode == "sim":
                _ = out_attn.numpy()
        else:
            for v_label, fused, v_knobs in variant_specs:
                if not args.warm_weights_only:
                    if args.full_vit:
                        vit_forward(
                            kp.tensor(patch_in_arr),
                            kp.tensor(pos_cls_arr),
                            patch_weights,
                            block_list,
                            head_weights,
                            mask,
                            fused=fused,
                            ring_depth=args.ring_depth,
                            query_blocked=args.query_blocked,
                            mlp_space=args.mlp_space,
                            attn_space=args.attn_space,
                            **v_knobs,
                        )
                    else:
                        block(
                            kp.tensor(input_array),
                            weights,
                            mask,
                            fused=fused,
                            ring_depth=args.ring_depth,
                            query_blocked=args.query_blocked,
                            mlp_space=args.mlp_space,
                            attn_space=args.attn_space,
                            **v_knobs,
                        )
                    dev.drain()
                x = kp.tensor(patch_in_arr if args.full_vit else input_array)
                pos_cls = kp.tensor(pos_cls_arr) if args.full_vit else None
                dev.drain()
                v_snap0 = unit_snapshot(dev, ctrl) if ctrl is not None else None
                if v_snap0 is not None:
                    last_snap = v_snap0
                mark, u_mark, start = len(dev.runs), len(run_units), time.perf_counter()
                if args.full_vit:
                    _, states = vit_forward(
                        x,
                        pos_cls,
                        patch_weights,
                        block_list,
                        head_weights,
                        mask,
                        fused=fused,
                        ring_depth=args.ring_depth,
                        query_blocked=args.query_blocked,
                        mlp_space=args.mlp_space,
                        attn_space=args.attn_space,
                        **v_knobs,
                    )
                else:
                    _, states = block(
                        x,
                        weights,
                        mask,
                        fused=fused,
                        ring_depth=args.ring_depth,
                        query_blocked=args.query_blocked,
                        mlp_space=args.mlp_space,
                        attn_space=args.attn_space,
                        **v_knobs,
                    )
                dev.drain()
                v_snap1 = unit_snapshot(dev, ctrl) if ctrl is not None else None
                runs = dev.runs[mark:]
                u_runs = run_units[u_mark:]
                recs = []
                for idx, r in enumerate(runs):
                    u_d = u_runs[idx] if idx < len(u_runs) else {}
                    max_u = max((u["busy_cycles"] for u in u_d.values()), default=0)
                    recs.append(
                        {
                            "name": r.name,
                            "cycles": r.cycles,
                            "idle_all_units_cycles": max(0, r.cycles - max_u),
                            "kind": r.kind,
                            **({"units": u_d} if u_d else {}),
                        }
                    )
                report["variants"].append(
                    {
                        "label": v_label,
                        "fused": fused,
                        "knobs": v_knobs,
                        "cycles": sum(r.cycles for r in runs),
                        "idle_all_units_cycles": sum(
                            r["idle_all_units_cycles"] for r in recs
                        ),
                        "launches": len(runs),
                        "seconds": time.perf_counter() - start,
                        "units": (
                            unit_delta(v_snap0, v_snap1) if v_snap0 is not None else {}
                        ),
                        "records": recs,
                    }
                )
                if args.mode == "sim":
                    values.append(
                        {k: v.numpy().astype(np.float32) for k, v in states.items()}
                    )
                print(json.dumps(report["variants"][-1]), flush=True)
    if values:
        report["numerics"] = {
            k: {
                "max_absolute": float(np.abs(values[-1][k] - v).max()),
                "rms": float(np.sqrt(np.mean((values[-1][k] - v) ** 2))),
                "peak": float(np.abs(v).max()),
                "finite": bool(np.isfinite(values[-1][k]).all()),
            }
            for k, v in values[0].items()
        }
        if len(values) > 2:
            report["numerics_by_variant"] = {
                report["variants"][vi]["label"]: {
                    k: {
                        "max_absolute": float(np.abs(values[vi][k] - v).max()),
                        "rms": float(np.sqrt(np.mean((values[vi][k] - v) ** 2))),
                    }
                    for k, v in values[0].items()
                }
                for vi in range(1, len(values))
            }
    report["counters"] = dict(dev.counters)
    if args.mode == "sim":
        dev.close()
        report["v2_counters"] = parse_v2_log(log_path)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2))
    if args.mode == "prebuild":
        (args.library / "recorded.json").write_text(
            json.dumps(captured, default=encode_bytes)
        )
        (args.library / "graphs.json").write_text(json.dumps(sources))
        shutil.make_archive(str(args.archive.with_suffix("")), "zip", args.library)
    if values:
        print(json.dumps(report["numerics"]), flush=True)
        for vi in range(1, len(values)):
            for k, v in values[0].items():
                np.testing.assert_allclose(
                    values[vi][k], v, atol=1e-3, rtol=1e-2, err_msg=f"{vi}:{k}"
                )


if __name__ == "__main__":
    main()
