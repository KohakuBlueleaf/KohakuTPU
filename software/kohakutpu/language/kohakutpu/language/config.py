"""The process environment, the project's one configuration source.

`ENV` reads at call time with `os.environ`'s semantics. `VARIABLES` is each
variable with the modules that read it.
"""

import os


class Environment:
    def get(self, name: str, default=None):
        return os.environ.get(name, default)

    def __contains__(self, name: str) -> bool:
        return name in os.environ

    def items(self):
        return os.environ.items()

    def copy(self) -> dict:
        return dict(os.environ)


ENV = Environment()

VARIABLES = {
    "KOHAKUTPU_ADMISSION_CONTEXT": ("application/runtime/packages.py",),
    "KOHAKUTPU_ARENA_LOOKUP": ("application/runtime/memory/arena.py",),
    "KOHAKUTPU_BOARD": (
        "application/runtime/card/__init__.py",
        "application/runtime/device/__init__.py",
    ),
    "KOHAKUTPU_BOOT_FLAGS": ("application/runtime/card/__init__.py",),
    "KOHAKUTPU_DEVICE": ("application/runtime/device/__init__.py",),
    "KOHAKUTPU_EAGER_FLUSH": ("application/runtime/device/__init__.py",),
    "KOHAKUTPU_EXECUTION_LIST": ("application/runtime/device/__init__.py",),
    "KOHAKUTPU_HOST_SUBMIT_CACHE": ("application/runtime/card/__init__.py",),
    "KOHAKUTPU_KEEP_FAILED": ("application/runtime/card/collection.py",),
    "KOHAKUTPU_LIBRARY_CLASSES": ("application/runtime/device/__init__.py",),
    "KOHAKUTPU_OS_COMPACT": ("application/runtime/os/card.py",),
    "KOHAKUTPU_OS_DE_FETCH": (
        "application/runtime/os/card.py",
        "os/tools/ostpu/replay.py",
    ),
    "KOHAKUTPU_OS_DE_FUSE": (
        "application/runtime/os/card.py",
        "os/tools/ostpu/replay.py",
    ),
    "KOHAKUTPU_OS_DE_STREAM": (
        "application/runtime/os/card.py",
        "os/tools/ostpu/replay.py",
    ),
    "KOHAKUTPU_OS_LAUNCH_RING": (
        "application/runtime/os/card.py",
        "os/tools/ostpu/replay.py",
    ),
    "KOHAKUTPU_OS_MOVERS": ("application/runtime/os/card.py",),
    "KOHAKUTPU_OS_PRUNE": (
        "application/runtime/os/card.py",
        "os/tools/ostpu/replay.py",
    ),
    "KOHAKUTPU_OS_SEGMENT": ("application/runtime/os/card.py",),
    "KOHAKUTPU_OS_STAMPS": (
        "application/runtime/os/card.py",
        "os/tools/ostpu/replay.py",
    ),
    "KOHAKUTPU_PACKAGE_HOME": ("application/runtime/device/__init__.py",),
    "KOHAKUTPU_PREPARED_REPLAY": ("application/runtime/device/__init__.py",),
    "KOHAKUTPU_PROFILE": ("application/runtime/device/__init__.py",),
    "KOHAKUTPU_QUEUE": ("application/runtime/device/__init__.py",),
    "KOHAKUTPU_QUEUE_BYTES": ("application/runtime/device/__init__.py",),
    "KOHAKUTPU_QUEUE_DEPTH": ("application/runtime/device/__init__.py",),
    "KOHAKUTPU_REPLAY_OVERCOMMIT": ("application/runtime/device/__init__.py",),
    "KOHAKUTPU_REPLAY_PACKAGE_BYTES": ("application/runtime/device/__init__.py",),
    "KOHAKUTPU_REPLAY_PACKAGE_FRACTION": ("application/runtime/device/__init__.py",),
    "KOHAKUTPU_RESIDENT": (
        "application/runtime/card/__init__.py",
        "application/runtime/device/__init__.py",
        "application/runtime/offline.py",
    ),
    "KOHAKUTPU_SIM_BUILD": ("application/runtime/device/__init__.py",),
    "KOHAKUTPU_SIM_NODES": ("application/runtime/device/__init__.py",),
    "KOHAKUTPU_TEMPLATE_PROOF_CAP": ("application/runtime/template_store.py",),
    "KOHAKUTPU_TEMPLATE_VERIFICATION": ("application/runtime/template_store.py",),
    "KOHAKUTPU_TRANSPORT": ("application/runtime/device/__init__.py",),
    "KOHAKU_VLT_ENV": ("simulation/verilator/replay.py",),
    "KTPU_AUTO_SEQUENCE": (
        "application/ktpugrad/captured/auto_sequence.py",
        "application/runtime/device/__init__.py",
    ),
    "KTPU_CLASS_BAND": (
        "application/kernels/classes/rows.py",
        "application/runtime/costs.py",
        "application/runtime/generated.py",
    ),
    "KTPU_CLASS_ROWS_DIRECT": (
        "application/kernels/classes/rows.py",
        "application/runtime/costs.py",
        "application/runtime/generated.py",
        "application/runtime/rows.py",
    ),
    "KTPU_CLASS_ROWS_ROUTE": (
        "application/runtime/costs.py",
        "application/runtime/rows.py",
    ),
    "KTPU_CLASS_WIDE_DIRECT": (
        "application/kernels/classes/wide.py",
        "application/runtime/generated.py",
    ),
    "KTPU_CLASS_WIDE_WIDTH": (
        "application/kernels/classes/wide.py",
        "application/runtime/generated.py",
    ),
    "KTPU_COMPOSED": ("application/runtime/forms.py",),
    "KTPU_COST_PROFILE": ("application/runtime/costs.py",),
    "KTPU_DUMP": ("compiler/stack/kernel.py",),
    "KTPU_EAGER_CALL_CAPTURE": ("application/ktpugrad/captured/call_capture.py",),
    "KTPU_EAGER_EXEC": ("application/runtime/device/__init__.py",),
    "KTPU_EAGER_RECIPE": ("application/runtime/device/__init__.py",),
    "KTPU_EAGER_SCHEDULE": ("application/ktpugrad/captured/eager_schedule.py",),
    "KTPU_FP16_MX7_BACKEND": ("application/runtime/machine.py",),
    "KTPU_FUSE_*": ("application/ktpugrad/planning/fusion.py",),
    "KTPU_GRAPH_*": ("application/runtime/graph/recorded.py",),
    "KTPU_GRAPH_FUSE": ("application/runtime/device/replay.py",),
    "KTPU_HOST_RECIPE": ("application/ktpugrad/captured/recipe.py",),
    "KTPU_INFERENCE_REPLAY": ("application/ktpugrad/inference.py",),
    "KTPU_LOCAL_SCRATCH": ("application/runtime/device/kernels.py",),
    "KTPU_LOCAL_SCRATCH_BYTES": ("application/runtime/device/kernels.py",),
    "KTPU_MACHINES": ("language/machine/__init__.py (at import)",),
    "KTPU_MATMUL_RING_DEPTH": ("application/ktpugrad/run/product.py",),
    "KTPU_MX7_CORES": ("application/runtime/machine.py",),
    "KTPU_MX7_LOOPED": ("application/runtime/machine.py",),
    "KTPU_MX7_PAIRING": ("application/kernels/matmul.py",),
    "KTPU_MX7_SLOTS": ("application/runtime/machine.py",),
    "KTPU_NATIVE_CAPTURE": ("application/ktpugrad/captured/call_capture.py",),
    "KTPU_NATIVE_DIRECT": ("application/runtime/device/__init__.py",),
    "KTPU_NATIVE_EXCHANGE": ("application/runtime/card/__init__.py",),
    "KTPU_NATIVE_GRAPH_REWRITE": ("application/ktpugrad/captured/graph_walk.py",),
    "KTPU_NATIVE_GRAPH_WALK": ("application/ktpugrad/captured/graph_walk.py",),
    "KTPU_NATIVE_MEMORY": ("application/runtime/memory/__init__.py",),
    "KTPU_NATIVE_POLL": ("application/runtime/card/__init__.py",),
    "KTPU_NATIVE_POLL_SLEEP": ("application/runtime/native/poll.py",),
    "KTPU_NATIVE_PROGRAM": ("application/ktpugrad/captured/recipe.py",),
    "KTPU_NATIVE_QUEUE": ("application/runtime/card/__init__.py",),
    "KTPU_NATIVE_RECIPE": ("application/runtime/device/__init__.py",),
    "KTPU_PIPE_FINITE_BATCH": (
        "application/kernels/matmul.py",
        "application/runtime/costs.py",
        "application/runtime/generated.py",
    ),
    "KTPU_PIPE_OVERLAP": (
        "application/kernels/matmul.py",
        "application/runtime/costs.py",
        "application/runtime/generated.py",
    ),
    "KTPU_PLAN_CACHE": ("application/ktpugrad/memo.py",),
    "KTPU_PREPARED": ("application/runtime/forms.py",),
    "KTPU_PREPARED_QUANT": ("application/runtime/forms.py",),
    "KTPU_PULL_TREE": ("application/runtime/costs.py",),
    "KTPU_QUANT_CLASSIFIER_OVERLAP": (
        "application/kernels/matmul.py",
        "application/runtime/costs.py",
        "application/runtime/generated.py",
    ),
    "KTPU_QUANT_FILL_OVERLAP": (
        "application/kernels/matmul.py",
        "application/runtime/costs.py",
        "application/runtime/generated.py",
    ),
    "KTPU_QUANT_PIPELINE": (
        "application/kernels/matmul.py",
        "application/runtime/costs.py",
        "application/runtime/generated.py",
    ),
    "KTPU_QUANT_STREAM_AXES": (
        "application/kernels/matmul.py",
        "application/runtime/costs.py",
        "application/runtime/generated.py",
    ),
    "KTPU_QUANT_ZERO_CLASSIFIER": (
        "application/kernels/matmul.py",
        "application/runtime/costs.py",
        "application/runtime/generated.py",
    ),
    "KTPU_QUANT_ZERO_REUSE": (
        "application/kernels/matmul.py",
        "application/runtime/costs.py",
        "application/runtime/generated.py",
    ),
    "KTPU_RS_MACHINE": ("compiler/stack/rs.py",),
    "KTPU_SINGLE_PACKAGE_AST": ("application/runtime/rows.py",),
    "KTPU_SPECIALISE_CACHE": ("application/runtime/device/__init__.py",),
    "KTPU_TINYGRAD_FUSE": ("application/ktpugrad/seam.py",),
    "KTPU_WINDOW_SWAP": ("application/ktpugrad/run/pad.py",),
    "KTPU_WIRE_RECIPE": ("application/runtime/device/__init__.py",),
    "USERPROFILE": ("simulation/verilator/replay.py",),
    "XDMA_KO": ("application/runtime/card/__init__.py",),
}
