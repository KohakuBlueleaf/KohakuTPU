"""Where a KohakuTPU unit word carries a memory address.

The project half of `Backend.addresses`: a package relocates each field found
here, so one compiled package serves every call whatever its buffers' addresses.

* Cluster ('MG'): FILL and memory DRAIN carry `addr` (34 bits) and `addr_hi`
  (6), one 40-bit address split in two (isa/cluster.py). A node-addressed
  DRAIN (`dnode`) carries an L1 word offset instead, which is not memory.
* Vector core ('VC'): a descriptor write (op 2) to field 0 is a base address,
  low 34 bits at [245:212] and high 6 at [68:63] (hw/vector.py `desc_flit`).
"""

from kohakuaccel.backend.slots import Backend
from kohakutpu.isa.cluster import ISA

_CU_LO = ISA.FILL.span("addr")
_CU_HI = ISA.FILL.span("addr_hi")
_CU_LO_BITS = ISA.cfg.addr_bits

#: Vector descriptor base: (bit, width) of the two halves, and the op/field codes.
_VC_LO, _VC_HI, _VC_SPLIT = (212, 34), (63, 6), 34
_VC_OP_DESC, _VC_FLD_BASE = 2, 0


def cluster_addresses(word: int) -> list:
    """``[(segments, address)]`` of one cluster word."""
    fmt = ISA.set.find(word)
    if fmt is None or fmt.name == "GEMM":
        return []
    f = fmt.decode(word)
    if fmt.name == "DRAIN" and f["dnode"]:
        return []
    addr = f["addr"] | f["addr_hi"] << _CU_LO_BITS
    return [(((*_CU_LO, 0), (*_CU_HI, _CU_LO_BITS)), addr)]


def vector_addresses(word: int) -> list:
    """``[(segments, address)]`` of one vector-core word."""
    if (word >> 252) & 0xF != _VC_OP_DESC or (word >> 246) & 0x7 != _VC_FLD_BASE:
        return []
    lo = (word >> _VC_LO[0]) & ((1 << _VC_LO[1]) - 1)
    hi = (word >> _VC_HI[0]) & ((1 << _VC_HI[1]) - 1)
    return [(((*_VC_LO, 0), (*_VC_HI, _VC_SPLIT)), lo | hi << _VC_SPLIT)]


class TpuFields(Backend):
    """KohakuTPU's answer to `Backend.addresses`; encodes nothing itself."""

    def encode(self, task, ctx) -> list[int]:
        raise NotImplementedError("TpuFields reads words; kohakutpu.lang encodes them")

    def addresses(self, word: int, unit_type: str) -> list:
        if unit_type == "MG":
            return cluster_addresses(word)
        if unit_type == "VC":
            return vector_addresses(word)
        return []


FIELDS = TpuFields()
