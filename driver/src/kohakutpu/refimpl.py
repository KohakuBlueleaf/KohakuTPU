"""Third-party MX quantisers, used as the baseline instead of our own.

Two independent production implementations:

  microxcaling  Microsoft's emulation library, the reference the OCP MX paper
                was written against (github.com/microsoft/microxcaling).
  torchao       PyTorch's MX implementation (torchao.prototype.mx_formats).

Our own quantiser lives in :mod:`kohakutpu.formats`. It is NOT the baseline --
a format comparison where we wrote both the candidate and the yardstick proves
nothing, because a mistake in the yardstick reads as a win for the candidate.
These are here so the yardstick is somebody else's code.

Both are optional. Import errors surface as `available()` returning False
rather than as a crash, so the comparison degrades to "our impl only, clearly
labelled" instead of silently substituting it.
"""

import numpy as np

_ERRORS = {}

# Deliberately broad: these are optional third-party libraries and ANY reason
# they will not import -- missing, wrong torch version, a broken CUDA probe --
# means the same thing here, which is that the comparison runs without them.
try:  # microxcaling
    import torch
    from mx.mx_ops import _quantize_mx

    _HAVE_MX = True
except Exception as e:  # noqa: BLE001
    _HAVE_MX = False
    _ERRORS["microxcaling"] = repr(e)

try:  # torchao
    import torch
    from torchao.prototype.mx_formats.mx_tensor import MXTensor

    _HAVE_AO = True
except Exception as e:  # noqa: BLE001
    _HAVE_AO = False
    _ERRORS["torchao"] = repr(e)


# microxcaling element names
MX_ELEM = {
    "E4M3": "fp8_e4m3",
    "E5M2": "fp8_e5m2",
    "E2M3": "fp6_e2m3",
    "E3M2": "fp6_e3m2",
    "E2M1": "fp4_e2m1",
    "int8": "int8",
}


def available():
    return {"microxcaling": _HAVE_MX, "torchao": _HAVE_AO}


def errors():
    return dict(_ERRORS)


def microxcaling_quantise(x, kind="E4M3", block=32):
    """Quantise-dequantise with Microsoft's reference emulation library."""
    if not _HAVE_MX:
        raise RuntimeError(f"microxcaling unavailable: {_ERRORS.get('microxcaling')}")
    t = torch.from_numpy(np.ascontiguousarray(np.asarray(x, dtype=np.float32)))
    q = _quantize_mx(
        t,
        8,  # scale_bits: E8M0
        MX_ELEM[kind],
        block_size=block,
        axes=[-1],
        round="nearest",
        flush_fp32_subnorms=False,
    )
    return q.numpy().astype(np.float64)


def torchao_quantise(x, kind="E4M3", block=32):
    """Quantise-dequantise with torchao's MXTensor."""
    if not _HAVE_AO:
        raise RuntimeError(f"torchao unavailable: {_ERRORS.get('torchao')}")
    dt = {
        "E4M3": torch.float8_e4m3fn,
        "E5M2": torch.float8_e5m2,
    }.get(kind)
    if dt is None:
        from torchao.prototype.mx_formats.constants import (
            DTYPE_FP4,
            DTYPE_FP6_E2M3,
            DTYPE_FP6_E3M2,
        )

        dt = {"E2M3": DTYPE_FP6_E2M3, "E3M2": DTYPE_FP6_E3M2, "E2M1": DTYPE_FP4}[kind]
    t = torch.from_numpy(np.ascontiguousarray(np.asarray(x, dtype=np.float32)))
    mx = MXTensor.to_mx(t, dt, block)
    # Dequantise through torchao's own to_dtype(), which is the documented path
    # back to high precision; the tensor subclass itself refuses .numpy().
    from torchao.prototype.mx_formats.mx_tensor import to_dtype

    hp = to_dtype(mx.qdata, mx.scale, mx.elem_dtype, mx.block_size, torch.float32)
    return np.asarray(hp.detach().cpu().reshape(t.shape), dtype=np.float64)
