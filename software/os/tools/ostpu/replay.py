"""Execute or compare a workload capture on KohakuAccel OS and firmware/HOSTL-LIST."""

import argparse
import gzip
import json
import os
import pathlib
import pickle
import struct
import sys
import time

TOOLS = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(TOOLS / "ostpu"))

import klog
from attribution import step_ring_summary
from boot import RG_DRAM_PRIV, RG_DRAM_SHARED, load, regions, tables
from hostlist import run_firmware_capture
from image import UIMG, segments
from relocate import relocate
from run_card import Card, arrays, load_node

from kohakuaccel.driver.device import rv64load
from kohakutpu.application.runtime import osabi as A
from kohakutpu.application.runtime.device import Device, Recorded
from kohakutpu.application.runtime.osgraph import (
    KDS_LZ_USER,
    KDS_STAMP,
    KOS_LAND_SIZE,
    KOS_LRING_MAX_LAUNCHES,
    KOS_LRING_OFF,
    KOS_LRING_REC_BYTES,
    KOS_LRING_STEP_SLOT,
    LAUNCH_REC_FIELDS,
    LAUNCH_REC_FMT,
    TASK_BYTES,
    Graph,
    ResidentGraph,
)


def decode_step_ring(raw_rec):
    w = struct.unpack(LAUNCH_REC_FMT, raw_rec)
    return dict(
        step_total=w[0],
        client_cache_acq=w[1],
        client_send=w[2],
        server_wake_recv=w[3],
        check_or_reuse=w[4],
        cache_release=w[5],
        de_run=w[6],
        cache_acquire=w[7],
        server_reply=w[8],
        kds_run_cycles=w[9],
        cyc_msgs=w[10],
        retained_hit=w[11],
    )


def decode_launch_ring(raw_ring, count):
    out = []
    for k in range(count):
        w = struct.unpack_from(LAUNCH_REC_FMT, raw_ring, k * KOS_LRING_REC_BYTES)
        out.append(dict(zip(LAUNCH_REC_FIELDS, w, strict=True)))
    return out


