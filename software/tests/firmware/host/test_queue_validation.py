"""Fatal queue publication is confined to authorized firmware state storage."""

import subprocess

import native


def test_fatal_destination(tmp_path):
    exe = native.on_double(
        tmp_path,
        "queue_validation",
        ["firmware/boot/args.c"],
        app="firmware/queue/service.c",
    )
    subprocess.run([str(exe)], check=True, timeout=10)


def test_engine_fault_retains_queue_ownership(tmp_path):
    exe = native.on_double(
        tmp_path,
        "queue_quarantine",
        ["firmware/boot/args.c", "firmware/dispatch/engine.c"],
        app="firmware/package/interp.c",
    )
    subprocess.run([str(exe)], check=True, timeout=10)
