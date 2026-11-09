"""Tool resolution shared by the OS builder and stage image identity."""

import os
import pathlib
import shutil

# Include diagnostic tools too: failure there makes image preparation fail.
TOOLS = ("clang", "ld.lld", "llvm-objcopy", "llvm-objdump", "llvm-size")


def toolchain(env=None, cwd=None):
    env = os.environ if env is None else env
    cwd = pathlib.Path.cwd() if cwd is None else pathlib.Path(cwd)
    if env.get("KOS_LLVM"):
        return cwd / env["KOS_LLVM"]
    key = "USERPROFILE" if os.name == "nt" else "HOME"
    home = cwd / env[key] if env.get(key) else pathlib.Path.home()
    mm = home / "micromamba/envs/kos/bin"
    if (mm / "ld.lld").exists():
        return mm
    return None


def tool(name, env=None, cwd=None):
    env = os.environ if env is None else env
    cwd = pathlib.Path.cwd() if cwd is None else pathlib.Path(cwd)
    directory = toolchain(env, cwd)
    if directory is not None:
        return str(directory / name)
    # The harness inspects tools in the builder's cwd, not its own invocation
    # directory. Empty PATH entries also name that directory.
    path = os.pathsep.join(
        str(cwd / part) for part in env.get("PATH", os.defpath).split(os.pathsep)
    )
    found = shutil.which(name, path=path)
    if not found:
        raise SystemExit(
            f"no {name}: set KOS_LLVM to a directory holding clang and ld.lld"
        )
    return found
