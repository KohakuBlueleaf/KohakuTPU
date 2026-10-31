"""Host prebuild and bounded immutable v9t6s graph execution; no local compile."""

import argparse
import json
import os
import pickle
import time
from pathlib import Path
from unittest.mock import patch

import numpy as np
from examples.tinygrad.vit_tiny import Block
from tinygrad import Tensor
from tinygrad.helpers import Context
from tinygrad.nn.state import get_state_dict

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import matmul as MM
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.ktpugrad.nn import place
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import generated
from kohakutpu.application.runtime.card import VerilatorCard
from kohakutpu.application.runtime.graph.firmware import FirmwareRunner
from kohakutpu.application.runtime.graph.native import (
    NativeLaunch,
    fuse,
    local_scratch,
    port_bytes,
)
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs
from kohakutpu.stack.buffer import Buffer
from kohakutpu.stack.kernel import arena_name

ROOT = D.ROOT


def bind_physical(dev, compiled, bindings):
    full = dict(bindings)
    scratch = []
    for arena in compiled.scratch or ():
        at = (
            dev.memory.stage(arena["bytes"], arena["node"] or 0)
            if arena["space"] == "stage"
            else dev.memory.alloc(arena["bytes"])
        )
        scratch.append(at)
        full[arena_name(arena)] = at
        for name, offset in arena["members"]:
            full[name] = at + offset
    for name, ty in compiled.l1.params(compiled.name, "l1"):
        if name not in full:
            spec = Buffer.of(ty, dev.machine.machine_json)
            at = (
                dev.memory.stage(spec.nbytes, spec.layout["node"] or 0)
                if spec.space == "stage"
                else dev.memory.alloc(spec.nbytes)
            )
            scratch.append(at)
            full[name] = at
    programs = compiled.programs(full)
    raw = next(iter(programs.values())).bound(True)
    high = {
        "dram": max(
            (a + n for a, n in dev.memory.dram.live.items()), default=dev.memory.dram.lo
        ),
        "stage": max(
            (a + n for a, n in dev.memory.staging[0].live.items()),
            default=dev.memory.staging[0].lo,
        ),
    }
    for at in scratch:
        dev.memory.free(at)
    return raw, high


def native(dev, kernel, given, binds, kind):
    trace = kernel.trace(given, machine=K.dsl_machine(dev.machine))
    return NativeLaunch(trace.graph, binds, kind)


def pilot(dev):
    rows, width, columns = 8, 64, 32
    mm = MM.plan(1, rows, columns, width, machine_json=dev.machine.machine_json)
    x = dev.alloc(rows * width * 2)
    q = dev.alloc(mm.mp * mm.kp)
    w = dev.alloc(mm.n * mm.kp)
    y = dev.alloc(mm.mp * columns * 2)
    z = dev.alloc(mm.mp * columns * 2)
    data = (np.arange(rows * width).reshape(rows, width) % 9 - 4).astype(
        np.float16
    ) / 16
    dev.write(x, data.tobytes())
    spec = Buffer.of(
        f"mx7b[{mm.n}, {mm.kp}] @dram(gn={mm.tn // 4}, nk={mm.nk})",
        dev.machine.machine_json,
    )
    dev.write(w, spec.pack(np.ones((mm.n, mm.kp), np.float16) / 16))
    quant, qgiven = MM.quant_kernel(
        V.Operand("full", pitch=width),
        rows,
        width,
        mm.mp,
        mm.kp,
        "a",
        mm.tm // 4,
        mm.nk,
        finite=True,
    )
    product, mgiven = MM.mm_kernel(
        mm, X.Load(0), (), V.Operand("full", pitch=columns), 1, 1
    )
    epilogue, egiven, _ = V.rows_kernel(
        X.op("max", X.Load(0), 0.0),
        (V.Operand("full", pitch=columns),),
        rows,
        columns,
        V.Operand("full", pitch=columns),
        1,
    )
    launches = [
        native(dev, quant, qgiven, {"x": x, "q": q}, "quant"),
        native(dev, product, mgiven, {"a": q, "b": w, "out": y}, "matmul"),
        native(dev, epilogue, egiven, {"in0": y, "out": z}, "dsl"),
    ]
    for launch in launches:
        launch.bindings = {k: dev.mmu.resolve(v)[1] for k, v in launch.bindings.items()}
    return launches, (dev.mmu.resolve(z)[1], rows * columns * 2), [], rows, width, "<f2"


