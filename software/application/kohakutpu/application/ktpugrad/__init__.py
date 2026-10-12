"""tinygrad on KohakuTPU: ``Tensor(x, device="KTPU")``, every kernel tinygrad
schedules lowered to a generated DSL kernel -> L2 -> L1 -> the card (the
Verilated card unless told otherwise).

    import kohakutpu.application.ktpugrad as ktpugrad    # installs the device
    from tinygrad import Tensor
    (Tensor(x, device="KTPU") @ Tensor(w, device="KTPU").T).relu().numpy()

`bridge(kernel)` runs your own `@kt.kernel` on Tensors; `capture(fn)` lists
the kernel asts `fn` scheduled; a kernel no lowering covers raises
`KTPUUnsupported` naming why. ``python -m kohakutpu.application.ktpugrad
--autoload`` lets ``DEV=KTPU`` find the device with no import at all.
docs/projects/kohakutpu/tinygrad.md is the reference.
"""

from kohakutpu.application.ktpugrad.bridge import bridge
from kohakutpu.application.ktpugrad.device import NAME, KTPUDevice, register
from kohakutpu.application.ktpugrad.lower import Plan, Refused, plan
from kohakutpu.application.ktpugrad.plans import PLANS
from kohakutpu.application.ktpugrad.seam import (
    KTPUUnsupported,
    capture,
    install,
    installed,
    to_program,
    uninstall,
)

install()

__all__ = [
    "NAME",
    "PLANS",
    "KTPUDevice",
    "KTPUUnsupported",
    "Plan",
    "Refused",
    "bridge",
    "capture",
    "install",
    "installed",
    "plan",
    "register",
    "to_program",
    "uninstall",
]
