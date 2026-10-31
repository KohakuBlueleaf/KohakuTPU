"""CPU contract experiment, not native execution or a device performance model."""

import json
from dataclasses import dataclass, replace
from time import perf_counter

import numpy as np

from kohakutpu.application.runtime.graph.capture import (
    Capture,
    Composer,
    Launch,
    Library,
    Rules,
    Span,
    Template,
)
from kohakutpu.language.numerics import fp16, mxfp7


@dataclass(frozen=True)
class Packet:
    evaluate: object
    inputs: tuple
    output: int


class Runner:
    """Host reference runner witnessing one submit call and boundary traffic."""

    def __init__(self, memory):
        self.memory = memory
        self.submissions = 0
        self.stores = 0

    def execute(self, packet):
        self.memory[packet.output] = packet.evaluate(
            *(self.memory[address] for address in packet.inputs)
        )
        self.stores += 1

    def prepare(self, packets, dependencies):
        return Prepared(self, packets, dependencies)


@dataclass
class Prepared:
    runner: Runner
    packets: tuple
    dependencies: tuple

    def submit(self):
        self.runner.submissions += 1
        for packet in self.packets:
            self.runner.execute(packet)


def primitive(name, family, operation):
    def bind(parameters, addresses):
        return Packet(
            lambda *xs: operation(parameters, *xs), addresses[:-1], addresses[-1]
        )

    return Template(("cpu-reference-v1", name), family, bind)


def build(key, left, right, links):
    count = key[-3]
    right_count = key[-2]

    def bind(parameters, addresses):
        lp, rp = parameters
        a = left.bind(lp, (*addresses[:count], None))
        b = right.bind(rp, (*([None] * right_count), addresses[-1]))

        def evaluate(*inputs):
            value = a.evaluate(*inputs[:count])
            external = iter(inputs[count:])
            args = tuple(
                value if i in links else next(external) for i in range(right_count)
            )
            return b.evaluate(*args)

        return Packet(evaluate, addresses[:-1], addresses[-1])

    return Template(key, right.family, bind)


def library():
    lib = Library()
    composer = Composer(("cpu-reference-composition-v1",), lambda *_: True, build)
    for rule, a, b in (
        ("elementwise", "linear", "elementwise"),
        ("quant", "quant", "linear"),
        ("layout", "layout", "linear"),
        ("norm", "norm", "linear"),
    ):
        lib.register(rule, a, b, composer)
    return lib


def normalized(_, x):
    value = x.astype(np.float32)
    mean = value.mean(axis=-1, keepdims=True)
    variance = ((value - mean) ** 2).mean(axis=-1, keepdims=True)
    return fp16.write((value - mean) / np.sqrt(variance + 1e-5))


LINEAR = primitive(
    "linear", "linear", lambda _, x, w: fp16.write(x.astype(np.float32) @ w)
)
ADD = primitive("add", "elementwise", lambda p, x: fp16.write(x.astype(np.float32) + p))
QUANT = primitive("quant-values", "quant", lambda _, x: fp16.write(mxfp7.value_fp16(x)))
LAYOUT = primitive("transpose", "layout", lambda _, x: x.T.copy())
NORM = primitive("norm", "norm", normalized)


def case(rule, rows=4, width=32, base=0):
    rng = np.random.default_rng(42)
    shape = (width, rows) if rule == "layout" else (rows, width)
    x = rng.standard_normal(shape).astype(np.float16)
    w = rng.standard_normal((width, 16)).astype(np.float16)
    spans = tuple(
        Span(i + base, 0, n)
        for i, n in enumerate((x.nbytes, rows * width * 2, w.nbytes, rows * 16 * 2))
    )
    a, t, weight, y = spans
    addresses = {s.allocation: (i + 1 + base) * 0x10000 for i, s in enumerate(spans)}
    memory = {addresses[a.allocation]: x, addresses[weight.allocation]: w}
    runner = Runner(memory)
    cap = Capture()
    if rule == "elementwise":
        t = Span(t.allocation, 0, rows * 16 * 2)
        cap.launch(LINEAR, (rows, width), (a, weight), t, addresses, runner.execute)
        cap.launch(ADD, 0.125, (t,), y, addresses, runner.execute)
    else:
        template = {"quant": QUANT, "layout": LAYOUT, "norm": NORM}[rule]
        cap.launch(template, (rows, width), (a,), t, addresses, runner.execute)
        cap.launch(LINEAR, (rows, width), (t, weight), y, addresses, runner.execute)
    return cap, y, addresses, memory


def main():
    report = {"backend": "CPU contract reference, not native fusion", "rules": {}}
    for rule in ("elementwise", "quant", "layout", "norm"):
        lib = library()
        cap, y, addresses, memory = case(rule)
        expected = memory[addresses[y.allocation]].copy()
        off = cap.finish((y,), lib, Rules(**{rule: False}))
        on = cap.finish((y,), lib)
        first_build_seconds = lib.build_seconds
        second, y2, addresses2, memory2 = case(rule, rows=7, base=100)
        warm = second.finish((y2,), lib)
        runner = Runner(memory)
        prepared = on.prepare(runner, addresses)
        start = perf_counter()
        prepared.submit()
        elapsed = perf_counter() - start
        actual = memory[addresses[y.allocation]]
        other_expected = memory2[addresses2[y2.allocation]].copy()
        warm.prepare(Runner(memory2), addresses2).submit()
        report["rules"][rule] = {
            "off_launches": len(off.launches),
            "on_launches": len(on.launches),
            "capture_seconds": on.capture_seconds,
            "warm_capture_seconds": warm.capture_seconds,
            "build_seconds": first_build_seconds,
            "builds": lib.builds,
            "cache_hits": lib.hits,
            "cpu_replay_seconds": elapsed,
            "submissions": runner.submissions,
            "boundary_stores": runner.stores,
            "bit_equal": bool(
                np.array_equal(actual.view(np.uint16), expected.view(np.uint16))
            ),
            "changed_shape_bit_equal": bool(
                np.array_equal(memory2[addresses2[y2.allocation]], other_expected)
            ),
        }
    cap = Capture()
    for i in range(10000):
        cap.launches.extend(
            (
                Launch(
                    LINEAR,
                    (4, 32),
                    (Span(i * 4, 0, 256), Span(i * 4 + 1, 0, 1024)),
                    Span(i * 4 + 2, 0, 128),
                ),
                Launch(ADD, 0.125, (Span(i * 4 + 2, 0, 128),), Span(i * 4 + 3, 0, 128)),
            )
        )
    lib = library()
    graph = cap.finish((), lib)
    report["large_graph"] = {
        "before": graph.before,
        "after": len(graph.launches),
        "capture_seconds": graph.capture_seconds,
        "builds": lib.builds,
        "hits": lib.hits,
    }
    arena = Capture()
    for launch in cap.launches:
        spans = tuple(
            Span("arena", span.allocation * 4096, span.nbytes)
            for span in (*launch.inputs, launch.output)
        )
        arena.launches.append(replace(launch, inputs=spans[:-1], output=spans[-1]))
    arena_graph = arena.finish((), lib)
    report["large_arena_views"] = {
        "before": arena_graph.before,
        "after": len(arena_graph.launches),
        "capture_seconds": arena_graph.capture_seconds,
        "builds": lib.builds,
        "hits": lib.hits,
    }
    report["brief_cycle_bound"] = {
        "baseline": 10_000_000,
        "quant_convert_move": 4_000_000,
        "perfect_elision_floor": 6_000_000,
        "old_rate_target": 900_000,
        "floor_over_target": 6_000_000 / 900_000,
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
