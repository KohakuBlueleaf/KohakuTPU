"""Firmware sources built for the host: pure logic with no hardware under it."""

import pathlib
import subprocess

SOFTWARE = pathlib.Path(__file__).resolve().parents[3]
FIRMWARE = SOFTWARE / "kohakuaccel" / "firmware"
HARNESS = pathlib.Path(__file__).resolve().parent / "harness"
FLAGS = ["-std=c11", "-O1", "-Wall", "-Wextra", "-Werror", f"-I{FIRMWARE / 'include'}"]


def build(out: pathlib.Path, harness: str, source: str, change=None) -> pathlib.Path:
    """`harness` linked with the firmware's `source`, `change` = (old, new) applied
    to a copy of it; a change that matches nothing is an error."""
    src = FIRMWARE / source
    if change:
        text = src.read_text(encoding="utf-8")
        assert change[0] in text, change[0]
        src = out / f"{src.stem}_{abs(hash(change))}.c"
        src.write_text(text.replace(*change), encoding="utf-8")
    exe = (
        src.parent / f"{out.name}_{harness}_{abs(hash(change))}"
        if change
        else out / harness
    )
    subprocess.run(
        ["gcc", *FLAGS, str(HARNESS / f"{harness}.c"), str(src), "-o", str(exe)],
        check=True,
        capture_output=True,
        text=True,
    )
    return exe


def on_double(
    out: pathlib.Path, harness: str, sources: list[str], change=None, app=None
):
    """`harness` on the hardware double, linked with `sources` (under `software/
    kohakuaccel`), `app` the one it includes as APP_SOURCE, `change` applied to a
    copy of the one that holds it."""
    os_dir = SOFTWARE / "kohakuaccel" / "os"
    paths = [SOFTWARE / "kohakuaccel" / s for s in [*sources, *([app] if app else [])]]
    if change:
        at = next(i for i, p in enumerate(paths) if change[0] in p.read_text("utf-8"))
        copy = out / f"changed_{paths[at].name}"
        copy.write_text(paths[at].read_text("utf-8").replace(*change), "utf-8")
        paths[at] = copy
    exe = out / f"{harness}_{abs(hash(change))}"
    flags = [
        f"-I{HARNESS / 'shim'}",
        f"-isystem{os_dir / 'include'}",
        f"-I{os_dir / 'kernel'}",
        f"-I{os_dir / 'apps' / 'ktpu'}",
        f"-I{os_dir / 'apps' / 'kds'}",
        "-DKOS_DE_TIMEOUT=0x40000UL",  # a hang is told in milliseconds
        *FLAGS,
    ]
    if app:
        flags += ["-fno-builtin", f'-DAPP_SOURCE="{paths.pop()}"']
    subprocess.run(
        [
            "gcc",
            *flags,
            str(HARNESS / f"{harness}.c"),
            str(HARNESS / "hw_double.c"),
            *map(str, paths),
            "-o",
            str(exe),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return exe


def run(exe: pathlib.Path, lines: list[str]) -> list[str]:
    done = subprocess.run(
        [str(exe)],
        input="\n".join(lines) + "\n",
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.splitlines()
