"""Host-orchestrated replicated linear training and row-sharded contractions.

All arithmetic in a measured step executes on the existing per-node firmware.
The host uploads immutable inputs and checks results, not intermediate tensors.
"""

import argparse
import json
import pathlib
import time

import numpy as np

from kohakutpu.application.kernels import transpose as TP
from kohakutpu.application.runtime.device import Device
from kohakutpu.language import dsl as kt
from kohakutpu.language.numerics import e8m15, mxfp7

BUILD = "/home/kblueleaf/ktpu/build/t2s/vlt_card_v9t6_2n"
BOARD = "vlt_v9t6_2n"


def product(a, b, out, m, n, k, node, row0=0):
    """One local contraction, packing and untile included; dimensions tile exactly."""
    aq = kt.buffer((m, k), "mx7a", gm=m // 4, nk=k // 32, space="stage", node=node)
    bq = kt.buffer((n, k), "mx7b", gn=n // 4, nk=k // 32, space="stage", node=node)
    tile = kt.buffer((m, n), "f16", tile=(m // 4, n // 4), space="stage", node=node)
    kt.quant(aq, a, finite=True)
    kt.quant(bq, b, finite=True)
    for _ in kt.par(1, on=kt.mg):
        acc = kt.gemm(kt.fill(aq), kt.fill(bq))
        kt.drain(acc, tile)
    for _ in kt.par(1, on=kt.vc):
        for r in kt.range(m // 4):
            kt.store(
                out[row0 + r * 4 : row0 + r * 4 + 4, :],
                kt.load(tile[r * 4 : r * 4 + 4, :]),
            )


def specs(ports):
    return {
        name: kt.TensorSpec.make(port.dims, port.dtype, port.layout, port.space)
        for name, port in ports.items()
    }


def matmul_kernel(m=32, n=32, k=32, nodes=2):
    """Row partition: distinct inputs/outputs per shard, shared read-only weights."""
    per = m // nodes
    ports = {"a": kt.In(m, k), "b": kt.In(n, k), "c": kt.Out(m, n)}

    def body(a, b, c):
        for node in range(nodes):
            with_node = kt.par(1, on=kt.node, at=node)
            for _ in with_node:
                product(
                    a[node * per : (node + 1) * per, :],
                    b,
                    c,
                    per,
                    n,
                    k,
                    node,
                    row0=node * per,
                )

    return kt.Kernel.build(f"mn_mm_{m}_{n}_{k}_{nodes}", body, ports), specs(ports)


def local_gradient_kernel(batch=32, inputs=32, outputs=32, nodes=2):
    """Linear MSE: E = X W^T - Y, unnormalised dW = E^T X on each replica."""
    ports = {"mask": kt.In(2, 128)}
    for node in range(nodes):
        ports.update(
            {
                f"x{node}": kt.In(batch, inputs),
                f"xt{node}": kt.In(inputs, batch),
                f"y{node}": kt.In(batch, outputs),
                f"w{node}": kt.In(outputs, inputs),
                f"g{node}": kt.Out(outputs, inputs),
            }
        )

    def body(**p):
        for node in range(nodes):
            for _ in kt.par(1, on=kt.node, at=node):
                pred = kt.buffer((batch, outputs), "f16", space="stage", node=node)
                error = kt.buffer((batch, outputs), "f16", space="stage", node=node)
                et = kt.buffer((outputs, batch), "f16", space="stage", node=node)
                product(
                    p[f"x{node}"], p[f"w{node}"], pred, batch, outputs, inputs, node
                )
                for _ in kt.par(1, on=kt.vc):
                    for r in kt.range(batch // 4):
                        lo = r * 4
                        e = kt.load(pred[lo : lo + 4, :], gt4=True) - kt.load(
                            p[f"y{node}"][lo : lo + 4, :], gt4=True
                        )
                        kt.store(error[lo : lo + 4, :], e)
                TP.emit(
                    error,
                    p["mask"],
                    0,
                    et,
                    1,
                    batch,
                    outputs,
                    1,
                    scratch_space="stage",
                    scratch_node=node,
                )
                product(et, p[f"xt{node}"], p[f"g{node}"], outputs, inputs, batch, node)

    return kt.Kernel.build(
        f"mn_grad_{batch}_{inputs}_{outputs}_{nodes}", body, ports
    ), specs(ports)


def reduce_update_kernel(batch=32, inputs=32, outputs=32, nodes=2, lr=0.125):
    """All-reduce by shared DRAM reads, then independent identical SGD updates."""
    ports = {f"g{i}": kt.In(outputs, inputs) for i in range(nodes)}
    for node in range(nodes):
        ports.update(
            {
                f"w{node}": kt.In(outputs, inputs),
                f"reduced{node}": kt.Out(outputs, inputs),
                f"next{node}": kt.Out(outputs, inputs),
            }
        )

    def body(**p):
        for node in range(nodes):
            for _ in kt.par(1, on=kt.node, at=node):
                for _ in kt.par(1, on=kt.vc):
                    for r in kt.range(outputs // 4):
                        lo = r * 4
                        total = kt.unpack(kt.load(p["g0"][lo : lo + 4, :], gt4=True))
                        for peer in range(1, nodes):
                            total = total + kt.load(
                                p[f"g{peer}"][lo : lo + 4, :], gt4=True
                            )
                        mean = total * (1.0 / (nodes * batch))
                        w = kt.load(p[f"w{node}"][lo : lo + 4, :], gt4=True)
                        kt.store(p[f"reduced{node}"][lo : lo + 4, :], mean)
                        kt.store(p[f"next{node}"][lo : lo + 4, :], w - mean * lr)

    return kt.Kernel.build(
        f"mn_sgd_{batch}_{inputs}_{outputs}_{nodes}", body, ports
    ), specs(ports)


def training_data(batch=32, inputs=32, outputs=32, nodes=2):
    rng = np.random.default_rng(27)
    arrays = {"mask": TP.masks()}
    initial = np.full((outputs, inputs), 0.015625, np.float16)
    for node in range(nodes):
        x = rng.choice([-0.125, 0.125], (batch, inputs)).astype(np.float16)
        arrays.update(
            {
                f"x{node}": x,
                f"xt{node}": x.T.copy(),
                f"y{node}": rng.choice([-0.125, 0.125], (batch, outputs)).astype(
                    np.float16
                ),
                f"w{node}": initial,
            }
        )
    return arrays


def contract_oracle(a, b):
    """One K=32 block: exact scaled integer sum, FP22 RNE, then FP16 RNE."""
    mantissa, exponent = np.frexp(mxfp7.model_matmul(a, b))
    fp22 = np.ldexp(np.rint(np.ldexp(mantissa, 15)), exponent - 15)
    return fp22.astype(np.float16)


def oracle(arrays, batch=32, nodes=2, lr=0.125):
    grads = []
    for node in range(nodes):
        x, y, w = (arrays[f"{key}{node}"] for key in ("x", "y", "w"))
        pred = contract_oracle(x, w)
        error = (pred.astype(np.float64) - y).astype(np.float16)
        grads.append(contract_oracle(error.T.copy(), x.T.copy()).astype(np.float64))
    total = grads[0]
    for gradient in grads[1:]:
        total = e8m15.value(total + gradient)
    mean = total / (nodes * batch)
    weights = e8m15.value(arrays["w0"].astype(np.float64) - lr * mean)
    return grads, mean.astype(np.float16), weights.astype(np.float16)


def witness(arrays, actual, batch=32, nodes=2, lr=0.125):
    grads, mean, weights = oracle(arrays, batch, nodes, lr)
    for node in range(nodes):
        np.testing.assert_array_equal(actual[f"g{node}"], grads[node])
    for node in range(nodes):
        np.testing.assert_array_equal(actual[f"reduced{node}"], mean)
        np.testing.assert_array_equal(actual[f"next{node}"], weights)
    assert not np.array_equal(grads[0], grads[1])
    assert np.any(weights != arrays["w0"])
    np.testing.assert_array_equal(actual["next0"], actual["next1"])


class Experiment:
    """Counts logical launches, per-node entries and actual harness protocol calls."""

    def __init__(self, dev):
        self.dev = dev
        self.addresses = {}
        self.shapes = {}
        self.metrics = []

    def put(self, name, array):
        at = self.dev.alloc(array.nbytes)
        self.dev.write(at, array.tobytes())
        self.addresses[name], self.shapes[name] = at, array.shape

    def output(self, name, shape):
        self.put(name, np.full(shape, -7, np.float16))

    def launch(self, name, recipe, bindings):
        kernel, given = recipe
        compiled = self.dev.specialise(kernel, given)
        transport = self.dev.card.t
        start = transport.status()["sys"]
        before = transport.calls
        t0 = time.monotonic()
        run = self.dev.run(
            compiled, {key: self.addresses[value] for key, value in bindings.items()}
        )
        elapsed = time.monotonic() - t0
        calls = transport.calls - before
        end = transport.status()["sys"]
        record = {
            "name": name,
            "package_cycles": run.cycles,
            "model_sys_cycles": end - start,
            "transport_calls": calls,
            "logical_launches": 1,
            "node_submissions": len(compiled.packages),
            "wall_seconds": elapsed,
        }
        self.metrics.append(record)
        print("MN_PHASE " + json.dumps(record), flush=True)

    def read(self, name):
        shape = self.shapes[name]
        return np.frombuffer(
            self.dev.read(self.addresses[name], int(np.prod(shape)) * 2), np.float16
        ).reshape(shape)

    def close(self):
        for at in self.addresses.values():
            self.dev.free(at)


def measure(case="all", build=BUILD):
    dev = Device(kind="sim", build=build, board=BOARD, nodes=2, queued=False)
    exp = Experiment(dev)
    result = {"board": BOARD, "build": build, "case": case, "phases": exp.metrics}
    try:
        if case in ("all", "matmul"):
            rng = np.random.default_rng(4)
            a = rng.choice([-0.125, 0.125], (32, 32)).astype(np.float16)
            b = rng.choice([-0.125, 0.125], (32, 32)).astype(np.float16)
            exp.put("a", a)
            exp.put("b", b)
            for count in (1, 2):
                out = f"mm{count}"
                exp.output(out, (32, 32))
                for repetition in range(2):
                    exp.launch(
                        f"{out}_{'cold' if repetition == 0 else 'warm'}",
                        matmul_kernel(nodes=count),
                        {"a": "a", "b": "b", "c": out},
                    )
                    np.testing.assert_array_equal(exp.read(out), contract_oracle(a, b))
            np.testing.assert_array_equal(exp.read("mm1"), exp.read("mm2"))
            result["matmul_exact"] = True
        if case in ("all", "train"):
            arrays = training_data()
            for name, array in arrays.items():
                exp.put(name, array)
            for node in range(2):
                for prefix in ("g", "reduced", "next"):
                    exp.output(f"{prefix}{node}", (32, 32))
            grad, gs = local_gradient_kernel()
            update, us = reduce_update_kernel()
            for repetition in range(2):
                label = "cold" if repetition == 0 else "warm"
                exp.launch(
                    f"local_gradients_{label}", (grad, gs), {name: name for name in gs}
                )
                exp.launch(
                    f"reduce_sgd_{label}", (update, us), {name: name for name in us}
                )
            actual = {
                f"{key}{node}": exp.read(f"{key}{node}")
                for key in ("g", "reduced", "next")
                for node in range(2)
            }
            expected_grads, expected_mean, _ = oracle(arrays)
            print(
                "MN_DIAGNOSTICS "
                + json.dumps(
                    {
                        "gradient_mismatches": [
                            int(np.count_nonzero(actual[f"g{i}"] != expected_grads[i]))
                            for i in range(2)
                        ],
                        "reduction_mismatches": [
                            int(
                                np.count_nonzero(actual[f"reduced{i}"] != expected_mean)
                            )
                            for i in range(2)
                        ],
                        "weight_replicas_equal": bool(
                            np.array_equal(actual["next0"], actual["next1"])
                        ),
                    }
                ),
                flush=True,
            )
            witness(arrays, actual)
            result["training_exact"] = True
            result["training"] = {
                "model": "bias-free linear MSE (MLP building block)",
                "global_batch": 64,
                "parameters": 1024,
                "gradient_bytes": 2048,
                "shared_gradient_remote_read_bytes": 4096,
                "weight_replicas_equal": True,
                "changed_weights": int(
                    np.count_nonzero(actual["next0"] != arrays["w0"])
                ),
                "nonzero_initial_weights": int(np.count_nonzero(arrays["w0"])),
            }
        result["decerr"] = dev.card.t.status()["decerr"]
        assert result["decerr"] == 0
        print("MN_RESULT " + json.dumps(result), flush=True)
        return result
    finally:
        exp.close()
        dev.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=("all", "matmul", "train"), default="all")
    parser.add_argument("--build", default=BUILD)
    parser.add_argument("--output", type=pathlib.Path)
    parser.add_argument("--describe", action="store_true")
    args = parser.parse_args()
    if args.describe:
        dev = Device(kind="offline", board=BOARD, nodes=2)
        kernel, given = local_gradient_kernel()
        compiled = dev.specialise(kernel, given)
        print(json.dumps(compiled.scratch, indent=2))
        print(compiled.l1_text)
        dev.close()
        return
    result = measure(args.case, args.build)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
