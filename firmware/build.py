"""Build the node firmware images with the WSL riscv64 toolchain.

    python firmware/build.py                    # every image
    python firmware/build.py kohakutpu_node     # one image
    python firmware/build.py --list

Each image is the framework core plus the parts named in IMAGES; the output is
`build/fw/<image>.elf`, with `.map`, `.lst` (disassembly) and `.size` beside it.
"""

import argparse
import pathlib
import re
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
FW = ROOT / "firmware"
OUT = ROOT / "build" / "fw"
DISTRO = "Ubuntu-24.04"
CROSS = "riscv64-unknown-elf-"

#: The node processor's link map: 32 KB instruction window, 32 KB scratchpad.
LINK = FW / "kohakuaccel" / "arch" / "rv64" / "node.ld"

CFLAGS = [
    "-march=rv64ima_zicsr_zifencei",
    "-mabi=lp64",
    "-mcmodel=medany",
    "-nostdlib",
    "-nostartfiles",
    "-ffreestanding",
    "-fno-builtin",
    "-ffunction-sections",
    "-fdata-sections",
    "-Os",
    "-g",
    "-Wall",
    "-Wextra",
    "-Werror",
    # The control region's exit register, crt0's store of main's return value.
    "-DEXIT_ADDR=0x20000",
]
LDFLAGS = ["-Wl,--gc-sections", "-lgcc"]

#: Source directories every image carries: the framework's runtime.
CORE = [
    "kohakuaccel/arch/rv64",
    "kohakuaccel/lib",
    "kohakuaccel/hal",
]

#: Directories (or files) per image, on top of CORE. A directory takes every
#: .c and .S beneath it.
IMAGES = {
    # The dispatcher: queue service, package interpreter, the unit plug-in
    # table, with KohakuTPU's units registered.
    "kohakutpu_node": [
        "kohakuaccel/boot",
        "kohakuaccel/os",
        "kohakuaccel/queue",
        "kohakuaccel/package",
        "kohakuaccel/dispatch",
        "kohakuaccel/unit",
        "kohakuaccel/apps/dispatcher",
        "kohakutpu/units",
    ],
    # The same dispatcher with only the framework's generic unit class.
    "kohakuaccel_node": [
        "kohakuaccel/boot",
        "kohakuaccel/os",
        "kohakuaccel/queue",
        "kohakuaccel/package",
        "kohakuaccel/dispatch",
        "kohakuaccel/unit",
        "kohakuaccel/apps/dispatcher",
    ],
    # Bring-up probe for the HAL's memory paths (scripts/py/sw_memprobe.py).
    "memprobe": [
        "kohakuaccel/boot",
        "kohakuaccel/apps/memprobe",
    ],
    # Tasks and region heaps, self-checked on the node (scripts/py/sw_os.py).
    "osprobe": [
        "kohakuaccel/boot",
        "kohakuaccel/os",
        "kohakuaccel/apps/osprobe",
    ],
}

INCLUDES = ["kohakuaccel/include"]

#: The two arrays an image must fit, and the sections each holds.
IMEM_BYTES = 32 * 1024
SPAD_BYTES = 32 * 1024
IMEM_SECTIONS = (".text",)
SPAD_SECTIONS = (".bootargs", ".rodata", ".data", ".bss", ".noinit", ".stack")


def wsl_path(p: pathlib.Path) -> str:
    s = p.resolve().as_posix()
    m = re.match(r"^([A-Za-z]):/(.*)$", s)
    return f"/mnt/{m.group(1).lower()}/{m.group(2)}" if m else s


def sources(parts: list[str]) -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for part in CORE + parts:
        p = FW / part
        if p.is_file():
            out.append(p)
            continue
        if not p.is_dir():
            raise SystemExit(f"image part {part!r} is neither a file nor a directory")
        out += sorted(q for q in p.rglob("*") if q.suffix in (".c", ".S"))
    return out


def build(name: str) -> dict:
    """Compile and link one image in ONE WSL call; returns its section sizes."""
    srcs = sources(IMAGES[name])
    obj_dir = OUT / "obj" / name
    obj_dir.mkdir(parents=True, exist_ok=True)
    inc = " ".join(f"-I{wsl_path(FW / i)}" for i in INCLUDES)
    flags = " ".join(CFLAGS)
    cmds, objs = [], []
    for s in srcs:
        rel = s.relative_to(FW).with_suffix(".o").as_posix().replace("/", "__")
        o = obj_dir / rel
        objs.append(wsl_path(o))
        cmds.append(f"{CROSS}gcc {flags} {inc} -c {wsl_path(s)} -o {wsl_path(o)}")
    elf = OUT / f"{name}.elf"
    cmds.append(
        f"{CROSS}gcc {flags} -T {wsl_path(LINK)} "
        f"-Wl,-Map={wsl_path(OUT / f'{name}.map')} {' '.join(objs)} "
        f"-o {wsl_path(elf)} {' '.join(LDFLAGS)}"
    )
    cmds.append(
        f"{CROSS}objdump -d -S {wsl_path(elf)} > {wsl_path(OUT / f'{name}.lst')}"
    )
    cmds.append(f"{CROSS}size -A {wsl_path(elf)} > {wsl_path(OUT / f'{name}.size')}")
    script = " && ".join(cmds)
    done = subprocess.run(
        ["wsl", "-d", DISTRO, "--", "bash", "-lc", script],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode:
        sys.stderr.write(done.stdout + done.stderr)
        raise SystemExit(f"firmware image {name} failed to build")
    return sizes(OUT / f"{name}.size")


def sizes(path: pathlib.Path) -> dict:
    """{section: bytes} from `size -A`."""
    out = {}
    for line in path.read_text().splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0].startswith(".") and parts[1].isdigit():
            out[parts[0]] = int(parts[1])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("images", nargs="*")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    if a.list:
        for k, v in IMAGES.items():
            print(f"{k:20s} {' '.join(v)}")
        return 0
    want = a.images or list(IMAGES)
    OUT.mkdir(parents=True, exist_ok=True)
    bad = 0
    for name in want:
        if name not in IMAGES:
            raise SystemExit(f"no image {name!r}; have {sorted(IMAGES)}")
        t0 = time.monotonic()
        s = build(name)
        imem = sum(s.get(k, 0) for k in IMEM_SECTIONS)
        spad = sum(s.get(k, 0) for k in SPAD_SECTIONS)
        ok = imem <= IMEM_BYTES and spad <= SPAD_BYTES
        bad += not ok
        print(
            f"{name:20s} imem {imem:6d}/{IMEM_BYTES} B  spad {spad:6d}/{SPAD_BYTES} B"
            f"  {time.monotonic() - t0:4.1f}s  {'ok' if ok else 'DOES NOT FIT'}"
        )
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
