"""What a kernel parameter or a scratch buffer is: shape, element type, layout
and memory space, printed as an L2 parameter type."""

from dataclasses import dataclass, field

#: Element types a memory buffer may hold (`layout.buffer.DTYPES` plus MXFP7).
DTYPES = ("f16", "f32", "mx7a", "mx7b", "u8", "u32", "i32", "u64")
#: Memory spaces a buffer may live in.
SPACES = ("dram", "stage", "mem", "any")
#: Layout keys, by element type (`layout.buffer.Buffer.check`).
LAYOUT_KEYS = {"mx7a": {"gm", "nk"}, "mx7b": {"gn", "nk"}}
ITEM = {"f16": 2, "f32": 4, "u8": 1, "u32": 4, "i32": 4, "u64": 8}


@dataclass(frozen=True)
class TensorSpec:
    """A buffer: `shape` (ints), `dtype`, `layout` (``gm``/``gn``/``nk`` of
    an MXFP7 packing, ``tile`` of drained sub-tiles, ``t`` transposed slabs,
    ``node`` a staging store), `space`."""

    shape: tuple
    dtype: str = "f16"
    layout: tuple = field(default=())  # sorted (key, value) pairs
    space: str = "dram"

    @classmethod
    def make(cls, shape, dtype="f16", layout=None, space="dram") -> "TensorSpec":
        lay = tuple(sorted((layout or {}).items()))
        spec = cls(tuple(int(d) for d in shape), dtype, lay, space)
        spec.check()
        return spec

    @property
    def layout_dict(self) -> dict:
        return dict(self.layout)

    def check(self) -> None:
        if self.dtype not in DTYPES:
            raise ValueError(f"no element type {self.dtype!r}: {', '.join(DTYPES)}")
        if self.space not in SPACES:
            raise ValueError(f"no space {self.space!r}: {', '.join(SPACES)}")
        lay = self.layout_dict
        want = LAYOUT_KEYS.get(self.dtype)
        if want is not None and set(lay) - {"t", "node"} != want:
            raise ValueError(
                f"{self.dtype} wants layout {sorted(want)}, got {sorted(lay)}"
            )
        if any(d <= 0 for d in self.shape):
            raise ValueError(f"a buffer's extents are positive, got {self.shape}")

    @property
    def tiled(self) -> bool:
        return "tile" in self.layout_dict

    @property
    def rowmajor(self) -> bool:
        return not (set(self.layout_dict) - {"node"}) and self.dtype in ITEM

    def nbytes(self) -> int:
        n = 1
        for d in self.shape:
            n *= d
        return n if self.dtype.startswith("mx7") else n * ITEM[self.dtype]

    def text(self) -> str:
        """``f16[512, 128] @dram(tile=64x64)``, as an L2 parameter type."""
        dims = ", ".join(str(d) for d in self.shape)
        args = []
        for k, v in self.layout:
            if k == "tile":
                args.append(f"tile={v[0]}x{v[1]}")
            else:
                args.append(f"{k}={v}")
        space = f"@{self.space}" + (f"({', '.join(args)})" if args else "")
        return f"{self.dtype}[{dims}] {space}"

    def with_(self, **kw) -> "TensorSpec":
        d = {
            "shape": self.shape,
            "dtype": self.dtype,
            "layout": self.layout_dict,
            "space": self.space,
        }
        d.update(kw)
        return TensorSpec.make(**d)


@dataclass(frozen=True)
class Machine:
    """What a kernel may read about the machine it is traced for, every field
    a machine-file fact (`of`)."""

    target: str  # die.target
    mg: int  # matmul clusters a node (mg.count)
    vc: int  # vector cores a node (vc.count)
    nodes: int
    lanes: int  # lanes a chunk, values a word (vc.lanes)
    chunk: int  # chunks a vector register (vc.chunks)
    sig_bits: int  # vector lane significand, including the implicit one
    packing_backend: str  # conversion.fp16_mx7.preferred_backend

    @classmethod
    def of(cls, m, nodes: int = 1) -> "Machine":
        """The machine file `m`'s (`kohakutpu.language.machine`), over `nodes`."""
        return cls(
            target=m.die.target,
            mg=m.mg.count,
            vc=m.vc.count,
            nodes=nodes,
            lanes=m.vc.lanes,
            chunk=m.vc.chunks,
            sig_bits=m.vc.sig_bits,
            packing_backend=m.conversion.fp16_mx7.preferred_backend,
        )

    def total(self, kind: str) -> int:
        """Units of `kind` ("mg" / "vc") over every node: what a par of
        them spans when the nodes share memory (`kt.par`)."""
        return getattr(self, kind) * self.nodes


__all__ = ["DTYPES", "ITEM", "SPACES", "Machine", "TensorSpec"]
