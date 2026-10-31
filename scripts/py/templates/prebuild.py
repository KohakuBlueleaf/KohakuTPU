"""Trace registered examples, prebuild exact/class libraries, or run library-only."""

import argparse
import json
import math
import pathlib
import runpy
import time
from collections import Counter, defaultdict
from contextlib import ExitStack
from functools import partial
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from examples.tinygrad.mnist_mlp import HostLearningRate, learning_rate, train_step
from examples.tinygrad.mnist_models import MODELS as MNIST_MODELS
from kohakutpu.application.ktpugrad.device import KTPUGraph
from kohakutpu.application.ktpugrad.nn import Adam16, place
from kohakutpu.application.runtime import device as D
from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime import generated, launch_boundary
from kohakutpu.application.runtime.class_library import ClassLibrary, structure
from kohakutpu.application.runtime.template_store import TemplateStore, digest
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs
from tinygrad import TinyJit
from tinygrad.helpers import Context
from tinygrad.nn.state import get_parameters
from tinygrad.tensor import Tensor

ROOT = pathlib.Path(__file__).resolve().parents[3]
_CAPTURE_PHASE = "setup"


def mnist_model(
    batch,
    forward_only=False,
    steps=1,
    jit=False,
    churn=(),
    *,
    model_name="lenet",
    eval_batch=None,
    schedule="constant",
    warmup=0,
    lr_min=0.0,
    weight_decay=0.0,
    capture_tail=False,
    phase_prefix="",
):
    global _CAPTURE_PHASE
    _CAPTURE_PHASE = phase_prefix + "setup"
    Tensor.manual_seed(0)
    with Context(DEV="CPU"):
        model = MNIST_MODELS[model_name]()
    place(model)
    x = np.zeros((batch, 784), dtype=np.float16)
    _CAPTURE_PHASE = phase_prefix + "forward"
    result = model(Tensor(x, device="KTPU")).realize()
    if forward_only:
        return result
    opt = Adam16(get_parameters(model), lr=2e-3, weight_decay=weight_decay)
    host_lr = HostLearningRate(opt)
    y = np.arange(batch, dtype=np.int32) % 10
    dev = D.device()
    eye = np.eye(10, dtype=x.dtype)
    t_start = time.monotonic()
    step_records = []

    def one(xb, yb):
        return train_step(model, opt, xb, yb)

    if jit:
        one = TinyJit(one)
    with Context(TRAINING=1):
        for step in range(steps):
            _CAPTURE_PHASE = f"{phase_prefix}{'jit_' if jit else ''}step{step}"
            if churn:
                dev.free(churn.pop())
            r0 = dev.counters["runs"]
            s0 = time.perf_counter()
            value = host_lr.write(
                learning_rate(step, steps, 2e-3, schedule, warmup, lr_min)
            )
            observed = host_lr.read()
            assert observed == value, (step, observed, value)
            xb = Tensor(x, device="KTPU").realize()
            yb = Tensor(eye[y], device="KTPU").realize()
            loss, _logits = one(xb, yb)
            val = loss.item()
            _logits.numpy()
            dt = time.perf_counter() - s0
            launches = dev.counters["runs"] - r0
            wall = time.monotonic() - t_start
            print(f"  step {step:4d}  loss {val:.4f}  ({wall:.0f} s)", flush=True)
            step_records.append(
                {
                    "step": step,
                    "lr": value,
                    "lr_buffer": observed,
                    "seconds": dt,
                    "launches": launches,
                    "ms_per_launch": dt * 1000.0 / max(1, launches),
                }
            )
    _CAPTURE_PHASE = phase_prefix + "evaluation"
    with Context(TRAINING=0):
        model(
            Tensor(
                np.zeros((eval_batch or batch, 784), dtype=np.float16), device="KTPU"
            )
        ).numpy()
    tail = 60000 % batch
    if capture_tail and tail:
        mnist_model(
            tail,
            forward_only,
            steps,
            jit,
            model_name=model_name,
            eval_batch=eval_batch,
            schedule=schedule,
            warmup=warmup,
            lr_min=lr_min,
            weight_decay=weight_decay,
            phase_prefix=f"tail{tail}_",
        )
    return step_records


def vit_tiny(
    batch, forward_only=True, steps=1, jit=False, churn=(), width=192, **recipe
):
    example = runpy.run_path(
        str(ROOT / "software/application/examples/tinygrad/vit_tiny.py")
    )
    model = example["seeded_model"](blocks=12, seed=17, width=width, image_size=224)
    place(model)
    inp = np.zeros((batch, 3, 224, 224), dtype=np.float16)
    if steps == 1:
        return model(Tensor(inp, device="KTPU")).realize()
    dev = D.device()
    step_records = []

    def forward(x):
        return model(x).realize()

    if jit:
        forward = TinyJit(forward)
    for step in range(steps):
        if churn:
            dev.free(churn.pop())
        r0 = dev.counters["runs"]
        s0 = time.perf_counter()
        forward(Tensor(inp, device="KTPU").realize())
        dev.drain()
        dt = time.perf_counter() - s0
        launches = dev.counters["runs"] - r0
        step_records.append(
            {
                "step": step,
                "seconds": dt,
                "launches": launches,
                "ms_per_launch": dt * 1000.0 / max(1, launches),
            }
        )
    return step_records


