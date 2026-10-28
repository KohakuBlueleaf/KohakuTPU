"""Build KohakuAccel OS images with clang and ld.lld.

    python software/os/tools/build.py              # every image
    python software/os/tools/build.py kos m0       # some
    python software/os/tools/build.py --list

Kernel-side sources (L0 and L1) and user-side sources (L2 runtime, the
dispatcher, utilities, apps) are compiled separately: the shared library sources are compiled once
for each side, and the link script places every user object's sections in the
user pages (`*/u/*.o`). Code that runs translated (each image's GAP sides) is
compiled to assembly and through tools/memgap.py first; `--no-memgap` turns
the pass off (for a core whose MMU releases only the access it resolved).
Output: <out>/<image>.elf with .map, .lst and .size.
The toolchain directory is $KOS_LLVM, else ~/micromamba/envs/kos/bin, else PATH.
"""

import argparse
import concurrent.futures
import os
import pathlib
import shutil
import subprocess
import sys
import time

import memgap

OS = pathlib.Path(__file__).resolve().parents[1]
ROOT = OS.parents[1]

CFLAGS = [
    "--target=riscv64-unknown-elf",
    "-march=rv64ima_zicsr_zifencei",
    "-mabi=lp64",
    "-mcmodel=medany",
    "-mno-relax",
    "-fno-pic",
    "-ffreestanding",
    "-fno-builtin",
    "-fno-stack-protector",
    "-ffunction-sections",
    "-fdata-sections",
    "-O2",
    "-g",
    "-Wall",
    "-Wextra",
    "-Werror",
    "-Wno-unused-command-line-argument",
    f"-I{(OS / 'include').as_posix()}",
]

LIB = ["lib/string.c", "lib/fmt.c"]


def tree(*dirs: str) -> list[str]:
    out = []
    for d in dirs:
        for p in sorted((OS / d).rglob("*")):
            if p.suffix in (".c", ".S"):
                out.append(p.relative_to(OS).as_posix())
    return out


#: image -> (link script, kernel-side sources, user-side sources, GAP sides:
#: the sides whose code runs translated). m0 is built twice: plain for the
#: timing probes, gapped (m0w) for `walk`, whose S code runs translated.
M0 = ["tests/m0/crt.S", "tests/m0/m0.c", *LIB]
IMAGES = {
    "m0": ("tests/m0/m0.ld", M0, [], set()),
    "m0w": ("tests/m0/m0.ld", M0, [], {"k"}),
    "kos": (
        "machine/kos.ld",
        [*tree("machine", "kernel"), *LIB],
        [*tree("user", "dispatch", "utils", "apps"), *LIB],
        {"u"},
    ),
}


def toolchain() -> pathlib.Path | None:
    env = os.environ.get("KOS_LLVM")
    if env:
        return pathlib.Path(env)
    mm = pathlib.Path.home() / "micromamba/envs/kos/bin"
    if (mm / "ld.lld").exists():
        return mm
    return None


def tool(name: str) -> str:
    d = toolchain()
    if d is not None:
        return str(d / name)
    found = shutil.which(name)
    if not found:
        raise SystemExit(
            f"no {name}: set KOS_LLVM to a directory holding clang and ld.lld"
        )
    return found


def run(cmd: list[str]) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if p.returncode:
        sys.stderr.write(p.stdout + p.stderr)
        raise SystemExit(f"failed: {' '.join(cmd[:3])} ... {cmd[-1]}")
    return p.stdout