def run_os_capture(capture, a, board, out):
    started = time.monotonic()
    board = dict(board)
    board["dram"] = dict(board["dram"], private=a.private_bytes)
    if a.profile == "mid":
        board["clock_hz"] = 200_000_000
    card_board = (
        "vlt_v9t6_2n"
        if a.model and board["card"]["board"] == "multimesh_v9t6"
        else board["card"]["board"]
    )
    units = {(u["mesh"], u["x"], u["y"]): i for i, u in enumerate(board["units"])}
    types = {
        (u["mesh"], u["x"], u["y"]): int.from_bytes(u["type"].encode(), "big")
        for u in board["units"]
    }
    de_stream_on = os.environ.get("KOHAKUTPU_OS_DE_STREAM", "0") == "1"
    de_fetch_on = os.environ.get("KOHAKUTPU_OS_DE_FETCH", "1") == "1"
    lring_on = os.environ.get("KOHAKUTPU_OS_LAUNCH_RING", "0") == "1"
    stamps_on = os.environ.get("KOHAKUTPU_OS_STAMPS", "0") == "1"
    de_fuse_on = (
        de_stream_on
        and os.environ.get("KOHAKUTPU_OS_DE_FUSE", "0") == "1"
        and not lring_on
        and not stamps_on
    )
    for event in capture["step"]:
        if event[0] in ("program", "run"):
            for item in event[1].values():
                raw = item[0] if event[0] == "run" else item.bound(False)
                Graph(
                    batch=8,
                    compact=True,
                    movers=True,
                    de_stream=de_stream_on,
                    de_fetch=de_fetch_on,
                ).package(raw, [], units, unit_types=types)
    result = dict(
        workload=capture["workload"],
        backend="os",
        board=a.board,
        shared_bytes=int(str(board["dram"].get("shared", 0)), 0),
        results=[],
    )

    def progress(phase):
        elapsed = time.monotonic() - started
        print(phase, elapsed, flush=True)
        (out / "progress.json").write_text(
            json.dumps(dict(phase=phase, seconds=elapsed)) + "\n"
        )

    image = pathlib.Path(a.image)
    parts = segments(image)
    card = Card(board, a.model, out / "model.log")
    try:
        priv, size = regions(board, 0)[0][:2]
        if a.jtag:
            t_paint0 = time.monotonic()
            rv64load.LoadWindow(
                card.t, card.m["ctrl"][0] + rv64load.WINDOW_OFFSET
            ).stop()
            for pa, count, kind, _owner in regions(board, 0):
                if kind in (RG_DRAM_PRIV, RG_DRAM_SHARED):
                    card.write(pa, bytes(count))
            result["paint_seconds"] = time.monotonic() - t_paint0

        def estimate_bytes(ev_list):
            writes_b = sum(
                len(ev[2]) + TASK_BYTES for ev in ev_list if ev[0] == "write"
            )
            g = Graph(
                batch=a.segment,
                compact=True,
                movers=True,
                prune=os.environ.get("KOHAKUTPU_OS_PRUNE", "0") == "1",
                de_stream=de_stream_on,
                de_fetch=de_fetch_on,
                de_fuse=de_fuse_on,
            )
            fr = ()
            if lring_on:
                fr = g.launch_mark(fr, (1 << 64) - 1)
            for idx, event in enumerate(ev_list):
                if event[0] in ("program", "run"):
                    for item in event[1].values():
                        raw = item[0] if event[0] == "run" else item.bound(False)
                        fr = g.package(raw, [], units, after=fr, unit_types=types)
                    if lring_on:
                        fr = g.launch_mark(fr, idx)
            return len(g.pack()) + writes_b

        init_run_evs = [ev for ev in capture["init"] if ev[0] in ("program", "run")]
        step_bytes = max(
            estimate_bytes(capture["step"]),
            estimate_bytes(init_run_evs) if init_run_evs else 0,
        )
        capacity = (step_bytes + 8192 + TASK_BYTES - 1) // TASK_BYTES
        command = f"ostpu {capacity} {capture['extent']} {a.retain} -e"
        t_load0 = time.monotonic()
        imem, spad, dram = arrays(image, tables(board, 0, 0, command)[0])
        for pa, data in parts:
            if pa >= UIMG:
                card.write(priv + pa - UIMG, data)
        for pa, data in dram:
            card.write(pa, data)
        window = load_node(card, 0, 0, imem, spad, "burn" if a.model else "axi")
        result["load_seconds"] = time.monotonic() - t_load0
        window.boot()
        progress("booted; waiting for resident allocation")
        mailbox_pa = priv + size - KOS_LAND_SIZE + KDS_LZ_USER
        session = ResidentGraph(card, mailbox_pa, timeout=180)
        progress("arena ready")
        dev = Device("os", board=card_board, os_session=session, os_units=units)
        dev.card.segment, dev.card.compact, dev.card.movers = a.segment, True, True
        base = session.data_pa
        init = relocate(capture["init"], capture["base"], base, capture["extent"])
        events = relocate(capture["step"], capture["base"], base, capture["extent"])
        init_runs = []
        for ev in init:
            if ev[0] == "write":
                card.write(ev[1], ev[2])
            elif ev[0] == "run":
                init_runs.append(ev)
            else:
                raise ValueError(f"unsupported init event: {ev[0]}")
        if init_runs:
            saved_ring = dev.card.launch_ring
            dev.card.launch_ring = False
            init_log = Recorded(init_runs, [], dev)
            dev.prepare_recorded(init_log)
            dev.card.replay(init_log, repeats=1)
            init_log.close()
            dev.card.launch_ring = saved_ring
            dev.card.submissions = 0
            dev.card.runs = 0
        progress("bound and initialized")
        log = Recorded(events, [], dev)
        dev.prepare_recorded(log)
        result.update(
            tasks=len(log.prepared.graph.tasks),
            wire_bytes=len(log.prepared.graph.pack()),
            launches=log.prepared.launches,
            submissions_per_step=1,
        )
        (out / "prepared.json").write_text(
            json.dumps(
                {**result, "segment": a.segment, "prune": dev.card.prune}, indent=2
            )
            + "\n"
        )
        (out / "tasks.json").write_text(
            json.dumps(
                [
                    dict(id=i, kind=t.kind, words=t.words, pred=t.pred)
                    for i, t in enumerate(log.prepared.graph.tasks)
                ]
            )
            + "\n"
        )
        progress("graph published")
        session.timeout = 300
        for epoch in range(a.steps):
            for ev in relocate(
                capture.get("epoch_inputs", [[]] * a.steps)[epoch],
                capture["base"],
                base,
                capture["extent"],
            ):
                card.write(ev[1], ev[2])
            for index in a.poison_reads:
                pa, sz = capture["reads"][index]
                card.write(base + pa - capture["base"], b"\xa5" * sz)
            try:
                dev.card.replay(log, repeats=a.batch_repeats)
            except (TimeoutError, RuntimeError) as exc:
                failure = dict(
                    error=str(exc),
                    seconds=time.monotonic() - started,
                    status=window.status(),
                    console=window.console(),
                    halt_pc=card.t.read64(window.base + rv64load.R_HALTPC),
                    log=klog.parse(card.read(priv + klog.offset(size), 16384)),
                    mailbox=card.read(session.mailbox, 288).hex(),
                    trace=struct.unpack(
                        f"<{A.OSTPU_TRACE_WORDS}Q",
                        card.read(
                            session.mailbox + A.OSTPU_OFF_TRACE,
                            8 * A.OSTPU_TRACE_WORDS,
                        ),
                    ),
                    model_status=(
                        {k: v for k, v in card.t.status().items() if isinstance(v, int)}
                        if card.sim
                        else {}
                    ),
                )
                (out / "failure.json").write_text(json.dumps(failure, indent=2) + "\n")
                raise
            progress("graph completed")
            outputs = [
                card.read(base + pa - capture["base"], count).hex()
                for pa, count in capture["reads"]
            ]
            stamps = []
            graph = log.prepared.graph
            packed = graph.pack()
            for i, task in enumerate(graph.tasks):
                if task.kind == KDS_STAMP:
                    offset = struct.unpack_from(
                        "<Q", packed, i * TASK_BYTES + A.KDS_TASK_OFF_WORDS
                    )[0]
                    stamps.append(
                        struct.unpack("<4Q", card.read(session.tasks_pa + offset, 32))
                    )
            profile = (
                struct.unpack(
                    f"<{A.OSTPU_PROF_WORDS}Q",
                    card.read(
                        session.mailbox + A.OSTPU_OFF_PROF,
                        8 * A.OSTPU_PROF_WORDS,
                    ),
                )
                if a.profile_sys
                else None
            )
            lring = []
            if dev.card.launch_ring:
                n_lring = min(log.prepared.launches, KOS_LRING_MAX_LAUNCHES)
                raw_ring = card.read(
                    session.mailbox + KOS_LRING_OFF, n_lring * KOS_LRING_REC_BYTES
                )
                lring = decode_launch_ring(raw_ring, n_lring)
            step_ring = None
            if dev.card.launch_ring or a.step_ring:
                raw_step = card.read(
                    session.mailbox
                    + KOS_LRING_OFF
                    + KOS_LRING_STEP_SLOT * KOS_LRING_REC_BYTES,
                    KOS_LRING_REC_BYTES,
                )
                step_ring = step_ring_summary(decode_step_ring(raw_step))
            result["results"].append(
                dict(
                    dev.card.last_result,
                    outputs=outputs,
                    stamps=stamps,
                    syscall_profile=profile,
                    launch_ring=lring,
                    step_ring=step_ring,
                )
            )
        log.close()
        dev.close()
        window.stop()
    finally:
        if card.sim:
            card.t.close()
    result["retain"] = a.retain
    result["batch_repeats"] = a.batch_repeats
    result["run_seconds"] = time.monotonic() - started
    return result


