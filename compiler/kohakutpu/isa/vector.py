"""KohakuTPU's vector-core ISA, declared as a field table.

Three flit kinds reach a vector core from the mesh -- load a word of instruction
memory, set a descriptor field, start at a program counter -- and the kernel
itself is the words IMEM carries. That split is why a vector core is programmed
rather than commanded: the cluster takes one instruction per dispatch, the vector
core takes a program and runs it.

The authority is ``src/ktpu/hw/vector.py``, which has run on silicon.
``compiler/tests/test_ktpu_isa.py`` checks this against it bit for bit.
"""

from dataclasses import dataclass

from kohakuaccel.backend import Field, InstFormat, InstSet

OP_IMEM = 1
OP_DESC = 2
OP_RUN = 3


@dataclass(frozen=True)
class VecConfig:
    """Field widths and machine constants the vector encoding depends on.

    `vlmax` is the longest vector one instruction may name and `lanes` how many
    elements the datapath retires per cycle; both are the core's, not the mesh's.
    """

    payload_bits: int = 256
    op_bits: int = 4
    addr_bits: int = 9
    word_bits: int = 32
    desc_sel_bits: int = 3
    desc_value_bits: int = 34

    vlmax: int = 128
    lanes: int = 16
    word_bytes: int = 32


DEFAULT = VecConfig()


class VecIsa:
    """The three flit kinds a vector core accepts, for one :class:`VecConfig`."""

    def __init__(self, cfg: VecConfig = DEFAULT) -> None:
        self.cfg = cfg
        pad_imem = cfg.payload_bits - cfg.op_bits - cfg.addr_bits - cfg.word_bits
        self.IMEM = InstFormat(
            "IMEM",
            [
                Field("op", cfg.op_bits, const=OP_IMEM),
                Field("addr", cfg.addr_bits, doc="instruction memory word index"),
                Field("_pad", pad_imem, default=0),
                Field("word", cfg.word_bits, doc="the kernel instruction itself"),
            ],
            width=cfg.payload_bits,
            doc="load one word of the core's instruction memory",
        )
        self.DESC = InstFormat(
            "DESC",
            [
                Field("op", cfg.op_bits, const=OP_DESC),
                Field("ad", cfg.desc_sel_bits, doc="which address descriptor"),
                Field("fld", cfg.desc_sel_bits, doc="which field of it"),
                Field("value", cfg.desc_value_bits),
            ],
            width=cfg.payload_bits,
            doc="set one field of one address descriptor",
        )
        self.RUN = InstFormat(
            "RUN",
            [
                Field("op", cfg.op_bits, const=OP_RUN),
                Field("pc", cfg.addr_bits, default=0, doc="start program counter"),
            ],
            width=cfg.payload_bits,
            doc="start the loaded kernel",
        )
        self.set = InstSet("kohakutpu-vec", [self.IMEM, self.DESC, self.RUN])

    def imem(self, addr: int, word: int) -> int:
        """One instruction-memory word."""
        return self.IMEM.encode(addr=addr, word=word)

    def desc(self, ad: int, fld: int, value: int) -> int:
        """One descriptor field."""
        return self.DESC.encode(ad=ad, fld=fld, value=value)

    def run(self, pc: int = 0) -> int:
        """Start the kernel at `pc`."""
        return self.RUN.encode(pc=pc)

    def program(self, words, pc: int = 0) -> list[int]:
        """A whole kernel: every IMEM word in order, then RUN.

        Descriptors are not included -- they change per invocation while the
        kernel does not, which is the reason they are separate flit kinds.
        """
        return [self.imem(i, w) for i, w in enumerate(words)] + [self.run(pc)]


ISA = VecIsa()
