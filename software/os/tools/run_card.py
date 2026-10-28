"""Run an image on the card, or on a Verilated card model, one node per mesh.

    python software/os/tools/run_card.py --board card_v9t6_2n \\
        --model ~/ktpu/build/t2s/vlt_card_v9t6_2n --image ~/kos/out/kos.elf \\
        --cmd "hello; units; mg" [--flags engine] [--json out.json]
    python software/os/tools/run_card.py --board card_v9t6_2n --jtag ...   # silicon

Per node: the core is held, its private span (and the shared region, once)
is painted with zeros — a never-written DRAM line is an ECC error on the
card, and a cached store fills its line first — the user image is written at
its private base, the instruction window and the scratchpad (boot table at
its base) are written (``--load burn`` pokes the model's arrays, ``axi``
streams through the load window as on silicon), and the nodes are started
last-to-first. The console is drained while they run; once a node stops, its
kernel log ring is read back from DRAM ({count, bytes from +64}), which holds
what a full console FIFO dropped. Lines "@ name value" are measurements.

A host DRAM address is the processor's with its mesh bits taken as the mesh
and the rest modulo the board's DRAM size (the memory a model implements is
smaller than the 40-bit space and aliases).
"""

import argparse
import json
import pathlib
import re
import sys
import time

import klog
from boot import BOOT_PA, MESH_SHIFT, RG_DRAM_SHARED, load, parse_flags, regions, tables
from image import UIMG, segments

from kohakuaccel.driver.device import rv64load
from kohakuaccel.driver.node.boot.loader import cpu_scope
from kohakuaccel.driver.transport.jtag import JtagTransport
from kohakuaccel.driver.transport.rebase import UnitGlobal
from kohakuaccel.driver.transport.verilator import VerilatorTransport
from kohakutpu.driver.clock.card import load_board
from kohakutpu.driver.host import board_map

SPAD_BASE, SPAD_BYTES, IMEM_BYTES = 0x1_0000, 0x8000, 0x8000
NODE_BASE = 0x1000_0000
LOG = klog.LOG
MEASURE = re.compile(r"^@ (\S+) (-?\d+)$")
PROGRESS_SECONDS = 30.0


class Card:
    def __init__(self, board: dict, model: str | None, log: pathlib.Path) -> None:
        cb = dict(load_board(board["card"]["board"]))
        cb["meshes"] = board["card"]["meshes"]
        self.m = board_map(cb)
        self.sim = model is not None
        self.t = VerilatorTransport(model, log=log) if model else JtagTransport()
        self.mem = UnitGlobal(
            self.t, self.m["dram"], self.m["mem"], self.m["size"], staging=True
        )

    def host(self, pa: int) -> int:
        mesh = (pa >> MESH_SHIFT) & 3
        return self.m["dram"][mesh] + ((pa & ((1 << MESH_SHIFT) - 1)) % self.m["size"])

    def write(self, pa: int, data: bytes) -> None:
        for k in range(0, len(data), 1 << 16):
            self.mem.write_block(self.host(pa + k), data[k : k + (1 << 16)])

    def read(self, pa: int, n: int) -> bytes:
        return self.mem.read_block(self.host(pa), n)

    def idle(self) -> None:
        if self.sim:
            self.t.run(20000)
        else:
            time.sleep(0.05)


def arrays(elf: pathlib.Path, table: bytes) -> tuple[bytes, bytes, list]:
    """(imem bytes, spad bytes with the boot table, DRAM segments)."""
    imem, spad, dram = bytearray(), bytearray(SPAD_BYTES), []
    for pa, data in segments(elf):
        if pa < SPAD_BASE:
            imem.extend(bytes(max(0, pa + len(data) - len(imem))))
            imem[pa : pa + len(data)] = data
        elif pa < SPAD_BASE + SPAD_BYTES:
            spad[pa - SPAD_BASE : pa - SPAD_BASE + len(data)] = data
        elif NODE_BASE <= pa < UIMG:
            dram.append((pa, data))
    off = BOOT_PA - SPAD_BASE
    spad[off : off + len(table)] = table
    imem += bytes(-len(imem) % 4)
    if len(imem) > IMEM_BYTES:
        raise ValueError(f"{len(imem)} bytes of text for a {IMEM_BYTES}-byte window")
    return bytes(imem), bytes(spad), dram


def load_node(card: Card, node: int, mesh: int, imem: bytes, spad: bytes, mode: str):
    win = rv64load.LoadWindow(card.t, card.m["ctrl"][mesh] + rv64load.WINDOW_OFFSET)
    win.stop()
    if mode == "burn":
        scope = cpu_scope(card.t, mesh)
        card.t.poke(f"{scope}.u_imem.u_ram", "mem", 0, imem)
        card.t.poke(scope, "spad", 0, spad)
    else:
        card.t.write64(win.base + rv64load.R_LOADC, rv64load.REGION_IMEM)
        card.t.write_seq(
            win.base + rv64load.R_LOADD,
            [int.from_bytes(imem[a : a + 4], "little") for a in range(0, len(imem), 4)],
        )
        win.write_spad(0, spad)
    win.entry = 0
    return win