MODELS = {name: partial(mnist_model, model_name=name) for name in MNIST_MODELS}
MODELS["vit-tiny"] = vit_tiny
MODELS["vit-small"] = partial(vit_tiny, width=384)
MODELS["vit-base"] = partial(vit_tiny, width=768)
lenet = MODELS["lenet"]
#: Steps a training capture runs: the layout hints the first learns from its
#: consumers (`forms.hints`) change the kernels of the next. The library
#: keeps those hints (`forms.save_hints`), so a run starts from them.
CAPTURE_STEPS = 2
#: Passes a capture makes at most to reach hints no pass changes.
CAPTURE_PASSES = 4


def capture(
    dev,
    batch,
    forward_only=False,
    model="lenet",
    edge_events=None,
    eval_batch=None,
    observe_kernel=None,
    **recipe,
):
    sources, launches = {}, []
    phases = Counter()
    last_phases = {}

    def _alias_sig(binds):
        names = sorted(binds)
        resolved = {}
        for p in names:
            entry, pa = dev.mmu.resolve(binds[p])
            resolved[p] = (id(entry.arena), entry.physical, pa)
        sig = []
        for i, p1 in enumerate(names):
            a1, base1, pa1 = resolved[p1]
            for p2 in names[i + 1 :]:
                a2, base2, pa2 = resolved[p2]
                if a1 == a2 and base1 == base2:
                    sig.append([p1, p2, pa2 - pa1])
        return sig

    def record(source, family, binds=None, kind="dsl", info=None):
        key = json.loads(rs.library_key(source, rs.machine_json(dev.machine)))["class"]
        entry = sources.setdefault(
            key, {"source": source, "family": family, "aliases": []}
        )
        if binds is not None:
            sig = _alias_sig(binds)
            if sig not in entry["aliases"]:
                entry["aliases"].append(sig)
        if observe_kernel is not None:
            observe_kernel(key, source, binds, kind, _CAPTURE_PHASE, _)
        launches.append(key)
        phases[_CAPTURE_PHASE] += 1
        if edge_events is not None:
            graph = json.loads(source)
            ports = []
            for port in graph["ports"]:
                entry, pa = dev.mmu.resolve(binds[port["name"]])
                spec = port["spec"]
                item = {"f16": 2, "f32": 4, "mx7a": 1, "mx7b": 1}[spec["dtype"]]
                ports.append(
                    dict(
                        port,
                        address=binds[port["name"]],
                        allocation=entry.virtual,
                        offset=pa - entry.physical,
                        nbytes=min(
                            math.prod(spec["shape"]) * item,
                            entry.extent - (pa - entry.physical),
                        ),
                    )
                )
            edge_events.append(
                {
                    "index": len(launches) - 1,
                    "key": key,
                    "name": graph["name"],
                    "family": family,
                    "kind": kind,
                    "info": json.loads(json.dumps(info, default=repr)),
                    "ports": ports,
                    "phase": _CAPTURE_PHASE,
                    "capture_pass": _,
                }
            )
        return D.Run(key, 0, 0.0)

    def launch(device, generator, arguments, bindings, kind="dsl", info=None):
        assert device is dev and dev.kind == "offline"
        launch_boundary.before_submit(device)
        try:
            source = generated.source(device, generator, arguments)
        except ValueError as error:
            error.add_note(f"generator {generator}: {arguments}")
            raise
        result = record(source, generator, bindings, kind, info)
        if edge_events is not None:
            edge_events[-1]["generator_arguments"] = arguments
        return result

    def specialise(device, kernel, given, **consts):
        traced = kernel.trace(given, machine=K.dsl_machine(device.machine), **consts)
        return SimpleNamespace(
            name=kernel.name, params=traced.params, source=json.dumps(traced.graph)
        )

    def run(device, compiled, binds, kind="dsl", info=None):
        assert device is dev and dev.kind == "offline"
        launch_boundary.before_submit(device)
        return record(compiled.source, "dsl", binds, kind, info)

    def forbidden(*args, **kwargs):
        raise AssertionError("trace-only capture invoked a compiler")

    with ExitStack() as stack:
        stack.enter_context(patch.object(generated, "launch", launch))
        stack.enter_context(patch.object(D.Device, "specialise", specialise))
        stack.enter_context(patch.object(D.Device, "run", run))
        stack.enter_context(patch.object(rs._rs, "compile", forbidden))
        stack.enter_context(patch.object(rs._rs, "compile_many", forbidden))
        # Passes until one learns no hint: it started from the hints a library
        # run starts from, so it made every kernel that run makes.
        learned = None
        for _ in range(CAPTURE_PASSES):
            MODELS[model](
                batch,
                forward_only,
                1 if forward_only else CAPTURE_STEPS,
                eval_batch=eval_batch,
                capture_tail=True,
                **recipe,
            )
            if not forward_only:
                previous_phases = phases.copy()
                MODELS[model](
                    batch,
                    forward_only,
                    3,
                    jit=True,
                    eval_batch=eval_batch,
                    capture_tail=True,
                    **recipe,
                )
                last_phases = dict(phases - previous_phases)
            now = dict(FM.hints(dev))
            if now == learned:
                break
            learned = now
        else:
            raise RuntimeError(
                f"layout hints still change after {CAPTURE_PASSES} passes"
            )
    return {
        "model": model,
        "batch": batch,
        "forward_only": forward_only,
        "eval_batch": eval_batch or batch,
        "phase_launches": dict(phases),
        "last_phase_launches": last_phases,
        "weight_decay": recipe.get("weight_decay", 0.0),
        "board": dev.machine.board,
        "sources": sources,
        "launches": launches,
        "layout_hints": dict(FM.hints(dev)),
    }


