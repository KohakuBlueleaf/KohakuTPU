"""KohakuTPU's compute units, declared to the framework.

``ktpu.hw.device`` held ``CU_MATMUL`` / ``CU_VECTOR`` and a ``decode_dbg`` that
switched on them, which put two of this project's unit types inside the
framework's own address-map module. They live here now, and the framework learns
about them by registration.
"""

from kohakuaccel.device import type_code
from kohakuaccel.transport.base import MASK32
from kohakuaccel.unit import UnitType, register

MATMUL = "MG"  # mx_cluster_cu
VECTOR = "VC"  # vec_cu

MATMUL_CODE = type_code(MATMUL)
VECTOR_CODE = type_code(VECTOR)


def matmul_dbg(word: int) -> dict:
    """CU_DBG for a matmul cluster: ``{compute_cycles, memory_cycles}``.

    The array running (``gemm_busy || sweep_busy``) against ``S_FILL`` waiting on
    operands. Both are free-running and INDEPENDENT, so they overlap whenever a
    fill and a GEMM are in flight together and their sum is not a total.
    """
    return {
        "compute_cycles": (word >> 32) & MASK32,
        "memory_cycles": word & MASK32,
        "scope": "cumulative",
    }


def vector_dbg(word: int) -> dict:
    """CU_DBG for a vector core: its kernel's cycle count.

    PER RUN, not cumulative: ``vec_core`` clears it at every RUN, so it describes
    the last kernel rather than the session and must not be differenced.
    """
    return {
        "compute_cycles": word & MASK32,
        "memory_cycles": None,
        "scope": "last-run",
    }


MATMUL_UNIT = register(
    UnitType(name=MATMUL, decode_dbg=matmul_dbg, summary="MXFP7 matmul cluster")
)
VECTOR_UNIT = register(
    UnitType(name=VECTOR, decode_dbg=vector_dbg, summary="vector core")
)
