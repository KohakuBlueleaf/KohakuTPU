"""The process state a vector or a mutant may touch, put back after every
test and every runner: the process device (`runtime.device.attached`) and
tinygrad's view of the KTPU device (the synthesised module's class, and the
class and instance tinygrad caches for the name for the life of the process).
A runner that attaches a device or a mutant that swaps the device class holds
it for its own test only, and the next test, of this suite or another in the
same process, finds the state it had.

``KTPU_SPEC_LEAK=1`` is the witness mutant: nothing is put back, so a later
test meets what a vector left (application/tests/conftest.py fails it by
name)."""

import os
import sys
from contextlib import AbstractContextManager, contextmanager, nullcontext

from tinygrad.device import Device as TinyDevice

from kohakutpu.application.ktpugrad import device as KD
from kohakutpu.application.runtime import device as D

LEAK_ENV = "KTPU_SPEC_LEAK"


@contextmanager
def _tinygrad_kept():
    """The synthesised module's KTPUDevice as it was, and tinygrad's caches
    for the name emptied when the name no longer resolves to that class."""
    mod = sys.modules.get(KD.MODULE)
    keep = vars(mod).get("KTPUDevice") if mod is not None else None
    try:
        yield
    finally:
        mod = sys.modules.get(KD.MODULE)
        if mod is not None and keep is not None:
            vars(mod)["KTPUDevice"] = keep
            try:
                resolved = TinyDevice.get_class(KD.NAME)
            except Exception:  # noqa: BLE001 -- any failure: not resolvable
                resolved = None
            if resolved is not keep:
                TinyDevice.get_class.cache_clear()
                TinyDevice._Device__get_canonicalized_item.cache_clear()


@contextmanager
def _kept():
    with D.attached(), _tinygrad_kept():
        yield


def scoped() -> AbstractContextManager:
    """The block's changes to the process device and tinygrad's KTPU class
    undone when it ends."""
    return nullcontext() if os.environ.get(LEAK_ENV) == "1" else _kept()
