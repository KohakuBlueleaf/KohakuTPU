"""CPU-only witnesses for card-round policy. No subprocess or card is started."""

import importlib.util
import json
import pathlib

import pytest

TOOLS = pathlib.Path(__file__).resolve().parents[3] / "measurements/tools"
SPEC = importlib.util.spec_from_file_location("card_round", TOOLS / "card_round.py")
assert SPEC and SPEC.loader
round_tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(round_tool)


def point(batch=1, path="firmware"):
    return {
        "workload": "vitb",
        "board": "multimesh_v9t6",
        "sysnodes": 4,
        "batch": batch,
        "path": path,
        "inputs": 16,
        "blocks": 12,
    }


def test_matrix_continues_software_failure_but_stops_on_signature_even_with_zero_exit(
    tmp_path,
):
    calls = []

    def execute(config, directory):
        calls.append(config["batch"])
        text = (
            "ValueError: compiler refused"
            if config["batch"] == 1
            else "FirmwareFatal BUSY"
        )
        code = 1 if config["batch"] == 1 else 0
        (directory / "failure.txt").write_text(text)
        return {"status": round_tool.classify(code, text)}

    points = [point(1), point(4), point(16)]
    assert round_tool.run_matrix(points, tmp_path, "r1", execute) == 2
    assert calls == [1, 4]
    assert (tmp_path / "STOP.json").exists()
    assert round_tool.run_matrix(points, tmp_path, "r1", execute) == 2
    assert calls == [1, 4]  # stop is durable across driver restarts


def test_finished_attempts_are_never_rerun_including_software_failures(tmp_path):
    calls = []

    def execute(config, directory):
        calls.append(config["batch"])
        return {"status": "software_failure" if config["batch"] == 1 else "complete"}

    points = [point(1), point(4)]
    assert round_tool.run_matrix(points, tmp_path, "r1", execute) == 1
    assert round_tool.run_matrix(points, tmp_path, "r1", execute) == 1
    assert calls == [1, 4]
    for config in points:
        receipt = tmp_path / round_tool.name(config, "r1") / "attempt.json"
        assert json.loads(receipt.read_text())["status"] != "started"


def test_interrupted_claim_is_not_retried_and_stops_later_points(tmp_path):
    directory = tmp_path / round_tool.name(point(), "r1")
    directory.mkdir()
    round_tool.dump(directory / "attempt.json", {"point": point(), "status": "started"})
    calls = []
    assert (
        round_tool.run_matrix(
            [point(), point(4)], tmp_path, "r1", lambda *args: calls.append(args)
        )
        == 2
    )
    assert calls == []


@pytest.mark.parametrize(
    "text",
    [
        "NodeError",
        "RecordSkew",
        "timed out",
        "xdma timeout",
        "KDS failed",
        "DE-feed failed -7",
    ],
)
def test_card_signature_stops_even_if_exit_status_was_lost(text):
    assert round_tool.classify(0, text) == "card_failure"


def test_process_bound_is_a_card_failure_without_a_traceback():
    assert round_tool.classify(-15, "", timed_out=True) == "card_failure"
    assert round_tool.classify(1, "compile refusal") == "software_failure"
    assert round_tool.classify(0, "completed") == "complete"


def test_configuration_identity_includes_every_axis():
    base = point()
    variants = [base]
    for key, value in {
        "workload": "vit",
        "board": "multimesh_v9t8",
        "sysnodes": 2,
        "batch": 4,
        "path": "kaos",
        "inputs": 17,
        "blocks": 1,
    }.items():
        variants.append(dict(base, **{key: value}))
    assert len({round_tool.name(p, "r1") for p in variants}) == len(variants)
    with pytest.raises(ValueError, match="duplicate"):
        round_tool.configurations([base, base])