def compile_one(
    src: str, side: str, obj_dir: pathlib.Path, defines: list[str], gap: bool
) -> tuple[str, int]:
    """(object, nops the memory-gap pass added)."""
    obj = obj_dir / side / (src.replace("/", "__") + ".o")
    obj.parent.mkdir(parents=True, exist_ok=True)
    flags = CFLAGS + defines + (["-DKOS_USER=1"] if side == "u" else [])
    if not gap:
        run([tool("clang"), *flags, "-c", (OS / src).as_posix(), "-o", obj.as_posix()])
        return obj.as_posix(), 0
    asm = obj.with_suffix(".s")
    # C to assembly; hand assembly through the preprocessor only
    stage = "-E" if src.endswith(".S") else "-S"
    run([tool("clang"), *flags, stage, (OS / src).as_posix(), "-o", asm.as_posix()])
    text, added = memgap.apply(asm.read_text())
    gapped = obj.with_suffix(".gap.s")
    gapped.write_text(text)
    run([tool("clang"), *flags, "-c", gapped.as_posix(), "-o", obj.as_posix()])
    return obj.as_posix(), added


#: what the kernel side calls on the user side; every other user symbol is
#: made local, so both sides can carry their own copy of the library
USER_EXPORTS = ["__kos_app_start", "__kos_thread_start"]


def user_object(objs: list[str], obj_dir: pathlib.Path) -> str:
    """The user side as one relocatable object with only USER_EXPORTS global."""
    joined = obj_dir / "u" / "user.o"
    run([tool("ld.lld"), "-r", *objs, "-o", joined.as_posix()])
    keep = [f"--keep-global-symbol={s}" for s in USER_EXPORTS]
    run([tool("llvm-objcopy"), *keep, joined.as_posix()])
    return joined.as_posix()


def build(
    name: str, out: pathlib.Path, defines: list[str], memgap_on: bool = True
) -> dict:
    ld, ksrc, usrc, gap_sides = IMAGES[name]
    obj_dir = out / "obj" / name
    if obj_dir.exists():
        shutil.rmtree(obj_dir)
    jobs = [(s, "k") for s in ksrc] + [(s, "u") for s in usrc]
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        done = list(
            ex.map(
                lambda j: compile_one(
                    j[0], j[1], obj_dir, defines, memgap_on and j[1] in gap_sides
                ),
                jobs,
            )
        )
    objs = [o for o, _ in done]
    nops = sum(n for _, n in done)
    if usrc:
        objs = objs[: len(ksrc)] + [user_object(objs[len(ksrc) :], obj_dir)]
    elf = out / f"{name}.elf"
    run(
        [
            tool("ld.lld"),
            "-T",
            (OS / ld).as_posix(),
            "--gc-sections",
            "-Map",
            (out / f"{name}.map").as_posix(),
            *objs,
            "-o",
            elf.as_posix(),
        ]
    )
    (out / f"{name}.lst").write_text(
        run([tool("llvm-objdump"), "-d", "-S", elf.as_posix()])
    )
    size = run([tool("llvm-size"), "-A", elf.as_posix()])
    (out / f"{name}.size").write_text(size)
    sections = {}
    for line in size.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0].startswith(".") and parts[1].isdigit():
            sections[parts[0]] = int(parts[1])
    sections["memgap_nops"] = nops
    return sections


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("images", nargs="*")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "build" / "os"))
    ap.add_argument(
        "-D", dest="defines", action="append", default=[], help="NAME[=VALUE]"
    )
    ap.add_argument(
        "--no-memgap", action="store_true", help="no memory-gap pass on translated code"
    )
    a = ap.parse_args()
    if a.list:
        for k, (ld, ks, us, gs) in IMAGES.items():
            print(
                f"{k:6s} {ld}  kernel {len(ks)} sources, user {len(us)}, gap {sorted(gs)}"
            )
        return 0
    out = pathlib.Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    defines = [f"-D{d}" for d in a.defines]
    for name in a.images or list(IMAGES):
        if name not in IMAGES:
            raise SystemExit(f"no image {name!r}; have {sorted(IMAGES)}")
        t0 = time.monotonic()
        s = build(name, out, defines, not a.no_memgap)
        shown = " ".join(f"{k} {v}" for k, v in sorted(s.items()) if v)
        print(f"{name}: {time.monotonic() - t0:.1f}s  {shown}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
