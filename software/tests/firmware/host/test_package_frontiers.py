"""Saved targets release independently of work sent after their capture."""

import native


def test_saved_frontiers_in_both_dispatch_modes(tmp_path):
    exe = native.on_double(
        tmp_path, "package_frontiers", ["firmware/dispatch/engine.c"]
    )
    native.run(exe, [])
