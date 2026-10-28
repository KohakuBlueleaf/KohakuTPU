"""Observed lowering stages, not inferred from the outer tinygrad plan kind."""

import math
from contextlib import ExitStack
from unittest.mock import patch

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.ktpugrad import run as R
from kohakutpu.application.ktpugrad import uops as U
from kohakutpu.application.runtime import rows as RW
from kohakutpu.application.runtime import tree as TR


class ArithmeticPath:
    """Capture the actual selected runtime path while executing one schedule AST."""

    def __init__(self, device):
        self.device = device
        self.stages = []
        self.unsupported = []
        self.materialized_reductions = 0
        self.stack = ExitStack()

    def __enter__(self):
        product, column, launch, launch_wide, materialise, narrow, flat = (
            R._product,
            TR.column,
            RW._launch,
            RW._launch_wide,
            R._materialise,
            R._narrow,
            R._flat,
        )

        def observe_product(dev, plan, bufs):
            if dev is self.device:
                m, n, k = plan.mnk
                if self.materialized_reductions or any(
                    isinstance(a.slot, tuple) and a.slot[0] in ("_tmp", "narrow")
                    for a in (plan.a, plan.b)
                ):
                    self.unsupported.append(
                        "MXFP7 conversion after computed intermediate: operand expression bound unavailable"
                    )
                count = math.prod(U.extent(r) for r in R._ks(k))
                self.stages.append(
                    dict(
                        stage="cluster_product",
                        M=U.extent(m),
                        N=U.extent(n),
                        K=count,
                        operand_slots=[repr(a.slot) for a in (plan.a, plan.b)],
                        blocks=math.ceil(count / 32),
                        formats=[
                            "f16",
                            "MXFP7",
                            "exact int sum/32",
                            "FP22",
                            "f16 drain",
                        ],
                        order="K blocks accumulated serially within one tile; clusters partition M/N tiles, not K",
                        source="ktpugrad/run.py:_product; NUM.MX7.002-003; NUM.ACC.002-006",
                    )
                )
            result = product(dev, plan, bufs)
            if dev is self.device:
                stage = next(
                    s for s in reversed(self.stages) if s["stage"] == "cluster_product"
                )
                for launched in result:
                    mm = launched.info.get("mm")
                    if mm is not None:
                        stage.update(
                            padded_K=mm.kp,
                            K_chunk=mm.kc,
                            K_trips=mm.kp // mm.kc,
                            tile_M=mm.tm,
                            tile_N=mm.tn,
                            padded_N=mm.n,
                            epilogue=repr(plan.expr),
                            name=launched.name,
                        )
            return result

        def observe_column(
            dev, e, operands, binds, rows, width, out, out_at, info=None
        ):
            if dev is self.device:
                levels = math.ceil(math.log2(rows)) if rows > 1 else 0
                self.stages.append(
                    dict(
                        stage="column_tree",
                        rows=rows,
                        width=width,
                        levels=levels,
                        materialize_argument=rows > 1
                        and 2 * len(operands) + 1 > TR.VIEWS,
                        formats=[
                            "f16 inputs",
                            "E8M15 pairwise fold",
                            "f16 at every level",
                            "E8M15 epilogue",
                            "f16",
                        ],
                        order="top/bottom half pairs, odd last row carried, ceil(log2 R) levels",
                        source="runtime/tree.py:column",
                    )
                )
            return column(dev, e, operands, binds, rows, width, out, out_at, info)

        def observe_launch(dev, e, operands, binds, rows, width, out, info, cores):
            result = launch(dev, e, operands, binds, rows, width, out, info, cores)
            if dev is self.device:
                self.stages.append(
                    dict(
                        stage="rows",
                        rows=rows,
                        width=width,
                        blocks=result[2],
                        output=out.kind,
                        expression=repr(e),
                        reductions=[r.op for r in X.reduces(e)],
                        formats=["f16", "E8M15", "f16"],
                        name=result[0].name,
                        source="runtime/rows.py:run,_launch; kernels/vector.py:rows_kernel",
                    )
                )
            return result

        def observe_materialise(dev, plan, bufs):
            if dev is self.device:
                self.stages.append(
                    dict(
                        stage="materialize_reduction",
                        expression=repr(R._leaf_reduce(plan)),
                        formats=["computed reduction", "f16 temporary", "consumer"],
                        source="ktpugrad/run.py:_materialise,_split_reduction",
                    )
                )
            result = materialise(dev, plan, bufs)
            if dev is self.device:
                self.materialized_reductions += 1
            return result

        def observe_narrow(dev, plan, bufs):
            if dev is self.device:
                self.unsupported.append(
                    "non-reduction expression materialized as f16 by _narrow: expression-bound propagation unavailable"
                )
                self.stages.append(
                    dict(
                        stage="narrow",
                        formats=["E8M15", "f16 temporary", "consumer"],
                        source="ktpugrad/run.py:_narrow",
                    )
                )
            return narrow(dev, plan, bufs)

        def observe_flat(dev, plan, bufs, cores):
            result = flat(dev, plan, bufs, cores)
            if dev is self.device and result is not None:
                self.stages.append(
                    dict(
                        stage="flat",
                        elements=plan.out_n,
                        expression=repr(plan.expr),
                        formats=["f16", "E8M15", "f16"],
                        name=result[0].name if result else plan.name,
                        source="ktpugrad/run.py:_flat; runtime/generated.py:flat",
                    )
                )
            return result

        def observe_launch_wide(dev, e, operands, binds, rows, width, out, info, cores):
            result = launch_wide(dev, e, operands, binds, rows, width, out, info, cores)
            if dev is self.device:
                self.stages.append(
                    dict(
                        stage="rows",
                        rows=rows,
                        width=width,
                        blocks=result[2],
                        output=out.kind,
                        expression=repr(e),
                        reductions=[r.op for r in X.reduces(e)],
                        formats=["f16", "E8M15", "f16"],
                        name=result[0].name,
                        source="runtime/rows.py:run,_launch_wide",
                    )
                )
            return result

        self.stack.enter_context(patch.object(R, "_product", observe_product))
        self.stack.enter_context(patch.object(TR, "column", observe_column))
        self.stack.enter_context(patch.object(RW, "_launch", observe_launch))
        self.stack.enter_context(patch.object(RW, "_launch_wide", observe_launch_wide))
        self.stack.enter_context(patch.object(R, "_materialise", observe_materialise))
        self.stack.enter_context(patch.object(R, "_narrow", observe_narrow))
        self.stack.enter_context(patch.object(R, "_flat", observe_flat))
        return self

    def __exit__(self, *exc):
        return self.stack.__exit__(*exc)

    def report(self, launches=()):
        products = [s for s in self.stages if s["stage"] == "cluster_product"]
        arithmetic_launches = sum(
            (2 if r["kind"] == "matmul" else 1)
            for r in launches
            if r["kind"] in ("matmul", "dsl")
        )

        def _stage_depth(s):
            if s["stage"] == "column_tree":
                return s.get("levels", 0) + int(bool(s.get("materialize_argument"))) + 1
            if s["stage"] == "rows":
                b = s.get("blocks", 1)
                if b > 1 and s.get("output") == "row" and s.get("reductions"):
                    return 2 + math.ceil(math.log2(b))
                return 1
            if s["stage"] in ("flat", "cluster_product"):
                return 1
            return 0

        return dict(
            stages=self.stages,
            uncertifiable_reasons=list(dict.fromkeys(self.unsupported)),
            contains_mxfp7=bool(products),
            max_cluster_blocks=max((s["blocks"] for s in products), default=0),
            # Every arithmetic rows launch or internal fold stage can add one storage rounding on a path.
            f16_stage_upper_bound=max(
                arithmetic_launches,
                sum(
                    s["stage"] in ("rows", "flat", "cluster_product")
                    for s in self.stages
                ),
                sum(_stage_depth(s) for s in self.stages),
            ),
            depth_policy="upper bound across all observed arithmetic launches; parallel branches deliberately overcounted",
            status="observed lowering, not a hardware fault attribution",
            suspected_error_sources=(
                [
                    "MXFP7 operand quantization",
                    "FP22 block accumulation",
                    "f16 drain before epilogue",
                ]
                if products
                else ["f16 partial stores before cancellation"]
            ),
            source="docs/spec/stack/numerics/formats.md#lowered-tinygrad-reduction-error-budget",
        )
