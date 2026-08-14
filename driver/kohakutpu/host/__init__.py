"""Host-side helpers for driving a KohakuTPU card.

These are driver code, not example code. An example that opens a card, packs an
operand and unpacks a drained tile is mostly plumbing, and the plumbing is the
same every time -- so it lives here and the example stays about the network.

A card carries SEVERAL MESHES, each with its own control window, its own units
and its own DRAM. :class:`Mesh` is one of them and holds everything that used to
be on :class:`Card`; `Card` holds the set and forwards to a default mesh, so code
written when there was only one keeps working.
"""

import numpy as np
from kohakuaccel.device import (
    enumerate_mesh,
    find_control_window,
    find_control_windows,
    read_agent_caps,
)
from kohakuaccel.device.registers import A_CAPS
from kohakuaccel.transport.jtag import JtagTransport
from kohakuaccel.transport.rebase import Rebased, Window
from kohakutpu.hw import tensor as T

WORD_BYTES = 32

#: Windows a block design might have assigned. The board files in this
#: repository do not agree, so the card is asked rather than trusted.
CANDIDATES = (0x4_0080_0000, 0x4_0000_0000, 0x8000_0000, 0x1000_0000, 0x0)

#: One control window per mesh, 64 KiB apart. Which of them answer is a property
#: of the bitstream, so these are candidates too.
CONTROL_WINDOWS = (0x4_0080_0000, 0x4_0081_0000, 0x4_0082_0000, 0x4_0083_0000)

#: Mesh id is `addr[37:36]`. **On a pre-v5 bitstream, which decoded `addr[33:32]`,
#: this stride reads mesh 0's DRAM for every mesh -- wrong data, no error.**
MESH_SHIFT = 36
MEMORY_WINDOWS = tuple(i << MESH_SHIFT for i in range(4))

#: What the card HAS, not what the space allows: 4 GB populated per 64 GB window.
#: Verified -- markers at 0, 256 MB, 1, 2, 3 GB and each window's last word differ.
MEMORY_SIZE = 1 << 32

#: The driver's own scratch, in the LAST megabyte of each mesh's DRAM. Calibration
#: destroys whatever is here, so it sits where no allocator grows into it.
SCRATCH_RESERVE = 1 << 20
SCRATCH = MEMORY_SIZE - SCRATCH_RESERVE

#: The address an allocator over a mesh's DRAM must stop below. Imported rather
#: than restated, so the reservation above cannot drift from what respects it.
ARENA_LIMIT = SCRATCH

#: What A_CAPS reports on every build of this mesh, which is what makes it a
#: read-skew reference that needs no write.
FLIT_WIDTH = 288


def _is_caps(word: int) -> bool:
    """Whether `word` decodes as a plausible A_CAPS: known flit width and grid."""
    return (word & 0xFFFF) == FLIT_WIDTH and 0 < ((word >> 16) & 0xFF) <= 8


