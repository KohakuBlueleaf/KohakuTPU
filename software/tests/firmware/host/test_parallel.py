"""The OS launcher starts all local resident applications before joining."""

import native


def test_parallel_starts_every_node_before_joining_and_preserves_failure(tmp_path):
    exe = native.on_double(
        tmp_path, "kos_parallel", [], app="os/utils/parallel.c", freestanding=False
    )
    native.run(exe, [])
