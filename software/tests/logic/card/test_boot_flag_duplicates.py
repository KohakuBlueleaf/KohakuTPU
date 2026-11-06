"""Repeating a boot option does not select a different option."""

import pytest

from kohakutpu.application.runtime.card.common import parse_boot_flags


@pytest.mark.parametrize("prefix", ["", "0:"])
def test_duplicate_flags_do_not_enable_another_option(prefix):
    assert parse_boot_flags(prefix + "TIMING+TIMING", (0, 1)) == parse_boot_flags(
        prefix + "TIMING", (0, 1)
    )
