"""Offline workload and warm-TinyJit event capture with relocatable Prebuilt packages."""

import argparse
import gzip
import json
import pathlib
import pickle
from dataclasses import replace
from unittest.mock import patch

import numpy as np
from examples.tinygrad.vit_tiny import Block
from tinygrad import Tensor, TinyJit, nn
from tinygrad.helpers import Context
from tinygrad.nn.state import get_parameters

from kohakutpu.application.ktpugrad.device import KTPUGraph
from kohakutpu.application.ktpugrad.nn import place
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime.osgraph import Graph


class ReducedLeNet:
    """Two conv/pool stages and a classifier, configurable channels and image size."""

    def __init__(self, channels, image_size, kernel_size):
        self.c1 = nn.Conv2d(1, channels, kernel_size)
        self.c2 = nn.Conv2d(channels, channels, kernel_size)
        spatial = ((image_size - kernel_size + 1) // 2 - kernel_size + 1) // 2
        self.fc = nn.Linear(channels * spatial * spatial, 2)

    def __call__(self, x):
        x = self.c1(x).relu().avg_pool2d(2)
        x = self.c2(x).relu().avg_pool2d(2)
        return self.fc(x.flatten(1))


def _unit_map(dev):
    return {
        (mesh.index, *coord): i
        for mesh in dev.card.machine.meshes
        for i, coord in enumerate(
            coord for coords in mesh.units.values() for coord in coords
        )
    }


def capture_workload(
    workload, board, channels=2, image_size=12, kernel_size=3, parameter_value=None
):
    Tensor.manual_seed(17)
    dev = D.Device("offline", board=board, nodes=1, classes=False)
    events, reads = [], []
    original_run, original_write, original_get = (
        dev.card.run,
        dev.card.write,
        dev.card.get,
    )

    def run(program, faults=False):
        programs = program if isinstance(program, dict) else {0: program}
        events.append(("program", {n: replace(p) for n, p in programs.items()}))
        return original_run(program, faults)

    def write(addr, raw):
        events.append(("write", addr, bytes(raw)))
        return original_write(addr, raw)

    def get(addr, size):
        reads.append((addr, size))
        return original_get(addr, size)

    with (
        D.attached(dev),
        patch.object(dev.card, "run", run),
        patch.object(dev.card, "write", write),
        patch.object(dev.card, "get", get),
    ):
        dev.record()
        with Context(DEV="CPU"):
            model = (
                ReducedLeNet(channels, image_size, kernel_size)
                if workload == "lenet"
                else Block(64)
            )
            if parameter_value is not None:
                for parameter in get_parameters(model):
                    parameter.assign(
                        Tensor.full(parameter.shape, parameter_value)
                    ).realize()
        place(model)
        shape = (1, 1, image_size, image_size) if workload == "lenet" else (1, 2, 64)
        x = Tensor(
            np.linspace(-0.5, 0.5, np.prod(shape), dtype=np.float16).reshape(shape),
            device="KTPU",
        ).realize()
        split = len(events)
        if workload == "lenet":
            with Context(TRAINING=1):
                logits = model(x)
                loss = logits.square().mean()
                loss.backward()
                params = get_parameters(model)
                Tensor.realize(
                    loss,
                    logits,
                    *[p.assign(p.detach() - p.grad * 0.01) for p in params],
                )
            outputs = [loss, logits, *params]
        elif workload == "vit-qkv":
            outputs = [model.qkv(model.norm1(x).realize()).realize()]
        else:
            outputs = [model(x).realize()]
        reads.clear()
        for output in outputs:
            output.numpy()
        record = dev.recorded()
        base = dev.dram.lo
        extent = max(at + size for at, size in dev.dram.live.items()) - base
        result = dict(
            workload=workload,
            board=board,
            input_shape=shape,
            channels=channels if workload == "lenet" else None,
            kernel_size=kernel_size if workload == "lenet" else None,
            parameter_value=parameter_value,
            base=base,
            extent=extent,
            init=events[:split],
            step=events[split:],
            reads=reads,
            executed_arithmetic=False,
        )
        graph = Graph(
            batch=8,
            compact=True,
            movers=True,
            de_stream=True,
            de_fetch=True,
            de_fuse=True,
        )
        frontier = ()
        units = _unit_map(dev)
        for event in result["step"]:
            if event[0] == "program":
                for program in event[1].values():
                    frontier = graph.package(
                        program.bound(False),
                        [],
                        units,
                        after=frontier,
                    )
        result["instruction_graph_tasks"] = len(graph.tasks)
        result["instruction_graph_bytes"] = len(graph.pack())
        record.close()
    dev.close()
    return result


def capture_warmjit(board):
    dev = D.Device("offline", board=board, nodes=1, classes=False)
    source, events, graphs, reads = {}, [], [], []
    original_run, original_replay = dev.card.run, dev.card.replay
    original_write, original_get, original_init = (
        dev.card.write,
        dev.card.get,
        KTPUGraph.__init__,
    )

    def init_graph(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        graphs.append(self)

    def run(program, faults=False):
        programs = program if isinstance(program, dict) else {0: program}
        frozen = {n: replace(p) for n, p in programs.items()}
        events.append(("program", frozen))
        result = original_run(program, faults)
        if dev.card.log is not None:
            for n, pair in dev.card.log[-1][1].items():
                source[(n, pair[0], tuple(pair[1]))] = frozen[n]
        return result

    def replay(log):
        for ev in log:
            if ev[0] == "write":
                events.append(ev)
            elif ev[0] == "run":
                events.append(
                    (
                        "program",
                        {
                            n: source[(n, raw, tuple(bindings))]
                            for n, (raw, bindings) in ev[1].items()
                        },
                    )
                )
            else:
                raise ValueError("warm graph reads host state")
        return original_replay(log)

    def write(pa, raw):
        events.append(("write", pa, bytes(raw)))
        return original_write(pa, raw)

    def get(pa, size):
        reads.append((pa, size))
        return original_get(pa, size)

    with (
        D.attached(dev),
        patch.object(dev.card, "run", run),
        patch.object(dev.card, "replay", replay),
        patch.object(dev.card, "write", write),
        patch.object(dev.card, "get", get),
        patch.object(KTPUGraph, "__init__", init_graph),
        Context(JIT_BATCH_SIZE=0),
    ):
        Tensor.manual_seed(17)
        with Context(DEV="CPU"):
            model = Block(64)
        place(model)
        fn = TinyJit(lambda x: model.qkv(model.norm1(x).realize()).realize())
        inputs = [
            Tensor(
                (np.linspace(-0.5, 0.5, 128, dtype=np.float16) ** (i + 1)).reshape(
                    1, 2, 64
                ),
                device="KTPU",
            ).realize()
            for i in range(2)
        ]
        first_call_programs = []
        for call_idx in range(6):
            fn(inputs[0])
            if call_idx == 0:
                first_call_programs = [ev for ev in events if ev[0] == "program"]
        base = dev.dram.lo
        extent = max(pa + size for pa, size in dev.dram.live.items()) - base
        warmup_events = list(events)
        initial = [
            ("write", base, dev.card.mem.read_block(base, extent)),
            *first_call_programs,
        ]
        epochs = []
        for x in inputs:
            events.clear()
            output = fn(x)
            epochs.append(list(events))
        reads.clear()
        output.numpy()
        split = next(i for i, ev in enumerate(epochs[0]) if ev[0] == "program")
        step = epochs[0][split:]
        assert step == epochs[1][split:]
        assert epochs[0][:split] != epochs[1][:split]
        counts = [g.counts for g in graphs]
        assert counts and all(c["replayed"] >= 2 for c in counts), counts
        units = _unit_map(dev)
        graph, frontier = (
            Graph(
                batch=32,
                compact=True,
                movers=True,
                prune=True,
                de_stream=True,
                de_fetch=True,
                de_fuse=True,
            ),
            (),
        )
        for ev in step:
            if ev[0] == "program":
                for p in ev[1].values():
                    frontier = graph.package(p.bound(False), [], units, after=frontier)
        result = dict(
            workload="vit-qkv-warmjit",
            board=board,
            base=base,
            extent=extent,
            init=initial,
            warmup_events=warmup_events,
            initialization_complete=bool(first_call_programs),
            step=step,
            reads=reads,
            epoch_inputs=[ev[:split] for ev in epochs],
            jit_counts=counts,
            executed_arithmetic=False,
            instruction_graph_bytes=len(graph.pack()),
        )
        for g in graphs:
            g._release_logs()
    dev.close()
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "workload",
        choices=("lenet", "vit", "vit-qkv", "vit-qkv-warmjit"),
        nargs="?",
        default="vit-qkv-warmjit",
    )
    p.add_argument("--out", required=True)
    p.add_argument("--board", default="vlt_v9t6s_1n")
    p.add_argument("--channels", type=int, default=2)
    p.add_argument("--image-size", type=int, default=12)
    p.add_argument("--kernel-size", type=int, default=3)
    p.add_argument("--parameter-value", type=float)
    a = p.parse_args()
    result = (
        capture_warmjit(a.board)
        if a.workload == "vit-qkv-warmjit"
        else capture_workload(
            a.workload,
            a.board,
            a.channels,
            a.image_size,
            a.kernel_size,
            a.parameter_value,
        )
    )
    path = pathlib.Path(a.out)
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wb") as f:
        pickle.dump(result, f)
    print(
        json.dumps(
            {
                k: v
                for k, v in result.items()
                if k not in ("init", "step", "reads", "epoch_inputs", "warmup_events")
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