class Mesh:
    """One mesh: its control window, its own DRAM, and its enumerated units.

    `ctrl` carries the driver's logical register map onto wherever the agent
    landed; `mem` addresses this mesh's DRAM from zero. `raw` is the whole card
    and is what calibration and cross-mesh access use. `dram_base` is the same
    memory in the GLOBAL space a unit issues into, which is what an allocator
    feeding instructions must build on.

    Raises :class:`RuntimeError` if the host window and the global base disagree.
    """

    def __init__(
        self,
        raw,
        index: int,
        ctrl_base: int,
        mem_base: int,
        mem_size: int = MEMORY_SIZE,
    ) -> None:
        self.raw = raw
        self.index = index
        self.base, self.mem_base, self.mem_size = ctrl_base, mem_base, mem_size
        if mem_base != self.dram_base:
            raise RuntimeError(
                f"mesh_{index}'s host window is at {mem_base:#x} but a unit on it "
                f"issues {self.dram_base:#x}; separate the two before using this "
                f"board, or the same address means two different bytes"
            )
        self.ctrl = Rebased(raw, ctrl_base)
        self.mem = Window(raw, mem_base, mem_size)
        self.caps = read_agent_caps(self.ctrl)
        self.endpoints = enumerate_mesh(self.ctrl, self.caps)

    @property
    def dram_base(self) -> int:
        """This mesh's DRAM in the global space, from its index alone.

        A unit's memory request names its mesh in `addr[33:32]`, so an address
        with them clear reads as mesh 0 wherever it is issued -- which is why an
        ordinary matmul off mesh_0 raises `IL_F_RD_REMOTE` and is right anyway,
        the request aliasing to local DRAM rather than routing.
        """
        return self.index << 32

    @property
    def agent(self) -> tuple:
        """Where a drain's acknowledgement must be addressed, from the card's grid.

        A CONVENTION, not a reading: `A_CAPS` reports the router grid and not
        ORC_X/ORC_Y, so no register says this. Memory port `p` sits west of row
        `p + 1` and the agent answers at port 0's, which puts it one column left
        of the grid on the grid's first row.

        Measured on multimesh_v3: a node drain acked here is counted by
        `A_SIG_DONE`, and one acked to `(0, 0)` is not -- that is the corner
        meaning "answer the sender", which drops it.
        """
        return (self.caps.grid_lo - 1, self.caps.grid_lo)

    def coords(self, unit_type: str) -> tuple:
        """Where units of `unit_type` are on this mesh, in scan order."""
        return tuple(e.coord for e in self.endpoints if e.name == unit_type)

    def idle(self, unit_type: str) -> tuple:
        """Units of `unit_type` that are not currently executing.

        A unit left running by an earlier program never retires the work sent to
        it, so dispatching there waits forever. Reads CU_STATUS per unit.
        """
        from kohakuaccel.device import CU_STATUS, control_read, decode_status

        out = []
        for coord in self.coords(unit_type):
            word = control_read(self.ctrl, coord, CU_STATUS)
            if word is not None and not decode_status(word)["busy"]:
                out.append(coord)
        return tuple(out)

    def verify_write_path(self, addr: int = SCRATCH, nwords: int = 8) -> None:
        """Write a pattern to this mesh's DRAM and read it back. Raises if they differ.

        The write path can skew silently and still report success, so a run that
        skips this reports wrong numbers as if they were right.
        """
        want = bytes((0x5A ^ i) for i in range(nwords * 8))
        self.mem.write_block(addr, want)
        got = self.mem.read_block(addr, len(want))
        if got != want:
            raise RuntimeError(
                f"mesh_{self.index}: write path is not clean at {addr:#x} "
                f"(card {self.mem_base + addr:#x}): wrote {want.hex()}, "
                f"read {got.hex()}"
            )

    def upload(self, byte_addr: int, x: np.ndarray) -> int:
        """Pad, pack and write one ``[lanes][K]`` FP16 operand; returns bytes."""
        words = T.to_fp16_words(T.pad_operand(x))
        blob = b"".join(w.to_bytes(WORD_BYTES, "little") for w in words)
        self.mem.write_block(byte_addr, blob)
        return len(blob)

    def read_tile(self, byte_addr: int, gm: int, gn: int) -> np.ndarray:
        """One cluster's drained block as a ``[gm*4][gn*4]`` array."""
        blob = self.mem.read_block(byte_addr, gm * gn * WORD_BYTES)
        words = [
            int.from_bytes(blob[i : i + WORD_BYTES], "little")
            for i in range(0, len(blob), WORD_BYTES)
        ]
        return T.unpack_c(words, gm * 4, gn * 4, gn)

    def gather(self, meta: dict) -> np.ndarray:
        """Reassemble ``C[m][n]`` from every cluster's drained block.

        `meta` is what the compiler recorded on the artifact: the problem shape,
        the sub-tile counts, where C starts, and each cluster's column slice.
        """
        m, n, gm = meta["m"], meta["n"], meta["gm"]
        out = np.zeros((m, n))
        for gn0, gn in (tuple(s) for s in meta["slices"]):
            tile = self.read_tile(meta["c_byte"] + gn0 * gm * WORD_BYTES, gm, gn)
            out[:, gn0 * 4 : (gn0 + gn) * 4] = tile[:m, : gn * 4]
        return out

    def __repr__(self) -> str:
        from kohakuaccel.device import by_type

        kinds = ", ".join(f"{k}x{len(v)}" for k, v in by_type(self.endpoints).items())
        return (
            f"Mesh({self.index}, ctrl={self.base:#x}, "
            f"mem={self.mem_base:#x}+{self.mem_size:#x}, {kinds or 'empty'})"
        )


