"""Logit diagnostics; a small margin is not evidence of a wrong classifier."""

import numpy as np

NUMERICS = "docs/spec/stack/numerics/formats.md"
RELATIVE_FLOOR = 1e-9
DEFAULT_INPUTS = 48
PER_IMAGE_SECONDS = 1.24


def distribution(values):
    values = np.asarray(values, np.float64)
    return dict(
        zip(
            ("min", "p50", "p90", "p95", "max"),
            map(float, np.quantile(values, [0, 0.5, 0.9, 0.95, 1])),
            strict=True,
        )
    )


def state_error(got, want):
    a, b = np.asarray(got, np.float64).ravel(), np.asarray(want, np.float64).ravel()
    if (
        a.shape != b.shape
        or not len(a)
        or not np.isfinite(a).all()
        or not np.isfinite(b).all()
    ):
        raise ValueError("state comparison needs matching finite nonempty arrays")
    absolute = float(np.abs(a - b).max())
    norm = float(np.linalg.norm(a) * np.linalg.norm(b))
    return dict(
        max_absolute_error=absolute,
        max_relative_error=absolute / max(float(np.abs(b).max()), RELATIVE_FLOOR),
        relative_l2=float(
            np.linalg.norm(a - b) / max(np.linalg.norm(b), RELATIVE_FLOOR)
        ),
        cosine_similarity=(
            float(np.dot(a, b) / norm) if norm else float(np.array_equal(a, b))
        ),
    )


def logit_report(got, want, relative_budget=0.03, cosine_min=0.999):
    """Stable class-index tie breaking, per image and aggregate diagnostics."""
    g, w = np.asarray(got, np.float64), np.asarray(want, np.float64)
    if g.shape != w.shape or g.ndim != 2 or g.shape[1] < 2 or not len(g):
        raise ValueError("logits must be matching nonempty [inputs, classes>=2] arrays")
    if not np.isfinite(g).all() or not np.isfinite(w).all():
        raise ValueError("logits must be finite")
    if (
        not np.isfinite(relative_budget)
        or relative_budget <= 0
        or not -1 <= cosine_min <= 1
    ):
        raise ValueError(
            "positive finite error budget and cosine threshold in [-1, 1] required"
        )
    rows = []
    for i, (a, b) in enumerate(zip(g, w, strict=True)):
        cpu = np.argsort(-b, kind="stable")
        card = np.argsort(-a, kind="stable")
        gap = float(b[cpu[0]] - b[cpu[1]])
        absolute = float(np.abs(a - b).max())
        norm = float(np.linalg.norm(a) * np.linalg.norm(b))
        cosine = float(np.dot(a, b) / norm) if norm else float(np.array_equal(a, b))
        c5, g5 = sorted(cpu[:5].tolist()), sorted(card[:5].tolist())
        k = min(5, len(b) - 1)
        selected, boundary = cpu[:k], b[cpu[k]]
        weights = b[selected] - boundary
        retained = np.isin(selected, card[:k])
        weight = float(weights.sum())
        correct_weight = float(weights[retained].sum())
        budget = relative_budget * max(float(np.abs(b).max()), RELATIVE_FLOOR)
        rows.append(
            dict(
                input=i,
                cpu_top1=int(cpu[0]),
                cpu_top2=int(cpu[1]),
                cpu_top1_logit=float(b[cpu[0]]),
                cpu_top2_logit=float(b[cpu[1]]),
                cpu_gap=gap,
                card_top1=int(card[0]),
                max_absolute_error=absolute,
                max_relative_error=absolute
                / max(float(np.abs(b).max()), RELATIVE_FLOOR),
                max_elementwise_relative_error=float(
                    (np.abs(a - b) / np.maximum(np.abs(b), RELATIVE_FLOOR)).max()
                ),
                cosine_similarity=cosine,
                decided=gap > 2 * absolute,
                top1_agrees=bool(cpu[0] == card[0]),
                cpu_top5=c5,
                card_top5=g5,
                top5_set_agrees=c5 == g5,
                top5_overlap=len(set(c5) & set(g5)) / len(c5),
                margin_topk=k,
                margin_weight=weight,
                retained_margin_weight=correct_weight,
                margin_weighted_topk_agreement=(
                    correct_weight / weight if weight else None
                ),
                diagnostic_absolute_budget=budget,
                within_diagnostic_budget=absolute <= budget,
                within_cosine_threshold=cosine >= cosine_min,
            )
        )
    decided = [r for r in rows if r["decided"]]
    return dict(
        per_input=rows,
        relative_floor=RELATIVE_FLOOR,
        decided_inputs=len(decided),
        undecided_inputs=len(rows) - len(decided),
        decided_top1_agreement=(
            sum(r["top1_agrees"] for r in decided) / len(decided) if decided else None
        ),
        top5_set_agreement=sum(r["top5_set_agrees"] for r in rows) / len(rows),
        top5_mean_overlap=sum(r["top5_overlap"] for r in rows) / len(rows),
        margin_weighted_topk_agreement=(
            sum(r["retained_margin_weight"] for r in rows)
            / sum(r["margin_weight"] for r in rows)
            if sum(r["margin_weight"] for r in rows)
            else None
        ),
        error_distributions={
            key: distribution([r[key] for r in rows])
            for key in ("max_absolute_error", "max_relative_error", "cosine_similarity")
        },
        diagnostic_budget=dict(
            relative_budget=relative_budget,
            cosine_min=cosine_min,
            inputs_within_error_budget=sum(r["within_diagnostic_budget"] for r in rows),
            inputs_within_cosine_threshold=sum(
                r["within_cosine_threshold"] for r in rows
            ),
            all_passed=all(
                r["within_diagnostic_budget"] and r["within_cosine_threshold"]
                for r in rows
            ),
            qualification="engineering diagnostic thresholds, not certified network bounds; 3% uses NUM.TOL.005 only as a local-scale yardstick, cosine threshold is user-configurable",
        ),
        margin_weight_definition="CPU top min(5,C-1) classes weighted by logit gap to next CPU class; count weight retained in card top-k; ties contribute zero",
    )


def depth_budget(blocks, measured_relative_error=None):
    """Conditional storage-only bound, not a bound on an ill-conditioned network."""
    u = 2**-11
    budget = blocks * u / (1 - blocks * u)
    return dict(
        source=f"{NUMERICS}: NUM.F16.003, NUM.MX7.001-003, NUM.ACC.002-005, NUM.TOL.005",
        blocks=blocks,
        f16_unit_roundoff=u,
        storage_only_relative_bound=budget,
        formula="gamma_b = b*2^-11/(1-b*2^-11)",
        measured_relative_error=measured_relative_error,
        within_storage_only_bound=(
            None
            if measured_relative_error is None
            else measured_relative_error <= budget
        ),
        full_network_bound=None,
        qualification=(
            "gamma_b assumes one normal f16 rounding per block, no amplification. "
            "It is a storage-only baseline, NOT a 12-block ViT accuracy guarantee. "
            "The numerics spec gives local bounds/tolerances, not a network bound: "
            "MXFP7 products, repeated operations, cancellation, normalization and "
            "residual conditioning must also be propagated. NUM.TOL.005's 3% is a "
            "local attention test tolerance, not a per-block composable bound."
        ),
    )
