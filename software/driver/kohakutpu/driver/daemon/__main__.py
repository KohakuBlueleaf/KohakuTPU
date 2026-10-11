"""Start the card daemon for a KohakuTPU board.

    python -m kohakutpu.driver.daemon --board multimesh_v65
    python -m kohakutpu.driver.daemon --board multimesh_v65 --backend model
    python -m kohakutpu.driver.daemon --board multimesh_v8t8 --backend verilator \\
        --build build/sw/vlt_card_v8t8_2n

This CLI is where the framework daemon meets the project: the daemon library
knows no clock wizard, so this wires the board's `CardClocks` in as the
governor's clock control, and the board's address map in as the node queues'
unit-global memory and firmware loader (node-queue.md §9).
`--backend model` runs the whole daemon against an in-memory card;
`--backend verilator` against the Verilated card, ungoverned.
"""

import argparse

from kohakuaccel.driver.daemon.server import DEFAULT_PORT, Daemon
from kohakuaccel.driver.node.boot import NodeBoot
from kohakuaccel.driver.transport.jtag import JtagTransport
from kohakuaccel.driver.transport.memory import MemoryTransport
from kohakuaccel.driver.transport.rebase import UnitGlobal
from kohakuaccel.driver.transport.verilator import VerilatorTransport
from kohakutpu.driver.clock.card import CardClocks, load_board
from kohakutpu.driver.clock.mmcm import REG_STATUS, STATUS_LOCKED
from kohakutpu.driver.host import board_map


class WizardClockCtl:
    """CardClocks as the governor's injected adapter."""

    def __init__(self, transport, board: dict) -> None:
        self.card = CardClocks(transport, board)
        self.nmesh = board["meshes"]
        self.profiles = board["profiles"]
        self.levels = sorted(self.profiles)
        self.idle_level = board.get("idle_profile", "low")

    def apply(self, mesh: int, level: str) -> dict:
        return self.card.mesh(mesh).set_profile(self.profiles[level])

    def read(self, mesh: int) -> dict:
        return self.card.mesh(mesh).read_all()


def model_transport(board: dict) -> MemoryTransport:
    """An in-memory card whose wizards are always locked: the status
    word intercepts as LOCKED so `load()` returns on its first poll."""
    stride = int(board["ctrl_stride"], 16)
    wbase = int(board["wizard_base"], 16)
    hooks = {}
    for i in range(board["meshes"]):
        word = (wbase + i * stride + REG_STATUS) & ~7
        shift = 32 if (wbase + i * stride + REG_STATUS) & 4 else 0
        hooks[word] = lambda n, s=shift: STATUS_LOCKED << s
    return MemoryTransport(on_read=hooks)


def node_wiring(transport, board: dict, sim=None):
    """`(node_mem, node_loader)` from the board's address map, or (None, None)
    for a board whose map names no per-node DRAM, staging and control."""
    try:
        m = board_map(board)
        mem = UnitGlobal(transport, m["dram"], m["mem"], m["size"], staging=True)
    except (KeyError, TypeError, ValueError):
        return None, None

    def loader(node: int, elf, args, mode: str) -> dict:
        if mode == "burn" and sim is None:
            raise ValueError("burn writes a model's arrays; this backend has none")
        return NodeBoot(transport, m["ctrl"][node], node, sim=sim).load(elf, args, mode)

    return mem, loader


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--board", required=True, help="boards/<name>.json")
    ap.add_argument("--backend", choices=["jtag", "model", "verilator"], default="jtag")
    ap.add_argument("--build", help="--backend verilator: the model's build directory")
    ap.add_argument(
        "--poll-cycles",
        type=int,
        default=600,
        help="--backend verilator: cycles the model advances between two node polls",
    )
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--idle-seconds", type=float, default=10.0)
    ap.add_argument("--no-governor", action="store_true")
    ap.add_argument("--allow-program", action="store_true")
    args = ap.parse_args()

    board = load_board(args.board)
    sim = poll_idle = None
    if args.backend == "model":
        transport = model_transport(board)
    elif args.backend == "verilator":
        transport = sim = VerilatorTransport(build_dir=args.build)

        def poll_idle():
            transport.run(args.poll_cycles)

    else:
        transport = JtagTransport()
        beats = board.get("max_burst_beats")
        width = getattr(transport, "beat_bytes", None)
        if beats and width:
            transport.max_block = min(transport.max_block, beats * width)

    governed = not args.no_governor and args.backend != "verilator"
    ctl = WizardClockCtl(transport, board) if governed else None
    node_mem, node_loader = node_wiring(transport, board, sim)
    daemon = Daemon(
        transport,
        board=board,
        clock_ctl=ctl,
        idle_seconds=args.idle_seconds,
        allow_program=args.allow_program,
        port=args.port,
        node_mem=node_mem,
        node_loader=node_loader,
        poll_idle=poll_idle,
    )
    port = daemon.start()
    print(
        f"kohaku daemon: {board['name']} via {args.backend} on 127.0.0.1:{port}",
        flush=True,
    )
    try:
        daemon.serve_forever()
    finally:
        daemon.stop()
        print("kohaku daemon: closed")


if __name__ == "__main__":
    main()
