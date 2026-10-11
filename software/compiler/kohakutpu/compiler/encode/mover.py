"""The node's mover: one op a move, lowered to its register writes by
`kohakuaccel.compiler.package.mover`."""

from dataclasses import dataclass

from kohakuaccel.compiler.package import mover as PM

#: Transform ids of KohakuTPU's slot bank (src/kohakutpu/transform/xform_bank.v).
XF_BYPASS, XF_QUANT, XF_GT4 = 0, 1, 2
#: The quantiser's mode[0]: the clusters' B packing.
QUANT_B = 1


@dataclass(frozen=True)
class Quantise:
    """`entries` FP16 entries at `src` (eight words each) to MXFP7 at `dst`,
    in the A packing, or the B packing with `b`."""

    src: int
    dst: int
    entries: int
    b: bool = False

    def writes(self) -> list:
        return PM.convert(
            self.src, self.dst, self.entries, xform_id=XF_QUANT, mode=int(self.b)
        )


@dataclass(frozen=True)
class Transpose4:
    """`groups` groups of four words at `src`, each a 4 x 4 array of 64-bit
    granules, transposed to `dst`: out word i, granule w = in word w, granule i."""

    src: int
    dst: int
    groups: int

    def writes(self) -> list:
        return PM.convert(
            self.src, self.dst, self.groups, xform_id=XF_GT4, in_words=4, out_words=4
        )


@dataclass(frozen=True)
class Copy:
    """`nbytes` (whole words) from `src` to `dst`."""

    src: int
    dst: int
    nbytes: int

    def writes(self) -> list:
        return PM.copy(self.src, self.dst, self.nbytes)


@dataclass(frozen=True)
class Move:
    """Any one move, as its register writes (`PM.move`); `patches` set fields
    of them from registers (word = the write's index)."""

    regs: tuple
    patches: tuple = ()

    def writes(self) -> list:
        return list(self.regs)
