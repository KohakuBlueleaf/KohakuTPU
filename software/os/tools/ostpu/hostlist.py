"""Run captures through the selected firmware/HOSTL runtime on a model or JTAG card."""

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

from relocate import relocate

from kohakutpu.application.runtime import card as runtime
from kohakutpu.application.runtime import osabi as A


def run_firmware_capture(
    capture,
    *,
    model=None,
    jtag=False,
    board="vlt_v9t6s_1n",
    profile="mid",
    lists=0,
    steps=2,
    fw=None,
    launch_ring=False,
    poison_reads=(),
    out_dir=None,
):
    if lists not in (0, 1, "0", "1"):
        raise ValueError(f"lists must be 0 (FW-RUN) or 1 (HOSTL-LIST), got {lists!r}")
    use_lists = bool(int(lists))
    started = time.monotonic()
    card = (
        runtime.SiliconCard(board, nodes=(0,), profile=profile, fw=fw, timeout=440)
        if jtag
        else runtime.VerilatorCard(
            model,
            board=board,
            log=(pathlib.Path(out_dir) / "fw_model.log") if out_dir else None,
            timeout=440,
            fw=fw,
        )
    )
    try:
        if use_lists and not hasattr(card, "prepare_execution"):
            raise RuntimeError(
                "HOSTL-LIST requested (--lists 1) but active runtime lacks prepare_execution"
            )
        result = dict(
            workload=capture["workload"],
            backend="HOSTL-LIST" if use_lists else "FW-RUN",
            runtime=str(pathlib.Path(runtime.__file__).resolve()),
            knobs={k: v for k, v in os.environ.items() if k.startswith("KOHAKUTPU_")},
            results=[],
        )
        base = card.alloc(capture["extent"])
        if jtag:
            card.write(base, bytes(capture["extent"]))
        init = relocate(capture["init"], capture["base"], base, capture["extent"])
        events = relocate(capture["step"], capture["base"], base, capture["extent"])
        for ev in init:
            if ev[0] == "write":
                card.write(ev[1], ev[2])
            elif ev[0] == "run":
                prep = card.prepare_recorded([ev], 32 << 20)
                for pev in prep:
                    card.collect(card.submit_prepared(pev[1]))
                prep.close()
            else:
                raise ValueError(f"unsupported init event: {ev[0]}")
        if launch_ring:
            card.queues[0].size = A.KA_LRING_QUEUE_OFF
            card.mem.write64(
                card.queues[0].base
                + A.KA_LRING_QUEUE_OFF
                + A.KA_LRING_SLOTS * A.KA_LRING_REC_BYTES,
                0,
            )
        prepared = card.prepare_recorded(events, 32 << 20)
        execution, groups = (
            card.prepare_execution(prepared) if use_lists else (prepared, [])
        )
        result.update(
            launches=sum(ev[0] == "run" for ev in events),
            submissions_per_step=sum(ev[0] != "write" for ev in execution),
            groups=groups,
        )
        for epoch in range(steps):
            for ev in relocate(
                capture.get("epoch_inputs", [[]] * steps)[epoch],
                capture["base"],
                base,
                capture["extent"],
            ):
                card.write(ev[1], ev[2])
            for index in poison_reads:
                pa, size = capture["reads"][index]
                card.write(base + pa - capture["base"], b"\xa5" * size)
            phases = []
            for ev in execution:
                if ev[0] == "write":
                    card.write(ev[1], ev[2])
                else:
                    submit = (
                        card.submit_execution
                        if ev[0] == "list"
                        else card.submit_prepared
                    )
                    r = card.collect(submit(ev[1]))
                    phases.append(dict(cycles=r["cycles"], phases=r["phases"]))
            outputs = [
                card.get(base + pa - capture["base"], n).hex()
                for pa, n in capture["reads"]
            ]
            lring = []
            if launch_ring:
                qbase = card.queues[0].base + A.KA_LRING_QUEUE_OFF
                cnt = card.mem.read64(qbase + A.KA_LRING_SLOTS * A.KA_LRING_REC_BYTES)
                n_rec = min(cnt, A.KA_LRING_SLOTS)
                raw_ring = card.mem.read_block(qbase, n_rec * A.KA_LRING_REC_BYTES)
                for k in range(n_rec):
                    w = struct.unpack_from(
                        A.LAUNCH_REC_FMT, raw_ring, k * A.KA_LRING_REC_BYTES
                    )
                    lring.append(dict(zip(A.LAUNCH_REC_FIELDS, w, strict=True)))
            result["results"].append(
                dict(
                    cycles=sum(r["cycles"] for r in phases),
                    packages=phases,
                    outputs=outputs,
                    launch_ring=lring,
                )
            )
        prepared.close()
        result["run_seconds"] = time.monotonic() - started
        return result
    finally:
        card.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("capture", type=pathlib.Path)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--model")
    src.add_argument("--jtag", action="store_true")
    p.add_argument("--board", default="vlt_v9t6s_1n")
    p.add_argument("--profile", default="mid")
    p.add_argument("--lists", type=int, choices=(0, 1), required=True)
    p.add_argument("--steps", type=int, default=2)
    p.add_argument("--fw")
    p.add_argument("--launch-ring", action="store_true")
    p.add_argument("--out", required=True)
    p.add_argument("--poison-reads", type=int, nargs="*", default=[])
    a = p.parse_args()
    with gzip.open(a.capture, "rb") as f:
        capture = pickle.load(f)
    out = pathlib.Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    result = run_firmware_capture(
        capture,
        model=a.model,
        jtag=a.jtag,
        board=a.board,
        profile=a.profile,
        lists=a.lists,
        steps=a.steps,
        fw=a.fw,
        launch_ring=a.launch_ring,
        poison_reads=a.poison_reads,
        out_dir=out,
    )
    (out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {**result, "results": [{"cycles": r["cycles"]} for r in result["results"]]},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