def _is_partial_output_reduction(gname, graph):
    if not gname.startswith("rows_"):
        return False
    body = graph.get("body", ())
    if len(body) > 1 or any(
        st.get("op") in ("buffer", "copy") for st in body if isinstance(st, dict)
    ):
        return True
    ports = graph.get("ports", ())
    if (
        len(ports) >= 3
        and ports[0]["name"] == "in0"
        and ports[1]["name"] == "in1"
        and ports[-1]["name"] == "out"
    ):
        s0 = ports[0]["spec"]["shape"]
        s1 = ports[1]["spec"]["shape"]
        so = ports[-1]["spec"]["shape"]
        if (
            len(s0) == len(so)
            and len(s1) == len(so)
            and s0[0] > so[0]
            and s0[0] == s1[0] + so[0]
        ):
            return True
    return False


def _classify_singleton_reason(gname, graph):
    if _is_partial_output_reduction(gname, graph):
        return "decomposition_differs:partial_output_reduction"
    prefix = gname.split("_")[0]
    body = graph.get("body", ())
    if prefix == "rows":
        if len(body) > 1 or any(
            st.get("op") in ("buffer", "copy") for st in body if isinstance(st, dict)
        ):
            return "decomposition_differs:partial_output_reduction"
        return "singleton_topology:rows_unique_expression_or_width"
    if prefix == "lanes":
        return "decomposition_differs:lanes_multiclass_monolithic"
    return f"singleton_topology:{prefix}"


