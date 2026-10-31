"""A pytest plugin: every kernel a double compiles lands in `KTPU_HARVEST` as an
L2 fixture, so the corpus of L2 to L1 holds what the planner really emits."""

import os
import pathlib

from kohakutpu.application.runtime.double import DoubleCard

DIR = pathlib.Path(os.environ["KTPU_HARVEST"])
compile_graph = DoubleCard.compile_graph


def harvested(self, source):
    out = compile_graph(self, source)
    name = out["name"]
    folder = DIR / name.split("_")[0]
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.ktpu").write_text(out["l2"], encoding="utf-8")
    return out


DoubleCard.compile_graph = harvested
