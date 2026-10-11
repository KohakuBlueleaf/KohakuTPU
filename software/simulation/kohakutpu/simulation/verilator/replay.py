"""Run CU instructions on one V2 vector core alone: `vec_replay_tb` built with
`VEC_CORE_V2`, its memory answering one word every `memwait + 1` cycles.

    got = replay(flits, memory, memwait=0)
    got.elapsed, got.res["busy"], got.memory[at : at + n]

`flits` are CU_INST flits (IMEM / DESC / RUN, `encode.vector`); `memory` is
the replay memory's initial bytes, `WORDS` 32-byte words at most. The model is
built on first use under `build` (`VL2_DEFS`-style defines via `defines`).
"""

import hashlib
import os
import pathlib
import re
import subprocess
import sys
from dataclasses import dataclass, field

ROOT = pathlib.Path(__file__).resolve().parents[5]
#: The replay bench's memory, in 32-byte words.
WORDS = 4096
BUILD = ROOT / "build" / "vl2"
VLT_ENV = pathlib.Path(
    os.environ.get(
        "KOHAKU_VLT_ENV",
        pathlib.Path(os.environ.get("USERPROFILE", "~"))
        / "micromamba/envs/vlt/Library",
    )
)
PAYLOAD = (1 << 256) - 1


@dataclass
class Replay:
    """One replay's outcome: the bench's counts, the core's counters (`res`,
    VRES; `eng`, each V2 engine's line) and the memory after."""

    payloads: int
    completions: int
    faults: int
    fault_arg: str
    elapsed: int
    res: dict
    eng: dict
    memory: bytes
    warn: list = field(default_factory=list)
    log: str = ""


def vsim(build: pathlib.Path = BUILD) -> pathlib.Path:
    work = pathlib.Path(build) / "vlt_vec2_replay" / "obj_dir"
    return work / ("vsim.exe" if os.name == "nt" else "vsim")


def build_model(build: pathlib.Path = BUILD, defines=()) -> None:
    """Build the replay model under `build`. Raises if no `vsim` results."""
    env = dict(os.environ)
    if os.name == "nt":
        env["PATH"] = str(VLT_ENV / "bin") + os.pathsep + env["PATH"]
        env["VERILATOR_ROOT"] = str(VLT_ENV)
    defs = ["VEC_CORE_V2", *defines]
    p = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/py/vlt.py"),
            "vec2_replay",
            "--native",
            "--keep",
            "--rtl",
            "-j",
            "16",
            "--build-root",
            str(pathlib.Path(build).relative_to(ROOT)),
            *[x for d in defs for x in ("-d", d)],
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if not vsim(build).exists():
        raise RuntimeError(
            f"vec2_replay did not build:\n{p.stdout[-4000:]}{p.stderr[-4000:]}"
        )


def _fields(text: str) -> dict:
    kv = text.split()
    return {kv[i]: int(kv[i + 1]) for i in range(0, len(kv) - 1, 2)}


def replay(
    flits,
    memory: bytes,
    memwait: int = 0,
    build: pathlib.Path = BUILD,
    rebuild: bool = False,
    defines=(),
) -> Replay:
    """Run `flits` against `memory`. Raises if the bench reports no result."""
    if len(memory) > WORDS * 32:
        raise ValueError(f"{len(memory)} bytes do not fit the {WORDS * 32}-byte memory")
    mem = bytes(memory) + bytes(WORDS * 32 - len(memory))
    payloads = [f & PAYLOAD for f in flits]
    key = hashlib.sha256(repr((payloads, mem, memwait)).encode()).hexdigest()[:16]
    run_dir = pathlib.Path(build) / "run" / key
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "prog.hex").write_text("".join(f"{w:064x}\n" for w in payloads))
    lines = [int.from_bytes(mem[32 * i : 32 * i + 32], "little") for i in range(WORDS)]
    (run_dir / "mem.hex").write_text("".join(f"{v:064x}\n" for v in lines))
    if rebuild or not vsim(build).exists():
        build_model(build, defines)
    args = [
        f"+{k}={(run_dir / f'{k}.hex').resolve().as_posix()}"
        for k in ("prog", "mem", "out")
    ]
    done = subprocess.run(
        [str(vsim(build)), *args, f"+memwait={memwait}"],
        cwd=vsim(build).parent,
        capture_output=True,
        text=True,
        check=False,
    )
    log = done.stdout + done.stderr
    (run_dir / "log.txt").write_text(log)
    rep = re.search(
        r"@@@ REPLAY payloads (\d+) completions (\d+) faults (\d+) fault_arg (\S+)", log
    )
    ela = re.search(r"@@@ ELAPSED (\d+)", log)
    vres = re.search(r"VRES \S+ (.*)", log)
    if not (rep and ela and vres):
        raise RuntimeError(f"no replay result in {run_dir / 'log.txt'}:\n{log[-3000:]}")
    out = b"".join(
        int(ln, 16).to_bytes(32, "little")
        for ln in (run_dir / "out.hex").read_text().split()
        if ln and not ln.startswith(("@", "//"))
    )
    eng = {
        m[1]: _fields(m[2]) for m in re.finditer(r"(V2\w+) vec_replay_tb\S* (.*)", log)
    }
    return Replay(
        payloads=int(rep[1]),
        completions=int(rep[2]),
        faults=int(rep[3]),
        fault_arg=rep[4],
        elapsed=int(ela[1]),
        res=_fields(vres[1]),
        eng=eng,
        memory=out,
        warn=sorted(set(re.findall(r"(V2_\w+|VEC_WB_COLLISION)", log))),
        log=log,
    )


__all__ = ["BUILD", "WORDS", "Replay", "build_model", "replay", "vsim"]
