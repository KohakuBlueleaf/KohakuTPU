"""The ViT library traces the production seeded twelve-block model."""

import json
import sys
from types import SimpleNamespace

import prebuild
import pytest


def test_vit_uses_production_seeded_model(monkeypatch):
    calls = []

    def seeded_model(**kwargs):
        calls.append(kwargs)
        return lambda tensor: SimpleNamespace(realize=lambda: "realized")

    monkeypatch.setattr(
        prebuild.runpy, "run_path", lambda path: {"seeded_model": seeded_model}
    )
    monkeypatch.setattr(prebuild, "place", lambda model: None)
    monkeypatch.setattr(prebuild, "Tensor", lambda *args, **kwargs: None)
    assert prebuild.vit_tiny(1) == "realized"
    assert calls == [{"blocks": 12, "seed": 17, "width": 192, "image_size": 224}]


@pytest.mark.parametrize("exists", [True, False])
def test_explicit_capture_is_never_retraced(tmp_path, monkeypatch, exists):
    capture = tmp_path / "input.json"
    doc = {
        "model": "lenet",
        "board": "multimesh_v9t6",
        "batch": 1,
        "sources": {},
        "launches": [],
    }
    if exists:
        capture.write_text(json.dumps(doc), encoding="utf-8")
    output = tmp_path / "output"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prebuild",
            "lenet",
            "--batch",
            "1",
            "--mode",
            "capture",
            "--capture",
            str(capture),
            "--output",
            str(output),
        ],
    )
    monkeypatch.setattr(prebuild.D, "Device", lambda **kwargs: SimpleNamespace())
    monkeypatch.setattr(prebuild.D, "attach", lambda dev: dev)

    def forbidden(*args, **kwargs):
        raise AssertionError("explicit capture was retraced")

    monkeypatch.setattr(prebuild, "capture", forbidden)
    if exists:
        prebuild.main()
        assert json.loads(capture.read_text(encoding="utf-8")) == doc
    else:
        with pytest.raises(FileNotFoundError):
            prebuild.main()