def block(dev, tokens, width, batch=1):
    rng = np.random.default_rng(71)
    with Context(DEV="CPU"):
        model = Block(width)
        for name, tensor in get_state_dict(model).items():
            value = (
                np.ones(tensor.shape)
                if "norm" in name and name.endswith("weight")
                else rng.standard_normal(tensor.shape) * 0.02
            )
            tensor.replace(Tensor(value.astype(np.float16), device="CPU"))
    place(model)
    x = Tensor(
        rng.standard_normal((batch, tokens, width)).astype(np.float16), device="KTPU"
    ).realize()
    dev.record()
    x.numpy()  # Input range: marks values that vary between warm forward steps.
    y = model(x).realize()
    output_dtype = y.numpy().dtype.str
    dev.drain()
    log = dev.recorded()
    launches = []
    for i, ((compiled, bindings), metadata) in enumerate(
        zip(log.templates, log.runs, strict=True)
    ):
        ports = {p["name"] for p in compiled.traced.graph["ports"]}
        physical = {
            name: dev.mmu.resolve(at)[1] for name, at in bindings if name in ports
        }
        launches.append(
            NativeLaunch(compiled.traced.graph, physical, metadata[1], (i,))
        )
    reads = [event for event in log if event[0] == "read"]
    output = reads[-1][1:]
    # A host write is hoistable only if no preceding kernel accessed its range.
    prior = []
    runs = iter(launches)
    for event in log:
        if event[0] == "run":
            launch = next(runs)
            for port in launch.graph["ports"]:
                at = launch.bindings[port["name"]]
                prior.append((at, at + port_bytes(port, dev.machine.machine_json)))
        elif event[0] == "write":
            at, data = event[1:]
            assert not any(
                at < hi and lo < at + len(data) for lo, hi in prior
            ), "host effect needs a graph boundary"
    return launches, output, [log, model, x, y], tokens, width, output_dtype


