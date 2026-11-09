"""The emulator stage prepares its image before starting timed consumers."""

import dataclasses
import importlib.util
import os
import pathlib
import subprocess
import sys

import pytest

SPEC = importlib.util.spec_from_file_location(
    "stage_runner", pathlib.Path(__file__).resolve().parents[1] / "run.py"
)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


def tool_files(directory):
    directory.mkdir(parents=True, exist_ok=True)
    for name in runner.KOS_TOOLS.TOOLS:
        (directory / name).write_bytes(b"tool")
    return directory


@pytest.mark.parametrize("build_fails", [False, True])
def test_emu_image_preparation_precedes_consumers(monkeypatch, tmp_path, build_fails):
    events = []
    suite = next(s for s in runner.SUITES if s.name == "firmware/emu")
    image = str(tmp_path / "kos.elf")
    monkeypatch.chdir(tmp_path)
    selected = tool_files(tmp_path / "selected-tools")
    suite = dataclasses.replace(
        suite, env=(("KOS_IMAGE", "kos.elf"), ("KOS_LLVM", str(selected)))
    )
    monkeypatch.setattr(runner, "SOFTWARE", tmp_path / "software")
    monkeypatch.setattr(runner, "REPORTS", tmp_path)
    tools = runner.SOFTWARE / "kohakuaccel" / "os" / "tools"
    tools.mkdir(parents=True)
    for name in ("build.py", "memgap.py", "toolchain.py"):
        (tools / name).write_text("input", encoding="utf-8")

    def build(cmd, *, cwd, env, check):
        events.append("build")
        assert cmd == [
            sys.executable,
            str(runner.SOFTWARE / "kohakuaccel" / "os" / "tools" / "build.py"),
            "kos",
            "--out",
            str(pathlib.Path(image).parent),
        ]
        assert env["KOS_IMAGE"] == image
        assert cwd == runner.ROOT and check
        pathlib.Path(image).write_bytes(b"image")
        if build_fails:
            raise subprocess.CalledProcessError(1, cmd)

    def clock():
        events.append("clock")
        return 0.0

    def rust(suite, jobs, env):
        events.append("rust")
        assert env["KOS_IMAGE"] == image
        return 0

    def python(suite, jobs, report, env):
        events.append("python")
        assert env["KOS_IMAGE"] == image
        return 0, []

    monkeypatch.setattr(runner.subprocess, "run", build)
    monkeypatch.setattr(runner.time, "monotonic", clock)
    monkeypatch.setattr(runner, "run_cargo", rust)
    monkeypatch.setattr(runner, "run_python", python)
    if build_fails:
        result = runner.run(suite, 1, None)
        assert not result["passed"] and result["code"] == 1
        assert events == ["build", "clock", "clock"]
        assert not pathlib.Path(image).exists()
    else:
        assert runner.run(suite, 1, None)["passed"]
        assert events == ["build", "clock", "rust", "python", "clock"]


def test_emu_image_reused_until_a_build_input_changes(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "SOFTWARE", tmp_path)
    inputs = [
        tmp_path / "kohakuaccel" / "os" / "tools" / "build.py",
        tmp_path / "kohakuaccel" / "os" / "tools" / "memgap.py",
        tmp_path / "kohakuaccel" / "os" / "tools" / "toolchain.py",
        tmp_path / "kohakuaccel" / "os" / "machine" / "kos.ld",
        tmp_path / "kohakuaccel" / "firmware" / "include" / "ka.h",
    ]
    for path in inputs:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("input", encoding="utf-8")
        os.utime(path, ns=(1_000_000_000, 1_000_000_000))
    image = tmp_path / "kos.elf"
    image.write_bytes(b"image")
    os.utime(image, ns=(2_000_000_000, 2_000_000_000))
    calls = []

    def build(cmd, **kwargs):
        assert not image.exists()
        calls.append(cmd)
        image.write_bytes(b"rebuilt")
        os.utime(image, ns=(4_000_000_000, 4_000_000_000))

    monkeypatch.setattr(runner.subprocess, "run", build)
    selected = tool_files(tmp_path / "selected-tools")
    home = tmp_path / "home"
    env = {
        "KOS_IMAGE": str(image),
        "KOS_LLVM": str(selected),
        "HOME": str(home),
        "USERPROFILE": str(home),
        "PATH": str(selected),
    }
    monkeypatch.setattr(
        runner.KOS_TOOLS.shutil,
        "which",
        lambda name, path: str(pathlib.Path(path) / name),
    )
    runner.prepare_emu(env)
    assert len(calls) == 1  # An image without a stage receipt has unknown flags.
    runner.prepare_emu(env)
    assert len(calls) == 1
    # Content changes count even with a preserved timestamp.
    inputs[-1].write_text("changed", encoding="utf-8")
    os.utime(inputs[-1], ns=(1_000_000_000, 1_000_000_000))
    runner.prepare_emu(env)
    assert len(calls) == 2
    runner.prepare_emu(env)
    assert len(calls) == 2
    inputs[-1].unlink()
    runner.prepare_emu(env)
    assert len(calls) == 3  # Removed inputs must not remain in a stale image.
    image.write_bytes(b"externally rebuilt with other flags")
    runner.prepare_emu(env)
    assert len(calls) == 4
    (selected / runner.KOS_TOOLS.TOOLS[0]).write_bytes(b"upgraded tool at same path")
    runner.prepare_emu(env)
    assert len(calls) == 5
    other = tool_files(tmp_path / "other-tools")
    runner.prepare_emu({**env, "KOS_LLVM": str(other)})
    assert len(calls) == 6
    del env["KOS_LLVM"]
    runner.prepare_emu(env)
    assert len(calls) == 7  # PATH, with no preferred installation.
    tool_files(home / "micromamba/envs/kos/bin")
    runner.prepare_emu(env)
    assert len(calls) == 8  # Same environment, newly preferred tools.


def test_stage_runner_changes_select_its_behavioral_witnesses():
    assert "logic" in {suite.name for suite in runner.selected(["tests/run.py"])}


def test_tool_identity_resolves_relative_paths_in_the_builder_directory(
    monkeypatch, tmp_path
):
    build_root = tmp_path / "repository"
    selected = tool_files(build_root / "relative-tools")
    home = tmp_path / "empty-home"
    for env in (
        {"KOS_LLVM": "relative-tools"},
        {"PATH": "relative-tools", "HOME": str(home), "USERPROFILE": str(home)},
    ):
        monkeypatch.setattr(
            runner.KOS_TOOLS.shutil,
            "which",
            lambda name, path: str(pathlib.Path(path) / name),
        )
        assert (
            pathlib.Path(runner.KOS_TOOLS.tool("clang", env, build_root))
            == selected / "clang"
        )
