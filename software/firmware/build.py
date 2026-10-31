"""Build the node firmware images, with the WSL riscv64 gcc or native clang.

    python software/firmware/build.py                    # every image
    python software/firmware/build.py kohakutpu_node     # one image
    python software/firmware/build.py --toolchain clang  # clang + ld.lld, no WSL
    python software/firmware/build.py --list

Each image is the framework core plus the parts named in IMAGES; the output is
`build/fw/<image>.elf`, with `.map`, `.lst` (disassembly) and `.size` beside it.
"""

import argparse
import pathlib
import re
import subprocess
import sys
import time

FW = pathlib.Path(__file__).resolve().parent
ROOT = FW.parents[1]
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
    "-O2",
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
    # The debug dispatcher (docs/spec/stack/debug/firmware.md, RUL.073): the
    # same sources with KA_DEBUG, every file for size.
    "kohakutpu_debug": [
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
    # Bring-up probe for the HAL's memory paths (tools/memprobe.py).
    "memprobe": [
        "kohakuaccel/boot",
        "kohakuaccel/apps/memprobe",
    ],
    # The transform bank's registers and the mover status (tools/xfprobe.py).
    "xfprobe": [
        "kohakuaccel/boot",
        "kohakutpu/apps/xfprobe",
    ],
    # Tasks and region heaps, self-checked on the node (tools/os.py).
    "osprobe": [
        "kohakuaccel/boot",
        "kohakuaccel/os",
        "kohakuaccel/apps/osprobe",
    ],
    # The processor's datapaths on known answers (tools/cpuprobe.py); the _m1
    # image is the bench's mesh-1 node.
    "cpuprobe": ["kohakuaccel/boot", "kohakuaccel/apps/cpuprobe"],
    "cpuprobe_m1": ["kohakuaccel/boot", "kohakuaccel/apps/cpuprobe"],
}

# A reversal witness image; never selected by a runtime or machine default.
IMAGES["kohakutpu_memory_mutant"] = list(IMAGES["kohakutpu_node"])
IMAGES["kohakutpu_snapshot_mutant"] = list(IMAGES["kohakutpu_node"])
IMAGES["kohakutpu_snapshot_debug_mutant"] = list(IMAGES["kohakutpu_debug"])

#: Extra compile defines per image. KA_DEBUG_LAST: completion words kept per
#: package unit in the debug region (RUL.077). KA_PKG_COPY_BYTES 0: no copy
#: of a cached package (L0.PKG.029); queue-heap packages are uncached.
DEFINES = {
    "cpuprobe_m1": ["-DCPUPROBE_MESH=1"],
    "kohakutpu_node": ["-DKA_PKG_COPY_BYTES=0"],
    "kohakutpu_debug": ["-DKA_DEBUG=1", "-DKA_DEBUG_LAST=4", "-DKA_PKG_COPY_BYTES=0"],
    "kohakuaccel_node": ["-DKA_PKG_COPY_BYTES=0"],
    "kohakutpu_memory_mutant": ["-DKA_PKG_COPY_BYTES=0", "-DKA_SPEC_MEMORY_MUTANT=1"],
    "kohakutpu_snapshot_mutant": [
        "-DKA_PKG_COPY_BYTES=0",
        "-DKA_SPEC_SNAPSHOT_AFTER_CLEAR=1",
    ],
    "kohakutpu_snapshot_debug_mutant": [
        "-DKA_DEBUG=1",
        "-DKA_DEBUG_LAST=4",
        "-DKA_PKG_COPY_BYTES=0",
        "-DKA_SPEC_SNAPSHOT_AFTER_CLEAR=1",
    ],
}

#: Images compiled for size throughout.
ALL_SIZE = ("kohakutpu_debug", "kohakutpu_snapshot_debug_mutant")

#: (link map, instruction bytes, scratchpad bytes) of an image not on node.ld.
CPUPROBE_LD = FW / "kohakuaccel" / "apps" / "cpuprobe" / "cpuprobe.ld"
MEMORY = {
    "cpuprobe": (CPUPROBE_LD, 8 * 1024, 4 * 1024),
    "cpuprobe_m1": (CPUPROBE_LD, 8 * 1024, 4 * 1024),
}

INCLUDES = ["kohakuaccel/include"]

#: Sources compiled for size: the instruction window is 32 KB, and the per-word
#: loops these hold run on the dispatch engine, which issues the words itself.
SIZE_OPT = ("kohakuaccel/package/", "kohakuaccel/os/", "kohakuaccel/queue/")


def file_flags(src: pathlib.Path, image: str) -> list[str]:
    rel = src.relative_to(FW).as_posix()
    return ["-Os"] if image in ALL_SIZE or rel.startswith(SIZE_OPT) else []


#: The two arrays an image must fit, and the sections each holds.
IMEM_BYTES = 32 * 1024
SPAD_BYTES = 32 * 1024


def memory(name: str) -> tuple:
    """(link map, instruction bytes, scratchpad bytes) of image `name`."""
    return MEMORY.get(name, (LINK, IMEM_BYTES, SPAD_BYTES))


IMEM_SECTIONS = (".text",)
SPAD_SECTIONS = (".bootargs", ".rodata", ".data", ".bss", ".noinit", ".stack")


