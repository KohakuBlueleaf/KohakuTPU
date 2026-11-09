"""Explicit input boundaries and global enclosing intervals, not local maxima."""

import json
import time
from contextlib import contextmanager
from types import SimpleNamespace

from h import meta

from kohakutpu.application.runtime import device as D


class Inputs:
    def __init__(self, trace):
        self.trace = trace
        self.rows = []
        self.stream = (trace.out / "inputs.jsonl").open("w", encoding="utf-8")
        self.timebases = (trace.out / "timebase.jsonl").open("w", encoding="utf-8")

    @staticmethod
    def timebase(dev):
        card = dev.card
        windows = getattr(card, "_perf_windows", None)
        if windows is not None:
            card = SimpleNamespace(
                nodes=list(windows),
                boots={n: (SimpleNamespace(win=w),) for n, w in windows.items()},
            )
        if not hasattr(card, "boots"):
            return None
        return meta.timebase(card)

    @contextmanager
    def __call__(self, index, group):
        dev = D.device()
        dev.drain()
        flush = getattr(dev, "_perf_flush", None)
        if flush is not None:
            flush()
        before = self.timebase(dev)
        row = {
            "input": len(self.rows),
            "index": index,
            "group": group,
            "first_seq": self.trace.count,
            "complete": False,
        }
        self.trace.current_input = row["input"]
        start = time.perf_counter_ns()
        try:
            yield
            dev.drain()
            row["complete"] = True
        finally:
            end = time.perf_counter_ns()
            row.update(
                end_seq=self.trace.count,
                host_start_ns=start,
                host_end_ns=end,
                global_host_bound_ns=end - start,
                global_scope="input upload through all-node drain; includes host and output readback when requested",
            )
            self.trace.current_input = None
            # Never interrogate a failed/possibly live card for a timing sample.
            after = self.timebase(dev) if row["complete"] else None
            if before and after:
                # HR_CYCLES epochs differ across nodes. These two samples are
                # on ONE reference node and enclose every participant's work.
                ref = next(iter(before))
                left = list(before.values())[-1]["first"][1]
                right = after[ref]["first"][0]
                if right < left:
                    raise ValueError("reference HR_CYCLES reset during input")
                row.update(
                    global_cycles_bound=right - left,
                    reference_node=ref,
                    global_clock="reference HR_CYCLES, enclosing bound, not busy makespan",
                )
                self.timebases.write(
                    json.dumps(
                        {
                            "input": row["input"],
                            "before": before,
                            "after": after,
                            "drift": meta.drift(before, after),
                        }
                    )
                    + "\n"
                )
                self.timebases.flush()
            self.rows.append(row)
            self.stream.write(json.dumps(row) + "\n")
            self.stream.flush()
            if row["complete"] and flush is not None:
                flush()

    def close(self):
        self.stream.close()
        self.timebases.close()
        return {
            "inputs_completed": sum(r["complete"] for r in self.rows),
            "input_intervals": len(self.rows),
        }