def compile_artifact(args):
    started = time.perf_counter()
    dev = D.Device("offline", board="vlt_v9t6s_1n", classes=False)
    dev.machine = machine_of("vlt_v9t6s_1n", packing_backend="vector_mx7")
    machine = dev.machine.machine_json
    with D.attached(dev):
        launches, output, held, tokens, width, output_dtype = (
            pilot(dev)
            if args.workload == "pilot"
            else block(dev, args.tokens, args.width, args.batch)
        )
        # Original eager private scratch is not part of the exported launch graph.
        # Keep port allocations pinned; release only disjoint, already-freed scratch.
        if held:
            spans = [
                (k.bindings[p["name"]], k.bindings[p["name"]] + port_bytes(p, machine))
                for k in launches
                for p in k.graph["ports"]
            ]
            kept = []
            for arena, at in held[0].references:
                end = at + arena.live[at]
                if at in arena.released and not any(
                    at < hi and lo < end for lo, hi in spans
                ):
                    arena.unretain(at)
                else:
                    kept.append((arena, at))
            held[0].references = kept
        variants = {}
        initialization = []
        high = {"dram": dev.memory.dram.lo, "stage": dev.memory.staging[0].lo}
        if args.warm:
            read = next(event for event in held[0] if event[0] == "read")
            dynamic = [(read[1], read[1] + read[2])]
            invariant, active = [], []
            for launch in launches:
                inputs, outputs = [], []
                for port in launch.graph["ports"]:
                    at = launch.bindings[port["name"]]
                    (outputs if port["role"] == "out" else inputs).append(
                        (at, at + port_bytes(port, machine))
                    )
                if any(a < hi and lo < b for a, b in inputs for lo, hi in dynamic):
                    active.append(launch)
                    dynamic.extend(outputs)
                else:
                    invariant.append(launch)
            for launch in invariant:
                source = json.dumps(launch.graph)
                result = rs._rs.library_get(str(args.library), source, machine)
                if result is None:
                    rs._rs.library_prebuild(str(args.library), source, machine)
                    result = rs._rs.library_get(str(args.library), source, machine)
                compiled = generated.from_output(dev, launch.graph, result)
                packet, top = bind_physical(dev, compiled, launch.bindings)
                initialization.append(packet)
                high = {k: max(high[k], top[k]) for k in high}
            launches = active
        for mode, quant, elementwise in (
            ("off", False, False),
            ("quant", True, False),
            ("elementwise", False, True),
            ("both", True, True),
            ("recursive", True, True),
            ("local", True, True),
            ("norm", False, False),
            ("layout", False, False),
            ("all", True, True),
        ):
            if mode not in args.compile_modes.split(","):
                continue
            selected, rules = fuse(
                launches,
                [output],
                machine,
                quant=quant,
                elementwise=elementwise,
                stage_bytes=dev.memory.staging[0].free,
                recursive=mode in ("recursive", "local", "norm", "layout", "all"),
                producer_quant=mode in ("recursive", "local", "all"),
                norm=mode in ("norm", "all"),
                layout=mode in ("layout", "all"),
            )
            if mode in ("local", "all"):
                selected = [
                    local_scratch(k, machine, dev.memory.staging[0].free)
                    for k in selected
                ]
            packets = []
            builds, hits = 0, 0
            build_started = time.perf_counter()
            for index, launch in enumerate(selected):
                source = json.dumps(launch.graph)
                result = rs._rs.library_get(str(args.library), source, machine)
                if result is None:
                    builds += 1
                    rs._rs.library_prebuild(str(args.library), source, machine)
                    result = rs._rs.library_get(str(args.library), source, machine)
                else:
                    hits += 1
                assert not result["verdict"]["refused"], result["verdict"]
                compiled = generated.from_output(dev, launch.graph, result)
                packet, top = bind_physical(dev, compiled, launch.bindings)
                packets.append(packet)
                high = {k: max(high[k], top[k]) for k in high}

            variants[mode] = dict(
                packets=packets,
                builds=builds,
                hits=hits,
                rules=rules,
                build_seconds=time.perf_counter() - build_started,
                kinds=[k.kind for k in selected],
                members=[k.members for k in selected],
            )
        initial = [
            (i * (1 << 20), bytes(page)) for i, page in dev.card.mem.pages.items()
        ]
        high["dram"] = -(-high["dram"] // (1 << 20)) * (1 << 20)
        artifact = dict(
            machine=machine,
            ring_depth=int(os.environ.get("KTPU_MM_EPILOGUE_RING", "0")),
            peak_flops=2
            * json.loads(machine)["machine"]["mg"]["count"]
            * json.loads(machine)["machine"]["mg"]["macs_per_cycle"],
            variants=variants,
            initialization=initialization,
            output=output,
            output_dtype=output_dtype,
            initial=initial,
            high=high,
            tokens=tokens,
            batch=args.batch,
            width=width,
            workload=args.workload,
            build_seconds=time.perf_counter() - started,
        )
        args.artifact.parent.mkdir(parents=True, exist_ok=True)
        args.artifact.write_bytes(pickle.dumps(artifact))
        print(
            json.dumps(
                {
                    "workload": args.workload,
                    "tokens": tokens,
                    "width": width,
                    "launches": {k: len(v["packets"]) for k, v in variants.items()},
                    "rules": {k: v["rules"] for k, v in variants.items()},
                    "high": high,
                    "build_seconds": artifact["build_seconds"],
                }
            ),
            flush=True,
        )
        for item in held[:1]:
            item.close()


def forbidden(*args, **kwargs):
    raise AssertionError("model execution must not compile")


def execute(args):
    artifact = pickle.loads(args.artifact.read_bytes())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    start = time.perf_counter()
    card = VerilatorCard(
        ROOT / "build/v9t6s/vlt_card_v9t6s_1n",
        board="vlt_v9t6s_1n",
        log=args.output.with_suffix(".log"),
        fw=args.firmware,
        alarm=550,
    )
    records = {}
    try:
        card.memory.alloc(
            artifact["high"]["dram"] - card.memory.dram.lo, owner="captured-graph"
        )
        stage_start = card.memory.staging[0].free_list[0][0]
        if artifact["high"]["stage"] > stage_start:
            card.memory.stage(
                artifact["high"]["stage"] - stage_start,
                0,
                owner="captured-graph",
            )
        reference = (
            bytes.fromhex(
                json.loads(args.reference.read_text())["records"]["off"]["output_hex"]
            )
            if args.reference is not None
            else None
        )
        with (
            patch.object(rs._rs, "compile", forbidden),
            patch.object(rs._rs, "library_prebuild", forbidden),
        ):
            for mode in args.modes.split(","):
                variant = artifact["variants"][mode]
                for at, data in artifact["initial"]:
                    card.write(at, data)
                at, nbytes = artifact["output"]
                card.write(at, bytes([0xA5]) * nbytes)
                q = card.queues[0]
                for raw in artifact.get("initialization", ()):
                    init = q.prepare(raw)
                    done = q.wait(q.submit(init))
                    assert done.ok, done.describe()
                    init.close()
                if args.initialization_only:
                    records[mode] = dict(
                        initialization_launches=len(artifact["initialization"])
                    )
                    continue
                packages = [q.prepare(raw) for raw in variant["packets"]]
                prepared = (
                    FirmwareRunner(q).prepare(variant["packets"], ())
                    if args.execution_list
                    else None
                )
                before = q.counters["submitted"]
                mode_start = time.perf_counter()
                timings = []
                if prepared is not None:
                    done = prepared.submit()
                    assert done.ok, done.describe()
                    timings.append(dict(done.record, cycles=done.cycles))
                else:
                    for p in packages:
                        done = q.wait(q.submit(p))
                        assert done.ok, done.describe()
                        timings.append(dict(done.record, cycles=done.cycles))
                submit_wait_seconds = time.perf_counter() - mode_start
                actual = card.get(at, nbytes)
                if reference is None:
                    assert mode == "off", "a fused-only run requires --reference"
                    reference = actual
                assert actual == reference, (
                    mode,
                    "native fused/unfused output mismatch",
                )
                values = np.frombuffer(actual, np.dtype(artifact["output_dtype"]))
                assert np.isfinite(values).all() and np.any(values != 0)
                cycles = sum(r["cycles"] for r in timings)
                flops = (
                    artifact.get("batch", 1)
                    * (
                        24 * artifact["tokens"] * artifact["width"] ** 2
                        + 4 * artifact["tokens"] ** 2 * artifact["width"]
                    )
                    if artifact["workload"] == "block"
                    else None
                )
                records[mode] = dict(
                    launches=len(packages),
                    host_submit_wait_seconds=submit_wait_seconds,
                    cycles=cycles,
                    submissions=q.counters["submitted"] - before,
                    rules=variant["rules"],
                    exact_equal=True,
                    output_hex=actual.hex(),
                    phases=timings,
                    matrix_mfu=(
                        None
                        if flops is None
                        else flops / (artifact["peak_flops"] * cycles)
                    ),
                )
                print(
                    json.dumps(
                        {
                            k: v
                            for k, v in records[mode].items()
                            if k not in ("output_hex", "phases")
                        }
                    ),
                    flush=True,
                )
                if prepared is not None:
                    prepared.close()
                for p in packages:
                    p.close()
                args.output.write_text(
                    json.dumps(
                        dict(records=records, wall_seconds=time.perf_counter() - start),
                        indent=2,
                    )
                )
    finally:
        card.close()
    args.output.write_text(
        json.dumps(
            dict(records=records, wall_seconds=time.perf_counter() - start), indent=2
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("compile", "execute"))
    parser.add_argument("--workload", choices=("pilot", "block"), default="pilot")
    parser.add_argument("--tokens", type=int, default=197)
    parser.add_argument("--batch", type=int, default=1)
    parser.add_argument("--warm", action="store_true")
    parser.add_argument("--width", type=int, default=192)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument(
        "--library", type=Path, default=Path("../.plan/gfuse/native-library")
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--firmware", type=Path)
    parser.add_argument("--execution-list", action="store_true")
    parser.add_argument("--initialization-only", action="store_true")
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--modes", default="off,quant,elementwise,both")
    parser.add_argument(
        "--compile-modes",
        default="off,quant,elementwise,both,recursive,local,norm,layout,all",
    )
    args = parser.parse_args()
    if args.warm and args.workload != "block":
        parser.error("--warm requires --workload block")
    if (
        args.mode == "execute"
        and not args.initialization_only
        and args.reference is None
        and args.modes.split(",")[0] != "off"
    ):
        parser.error("fused-only execution requires --reference or an initial off mode")
    (compile_artifact if args.mode == "compile" else execute)(args)


if __name__ == "__main__":
    main()
