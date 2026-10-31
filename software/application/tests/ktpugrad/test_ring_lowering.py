"""Tinygrad contractions use the shared ring generator without DRAM products.

Requires the MMEFF ring-capable compiler build; the legacy build must fail
this witness rather than qualify an ignored schedule flag.
"""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from examples.tinygrad import fusion_measure

from kohakutpu.application.runtime import generated
from kohakutpu.stack import rs


def test_tinygrad_ring_graph_and_disabled_mutation(tmp_path, monkeypatch):
    args = SimpleNamespace(
        phase="compile",
        record=None,
        graphs=None,
        tokens=197,
        initialized=False,
        workload="probe",
        level="bounded",
        realize_ops=False,
        board="vlt_v9t6s_1n",
        library=str(tmp_path),
        library_only=False,
        batch=1,
        width=192,
        blocks=1,
    )
    original = generated.graph

    def witness():
        products = []

        def observe(generator, arguments, machine, mutant):
            source = original(generator, arguments, machine, mutant)
            if generator == "matmul.mm_kernel":
                products.append(json.loads(source))
            return source

        with patch.object(generated, "graph", observe):
            fusion_measure.measure(args)
        assert len(products) == 1
        scratch = products[0]["scratch"]
        assert scratch and all(
            p["spec"]["space"] == "stage" for p in scratch
        ), "product is not stage-local"
        return products[0]

    monkeypatch.setenv("KTPU_MATMUL_RING_DEPTH", "2")
    ring = witness()
    monkeypatch.setenv("KTPU_MATMUL_RING_DEPTH", "0")
    with pytest.raises(AssertionError, match="product is not stage-local"):
        witness()
    monkeypatch.setenv("KTPU_MATMUL_RING_DEPTH", "1")
    single = witness()
    assert (
        ring["scratch"][0]["spec"]["shape"][0]
        == 2 * single["scratch"][0]["spec"]["shape"][0]
    )


def test_ring_request_rejects_legacy_generator_and_bad_depth(monkeypatch):
    legacy = json.dumps(dict(scratch=[dict(spec=dict(space="dram"))]))
    monkeypatch.setattr(rs._rs, "generate", lambda *a: legacy)
    generated.graph.cache_clear()
    try:
        with pytest.raises(ValueError, match="ring-capable compiler"):
            generated.graph(
                "matmul.mm_kernel", '{"ring_depth":2}', "test-machine", None
            )
        with pytest.raises(ValueError, match="depth must"):
            generated.graph(
                "matmul.mm_kernel", '{"ring_depth":-1}', "test-machine", None
            )
        assert generated.graph("matmul.mm_kernel", "{}", "test-machine", None) == legacy
    finally:
        generated.graph.cache_clear()
