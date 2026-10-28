"""Boot tables from a board description (include/kos/boot.h, packed here).

    python software/os/tools/boot.py core4s --out /tmp/b [--flags shared_uncached,poll]
                                     [--tick 200000] [--cmd "init preduce"]

Writes <out>/boot<i>.bin for every node. `tables()` is the same for callers.
A board names its DRAM once, as addresses without mesh bits; `spelling` says
which mesh bits each node puts on them ("own": its own mesh's, "mesh0": none),
per region kind or for both.
"""

import argparse
import json
import pathlib
import secrets
import struct
import sys

OS = pathlib.Path(__file__).resolve().parents[1]
BOARDS = OS / "boards"

MAGIC = 0x31544F4F42534F4B  # "KOSBOOT1"
VERSION = 1
BOOT_PA = 0x10000
MAX_NODES, MAX_REGIONS, MAX_UNITS, CMDLINE = 4, 16, 32, 512
RG_DRAM_PRIV, RG_DRAM_SHARED, RG_STAGING = 1, 2, 3
MESH_SHIFT, SPECIAL = 36, 1 << 39

FLAGS = {
    "shared_uncached": 1 << 0,
    "pt_dram": 1 << 1,
    "lock_bakery": 1 << 2,
    "poll": 1 << 3,
    "engine": 1 << 4,
    "trace": 1 << 5,
}

HEADER = struct.Struct("<QQII4BIQQQIIQ24x")
REGION = struct.Struct("<QQIIQ")
UNIT = struct.Struct("<HBBBBH")
#: Offsets the C side asserts (boot.h): region, unit, cmdline.
OFF_REGION = HEADER.size
OFF_UNIT = OFF_REGION + MAX_REGIONS * REGION.size
OFF_CMDLINE = OFF_UNIT + MAX_UNITS * UNIT.size
SIZE = OFF_CMDLINE + CMDLINE


def load(name: str) -> dict:
    p = pathlib.Path(name)
    if not p.exists():
        p = BOARDS / f"{name}.json"
    return json.loads(p.read_text(encoding="utf-8"))


def num(v) -> int:
    return int(v, 0) if isinstance(v, str) else int(v)


def spell(board: dict, kind: str, node: int) -> int:
    s = board.get("spelling", "own")
    if isinstance(s, dict):
        s = s[kind]
    if s == "own":
        return board["mesh_of"][node] << MESH_SHIFT
    if s == "mesh0":
        return 0
    raise ValueError(f"spelling {s!r}: 'own' or 'mesh0'")


def regions(board: dict, node: int) -> list[tuple]:
    """(base, size, kind, owner) as node `node` sees them."""
    d = board["dram"]
    base, priv, shared = num(d["base"]), num(d["private"]), num(d["shared"])
    n = board["nodes"]
    out = []
    if d["layout"] not in ("separate", "carved"):
        raise ValueError(f"dram layout {d['layout']!r}: 'separate' or 'carved'")

    def pbase(j: int) -> int:
        return base + (j * priv if d["layout"] == "carved" else 0)

    out.append((pbase(node) | spell(board, "private", node), priv, RG_DRAM_PRIV, node))
    # Every other node's span as a mover here must name it: with that node's
    # mesh bits, which route the write to it.
    for j in range(n):
        if j != node:
            out.append(
                (pbase(j) | board["mesh_of"][j] << MESH_SHIFT, priv, RG_DRAM_PRIV, j)
            )
    if shared:
        sbase = base + (n * priv if d["layout"] == "carved" else priv)
        out.append((sbase | spell(board, "shared", node), shared, RG_DRAM_SHARED, 0))
    stg = num(board["staging"])
    for j in range(n):
        out.append((SPECIAL | board["mesh_of"][j] << MESH_SHIFT, stg, RG_STAGING, j))
    return out


def table(
    board: dict, node: int, flags: int = 0, tick: int = 0, cmd: str = "", run: int = 1
) -> bytes:
    n = board["nodes"]
    if not 1 <= n <= MAX_NODES:
        raise ValueError(f"{n} nodes: 1..{MAX_NODES}")
    rg = regions(board, node)
    units = board.get("units", [])
    if len(rg) > MAX_REGIONS or len(units) > MAX_UNITS:
        raise ValueError("too many regions or units for the boot table")
    mesh = list(board["mesh_of"]) + [0] * (MAX_NODES - n)
    engine = int(bool(board.get("engine", False)))
    b = bytearray(SIZE)
    HEADER.pack_into(
        b,
        0,
        MAGIC,
        VERSION,
        node,
        n,
        *mesh,
        flags,
        num(board.get("clock_hz", 100_000_000)),
        tick,
        engine,
        len(rg),
        len(units),
        run,
    )
    for i, (base, size, kind, owner) in enumerate(rg):
        REGION.pack_into(b, OFF_REGION + i * REGION.size, base, size, kind, owner, 0)
    for i, u in enumerate(units):
        t = u["type"].encode()
        UNIT.pack_into(
            b,
            OFF_UNIT + i * UNIT.size,
            t[0] << 8 | t[1],
            u["mesh"],
            u["x"],
            u["y"],
            u.get("flags", 0),
            u.get("depth", 0),
        )
    c = cmd.encode()
    if len(c) >= CMDLINE:
        raise ValueError(f"command line of {len(c)} bytes; at most {CMDLINE - 1}")
    b[OFF_CMDLINE : OFF_CMDLINE + len(c)] = c
    return bytes(b)


def parse_flags(text: str) -> int:
    v = 0
    for f in filter(None, (s.strip() for s in text.split(","))):
        if f not in FLAGS:
            raise ValueError(f"flag {f!r}: one of {sorted(FLAGS)}")
        v |= FLAGS[f]
    return v


def tables(board: dict, flags: int = 0, tick: int = 0, cmd: str = "") -> list[bytes]:
    """One boot's tables: every node gets the same fresh run id, so state a
    previous boot left in staging never matches this one (include/kos/start.h)."""
    run = secrets.randbits(63) | 1
    return [table(board, i, flags, tick, cmd, run) for i in range(board["nodes"])]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("board")
    ap.add_argument("--out", required=True)
    ap.add_argument("--flags", default="")
    ap.add_argument("--tick", type=int, default=0)
    ap.add_argument("--cmd", default="")
    a = ap.parse_args()
    board = load(a.board)
    out = pathlib.Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for i, t in enumerate(tables(board, parse_flags(a.flags), a.tick, a.cmd)):
        (out / f"boot{i}.bin").write_bytes(t)
    print(f"{board['name']}: {board['nodes']} boot tables of {SIZE} bytes in {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
