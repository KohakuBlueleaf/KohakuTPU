"""Multi-output elementwise package emission for explicitly independent members."""

from kohakutpu.application.kernels import expr as X
from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime import rows as RW


def _remap_expr_loads(e, mapping: dict):
    if isinstance(e, X.Load):
        return X.Load(mapping[e.i])
    if isinstance(e, X.Op):
        return X.Op(e.name, tuple(_remap_expr_loads(a, mapping) for a in e.args))
    if isinstance(e, X.Reduce):
        return X.Reduce(e.op, _remap_expr_loads(e.arg, mapping))
    return e


def flush_multi_stage(scope) -> list:
    """Compile and launch all stages recorded in `scope` as one DSL package."""
    if scope.flushed or not scope.stages:
        return []
    scope.flushed = True
    dev = scope.dev
    cores = dev.machine.count("VC")
    stages = list(scope.stages)
    if len(stages) == 1 and RW.MUTANT is None:
        st0 = stages[0]
        if st0[0] == "flat":
            _, expr, dts, n, _, lanes, binds, info = st0
            return [
                FM.generated.flat(dev, expr, dts, n, binds, info=info, scalars=lanes)
            ]
        if st0[0] == "rows":
            _, expr, ops, R, W, out_op, binds, info = st0
            return [RW._launch(dev, expr, ops, binds, R, W, out_op, info, cores)]
        if st0[0] == "copy":
            _, nbytes, src_a, dst_a, info = st0
            return [
                FM.generated.copy(dev, nbytes, {"src": src_a, "dst": dst_a}, info=info)
            ]

    if RW.MUTANT == "drop_member" and len(stages) > 1:
        stages = stages[:-1]
    elif RW.MUTANT == "wrong_order" and len(stages) > 1:
        stages = list(reversed(stages))

    # 1. Stage descriptors, a copy of a flat stage's read folded into it
    # (a later consumer may split the scope: intermediate stores stay visible).
    norm_stages: list = []
    for st in stages:
        kind = st[0]
        if kind == "flat":
            _, expr, dts, n, out_dt, lanes, binds, info = st
            total = -(-n // 128)
            in_list = [binds[f"in{i}"] for i in range(len(dts))]
            norm_stages.append(
                {
                    "kind": "flat",
                    "n": n,
                    "total": total,
                    "out_dt": out_dt,
                    "exprs": [expr],
                    "dts": list(dts),
                    "lanes": dict(lanes),
                    "ins": in_list,
                    "outs": [binds["out"]],
                }
            )
        elif kind == "rows":
            _, expr, ops, R, W, out_op, binds, info = st
            in_list = [binds[f"in{i}"] for i in range(len(ops))]
            norm_stages.append(
                {
                    "kind": "rows",
                    "R": R,
                    "W": W,
                    "exprs": [expr],
                    "ops": list(ops),
                    "out_ops": [out_op],
                    "ins": in_list,
                    "outs": [binds["out"]],
                }
            )
        elif kind == "copy":
            _, nbytes, src_a, dst_a, info = st
            if (
                norm_stages
                and norm_stages[-1]["kind"] == "flat"
                and src_a in norm_stages[-1]["ins"]
                and len(norm_stages[-1]["outs"]) == 1
                and dst_a not in norm_stages[-1]["outs"]
            ):
                prev = norm_stages[-1]
                load_idx = prev["ins"].index(src_a)
                prev["exprs"].append(X.Load(load_idx))
                prev["outs"].append(dst_a)
            else:
                norm_stages.append(
                    {
                        "kind": "copy",
                        "shape": (-(-nbytes // 32), 16),
                        "ins": [src_a],
                        "outs": [dst_a],
                    }
                )

    # 2. Co-schedule consecutive independent flat or rows stages on the same domain.
    merged: list = []
    for st in norm_stages:
        if (
            merged
            and merged[-1]["kind"] == "flat"
            and st["kind"] == "flat"
            and merged[-1]["n"] == st["n"]
            and merged[-1]["out_dt"] == st["out_dt"]
            and len(merged[-1]["outs"]) == 1
            and len(st["outs"]) == 1
            and st["outs"][0] not in merged[-1]["outs"]
            and st["outs"][0] not in merged[-1]["ins"]
            and merged[-1]["outs"][0] not in st["ins"]
            and RW.MUTANT != "wrong_order"
        ):
            prev = merged[-1]
            # Merge inputs
            comb_dts = list(prev["dts"])
            comb_lanes = dict(prev["lanes"])
            comb_ins = list(prev["ins"])
            mapping = {}
            for i, (a, dt) in enumerate(zip(st["ins"], st["dts"])):
                l_val = st["lanes"].get(i)
                found = None
                for j, (pa, pdt) in enumerate(zip(comb_ins, comb_dts)):
                    if pa == a and pdt == dt and comb_lanes.get(j) == l_val:
                        found = j
                        break
                if found is None:
                    found = len(comb_ins)
                    comb_ins.append(a)
                    comb_dts.append(dt)
                    if l_val is not None:
                        comb_lanes[found] = l_val
                mapping[i] = found
            full_cnt = (
                sum(1 for j in range(len(comb_ins)) if j not in comb_lanes)
                + len(prev["outs"])
                + 1
            )
            if full_cnt <= 5:
                prev["dts"] = comb_dts
                prev["lanes"] = comb_lanes
                prev["ins"] = comb_ins
                prev["exprs"].append(_remap_expr_loads(st["exprs"][0], mapping))
                prev["outs"].append(st["outs"][0])
                continue
        if (
            merged
            and merged[-1]["kind"] == "rows"
            and st["kind"] == "rows"
            and merged[-1]["R"] == st["R"]
            and merged[-1]["W"] == st["W"]
            and len(merged[-1]["outs"]) == 1
            and len(st["outs"]) == 1
            and merged[-1]["out_ops"][0].kind == "full"
            and st["out_ops"][0].kind == "full"
            and merged[-1]["out_ops"][0].offset == st["out_ops"][0].offset == 0
            and st["outs"][0] not in merged[-1]["outs"]
            and st["outs"][0] not in merged[-1]["ins"]
            and merged[-1]["outs"][0] not in st["ins"]
            and RW.MUTANT != "wrong_order"
        ):
            prev = merged[-1]
            comb_ops = list(prev["ops"])
            comb_ins = list(prev["ins"])
            mapping = {}
            for i, (a, op) in enumerate(zip(st["ins"], st["ops"])):
                found = None
                for j, (pa, pop) in enumerate(zip(comb_ins, comb_ops)):
                    if pa == a and pop == op:
                        found = j
                        break
                if found is None:
                    found = len(comb_ins)
                    comb_ins.append(a)
                    comb_ops.append(op)
                mapping[i] = found
            if len(comb_ops) + 2 <= 7:
                prev["ops"] = comb_ops
                prev["ins"] = comb_ins
                prev["exprs"].append(_remap_expr_loads(st["exprs"][0], mapping))
                prev["out_ops"].append(st["out_ops"][0])
                prev["outs"].append(st["outs"][0])
                continue
        merged.append(st)

    if RW.MUTANT == "alias_members" and len(merged) >= 1:
        if len(merged[0]["outs"]) >= 2:
            merged[0]["outs"][1] = merged[0]["outs"][0]
        elif len(merged) >= 2:
            merged[1]["outs"][0] = merged[0]["outs"][0]

    # 3. Assign ports and scratch names.
    written_addrs = {a for st in merged for a in st["outs"]}
    read_addrs = {a for st in merged for a in st["ins"]}
    port_of: dict = {}
    ports: list = []
    port_binds: dict = {}
    scratch: list = []
    staged: list = []

    def port(addr: int, shape: tuple, dtype: str, written: bool) -> str:
        name = f"p{len(ports)}"
        ports.append({"name": name, "shape": shape, "dtype": dtype, "written": written})
        port_binds[name] = addr
        return name

    def get_buf_name(
        addr: int, shape: tuple, dtype: str = "f16", n_flat: int | None = None
    ) -> str:
        if n_flat is not None and n_flat % 128 != 0 and shape != (16,):
            item_b = 4 if dtype == "f32" else 2
            ext_w = -(-(n_flat * item_b) // 32)
            ext_shape = (ext_w, 32 // item_b)
            pkey = (addr, ext_shape, dtype)
            if pkey not in port_of:
                is_out = addr in written_addrs
                sname = f"s_{len(scratch)}"
                scratch.append({"name": sname, "shape": shape, "dtype": dtype})
                staged.append(
                    {
                        "scratch": sname,
                        "port": port(addr, ext_shape, dtype, is_out),
                        "words": ext_w,
                        "read": addr in read_addrs,
                        "written": is_out,
                    }
                )
                port_of[pkey] = sname
            return port_of[pkey]
        pkey = (addr, shape, dtype)
        if pkey not in port_of:
            is_out = addr in written_addrs and shape != (16,)
            port_of[pkey] = port(addr, shape, dtype, is_out)
        return port_of[pkey]

    steps: list = []
    for st in merged:
        if st["kind"] == "flat":
            n_el = st["n"]
            total = st["total"]
            out_dt = st["out_dt"]
            lanes = st["lanes"]
            in_names = [
                get_buf_name(
                    a,
                    (16,) if i in lanes else (total, 128),
                    st["dts"][i],
                    None if i in lanes else n_el,
                )
                for i, a in enumerate(st["ins"])
            ]
            out_names = [
                get_buf_name(oa, (total, 128), out_dt, n_el) for oa in st["outs"]
            ]
            steps.append(
                {
                    "kind": "flat",
                    "total": total,
                    "dtypes": st["dts"],
                    "out_dtype": out_dt,
                    "exprs": [FM.generated.expr(e) for e in st["exprs"]],
                    "ins": in_names,
                    "lanes": sorted(lanes.items()),
                    "outs": out_names,
                }
            )
        elif st["kind"] == "rows":
            R, W = st["R"], st["W"]
            in_names = [
                get_buf_name(a, FM.generated.port_shape(dev, o, R, W), o.dtype)
                for a, o in zip(st["ins"], st["ops"])
            ]
            out_names = [
                get_buf_name(oa, FM.generated.port_shape(dev, oo, R, W), oo.dtype)
                for oa, oo in zip(st["outs"], st["out_ops"])
            ]
            steps.append(
                {
                    "kind": "rows",
                    "R": R,
                    "W": W,
                    "exprs": [FM.generated.expr(e) for e in st["exprs"]],
                    "operands": [vars(o) for o in st["ops"]],
                    "ins": in_names,
                    "out_operands": [vars(o) for o in st["out_ops"]],
                    "outs": out_names,
                }
            )
        elif st["kind"] == "copy":
            sh = st["shape"]
            steps.append(
                {
                    "kind": "copy",
                    "src": get_buf_name(st["ins"][0], sh),
                    "dst": get_buf_name(st["outs"][0], sh),
                }
            )

    package = {"ports": ports, "scratch": scratch, "staged": staged, "steps": steps}
    return [
        FM.generated.launch(dev, "fused.kernel", package, port_binds, info=scope.info)
    ]
