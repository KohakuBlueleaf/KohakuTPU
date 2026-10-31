"""One native composed class, two admitted shapes, two relocated address sets."""

import argparse
import json
import pickle
import time
from pathlib import Path
from unittest.mock import patch

import numpy as np

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.kernels.classes.policy import ShapeClass
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime.card import VerilatorCard
from kohakutpu.application.runtime.graph.native import NativeLaunch, admit_pair, compose
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs


def sources_for(dev):
    sources = []
    for rows in (16, 32):
        stages = []
        for expression in (X.op("add", X.Load(0), 0.125), X.op("max", X.Load(0), 0)):
            kernel, given, _ = V.rows_kernel(
                expression,
                (V.Operand("full", pitch=128),),
                rows,
                128,
                V.Operand("full", pitch=128),
                1,
                shape_class=ShapeClass(32, 256),
            )
            graph = kernel.trace(given, machine=K.dsl_machine(dev.machine)).graph
            stages.append(NativeLaunch(graph, {}, "dsl"))
        sources.append(json.dumps(compose(stages[0], stages[1], "out", ("in0",)).graph))
    return sources


def forbidden(*args, **kwargs):
    raise AssertionError("compile/prebuild on native composed class hit")


def witness(root):
    dev = D.Device("offline", board="vlt_v9t6s_1n", library=root, library_only=True)
    dev.machine = machine_of("vlt_v9t6s_1n", packing_backend="vector_mx7")
    machine = dev.machine.machine_json
    sources = sources_for(dev)
    start = time.perf_counter()
    outputs = rs._rs.template_compile(sources[0], sources, machine)
    identity, table = admit_pair(root, machine, sources, outputs)
    build_seconds = time.perf_counter() - start
    packages, bindings = [], []
    high = {"dram": dev.memory.dram.lo, "stage": dev.memory.staging[0].free_list[0][0]}
    with (
        patch.object(rs._rs, "compile", forbidden),
        patch.object(rs._rs, "library_prebuild", forbidden),
        patch.object(rs._rs, "template_compile", forbidden),
    ):
        for source in sources:
            compiled = dev.kernel(source)
            for _ in range(2):
                nbytes = json.loads(source)["ports"][0]["spec"]["shape"][0] * 128 * 2
                x, y = dev.alloc(nbytes), dev.alloc(nbytes)
                dev.record()
                dev.run(compiled, {"p_in0": x, "c_out": y})
                log = dev.recorded()
                packages.append(log[0][1][0][0])
                bindings.append((dev.mmu.resolve(x)[1], dev.mmu.resolve(y)[1], nbytes))
                high["dram"] = max(
                    high["dram"], max(a + n for a, n in dev.memory.dram.live.items())
                )
                high["stage"] = max(
                    high["stage"],
                    max(a + n for a, n in dev.memory.staging[0].live.items()),
                )
                log.close()
    assert packages[0] != packages[1] and packages[2] != packages[3]
    assert dev.counters["class_hits"] == 2 and dev.counters["compile_calls"] == 0
    result = dict(
        template_id=identity,
        template_table=json.loads(table),
        certified_templates=1,
        admitted_shapes=[[16, 128], [32, 128]],
        address_sets_per_shape=2,
        class_hits=dev.counters["class_hits"],
        compile_calls=dev.counters["compile_calls"],
        runs=dev.counters["runs"],
        build_seconds=build_seconds,
        packages=packages,
        bindings=bindings,
        high=high,
    )
    dev.close()
    return result


def execute(artifact, output):
    saved = pickle.loads(artifact.read_bytes())
    start = time.perf_counter()
    card = VerilatorCard(
        D.ROOT / "build/v9t6s/vlt_card_v9t6s_1n",
        board="vlt_v9t6s_1n",
        log=output.with_suffix(".log"),
        alarm=550,
    )
    results = []
    try:
        card.memory.alloc(saved["high"]["dram"] - card.memory.dram.lo)
        stage_start = card.memory.staging[0].free_list[0][0]
        card.memory.stage(saved["high"]["stage"] - stage_start, 0)
        with (
            patch.object(rs._rs, "compile", forbidden),
            patch.object(rs._rs, "library_prebuild", forbidden),
            patch.object(rs._rs, "template_compile", forbidden),
        ):
            for index, (raw, (x, y, nbytes)) in enumerate(
                zip(saved["packages"], saved["bindings"], strict=True)
            ):
                values = (np.arange(nbytes // 2) % 99 / 32 - 1.5).astype(np.float16)
                expected = np.maximum(
                    (values + np.float16(0.125)).astype(np.float16), np.float16(0)
                )
                card.write(x, values.tobytes())
                card.write(y, bytes([0xA5]) * nbytes)
                queue = card.queues[0]
                prepared = queue.prepare(raw)
                done = queue.wait(queue.submit(prepared))
                assert done.ok, done.describe()
                actual = card.get(y, nbytes)
                assert actual == expected.tobytes()
                results.append(
                    dict(
                        index=index,
                        shape=[nbytes // 256, 128],
                        x=x,
                        y=y,
                        cycles=done.cycles,
                        bit_equal=True,
                    )
                )
                prepared.close()
    finally:
        card.close()
    output.write_text(
        json.dumps(
            dict(
                results=results,
                wall_seconds=time.perf_counter() - start,
                compile_calls=0,
            ),
            indent=2,
        )
    )
    print(output.read_text())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path)
    parser.add_argument("--execute", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.execute is not None:
        execute(args.execute, args.output)
        return
    result = witness(args.library)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.with_suffix(".pkl").write_bytes(pickle.dumps(result))
    args.output.write_text(
        json.dumps({k: v for k, v in result.items() if k != "packages"}, indent=2)
    )
    print(args.output.read_text())


if __name__ == "__main__":
    main()
