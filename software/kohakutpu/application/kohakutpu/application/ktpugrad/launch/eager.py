"""Schedule-preserving eager horizontal groups and intermediate lifetimes."""

from tinygrad.device import Buffer
from tinygrad.engine import realize
from tinygrad.helpers import perf_counter_us
from tinygrad.uop import Ops

from kohakutpu.application.ktpugrad import run as R
from kohakutpu.application.ktpugrad.captured import recipe
from kohakutpu.application.ktpugrad.launch import schedule as launch_schedule
from kohakutpu.application.ktpugrad.planning.plans import PLANS
from kohakutpu.application.runtime import chain


def _own_spans(b: Buffer) -> set:
    """The bytes `b` owns: its own range of its storage, the storage's forms
    too where `b` is the whole storage."""
    st, off = R.FM.base_of(b._buf)
    if off or b.nbytes != st.nbytes:
        return {(st.addr + off, st.addr + off + b.nbytes)}
    return launch_schedule.spans(b._buf)


def try_group(linear, ctx, used, inputs, held, rt, _on_ktpu):
    def _is_ktpu_plan_call(c) -> bool:
        return (
            c.op is Ops.CALL
            and c.src[0].op is Ops.PROGRAM
            and len(c.src[0].src) >= 4
            and c.src[0].src[3].arg in PLANS
        )

    can_fuse = (
        R.horizontal_fusion_enabled()
        and rt is not None
        and not (rt.kind != "offline" and R._product.__name__ != "_product")
        and sum(1 for c in linear.src if _is_ktpu_plan_call(c)) > 1
    )
    if can_fuse:
        call_indices = [i for i, c in enumerate(linear.src) if _is_ktpu_plan_call(c)]
        sink_indices = [i for i, c in enumerate(linear.src) if c.op is Ops.SINK]
        other_indices = [
            i
            for i, c in enumerate(linear.src)
            if c.op is not Ops.SINK and not _is_ktpu_plan_call(c)
        ]
        ktpu_written = {id(used[i][0]) for i in call_indices if used[i]}
        other_reads_ktpu = any(
            id(b) in ktpu_written for i in other_indices for b in used[i][1:]
        )
        if not other_reads_ktpu:
            for orig_i in other_indices:
                call = linear.src[orig_i]
                ets = realize.pm_exec.rewrite(call, ctx) or []
                realize.track_stats(ctx, call, perf_counter_us(), ets)
            sched_items = []
            named = set()
            for idx in call_indices:
                c = linear.src[idx]
                p = PLANS[c.src[0].src[3].arg]
                slots = sorted(u.arg.slot for u in c.src[0].src[1].src)
                bs = [u.buffer for u in realize.resolve_params(c, inputs)]
                by_slot = {}
                for slot_num, b in zip(slots, bs):
                    if isinstance(b, Buffer) and _on_ktpu(b.device):
                        b.ensure_allocated()
                        by_slot[slot_num] = b._buf
                        if id(b.base) in held:
                            named.add(id(R.FM.base_of(b._buf)[0]))
                sched_items.append((p, by_slot))
            units = R.units(sched_items, named)
            groups = R.plan_schedule([(p, by_slot) for _, p, by_slot in units])
            last_grp = {}
            for orig_i in other_indices:
                for b in used[orig_i]:
                    last_grp[id(b)] = (-1, b)
            for g_idx, (grp, _) in enumerate(groups):
                for unit in grp:
                    for local_i in units[unit][0]:
                        orig_i = call_indices[local_i]
                        for b in used[orig_i]:
                            last_grp[id(b)] = (g_idx, b)
            dead_by_grp: dict = {}
            for k, (g_idx, b) in last_grp.items():
                if k not in held:
                    dead_by_grp.setdefault(g_idx, []).append(b)
            for b in dead_by_grp.get(-1, ()):
                if b.is_allocated() and not b.allocated_views:
                    b.deallocate()

            def run_unit(unit):
                members, plan, by_slot = units[unit]
                if len(members) > 1 or plan is not sched_items[members[0]][0]:
                    recipe.submit(rt, plan, by_slot)
                    return
                call = linear.src[call_indices[members[0]]]
                ets = realize.pm_exec.rewrite(call, ctx) or []
                realize.track_stats(ctx, call, perf_counter_us(), ets)

            with chain.chaining(rt) as chained:
                for g_idx, (grp, dead_addrs) in enumerate(groups):
                    # What no later call reads: an output the package need not store.
                    dead = set(dead_addrs).union(
                        *(
                            _own_spans(b)
                            for b in dead_by_grp.get(g_idx, ())
                            if b.is_allocated() and not b.allocated_views
                        )
                    )
                    if chained is not None:
                        chained.dead = dead
                    if len(grp) == 1:
                        run_unit(grp[0])
                    else:
                        recipe.submit_group(
                            rt,
                            [units[unit][1:] for unit in grp],
                            dead,
                            {"plan": units[grp[-1]][1].name},
                        )
                    for b in dead_by_grp.get(g_idx, ()):
                        if b.is_allocated() and not b.allocated_views:
                            b.deallocate()
            for orig_i in sink_indices:
                call = linear.src[orig_i]
                ets = realize.pm_exec.rewrite(call, ctx) or []
                realize.track_stats(ctx, call, perf_counter_us(), ets)
            return True

    return False
