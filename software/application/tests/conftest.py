"""The runtime device the card tests share: the Verilated card model the
runtime opens (``KOHAKUTPU_SIM_BUILD``, ``KOHAKUTPU_SIM_NODES``); every test
using it is skipped when that model is not built.

No test runs on a KTPU device class an earlier test left in tinygrad's
cache (`tinygrad_resolves_ktpu`). An on-card test never computes on another
device without saying so: one
asked to run offline (``KOHAKUTPU_DEVICE=offline``) is skipped with that
reason, and one whose process device is not the card it opened (a device
attached by an earlier test and left) fails, naming both."""

import os
import sys

import numpy as np
import pytest
from tinygrad.device import Device as TinyDevice

from kohakutpu.application.ktpugrad import device as KD
from kohakutpu.application.runtime import device as D

#: Device kinds that execute what they are given.
EXECUTES = ("sim", "card")


@pytest.fixture(autouse=True)
def tinygrad_resolves_ktpu():
    """tinygrad's KTPU name resolves to the stack's KTPUDevice before each
    test: tinygrad caches the class for the process, so a class an earlier
    test swapped in and left would be every later test's device."""
    if KD.MODULE in sys.modules:
        try:
            got = TinyDevice.get_class(KD.NAME)
        except Exception as e:
            got = e
        if got is not KD.KTPUDevice:
            pytest.fail(
                f"tinygrad resolves {KD.NAME!r} to {got!r}, not "
                f"{KD.KTPUDevice!r}: a class an earlier test swapped in was left "
                f"in tinygrad's device cache",
                pytrace=False,
            )


@pytest.fixture(scope="session")
def card_device():
    """The process's device as the card tests open it: a card or its model."""
    if not os.environ.get(D.BOARD_ENV):
        pytest.skip("RUL.011: card tests require KOHAKUTPU_BOARD")
    try:
        d = D.device()
    except FileNotFoundError as e:
        pytest.skip(str(e))
    if d.kind in EXECUTES:
        return d
    asked = os.environ.get(D.DEVICE_ENV)
    if asked == d.kind:
        pytest.skip(f"{D.DEVICE_ENV}={asked}: the card tests need a card or its model")
    pytest.fail(
        f"the card tests' device is {d!r}, which executes nothing, and "
        f"{D.DEVICE_ENV} is {asked!r}: a device attached by an earlier test was "
        f"left as the process's device",
        pytrace=False,
    )


@pytest.fixture
def dev(card_device):
    """The card device, checked to still be the process's device (tinygrad's
    KTPU device runs on the process's device) before each test."""
    now = D._device
    if now is not card_device:
        pytest.fail(
            f"the process's device is {now!r}, not the card tests' "
            f"{card_device!r}: a device attached by an earlier test was left",
            pytrace=False,
        )
    return card_device


@pytest.fixture(scope="session")
def rel():
    """max |got - want| / max |want|, in float64."""

    def measure(got, want) -> float:
        got, want = np.asarray(got, np.float64), np.asarray(want, np.float64)
        return float(np.abs(got - want).max() / max(np.abs(want).max(), 1e-9))

    return measure
