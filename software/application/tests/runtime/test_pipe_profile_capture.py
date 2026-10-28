"""Prebuild implicit output is not a reusable capture across profile settings."""

import json
import runpy
import sys
from types import SimpleNamespace
from unittest.mock import Mock

from kohakutpu.application.kernels import matmul as MM
from kohakutpu.application.kernels.vector import Operand
from kohakutpu.application.runtime import card as C
from kohakutpu.application.runtime import costs
from kohakutpu.application.runtime.device import ROOT
from kohakutpu.application.runtime.offline import HostMemory, OfflineCard
from kohakutpu.stack import kernel as K
from kohakutpu.stack import rs


def test_prebuild_retraces_profile_for_enumerated_runtime(tmp_path, monkeypatch):
    offline = OfflineCard("multimesh_v9t6")
    mesh = SimpleNamespace(
        index=0,
        caps=SimpleNamespace(grid_hi=2),
        coords=lambda kind: list(offline.machine.meshes[0].units.get(kind, ())),
    )
    # Only transport/boot are substituted: NodeCard's enumeration, machine
    # construction, unit checks and memory setup are the real constructor.
    monkeypatch.setattr(
        C.Card, "from_board", lambda *a, **kw: SimpleNamespace(meshes=[mesh])
    )
    monkeypatch.setattr(C, "UnitGlobal", lambda *a, **kw: HostMemory())
    monkeypatch.setattr(C, "NodeBoot", Mock())
    monkeypatch.setattr(
        C, "node_queue", lambda mem, base, *a, **kw: SimpleNamespace(base=base)
    )
    monkeypatch.setattr(C.NodeCard, "load", lambda *a, **kw: None)
    runtime = C.NodeCard(None, SimpleNamespace(line=32), "multimesh_v9t6")
    assert offline.machine.board == runtime.machine.board == "multimesh_v9t6"

    src = Operand("full", pitch=12544)
    args = (25, 12544, 28, 12544, "a", 7, 2, 1)
    recipe = costs.quant_recipe(src, *args)
    profile = tmp_path / "costs.json"
    profile.write_text(
        json.dumps(
            dict(
                schedule=dict(finite_batch=4, overlap=True),
                recipes={
                    costs.key(
                        "staged_padding", dict(board="multimesh_v9t6", recipe=recipe)
                    ): dict(original=834959, alternative=621814, verified=True)
                },
            )
        )
    )
    monkeypatch.setenv("KTPU_COST_PROFILE", str(profile))
    monkeypatch.setenv("KTPU_PIPE_FINITE_BATCH", "4")
    monkeypatch.setenv("KTPU_PIPE_OVERLAP", "1")
    monkeypatch.setenv("KTPU_WINDOW_SWAP", "0")

    def source(machine, strategy):
        k, given = MM.quant_kernel(src, *args, pad_strategy=strategy)
        graph = json.dumps(k.trace(given, machine=K.dsl_machine(machine)).graph)
        return json.loads(rs._rs.library_key(graph, rs.machine_json(machine)))

    old = source(offline.machine, "native")
    expected = source(runtime.machine, "staged")
    assert old != expected
    target = tmp_path / "capture.json"
    target.write_text(
        json.dumps(
            dict(
                model="lenet",
                batch=8,
                board="multimesh_v9t6",
                sources={old["class"]: {}},
                launches=[old["class"]],
            )
        )
    )
    observed = []

    def capture(dev, batch, forward_only, model, **recipe_args):
        assert costs.choose("staged_padding", recipe, dev.machine.board)
        assert costs.choose("staged_padding", recipe, runtime.machine.board)
        selected = source(dev.machine, "staged")
        observed.append(selected)
        return dict(
            model=model,
            batch=batch,
            board=dev.machine.board,
            sources={selected["class"]: {}},
            launches=[selected["class"]],
        )

    prebuild = runpy.run_path(str(ROOT / "scripts/py/templates/prebuild.py"))
    monkeypatch.setitem(prebuild["main"].__globals__, "capture", capture)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prebuild.py",
            "lenet",
            "--batch",
            "8",
            "--board",
            "multimesh_v9t6",
            "--mode",
            "capture",
            "--output",
            str(tmp_path),
            "--library",
            str(tmp_path / "library"),
        ],
    )
    prebuild["main"]()
    assert observed == [expected]
    assert list(json.loads(target.read_text())["sources"]) == [expected["class"]]
