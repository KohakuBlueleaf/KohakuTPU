"""Complete scheduled-L1 prices for graph-backed runtime witnesses."""

import json
from functools import lru_cache

from kohakutpu.stack import rs


@lru_cache(maxsize=128)
def scheduled_cycles(source, machine):
    return rs._rs.graph_cycles(source, machine)


def graph_cycles(graph, machine):
    return scheduled_cycles(json.dumps(graph, sort_keys=True), rs.machine_json(machine))


def prices(*runs, machine):
    return [graph_cycles(run.info["compiled"].traced.graph, machine) for run in runs]
