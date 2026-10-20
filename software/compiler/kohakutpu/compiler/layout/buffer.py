"""An L1 parameter's memory layout, from its type: what the host writes for an
input and how it reads an output back.

    f16[1024, 128] @dram                    row-major fp16
    mx7a[1024, 1024] @dram(gm=64, nk=2)     MXFP7, the clusters' A packing,
                                            tile-major (`tensor.pack_a`)
    mx7b[1024, 1024] @dram(gn=64, nk=2)     MXFP7, B packing (`tensor.pack_b`)
    f16[1024, 1024] @dram(tile=64x64)       drained sub-tiles: tiles of 64x64
                                            sub-tiles (256x256), row-major
                                            tiles, row-major sub-tiles in one;
                                            the clusters' fp16 (no subnormals,
                                            exponent 31 an ordinary one)
    f16[256, 128] @dram(tile=1x32, ieee=1)  the same tiling, IEEE fp16 values
                                            (what a row unit reads and writes)
    mx7b[64, 1024] @dram(gn=16, nk=2, t=1)  any of these holding the transpose
                                            of the host's [1024, 64] value

Leading dimensions are a batch: each [M, N] slab is laid out as above, slabs
one after another (`mx7a[10, 1024, 64]` is ten `mx7a[1024, 64]`); `t=1`
transposes each slab.
"""

from dataclasses import dataclass, field

import numpy as np

from kohakutpu.compiler.layout import tensor as T
from kohakutpu.language.text.syntax import Call, Dims, Int, Kw, Name, Placed, View

DTYPES = {
    "f16": np.float16,
    "f32": np.float32,
    "u8": np.uint8,
    "u32": np.uint32,
    "i32": np.int32,
    "u64": np.uint64,
}


@dataclass
class Buffer:
    dtype: str
    shape: tuple
    space: str = "dram"
    layout: dict = field(default_factory=dict)

    @classmethod
    def of(cls, t) -> "Buffer":
        space, layout = "dram", {}
        if isinstance(t, Placed):
            t, at = t.x, t.space
            if isinstance(at, Call):
                for a in at.args:
                    if not isinstance(a, Kw):
                        raise TypeError(f"a layout is NAME=VALUE, got {a!r}")
                    v = a.value
                    layout[a.name] = v.values if isinstance(v, Dims) else v.value
                at = Name(at.name)
            space = at.text
        if not isinstance(t, View) or not all(isinstance(d, Int) for d in t.slices):
            raise ValueError(f"an L1 buffer is DTYPE[constant dims], got {t!r}")
        b = cls(t.name, tuple(d.value for d in t.slices), space, layout)
        b.check()
        return b

    def check(self) -> None:
        want = {"mx7a": {"gm", "nk"}, "mx7b": {"gn", "nk"}}.get(self.dtype)
        if want is not None and set(self.layout) - {"t", "node"} != want:
            raise ValueError(f"{self.dtype} wants @dram({', '.join(sorted(want))}=...)")
        if self.dtype not in ("mx7a", "mx7b", *DTYPES):
            raise ValueError(f"no buffer dtype {self.dtype}")
        if set(self.layout) - {"gm", "gn", "nk", "tile", "ieee", "t", "node"}:
            raise ValueError(
                f"layout keys: gm gn nk tile ieee t node; got {sorted(self.layout)}"
            )
        if "ieee" in self.layout and "tile" not in self.layout:
            raise ValueError("ieee= reads a tile= layout's values as IEEE fp16")
        if "node" in self.layout and self.space != "stage":
            raise ValueError("node= places a buffer in that node's staging: @stage")
        if set(self.layout) - {"node"} and len(self.shape) < 2:
            raise ValueError(f"a {sorted(self.layout)} layout is of [.., M, N] slabs")

    @property
    def host_dtype(self):
        """What the host's values are before packing: fp16 for MXFP7."""
        return np.float16 if self.dtype.startswith("mx7") else DTYPES[self.dtype]

    @property
    def host_shape(self) -> tuple:
        """The host value's shape: the buffer's, each slab transposed under
        `t=1`."""
        if self.layout.get("t"):
            return (*self.shape[:-2], self.shape[-1], self.shape[-2])
        return self.shape

    def pack(self, x) -> bytes:
        x = np.asarray(x, self.host_dtype).reshape(self.host_shape)
        if self.layout.get("t"):
            x = np.ascontiguousarray(np.swapaxes(x, -1, -2))
        if len(self.shape) > 2 and set(self.layout) - {"t", "node"}:
            slabs = x.reshape(-1, *self.shape[-2:])
            return b"".join(self._pack(s) for s in slabs)
        return self._pack(x)

    def _pack(self, x) -> bytes:
        if self.dtype == "mx7a":
            return T.pack_a(x, self.layout["gm"], self.layout["nk"])
        if self.dtype == "mx7b":
            return T.pack_b(x, self.layout["gn"], self.layout["nk"])
        if "tile" in self.layout:
            return self._tiled(x)
        return np.ascontiguousarray(x).tobytes()

    @property
    def nbytes(self) -> int:
        n = int(np.prod(self.shape))
        if self.dtype.startswith("mx7"):
            return n  # one entry (128 B) per 128 values
        return n * np.dtype(DTYPES[self.dtype]).itemsize

    def unpack(self, raw: bytes) -> np.ndarray:
        """float64 values in the host's shape."""
        out = self._unpack(raw)
        return np.swapaxes(out, -1, -2) if self.layout.get("t") else out

    def _unpack(self, raw: bytes) -> np.ndarray:
        if self.dtype.startswith("mx7"):
            raise ValueError("an MXFP7 buffer is read back through its fp16 source")
        if "tile" in self.layout:
            gm, gn = self.layout["tile"]
            m, n = self.shape[-2:]
            slab = m * n * 2
            ieee = bool(self.layout.get("ieee"))
            slabs = [
                T.unpack_tiles(raw[i : i + slab], m, n, gm, gn, ieee)
                for i in range(0, len(raw), slab)
            ]
            return np.stack(slabs).reshape(self.shape)
        return (
            np.frombuffer(raw, DTYPES[self.dtype])
            .astype(np.float64)
            .reshape(self.shape)
        )

    def _tiled(self, x) -> bytes:
        gm, gn = self.layout["tile"]
        m, n = self.shape[-2:]
        tiles = x.reshape(m // (4 * gm), gm, 4, n // (4 * gn), gn, 4)
        return np.ascontiguousarray(tiles.transpose(0, 3, 1, 4, 2, 5)).tobytes()


__all__ = ["Buffer"]
