"""Exercise the real Device library/allocator/upload path up to the first rows kernel."""

import argparse
import json
import runpy
from pathlib import Path

import numpy as np
from kohakutpu.application.ktpugrad.nn import Adam16
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime.class_library import ClassLibrary
from kohakutpu.application.runtime.machine import machine_of
from kohakutpu.stack import rs
from tinygrad import Tensor
from tinygrad.helpers import Context
from tinygrad.nn.state import get_parameters

ROOT = Path(__file__).resolve().parents[3]


class Done(Exception):
    pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--board", default="vlt_v9t6s_1n")
    ap.add_argument("--classes", action="store_true")
    ap.add_argument("--simulate", action="store_true")
    ap.add_argument("--card-artifacts", action="store_true")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--tables-after-step", action="store_true")
    ap.add_argument("--watch-table", action="store_true")
    ap.add_argument("--watch-completion", action="store_true")
    ap.add_argument("--fixed", action="store_true")
    ap.add_argument(
        "--work", type=Path, required=True, help="pipe work tree (uses loop6/)"
    )
    args = ap.parse_args()
    base = args.work / "loop6"
    if args.card_artifacts:
        # Diagnostic use of the supplied card artifacts: graph/lowering fingerprints
        # differ on this job, while the admitted package/verify ABI is identical.
        versions = json.loads((base / "fault-library.json").read_text())["key"][
            "versions"
        ]
        key_path = ClassLibrary._key_path

        def card_key(self, source):
            key, member, path = key_path(self, source)
            key["versions"] = versions
            return key, member, path

        ClassLibrary._key_path = card_key
    build = Path("C:/Users/apoll/Desktop/code/Project/KohakuTPU/build") / (
        "v9t6/vlt_card_v9t6_1n"
        if args.board == "multimesh_v9t6"
        else "v9t6s/vlt_card_v9t6s_1n"
    )
    dev = D.attach(
        D.Device(
            kind="sim" if args.simulate else "offline",
            board=args.board,
            classes=args.classes,
            library=base / "library",
            library_only=True,
            build=build,
        )
    )
    if args.simulate:
        dev.card.alarm = 450
        patched = (
            {
                p.stem: json.loads(p.read_text())["compiled"]
                for p in (base / "fixed-library").glob("*/*.json")
                if p.name != "index.json"
            }
            if args.fixed
            else {}
        )

        def artifact(source):
            identity = json.loads(
                rs._rs.library_key(source, rs.machine_json(dev.machine))
            )
            c = (
                patched[identity["class"]]
                if identity["class"] in patched
                else json.loads(
                    (
                        base
                        / "library"
                        / identity["machine"]
                        / (identity["class"] + ".json")
                    ).read_text()
                )["compiled"]
            )
            c["packages"] = {
                int(n): dict(
                    bytes=bytes.fromhex(p["bytes"]),
                    params=p["params"],
                    images_json=json.dumps(p["images"]),
                )
                for n, p in c["packages"].items()
            }
            dev.counters["library_hits"] += 1
            dev.counters["exact_hits"] += 1
            return c

        dev.compile_graph = artifact
    dev.machine = machine_of(args.board, nodes=1, packing_backend="vector_mx7")
    FM.seed_hints(dev, base / "card-hints")
    events = []
    write = dev.card.write

    def upload(at, raw):
        events.append(dict(write=at, data=raw.hex()))
        return write(at, raw)

    def execute(card, built):
        pkg, bindings = built[0]
        events.append(dict(run=pkg.to_bytes().hex(), bindings=bindings))
        print("run", sum("run" in e for e in events), "bindings", bindings, flush=True)
        if sum("run" in e for e in events) == 5:
            raise Done()
        return dict(cycles=0, nodes={0: dict(cycles=0, steps=[], phases={}, pkg=pkg)})

    dev.card.write = upload
    if args.simulate:
        real_run = dev.run
        table = FM.table
        expected_tables = {}

        def tracked_table(device, arr, dtype=np.float16):
            addr = table(device, arr, dtype)
            expected_tables[addr] = np.ascontiguousarray(arr, dtype).tobytes()
            return addr

        FM.table = tracked_table
        recent = []
        card_submit = dev.card.submit

        def submission(progs, **kwargs):
            items = progs.items() if isinstance(progs, dict) else [(0, progs)]
            recent.append(
                {
                    str(n): dict(
                        bindings=p.bindings,
                        translations=p.translations,
                        package=p.package.hex(),
                        images=p.images_json,
                    )
                    for n, p in items
                }
            )
            if count == 49:
                for p in recent[-1].values():
                    p["data"] = [
                        dev.card.get(physical, size).hex()
                        for virtual, physical, size in p["translations"]
                    ]
                (base / "site49.json").write_text(json.dumps(recent[-1]))
            del recent[:-3]
            return card_submit(progs, **kwargs)

        dev.card.submit = submission
        if args.watch_completion:
            collect = dev._collect_one

            def checked_collect(submitted):
                result = collect(submitted)
                for address, expected in expected_tables.items():
                    if len(expected) != 50176:
                        continue
                    _, physical = dev.mmu.resolve(address)
                    actual = dev.card.get(physical + 12288, 128)
                    if actual != expected[12288:12416]:
                        (base / "first-corruption.json").write_text(
                            json.dumps(
                                dict(
                                    count=dev.counters["runs"],
                                    name=submitted.name,
                                    address=address,
                                    physical=physical,
                                    info=str(submitted.info),
                                    recent=recent,
                                    expected=expected[12288:12416].hex(),
                                    actual=actual.hex(),
                                )
                            )
                        )
                        print(
                            "FIRST CORRUPTION after completion",
                            dev.counters["runs"],
                            submitted.name,
                            hex(physical),
                            flush=True,
                        )
                        raise Done()
                return result

            dev._collect_one = checked_collect
        count = 0

        def run(compiled, binds, **kwargs):
            nonlocal count
            count += 1
            if count <= 5 or count >= 225:
                print("running", count, compiled.name, flush=True)
            result = real_run(compiled, binds, **kwargs)
            if args.watch_table:
                dev.drain()
                for address, expected in expected_tables.items():
                    if len(expected) != 50176:
                        continue
                    actual = dev.read(address, len(expected))
                    if actual != expected:
                        entry, physical = dev.mmu.resolve(address)
                        (base / "first-corruption.json").write_text(
                            json.dumps(
                                dict(
                                    count=count,
                                    name=compiled.name,
                                    address=address,
                                    physical=physical,
                                    binds=binds,
                                    source=compiled.traced.graph_json,
                                    expected=expected.hex(),
                                    actual=actual.hex(),
                                )
                            )
                        )
                        print(
                            "FIRST CORRUPTION",
                            count,
                            compiled.name,
                            hex(address),
                            hex(physical),
                            flush=True,
                        )
                        raise Done()
            if count == args.limit:
                dev.drain()
                raise Done()
            return result

        dev.run = run
    else:
        dev.card.execute = execute
    prebuild = runpy.run_path(str(ROOT / "scripts/py/templates/prebuild.py"))
    example = runpy.run_path(
        str(ROOT / "software/application/examples/tinygrad/mnist_mlp.py")
    )
    Tensor.manual_seed(0)
    with Context(DEV="CPU"):
        model = example["LeNet"]()
    prebuild["place"](model)
    x = (np.random.default_rng(0).standard_normal((8, 784)) * 0.2).astype(np.float16)
    try:
        opt = Adam16(get_parameters(model), lr=2e-3)
        inp = Tensor(x, device="KTPU").realize()
        lab = Tensor(
            np.eye(10, dtype=np.float16)[np.arange(8) % 10], device="KTPU"
        ).realize()
        with Context(TRAINING=1):
            for iteration in range(2):
                loss = -(model(inp).log_softmax() * lab).sum(-1).mean()
                opt.zero_grad()
                opt.scale(loss).backward()
                Tensor.realize(loss, *opt.schedule_step())
                print("iteration", iteration, "loss", loss.item(), flush=True)
                if args.tables_after_step:
                    bad = []
                    for address, expected in expected_tables.items():
                        actual = dev.read(address, len(expected))
                        if actual != expected:
                            bad.append(
                                dict(
                                    address=address,
                                    expected=expected.hex(),
                                    actual=actual.hex(),
                                )
                            )
                    (
                        base
                        / ("fixed-bad-tables.json" if args.fixed else "bad-tables.json")
                    ).write_text(json.dumps(bad))
                    print(
                        "bad tables",
                        [(hex(r["address"]), len(r["actual"]) // 2) for r in bad],
                        flush=True,
                    )
                    raise Done()
    except Done:
        pass
    print("counters", dev.counters, flush=True)
    if args.simulate:
        dev.close()
        return
    (
        base / f'bound-prefix-{args.board}{"-classes" if args.classes else ""}.json'
    ).write_text(json.dumps(events))


if __name__ == "__main__":
    main()
