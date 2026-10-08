"""Put a firmware image and its boot block on a node, and start it.

Two ways to place the image, one result:

* ``axi``  -- stream it through the node's load window (`rv64load`), which is
  what the card does: one write per instruction word.
* ``burn`` -- the simulated card only: write the imem/spad arrays directly
  (`VerilatorTransport.poke`), then boot through the window the same way.

The boot block (`args.BootArgs`) goes to the scratchpad's base AFTER the image,
because the image's own `.bootargs` section is zeros.
"""

import pathlib
import time

from kohakuaccel.device import rv64load
from kohakuaccel.node.boot.args import SPAD_OFFSET, BootArgs

MODES = ("axi", "burn")


def cpu_scope(sim, mesh: int) -> str:
    """The node's rv64_syscore scope in a Verilated card, found by its `spad`.

    Raises :class:`RuntimeError` when the model exposes no such array
    (sim/verilator/card.vlt makes them public).
    """
    found = [
        s for s, v, _, _ in sim.arrays(f"u_mesh{mesh}.u_mag.u_pe.u_cpu") if v == "spad"
    ]
    if not found:
        raise RuntimeError(f"the model exposes no spad array for mesh {mesh}")
    return found[0]


class NodeBoot:
    """One node's processor, seen through its load window."""

    def __init__(self, transport, ctrl_base: int, mesh: int, sim=None) -> None:
        self.t = transport
        self.mesh = mesh
        self.win = rv64load.LoadWindow(transport, ctrl_base + rv64load.WINDOW_OFFSET)
        #: The Verilated card, for ``burn``; None on a real card.
        self.sim = sim
        self.info: dict = {}

    def load(self, elf: pathlib.Path | str, args: BootArgs, mode: str = "axi") -> dict:
        """Stop the core, place the image and the boot block, and start it.

        Returns the image's sizes and the seconds each part took. Raises
        :class:`ValueError` for an unknown mode or ``burn`` with no model.
        """
        if mode not in MODES:
            raise ValueError(f"load mode must be one of {MODES}, not {mode!r}")
        t0 = time.monotonic()
        self.win.stop()
        if mode == "burn":
            if self.sim is None:
                raise ValueError("burn writes the model's arrays; this card has none")
            scope = cpu_scope(self.sim, self.mesh)
            info = self.win.burn_elf(elf, self.sim, scope)
            self.sim.poke(scope, "spad", SPAD_OFFSET // 8, args.pack())
        else:
            info = self.win.load_elf(elf)
            self.win.write_spad(SPAD_OFFSET, args.pack())
        t_load = time.monotonic() - t0
        self.win.boot()
        self.info = {**info, "mode": mode, "load_seconds": t_load}
        return self.info

    def status(self) -> dict:
        """HR_STATUS decoded, plus the exit word once the core has stopped."""
        st = self.win.status()
        out = {"exited": bool(st & 0x8), "halted": bool(st & 0x4), "cause": st & 0x3}
        if st & 0xC:
            out["exit"] = self.t.read64(self.win.base + rv64load.R_EXIT)
            out["halt_pc"] = self.t.read64(self.win.base + rv64load.R_HALTPC)
        return out

    def console(self) -> str:
        """Bytes waiting in the load window's console FIFO (256 deep)."""
        return self.win.console()
