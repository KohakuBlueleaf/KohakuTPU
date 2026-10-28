"""A device attached for a block is gone after it, however the block ends,
and tinygrad's KTPU device runs on whatever the process's device is."""

import pytest
from tinygrad.device import Device as TinyDevice

from kohakutpu.application.ktpugrad import device as KD
from kohakutpu.application.runtime import device as D


def test_an_attached_device_is_put_back_after_its_block():
    before = D._device
    a = D.Device(kind="offline", board="multimesh_v9t8")
    with D.attached(a) as got:
        assert got is a and D.device() is a
    assert D._device is before


def test_an_attach_inside_the_block_is_undone_and_an_error_too():
    before = D._device
    with pytest.raises(RuntimeError), D.attached():
        D.attach(D.Device(kind="offline", board="multimesh_v9t8"))
        raise RuntimeError("the block failed")
    assert D._device is before


def test_tinygrads_device_follows_the_process_device():
    KD.register()
    tiny = TinyDevice[KD.NAME]
    a, b = D.Device(kind="offline", board="multimesh_v9t8"), D.Device(
        kind="offline", board="multimesh_v9t8"
    )
    with D.attached(a):
        assert tiny.rt is a
        with D.attached(b):
            assert tiny.rt is b
        assert tiny.rt is a