def wsl_path(p: pathlib.Path) -> str:
    s = p.resolve().as_posix()
    m = re.match(r"^([A-Za-z]):/(.*)$", s)
    return f"/mnt/{m.group(1).lower()}/{m.group(2)}" if m else s


# Dispatcher images have no firmware allocator; the standalone probe is separate.
DISPATCH_EXCLUDE = ("kohakuaccel/os/heap/",)


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
    if "kohakuaccel/queue" in parts:
        out = [
            s
            for s in out
            if not s.relative_to(FW).as_posix().startswith(DISPATCH_EXCLUDE)
        ]
    return out


def build_clang(name: str) -> dict:
    """Compile and link one image with native clang and ld.lld; returns sizes.

    The same flags as the gcc build; libgcc is not linked, so an image needing
    one of its helpers fails at link rather than at run.
    """
    srcs = sources(IMAGES[name])
    obj_dir = OUT / "obj-clang" / name
    obj_dir.mkdir(parents=True, exist_ok=True)
    # -nostdlib already implies -nostartfiles to clang's bare-metal driver, and
    # an unused flag is an error under -Werror.
    flags = ["--target=riscv64-unknown-elf", "-Wno-unused-command-line-argument"]
    flags += CFLAGS + DEFINES.get(name, [])
    flags += [f"-I{(FW / i).as_posix()}" for i in INCLUDES]
    objs = []
    for s in srcs:
        rel = s.relative_to(FW).with_suffix(".o").as_posix().replace("/", "__")
        o = obj_dir / rel
        objs.append(o.as_posix())
        run(
            [
                "clang",
                *flags,
                *file_flags(s, name),
                "-c",
                s.as_posix(),
                "-o",
                o.as_posix(),
            ],
            name,
        )
    elf = OUT / f"{name}.elf"
    run(
        [
            "clang",
            *flags,
            "-fuse-ld=lld",
            "-T",
            memory(name)[0].as_posix(),
            "-Wl,--gc-sections",
            f"-Wl,-Map={(OUT / f'{name}.map').as_posix()}",
            *objs,
            "-o",
            elf.as_posix(),
        ],
        name,
    )
    lst = run(["llvm-objdump", "-d", "-S", elf.as_posix()], name)
    (OUT / f"{name}.lst").write_text(lst, encoding="utf-8")
    size = run(["llvm-size", "-A", elf.as_posix()], name)
    (OUT / f"{name}.size").write_text(size, encoding="utf-8")
    return sizes(OUT / f"{name}.size")


def run(cmd: list, name: str) -> str:
    """One native tool call; its stdout, or SystemExit naming the image."""
    done = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if done.returncode:
        sys.stderr.write(done.stdout + done.stderr)
        raise SystemExit(f"firmware image {name} failed to build: {cmd[0]}")
    return done.stdout


def build(name: str) -> dict:
    """Compile and link one image in ONE WSL call; returns its section sizes."""
    srcs = sources(IMAGES[name])
    obj_dir = OUT / "obj" / name
    obj_dir.mkdir(parents=True, exist_ok=True)
    inc = " ".join(f"-I{wsl_path(FW / i)}" for i in INCLUDES)
    flags = " ".join(CFLAGS + DEFINES.get(name, []))
    cmds, objs = [], []
    for s in srcs:
        rel = s.relative_to(FW).with_suffix(".o").as_posix().replace("/", "__")
        o = obj_dir / rel
        objs.append(wsl_path(o))
        extra = " ".join(file_flags(s, name))
        cmds.append(
            f"{CROSS}gcc {flags} {extra} {inc} -c {wsl_path(s)} -o {wsl_path(o)}"
        )
    elf = OUT / f"{name}.elf"
    cmds.append(
        f"{CROSS}gcc {flags} -T {wsl_path(memory(name)[0])} "
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
    ap.add_argument("--toolchain", choices=("gcc", "clang"), default="gcc")
    ap.add_argument(
        "--size-opt",
        action="append",
        default=[],
        help="also compile sources under this firmware path for size",
    )
    ap.add_argument("--out", help="the output directory (default build/fw)")
    ap.add_argument(
        "-D", dest="defines", action="append", default=[], help="NAME[=VALUE]"
    )
    a = ap.parse_args()
    global OUT, SIZE_OPT, CFLAGS
    CFLAGS = CFLAGS + [f"-D{d}" for d in a.defines]
    SIZE_OPT = SIZE_OPT + tuple(a.size_opt)
    OUT = pathlib.Path(a.out) if a.out else OUT
    make = build_clang if a.toolchain == "clang" else build
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
        s = make(name)
        imem = sum(s.get(k, 0) for k in IMEM_SECTIONS)
        spad = sum(s.get(k, 0) for k in SPAD_SECTIONS)
        _, imem_max, spad_max = memory(name)
        ok = imem <= imem_max and spad <= spad_max
        bad += not ok
        print(
            f"{name:20s} imem {imem:6d}/{imem_max} B  spad {spad:6d}/{spad_max} B"
            f"  {time.monotonic() - t0:4.1f}s  {'ok' if ok else 'DOES NOT FIT'}"
        )
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
