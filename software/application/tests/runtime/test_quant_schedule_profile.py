"""Measured recipe admission includes the classifier's scheduling choice."""

import json

import pytest

from kohakutpu.application.runtime import costs


@pytest.mark.parametrize(
    "knob, field",
    [
        ("KTPU_QUANT_CLASSIFIER_OVERLAP", "classifier_overlap"),
        ("KTPU_QUANT_PIPELINE", "pipeline"),
        ("KTPU_QUANT_ZERO_CLASSIFIER", "zero_classifier"),
        ("KTPU_QUANT_STREAM_AXES", "stream_axes"),
        ("KTPU_QUANT_FILL_OVERLAP", "fill_overlap"),
        ("KTPU_PULL_TREE", "pull_tree"),
        ("KTPU_QUANT_ZERO_REUSE", "zero_reuse"),
    ],
)
def test_classifier_overlap_does_not_reuse_serial_costs(
    tmp_path, monkeypatch, knob, field
):
    recipe = {"rows": 4, "cols": 32}
    key = costs.key("staged_padding", dict(board="vlt_v9t6s_1n", recipe=recipe))
    record = {key: dict(original=10, alternative=8, verified=True)}
    monkeypatch.setenv("KTPU_PIPE_FINITE_BATCH", "4")
    monkeypatch.setenv("KTPU_PIPE_OVERLAP", "1")
    monkeypatch.setenv("KTPU_CLASS_BAND", "1")
    for name in (
        "KTPU_QUANT_CLASSIFIER_OVERLAP",
        "KTPU_QUANT_PIPELINE",
        "KTPU_QUANT_ZERO_CLASSIFIER",
        "KTPU_QUANT_STREAM_AXES",
        "KTPU_QUANT_FILL_OVERLAP",
        "KTPU_PULL_TREE",
        "KTPU_QUANT_ZERO_REUSE",
    ):
        monkeypatch.setenv(name, "0")
    for overlap in (0, 1):
        schedule = dict(finite_batch=4, overlap=True)
        if overlap:
            schedule[field] = True
        profile = tmp_path / f"schedule-{overlap}.json"
        profile.write_text(json.dumps(dict(schedule=schedule, recipes=record)))
        monkeypatch.setenv("KTPU_COST_PROFILE", str(profile))
        monkeypatch.setenv(knob, str(overlap))
        assert costs.choose("staged_padding", recipe, "vlt_v9t6s_1n")
        monkeypatch.setenv(knob, str(1 - overlap))
        assert not costs.choose("staged_padding", recipe, "vlt_v9t6s_1n")


def test_class_band_profile_admission(tmp_path, monkeypatch):
    for name in (
        "KTPU_QUANT_CLASSIFIER_OVERLAP",
        "KTPU_QUANT_PIPELINE",
        "KTPU_QUANT_ZERO_CLASSIFIER",
        "KTPU_QUANT_STREAM_AXES",
        "KTPU_QUANT_FILL_OVERLAP",
        "KTPU_PULL_TREE",
        "KTPU_QUANT_ZERO_REUSE",
    ):
        monkeypatch.setenv(name, "0")
    monkeypatch.setenv("KTPU_PIPE_FINITE_BATCH", "4")
    monkeypatch.setenv("KTPU_PIPE_OVERLAP", "1")
    monkeypatch.setenv("KTPU_CLASS_BAND", "4")
    recipe = {"rows": 32, "cols": 128}
    key = costs.key("staged_padding", dict(board="vlt_v9t6s_1n", recipe=recipe))
    profile = tmp_path / "band.json"
    profile.write_text(
        json.dumps(
            dict(
                schedule=dict(finite_batch=4, overlap=True, class_band=4),
                recipes={key: dict(original=10, alternative=8, verified=True)},
            )
        )
    )
    monkeypatch.setenv("KTPU_COST_PROFILE", str(profile))
    assert costs.choose("staged_padding", recipe, "vlt_v9t6s_1n")
    monkeypatch.setenv("KTPU_CLASS_BAND", "1")
    assert not costs.choose("staged_padding", recipe, "vlt_v9t6s_1n")
