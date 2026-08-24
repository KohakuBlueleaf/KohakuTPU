"""The MAG staging store as an allocatable tier, and a model that serves it.

Level 2. `mag_stage.v` decodes
``addr[39] && !addr[38] && addr[37:36]==MESH && addr[35:32]==AP_STAGE``, so the
L2 is reached BY ADDRESS and never by an instruction; `machinespec.stage_addr`
forms it and this hands out the 2 MB behind it.

THE HOST CANNOT REACH THIS STORE AT THE UNIT'S ADDRESS. Measured on v7 silicon
2026-08-23: a raw `read64` at `stage_addr(0x40000)` is refused and takes the JTAG
path down with it, because the host arrives through `S_AXI_MEM` and the station
bus decodes its own 43-bit map, in which bit 39 of a unit address means nothing.
`mag_stage_port` sits on the CONVERGED path inside the agent, so only a unit --
or the host through a window the block design maps onto the aperture -- reaches
it. Do not probe an aperture address with the raw transport; it costs a reprogram.

The tier itself is verified against a card: `attach` gives a real `Device` an
arena at `0x8000000000` and allocations land at aperture 0 while DRAM allocations
stay at aperture None. What is still untested is a unit ISSUING one.
"""

from kohakuaccel.machinespec import AP_STAGE, STAGE_BYTES, MachineSpec
from kohakuaccel.memory import Arena
from kohakuaccel.sim import MEM_BASE, MEM_SIZE, Memory

#: One L1 entry. Finer than a batch, because nothing staged here is a vector
#: pass's own operand -- the runs address it by word.
STAGE_ALIGN = 256


def stage_arena(machine: MachineSpec, mesh: int | None = None) -> Arena:
    """An arena over one mesh's staging store."""
    return Arena(machine.stage_addr(0, mesh), STAGE_BYTES, align=STAGE_ALIGN, mesh=mesh)


class StagedMemory(Memory):
    """A simulated memory that also answers for the staging aperture.

    A unit model reads at ``addr - mem_base``, so an aperture address arrives
    here as an offset far past the DRAM window; without this it reads short and
    the model reports "past the memory window" rather than serving the store the
    silicon has.
    """

    def __init__(self, size: int = MEM_SIZE, mem_base: int = MEM_BASE) -> None:
        super().__init__(size)
        self.mem_base = mem_base
        self.stage = bytearray(STAGE_BYTES)

    def _where(self, off: int) -> tuple:
        addr = off + self.mem_base
        if MachineSpec.addr_aperture(addr) != AP_STAGE:
            return self.buf, off
        # The row and bank fields stop at bit 20, so a larger offset WRAPS onto
        # another entry rather than faulting -- `machinespec.stage_addr` refuses
        # to form one, and this is what the hardware would do with it anyway.
        return self.stage, addr & (STAGE_BYTES - 1)

    def read(self, addr: int, nbytes: int) -> bytes:
        buf, at = self._where(addr)
        return bytes(buf[at : at + nbytes])

    def write(self, addr: int, data: bytes) -> None:
        buf, at = self._where(addr)
        buf[at : at + len(data)] = data


def attach(device) -> object:
    """Give `device` a staging tier, and its model a store to serve it from.

    Returns the device. On a simulated machine the memory is replaced with one
    that decodes the aperture; on a card there is nothing to replace, and the
    silicon either answers or does not -- which is the thing this cannot test.
    """
    device.staging = stage_arena(device.machine)
    card = getattr(device, "card", None)
    mem = getattr(card, "mem", None)
    if mem is not None and not isinstance(mem, StagedMemory):
        held = StagedMemory(len(mem.buf))
        held.buf = mem.buf
        card.mem = held
    return device
