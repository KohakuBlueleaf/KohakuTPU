"""Final bounded session: IEEE classifier exhaustive values and workload quant parity."""

import argparse
import copy
import hashlib
import json
import pickle
import time
from pathlib import Path

import numpy as np
from vq_census import census
from vq_model import ROOT, model_counters, packed_reference
from vq_workload import walk

from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import generated
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.application.runtime.packing import NonfinitePackingError
from kohakutpu.stack import rs


def compile_artifact(args):
    machine = machine_of("vlt_v9t6s_1n", packing_backend="vector_mx7")
    cases = []
    for model, measurement in (("lenet", "run6-lenet"), ("vit", "run7-vit")):
        bundle = pickle.loads((args.directory / f"{model}-variants.pkl").read_bytes())
        records = {
            r["key"]: r
            for r in json.loads((args.directory / f"{measurement}.json").read_text())[
                "records"
            ]
        }
        for case in bundle["cases"]:
            if not case["changed"]:
                continue
            graph = copy.deepcopy(case["graph"])
            for node in walk(graph["body"]):
                if node.get("op") == "quant":
                    node["kw"] = [
                        p
                        for p in node["kw"]
                        if p[0] not in ("pipeline", "zero_classifier")
                    ]
                    node["kw"] += [
                        ["pipeline", {"int": 1}],
                        ["zero_classifier", {"int": 1}],
                    ]
            compiled = rs._rs.compile(
                json.dumps(graph), rs.machine_json(machine), cache_dir="", fetch=True
            )
            assert not compiled["verdict"]["refused"], compiled["verdict"]
            case.update(
                final=compiled,
                expected=records[case["key"]]["output_sha256"],
                model=model,
                baseline_cycles=records[case["key"]]["before"],
                pipeline_cycles=records[case["key"]]["after"],
            )
            cases.append(case)
    # The source graph provides port metadata; packages come from exact L1 below.
    graph = dict(
        ports=[
            dict(name=n, spec=dict(shape=[512, 128], dtype="f16")) for n in ("x", "y")
        ]
    )
    source = """target ktpu.v9
image classify(a, b)
    desc a0=a walk=(32, 8)
    desc a1=b walk=(32, 8)
    ainc a0 += 256
    ainc a1 += 256
    seti s0 = 512
    loop s0
        vfill a0 -> l1[0] rel
        vsync fe on=fill
        vunpk v0 <- l1[0] f16
        vmul v1, v0, k0
        vexp2d v3, v1, k0
        vpack v3 -> l1[16] f16
        vsync drain on=pack
        vdrain a1 <- l1[16] rel
        vsync fe on=drain
    halt
fn classifier.l1(x: f16[512,128] @dram, y: f16[512,128] @dram)
    send vc[0]
        run classify(x+0, y+0)
    wait vc[0]
"""
    probe = dict(
        graph=graph,
        compiled=rs._rs.compile(
            source,
            rs.machine_json(machine),
            name="classifier",
            cache_dir="",
            fetch=True,
            l1=True,
        ),
    )
    assert not probe["compiled"]["verdict"]["refused"], probe["compiled"]["verdict"]
    finite = []
    for pipeline, zero in ((0, 0), (1, 0), (0, 1), (1, 1)):
        source = f"target ktpu.v9\nfn allfinite.l2(x: f16[1984,32] @dram, q: mx7a[1984,32] @dram(gm=496,nk=1))\n    quant q <- x finite_batch=4 overlap=1 pipeline={pipeline} zero_classifier={zero}\n"
        compiled = rs._rs.compile(
            source, rs.machine_json(machine), cache_dir="", fetch=True
        )
        assert not compiled["verdict"]["refused"], compiled["verdict"]
        graph = dict(
            ports=[
                dict(name="x", spec=dict(shape=[1984, 32], dtype="f16")),
                dict(
                    name="q",
                    spec=dict(
                        shape=[1984, 32], dtype="mx7a", layout=[["gm", 496], ["nk", 1]]
                    ),
                ),
            ]
        )
        finite.append(
            dict(graph=graph, compiled=compiled, pipeline=pipeline, zero=zero)
        )
    args.output.write_bytes(
        pickle.dumps(
            dict(machine=machine.machine_json, cases=cases, probe=probe, finite=finite)
        )
    )


