"""Inclusive phase buckets, per-launch cycle-stamp ring and per-step IPC/cache attribution."""

import argparse
import json
import pathlib

TERMS = (
    "dispatcher_bookkeeping",
    "syscalls_and_traps",
    "dependency_completion",
    "soft_mmu_translation",
    "engine_setup",
    "user_dram_fetch",
    "tpu_execution",
)


def attribution(os_run, firmware):
    n = os_run["launches"]
    o, f = os_run["results"][0], firmware["results"][0]
    phases = {
        name: sum(p["phases"].get(name, 0) for p in f["packages"])
        for name in ("head", "copy", "bind", "steps", "barrier", "idle")
    }
    buckets = {k: v for k, v in o.items() if k.startswith("cyc_")}
    return dict(
        launches=n,
        os_total_per_launch=o["cycles"] / n,
        firmware_package_total_per_launch=f["cycles"] / n,
        os_inclusive_per_launch={k: v / n for k, v in buckets.items()},
        firmware_inclusive_per_launch={k: v / n for k, v in phases.items()},
        os_phase_sum_minus_run_cycles=sum(buckets.values()) - o["cycles"],
        engine_syscalls_per_launch=o["unit_batches"] / n,
        tasks_per_launch=o["tasks"] / n,
        requested_exclusive_os={term: None for term in TERMS},
        requested_exclusive_firmware={
            term: (
                0
                if term
                in ("syscalls_and_traps", "soft_mmu_translation", "user_dram_fetch")
                else None
            )
            for term in TERMS
        },
    )


def ring_summary(records, is_os=True):
    n = len(records)
    if not n:
        raise ValueError("expected non-empty launch ring")
    keys = (
        "cyc_total",
        "cyc_walk",
        "cyc_dep",
        "cyc_xlat",
        "cyc_build",
        "cyc_submit",
        "cyc_wait",
        "cyc_retire",
        "cyc_sys",
        "syscalls",
        "batches",
    )
    sums = {k: sum(r[k] for r in records) for k in keys}
    means = {k: sums[k] / n for k in keys}
    if not is_os:
        return dict(
            launches=n,
            sums=sums,
            means=means,
            firmware_imem_split=dict(
                package_imem_total=sums["cyc_total"],
                package_imem_per_launch=sums["cyc_total"] / n,
                boundary_idle_total=sums["cyc_sys"],
                boundary_idle_per_launch=sums["cyc_sys"] / n,
                user_dram_total=0,
                soft_mmu_xlat_total=0,
                os_syscalls_total=0,
            ),
        )
    kernel_other = (
        sums["cyc_sys"] - sums["cyc_xlat"] - sums["cyc_submit"] - sums["cyc_wait"]
    )
    user_dram = sums["cyc_total"] - sums["cyc_sys"]
    return dict(
        launches=n,
        sums=sums,
        means=means,
        disjoint_cpu_split=dict(
            user_dram_total=user_dram,
            user_dram_per_launch=user_dram / n,
            kernel_imem_total=sums["cyc_sys"],
            kernel_imem_per_launch=sums["cyc_sys"] / n,
            kernel_xlat_total=sums["cyc_xlat"],
            kernel_submit_total=sums["cyc_submit"],
            kernel_wait_total=sums["cyc_wait"],
            kernel_trap_and_other_total=kernel_other,
        ),
    )


def step_ring_summary(step_rec):
    ipc_and_cache = (
        step_rec["client_cache_acq"]
        + step_rec["client_send"]
        + step_rec["server_wake_recv"]
        + step_rec["check_or_reuse"]
        + step_rec["cache_release"]
        + step_rec["cache_acquire"]
        + step_rec["server_reply"]
    )
    return dict(
        step_total=step_rec["step_total"],
        kds_run_cycles=step_rec["kds_run_cycles"],
        cyc_msgs=step_rec["cyc_msgs"],
        retained_hit=bool(step_rec["retained_hit"]),
        de_run_cycles=step_rec["de_run"],
        ipc_and_cache_total=ipc_and_cache,
        breakdown=dict(
            client_cache_acq=step_rec["client_cache_acq"],
            client_send=step_rec["client_send"],
            server_wake_recv=step_rec["server_wake_recv"],
            check_or_reuse=step_rec["check_or_reuse"],
            cache_release=step_rec["cache_release"],
            cache_acquire=step_rec["cache_acquire"],
            server_reply=step_rec["server_reply"],
        ),
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("os_result", type=pathlib.Path)
    p.add_argument("firmware_result", type=pathlib.Path)
    p.add_argument("--out", type=pathlib.Path, required=True)
    a = p.parse_args()
    result = attribution(
        json.loads(a.os_result.read_text()), json.loads(a.firmware_result.read_text())
    )
    a.out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