class Card:
    """An attached KohakuTPU, with every mesh on it already enumerated.

    `meshes` is all of them; `mesh` is the one this card forwards to, so
    ``card.coords``, ``card.upload`` and the rest keep meaning what they meant
    when a card had one mesh. `raw` addresses the card directly.
    """

    def __init__(
        self, transport=None, verify: bool = True, which=None, realign: bool = False
    ) -> None:
        self.raw = transport or JtagTransport()
        # Reads first, and from a known register: a write lag measured as a read
        # skew cancels itself on a round trip and hides in every check there is.
        self._calibrate_reads()
        preflight = getattr(self.raw, "verify_write_path", None)
        if preflight is not None:
            preflight(SCRATCH, realign=realign)
        self.meshes = tuple(self._discover(which))
        if not self.meshes:
            raise RuntimeError("no agent answered at any candidate window")
        self.mesh = self.meshes[0]
        if verify:
            self.mesh.verify_write_path()

    def _calibrate_reads(self) -> None:
        """Set the transport's read skew from a register whose value is known.

        `A_CAPS` is the one register with a right answer known before anything
        has run, so it fixes the skew with no write involved. Raises
        :class:`RuntimeError` if no candidate window answers at any offset.
        """
        probe = getattr(self.raw, "measure_read_skew", None)
        if probe is None:
            return
        for base in CONTROL_WINDOWS + CANDIDATES:
            skew = probe(base + A_CAPS, _is_caps)
            if skew is not None:
                self.raw.read_skew = skew
                return
        raise RuntimeError(
            "no candidate window answered A_CAPS at any read offset, so the read "
            "skew is unknown and every register read would be displaced"
        )

    def _discover(self, which) -> list:
        """Build a :class:`Mesh` per control window that answers.

        `which` restricts the scan to those mesh indices, which is worth doing
        over JTAG: enumerating one mesh is several hundred accesses.
        """
        wanted = range(len(CONTROL_WINDOWS)) if which is None else which
        windows = [CONTROL_WINDOWS[i] for i in wanted]
        found = find_control_windows(self.raw, windows)
        if not found:
            # A board that put the one mesh somewhere else entirely. Its memory
            # is the whole card, since there is no second mesh to collide with.
            base = find_control_window(self.raw, CANDIDATES)
            return [] if base is None else [Mesh(self.raw, 0, base, 0x0, None)]
        out = []
        for base in found:
            i = CONTROL_WINDOWS.index(base)
            out.append(Mesh(self.raw, i, base, MEMORY_WINDOWS[i], MEMORY_SIZE))
        return out

    @property
    def base(self) -> int:
        """The default mesh's control window."""
        return self.mesh.base

    @property
    def ctrl(self):
        """The default mesh's register map."""
        return self.mesh.ctrl

    @property
    def endpoints(self) -> list:
        """The default mesh's units."""
        return self.mesh.endpoints

    def select(self, index: int) -> "Mesh":
        """Make mesh `index` the one this card forwards to; returns it."""
        for mesh in self.meshes:
            if mesh.index == index:
                self.mesh = mesh
                return mesh
        raise ValueError(
            f"mesh {index} did not answer; this card has "
            f"{[m.index for m in self.meshes]}"
        )

    def all_coords(self, unit_type: str) -> tuple:
        """Every unit of `unit_type` on the card, as ``(mesh_index, coord)``.

        A coordinate ALONE DOES NOT NAME A UNIT once there is more than one mesh:
        every mesh has a (1,1), and they are different silicon on different DRAM.
        """
        return tuple(
            (mesh.index, coord)
            for mesh in self.meshes
            for coord in mesh.coords(unit_type)
        )

    def verify_all(self) -> None:
        """Check the write path into EVERY mesh's DRAM. Raises on the first bad one.

        Each mesh reaches its DRAM through its own AXI master, so a clean path to
        one says nothing about the others.
        """
        for mesh in self.meshes:
            mesh.verify_write_path()

    def coords(self, unit_type: str) -> tuple:
        """Where units of `unit_type` are on the default mesh, in scan order."""
        return self.mesh.coords(unit_type)

    def idle(self, unit_type: str) -> tuple:
        """Units of `unit_type` on the default mesh that are not executing."""
        return self.mesh.idle(unit_type)

    def verify_write_path(self, addr: int = SCRATCH, nwords: int = 8) -> None:
        """Check the write path into the default mesh's DRAM."""
        self.mesh.verify_write_path(addr, nwords)

    def upload(self, byte_addr: int, x: np.ndarray) -> int:
        """Pad, pack and write one operand to the default mesh; returns bytes."""
        return self.mesh.upload(byte_addr, x)

    def read_tile(self, byte_addr: int, gm: int, gn: int) -> np.ndarray:
        """One cluster's drained block from the default mesh."""
        return self.mesh.read_tile(byte_addr, gm, gn)

    def gather(self, meta: dict) -> np.ndarray:
        """Reassemble ``C[m][n]`` from the default mesh's drained blocks."""
        return self.mesh.gather(meta)

    def __repr__(self) -> str:
        return f"Card(default=mesh_{self.mesh.index}, {list(self.meshes)})"
