"""Writer alternatives pay for every demanded consumer packing."""

from types import SimpleNamespace

import pytest

from kohakutpu.application.ktpugrad.launch import emit


@pytest.mark.parametrize("missing", [False, True])
def test_packed_prefix_prices_remaining_consumers(monkeypatch, missing):
    src = emit.Operand("full", pitch=17, rows=4)
    keys = [("mx7", side, src, 4, 17, 4, 32, 1, 1, 1) for side in ("a", "b")]
    monkeypatch.setattr(emit, "WRITER_PACKS", True)
    monkeypatch.setattr(emit, "WRITER_PACK_FORM", "auto")
    monkeypatch.setattr(emit.FM, "demands", lambda dev: {0: keys})
    monkeypatch.setattr(emit.FM, "updated_site", lambda st: 0)
    monkeypatch.setattr(
        emit.FM, "writer_quant", lambda dev, k: None if missing and k == keys[1] else k
    )
    monkeypatch.setattr(
        emit.MF,
        "load",
        lambda board: SimpleNamespace(vc=SimpleNamespace(descriptors=3)),
    )
    monkeypatch.setattr(emit, "_fits", lambda *args: True)
    priced_tail = []
    monkeypatch.setattr(
        emit.FM,
        "quant_production",
        lambda dev, steps, src, rows, cols, rp, cp, side, *args: (
            ("materialized", None, None)
            if missing and side == "b"
            else ("composed", {}, None)
        ),
        raising=False,
    )
    monkeypatch.setattr(emit.FM.generated, "relayout_plan", lambda *args: {"plan": 1})
    monkeypatch.setattr(emit.FM.generated, "planned", lambda *args: {})
    monkeypatch.setattr(emit.FM.costs, "choose", lambda *args: False)

    class Priced:
        def __init__(self, *args):
            pass

        def call(self, shape, demands):
            return 10 if shape else 50 if demands else 20

        def cheaper(self, candidate, other):
            def total(calls):
                calls = calls if isinstance(calls, list) else [calls]
                priced_tail.extend(c for c in calls if isinstance(c, dict))
                return (
                    sum(
                        (
                            (30 if c["gen"] == "relayout.relayout_kernel" else 100)
                            if isinstance(c, dict)
                            else c
                        )
                        for c in calls
                    )
                    + (len(calls) - 1) * 5
                )

            return total(candidate) <= total(other)

    monkeypatch.setattr(emit, "_Priced", Priced)
    dev = SimpleNamespace(
        machine=SimpleNamespace(board="test"),
        home=None,
        card=SimpleNamespace(nodes=(0,)),
    )
    st = SimpleNamespace(form=lambda key, size: SimpleNamespace(addr=0))
    result = emit.packings(dev, st, 68, {}, 1)
    assert result is not None
    if missing:
        assert result[0] == (4, 17)
        assert result[2] == (keys[0],)
        assert [c["gen"] for c in priced_tail[:2]] == [
            "relayout.relayout_kernel",
            "matmul.quant_kernel",
        ]
    else:
        assert result[0] is None
        assert emit.CHOSEN[((4, 17), tuple(keys))] == "b"


@pytest.mark.parametrize("composed", ["off", "auto"])
def test_prefix_tail_uses_selected_prepared_production(monkeypatch, composed):
    fm = emit.FM
    src = emit.Operand("full", pitch=17, rows=4)
    keys = [("mx7", side, src, 4, 17, 4, 32, 1, 1, 1) for side in ("a", "b")]
    dev = SimpleNamespace(
        machine=SimpleNamespace(board="test"),
        home=None,
        card=SimpleNamespace(nodes=(0,)),
    )
    uploads, launched, candidates = [], [], []
    monkeypatch.setattr(fm, "COMPOSED", composed)
    monkeypatch.setattr(fm, "PREPARED_QUANT", True)
    monkeypatch.setattr(fm, "_decided", lambda *args: False)
    monkeypatch.setattr(fm.generated.rs, "machine_json", lambda *args: "{}")
    monkeypatch.setattr(fm.generated, "relayout_plan", lambda *args: {"plan": "rows"})
    monkeypatch.setattr(fm, "plans_of", lambda *args: ({"direct": True},))

    def prepared(steps, src, rows, cols, *args):
        return {
            "steps": [{k: v for k, v in vars(s).items() if k != "src"} for s in steps],
            "quant": {"src": {"pitch": 32}, "cols": 32},
        }

    monkeypatch.setattr(fm.costs, "prepared_quant", prepared)

    def bind(*args):
        uploads.append("tables")
        return {"p_index0": 101, "p_mask0": 102}

    monkeypatch.setattr(fm, "step_binds", bind)
    monkeypatch.setattr(
        fm.generated,
        "launch",
        lambda dev, gen, args, binds, **kw: launched.append((args, binds)),
    )
    monkeypatch.setattr(emit, "WRITER_PACKS", True)
    monkeypatch.setattr(emit, "WRITER_PACK_FORM", "auto")
    monkeypatch.setattr(fm, "demands", lambda dev: {0: keys})
    monkeypatch.setattr(fm, "updated_site", lambda st: 0)
    monkeypatch.setattr(fm, "writer_quant", lambda dev, key: None)
    monkeypatch.setattr(
        emit.MF,
        "load",
        lambda board: SimpleNamespace(vc=SimpleNamespace(descriptors=3)),
    )
    monkeypatch.setattr(emit, "_fits", lambda *args: True)

    class Priced:
        def __init__(self, *args):
            pass

        def call(self, *args):
            return {"gen": "writer", "args": {}}

        def cheaper(self, candidate, other):
            candidates.append(candidate)
            return True

    monkeypatch.setattr(emit, "_Priced", Priced)
    forms, valid = {}, set()

    def form(key, size):
        return forms.setdefault(key, SimpleNamespace(addr=200))

    holder = SimpleNamespace(
        form=form, forms=forms, valid=valid, made=valid.add, nbytes=136, site=None
    )
    monkeypatch.setattr(fm, "f16", lambda *args: 100)
    result = emit.packings(dev, holder, 68, {}, 1)
    assert result is not None
    assert result[2] == (keys[0],)
    assert not uploads
    assert [c["gen"] for c in candidates[0]] == ["writer", "matmul.quant_kernel"]
    tail = candidates[0][1]["args"]
    assert "prepared" in tail and "composed" not in tail
    at = fm.packed(dev, holder, "float16", src, 4, 17, 4, 32, "b", 1, 1, 1)
    assert at == 200
    assert uploads == ["tables"]
    assert launched == [(tail, {"p_x": 100, "q": 200, "p_index0": 101, "p_mask0": 102})]