def execute(args):
    data = pickle.loads(args.artifact.read_bytes())
    started = time.monotonic()
    log = args.output.with_suffix(".log")
    dev = D.Device(
        kind="sim",
        board="vlt_v9t6s_1n",
        nodes=1,
        build=ROOT / "build/v9t6s/vlt_card_v9t6s_1n",
        log=log,
        classes=False,
    )
    dev.machine = machine_of("vlt_v9t6s_1n", packing_backend="vector_mx7")
    records = []
    aggregate = dict(fill_words=0, drain_words=0, lane_slots=0)

    def run(compiled, graph, binds):
        result = dev.run(
            generated.from_output(dev, graph, compiled), binds, kind="quant"
        )
        work = census(compiled["l1"])
        for key in aggregate:
            aggregate[key] += work[key]
        return result.cycles, work

    def publish(record):
        records.append(record)
        args.output.write_text(
            json.dumps(dict(records=records), indent=2), encoding="utf-8"
        )
        print(json.dumps(record), flush=True)

    try:
        bits = np.arange(65536, dtype="<u2")
        finite_mask = bits & 0x7C00 != 0x7C00
        x, y = dev.alloc(bits.nbytes), dev.alloc(bits.nbytes)
        dev.write(x, bits.tobytes())
        probe = data["probe"]
        cycles, work = run(probe["compiled"], probe["graph"], {"x": x, "y": y})
        got = np.frombuffer(dev.read(y, bits.nbytes), dtype="<u2")
        assert np.all(got[finite_mask] == 0x3C00)
        assert np.all((got[~finite_mask] & 0x7FFF) > 0x7C00)
        publish(
            dict(
                label="classifier-all-fp16",
                finite=int(finite_mask.sum()),
                nonfinite=int((~finite_mask).sum()),
                cycles=cycles,
                work=work,
                parity="all 65536 encodings",
            )
        )
        dev.free(x)
        dev.free(y)
        finite_bits = bits[finite_mask].reshape(1984, 32)
        expected = packed_reference(finite_bits.view("<f2"), "a", 496, 1)
        x, q = dev.alloc(finite_bits.nbytes), dev.alloc(len(expected))
        dev.write(x, finite_bits.tobytes())
        for case in data["finite"]:
            cycles, work = run(case["compiled"], case["graph"], {"x": x, "q": q})
            assert dev.read(q, len(expected)) == expected
            publish(
                dict(
                    label="all-finite-quant",
                    pipeline=case["pipeline"],
                    zero=case["zero"],
                    cycles=cycles,
                    work=work,
                    parity="exact independent MX7 bytes",
                )
            )
        dev.free(x)
        dev.free(q)
        for case in data["cases"]:
            assert time.monotonic() - started < 510, "session budget"
            addresses = {
                base: dev.alloc(a["extent"] + 256)
                for base, a in case["allocations"].items()
            }
            binds = {
                name: addresses[v["allocation"]] + 128 + v["offset"]
                for name, v in case["views"].items()
            }
            try:
                for base, a in case["allocations"].items():
                    dev.write(
                        addresses[base],
                        bytes([0xD3]) * 128
                        + a["data"]
                        + bytes(a["extent"] - len(a["data"]))
                        + bytes([0xD3]) * 128,
                    )
                cycles, work = run(case["final"], case["graph"], binds)
                hashes = {
                    name: hashlib.sha256(dev.read(binds[name], v["nbytes"])).hexdigest()
                    for name, v in case["views"].items()
                    if v["role"] != "in"
                }
                assert hashes == case["expected"], case["label"]
                for base, a in case["allocations"].items():
                    assert (
                        dev.read(addresses[base], 128)
                        == dev.read(addresses[base] + 128 + a["extent"], 128)
                        == bytes([0xD3]) * 128
                    )
                publish(
                    dict(
                        label=case["label"],
                        model=case["model"],
                        key=case["key"],
                        before=case["baseline_cycles"],
                        pipeline=case["pipeline_cycles"],
                        after=cycles,
                        work=work,
                        parity="exact baseline SHA256",
                    )
                )
            finally:
                for address in addresses.values():
                    dev.free(address)
        # A live infinity must reach the actual firmware contract error, not only NaN math.
        case = data["finite"][-1]
        x, q = dev.alloc(finite_bits.nbytes), dev.alloc(len(expected))
        raw = finite_bits.copy()
        raw[-1, -1] = 0xFC00
        dev.write(x, raw.tobytes())
        try:
            run(case["compiled"], case["graph"], {"x": x, "q": q})
        except NonfinitePackingError as error:
            work = census(case["compiled"]["l1"])
            for key in aggregate:
                aggregate[key] += work[key]
            publish(
                dict(
                    label="zero-classifier-live-negative-infinity", rejected=str(error)
                )
            )
        else:
            raise AssertionError("zero classifier accepted infinity")
        finally:
            dev.free(x)
            dev.free(q)
    finally:
        try:
            dev.close()
        except NonfinitePackingError:
            assert records[-1]["label"] == "zero-classifier-live-negative-infinity"
        counters = model_counters(log)
        actual = next(iter(counters.values()))["VRES"]
        checks = {
            key: dict(
                census=value, model=actual["lanes" if key == "lane_slots" else key]
            )
            for key, value in aggregate.items()
        }
        args.output.write_text(
            json.dumps(
                dict(
                    records=records,
                    wall_seconds=time.monotonic() - started,
                    counters=counters,
                    checks=checks,
                ),
                indent=2,
            ),
            encoding="utf-8",
        )
        assert all(v["census"] == v["model"] for v in checks.values()), checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("compile", "execute"))
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    (compile_artifact if args.mode == "compile" else execute)(args)


if __name__ == "__main__":
    main()