def run_fw_leg(capture, a, board, out):
    if not a.fw:
        raise ValueError("--fw is required for firmware and compare runs")
    fw_board = a.fw_board or board["card"]["board"]
    if a.model and fw_board == "multimesh_v9t6":
        raise ValueError(
            "board card.board='multimesh_v9t6' is a JTAG silicon board; pass --board card_v9t6_1n or explicit --fw-board vlt_v9t6_2n for model runs"
        )
    return run_firmware_capture(
        capture,
        model=a.model,
        jtag=a.jtag,
        board=fw_board,
        profile=a.profile,
        lists=int(a.lists),
        steps=a.steps * a.batch_repeats,
        fw=a.fw,
        poison_reads=a.poison_reads,
        out_dir=out,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=pathlib.Path)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--model")
    src.add_argument("--jtag", action="store_true")
    parser.add_argument(
        "--backend", choices=("os", "firmware", "compare"), default="os"
    )
    parser.add_argument("--board", default="card_v9t6s_1n")
    parser.add_argument("--fw-board")
    parser.add_argument("--profile", default="mid")
    parser.add_argument("--image")
    parser.add_argument("--fw")
    parser.add_argument("--lists", type=int, choices=(0, 1), default=0)
    parser.add_argument("--out", required=True)
    parser.add_argument("--steps", type=int, default=1)
    parser.add_argument("--poison-reads", type=int, nargs="*", default=[])
    parser.add_argument("--segment", type=int, default=8)
    parser.add_argument("--retain", type=int, choices=(0, 1), default=0)
    parser.add_argument("--profile-sys", action="store_true")
    parser.add_argument("--step-ring", action="store_true")
    parser.add_argument("--batch-repeats", type=int, default=1)
    parser.add_argument("--private-bytes", type=lambda s: int(s, 0), default=1 << 20)
    a = parser.parse_args()
    with gzip.open(a.capture, "rb") as f:
        capture = pickle.load(f)
    out = pathlib.Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    board = load(a.board)
    if a.backend == "os":
        result = run_os_capture(capture, a, board, out)
    elif a.backend == "firmware":
        result = run_fw_leg(capture, a, board, out)
        result["retain"] = a.retain
        result["batch_repeats"] = a.batch_repeats
    else:
        a.steps = max(2, a.steps)
        a.retain = 1
        os_res = run_os_capture(capture, a, board, out)
        fw_res = run_fw_leg(capture, a, board, out)
        bit_exact = [r["outputs"] for r in os_res["results"]] == [
            r["outputs"] for r in fw_res["results"]
        ]
        assert bit_exact, "OS and firmware/HOSTL outputs differ"
        os_cold = os_res["results"][0]["cycles"]
        os_ret = os_res["results"][1]["cycles"]
        fw_s0 = fw_res["results"][0]["cycles"]
        fw_s1 = fw_res["results"][1]["cycles"]
        result = dict(
            workload=capture["workload"],
            backend="compare",
            board=a.board,
            card_board=board["card"]["board"],
            shared_bytes=os_res["shared_bytes"],
            mode="jtag" if a.jtag else "model",
            profile=a.profile,
            bit_exact=bit_exact,
            launches=os_res["launches"],
            os=dict(
                cold_cycles=os_cold,
                retained_cycles=os_ret,
                submissions_per_step=os_res["submissions_per_step"],
                tasks=os_res["tasks"],
                wire_bytes=os_res["wire_bytes"],
                load_seconds=os_res.get("load_seconds"),
                paint_seconds=os_res.get("paint_seconds"),
                run_seconds=os_res["run_seconds"],
                results=os_res["results"],
            ),
            firmware=dict(
                backend=fw_res["backend"],
                fw_image=str(a.fw),
                step0_cycles=fw_s0,
                step1_cycles=fw_s1,
                submissions_per_step=fw_res["submissions_per_step"],
                run_seconds=fw_res["run_seconds"],
                results=fw_res["results"],
            ),
            comparison=dict(
                speedup_cold_vs_fw=fw_s0 / os_cold,
                speedup_retained_vs_fw=fw_s1 / os_ret,
                ratio_cold_over_fw=os_cold / fw_s0,
                ratio_retained_over_fw=os_ret / fw_s1,
                os_submissions_per_step=os_res["submissions_per_step"],
                fw_submissions_per_step=fw_res["submissions_per_step"],
            ),
        )
    (out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    if a.backend == "compare":
        print(
            json.dumps(
                {k: v for k, v in result.items() if k not in ("os", "firmware")}
                | {
                    "os": {k: v for k, v in result["os"].items() if k != "results"},
                    "firmware": {
                        k: v for k, v in result["firmware"].items() if k != "results"
                    },
                },
                indent=2,
            )
        )
    else:
        print(
            json.dumps(
                {
                    **result,
                    "results": [
                        {k: v for k, v in r.items() if k not in ("outputs", "packages")}
                        for r in result["results"]
                    ],
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
