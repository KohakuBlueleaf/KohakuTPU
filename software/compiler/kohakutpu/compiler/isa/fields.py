"""Where a KohakuTPU unit word carries a memory address: the `addresses`
function `Program.build` relocates with.

* Cluster ('MG'): FILL, memory DRAIN and an emitting GEMM (`emit`, the fused
  drain's sweep) carry `addr` and `addr_hi`, one 40-bit address split in two.
  A node-addressed word (`dnode`) carries an L1 word offset instead, which is
  not memory.
* Vector core ('VC'): a DESC to field 0 is a base address, `value` and
  `value_hi`.
* Streaming Row Unit ('SR'): LOAD and STORE `addr`; RUN `aaddr` when A is
  from memory, `oaddr` when the output goes to memory, `baddr` when set.
* The mover: every walker header's base (`kohakuaccel.compiler.package.mover`).
"""

from kohakuaccel.compiler.package import mover
from kohakutpu.compiler.isa import sru as SR
from kohakutpu.compiler.isa.cluster import ISA as CU
from kohakutpu.compiler.isa.vector import ISA as VEC


def cluster_addresses(word: int) -> list:
    """``[(segments, address)]`` of one cluster word."""
    fmt = CU.set.find(word)
    if fmt is None:
        return []
    f = fmt.decode(word)
    if f["dnode"] or (fmt.name == "GEMM" and not f["emit"]):
        return []
    lo, hi = fmt.span("addr"), fmt.span("addr_hi")
    addr = f["addr"] | f["addr_hi"] << CU.cfg.addr_bits
    return [(((*lo, 0), (*hi, CU.cfg.addr_bits)), addr)]


def vector_addresses(word: int) -> list:
    """``[(segments, address)]`` of one vector-core word."""
    if not VEC.DESC.matches(word):
        return []
    f = VEC.DESC.decode(word)
    if f["fld"] != 0:
        return []
    split = VEC.cfg.desc_value_bits
    lo, hi = VEC.DESC.span("value"), VEC.DESC.span("value_hi")
    return [(((*lo, 0), (*hi, split)), f["value"] | f["value_hi"] << split)]


def row_addresses(word: int) -> list:
    """``[(segments, address)]`` of one Streaming Row Unit word."""
    fmt = SR.ISA.find(word)
    if fmt is None:
        return []
    f = fmt.decode(word)
    names = []
    if fmt.name in ("LOAD", "STORE"):
        names = ["addr"]
    elif fmt.name == "RUN":
        names = [
            n
            for n, on in (
                ("aaddr", f["amem"]),
                ("oaddr", not f["peer"]),
                ("baddr", f["baddr"]),
            )
            if on
        ]
    return [(((*fmt.span(n), 0),), f[n]) for n in names]


def addresses(word: int, unit_type: str) -> list:
    """Every address field of `word`, a payload for a unit of `unit_type`."""
    if unit_type == "MG":
        return cluster_addresses(word)
    if unit_type == "VC":
        return vector_addresses(word)
    if unit_type == "SR":
        return row_addresses(word)
    if unit_type == "mover":
        return mover.addresses(word, unit_type)
    return []


__all__ = ["addresses", "cluster_addresses", "row_addresses", "vector_addresses"]