def _derive_and_verify_group(group, machine):
    """Derive a shared template for `group`, verify confinement/coverage, and byte-prove all members."""
    p0_bytes = group[0]["pkg_bytes"]
    site_vals = defaultdict(lambda: [None] * len(group))
    site_meta = {}
    for i in range(1, len(group)):
        pi_bytes = group[i]["pkg_bytes"]
        d = rs._rs.template_derive(
            [p0_bytes, pi_bytes],
            json.dumps([{"i": 0}, {"i": 1}]),
            json.dumps({"i": {"lo": 0, "hi": 1}}),
            machine,
            values_json=json.dumps({"v": {"op": "param", "name": "i"}}),
        )
        t = json.loads(d["template_json"])
        for site in t["sites"]:
            skey = (
                site["name"],
                site.get("payload", 0),
                site.get("step"),
                site["bit"],
                site["width"],
                site.get("shift", 0),
                site.get("buffer"),
            )
            site_meta[skey] = site
            v0 = site["constant"]
            vi = site["constant"] + site["coefficients"].get("i", 0)
            site_vals[skey][0] = v0
            site_vals[skey][i] = vi
    for skey, vals in site_vals.items():
        v0 = vals[0]
        for i in range(len(group)):
            if vals[i] is None:
                vals[i] = v0
    dirs = {}
    sites_out = []
    for skey, vals in sorted(site_vals.items()):
        lo, hi = min(vals), max(vals)
        g_pos = math.gcd(*(v - lo for v in vals))
        u_pos = tuple((v - lo) // g_pos for v in vals)
        g_neg = math.gcd(*(hi - v for v in vals))
        u_neg = tuple((hi - v) // g_neg for v in vals)
        if u_pos in dirs:
            pname = dirs[u_pos]
            const, coef = lo, g_pos
        elif u_neg in dirs:
            pname = dirs[u_neg]
            const, coef = hi, -g_neg
        else:
            pname = f"p{len(dirs)}"
            dirs[u_pos] = pname
            const, coef = lo, g_pos
        meta = dict(site_meta[skey])
        meta["constant"] = const
        meta["coefficients"] = {pname: coef}
        sites_out.append(meta)
    parameters = {pname: {"lo": min(u), "hi": max(u)} for u, pname in dirs.items()}
    param_rows = [{pname: u[i] for u, pname in dirs.items()} for i in range(len(group))]
    buf_lists = [item["buffers"] for item in group]
    max_bufs = [
        max(bl[j]["size"] for bl in buf_lists) for j in range(len(buf_lists[0]))
    ]
    pair = None
    for ia in range(len(group)):
        for ib in range(ia + 1, len(group)):
            if all(vals[ia] != vals[ib] for vals in site_vals.values()) and all(
                max(buf_lists[ia][j]["size"], buf_lists[ib][j]["size"]) == max_bufs[j]
                for j in range(len(max_bufs))
            ):
                pair = (ia, ib)
                break
        if pair is not None:
            break
    if pair is None:
        raise ValueError("no pair spans all varying sites and max buffer extents")
    d_full = rs._rs.template_derive(
        [group[pair[0]]["pkg_bytes"], group[pair[1]]["pkg_bytes"]],
        json.dumps([{"i": 0}, {"i": 1}]),
        json.dumps({"i": {"lo": 0, "hi": 1}}),
        machine,
        values_json=json.dumps({"v": {"op": "param", "name": "i"}}),
    )
    raw = d_full["bytes"]
    t_full = json.loads(d_full["template_json"])
    t_full["parameters"] = parameters
    t_full["sites"] = sites_out
    pkg_info = rs._rs.package_read(raw, machine)
    bindings = [0x10000000 + i * 0x1000000 for i in range(len(pkg_info["buffers"]))]
    param_names = group[0]["output"]["packages"][0]["params"]
    for p1, p2, delta in group[0].get("alias_sig", ()):
        if p1 in param_names and p2 in param_names:
            i1 = param_names.index(p1)
            i2 = param_names.index(p2)
            bindings[i2] = bindings[i1] + delta
    table_json = json.dumps(t_full, separators=(",", ":"))
    t_env = dict(
        t_full,
        environment=json.loads(rs._rs.template_environment(raw, bindings, machine)),
    )
    table_env_json = json.dumps(t_env, separators=(",", ":"))
    rs._rs.template_certificate(raw, table_env_json, machine)
    rs._rs.template_verify(
        {0: raw},
        {0: table_env_json},
        {0: bindings},
        machine,
        group[0]["key"],
        confined=True,
    )
    for i, item in enumerate(group):
        t_member = dict(t_env, buffer_sizes=[bf["size"] for bf in buf_lists[i]])
        rs._rs.template_prove(
            raw,
            json.dumps(t_member, separators=(",", ":")),
            item["pkg_bytes"],
            json.dumps(param_rows[i]),
            bindings,
            machine,
            item["name"],
            0,
        )
    # Validate every captured environment before publishing any member of this group.
    verified_environments = {json.dumps(t_env["environment"])}
    for i, item in enumerate(group):
        for alternate in _environment_bindings(item, len(bindings)):
            environment = json.loads(
                rs._rs.template_environment(raw, alternate, machine)
            )
            alternate_table = dict(t_full, environment=environment)
            environment_key = json.dumps(environment)
            if environment_key not in verified_environments:
                rs._rs.template_verify(
                    {0: raw},
                    {0: json.dumps(alternate_table)},
                    {0: alternate},
                    machine,
                    item["key"],
                    confined=True,
                )
                verified_environments.add(environment_key)
            alternate_table["buffer_sizes"] = [b["size"] for b in buf_lists[i]]
            rs._rs.template_prove(
                raw,
                json.dumps(alternate_table),
                item["pkg_bytes"],
                json.dumps(param_rows[i]),
                alternate,
                machine,
                item["name"],
                0,
            )
    return raw, table_json, param_rows, bindings


def _environment_bindings(item, count):
    names = item["output"]["packages"][0]["params"]
    for alias in item.get("aliases", []):
        bindings = [0x10000000 + j * 0x1000000 for j in range(count)]
        for p1, p2, delta in alias:
            if p1 in names and p2 in names:
                bindings[names.index(p2)] = bindings[names.index(p1)] + delta
        yield bindings


def _admit_verified_group(group, raw, table_json, param_rows, bindings, store, clib):
    tid = store.admit(group[0]["source"], raw, table_json, bindings)
    for i, item in enumerate(group):
        ref = {
            0: {
                "source": group[0]["source"],
                "table": table_json,
                "params": param_rows[i],
            }
        }
        for alternate in _environment_bindings(item, len(bindings)):
            if alternate != bindings:
                store.admit(group[0]["source"], raw, table_json, alternate)
                clib.admit(item["source"], item["output"], ref, {0: alternate})
        clib.admit(item["source"], item["output"], ref, {0: bindings})
    return tid


def _partition_and_admit(bucket, store, clib, machine):
    if len(bucket) < 2:
        return [], [(x, "package_structure_differs") for x in bucket]
    try:
        raw, table_json, param_rows, bindings = _derive_and_verify_group(
            bucket, machine
        )
        tid = _admit_verified_group(
            bucket, raw, table_json, param_rows, bindings, store, clib
        )
        return [(tid, bucket, raw, table_json, param_rows, bindings)], []
    except (ValueError, RuntimeError, KeyError) as first_err:
        remaining = list(bucket)
        admitted_groups = []
        singletons = []
        while len(remaining) >= 2:
            head = remaining.pop(0)
            cur = [head]
            rest = []
            last_err = str(first_err)
            cur_verified = None
            for cand in remaining:
                try:
                    cur_verified = _derive_and_verify_group(cur + [cand], machine)
                    cur.append(cand)
                except (ValueError, RuntimeError, KeyError) as err:
                    last_err = str(err)
                    rest.append(cand)
            if len(cur) >= 2 and cur_verified is not None:
                raw, table_json, param_rows, bindings = cur_verified
                tid = _admit_verified_group(
                    cur, raw, table_json, param_rows, bindings, store, clib
                )
                admitted_groups.append(
                    (tid, cur, raw, table_json, param_rows, bindings)
                )
            else:
                reason = last_err.splitlines()[0][:120]
                singletons.append((head, f"verification_refusal:{reason}"))
            remaining = rest
        for r in remaining:
            reason = str(first_err).splitlines()[0][:120]
            singletons.append((r, f"verification_refusal:{reason}"))
        return admitted_groups, singletons


def prebuild_classes(library_root, doc, machine, exact_results, cold_times):
    started = time.perf_counter()
    store = TemplateStore(library_root, machine)
    clib = ClassLibrary(library_root, machine)
    by_struct = defaultdict(list)
    machine_digest = None
    for key, entry in doc["sources"].items():
        source = entry["source"]
        output = rs.library_get(str(library_root), source, machine)
        if output is None:
            raise RuntimeError(f"missing exact prebuild for {key}")
        g = json.loads(source)
        gname = g.pop("name", "")
        skey = digest(structure(g))
        if machine_digest is None:
            machine_digest = json.loads(rs.library_key(source, machine))["machine"]
        pkg_bytes = output["packages"][0]["bytes"]
        buffers = rs._rs.package_read(pkg_bytes, machine)["buffers"]
        aliases = entry.get("aliases", [])
        by_struct[skey].append(
            {
                "key": key,
                "name": gname,
                "family": entry["family"],
                "source": source,
                "output": output,
                "pkg_bytes": pkg_bytes,
                "buffers": buffers,
                "skey": skey,
                "aliases": aliases,
                "alias_sig": (tuple(tuple(x) for x in aliases[0]) if aliases else ()),
            }
        )

    admitted_sources = {}
    refused_sources = {}
    template_ids = {}
    dispatch_keys = set()

    for skey, members in sorted(by_struct.items()):
        active_members = []
        for item in members:
            g = json.loads(item["source"])
            aliases = item.get("aliases", [])
            if _is_partial_output_reduction(item["name"], g) or any(
                d != 0 for alias in aliases for _, _, d in alias
            ):
                refused_sources[item["key"]] = {
                    "name": item["name"],
                    "family": item["family"],
                    "reason": "decomposition_differs:partial_output_reduction",
                }
            else:
                active_members.append(item)
        if not active_members:
            continue
        if len(active_members) == 1:
            item = active_members[0]
            g = json.loads(item["source"])
            refused_sources[item["key"]] = {
                "name": item["name"],
                "family": item["family"],
                "reason": _classify_singleton_reason(item["name"], g),
            }
            continue
        buckets = []
        for item in active_members:
            placed = False
            p_item = item["pkg_bytes"]
            for b in buckets:
                if b[0]["alias_sig"] != item["alias_sig"]:
                    continue
                p0 = b[0]["pkg_bytes"]
                try:
                    rs._rs.template_derive(
                        [p0, p_item],
                        json.dumps([{"i": 0}, {"i": 1}]),
                        json.dumps({"i": {"lo": 0, "hi": 1}}),
                        machine,
                        values_json=json.dumps({"v": {"op": "param", "name": "i"}}),
                    )
                    b.append(item)
                    placed = True
                    break
                except ValueError:
                    pass
            if not placed:
                buckets.append([item])
        for b in buckets:
            if len(b) == 1:
                other = next(ob[0] for ob in buckets if ob is not b)
                prefix = b[0]["name"].split("_")[0]
                try:
                    rs._rs.template_derive(
                        [other["pkg_bytes"], b[0]["pkg_bytes"]],
                        json.dumps([{"i": 0}, {"i": 1}]),
                        json.dumps({"i": {"lo": 0, "hi": 1}}),
                        machine,
                        values_json=json.dumps({"v": {"op": "param", "name": "i"}}),
                    )
                    reason = f"package_structure_differs:{prefix}"
                except ValueError as err:
                    reason = f"package_structure_differs:{prefix}:{err}"
                refused_sources[b[0]["key"]] = {
                    "name": b[0]["name"],
                    "family": b[0]["family"],
                    "reason": reason,
                }
            else:
                groups, sings = _partition_and_admit(b, store, clib, machine)
                for tid, grp, raw, _, param_rows, bindings in groups:
                    template_ids[tid] = {
                        "representative": grp[0]["key"],
                        "members": [x["key"] for x in grp],
                        "pkg_bytes": len(raw),
                    }
                    dispatch_keys.add(skey)
                    for idx, x in enumerate(grp):
                        admitted_sources[x["key"]] = {
                            "name": x["name"],
                            "family": x["family"],
                            "template_id": tid,
                            "dispatch_key": skey,
                            "params": param_rows[idx],
                            "bindings": bindings,
                            "pkg_bytes": x["pkg_bytes"],
                        }
                for x, r in sings:
                    refused_sources[x["key"]] = {
                        "name": x["name"],
                        "family": x["family"],
                        "reason": r,
                    }

    # Retain certified environment variants for admitted sources across prebuilds.
    dispatch_dir = pathlib.Path(library_root) / machine_digest / "dispatch"
    for skey, members in by_struct.items():
        dpath = dispatch_dir / f"{skey}.json"
        if dpath.exists():
            dentry = json.loads(dpath.read_text(encoding="utf-8"))
            dentry.pop("integrity", None)
            for item in members:
                if item["key"] in refused_sources:
                    dentry.get("members", {}).pop(item["key"], None)
            for member, entry in dentry["members"].items():
                if member not in admitted_sources:
                    continue
                for variant in [entry, *entry.get("alternatives", [])]:
                    tid = variant["packages"]["0"]["template"]["id"]
                    if tid not in template_ids:
                        raw, _ = store.load_id(tid)
                        template_ids[tid] = {
                            "representative": member,
                            "members": [member],
                            "pkg_bytes": len(raw),
                        }
            dentry["integrity"] = digest(dentry)
            dpath.write_text(
                json.dumps(dentry, separators=(",", ":")), encoding="utf-8"
            )

    # Verify original-launch byte equivalence for EVERY launch in doc["launches"]
    launch_counts = Counter(doc["launches"])
    proven_launches = 0
    admitted_launch_count = 0
    refused_launch_count = 0
    refused_by_reason_sources = Counter()
    refused_by_reason_launches = Counter()

    for k, meta in refused_sources.items():
        r = meta["reason"]
        refused_by_reason_sources[r] += 1
        refused_by_reason_launches[r] += launch_counts[k]

    verified_keys = set()
    for k in doc["launches"]:
        source = doc["sources"][k]["source"]
        if k in admitted_sources:
            if k not in verified_keys:
                got = clib.lookup(source)
                if got is None:
                    raise RuntimeError(
                        f"admitted launch {k} missed ClassLibrary.lookup"
                    )
                pkg0 = got["packages"][0]
                meta = admitted_sources[k]
                rs._rs.template_prove(
                    pkg0["bytes"],
                    pkg0["template_json"],
                    meta["pkg_bytes"],
                    json.dumps(pkg0["template_params"]),
                    meta["bindings"],
                    machine,
                    meta["name"],
                    0,
                )
                verified_keys.add(k)
            admitted_launch_count += 1
            proven_launches += 1
        else:
            if k not in refused_sources:
                raise RuntimeError(f"launch {k} neither admitted nor refused")
            if k not in verified_keys:
                if clib.lookup(source) is not None:
                    raise RuntimeError(
                        f"refused launch {k} unexpectedly hit ClassLibrary"
                    )
                verified_keys.add(k)
            refused_launch_count += 1

    class_seconds = time.perf_counter() - started
    lib_dir = pathlib.Path(library_root) / machine_digest
    exact_total_bytes = sum(
        (lib_dir / f"{k}.json").stat().st_size for k in doc["sources"]
    )
    exact_pkg_bytes = sum(
        len(item["pkg_bytes"]) for members in by_struct.values() for item in members
    )
    class_template_bytes = sum(
        (lib_dir / "classes" / f"{tid}.json").stat().st_size for tid in template_ids
    )
    dispatch_bytes = 0
    for skey in dispatch_keys:
        dentry = json.loads(
            (lib_dir / "dispatch" / f"{skey}.json").read_text(encoding="utf-8")
        )
        own = {
            "key": dentry["key"],
            "members": {
                k: v for k, v in dentry["members"].items() if k in doc["sources"]
            },
        }
        own["integrity"] = digest(own)
        dispatch_bytes += len(json.dumps(own, separators=(",", ":")).encode("utf-8"))
    exact_fallback_bytes = sum(
        (lib_dir / f"{k}.json").stat().st_size for k in refused_sources
    )
    class_pkg_bytes = sum(t["pkg_bytes"] for t in template_ids.values()) + sum(
        len(item["pkg_bytes"])
        for members in by_struct.values()
        for item in members
        if item["key"] in refused_sources
    )
    exact_cold_s = sum(
        cold_times.get(k, exact_results[k]["seconds"]) for k in doc["sources"]
    )
    rep_keys = {t["representative"] for t in template_ids.values()} | set(
        refused_sources
    )
    rep_cold_s = sum(cold_times.get(k, exact_results[k]["seconds"]) for k in rep_keys)

    return {
        "admitted_sources": len(admitted_sources),
        "refused_sources": len(refused_sources),
        "admitted_launches": admitted_launch_count,
        "refused_launches": refused_launch_count,
        "byte_proven_launches": proven_launches,
        "class_templates": len(template_ids),
        "dispatch_files": len(dispatch_keys),
        "exact_fallback_entries": len(refused_sources),
        "total_entries_class_mode": len(template_ids) + len(refused_sources),
        "total_entries_exact_mode": len(doc["sources"]),
        "library_bytes": {
            "class_template_bytes": class_template_bytes,
            "dispatch_bytes": dispatch_bytes,
            "exact_fallback_bytes": exact_fallback_bytes,
            "total_class_mode_bytes": class_template_bytes
            + dispatch_bytes
            + exact_fallback_bytes,
            "total_exact_mode_bytes": exact_total_bytes,
            "package_binary_bytes_class_mode": class_pkg_bytes,
            "package_binary_bytes_exact_mode": exact_pkg_bytes,
        },
        "prebuild_seconds": {
            "class_derivation_and_proof_seconds": class_seconds,
            "representative_compile_plus_cert_seconds": rep_cold_s + class_seconds,
            "full_exact_plus_class_proof_seconds": exact_cold_s + class_seconds,
            "exact_cold_compile_seconds": exact_cold_s,
        },
        "refused_by_reason_sources": dict(refused_by_reason_sources.most_common()),
        "refused_by_reason_launches": dict(refused_by_reason_launches.most_common()),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", choices=list(MODELS), nargs="?")
    parser.add_argument("--model", choices=list(MODELS), dest="model_option")
    parser.add_argument("--eval-batch", type=int)
    parser.add_argument(
        "--schedule", choices=("constant", "cosine", "onecycle"), default="constant"
    )
    parser.add_argument("--warmup", type=int, default=0)
    parser.add_argument("--lr-min", type=float, default=0.0)
    parser.add_argument("--weight-decay", type=float, default=0.0)
    parser.add_argument("--batch", type=int, default=8)
    parser.add_argument("--forward-only", action="store_true")
    parser.add_argument("--board", default="multimesh_v9t6")
    parser.add_argument(
        "--library", type=pathlib.Path, default=ROOT / "build/templates"
    )
    parser.add_argument("--output", type=pathlib.Path, default=ROOT / "build/lib/exact")
    parser.add_argument(
        "--capture",
        type=pathlib.Path,
        help="read an existing capture without retracing",
    )
    parser.add_argument(
        "--mode", choices=["capture", "prebuild", "run"], default="prebuild"
    )
    parser.add_argument("--steps", type=int, default=1)
    parser.add_argument(
        "--jit", action="store_true", help="TinyJit and graph pin metrics"
    )
    parser.add_argument(
        "--fragment", action="store_true", help="unrelated heap history"
    )
    parser.add_argument("--classes", type=int, choices=[0, 1], default=None)
    parser.add_argument(
        "--zero-sign",
        nargs="+",
        choices=rs._rs.ZERO_SIGNS,
        default=[rs._rs.ZERO_SIGN],
        help="prebuild: library entries under each value; run: the one value run",
    )
    args = parser.parse_args()
    args.model = args.model_option or args.model
    if args.model is None:
        parser.error("a model is required")
    if args.mode == "run" and len(args.zero_sign) != 1:
        parser.error("--mode run runs under one --zero-sign value")
    recipe = {
        "eval_batch": args.eval_batch or args.batch,
        "schedule": args.schedule,
        "warmup": args.warmup,
        "lr_min": args.lr_min,
        "weight_decay": args.weight_decay,
    }
    if args.jit and (args.mode != "run" or args.steps < 6):
        parser.error("--jit requires --mode run and --steps >= 6")
    args.output.mkdir(parents=True, exist_ok=True)
    classes = None if args.classes is None else bool(args.classes)
    dev = D.attach(
        D.Device(
            kind="offline",
            board=args.board,
            library_only=args.mode == "run",
            library=args.library if args.mode == "run" else None,
            classes=classes,
        )
    )
    if args.mode == "run":
        with rs.zero_sign(args.zero_sign[0]):
            t0 = time.perf_counter()
            graphs = []
            graph_keys, replay_keys, source_keys = {}, {}, {}
            original_run = D.Device.run
            original = KTPUGraph.__call__

            def observe_run(device, compiled, *a, **kw):
                if compiled.name not in source_keys:
                    source_keys[compiled.name] = json.loads(
                        rs.library_key(
                            compiled.traced.graph_json, rs.machine_json(device.machine)
                        )
                    )["class"]
                return original_run(device, compiled, *a, **kw)

            def observe(graph, *a, **kw):
                if graph not in graphs:
                    graphs.append(graph)
                start = len(dev.runs)
                replayed = graph.counts["replayed"]
                result = original(graph, *a, **kw)
                if graph.counts["replayed"] > replayed:
                    assert len(dev.runs) - start == len(graph_keys[graph])
                    replay_keys.update(enumerate(graph_keys[graph], start))
                else:
                    graph_keys[graph] = [r.name for r in dev.runs[start:]]
                return result

            churn = (
                [dev.alloc(8192) for _ in range(args.steps)] if args.fragment else []
            )
            holes = (
                [dev.alloc(n) for n in (256, 4096, 256, 2048, 256, 8192)]
                if args.fragment
                else []
            )
            if args.fragment:
                for i in (1, 3, 5):
                    dev.free(holes[i])
            with (
                patch.object(KTPUGraph, "__call__", observe),
                patch.object(D.Device, "run", observe_run),
            ):
                step_records = MODELS[args.model](
                    args.batch,
                    args.forward_only,
                    args.steps,
                    jit=args.jit,
                    churn=churn,
                    **recipe,
                )
            dev.drain()
            wall = time.perf_counter() - t0
            assert dev.counters["compile_calls"] == 0
            assert dev.counters["library_misses"] == 0
            assert dev.counters["library_hits"] > 0
            if dev.classes:
                assert dev.counters["class_hits"] > 0
            else:
                assert dev.counters["class_hits"] == 0
                assert dev.counters["exact_hits"] == dev.counters["library_hits"]
            result = dict(
                dev.counters,
                classes=dev.classes,
                model=args.model,
                schedule=args.schedule,
                eval_batch=args.eval_batch or args.batch,
                step_records=step_records,
                launch_keys=[
                    source_keys[replay_keys.get(i, r.name)]
                    for i, r in enumerate(dev.runs)
                ],
                batch=args.batch,
                board=args.board,
                steps=args.steps,
                zero_sign=args.zero_sign[0],
                seconds=wall,
                ms_per_launch=wall * 1000.0 / max(1, dev.counters["runs"]),
            )
            if args.jit:
                refs = {ref for g in graphs if g.log for ref in g.log[1].references}
                placements = {
                    ref for g in graphs if g.placement for ref in g.placement.references
                }
                owners = Counter()
                for arena, at in placements:
                    owners[arena.owners[at].split(":")[0]] += arena.live[at]
                result["jit"] = {
                    "graphs": len(graphs),
                    "counts": {
                        k: sum(g.counts[k] for g in graphs)
                        for k in ("planned", "recorded", "replayed")
                    },
                    "placement_bytes": sum(a.live[at] for a, at in placements),
                    "placement_spans": len(placements),
                    "placement_by_owner": dict(owners),
                    "recording_bytes": sum(a.live[at] for a, at in refs),
                    "pinned_union_bytes": sum(
                        a.live[at] for a, at in refs | placements
                    ),
                    "misses": [g.misses for g in graphs],
                }
                assert graphs and result["jit"]["counts"]["replayed"] > 0
                for graph in graphs:
                    graph._release_logs()
                result["jit"]["placement_bytes_after_release"] = sum(
                    g.placement.nbytes for g in graphs if g.placement is not None
                )
            for at in churn:
                dev.free(at)
            for i, at in enumerate(holes):
                if not args.fragment or i not in (1, 3, 5):
                    dev.free(at)
            if isinstance(step_records, list) and len(step_records) > 1:
                warm = step_records[1:]
                warm_s = sum(r["seconds"] for r in warm)
                warm_n = sum(r["launches"] for r in warm)
                steady = step_records[4:] if len(step_records) > 4 else warm
                steady_s = sum(r["seconds"] for r in steady)
                steady_n = sum(r["launches"] for r in steady)
                result.update(
                    step0_seconds=step_records[0]["seconds"],
                    step0_ms_per_launch=step_records[0]["ms_per_launch"],
                    warm_steps=len(warm),
                    warm_seconds=warm_s,
                    warm_per_step_seconds=warm_s / len(warm),
                    warm_launches=warm_n,
                    warm_ms_per_launch=warm_s * 1000.0 / max(1, warm_n),
                    steady_ms_per_launch=steady_s * 1000.0 / max(1, steady_n),
                    step_seconds=[round(r["seconds"], 4) for r in step_records],
                )
            run_name = (
                "run.json"
                if args.classes is None
                else f"run-classes-{int(dev.classes)}.json"
            )
            (args.output / run_name).write_text(
                json.dumps(result, indent=2), encoding="utf-8"
            )
            if args.classes is not None:
                (
                    args.output
                    / f"run-steps{args.steps}-classes-{int(dev.classes)}.json"
                ).write_text(json.dumps(result, indent=2), encoding="utf-8")
                (args.output / "run.json").write_text(
                    json.dumps(result, indent=2), encoding="utf-8"
                )
            print(json.dumps({k: v for k, v in result.items() if k != "launch_keys"}))
            return
    cap = args.capture or args.output / "capture.json"
    if args.capture is not None and not cap.is_file():
        raise FileNotFoundError(cap)
    if args.capture is not None:
        doc = json.loads(cap.read_text(encoding="utf-8"))
        if (
            doc.get("model", "lenet") != args.model
            or doc["batch"] != args.batch
            or doc["board"] != args.board
            or doc.get("forward_only", False) != args.forward_only
            or doc.get("eval_batch", doc["batch"]) != (args.eval_batch or args.batch)
            or doc.get("weight_decay", 0.0) != args.weight_decay
        ):
            raise ValueError(
                "capture model/batch/eval-batch/board/weight-decay mismatch; choose another output directory"
            )
    else:
        FM.seed_hints(dev, args.library)
        doc = capture(dev, args.batch, args.forward_only, args.model, **recipe)
        cap.write_text(json.dumps(doc), encoding="utf-8")
    print(
        f"capture: {len(doc['launches'])} launches, {len(doc['sources'])} exact classes",
        flush=True,
    )
    if args.mode == "capture":
        return
    FM.save_hints(doc.get("layout_hints", {}), args.library)
    cold_path = args.library / "cold_compile_times.json"
    cold_times = (
        json.loads(cold_path.read_text(encoding="utf-8")) if cold_path.exists() else {}
    )
    started = time.perf_counter()
    machine_json = rs.machine_json(dev.machine)
    result = {
        "policy": "class+exact" if dev.classes else "exact graph",
        "batch": args.batch,
        "board": args.board,
        "zero_sign": {},
    }
    for zero_sign in args.zero_sign:
        result["zero_sign"][zero_sign] = prebuild_value(
            args, doc, dev, machine_json, zero_sign, cold_times
        )
        args.library.mkdir(parents=True, exist_ok=True)
        cold_path.write_text(json.dumps(cold_times, indent=2), encoding="utf-8")
    result["seconds"] = time.perf_counter() - started
    (args.output / "prebuild.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    failures = {
        z: len(v["failures"]) for z, v in result["zero_sign"].items() if v["failures"]
    }
    if failures:
        raise RuntimeError(
            f"classes refused per zero_sign {failures}; see prebuild.json"
        )


def prebuild_value(args, doc, dev, machine_json, zero_sign, cold_times):
    """The exact (and, with classes, the class) library entries under one
    zero_sign value; `cold_times[zero_sign]` holds its cold compile seconds."""
    started = time.perf_counter()
    results, failures = {}, {}
    cold = cold_times.setdefault(zero_sign, {})
    with rs.zero_sign(zero_sign):
        for idx, (key, entry) in enumerate(doc["sources"].items(), 1):
            try:
                res = rs.library_prebuild(args.library, entry["source"], machine_json)
                results[key] = res
                if not res["hit"]:
                    cold[f"{args.board}:{key}"] = res["seconds"]
                    cold[key] = res["seconds"]
            except ValueError as error:
                failures[key] = {"family": entry["family"], "error": str(error)}
            if idx % 25 == 0 or idx == len(doc["sources"]):
                print(
                    f"{zero_sign} {len(results) + len(failures)}/{len(doc['sources'])}: {entry['family']}: {'refused' if key in failures else 'ready'}",
                    flush=True,
                )
        value = {
            "seconds": time.perf_counter() - started,
            "results": results,
            "failures": failures,
        }
        if not failures and dev.classes:
            migration = prebuild_classes(args.library, doc, machine_json, results, cold)
            value["class_migration"] = migration
            print(
                f"{zero_sign} class migration: {migration['admitted_sources']}/{len(doc['sources'])} sources ({migration['admitted_launches']}/{len(doc['launches'])} launches) admitted into {migration['class_templates']} templates in {migration['prebuild_seconds']['class_derivation_and_proof_seconds']:.2f}s",
                flush=True,
            )
    return value


if __name__ == "__main__":
    main()