def run(a, board: dict) -> dict:
    out = pathlib.Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    card = Card(board, a.model, out / "model.log")
    image = pathlib.Path(a.image)
    flags = parse_flags(a.flags)
    n = board["nodes"]
    priv = [regions(board, i)[0] for i in range(n)]
    t0 = time.monotonic()
    painted = set()
    wins = []
    for i, tb in enumerate(tables(board, flags, a.tick, a.cmd)):
        imem, spad, dram = arrays(image, tb)
        base, size = priv[i][0], priv[i][1]
        if a.paint:
            card.write(base, bytes(size))
            for rg in regions(board, i):
                if rg[2] == RG_DRAM_SHARED and rg[0] not in painted:
                    card.write(rg[0], bytes(rg[1]))
                    painted.add(rg[0])
        for pa, data in segments(image):
            if pa >= UIMG:
                card.write(base + pa - UIMG, data)
        for pa, data in dram:
            card.write(pa | board["mesh_of"][i] << MESH_SHIFT, data)
        wins.append(load_node(card, i, board["mesh_of"][i], imem, spad, a.load))
    t_load = time.monotonic() - t0
    for w in reversed(wins):
        w.boot()
    t1 = time.monotonic()
    console = [""] * n
    status = [0] * n
    log_at = [p[0] + klog.offset(p[1]) for p in priv]
    shown = t1
    while time.monotonic() - t1 < a.timeout:
        for i, w in enumerate(wins):
            console[i] += w.console()
            status[i] = w.status()
        if all(s & 0xC for s in status):
            break
        if time.monotonic() - shown >= PROGRESS_SECONDS:
            shown = time.monotonic()
            for i in range(n):
                nd = klog.parse(card.read(log_at[i], 64) + bytes(LOG - 64))
                print(f"[{i}] at {shown - t1:.0f}s: {klog.describe(nd)}", flush=True)
        card.idle()
    wall = time.monotonic() - t1
    nodes = []
    for i, w in enumerate(wins):
        console[i] += w.console()
        nodes.append(
            {
                "status": status[i],
                "exit": card.t.read64(w.base + rv64load.R_EXIT),
                "halt_pc": card.t.read64(w.base + rv64load.R_HALTPC),
                "console": console[i],
                **klog.parse(card.read(log_at[i], LOG)),
            }
        )
    meas: dict[str, dict[str, int]] = {}
    for i, nd in enumerate(nodes):
        src = nd["log"] if len(nd["log"]) >= len(nd["console"]) else nd["console"]
        for line in src.splitlines():
            mm = MEASURE.match(line.strip())
            if mm:
                meas.setdefault(str(i), {})[mm[1]] = int(mm[2])
    st = card.t.status() if card.sim else {}
    if card.sim:
        card.t.close()
    ok = all(nd["status"] & 0x8 and nd["exit"] == 0 for nd in nodes)
    return {
        "ok": ok,
        "load_seconds": t_load,
        "run_seconds": wall,
        "nodes": nodes,
        "measures": meas,
        "model_status": {k: v for k, v in st.items() if isinstance(v, int)},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--board", required=True)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--model", help="a Verilated card build directory")
    src.add_argument("--jtag", action="store_true", help="the card, through Vivado")
    ap.add_argument("--image", required=True)
    ap.add_argument("--cmd", default="")
    ap.add_argument("--flags", default="")
    ap.add_argument("--tick", type=int, default=0)
    ap.add_argument("--load", choices=("burn", "axi"), default=None)
    ap.add_argument("--timeout", type=float, default=1800.0)
    ap.add_argument("--out", default="kos_card_run")
    ap.add_argument("--json")
    ap.add_argument(
        "--paint",
        choices=("yes", "no"),
        default=None,
        help="zero the spans before loading: needed on the card (ECC, and an "
        "earlier run's bytes); a fresh model's DRAM starts zero (default: yes "
        "with --jtag, no with --model)",
    )
    a = ap.parse_args()
    a.load = a.load or ("burn" if a.model else "axi")
    a.paint = (a.paint or ("no" if a.model else "yes")) == "yes"
    r = run(a, load(a.board))
    for i, nd in enumerate(r["nodes"]):
        text = nd["log"] if len(nd["log"]) >= len(nd["console"]) else nd["console"]
        for line in text.splitlines():
            print(f"[{i}] {line}")
        print(
            f"[{i}] status {nd['status']:#x} exit {nd['exit']:#x} halt_pc {nd['halt_pc']:#x}"
            f" {klog.describe(nd)}"
        )
    print(
        f"run_card: {'PASS' if r['ok'] else 'FAIL'}; load {r['load_seconds']:.1f}s, "
        f"run {r['run_seconds']:.1f}s; model {r['model_status']}"
    )
    if a.json:
        doc = dict(
            r, board=a.board, image=a.image, cmd=a.cmd, flags=a.flags, tick=a.tick
        )
        pathlib.Path(a.json).write_text(json.dumps(doc, indent=1))
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
