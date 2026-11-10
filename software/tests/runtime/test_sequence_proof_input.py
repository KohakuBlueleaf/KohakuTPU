"""Supplemental checker API guards; arithmetic witnesses live alongside this."""

import pytest

from kohakutpu.stack import rs


@pytest.mark.parametrize(
    "runs, independent, every, message",
    [
        ([], True, 0, "need participants"),
        ([{}], True, 0, "need participants"),
        ([{0: b""}, {1: b""}], True, 2, "identical participants"),
        ([{0: b""}] * 3, True, 2, "must divide"),
        ([{0: b""}], False, 1, "requires independent_packages"),
    ],
)
def test_invalid_epoch_layout_refuses_before_package_or_machine_decode(
    runs, independent, every, message
):
    # Deliberately invalid bytes ensure no parse/proof is attempted before the
    # structural guard. These supplement, not replace, numerical base witnesses.
    with pytest.raises(ValueError, match=message):
        rs._rs.package_check_memory(
            runs,
            "not machine JSON",
            {},
            independent_packages=independent,
            host_join_every=every,
        )
