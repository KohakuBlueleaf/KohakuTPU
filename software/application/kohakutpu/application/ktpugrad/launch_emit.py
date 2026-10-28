"""Multi-output elementwise package emission for explicitly independent members."""

from kohakutpu.application.kernels import convert as C
from kohakutpu.application.kernels import expr as X
from kohakutpu.application.kernels import vector as V
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.runtime import forms as FM
from kohakutpu.application.runtime import rows as RW
from kohakutpu.language import dsl as kt


def _remap_expr_loads(e, mapping: dict):
    if isinstance(e, X.Load):
        return X.Load(mapping[e.i])
    if isinstance(e, X.Op):
        return X.Op(e.name, tuple(_remap_expr_loads(a, mapping) for a in e.args))
    if isinstance(e, X.Reduce):
        return X.Reduce(e.op, _remap_expr_loads(e.arg, mapping))
    return e


def _emit_flat_par(total: int, rows: int, cores: int, trip_fn) -> None:
    full, rem = divmod(total, rows)
    if full:
        ncore, start, trips = V.split(full, cores)
        for c in kt.par(ncore, on=kt.vc):
            for t in kt.range(trips(c)):
                trip_fn(start(c) * rows + t * rows, rows)
    r = full * rows
    for h in (rem // 4 * 4, rem % 4 & 2, rem % 2):
        if h:
            for _ in kt.par(1, on=kt.vc):
                for _ in kt.range(1):
                    trip_fn(r, h)
            r += h


def _emit_multi_rows(
    p_map, exprs, all_ops, in_keys, outs, out_keys, R: int, W: int, cores: int
) -> None:
    all_ops = tuple(all_ops)
    outs = tuple(outs)
    extra_fulls = tuple(Operand("full", pitch=W) for o in outs[1:] if o.kind == "full")
    extra_rows = tuple(Operand("row") for o in outs[1:] if o.kind == "row")
    q = V.STAT_ROWS if V.holds_stat(all_ops + extra_rows, outs[0].kind) else 4
    pl = V.plan(all_ops + extra_fulls + extra_rows, outs[0].kind, R, W, None, quantum=q)
    for sp, row0, rows in V.stages(pl, R, q):
        if rows is not None:
            for e_m, out_m, ok_m in zip(exprs, outs, out_keys):
                b_m = {f"in{i}": p_map[ik] for i, ik in enumerate(in_keys)}
                b_m["out"] = p_map[ok_m]
                V._tail_band(b_m, e_m, all_ops, out_m, sp, row0, rows, W)
            continue
        for cb in range(sp.nblk):
            n_inst, start, trips = V.split(sp.nb, cores)
            for c in kt.par(n_inst, on=kt.vc):
                col0 = cb * sp.wc
                pro = {}
                for i, o in enumerate(all_ops):
                    buf = p_map[in_keys[i]]
                    if o.kind == "col":
                        lo = o.offset + col0
                        pro[i] = kt.unpack(kt.load(buf[lo : lo + sp.wc], gt4=True))
                for t in kt.range(trips(c)):
                    r = start(c) * sp.rows + t * sp.rows + row0
                    vals = dict(pro)
                    for i, o in enumerate(all_ops):
                        buf = p_map[in_keys[i]]
                        if o.kind in ("full", "tile"):
                            rr, cc = divmod(o.offset, o.pitch)
                            view = buf[
                                rr + r : rr + r + sp.rows,
                                cc + col0 : cc + col0 + sp.wc,
                            ]
                            vals[i] = kt.load(view, gt4=True)
                        elif o.kind == "row":
                            rr = o.offset + r
                            vals[i] = kt.load(buf[rr : rr + sp.rows])
                        elif o.kind == "scalar":
                            w0 = o.offset // V.STAT_ROWS * V.STAT_ROWS
                            lane = o.offset % V.STAT_ROWS
                            vals[i] = kt.unpack(
                                kt.load(buf[w0 : w0 + V.STAT_ROWS]), lane=lane
                            )
                    for e_m, out_m, ok_m in zip(exprs, outs, out_keys):
                        v = X.emit(e_m, vals)
                        dst = p_map[ok_m]
                        if out_m.kind == "row":
                            kt.store(dst[r : r + sp.rows], v)
                        else:
                            rr, cc = divmod(out_m.offset, out_m.pitch)
                            V._store_over(dst, rr + r, cc + col0, sp.rows, sp.wc, v)


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
            one, _, _ = RW._launch(dev, expr, ops, binds, R, W, out_op, info, cores)
            return [one]
        if st0[0] == "copy":
            _, nbytes, src_a, dst_a, info = st0
            k, given = C.copy_kernel(nbytes)
            return [
                FM.launch(
                    dev, k, given, {"src": src_a, "dst": dst_a}, kind="copy", info=info
                )
            ]

    if RW.MUTANT == "drop_member" and len(stages) > 1:
        stages = stages[:-1]
    elif RW.MUTANT == "wrong_order" and len(stages) > 1:
        stages = list(reversed(stages))

    # 1. Identify group-local dead intermediate buffers that can be internal kt.buffer scratches.
    first_seen_as_write: dict = {}
    addr_shapes: dict = {}
    alias_addr: dict = {}
    filtered = []
    for st in stages:
        kind = st[0]
        if kind == "flat":
            _, expr, dts, n, out_dt, lanes, binds, info = st
            total = -(-n // 128)
            lane_map = dict(lanes)
            for i in range(len(dts)):
                a = alias_addr.get(binds[f"in{i}"], binds[f"in{i}"])
                binds[f"in{i}"] = a
                sh = (16,) if i in lane_map else (total, 128)
                first_seen_as_write.setdefault(a, False)
                addr_shapes.setdefault(a, set()).add((sh, dts[i]))
            oa = alias_addr.get(binds["out"], binds["out"])
            binds["out"] = oa
            first_seen_as_write.setdefault(oa, True)
            addr_shapes.setdefault(oa, set()).add(((total, 128), out_dt))
            filtered.append(st)
        elif kind == "rows":
            _, expr, ops, R, W, out_op, binds, info = st
            for i, o in enumerate(ops):
                a = alias_addr.get(binds[f"in{i}"], binds[f"in{i}"])
                binds[f"in{i}"] = a
                sh = V.port(o, R, W)[1].shape
                first_seen_as_write.setdefault(a, False)
                addr_shapes.setdefault(a, set()).add((sh, o.dtype))
            oa = alias_addr.get(binds["out"], binds["out"])
            binds["out"] = oa
            sh = V.port(out_op, R, W)[1].shape
            first_seen_as_write.setdefault(oa, True)
            addr_shapes.setdefault(oa, set()).add((sh, out_op.dtype))
            filtered.append(st)
        elif kind == "copy":
            _, nbytes, src_a, dst_a, info = st
            src_a = alias_addr.get(src_a, src_a)
            dst_a = alias_addr.get(dst_a, dst_a)
            first_seen_as_write.setdefault(src_a, False)
            first_seen_as_write.setdefault(dst_a, True)
            filtered.append(("copy", nbytes, src_a, dst_a, info))

    internal: dict = {}
    # A later immediate consumer may split this scope: intermediate stores must remain visible.

    # 2. Convert stages into normalized stage descriptors and fold copy of internal scratch into preceding flat stage.
    norm_stages: list = []
    for st in filtered:
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
                sh = internal[src_a][0] if src_a in internal else (-(-nbytes // 32), 16)
                norm_stages.append(
                    {
                        "kind": "copy",
                        "shape": sh,
                        "ins": [src_a],
                        "outs": [dst_a],
                    }
                )

    # 3. Co-schedule consecutive independent flat or rows stages on the same domain.
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

    # 4. Assign external ports and internal scratch names.
    written_addrs = {a for st in merged for a in st["outs"] if a not in internal}
    read_addrs = {a for st in merged for a in st["ins"] if a not in internal}
    port_of: dict = {}
    ports: dict = {}
    given: dict = {}
    port_binds: dict = {}
    scratch_specs: dict = {}
    staged_flat: dict = {}  # sname -> (pname, extent, is_in, is_out)

    def get_buf_name(
        addr: int, shape: tuple, dtype: str = "f16", n_flat: int | None = None
    ) -> str:
        if addr in internal:
            sname = f"s_{len(scratch_specs)}" if addr not in port_of else port_of[addr]
            if addr not in port_of:
                port_of[addr] = sname
                scratch_specs[sname] = internal[addr]
            return sname
        if n_flat is not None and n_flat % 128 != 0 and shape != (16,):
            item_b = 4 if dtype == "f32" else 2
            ext_w = -(-(n_flat * item_b) // 32)
            ext_shape = (ext_w, 32 // item_b)
            pkey = (addr, ext_shape, dtype)
            if pkey not in port_of:
                pname = f"p{len(ports)}"
                is_out = addr in written_addrs
                ports[pname] = (
                    kt.Out(*ext_shape, dtype=dtype)
                    if is_out
                    else kt.In(*ext_shape, dtype=dtype)
                )
                given[pname] = kt.TensorSpec.make(ext_shape, dtype)
                port_binds[pname] = addr
                sname = f"s_{len(scratch_specs)}"
                scratch_specs[sname] = (shape, dtype)
                staged_flat[sname] = (
                    pname,
                    ext_w,
                    addr in read_addrs,
                    is_out,
                )
                port_of[pkey] = sname
            return port_of[pkey]
        pkey = (addr, shape, dtype)
        if pkey not in port_of:
            pname = f"p{len(ports)}"
            port_of[pkey] = pname
            is_out = addr in written_addrs and shape != (16,)
            ports[pname] = (
                kt.Out(*shape, dtype=dtype) if is_out else kt.In(*shape, dtype=dtype)
            )
            given[pname] = kt.TensorSpec.make(shape, dtype)
            port_binds[pname] = addr
        return port_of[pkey]

    exec_plan: list = []
    sig_parts: list = []
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
            item = {"f16": 2, "f32": 4}
            words = sum(
                128 * item[d] // 32 for i, d in enumerate(st["dts"]) if i not in lanes
            )
            words += len(lanes) + len(out_names) * (128 * item[out_dt] // 32)
            rows = 16
            while rows > 1 and 2 * rows * words > V.L1_WORDS - V.L1_MARGIN:
                rows //= 2
            exec_plan.append(
                (
                    "flat",
                    total,
                    rows,
                    tuple(st["exprs"]),
                    tuple(in_names),
                    tuple(sorted(lanes.items())),
                    tuple(out_names),
                )
            )
            sig_parts.append(
                (
                    "flat",
                    total,
                    rows,
                    tuple(V.expr_key(e) for e in st["exprs"]),
                    tuple(in_names),
                    tuple(sorted(lanes.items())),
                    tuple(out_names),
                )
            )
        elif st["kind"] == "rows":
            R, W = st["R"], st["W"]
            ops = tuple(st["ops"])
            out_ops = tuple(st["out_ops"])
            in_names = [
                get_buf_name(a, V.port(o, R, W)[1].shape, o.dtype)
                for a, o in zip(st["ins"], ops)
            ]
            out_names = [
                get_buf_name(oa, V.port(oo, R, W)[1].shape, oo.dtype)
                for oa, oo in zip(st["outs"], out_ops)
            ]
            exec_plan.append(
                (
                    "rows",
                    R,
                    W,
                    tuple(st["exprs"]),
                    ops,
                    tuple(in_names),
                    out_ops,
                    tuple(out_names),
                )
            )
            sig_parts.append(
                (
                    "rows",
                    R,
                    W,
                    tuple(V.expr_key(e) for e in st["exprs"]),
                    ops,
                    tuple(in_names),
                    out_ops,
                    tuple(out_names),
                )
            )
        elif st["kind"] == "copy":
            sh = st["shape"]
            sname = get_buf_name(st["ins"][0], sh, "f16")
            dname = get_buf_name(st["outs"][0], sh, "f16")
            exec_plan.append(("copy", sname, dname))
            sig_parts.append(("copy", sh, sname, dname))

    kkey = (
        "rows_fused_multi",
        tuple(sig_parts),
        tuple(sorted(scratch_specs.items())),
        tuple(sorted(staged_flat.items())),
        tuple((k, v.shape, v.dtype, ports[k].role) for k, v in given.items()),
        cores,
        RW.MUTANT,
        kt.Kernel.build,
    )
    hit = V._kernels.get(kkey)
    if hit is not None:
        V._kernels.move_to_end(kkey)
        k_built, given_built = hit
    else:

        def body(**kw):
            env_map = dict(kw)
            for sname, (sh, dt) in scratch_specs.items():
                env_map[sname] = kt.buffer(sh, dt)
            for sname, (pname, ext_w, is_in, _) in staged_flat.items():
                if is_in:
                    s_buf = env_map[sname]
                    kt.fill_value(s_buf, 0)
                    w_spec = ((ext_w, 32),)
                    kt.copy(s_buf, env_map[pname], walk=w_spec, dwalk=w_spec)
            for step_item in exec_plan:
                skind = step_item[0]
                if skind == "flat":
                    _, total, rows, exprs, in_names, lanes_tup, out_names = step_item
                    lane_dict = dict(lanes_tup)

                    def trip(
                        r,
                        h,
                        _in=in_names,
                        _lanes=lane_dict,
                        _exprs=exprs,
                        _out=out_names,
                    ):
                        vals = {}
                        for i, ik in enumerate(_in):
                            if i in _lanes:
                                vals[i] = kt.unpack(
                                    kt.load(env_map[ik][0:16]), lane=_lanes[i]
                                )
                            else:
                                vals[i] = kt.load(env_map[ik][r : r + h, :])
                        for e_m, ok in zip(_exprs, _out):
                            kt.store(env_map[ok][r : r + h, :], X.emit(e_m, vals))

                    _emit_flat_par(total, rows, cores, trip)
                elif skind == "rows":
                    _, R, W, exprs, ops, in_names, out_ops, out_names = step_item
                    if len(exprs) == 1:
                        b_m = {f"in{i}": env_map[ik] for i, ik in enumerate(in_names)}
                        b_m["out"] = env_map[out_names[0]]
                        RW.emit_rows_stages(b_m, exprs[0], ops, R, W, out_ops[0], cores)
                    else:
                        _emit_multi_rows(
                            env_map,
                            exprs,
                            ops,
                            in_names,
                            out_ops,
                            out_names,
                            R,
                            W,
                            cores,
                        )
                elif skind == "copy":
                    _, sname, dname = step_item
                    kt.copy(env_map[dname], env_map[sname])
            for sname, (pname, ext_w, _, is_out) in staged_flat.items():
                if is_out:
                    w_spec = ((ext_w, 32),)
                    kt.copy(env_map[pname], env_map[sname], walk=w_spec, dwalk=w_spec)

        k_built = kt.Kernel.build(
            V.name("rows", "fused", sig_parts, cores, RW.MUTANT), body, ports
        )
        given_built = given
        if V.KERNELS_KEPT > 0:
            V._kernels[kkey] = (k_built, given_built)
            while len(V._kernels) > V.KERNELS_KEPT:
                V._kernels.popitem(last=False)

    return [FM.launch(dev, k_built, given_built, port_binds, info=scope.info)]
