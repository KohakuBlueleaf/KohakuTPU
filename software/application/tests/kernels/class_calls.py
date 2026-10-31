"""The class calls `test_class_pins` pins: one of each route."""

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels.classes.policy import ShapeClass
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.runtime import generated as G

CLASS = ShapeClass(32, 256)
ADD = X.op("add", X.Load(0), X.Load(1))
PLUS_ONE = X.op("add", X.Load(0), 1.0)


def full(pitch, **fields):
    return Operand("full", pitch=pitch, **fields)


def rows(e, operands, R, W, out):
    return "vector.rows_kernel", G.rows_arguments(e, operands, R, W, out), {}


def lanes(words, keep):
    arguments = {
        "class": {"taps": [[0, 0x5555], [1, 0xAAAA]], "words": words, "keep": keep},
        "src_words": 4096,
        "dst_words": 4096,
        "core_policy": "all",
    }
    return "lanes.class_kernel", arguments, {}


#: name -> (generator, arguments, `KTPU_CLASS_*` environment).
CALLS = {
    "rows": rows(PLUS_ONE, (full(96),), 17, 96, full(96)),
    "rows_offset": rows(
        PLUS_ONE, (full(160, offset=192),), 20, 128, full(160, offset=32)
    ),
    "rows_f32": rows(
        PLUS_ONE, (full(128, dtype="f32"),), 32, 128, full(128, dtype="f32")
    ),
    "rows_stat": rows(X.Reduce("sum", PLUS_ONE), (full(96),), 17, 96, Operand("row")),
    "rows_prod": rows(X.Reduce("prod", PLUS_ONE), (full(96),), 17, 96, Operand("row")),
    "rows_row": rows(ADD, (full(128), Operand("row")), 32, 128, full(128)),
    "rows_col": rows(ADD, (full(128), Operand("col", offset=32)), 32, 128, full(128)),
    "rows_scalar": rows(
        ADD, (full(128), Operand("scalar", offset=7)), 32, 128, full(128)
    ),
    "wide": rows(ADD, (full(416), full(416)), 17, 416, full(416)),
    "wide_col": rows(ADD, (full(416), Operand("col")), 17, 416, full(416)),
    "wide_row": rows(ADD, (full(416), Operand("row")), 17, 416, full(416)),
    "wide_scalar": rows(ADD, (full(416), Operand("scalar")), 17, 416, full(416)),
    "fold": rows(
        X.Reduce("max", ADD), (full(416), Operand("col")), 33, 416, Operand("row")
    ),
    "flat": (
        "vector.flat_kernel",
        {
            "e": G.expr(ADD),
            "dtypes": ["f16", "f16"],
            "n": 129,
            "out_dtype": "f16",
            "scalars": [[1, 7]],
        },
        {},
    ),
    "copy": ("convert.copy_kernel", {"nbytes": 192}, {}),
    "walk": (
        "convert.walk_kernel",
        {
            "src_bytes": 448,
            "levels": [[2, 224], [7, 32]],
            "at": 0,
            "dst_bytes": 448,
            "dlevels": [[14, 32]],
            "dat": 0,
        },
        {},
    ),
    "transpose": ("transpose.transpose_kernel", {"nb": 2, "A": 16, "B": 48}, {}),
    "quant": (
        "matmul.quant_kernel",
        {
            "src": vars(full(96, offset=16)),
            "rows": 17,
            "cols": 80,
            "rp": 24,
            "cp": 128,
            "side": "a",
            "g": 2,
            "nk": 1,
            "batches": 3,
        },
        {},
    ),
    "mm": (
        "matmul.mm_kernel",
        {
            "mm": {
                "made_by": {
                    "gen": "matmul.plan",
                    "args": {"bt": 2, "m": 9, "n": 96, "k": 65},
                }
            },
            "e": G.expr(ADD),
            "extras": [vars(full(128, offset=16))],
            "out": vars(full(128, offset=16)),
        },
        {},
    ),
    "direct": (
        *rows(X.Reduce("sum", X.Load(0)), (full(128),), 32, 128, Operand("row"))[:2],
        {"KTPU_CLASS_ROWS_DIRECT": "1"},
    ),
    "band_1": (
        *rows(PLUS_ONE, (full(128),), 32, 128, full(128))[:2],
        {"KTPU_CLASS_BAND": "1"},
    ),
    "wide_256": (
        *rows(X.op("max", X.Load(0), 0.0), (full(512),), 16, 512, full(512))[:2],
        {"KTPU_CLASS_WIDE_WIDTH": "256"},
    ),
    "wide_direct": (
        *rows(ADD, (full(512), Operand("col")), 16, 512, full(512))[:2],
        {"KTPU_CLASS_WIDE_WIDTH": "256", "KTPU_CLASS_WIDE_DIRECT": "1"},
    ),
    "lanes": lanes(65, False),
    "lanes_keep": lanes(7, True),
}
